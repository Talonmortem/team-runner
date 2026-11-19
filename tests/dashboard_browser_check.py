#!/usr/bin/env python3
"""Exercise the dashboard with temporary Team Runner data and fake Orca/hoster."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread
from time import time

from runner_browser_check import BrowserCheck
from runner_browser_fixture import prepare_checkout
from team_runner import dashboard, goal_intent, team_graph
from team_runner.catalog import Catalog
from team_runner.engine import Runner
from team_runner.runner_config import defaults
from team_runner.runner_store import atomic_json, read_json, running


class DashboardBrowserCheck(BrowserCheck):
    def __init__(self, root, evidence, worktree):
        super().__init__(root, evidence, worktree)
        self.fixture_catalog = Catalog(root / 'config', root / 'state')
        self.dashboard_thread = None
        self.runner_process = None
        self.switches = []
        self.model_calls = []
        self.orca_unavailable = False

    def seed(self):
        alpha, beta = (self.fixture_catalog.project(name)['id'] for name in ('Alpha', 'Beta'))
        for pid, name in ((alpha, 'Alpha main'), (alpha, 'Alpha spare'), (beta, 'Beta legacy')):
            repo = self.root / name
            prepare_checkout(repo)
            workspace = self.fixture_catalog.workspace(pid, {
                'name': name, 'path': str(repo), 'orca_id': name, 'orca_instance': 'fixture'
            })
            self.workspaces[name] = (pid, workspace['id'])

        self.runner = Runner(self.fixture_catalog.context(*self.workspaces['Alpha main']))
        self.attempt = self.runner.state_dir / 'attempts' / 'fixture'
        self.goal = 'feat/goal-fixture'
        goal_intent.authorize(self.runner, {'goal': self.goal, 'mode': 'auto'},
                              'f8089a01-73a1-405b-be53-cac6c9ae962b')
        self.runner.context.snapshot(self.attempt)
        graph = team_graph.migrate(defaults())
        graph['nodes'] = [entry for entry in graph['nodes'] if entry['role'] in ('coordinator', 'planner')]
        graph['edges'] = [edge for edge in graph['edges'] if edge['target'] == 'planner']
        graph['layout'] = {'coordinator': {'x': 0, 'y': 0}, 'planner': {'x': 280, 'y': 58}}
        graph['nodes'][0]['name'] = 'Snapshot node'
        atomic_json(self.attempt / 'config.json', graph)

        def agent(i, role, activity, status):
            dispatch = f'ctx_{i:012x}'
            return {'id': 'agent-' + role, 'node_id': role, 'role': role, 'activity': activity,
                    'status': status, 'dispatch_id': dispatch, 'run_id': 'run-fixture',
                    'task_id': f'task_{i:012x}', 'terminal': 'fixture-terminal-' + role,
                    'incarnation': 'incarnation-' + role, 'terminal_owned': True,
                    'working_evidence': {'dispatch': dispatch, 'at': time(), 'source': 'exact fresh agent_status'},
                    'requested': {'agent': 'codex', 'model': 'fixture-model'}}
        waiting = agent(1, 'coordinator', 'waiting_child', 'waiting_child')
        self.agent = agent(2, 'planner', 'working', 'active')
        call = {'id': 'fixture-delegation', 'kind': 'delegation', 'status': 'running',
                'edge_id': 'delegate_planner', 'parent_agent': waiting['id'],
                'child_agent': self.agent['id'], 'dispatch_id': self.agent['dispatch_id']}
        self.registry = {'agents': [waiting, self.agent], 'runs': [], 'graph_calls': [call]}
        atomic_json(self.attempt / 'agents.json', self.registry)
        atomic_json(self.runner.state_file, {'goal': self.goal, 'history': [], 'current': {
            'attempt_dir': str(self.attempt), 'item': 'T1', 'dispatch_id': waiting['dispatch_id'],
        }})
        atomic_json(self.runner.state_dir / 'liveness-incident.json', {
            'scope': {'attempt': str(self.attempt), 'dispatch': waiting['dispatch_id']},
            'started_at': time() - 60, 'visible': True,
            'reason': 'Orca не подтвердила состояние координатора', 'diagnosis': {'status': 'matched'},
        })
        legacy = Runner(self.fixture_catalog.context(*self.workspaces['Beta legacy']))
        legacy_attempt = legacy.state_dir / 'attempts' / 'legacy-fixture'
        legacy.context.snapshot(legacy_attempt)
        atomic_json(legacy_attempt / 'config.json', defaults())
        atomic_json(legacy.state_file, {'goal': self.goal, 'history': [], 'current': {
            'attempt_dir': str(legacy_attempt), 'item': 'T3', 'dispatch_id': 'ctx_000000000003',
        }})

        # The test-owned runner stand-in keeps its real ownership lock across dashboard restarts.
        script = ('from team_runner.runner_store import lock; import sys, time\n'
                  'with lock(sys.argv[1]):\n print("ready", flush=True); time.sleep(180)\n')
        self.runner_process = subprocess.Popen([sys.executable, '-c', script, str(self.runner.state_dir / 'runner.lock')],
            stdout=subprocess.PIPE, text=True)
        self.wait(lambda: running(self.runner.state_dir), 'Fixture runner holds ownership lock', timeout=5)

    def make_runner(self, context):
        runner = Runner(context)

        def transport(*args, **kwargs):
            if self.orca_unavailable:
                raise RuntimeError('fixture Orca unavailable')
            if args[:2] == ('worktree', 'show'):
                assert args[2:4] == ('--worktree', 'id:' + context.orca_id)
                return {'worktree': {'id': context.orca_id, 'instanceId': context.orca_instance,
                                     'path': str(context.repo), 'hostId': 'local'}}
            assert context.identity() == self.runner.context.identity()
            record = self.agent
            terminal = {'handle': record['terminal'], 'incarnationId': record['incarnation'],
                        'worktreeId': context.orca_id}
            if args[:2] == ('orchestration', 'worker-show'):
                assert args[2:4] == ('--dispatch', record['dispatch_id'])
                return {'observation': {'exactWorker': True}, 'terminal': terminal,
                        'terminalResource': {'ownerDispatchId': record['dispatch_id']},
                        'dispatch': {'id': record['dispatch_id'], 'runId': record['run_id'],
                                     'taskId': record['task_id']}}
            assert args[2:4] == ('--terminal', record['terminal'])
            if args[:2] == ('terminal', 'show'):
                return {'terminal': terminal}
            if args[:2] == ('terminal', 'switch'):
                self.switches.append(record['terminal'])
                return {}
            raise AssertionError(args)

        runner.launcher.orca = transport
        return runner

    def model(self, message, facts):
        self.model_calls.append((message, facts))
        return 'Отчёт фикстуры'

    def start_server(self, enable_actions=True):
        dashboard.Runner = self.make_runner
        class EmptyUsage:
            def collect(self, _catalog):
                return {'collected_at': '2026-11-18T12:00:00Z', 'current': {}, 'history': {}}
        self.server, token = dashboard.make_server(
            self.fixture_catalog, session_dir=self.root / ('control' if enable_actions else 'preview'),
            enable_actions=enable_actions, model=self.model, watch_interval=.2, usage_collector=EmptyUsage())
        self.dashboard_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.dashboard_thread.start()
        return f'http://127.0.0.1:{self.server.server_port}/#token={token}'

    def stop_server(self):
        if self.server:
            self.server.shutdown()
            self.dashboard_thread.join(5)
            self.server.server_close()
            self.server = None

    def close_tab(self):
        if self.page:
            self.browser('tab', 'close', '--page', self.page)
            self.page = None

    def open(self, url):
        self.page = self.browser('tab', 'create', '--url', url)['browserPageId']
        self.dom("document.querySelectorAll('#projects .project').length === 2", 'Two projects loaded')

    def choose(self, name):
        _, identifier = self.workspaces[name]
        self.click_visible('[data-workspace="' + identifier + '"]')
        self.dom('document.querySelector("#workspace-name").textContent === ' + json.dumps(name),
                 'Checkout selected: ' + name)

    def work_state(self):
        # Capture all runner/catalog writes, including unexpected new locks or files.
        return {str(path): path.read_bytes()
                for root in (self.fixture_catalog.state_root, self.fixture_catalog.config_root)
                for path in root.rglob('*') if path.is_file()}

    def send_chat(self, message):
        self.fill('#chat-text', message)
        self.click_visible('#send')

    def scenario(self):
        self.seed()
        initial = self.work_state()
        self.open(self.start_server(enable_actions=False))
        self.choose('Alpha main')
        self.dom("!!document.querySelector('#graph-view [data-node=planner][data-agent-state=working]')",
                 'Fresh worker is working')
        self.dom("!!document.querySelector('#graph-view [data-node=coordinator][data-agent-state=waiting]')",
                 'Waiting agent shown on its snapshot node')
        self.dom("""(() => {
            const graph = document.querySelector('#graph-view');
            const edge = graph.querySelector('[data-edge=delegate_planner].working');
            const spinner = graph.querySelector('[data-node=planner] .working-spinner');
            if (!edge || !spinner || graph.querySelector('[data-node=coordinator] .working-spinner')) return false;
            const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
            return getComputedStyle(edge).animationName === (reduced ? 'none' : 'edge-flow') &&
                getComputedStyle(spinner).animationName === (reduced ? 'none' : 'agent-spin');
        })()""", 'Registered working Dispatch animates its node and incoming edge')
        self.dom("document.querySelector('#graph-view').textContent.includes('Snapshot node')",
                 'Immutable graph used')
        self.click_visible('[data-view=future]')
        self.dom("!document.querySelector('#future-view').textContent.includes('Snapshot node')",
                 'Future graph excludes snapshot-only name')
        self.dom("!document.querySelector('#future-view .edge.working, #future-view .working-spinner')",
                 'Future profile has no live animations')
        self.click_visible('[data-view=graph]')
        self.click_visible('#graph-view [data-node=planner]')
        self.dom("!document.querySelector('[data-focus]') && document.querySelector('#agent').textContent.includes('fixture-terminal-planner')",
                 'Preview displays terminal ID with focus disabled')
        alpha, main = self.workspaces['Alpha main']
        beta, _ = self.workspaces['Beta legacy']
        alpha_toggle = f'[data-project="{alpha}"] .project-toggle'
        self.click_visible(alpha_toggle)
        self.dom(f'document.querySelector({json.dumps(alpha_toggle)}).getAttribute("aria-expanded") === "false" && '
                 'document.querySelector("#workspace-name").textContent === "Alpha main"',
                 'Collapsing a project keeps its checkout selected')
        self.click_visible(alpha_toggle)
        grip = f'[data-order-id="{main}"] .drag-grip'
        self.evaluate(f'document.querySelector({json.dumps(grip)}).focus()')
        self.browser('keypress', '--page', self.page, '--key', 'ArrowDown')
        self.dom(f'document.querySelector("[data-project] [data-workspace]").dataset.workspace !== "{main}"',
                 'Checkout moves within its project using the focused grip')
        self.dom(f"""(() => {{
            const source = document.querySelector('[data-order-id="{beta}"] .drag-grip');
            const target = document.querySelector('[data-order-id="{alpha}"]');
            const dataTransfer = new DataTransfer();
            source.dispatchEvent(new DragEvent('dragstart', {{dataTransfer}}));
            const event = new DragEvent('dragover', {{dataTransfer, cancelable: true,
                clientY: target.getBoundingClientRect().top + 1}});
            target.dispatchEvent(event);
            target.dispatchEvent(new DragEvent('drop', {{dataTransfer, cancelable: true, clientY: event.clientY}}));
            return event.defaultPrevented && document.querySelector('.project').dataset.project === '{beta}';
        }})()""", 'Dragging a project reorders the sidebar')
        self.click_visible(f'[data-project="{beta}"] .project-toggle')
        self.browser('reload', '--page', self.page)
        group = f'[data-project="{beta}"] .project-checkouts'
        self.dom(f'document.querySelector({json.dumps(group)})?.hidden === true',
                 'Collapsed project stays collapsed after reload')
        self.click_visible(f'[data-project="{beta}"] .project-toggle')
        _, spare = self.workspaces['Alpha spare']
        self.dom(f"""document.querySelector('.project')?.dataset.project === '{beta}' &&
          document.querySelector('[data-project="{alpha}"] [data-workspace]')?.dataset.workspace === '{spare}'
          """, 'Project and checkout order persist after reload')
        self.choose('Alpha spare')
        self.dom("document.querySelector('#graph-view').textContent.includes('Нет текущей попытки')",
                 'Second checkout has independent state')
        self.choose('Beta legacy')
        self.dom("document.querySelector('#graph-view').textContent.includes('Роли попытки')",
                 'Legacy attempt displays roles')
        self.close_tab()
        self.stop_server()
        assert self.work_state() == initial
        assert self.runner_process.poll() is None and running(self.runner.state_dir)

        # Fresh proof for an exact terminal test after switching between browser tabs.
        self.agent['working_evidence']['at'] = time()
        atomic_json(self.attempt / 'agents.json', self.registry)
        initial = self.work_state()
        self.open(self.start_server())
        self.choose('Alpha main')
        self.click_visible('#graph-view [data-node=planner]')
        self.click_visible('[data-focus=agent-planner]')
        self.wait(lambda: self.switches == [self.agent['terminal']], 'Only the exact selected terminal switched')
        self.orca_unavailable = True
        self.click_visible('[data-view=diagnostics]')
        self.dom("document.querySelector('#diagnostic-orca summary').textContent.includes('Недоступно')",
                 'Orca outage visible in diagnostics')
        self.click_visible('#diagnostic-waits summary')
        self.dom("""['Начало', 'Длительность', 'Orca не подтвердила состояние координатора', 'Записи совпадают']
                 .every(text => document.querySelector('#diagnostic-waits').innerText.includes(text))""",
                 'Sustained coordinator incident shows timing, reason and diagnostic result')
        self.click_visible('[data-view=graph]')
        self.dom("!!document.querySelector('#graph-view [data-node=planner][data-agent-state=unconfirmed]')",
                 'Working state becomes unconfirmed during outage')
        self.dom("!document.querySelector('#graph-view .edge.working, #graph-view .working-spinner')",
                 'Orca outage stops node and edge animations')
        self.dom("!!document.querySelector('#graph-view [data-node=planner] .state-flash')",
                 'Changed agent status is highlighted once')
        self.click_visible('[data-inspector=hoster]')
        self.send_chat('Дай отчёт')
        self.dom("document.querySelector('#messages').textContent.includes('Отчёт фикстуры')", 'Hoster report shown')
        self.send_chat('понаблюдай')
        self.dom("!document.querySelector('#cancel-watch').hidden", 'Watch started')
        dialog_id = self.evaluate('document.querySelector("#dialogs").value')
        dialog_path = next((self.root / 'control').rglob(dialog_id + '.json'))
        self.close_tab()
        assert self.work_state() == initial
        atomic_json(self.runner.state_file, {**self.runner.load_state(), 'last_error': 'Fixture blocker changed'})
        self.wait(lambda: any('Fixture blocker changed' in m['text'] for m in read_json(dialog_path)['messages']),
                  'Watch captured new blocker with tab closed')
        before_restart = self.work_state()
        self.stop_server()
        self.open(self.start_server())
        assert self.work_state() == before_restart
        assert self.runner_process.poll() is None and running(self.runner.state_dir)
        state = self.runner.load_state()
        current = state.pop('current')
        history = {'attempt_dir': current['attempt_dir'], 'item': current['item'], 'outcome': 'принята'}
        atomic_json(self.runner.state_file, {**state, 'history': [history]})
        self.wait(lambda: read_json(dialog_path)['watch']['status'] == 'finished',
                  'Watch resumed for the same attempt and recorded its outcome')
        assert len(self.model_calls) == 1

    def run(self):
        original_runner = dashboard.Runner
        try:
            self.scenario()
            atomic_json(self.evidence / 'dashboard-browser-result.json', {'status': 'PASS', 'steps': self.steps})
            print('PASS: isolated checkout/graph, terminal, outage, report, watch, panel restart')
        except Exception as exc:
            atomic_json(self.evidence / 'dashboard-browser-result.json',
                        {'status': 'FAIL', 'steps': self.steps, 'error': str(exc)})
            raise
        finally:
            try:
                self.close_tab()
            finally:
                self.stop_server()
                dashboard.Runner = original_runner
                if self.runner_process:
                    self.runner_process.terminate()
                    self.runner_process.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worktree', required=True, help='explicit Orca browser worktree selector')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if args.worktree in ('current', 'active'):
        parser.error('explicit worktree selector required')
    with tempfile.TemporaryDirectory(prefix='team-runner-dashboard-fixture-') as tmp:
        DashboardBrowserCheck(Path(tmp), args.evidence, args.worktree).run()


if __name__ == '__main__':
    main()
