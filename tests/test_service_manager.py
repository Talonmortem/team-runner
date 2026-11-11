"""Manager proofs use temporary roots, fake services, and owned subprocesses only."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from team_runner.catalog import Catalog
from team_runner.runner_store import atomic_json, read_json, lock
from team_runner.runner_entry import process_stamp, boot_id
from team_runner.service_manager import Manager


class ManagerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'config', self.root / 'state')
        self.manager = Manager(self.catalog, wait_timeout=2)
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        self.manager.stop(str(uuid.uuid4()))
        for child in self.manager.children:
            try:
                child.wait(timeout=4)
            except TimeoutError:
                child.kill()
                child.wait(timeout=4)
            except __import__('subprocess').TimeoutExpired:
                child.kill()
                child.wait(timeout=4)

    def wait_stopped(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = self.manager.status()
            if state['state'] == 'stopped' and state['confirmed']:
                return state
            time.sleep(.02)
        self.fail('owned manager did not exit')

    def test_status_is_read_only(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        self.assertEqual(self.manager.status(), {'state': 'stopped', 'confirmed': True})
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_concurrent_ensure_one_detached_owner_and_cooperative_stop(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.manager.ensure, [str(uuid.uuid4()) for _ in range(4)]))
        self.assertEqual(len(self.manager.children), 1)
        self.assertEqual(len({value['nonce'] for value in results}), 1)
        status = self.manager.status()
        self.assertEqual(status['state'], 'running')
        self.assertTrue(status['confirmed'])
        self.assertEqual(status['roots'], self.manager._roots())
        self.assertNotEqual(status['pid'], os.getpid())
        other = Manager(Catalog(self.catalog.config_root, self.catalog.state_root))
        self.assertEqual(other.status()['nonce'], status['nonce'])
        other.stop(str(uuid.uuid4()))
        self.wait_stopped()

    def test_interrupted_spawn_and_missing_identity_do_not_retry(self):
        nonce = str(uuid.uuid4())
        atomic_json(self.manager.directory / 'instance.json', {
            'schema_version': 1, 'nonce': nonce, 'roots': self.manager._roots(), 'state': 'starting',
            'pid': None, 'process_stamp': None, 'boot_id': boot_id()})
        with patch.object(self.manager, 'spawn') as spawn:
            for _ in range(2):
                result = self.manager.ensure(str(uuid.uuid4()))
                self.assertFalse(result['confirmed'])
                self.assertEqual(result['nonce'], nonce)
            spawn.assert_not_called()

    def test_proven_pre_spawn_failure_new_request_only(self):
        request_id = str(uuid.uuid4())
        with patch.object(self.manager, 'spawn', side_effect=OSError('fixture')) as spawn:
            result = self.manager.ensure(request_id)
            self.assertTrue(result['confirmed'])
            self.assertEqual(result['state'], 'stopped')
            self.manager.ensure(request_id)
            self.assertEqual(spawn.call_count, 1)
        result = self.manager.ensure(str(uuid.uuid4()))
        self.assertEqual(result['state'], 'running')
        self.assertTrue(result['confirmed'])

    def test_claim_and_lifetime_lock_both_required(self):
        nonce = str(uuid.uuid4())
        record = {'schema_version': 1, 'nonce': nonce, 'roots': self.manager._roots(), 'state': 'running',
                  'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid()), 'boot_id': boot_id()}
        atomic_json(self.manager.directory / 'instance.json', record)
        atomic_json(self.manager.directory / (nonce + '.claim'), record)
        self.assertFalse(self.manager.status()['confirmed'])
        with lock(self.manager.directory / 'instance.lock'):
            self.assertTrue(self.manager.status()['confirmed'])
            atomic_json(self.manager.directory / (nonce + '.claim'), dict(record, pid=1))
            self.assertFalse(self.manager.status()['confirmed'])

    def test_old_stop_receipt_cannot_stop_new_incarnation(self):
        first = self.manager.ensure(str(uuid.uuid4()))
        stop_id = str(uuid.uuid4())
        self.manager.stop(stop_id)
        self.wait_stopped()
        second = self.manager.ensure(str(uuid.uuid4()))
        self.assertNotEqual(first['nonce'], second['nonce'])
        self.assertEqual(self.manager.stop(stop_id)['state'], 'superseded')
        self.assertEqual(self.manager.status()['state'], 'running')

    def test_pending_stop_is_not_available_to_ensure(self):
        running = self.manager.ensure(str(uuid.uuid4()))
        with patch.object(self.manager, 'spawn') as spawn:
            self.manager.stop(str(uuid.uuid4()))
            result = self.manager.ensure(str(uuid.uuid4()))
            self.assertNotEqual(result['state'], 'running')
            if result.get('nonce'):
                self.assertEqual(result['nonce'], running['nonce'])
            spawn.assert_not_called()

    def test_readiness_timeout_observes_same_spawn(self):
        self.manager.wait_timeout = 0
        request = str(uuid.uuid4())
        first = self.manager.ensure(request)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.manager.ensure(request)
            if result['confirmed']:
                break
            time.sleep(.02)
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['nonce'], first['nonce'])
        self.assertEqual(len(self.manager.children), 1)

    def test_child_lifecycle_rejected_status_allowed(self):
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': '/fixture/attempt'}):
            for function in (self.manager.ensure, self.manager.stop):
                with self.assertRaisesRegex(ValueError, 'child attempts'):
                    function(str(uuid.uuid4()))
            self.assertEqual(self.manager.status()['state'], 'stopped')

    def test_slow_project_does_not_block_another(self):
        import threading
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        atomic_json(self.catalog.path, {'version': 1, 'projects': {first: {}, second: {}}})
        nonce = str(uuid.uuid4())
        atomic_json(self.manager.directory / 'instance.json', {'schema_version': 1, 'nonce': nonce,
                    'roots': self.manager._roots(), 'state': 'starting'})
        blocked, release, progressed = threading.Event(), threading.Event(), threading.Event()
        def tick(project):
            if project == first:
                blocked.set()
                release.wait(2)
            else:
                progressed.set()
        self.manager.tick = tick
        thread = threading.Thread(target=self.manager.run, args=(nonce,), kwargs={'poll_interval': .01})
        thread.start()
        try:
            self.assertTrue(blocked.wait(1))
            self.assertTrue(progressed.wait(1))
            self.manager.stop(str(uuid.uuid4()))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_parent_process_exit_does_not_stop_manager(self):
        import subprocess
        code = ("import json,sys; from pathlib import Path; from team_runner.catalog import Catalog; "
                "from team_runner.service_manager import Manager; "
                "print(json.dumps(Manager(Catalog(Path(sys.argv[1]),Path(sys.argv[2]))).ensure(sys.argv[3])))")
        result = subprocess.run([__import__('sys').executable, '-c', code,
                                 str(self.catalog.config_root), str(self.catalog.state_root), str(uuid.uuid4())],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manager.status()['state'], 'running')
        self.manager.stop(str(uuid.uuid4()))
        self.wait_stopped()


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.catalog = Catalog(root / 'config', root / 'state')
        self.project = str(uuid.uuid4())
        self.directory = self.catalog.state_root / 'projects' / self.project / 'service'
        self.intent = {'epoch': 1, 'desired_state': 'running', 'policy': {'max_attempts': 2}}
        self.now = 100.0
        self.calls = []
        self.record = {}
        self.observed = {'state': 'stopped', 'confirmed': True}
        self.finish = 'error'
        def action(project, verb, request_id, **kwargs):
            self.calls.append((project, verb, request_id, kwargs))
            atomic_json(self.directory / 'commands' / (request_id + '.json'), {'status': self.finish})
        self.services = SimpleNamespace(observe=lambda project: dict(self.observed),
                                        record=lambda project: dict(self.record), action=action)
        self.manager = Manager(self.catalog, self.services, clock=lambda: self.now)
        self.intent_module = SimpleNamespace(read=lambda *args: self.intent,
            permitted=lambda cat, project, epoch: self.intent['desired_state'] == 'running' and self.intent['epoch'] == epoch)
        self.modules = patch.dict('sys.modules', {'team_runner.service_intent': self.intent_module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        import team_runner
        self.attribute = patch.object(team_runner, 'service_intent', self.intent_module, create=True)
        self.attribute.start()
        self.addCleanup(self.attribute.stop)

    def tick(self):
        value = self.manager.tick(self.project)
        self.now += 100
        return value

    def test_bounded_budget_survives_manager_restart(self):
        first = self.tick()
        self.assertEqual(first['attempts'], 1)
        self.manager = Manager(self.catalog, self.services, clock=lambda: self.now)
        second = self.tick()
        self.assertEqual(second['attempts'], 2)
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        third = self.tick()
        self.assertEqual(third['phase'], 'needs_action')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][3], {'expected_service_epoch': 1})
        self.tick()
        self.assertEqual(len(self.calls), 2)

    def test_unknown_effect_retains_uuid_and_never_replays(self):
        self.finish = 'unknown'
        first = self.tick()
        for _ in range(22):
            value = self.tick()
            self.assertEqual(value['operation_id'], first['operation_id'])
        self.assertEqual(value['phase'], 'needs_action')
        self.assertEqual(len(self.calls), 1)

    def test_backoff_and_stop_suppression_persist(self):
        value = self.manager.tick(self.project)
        self.assertGreater(value['next_check_at'], self.now)
        self.manager.tick(self.project)
        self.assertEqual(len(self.calls), 1)
        self.intent.update(epoch=2, desired_state='stopped')
        self.now += 100
        self.manager = Manager(self.catalog, self.services, clock=lambda: self.now)
        self.assertEqual(self.tick()['phase'], 'suppressed')
        self.assertEqual(len(self.calls), 1)

    def test_unresponsive_live_owner_is_not_dead(self):
        self.record = {'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid()), 'boot_id': boot_id()}
        self.observed = {'state': 'unknown', 'confirmed': False}
        self.assertEqual(self.tick()['phase'], 'observing')
        self.assertEqual(self.calls, [])

    def test_foreign_healthy_incarnation_after_unknown_effect_needs_action(self):
        self.finish = 'unknown'
        self.tick()
        self.observed = {'state': 'running', 'confirmed': True, 'nonce': str(uuid.uuid4())}
        self.assertEqual(self.tick()['phase'], 'needs_action')
        self.assertEqual(len(self.calls), 1)

    def test_stop_during_observation_fences_effect(self):
        def observe(project):
            self.intent.update(epoch=2, desired_state='stopped')
            return {'state': 'stopped', 'confirmed': True}
        self.services.observe = observe
        self.assertEqual(self.tick()['phase'], 'suppressed')
        self.assertEqual(self.calls, [])

    def test_healthy_service_and_recovery_match_do_not_restart(self):
        self.observed = {'state': 'running', 'confirmed': True, 'nonce': str(uuid.uuid4())}
        self.assertEqual(self.tick()['phase'], 'observing')
        self.assertEqual(self.calls, [])

    def test_invalid_intent_persists_precise_blocker(self):
        self.intent_module.read = lambda *args: (_ for _ in ()).throw(ValueError('invalid'))
        value = self.tick()
        self.assertEqual(value['phase'], 'needs_action')
        self.assertEqual(value['blocker'], 'invalid service authorization')
        self.assertEqual(read_json(self.directory / 'watchdog.json'), value)
        self.assertEqual(self.calls, [])

    def test_interrupted_proven_no_effect_receipt_allows_bounded_new_attempt(self):
        self.finish = 'unknown'
        first = self.tick()
        nonce = str(uuid.uuid4())
        operation = self.directory / 'commands' / (first['operation_id'] + '.json')
        atomic_json(operation, {'status': 'applying', 'nonce': nonce})
        self.record = {'state': 'error', 'no_effect': True, 'nonce': nonce, 'pid': None}
        self.observed = {'state': 'error', 'confirmed': True}
        second = self.tick()
        self.assertEqual(second['attempts'], 2)
        self.assertNotEqual(second['operation_id'], first['operation_id'])
        self.assertEqual(len(self.calls), 2)

    def test_malformed_watchdog_stably_blocks_without_observation_or_effect(self):
        import copy
        valid = self.tick()
        corruptions = [None, [], {'schema_version': 2},
                       dict(valid, checks='many'), dict(valid, checks=-1),
                       dict(valid, attempts=True), dict(valid, attempts=4),
                       dict(valid, next_check_at=float('nan')), dict(valid, next_check_at=float('inf')),
                       dict(valid, next_check_at=-1), dict(valid, epoch=False),
                       dict(valid, phase='unknown_phase'), dict(valid, incident='../escape'),
                       dict(valid, operation_id='bad'), dict(valid, identity={'project_id': self.project})]
        path = self.directory / 'watchdog.json'
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                atomic_json(path, copy.deepcopy(corrupt))
                with patch.object(self.services, 'observe') as observe:
                    first = self.manager.tick(self.project)
                    before = path.read_bytes()
                    second = Manager(self.catalog, self.services, clock=lambda: self.now + 100).tick(self.project)
                    observe.assert_not_called()
                self.assertEqual(first, second)
                self.assertEqual(first['phase'], 'needs_action')
                self.assertEqual(first['blocker'], 'invalid_watchdog_journal')
                self.assertEqual(first['identity'], {'project_id': self.project, **self.manager._roots()})
                self.assertEqual(path.read_bytes(), before)
        path.write_text('{broken json')
        self.assertEqual(self.manager.tick(self.project)['blocker'], 'invalid_watchdog_journal')
        self.assertEqual(len(self.calls), 1)

    def test_observation_errors_exhaust_persisted_budget(self):
        with patch.object(self.services, 'observe', side_effect=OSError('fixture')):
            for _ in range(22):
                value = self.tick()
        self.assertEqual(value['phase'], 'needs_action')
        self.assertEqual(value['checks'], 21)
        self.assertEqual(value['blocker'], 'service observation unavailable')
        self.assertEqual(self.calls, [])

    def test_no_authorization_no_effect(self):
        self.intent = {}
        self.assertEqual(self.tick()['phase'], 'suppressed')
        self.assertEqual(self.calls, [])

class ManagerIntegration(unittest.TestCase):
    def test_authorized_service_start_then_manual_stop_suppresses_watchdog(self):
        from team_runner.project_services import ProjectServices
        from team_runner import service_intent
        with tempfile.TemporaryDirectory() as root:
            catalog = Catalog(Path(root) / 'config', Path(root) / 'state')
            project = catalog.project(name='isolated manager fixture')['id']
            services = ProjectServices(catalog)
            manager = Manager(catalog, services)
            service_intent.authorize(catalog, project, str(uuid.uuid4()))
            try:
                value = manager.tick(project)
                self.assertEqual(value['attempts'], 1)
                observed = services.status(project)
                self.assertEqual(observed['state'], 'running')
                self.assertTrue(observed['confirmed'])
                operation = read_json(services.directory(project) / 'commands' / (value['operation_id'] + '.json'))
                self.assertEqual(operation['status'], 'done')
                self.assertEqual(operation['data']['service_epoch'], 1)
                services.action(project, 'stop', str(uuid.uuid4()))
                self.assertEqual(Manager(catalog, services).tick(project)['phase'], 'suppressed')
                self.assertEqual(services.status(project)['state'], 'stopped')
            finally:
                services.action(project, 'stop', str(uuid.uuid4()))
                for child in services.children:
                    try:
                        child.wait(timeout=4)
                    except __import__('subprocess').TimeoutExpired:
                        child.kill()
                        child.wait(timeout=4)

    def test_service_adoption_receipt_names_exact_incarnation(self):
        from team_runner.project_services import ProjectServices
        from team_runner import service_intent
        with tempfile.TemporaryDirectory() as root:
            catalog = Catalog(Path(root) / 'config', Path(root) / 'state')
            project = catalog.project(name='fixture')['id']
            services = ProjectServices(catalog)
            epoch = service_intent.authorize(catalog, project, str(uuid.uuid4()))['epoch']
            observed = {'confirmed': True, 'state': 'running', 'nonce': str(uuid.uuid4())}
            with patch.object(services, 'observe', return_value=observed), patch.object(services, 'spawn') as spawn:
                operation = services.action(project, 'start', str(uuid.uuid4()), expected_service_epoch=epoch)
            self.assertEqual(operation['status'], 'done')
            self.assertEqual(operation['nonce'], observed['nonce'])
            spawn.assert_not_called()

    def test_service_spawn_oserror_settles_without_unknown_replay(self):
        from team_runner.project_services import ProjectServices
        from team_runner import service_intent
        with tempfile.TemporaryDirectory() as root:
            catalog = Catalog(Path(root) / 'config', Path(root) / 'state')
            project = catalog.project(name='fixture')['id']
            services = ProjectServices(catalog)
            service_intent.authorize(catalog, project, str(uuid.uuid4()))
            now = [100.0]
            manager = Manager(catalog, services, clock=lambda: now[0])
            with patch.object(services, 'spawn', side_effect=OSError('fixture')) as spawn:
                for count in range(3):
                    value = manager.tick(project)
                    record = read_json(services.directory(project) / 'commands' / (value['operation_id'] + '.json'))
                    self.assertEqual(record['status'], 'error')
                    self.assertTrue(record['no_effect'])
                    now[0] += 100
                self.assertEqual(manager.tick(project)['phase'], 'needs_action')
                self.assertEqual(spawn.call_count, 3)


if __name__ == '__main__':
    unittest.main()
