"""Durable execute boundaries using fake services and launch effects only."""
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_execute, goal_intent
from team_runner.protocol import ContractError
from team_runner.runner_store import atomic_json, read_json


class ManagerFixture:
    def __init__(self):
        self.requests = []
        self.observation = {'state': 'running', 'confirmed': True}

    def ensure(self, identifier):
        self.requests.append(identifier)
        return self.observation


class Services:
    def __init__(self):
        self.actions = []
        self.observation = {'confirmed': True, 'state': 'running'}
        self.before = lambda: None

    def action(self, project, action, identifier, **kwargs):
        self.before()
        self.actions.append((project, action, identifier))
        return {'status': 'done'}

    def status(self, project):
        return self.observation


class GoalExecute(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def options(self):
        return {'goal': application.goals(self.a)[0]}

    def execute(self, identifier=None, options=None, **kwargs):
        kwargs.setdefault('manager', ManagerFixture())
        return goal_execute.execute(self.a, self.options() if options is None else options,
                                    identifier or str(uuid.uuid4()), **kwargs)

    def test_authorization_precedes_service_and_launch(self):
        services = Services()
        services.before = lambda: self.assertEqual(goal_intent.read(self.a)['desired_state'], 'running')
        def start(runner, options, **kwargs):
            self.assertEqual(options, goal_intent.read(runner)['options'])
            self.assertEqual(options['mode'], 'auto')
            self.assertTrue(options['final'])
            self.assertEqual(len(services.actions), 1)
            return {'status': 'starting', 'nonce': 'fixture'}
        result = self.execute(services=services, start=start)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['status'], 'accepted')
        self.assertFalse(self.a.state_file.exists())

    def test_concurrent_identical_requests_launch_once(self):
        services = Services()
        with patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(lambda _: self.execute(services=services), range(5)))
        self.assertEqual(start.call_count, 1)
        self.assertEqual(len(services.actions), 1)
        self.assertEqual({r['status'] for r in results}, {'succeeded'})
        self.assertEqual(len({r['result']['generation'] for r in results}), 1)

    def test_terminal_receipt_survives_pause_and_repeat(self):
        services = Services(); identifier = str(uuid.uuid4())
        with patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            first = self.execute(identifier, services=services)
            before = goal_execute._path(self.a, identifier).read_bytes()
            goal_intent.change(self.a, desired_state='paused')
            self.assertEqual(self.execute(identifier, services=services), first)
            self.assertEqual(goal_execute.lookup(self.a, identifier), first)
            self.assertEqual(goal_execute._path(self.a, identifier).read_bytes(), before)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'paused')

    def test_same_id_payload_conflict(self):
        services = Services(); identifier = str(uuid.uuid4())
        self.execute(identifier, services=services, start=lambda *_, **kwargs: {'status': 'starting'})
        with self.assertRaisesRegex(ContractError, 'already used'):
            self.execute(identifier, {**self.options(), 'final': False}, services=services)

    def test_service_unknown_is_observed_without_new_action(self):
        services = Services(); services.observation = {'confirmed': False, 'state': 'unknown'}
        identifier = str(uuid.uuid4())
        with patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            first = self.execute(identifier, services=services)
            self.assertEqual(first['status'], 'unknown'); start.assert_not_called()
            self.assertEqual(self.execute(identifier, services=services)['status'], 'unknown')
            self.assertEqual(self.execute(services=services)['status'], 'unknown')
            self.assertEqual(len(services.actions), 1)
            services.observation = {'confirmed': True, 'state': 'running'}
            self.assertEqual(self.execute(identifier, services=services), first)
            self.assertEqual(self.execute(services=services)['status'], 'succeeded')
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(services.actions), 1)

    def test_unknown_launch_never_repeated_even_with_new_request_id(self):
        services = Services(); identifier = str(uuid.uuid4())
        with patch('team_runner.service.start_runner', side_effect=TimeoutError) as start:
            self.assertEqual(self.execute(identifier, services=services)['status'], 'unknown')
            self.assertEqual(self.execute(identifier, services=services)['status'], 'unknown')
            self.assertEqual(self.execute(services=services)['status'], 'unknown')
        self.assertEqual(start.call_count, 1)
        self.assertEqual(len(services.actions), 1)

    def test_lost_launch_reply_resolves_exact_receipt(self):
        services = Services(); identifier = str(uuid.uuid4())
        def start(runner, options, **kwargs):
            auth = goal_intent.read(runner)
            nonce = str(uuid.uuid4())
            atomic_json(runner.state_dir / 'launch.json', {'nonce': nonce, 'identity': runner.context.identity(),
                        'options': options, 'authorization': {k: auth[k] for k in ('generation', 'epoch')}})
            atomic_json(runner.state_dir / 'startup.json', {'nonce': nonce, 'status': 'starting', 'terminal': 'fixture'})
            raise TimeoutError('lost reply')
        result = self.execute(identifier, services=services, start=start)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(goal_execute.lookup(self.a, identifier), result)

    def test_starting_marker_without_effect_receipt_is_unknown(self):
        services = Services()
        def start(runner, options, **kwargs):
            auth = goal_intent.read(runner); nonce = str(uuid.uuid4())
            atomic_json(runner.state_dir / 'launch.json', {'nonce': nonce, 'identity': runner.context.identity(),
                        'options': options, 'authorization': {k: auth[k] for k in ('generation', 'epoch')}})
            atomic_json(runner.state_dir / 'startup.json', {'nonce': nonce, 'status': 'starting'})
            raise TimeoutError()
        self.assertEqual(self.execute(services=services, start=start)['status'], 'unknown')

    def test_cancel_between_service_and_launch(self):
        services = Services()
        def status(project):
            goal_intent.change(self.a, desired_state='stopped')
            return {'confirmed': True, 'state': 'running'}
        services.status = status
        with patch('team_runner.service.start_runner') as start:
            result = self.execute(services=services)
            start.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'authorization_cancelled')

    def test_crash_after_intent_before_execute_record_can_continue(self):
        identifier = str(uuid.uuid4()); services = Services()
        auth = goal_intent.authorize(self.a, self.options(), identifier)
        result = self.execute(identifier, services=services, start=lambda *_, **kwargs: {'status': 'starting'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['generation'], auth['generation'])
        self.assertEqual(len(services.actions), 1)

    def test_crash_after_launch_phase_never_repeats_effect(self):
        services = Services(); identifier = str(uuid.uuid4())
        class Crash(BaseException):
            pass
        with self.assertRaises(Crash):
            self.execute(identifier, services=services, start=lambda *_, **kwargs: (_ for _ in ()).throw(Crash()))
        with patch('team_runner.service.start_runner') as start:
            self.assertEqual(self.execute(identifier, services=services)['status'], 'unknown')
            start.assert_not_called()
        self.assertEqual(len(services.actions), 1)

    def test_lookup_is_read_only_and_projects_shared_receipt(self):
        services = Services(); identifier = str(uuid.uuid4())
        result = self.execute(identifier, services=services, start=lambda *_, **kwargs: {'status': 'starting'})
        path = goal_execute._path(self.a, identifier)
        record = read_json(path); record.update(status='running', result=None)
        atomic_json(path, record); before = path.read_bytes()
        with patch.object(Services, 'status', side_effect=AssertionError), patch('team_runner.service.start_runner') as start:
            self.assertEqual(goal_execute.lookup(self.a, identifier), result)
            start.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_request_collision_rejected_before_authorizing(self):
        identifier = str(uuid.uuid4())
        atomic_json(self.a.state_dir / 'commands' / (identifier + '.json'), {'id': identifier})
        with self.assertRaisesRegex(ContractError, 'already used'):
            self.execute(identifier, services=Services())
        self.assertEqual(goal_intent.read(self.a), {})

    def test_foreign_journal_is_rejected(self):
        identifier = str(uuid.uuid4())
        self.execute(identifier, services=Services(), start=lambda *_, **kwargs: {'status': 'starting'})
        atomic_json(goal_execute._path(self.b, identifier), read_json(goal_execute._path(self.a, identifier)))
        with self.assertRaisesRegex(ContractError, 'another workspace'):
            goal_execute.lookup(self.b, identifier)

    def test_existing_start_runner_persists_authorized_launch(self):
        services = Services()
        with patch.object(self.a.launcher, 'create_terminal', return_value={'handle': 'fixture'}) as create:
            result = self.execute(services=services)
        self.assertEqual(result['status'], 'succeeded')
        create.assert_called_once()
        launch = read_json(self.a.state_dir / 'launch.json')
        intent = goal_intent.read(self.a)
        self.assertEqual(launch['options'], intent['options'])
        self.assertEqual(launch['authorization'], {k: intent[k] for k in ('generation', 'epoch')})
        self.assertEqual(len(services.actions), 1)

    def test_pause_resume_between_execute_check_and_start_cannot_launch_old_epoch(self):
        from team_runner.service import start_runner
        def start(runner, options, **kwargs):
            paused = goal_intent.change(runner, desired_state='paused')
            goal_intent.authorize(runner, {'resume': True}, str(uuid.uuid4()),
                                  generation=paused['generation'], epoch=paused['epoch'])
            return start_runner(runner, options, **kwargs)
        with patch.object(self.a.launcher, 'create_terminal') as create:
            result = self.execute(services=Services(), start=start)
            create.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'authorization_cancelled')
        self.assertFalse((self.a.state_dir / 'launch.json').exists())

    def test_shared_journal_scope_mismatch_fails_closed(self):
        services = Services(); identifier = str(uuid.uuid4())
        services.observation = {'confirmed': False, 'state': 'unknown'}
        self.execute(identifier, services=services)
        auth = goal_intent.read(self.a)
        receipt = read_json(goal_execute._path(self.a, identifier))
        path = goal_execute._shared_path(self.a, auth['generation'], auth['epoch'], receipt['authorization']['service_epoch'])
        shared = read_json(path); shared['generation'] = str(uuid.uuid4()); atomic_json(path, shared)
        with patch('team_runner.service.start_runner') as start:
            with self.assertRaisesRegex(ContractError, 'authorization mismatch'):
                self.execute(services=services)
            start.assert_not_called()
        self.assertEqual(len(services.actions), 1)

    def test_service_readiness_wait_does_not_hold_cancellation_lock(self):
        from types import SimpleNamespace
        from team_runner.project_services import ProjectServices
        from team_runner.runner_store import lock
        services = ProjectServices(self.catalog)
        def wait(*args):
            with lock(self.a.state_dir / 'goal-intent.lock', blocking=False):
                pass
            goal_intent.change(self.a, desired_state='paused')
            return {'confirmed': True, 'state': 'running'}
        with patch.object(services, 'observe', return_value={'confirmed': True, 'state': 'stopped'}), \
                patch.object(services, 'status', return_value={'confirmed': True, 'state': 'running'}), \
                patch.object(services, 'spawn', return_value=SimpleNamespace(pid=12345)) as spawn, \
                patch.object(services, '_wait', side_effect=wait), \
                patch('team_runner.service.start_runner') as start:
            result = self.execute(services=services)
            spawn.assert_called_once(); start.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'authorization_cancelled')

    def test_cancellation_during_service_observation_prevents_spawn(self):
        from team_runner.project_services import ProjectServices
        services = ProjectServices(self.catalog)
        def observe(*args):
            goal_intent.change(self.a, desired_state='stopped')
            return {'confirmed': True, 'state': 'stopped'}
        with patch.object(services, 'observe', side_effect=observe), \
                patch.object(services, 'spawn') as spawn, patch('team_runner.service.start_runner') as start:
            result = self.execute(services=services)
            spawn.assert_not_called(); start.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'authorization_cancelled')
        self.assertFalse((services.directory(self.p1) / 'instance.json').exists())

    def running_fixture(self):
        import os
        from team_runner.runner_entry import process_stamp, boot_id
        auth = goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
        intent = goal_intent.read(self.a); nonce = str(uuid.uuid4())
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'options': intent['options'], 'authorization': {k: auth[k] for k in ('generation', 'epoch')}})
        atomic_json(self.a.state_dir / 'startup.json', {'nonce': nonce, 'status': 'running'})
        atomic_json(self.a.state_dir / 'runner-process.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid()), 'boot_id': boot_id()})
        (self.a.state_dir / ('launch-' + nonce + '.claim')).touch()
        return auth, intent['options']

    def test_live_matching_runner_is_adopted_despite_held_checkout_lock(self):
        from team_runner.service import start_runner
        from team_runner.runner_store import lock
        auth, options = self.running_fixture()
        with lock(self.a.context.checkout_lock), lock(self.a.state_dir / 'runner.lock'), \
                patch.object(self.a.launcher, 'create_terminal') as create:
            result = start_runner(self.a, options, expected_authorization=auth)
            create.assert_not_called()
        self.assertEqual(result['status'], 'running')

    def test_live_runner_with_foreign_nonce_is_not_adopted(self):
        from team_runner.service import start_runner
        from team_runner.runner_store import lock
        auth, options = self.running_fixture()
        process = read_json(self.a.state_dir / 'runner-process.json')
        process['nonce'] = str(uuid.uuid4()); atomic_json(self.a.state_dir / 'runner-process.json', process)
        with lock(self.a.context.checkout_lock), lock(self.a.state_dir / 'runner.lock'), \
                patch.object(self.a.launcher, 'create_terminal') as create:
            with self.assertRaisesRegex(ValueError, 'unconfirmed'):
                start_runner(self.a, options, expected_authorization=auth)
            create.assert_not_called()

    def test_arbitrary_checkout_lock_is_not_runner_evidence(self):
        from team_runner.service import start_runner
        from team_runner.runner_store import lock
        auth, options = self.running_fixture()
        with lock(self.a.context.checkout_lock), patch.object(self.a.launcher, 'create_terminal') as create:
            with self.assertRaises(RuntimeError):
                start_runner(self.a, options, expected_authorization=auth)
            create.assert_not_called()

    def test_manager_is_confirmed_before_service_start(self):
        manager = ManagerFixture(); services = Services()
        services.before = lambda: self.assertEqual(len(manager.requests), 1)
        result = self.execute(manager=manager, services=services, start=lambda *_, **kwargs: {'status': 'starting'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(manager.requests), 1)

    def test_manager_unknown_reuses_saved_request_and_has_no_service_effect(self):
        manager = ManagerFixture(); manager.observation = {'state': 'unknown', 'confirmed': False}
        services = Services(); identifier = str(uuid.uuid4())
        with patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            self.assertEqual(self.execute(identifier, manager=manager, services=services)['status'], 'unknown')
            self.assertEqual(self.execute(identifier, manager=manager, services=services)['status'], 'unknown')
            self.assertEqual(services.actions, []); start.assert_not_called()
            self.assertEqual(len(set(manager.requests)), 1)
            frozen = goal_execute.lookup(self.a, identifier)
            manager.observation = {'state': 'running', 'confirmed': True}
            self.assertEqual(self.execute(identifier, manager=manager, services=services), frozen)
            self.assertEqual(self.execute(manager=manager, services=services)['status'], 'succeeded')
            self.assertEqual(goal_execute.lookup(self.a, identifier), frozen)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(set(manager.requests)), 1)

    def _completed_execute(self, final):
        import test_goal_completion
        from team_runner import goal_completion, service_intent
        test_goal_completion.GoalCompletion.prepare(self, final=final)
        goal_completion.record(self.a, self.state)
        intent = goal_intent.read(self.a)
        services = Services(); manager = ManagerFixture()
        with patch('team_runner.service.start_runner') as start:
            result = self.execute(options={'resume': True}, generation=intent['generation'], epoch=intent['epoch'],
                                  services=services, manager=manager)
            start.assert_not_called()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['status'], 'already_completed')
        self.assertEqual(result['result']['completion']['status'], 'completed' if final else 'queue_completed')
        self.assertEqual(manager.requests, [])
        self.assertEqual(services.actions, [])
        self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')

    def test_completed_goal_resume_has_no_manager_service_or_runner_effect(self):
        self._completed_execute(True)

    def test_queue_completed_with_saved_final_false_does_not_launch(self):
        self._completed_execute(False)

    def test_manual_service_stop_during_readiness_cancels_runner_launch(self):
        from team_runner import service_intent
        services = Services()
        def status(project):
            service_intent.cancel(self.catalog, project, str(uuid.uuid4()))
            return {'state': 'running', 'confirmed': True}
        services.status = status
        with patch('team_runner.service.start_runner') as start:
            result = self.execute(services=services)
        start.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'authorization_cancelled')
