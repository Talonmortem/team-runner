"""Service permission ordering; no real service processes or goals."""
import unittest
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_platform as fixtures
from team_runner import service_intent, goal_intent, goal_execute, application
from team_runner.project_services import ProjectServices
from team_runner.protocol import ContractError
from team_runner.runner_store import atomic_json, read_json, lock
from test_goal_execute import Services, ManagerFixture


class ServiceIntent(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def rid(self):
        return str(uuid.uuid4())

    def options(self):
        return {'goal': application.goals(self.a)[0]}

    def test_reservation_is_not_watchdog_permission(self):
        rid = self.rid()
        reservation = service_intent.reserve(self.catalog, self.p1, rid, {'fixture': True})
        self.assertEqual(reservation['status'], 'reserved')
        self.assertFalse(service_intent.permitted(self.catalog, self.p1, reservation['epoch']))
        self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')

    def test_cancel_invalidates_reserved_execute_even_after_later_start(self):
        rid = self.rid(); reserved = service_intent.reserve(self.catalog, self.p1, rid, {})
        service_intent.cancel(self.catalog, self.p1, self.rid())
        service_intent.authorize(self.catalog, self.p1, self.rid())
        with self.assertRaisesRegex(ContractError, 'changed after reservation'):
            service_intent.authorize(self.catalog, self.p1, rid, expected_epoch=reserved['epoch'], goal={'fixture': True})

    def test_concurrent_reservations_join_one_running_transition(self):
        first, second = self.rid(), self.rid()
        one = service_intent.reserve(self.catalog, self.p1, first, {})
        two = service_intent.reserve(self.catalog, self.p1, second, {})
        a = service_intent.authorize(self.catalog, self.p1, first, expected_epoch=one['epoch'], goal={'fixture': True})
        b = service_intent.authorize(self.catalog, self.p1, second, expected_epoch=two['epoch'], goal={'fixture': True})
        self.assertEqual(a['epoch'], b['epoch'])
        self.assertEqual(a['epoch'], 1)

    def test_same_start_id_cannot_undo_stop_but_new_start_can(self):
        rid = self.rid(); first = service_intent.authorize(self.catalog, self.p1, rid)
        cancelled = service_intent.cancel(self.catalog, self.p1, self.rid())
        self.assertEqual(service_intent.authorize(self.catalog, self.p1, rid), first)
        self.assertEqual(service_intent.read(self.catalog, self.p1)['epoch'], cancelled['epoch'])
        self.assertFalse(service_intent.permitted(self.catalog, self.p1, first['epoch']))
        renewed = service_intent.authorize(self.catalog, self.p1, self.rid())
        self.assertTrue(service_intent.permitted(self.catalog, self.p1, renewed['epoch']))

    def test_stop_suppression_precedes_operations_lock(self):
        services = ProjectServices(self.catalog); rid = self.rid()
        directory = services.directory(self.p1)
        service_intent.authorize(self.catalog, self.p1, self.rid())
        with patch.object(services, '_stop', return_value={'confirmed': True, 'state': 'stopped'}), ThreadPoolExecutor(max_workers=1) as pool:
            with lock(directory / 'operations.lock'):
                future = pool.submit(services.action, self.p1, 'stop', rid)
                deadline = time.monotonic() + 2
                while service_intent.read(self.catalog, self.p1)['desired_state'] != 'stopped' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=2)['status'], 'done')

    def test_watchdog_epoch_cannot_reauthorize_after_stop(self):
        services = ProjectServices(self.catalog)
        auth = service_intent.authorize(self.catalog, self.p1, self.rid())
        service_intent.cancel(self.catalog, self.p1, self.rid())
        with patch.object(services, 'observe', return_value={'confirmed': True, 'state': 'stopped'}), patch.object(services, 'spawn') as spawn:
            result = services.action(self.p1, 'start', self.rid(), expected_service_epoch=auth['epoch'])
            spawn.assert_not_called()
        self.assertEqual(result['error_code'], 'authorization_cancelled')
        self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')

    def test_delayed_execute_reservation_does_not_undo_stop(self):
        original = goal_intent.authorize
        def authorize(*args, **kwargs):
            service_intent.cancel(self.catalog, self.p1, self.rid())
            return original(*args, **kwargs)
        services = Services()
        with patch('team_runner.goal_intent.authorize', side_effect=authorize), patch('team_runner.service.start_runner') as start:
            result = goal_execute.execute(self.a, self.options(), self.rid(), services=services, manager=ManagerFixture())
            start.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'authorization_cancelled')
        self.assertEqual(services.actions, [])
        self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')

    def test_crash_after_reservation_keeps_original_epoch(self):
        rid = self.rid(); services = Services()
        class Crash(BaseException):
            pass
        with patch('team_runner.goal_intent.authorize', side_effect=Crash), self.assertRaises(Crash):
            goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
        service_intent.cancel(self.catalog, self.p1, self.rid())
        result = goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(services.actions, [])

    def test_new_execute_restores_service_without_repeating_accepted_launch(self):
        services = Services(); rid = self.rid()
        with patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            first = goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
            before = goal_execute._path(self.a, rid).read_bytes()
            service_intent.cancel(self.catalog, self.p1, self.rid())
            self.assertEqual(goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture()), first)
            self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')
            second = goal_execute.execute(self.a, self.options(), self.rid(), services=services, manager=ManagerFixture())
            self.assertEqual(second['status'], 'succeeded')
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(services.actions), 2)
        self.assertNotEqual(services.actions[0][2], services.actions[1][2])
        self.assertEqual(goal_execute._path(self.a, rid).read_bytes(), before)

    def test_unknown_launch_is_not_replayed_after_new_service_authorization(self):
        services = Services()
        with patch('team_runner.service.start_runner', side_effect=TimeoutError) as start:
            first = goal_execute.execute(self.a, self.options(), self.rid(), services=services, manager=ManagerFixture())
            service_intent.cancel(self.catalog, self.p1, self.rid())
            second = goal_execute.execute(self.a, self.options(), self.rid(), services=services, manager=ManagerFixture())
            self.assertEqual(first['status'], 'unknown')
            self.assertEqual(second['status'], 'unknown')
            self.assertEqual(start.call_count, 1)

    def test_old_nonterminal_execute_without_service_scope_fails_closed(self):
        services = Services(); rid = self.rid()
        services.observation = {'confirmed': False, 'state': 'unknown'}
        goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
        path = goal_execute._path(self.a, rid); record = read_json(path)
        record.pop('service_reservation'); record['status'] = 'running'; atomic_json(path, record)
        with self.assertRaisesRegex(ContractError, 'predates service authorization'):
            goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
        self.assertEqual(len(services.actions), 1)

    def test_foreign_roots_and_unsupported_version_fail_closed(self):
        service_intent.authorize(self.catalog, self.p1, self.rid())
        path = service_intent.directory(self.catalog, self.p1) / 'intent.json'
        data = read_json(path); data['identity']['state_root'] = '/foreign'; atomic_json(path, data)
        with self.assertRaisesRegex(ContractError, 'another project or roots'):
            service_intent.read(self.catalog, self.p1)
        data['schema_version'] = 2; atomic_json(path, data)
        with self.assertRaisesRegex(ContractError, 'Unsupported schema'):
            service_intent.read(self.catalog, self.p1)

    def test_stop_does_not_reclassify_unknown_launch_as_no_effect(self):
        services = Services(); rid = self.rid()
        with patch('team_runner.service.start_runner', side_effect=TimeoutError) as start:
            first = goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
            service_intent.cancel(self.catalog, self.p1, self.rid())
            goal_intent.change(self.a, desired_state='stopped')
            second = goal_execute.execute(self.a, self.options(), rid, services=services, manager=ManagerFixture())
            self.assertEqual(first['status'], 'unknown')
            self.assertEqual(second['status'], 'unknown')
            self.assertEqual(start.call_count, 1)
        self.assertEqual(service_intent.read(self.catalog, self.p1)['desired_state'], 'stopped')
