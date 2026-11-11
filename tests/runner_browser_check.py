#!/usr/bin/env python3
"""Automatic isolated Orca browser scenario. Exit nonzero if Orca is unavailable."""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from team_runner.orca import call as real_orca
from team_runner.runner_store import read_json, atomic_json, running, lock
from team_runner.runner_entry import process_stamp
from team_runner.catalog import Catalog
from team_runner.runner_config import ROLES
from team_runner.project_services import ProjectServices
from runner_browser_fixture import make_runner, OrcaFixture


class BrowserCheck:
    def __init__(self, root, evidence, worktree):
        self.root, self.evidence = root, evidence
        self.worktree = worktree
        self.server = None
        self.page = None
        self.steps = []
        self.real_orca = real_orca
        self.selected = None
        self.workspaces = {}

    def browser(self, *args):
        if args[:2] == ('tab', 'create'):
            args = (*args, '--worktree', self.worktree)
        return self.real_orca(*args, timeout=25)

    def evaluate(self, js):
        result = self.browser('eval', '--page', self.page, '--expression', js)['result']
        try:
            return json.loads(result)
        except (TypeError, ValueError):
            return result

    def wait(self, predicate, label, timeout=25):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                self.steps.append(label)
                return
            time.sleep(.15)
        raise AssertionError('timeout: ' + label)

    def dom(self, expression, label):
        self.wait(lambda: self.evaluate(expression), label)

    def reveal_details(self, selector):
        # Open enclosing details from outside in, using their visible summaries.
        # DOM inspection identifies controls; it never invokes element.click().
        while True:
            summary = self.evaluate(f'''(() => {{
              const target = document.querySelector({json.dumps(selector)});
              if (!target) return null;
              const closed = [];
              for (let parent = target.parentElement; parent; parent = parent.parentElement) {{
                if (parent.tagName === 'DETAILS' && !parent.open &&
                    !(target.tagName === 'SUMMARY' && target.parentElement === parent)) closed.push(parent);
              }}
              if (!closed.length) return null;
              const details = closed.at(-1);
              const path = element => {{
                if (element.id) return '#' + CSS.escape(element.id);
                if (!element.parentElement) return element.tagName.toLowerCase();
                const siblings = [...element.parentElement.children].filter(e => e.tagName === element.tagName);
                return path(element.parentElement) + ' > ' + element.tagName.toLowerCase() +
                       ':nth-of-type(' + (siblings.indexOf(element) + 1) + ')';
              }};
              return path(details) + ' > summary';
            }})()''')
            if not summary:
                return
            self.click_visible(summary)

    def visible(self, selector):
        self.dom(f'''(() => {{ const element = document.querySelector({json.dumps(selector)});
          if (!element || element.matches(':disabled')) return false;
          const box = element.getBoundingClientRect(), style = getComputedStyle(element);
          return box.width > 0 && box.height > 0 && style.visibility === 'visible' && style.display !== 'none';
        }})()''', selector + ' visible and enabled')

    def click_visible(self, selector):
        self.visible(selector)
        # Orca's bundled agent-browser click accepts CSS selectors and performs
        # a real browser input action (visibility/actionability checks included).
        self.browser('snapshot', '--page', self.page)
        self.browser('exec', '--page', self.page, '--command', 'click ' + shlex.quote(selector))
        self.browser('snapshot', '--page', self.page)

    def click(self, selector):
        self.reveal_details(selector)
        self.click_visible(selector)

    def fill(self, selector, value):
        self.reveal_details(selector)
        self.visible(selector)
        self.evaluate(f'''(() => {{ const e=document.querySelector({json.dumps(selector)}); e.value={json.dumps(value)};
                          e.dispatchEvent(new Event('input',{{bubbles:true}})); e.dispatchEvent(new Event('change',{{bubbles:true}})); return true; }})()''')

    def state(self):
        return read_json(self.state_dir() / 'state.json', {})

    def start_server(self):
        info = self.root / 'server.json'
        info.unlink(missing_ok=True)
        with (self.root / 'server.log').open('a') as output:
            self.server = subprocess.Popen([sys.executable, str(Path(__file__).with_name('runner_browser_fixture.py')),
                            '--root', str(self.root)], stdout=output, stderr=output)
        self.wait(info.exists, 'server ready')
        return read_json(info)['url']

    def open(self, url):
        self.page = self.browser('tab', 'create', '--url', url)['browserPageId']
        self.dom(f"document.querySelectorAll('#roles tr').length === {2 * len(ROLES)}", 'primary and reserve settings roles')

    def catalog(self):
        return Catalog(self.root/'config',self.root/'state')

    def state_dir(self):
        return self.catalog().state_dir(*self.workspaces[self.selected])

    def choose(self, name):
        self.selected = name
        pid,wid = self.workspaces[name]
        self.fill('#project', pid)
        self.dom(f'document.querySelector("#workspace").value === {json.dumps(wid)}', 'workspace selected '+name)
        self.dom(f"document.querySelectorAll('#roles tr').length === {2 * len(ROLES)}", 'settings loaded '+name)

    def stop_runner_process(self, name=None):
        name = name or self.selected
        if not name: return
        info = read_json(self.root / name / 'runner-process.json', {})
        if info.get('stamp') and process_stamp(info['pid']) == info['stamp']:
            os.kill(info['pid'], signal.SIGTERM)
            state = self.catalog().state_dir(*self.workspaces[name])
            self.wait(lambda: not running(state), 'runner process stopped '+name)

    def scenario(self):
        self.page = self.browser('tab', 'create', '--url', self.start_server())['browserPageId']
        self.dom("!!document.querySelector('#project-add')", 'catalog ready')
        for name in ('alpha', 'beta'):
            self.fill('#project-name', name)
            self.click('#project-add')
            self.dom(f'document.querySelector("#project option:checked")?.textContent === {json.dumps(name)}', 'project created ' + name)
            self.fill('#workspace-path-input', str(self.root / name))
            self.click('#workspace-add')
            self.dom(f'document.querySelector("#workspace-path").textContent === {json.dumps(str(self.root / name))}', 'checkout attached ' + name)
            project = self.evaluate('document.querySelector("#project").value')
            workspace = self.evaluate('document.querySelector("#workspace").value')
            self.workspaces[name] = (project, workspace)
            self.selected = name
            self.dom(f"document.querySelectorAll('#roles tr').length === {2 * len(ROLES)}", 'settings loaded ' + name)
        self.choose('alpha')
        self.click('#start')
        self.wait(lambda: bool((self.state().get('current') or {}).get('dispatch_id')), 'cold execute starts alpha')
        self.dom("document.querySelectorAll('#questions input').length === 1", 'alpha question visible')
        self.fill('#questions input', 'complete')
        self.choose('beta')
        self.click('#start')
        self.wait(lambda: bool((self.state().get('current') or {}).get('dispatch_id')), 'beta independently starts')
        beta_dispatch = self.state()['current']['dispatch_id']
        self.choose('alpha')
        self.dom("document.querySelector('#questions input')?.value === 'complete'", 'reply draft survives selection')
        self.click('#pause')
        self.wait(lambda: self.state().get('paused'), 'pause intent persisted')
        self.click('#questions button')
        self.wait(lambda: not self.state().get('current') and not running(self.state_dir()), 'paused after first item')
        self.click('#resume')
        for expected in ('feature/T2', 'feature/T3', 'feature/T4', 'GOAL-FINAL-REVIEW'):
            self.wait(lambda: (self.state().get('current') or {}).get('item') == expected, 'current ' + expected)
            self.dom("document.querySelectorAll('#questions input').length === 1", 'question for ' + expected)
            self.fill('#questions input', 'complete')
            self.click('#questions button')
            self.wait(lambda: (self.state().get('current') or {}).get('item') != expected, 'accepted ' + expected)
        self.wait(lambda: read_json(self.state_dir() / 'goal-completion.json', {}).get('status') == 'completed', 'final acceptance recorded')
        self.dom("document.querySelector('#goal-state').textContent === 'Финальная приёмка пройдена'", 'completed visible')
        self.choose('beta')
        assert self.state()['current']['dispatch_id'] == beta_dispatch
        self.click('#stop')
        self.wait(lambda: self.state().get('stop_status') == 'stopped' and not running(self.state_dir()), 'beta stop confirmed')
        self.click('#retry')
        self.wait(lambda: self.state()['current'].get('attempt') == 2, 'retry prepared')
        self.click('#resume')
        self.wait(lambda: (self.state().get('current') or {}).get('dispatch_id') not in (None, beta_dispatch), 'retry dispatch created')
        self.click('#stop')
        self.wait(lambda: self.state().get('stop_status') == 'stopped' and not running(self.state_dir()), 'retry stopped')
        services = ProjectServices(self.catalog())
        before = {name: services.status(ids[0])['nonce'] for name, ids in self.workspaces.items()}
        # Crash only the panel; fixture's graceful shutdown intentionally cleans children.
        self.server.kill(); self.server.wait(timeout=5)
        self.browser('tab', 'close', '--page', self.page)
        self.page = self.browser('tab', 'create', '--url', self.start_server())['browserPageId']
        self.dom("document.querySelectorAll('#project option').length === 2", 'catalog restored after panel restart')
        self.choose('alpha')
        self.dom("document.querySelector('#goal-state').textContent === 'Финальная приёмка пройдена'", 'accepted state survives panel restart')
        assert {name: services.status(ids[0])['nonce'] for name, ids in self.workspaces.items()} == before
        self.screenshot('team-runner.png')

    def screenshot(self, filename):
        self.browser('tab', 'switch', '--page', self.page)
        result = self.browser('screenshot', '--page', self.page, '--format', 'png')
        def image_data(value):
            if isinstance(value, dict):
                for child in value.values():
                    found = image_data(child)
                    if found:
                        return found
            elif isinstance(value, str):
                try:
                    raw = base64.b64decode(value.split('base64,')[-1], validate=True)
                    if raw.startswith(b'\x89PNG'):
                        return raw
                except ValueError:
                    pass
                if len(value) < 1024 and value.endswith('.png') and Path(value).is_file():
                    return Path(value).read_bytes()
            return None
        data = image_data(result)
        if data is None:
            raise RuntimeError('Orca returned no screenshot image')
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / filename).write_bytes(data)

    def run(self):
        try:
            self.scenario()
            atomic_json(self.evidence / 'browser-result.json', {'status':'PASS','steps':self.steps})
            print('PASS: isolated cold execute, queue/final, pause/resume, stop/retry, drafts and panel restart')
        except Exception as exc:
            self.evidence.mkdir(parents=True, exist_ok=True)
            screenshot_error = None
            if self.page:
                try:
                    self.screenshot('browser-failure.png')
                except Exception as screenshot_exc:
                    screenshot_error = str(screenshot_exc)[:1000]
            atomic_json(self.evidence / 'browser-result.json', {'status':'FAIL','error':str(exc)[:1500],
                        'steps':self.steps, 'screenshot_error':screenshot_error, 'runner_error':self.state().get('last_error') if self.selected else None,
                        'log':(self.root / 'process.log').read_text()[-6000:] if (self.root / 'process.log').exists() else ''})
            raise
        finally:
            for name in self.workspaces: self.stop_runner_process(name)
            manager = ProjectServices(self.catalog())
            for project, _ in self.workspaces.values():
                manager.action(project, 'stop', str(uuid.uuid4()))
            if self.server:
                self.server.terminate(); self.server.wait(timeout=12)
            if self.page:
                self.browser('tab', 'close', '--page', self.page)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/team-runner-browser-evidence'))
    parser.add_argument('--worktree', required=True, help='explicit Orca browser worktree selector; never current/active')
    args = parser.parse_args()
    if args.worktree in ('current', 'active'):
        parser.error('explicit worktree selector required')
    with tempfile.TemporaryDirectory(prefix='team-runner-browser-check-') as tmp:
        BrowserCheck(Path(tmp), args.evidence, args.worktree).run()


if __name__ == '__main__':
    main()
