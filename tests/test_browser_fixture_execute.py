"""Real cold HTTP execute on owned fixture services and synthetic Orca workers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from team_runner.runner_store import read_json
from team_runner.project_services import departed


class BrowserFixtureExecute(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = Path(__file__).with_name('runner_browser_fixture.py')
        env = {key: value for key, value in os.environ.items() if not key.startswith('TEAM_RUNNER_')}
        env.update(XDG_CONFIG_HOME=str(self.root / 'xdg-config'), XDG_STATE_HOME=str(self.root / 'xdg-state'))
        self.log = (self.root / 'fixture.log').open('w'); self.addCleanup(self.log.close)
        self.child = subprocess.Popen([sys.executable, str(script), '--root', str(self.root), '--seed'],
                                      env=env, stdout=self.log, stderr=self.log)
        self.addCleanup(self.cleanup)
        url = self.wait(lambda: read_json(self.root / 'server.json', {}).get('url'), 'fixture server ready')
        self.origin, self.token = url.split('/#token=')
        self.projects = self.request('/api/projects')['projects']
        project = next((p for p in self.projects.values() if p['name'] == 'alpha'), None)
        self.project = project['id']; self.workspace = next(iter(project['workspaces']))
        self.prefix = f'/api/projects/{self.project}/workspaces/{self.workspace}'

    def cleanup(self):
        self.child.terminate()
        try:
            self.child.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.child.kill(); self.child.wait(timeout=3)
        records = read_json(self.root / 'fixture-processes.json', [])
        self.assertFalse([record for record in records if not departed(record)], 'fixture left owned child processes')

    def wait(self, predicate, description, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if self.child.poll() is not None:
                self.fail((self.root / 'fixture.log').read_text())
            time.sleep(.05)
        logs = (self.root / 'alpha/process.log').read_text() if (self.root / 'alpha/process.log').exists() else ''
        diagnostics = []
        for path in self.root.glob('state/projects/*/workspaces/*/commands/*.json'):
            diagnostics.append(read_json(path))
        self.fail(description + ': ' + logs[-5000:] + '\n' + repr(diagnostics))

    def request(self, path, data=None, allow_stale=False):
        req = urllib.request.Request(self.origin + path, data=None if data is None else json.dumps(data).encode(),
              headers={'Authorization': 'Bearer ' + self.token, 'Origin': self.origin, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if allow_stale and exc.code == 400 and json.loads(body).get('error', {}).get('code') == 'stale_revision':
                return {'rejected_stale_revision': True}
            raise AssertionError(f'{path}: HTTP {exc.code}: {body}') from exc

    def summary(self):
        return self.request(self.prefix + '/summary')

    def command(self, action, payload=None):
        # Match the UI protocol: only a definite pre-effect stale_revision refusal
        # permits refreshing scope. Never retry a pending/unknown/accepted command.
        for _ in range(5):
            summary = self.summary()
            receipt = self.request(self.prefix + '/commands', {'schema_version': 1, 'request_id': str(uuid.uuid4()),
                'action': action, 'payload': payload or {}, 'scope': {'identity': summary['identity'],
                    'generation': summary['generation'], 'attempt': summary['current']['attempt'], 'revision': summary['revision']}}, allow_stale=True)
            if not receipt.get('rejected_stale_revision'):
                break
        self.assertFalse(receipt.get('rejected_stale_revision'), 'fixture scope never stabilized')
        terminal = self.wait(lambda: (op if (op := self.request(self.prefix + '/operations/' + receipt['request_id']))['status'] in ('succeeded', 'failed', 'unknown') else None), 'command receipt')
        self.assertEqual(terminal['status'], 'succeeded', terminal)
        return terminal

    def question(self):
        def pending():
            state = self.request(self.prefix + '/state')['state']
            return next((m for m in (state.get('current') or {}).get('messages', [])
                         if m['type'] == 'question' and not m.get('answered') and not m.get('closed')), None)
        return self.wait(pending, 'pending synthetic worker question')

    def test_cold_execute_pause_resume_replies_and_final_source_proof(self):
        receipt = self.request(self.prefix + '/execute', {'request_id': str(uuid.uuid4()),
                                                          'options': {'goal': 'feat/goal-fixture'}})
        self.assertEqual(receipt['status'], 'succeeded')
        first = self.question()
        self.command('pause')
        self.command('reply', {'message_id': first['id'], 'text': 'complete'})
        paused = self.wait(lambda: (s if (s := self.summary())['observed_state'] == 'paused' else None), 'pause after item')
        self.request(self.prefix + '/execute', {'request_id': str(uuid.uuid4()), 'options': {'resume': True},
                     'generation': paused['generation'], 'epoch': paused['epoch']})
        for _ in range(4):  # Three remaining items plus final review.
            question = self.question()
            self.command('reply', {'message_id': question['id'], 'text': 'complete'})
        completed = self.wait(lambda: (s if (s := self.summary())['observed_state'] == 'completed' else None), 'goal completion')
        self.assertEqual(completed['final_acceptance']['status'], 'accepted')
        root = self.root / 'alpha/feat/goal-fixture'
        self.assertTrue((root / 'evidence/goal-final-review.md').is_file())
        for item in ('t1', 't2', 't3', 't4'):
            self.assertTrue((root / f'feat-one/evidence/{item}/acceptance.md').is_file())
        state = self.root / 'state/projects' / self.project / 'workspaces' / self.workspace
        self.assertEqual(read_json(state / 'goal-completion.json')['status'], 'completed')
        self.assertFalse((self.root / 'state/manager/instance.json').exists())
        self.assertTrue((self.root / 'fixture-manager.json').is_file())
