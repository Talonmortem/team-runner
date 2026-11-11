#!/usr/bin/env python3
"""Isolated manual/browser fixture using the real HTTP UI and runner state machine.

python3 tests/runner_browser_fixture.py --root TEMP_DIRECTORY --seed
Only temporary fixture tracking and synthetic workers; no real Orca/provider calls.
Reply complete to each question, including final review. SIGTERM cleans owned children.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import shlex
import signal
import uuid
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

TEAM = Path(__file__).resolve().parents[1] / 'src/team_runner'
sys.path.insert(0, str(TEAM.parent))
sys.path.insert(0, str(TEAM))
from team_runner import runner_config, runner_ui, application
from team_runner.engine import Runner
from team_runner.catalog import Catalog
from team_runner.runner_store import atomic_json, lock, read_json, running
from team_runner.runner_launch import Launcher
from team_runner.project_services import ProjectServices, run_service, departed
from team_runner.runner_entry import process_stamp, boot_id


def fixture_environment(root):
    env = {key: value for key, value in os.environ.items() if not key.startswith('TEAM_RUNNER_')}
    env.update(XDG_CONFIG_HOME=str(root / 'xdg-config'), XDG_STATE_HOME=str(root / 'xdg-state'),
               PYTHONPATH=str(TEAM.parent))
    return env


def remember_process(root, child, role):
    with lock(root / 'fixture-processes.lock'):
        records = read_json(root / 'fixture-processes.json', [])
        records.append({'pid': child.pid, 'process_stamp': process_stamp(child.pid),
                        'boot_id': boot_id(), 'role': role, 'root': str(root.resolve())})
        atomic_json(root / 'fixture-processes.json', records)
    threading.Thread(target=child.wait, daemon=True).start()


class FixtureManager:
    """Explicit fake manager boundary: no installed manager is ever spawned."""
    def __init__(self, root):
        self.root = root

    def ensure(self, identifier):
        with lock(self.root / 'fixture-manager.lock'):
            value = read_json(self.root / 'fixture-manager.json', {'requests': []})
            if identifier not in value['requests']:
                value['requests'].append(identifier)
                atomic_json(self.root / 'fixture-manager.json', value)
        return {'state': 'running', 'confirmed': True, 'fixture': True}


def install_fixture(catalog):
    """Use real application/HTTP/runner logic with only external edges replaced."""
    from team_runner import engine, orca
    def discover(*args, **kwargs):
        if args != ('worktree', 'list'):
            raise AssertionError('Unexpected non-fixture Orca request')
        return {'worktrees': [{'id': 'fixture', 'instanceId': 'instance', 'hostId': 'local',
                              'path': str(path.resolve())}
                             for path in catalog.config_root.parent.iterdir() if (path / '.git').is_dir()]}
    orca.call = discover
    services = FixtureServices(catalog)
    runner_ui.Runner = lambda ctx: make_runner(catalog, ctx.project_id, ctx.workspace_id)
    # Supervisor imports engine.Runner at construction, separately from HTTP.
    engine.Runner = lambda ctx: make_runner(catalog, ctx.project_id, ctx.workspace_id)
    runner_ui.execute = lambda runner, data: application.execute(runner, data, services=services,
                                                 manager=FixtureManager(catalog.config_root.parent))
    return services


def shutdown_fixture(catalog, services):
    """Stop only processes recorded by this temporary fixture's own Popen calls."""
    from team_runner import service_intent
    for project in catalog.read()['projects']:
        try:
            service_intent.cancel(catalog, project, str(uuid.uuid4()))
            services.action(project, 'stop', str(uuid.uuid4()))
        except (OSError, ValueError, RuntimeError):
            pass
    root = catalog.config_root.parent
    records = read_json(root / 'fixture-processes.json', [])
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for record in records:
            if (record.get('root') == str(root.resolve()) and record.get('boot_id') == boot_id()
                    and record.get('process_stamp') and not departed(record)
                    and process_stamp(record['pid']) == record['process_stamp']):
                try:
                    os.kill(record['pid'], sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(not departed(record) for record in records):
            time.sleep(.03)
    survivors = [record for record in records if not departed(record)]
    if survivors:
        raise RuntimeError('owned browser fixture processes did not stop')


class FixtureServices(ProjectServices):
    def spawn(self, project_id, nonce):
        child = subprocess.Popen([sys.executable, __file__, '--root', str(self.catalog.config_root.parent),
                                  '--service', '--project', project_id, '--nonce', nonce],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True,
                                 env=fixture_environment(self.catalog.config_root.parent))
        self.children = [p for p in self.children if p.poll() is None]
        self.children.append(child)
        remember_process(self.catalog.config_root.parent, child, 'service')
        return child


class OrcaFixture:
    def __init__(self, root, worktree_path=None):
        self.root = root
        self.worktree_path = worktree_path or root
        self.orca_id = "fixture"
        self.messages = []
        self.tasks = {}
        self.workers = {}
        self.gates = {}
        self.serial = 0
        self.terminals = {}
        self.receipts = {}
        self.guard = threading.RLock()

    def __call__(self, *args, **kwargs):
        with self.guard, lock(self.root / 'transport.lock'):
            saved = read_json(self.root / 'transport.json', {})
            for key in ('messages', 'tasks', 'workers', 'gates', 'serial', 'terminals', 'receipts'):
                if key in saved:
                    setattr(self, key, saved[key])
            request = args[args.index('--retry-request') + 1] if '--retry-request' in args else None
            if args[:2] == ('orchestration', 'request-show'):
                request = args[args.index('--request') + 1]
                return ({'state': 'completed', 'receipt': self.receipts[request]} if request in self.receipts
                        else {'state': 'absent'})
            if request in self.receipts:
                return self.receipts[request]
            result = self.perform(*args, **kwargs)
            if request:
                self.receipts[request] = result
            atomic_json(self.root / 'transport.json', {k: getattr(self, k) for k in
                        ('messages', 'tasks', 'workers', 'gates', 'serial', 'terminals', 'receipts')})
            if args[:2] == ('orchestration', 'worker-start') and (self.root / 'lose-dispatch').exists():
                (self.root / 'lose-dispatch').unlink()
                raise RuntimeError('fixture lost Dispatch response')
            return result

    def perform(self, *args, **kwargs):
        def value(flag):
            return args[args.index(flag) + 1]
        with self.guard:
            op = args[:2]
            if op == ('worktree', 'show'):
                return {'worktree':{'id':self.orca_id,'instanceId':'instance','path':str(self.worktree_path),'hostId':'local'}}
            if op == ('orchestration', 'run-current'):
                return {'run': {'id': 'fixture-run'}}
            if op == ('orchestration', 'run-create'):
                return {'run': {'id': 'fixture-run'}}
            if op == ('orchestration', 'task-create'):
                task = 'task-' + str(len(self.tasks) + 1)
                self.tasks[task] = ('GOAL-FINAL-REVIEW' if value('--task-title') == 'goal final review'
                                    else value('--task-title').split()[0])
                return {'task': {'id': task}}
            if op == ('terminal', 'create'):
                self.serial += 1
                terminal = {'handle': 'fixture-terminal-' + str(self.serial), 'incarnationId': str(uuid.uuid4()),
                            'ptyId': str(self.serial), 'worktreeId': self.orca_id}
                self.terminals[terminal['handle']] = terminal
                command = shlex.split(value('--command'))
                if len(command) > 3 and 'team_runner.runner_entry' in command:
                    from team_runner.runner_entry import process_stamp
                    i = command.index('team_runner.runner_entry')
                    atomic_json(command[i+1], {'nonce': command[i+2], 'terminal': terminal,
                                'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid())})
                return {'terminal': terminal}
            if op == ('terminal', 'show'):
                return {'terminal': self.terminals[value('--terminal')]}
            if op == ('terminal', 'list'):
                return {'terminals': list(self.terminals.values())}
            if op == ('terminal', 'close'):
                self.terminals.pop(value('--terminal'), None)
                return {}
            if op == ('terminal', 'wait'):
                return {'wait': {'satisfied': True}}
            if op == ('terminal', 'read'):
                return {'terminal': {'source': 'screen', 'tail': ['Ready']}}
            if op == ('orchestration', 'worker-start'):
                dispatch = 'dispatch-' + str(len(self.workers) + 1)
                self.workers[dispatch] = {'item': self.tasks[value('--task')], 'status': 'live', 'terminal': value('--terminal')}
                self.messages.append({'id':'question-' + dispatch, 'type':'question',
                                      'body':'Тестовый вопрос. Ответ complete завершает пункт, hold оставляет его активным.',
                                      'payload': {'dispatchId': dispatch}})
                return {'dispatchId': dispatch}
            if op == ('orchestration', 'worker-show'):
                worker = self.workers[value('--dispatch')]
                owner = next(d for d, w in reversed(list(self.workers.items())) if w['terminal'] == worker['terminal'])
                return {'terminal': self.terminals.get(worker['terminal']),
                        'terminalResource': {'ownershipState': 'external', 'ownerDispatchId': owner},
                        'observation': {'status': self.workers[value('--dispatch')]['status']},
                        'dispatch': {'status': 'completed' if self.workers[value('--dispatch')].get('done') else 'dispatched'}}
            if op in (('orchestration', 'worker-stop'), ('orchestration', 'worker-release'), ('orchestration', 'worker-abandon')):
                self.workers[value('--dispatch')]['status'] = 'exited'
                return {}
            if op == ('orchestration', 'worker-read'):
                return {'text': 'Isolated synthetic worker output; no provider or registered project goal was started.'}
            if op == ('orchestration', 'check'):
                if '--ack' in args:
                    self.messages = [m for m in self.messages if m['id'] != value('--ack')]
                if self.messages:
                    return {'messages': self.messages[:1], 'deliveryId': self.messages[0]['id']}
                return {'messages': []}
            if op == ('orchestration', 'reply'):
                dispatch = value('--id').removeprefix('question-')
                if value('--body') == 'complete':
                    self.workers[dispatch]['done'] = True
                    goal = self.worktree_path / 'feat/goal-fixture'
                    item_id = self.workers[dispatch]['item']
                    if item_id == 'GOAL-FINAL-REVIEW':
                        artifact = goal / 'evidence/goal-final-review.md'
                    else:
                        data = read_json(goal / 'feat-one/Tasks.json')
                        for item in data['queue']:
                            if item['id'] == item_id.split('/')[-1]:
                                item['status'] = 'completed'
                        atomic_json(goal / 'feat-one/Tasks.json', data)
                        if all(item['status'] == 'completed' for item in data['queue']):
                            tracking = read_json(goal / 'Tasks.json')
                            tracking['queue'][0]['status'] = 'completed'
                            atomic_json(goal / 'Tasks.json', tracking)
                        artifact = goal / 'feat-one/evidence' / item_id.split('/')[-1].lower() / 'acceptance.md'
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text('Accepted isolated browser fixture. Fake worker report; no real provider execution.\n')
                    self.messages.append({'id':'done-' + dispatch, 'type':'worker_done', 'body':'Fixture completed',
                                          'payload':{'dispatchId':dispatch,'outcome':'succeeded'}})
                return {}
            if op == ('orchestration', 'gate-create'):
                gate = 'gate-' + str(len(self.gates) + 1)
                self.gates[gate] = {'id':gate,'status':'pending'}
                return {'gate':self.gates[gate]}
            if op == ('orchestration', 'gate-list'):
                return {'gates':list(self.gates.values())}
            if op == ('orchestration', 'gate-resolve'):
                self.gates[value('--id')].update(status='resolved', resolution=value('--resolution'))
                return {}
            return {}


def make_runner(catalog, project_id, workspace_id):
    runner = Runner(catalog.context(project_id, workspace_id, allow_disabled=True))
    fake = OrcaFixture(runner.repo / '.fixture-transport', runner.repo)
    fake.orca_id = runner.context.orca_id
    def transport(*a, **kw):
        result = fake(*a, **kw)
        if a[:2] == ('orchestration','check') and not result.get('messages'): time.sleep(.1)
        if a[:2] == ('terminal','create'):
            command = shlex.split(a[a.index('--command') + 1])
            if '_runner' in command:
                nonce = command[command.index('--nonce') + 1]
                with (runner.repo / 'process.log').open('a') as output:
                    process = subprocess.Popen([sys.executable, __file__, '--root', str(catalog.config_root.parent),
                           '--runner', '--project', project_id, '--workspace', workspace_id, '--nonce', nonce],
                           stdout=output, stderr=output, start_new_session=True, close_fds=True,
                           env=fixture_environment(catalog.config_root.parent))
                remember_process(catalog.config_root.parent, process, 'runner')
                atomic_json(runner.repo / 'runner-process.json', {'pid':process.pid,'stamp':process_stamp(process.pid)})
        return result
    runner.orca = runner.launcher.orca = transport
    runner.git = lambda *a: 'fixture-head'
    runner.dirty_paths = lambda: []
    runner.notify = lambda *a: None
    runner.poll_outputs = lambda *a: None
    return runner


def prepare_checkout(root):
    root.mkdir(parents=True, exist_ok=True)
    if not (root / '.git').exists(): subprocess.run(['git', 'init', '-q', str(root)], check=True)
    exclude = root / '.git/info/exclude'
    existing = exclude.read_text() if exclude.exists() else ''
    patterns = ['.fixture-transport/', 'process.log', 'runner-process.json']
    missing = [pattern for pattern in patterns if pattern not in existing.splitlines()]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(existing + '\n' + '\n'.join(missing) + '\n')
    if not (root / 'feat/goal-fixture/Tasks.json').exists():
        atomic_json(root / 'feat/goal-fixture/Tasks.json', {'kind':'goal','queue':[
            {'id':'feature','path':'feat-one','status':'in_progress'}]})
        atomic_json(root / 'feat/goal-fixture/feat-one/Tasks.json', {'queue':[
            {'id':i,'task':'Browser fixture '+i,'status':'not_started'} for i in ('T1','T2','T3','T4')]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--runner', action='store_true')
    parser.add_argument('--service', action='store_true')
    parser.add_argument('--seed', action='store_true', help='register three temporary browser projects')
    parser.add_argument('--port', type=int, default=0, help='isolated panel port for restart scenarios')
    for name in ('project','workspace','nonce'): parser.add_argument('--'+name)
    args = parser.parse_args()
    cat = Catalog(args.root / 'config', args.root / 'state')
    if args.runner:
        from team_runner.service import run_process
        return run_process(make_runner(cat,args.project,args.workspace), args.nonce)
    services = install_fixture(cat)
    if args.service:
        return run_service(cat, args.project, args.nonce)
    for name in ('alpha','beta','gamma'):
        prepare_checkout(args.root / name)
        if args.seed and not any(p['name'] == name for p in cat.read()['projects'].values()):
            project = cat.project(name)['id']
            cat.workspace(project, {'path': str(args.root / name), 'orca_id': 'fixture', 'orca_instance': 'instance'})
    server, token = runner_ui.make_server(cat, port=args.port, services=services)
    url = f'http://127.0.0.1:{server.server_port}/#token={token}'
    atomic_json(args.root / 'server.json', {'url':url})
    print(url, flush=True)
    def interrupt(*args):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        shutdown_fixture(cat, services)


if __name__ == '__main__':
    raise SystemExit(main())
