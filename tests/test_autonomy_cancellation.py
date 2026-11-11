"""Cancellation and cold recovery boundaries; all terminal effects are fixtures."""
import copy
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_controller, goal_intent
from team_runner.runner_store import atomic_json, read_json


class AutonomyCancellation(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def authorize(self, items=True):
        options = {'goal': application.goals(self.a)[0], 'mode': 'confirm', 'final': False,
                   'recovery': True, 'fast': False, 'items': 'T1'}
        if not items:
            options.pop('items')
        receipt = goal_intent.authorize(self.a, options, str(uuid.uuid4()))
        return receipt, copy.deepcopy(goal_intent.read(self.a))

    def tick(self, now):
        with patch('team_runner.goal_completion.read', return_value={}):
            return goal_controller.tick(self.a, now)

    def test_cold_controller_real_start_preserves_policy_and_unknown_terminal_never_replays(self):
        _, before = self.authorize()
        self.assertFalse(self.a.state_file.exists())
        with patch.object(self.a.launcher, 'create_terminal', return_value={'handle': 'fixture-terminal'}) as create:
            for now in (100, 103, 200, 300, 400):
                self.tick(now)
            self.assertEqual(create.call_count, 1)
        launch = read_json(self.a.state_dir / 'launch.json')
        self.assertEqual(launch['options'], before['options'])
        self.assertEqual(launch['authorization'], {key: before[key] for key in ('generation', 'epoch')})
        self.assertEqual(goal_intent.read(self.a)['policy'], before['policy'])
        journal = read_json(self.a.state_dir / 'goal-controller.json')
        self.assertEqual(journal['status'], 'needs_action')
        self.assertEqual(journal['reason'], 'runner_death_unconfirmed')

    def test_pause_after_current_finishes_does_not_choose_or_dispatch_next(self):
        _, intent = self.authorize(items=False)
        args = SimpleNamespace(**{key: None for key in ('config', 'agent', 'profile', 'provider', 'model', 'effort', 'handoff')},
                               **intent['options'])
        args.items = None
        self.a.save_state({'history': [], 'run_id': 'fixture', 'current': {'item': 'T1'}})
        finishes = []
        def wait(state):
            goal_intent.change(self.a, desired_state='paused')
            self.a.apply_control(state, 'pause', {})
            return {'outcome': 'succeeded'}
        def finish(args, state, done, verify):
            finishes.append(state['current']['item'])
            state['history'].append({'item': state['current']['item'], 'outcome': 'succeeded', 'problems': []})
            state['current'] = None
            self.a.save_state(state)
            return True
        with patch.object(self.a, 'ensure_run', return_value='fixture'), \
                patch.object(self.a, 'process_controls'), patch.object(self.a, 'coordinator_spec', return_value='fixture'), \
                patch.object(self.a, 'start_coordinator', return_value=None) as start, \
                patch.object(self.a, 'wait_coordinator', side_effect=wait), \
                patch.object(self.a, 'finish_item', side_effect=finish), patch.object(self.a, 'notify'):
            self.assertEqual(self.a.cmd_run(args), 0)
        self.assertEqual(finishes, ['T1'])
        self.assertEqual(start.call_count, 1)
        self.assertTrue(self.a.load_state()['paused'])
        self.assertIsNone(self.a.load_state()['current'])
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'paused')
        with patch('team_runner.service.start_runner') as restart:
            self.tick(1000)
        restart.assert_not_called()
        self.assertEqual(goal_intent.read(self.a)['options'], intent['options'])

    def test_stop_during_cold_backoff_remains_suppressed_after_repeated_observations(self):
        _, before = self.authorize()
        self.tick(100)
        stopped = goal_intent.change(self.a, desired_state='stopped')
        with patch.object(self.a.launcher, 'create_terminal') as create:
            for now in (103, 1000, 10000):
                self.tick(now)
        create.assert_not_called()
        self.assertFalse((self.a.state_dir / 'launch.json').exists())
        self.assertEqual(goal_intent.read(self.a)['epoch'], stopped['epoch'])
        self.assertEqual(goal_intent.read(self.a)['options'], before['options'])

    def test_stop_after_controller_preparation_before_real_start_has_no_effect(self):
        from team_runner.service import start_runner
        _, before = self.authorize()
        self.tick(100)
        def cancel(runner, data, **kwargs):
            prepared = read_json(runner.state_dir / 'goal-controller.json')
            self.assertEqual((prepared['status'], prepared['phase']), ('recovering', 'launch'))
            goal_intent.change(runner, desired_state='stopped')
            return start_runner(runner, data, **kwargs)
        with patch('team_runner.service.start_runner', side_effect=cancel), \
                patch.object(self.a.launcher, 'create_terminal') as create:
            self.tick(103)
            self.tick(1000)
        create.assert_not_called()
        self.assertFalse((self.a.state_dir / 'launch.json').exists())
        self.assertEqual(goal_intent.read(self.a)['options'], before['options'])

    def _active_recovery(self, cancel):
        from team_runner import recovery, service_intent
        _, before = self.authorize()
        service_intent.authorize(self.catalog, self.a.context.project_id, str(uuid.uuid4()))
        nonce = str(uuid.uuid4())
        attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        state = {'goal': before['goal'], 'run_id': 'fixture', 'history': [],
                 'options': before['options'], 'current': {'item': 'T1', 'attempt_dir': str(attempt),
                                                         'dispatch_id': 'fixture-dispatch'}}
        self.a.save_state(state)
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'authorization': {key: before[key] for key in ('generation', 'epoch')}})
        atomic_json(self.a.state_dir / 'runner-process.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'pid': 123, 'process_stamp': 'fixture', 'recovery_protocol': 1})
        supervisor = recovery.Supervisor(self.catalog, self.a.context.project_id, clock=lambda: 100)
        observed_permissions = []
        with patch('team_runner.project_services.departed', return_value=True), \
                patch.object(self.a, 'process_controls'), patch.object(self.a, 'recover', side_effect=lambda *a, **k: cancel()), \
                patch.object(self.a, 'cleanup_accepted_reports', side_effect=lambda st, permitted: observed_permissions.append(permitted())), \
                patch.object(self.a.launcher, 'resume_ready_launches') as ready, \
                patch('team_runner.service.start_runner') as start:
            supervisor.tick(self.a)
            supervisor.clock = lambda: 103
            supervisor.tick(self.a)
        self.assertEqual(observed_permissions, [False])
        ready.assert_not_called()
        start.assert_not_called()
        self.assertEqual(goal_intent.read(self.a)['options'], before['options'])
        self.assertEqual(self.a.load_state()['current'], state['current'])

    def test_active_recovery_captured_epoch_cannot_survive_pause_resume(self):
        def pause_resume():
            paused = goal_intent.change(self.a, desired_state='paused')
            goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                  generation=paused['generation'], epoch=paused['epoch'])
        self._active_recovery(pause_resume)
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'running')

    def test_active_recovery_captured_service_epoch_cannot_survive_stop_restart(self):
        from team_runner import service_intent
        def stop_start():
            service_intent.cancel(self.catalog, self.a.context.project_id, str(uuid.uuid4()))
            service_intent.authorize(self.catalog, self.a.context.project_id, str(uuid.uuid4()))
        self._active_recovery(stop_start)
        self.assertEqual(service_intent.read(self.catalog, self.a.context.project_id)['desired_state'], 'running')
