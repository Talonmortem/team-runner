"""Owned manager/service subprocess crash fixture; no real Orca or goal launch."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from team_runner.catalog import Catalog
from team_runner.engine import Runner
from team_runner.project_services import ProjectServices, departed, run_service
from team_runner.runner_entry import process_stamp, boot_id
from team_runner.runner_store import atomic_json, lock, read_json, running
from team_runner.service_manager import Manager
from team_runner import service_intent
from runner_browser_fixture import OrcaFixture, prepare_checkout


def spawn_fixture(root, role, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('TEAM_RUNNER_')}
    env.update(XDG_CONFIG_HOME=str(root / 'xdg-config'), XDG_STATE_HOME=str(root / 'xdg-state'),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--fixture', role,
                             '--root', str(root), *args], cwd='/', env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


class FixtureServices(ProjectServices):
    def spawn(self, project_id, nonce):
        child = spawn_fixture(self.catalog.config_root.parent, 'service', '--project', project_id, '--nonce', nonce)
        self.children.append(child)
        threading.Thread(target=child.wait, daemon=True).start()
        return child


def fixture_main(args):
    root = args.root
    catalog = Catalog(root / 'config', root / 'state')
    identity = {'role': args.fixture, 'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid()),
                'boot_id': boot_id(), 'nonce': args.nonce, 'root': str(root)}
    with lock(root / 'owned.lock'):
        records = read_json(root / 'owned.json', [])
        records.append(identity)
        atomic_json(root / 'owned.json', records)
    if args.fixture == 'manager':
        return Manager(catalog, services=FixtureServices(catalog)).run(args.nonce, poll_interval=.05)
    if args.fixture == 'service':
        # Both HTTP and the real Supervisor get the same isolated fake transport.
        from team_runner import engine, runner_ui
        original = engine.Runner
        def factory(context):
            runner = original(context)
            runner.launcher.orca = OrcaFixture(root / 'transport', runner.repo)
            return runner
        engine.Runner = factory
        runner_ui.Runner = factory
        return run_service(catalog, args.project, args.nonce)
    context = catalog.context(args.project, args.workspace)
    with lock(context.checkout_lock), lock(context.state_dir / 'runner.lock'):
        atomic_json(context.state_dir / 'runner-process.json', {**identity, 'identity': context.identity(),
                    'recovery_protocol': 1})
        atomic_json(root / 'runner-ready.json', identity)
        # This process only represents a live owner: it never dispatches or
        # consumes technical commands, mailbox messages, acceptance, or tracking.
        while True:
            signal.pause()


class ManagerCrash(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'config', self.root / 'state')
        self.project = self.catalog.project('Owned crash fixture')['id']
        self.repo = self.root / 'checkout'; prepare_checkout(self.repo)
        self.workspace = self.catalog.workspace(self.project, {'path': str(self.repo), 'orca_id': 'fixture',
                                                              'orca_instance': 'instance'})['id']
        self.runner = Runner(self.catalog.context(self.project, self.workspace))
        self.services = FixtureServices(self.catalog, timeout=.2, wait_timeout=4)
        self.manager_children = []
        def spawn(nonce):
            child = spawn_fixture(self.root, 'manager', '--nonce', nonce)
            self.manager_children.append(child)
            return child
        self.manager = Manager(self.catalog, spawn=spawn, wait_timeout=4)
        self.runner_child = None
        self.addCleanup(self.cleanup)

    def wait_for(self, predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.03)
        self.fail(message)

    def owned(self):
        return read_json(self.root / 'owned.json', [])

    def checked_signal(self, record, sig):
        self.assertEqual(record['root'], str(self.root))
        self.assertIn(record, self.owned())
        self.assertEqual(record['boot_id'], boot_id())
        self.assertEqual(record['process_stamp'], process_stamp(record['pid']))
        os.kill(record['pid'], sig)

    def cleanup(self):
        errors = []
        try:
            service_intent.cancel(self.catalog, self.project, str(uuid.uuid4()))
            self.services.action(self.project, 'stop', str(uuid.uuid4()))
            self.manager.stop(str(uuid.uuid4()))
        except Exception as exc:
            errors.append(str(exc))
        for child in [*self.manager_children, *self.services.children, *([self.runner_child] if self.runner_child else [])]:
            try:
                if child is self.runner_child and child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=3)
        # A restarted service is the manager's child, not this test's Popen.
        for record in self.owned():
            if not departed(record) and record['process_stamp'] == process_stamp(record['pid']):
                self.checked_signal(record, signal.SIGTERM)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and any(not departed(record) for record in self.owned()):
            time.sleep(.03)
        survivors = [record for record in self.owned() if not departed(record)]
        for record in survivors:
            self.checked_signal(record, signal.SIGKILL)
        self.assertFalse(survivors, 'owned fixture children survived cooperative cleanup')
        self.temp.cleanup()
        self.assertFalse(errors, '; '.join(errors))

    def test_service_crash_restores_supervisor_without_duplicate_live_owner(self):
        state_dir = self.runner.state_dir
        attempt = state_dir / 'attempts' / str(uuid.uuid4())
        self.runner.context.snapshot(attempt)
        registry = {'fenced': False, 'agents': [{'id': 'fixture-agent', 'dispatch_id': 'fixture-dispatch',
                     'status': 'running'}], 'runs': []}
        atomic_json(attempt / 'agents.json', registry)
        state = {'goal': 'feat/goal-fixture', 'run_id': 'fixture-run', 'history': [],
                 'current': {'item': 'one/T1', 'attempt_dir': str(attempt), 'dispatch_id': 'fixture-dispatch'},
                 'recovery_issues': ['fixture requires technical observation']}
        self.runner.save_state(state)
        atomic_json(state_dir / 'recovery-settings.json', {'enabled': True, 'intent': 'running'})
        self.runner_child = spawn_fixture(self.root, 'runner', '--project', self.project,
                                          '--workspace', self.workspace, '--nonce', str(uuid.uuid4()))
        owner = self.wait_for(lambda: read_json(self.root / 'runner-ready.json'), 'fixture owner did not hold locks')
        state_bytes = self.runner.state_file.read_bytes()
        registry_bytes = (attempt / 'agents.json').read_bytes()
        snapshots = {str(p.relative_to(attempt)): p.read_bytes() for p in attempt.rglob('*') if p.is_file()}
        first = self.services.action(self.project, 'start', str(uuid.uuid4()))
        self.assertEqual(first['status'], 'done')
        first_record = self.services.record(self.project)
        manager = self.manager.ensure(str(uuid.uuid4()))
        self.assertTrue(manager['confirmed']); self.assertEqual(manager['state'], 'running')
        journal = state_dir / 'recovery.json'
        self.wait_for(lambda: read_json(journal, {}).get('mode') == 'live', 'initial Supervisor did not observe live owner')
        watchdog = self.services.directory(self.project) / 'watchdog.json'
        self.wait_for(lambda: read_json(watchdog, {}).get('observed_incarnation') == first_record['nonce'],
                      'live manager did not observe initial service')
        owned_service = next(row for row in self.owned() if row['role'] == 'service' and row['nonce'] == first_record['nonce'])
        self.assertEqual(owned_service['pid'], first_record['pid'])
        self.assertEqual(owned_service['process_stamp'], first_record['process_stamp'])
        self.checked_signal(owned_service, signal.SIGKILL)
        self.wait_for(lambda: departed(owned_service), 'owned crashed service did not exit')
        journal.unlink(missing_ok=True)
        def restored():
            status = self.services.status(self.project)
            return status if status['confirmed'] and status['state'] == 'running' and status.get('nonce') != first_record['nonce'] else None
        second = self.wait_for(restored, 'manager did not restore owned crashed service', timeout=10)
        self.wait_for(lambda: read_json(journal, {}).get('mode') == 'live', 'restarted Supervisor did not observe live owner')
        self.wait_for(lambda: read_json(watchdog, {}).get('observed_incarnation') == second['nonce'],
                      'manager did not settle the recovered service incarnation')
        self.assertEqual(self.manager.status()['nonce'], manager['nonce'])
        self.assertEqual(process_stamp(owner['pid']), owner['process_stamp'])
        self.assertIsNone(self.runner_child.poll())
        self.assertTrue(running(state_dir))
        self.assertEqual(self.runner.state_file.read_bytes(), state_bytes)
        self.assertEqual((attempt / 'agents.json').read_bytes(), registry_bytes)
        self.assertEqual({str(p.relative_to(attempt)): p.read_bytes() for p in attempt.rglob('*') if p.is_file()}, snapshots)
        records = self.owned()
        self.assertEqual(len([r for r in records if r['role'] == 'manager']), 1)
        self.assertEqual(len([r for r in records if r['role'] == 'runner']), 1)
        self.assertEqual(len([r for r in records if r['role'] == 'service']), 2)
        self.assertNotEqual(second['pid'], first_record['pid'])
        self.assertFalse((self.root / 'transport' / 'transport.json').exists(), 'unexpected Orca effect/dispatch')


if __name__ == '__main__':
    if '--fixture' in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument('--fixture', choices=('manager', 'service', 'runner'))
        parser.add_argument('--root', type=Path, required=True)
        for name in ('project', 'workspace', 'nonce'):
            parser.add_argument('--' + name)
        fixture_main(parser.parse_args())
    else:
        unittest.main()
