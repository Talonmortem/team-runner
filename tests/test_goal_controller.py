"""Cold/between-item controller boundaries use fake clock and no processes."""
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_controller, goal_intent, recovery
from team_runner.runner_store import atomic_json, read_json


class GoalController(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def authorize(self):
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        return {k: goal_intent.read(self.a)[k] for k in ('generation', 'epoch')}

    def tick(self, now):
        with patch('team_runner.goal_completion.read', return_value={}):
            goal_controller.tick(self.a, now)

    def test_legacy_idle_does_not_start(self):
        with patch('team_runner.service.start_runner') as start:
            self.tick(100); self.tick(200)
        start.assert_not_called()
        self.assertFalse((self.a.state_dir / 'goal-controller.json').exists())

    def test_cold_authorized_gap_and_supervisor_entry(self):
        scope = self.authorize()
        supervisor = recovery.Supervisor(self.catalog, self.a.context.project_id, clock=lambda: 100)
        with patch('team_runner.goal_completion.read', return_value={}), \
                patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            supervisor.tick(self.a)
            start.assert_not_called()
            supervisor.clock = lambda: 103
            supervisor.tick(self.a)
        start.assert_called_once_with(self.a, {'resume': True}, recovery=True, expected_authorization=scope)

    def test_pause_during_backoff_survives_new_controller(self):
        self.authorize(); self.tick(100)
        goal_intent.change(self.a, desired_state='paused')
        with patch('team_runner.service.start_runner') as start:
            self.tick(500)
        start.assert_not_called()

    def test_unknown_launch_never_replayed_and_budget_persists(self):
        scope = self.authorize()
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': str(uuid.uuid4()),
                    'identity': self.a.context.identity(), 'authorization': scope})
        with patch('team_runner.service.start_runner') as start:
            for now in (100, 200, 300, 400, 500):
                self.tick(now)
        start.assert_not_called()
        journal = read_json(self.a.state_dir / 'goal-controller.json')
        self.assertEqual(journal['status'], 'needs_action')
        self.assertEqual(journal['checks'], 3)
        self.assertEqual(journal['reason'], 'runner_death_unconfirmed')

    def test_dead_claimed_runner_between_items_resumes_saved_queue(self):
        scope = self.authorize(); nonce = str(uuid.uuid4())
        identity = self.a.context.identity()
        self.a.save_state({'goal': goal_intent.read(self.a)['goal'], 'run_id': 'saved-run',
                           'history': [{'item': 'accepted', 'problems': []}], 'current': None})
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': identity, 'authorization': scope})
        atomic_json(self.a.state_dir / 'runner-process.json', {'nonce': nonce, 'identity': identity,
                                                            'pid': 123, 'process_stamp': 'fixture'})
        (self.a.state_dir / ('launch-' + nonce + '.claim')).touch()
        atomic_json(self.a.state_dir / 'startup.json', {'status': 'running', 'nonce': nonce})
        before = self.a.state_file.read_bytes()
        with patch('team_runner.project_services.departed', return_value=True), \
                patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            self.tick(100); self.tick(103)
        start.assert_called_once()
        self.assertEqual(self.a.state_file.read_bytes(), before)
        self.assertEqual(read_json(self.a.state_dir / 'startup.json')['status'], 'idle')

    def test_completed_policy_and_current_attempt_not_restarted(self):
        self.authorize()
        with patch('team_runner.goal_completion.read', return_value={'status': 'completed'}), \
                patch('team_runner.service.start_runner') as start:
            goal_controller.tick(self.a, 100)
        start.assert_not_called()
        self.a.save_state({'current': {'item': 'active'}, 'history': []})
        with patch('team_runner.service.start_runner') as start:
            self.tick(100); self.tick(200)
        start.assert_not_called()

    def test_cancel_at_start_boundary_creates_no_launch(self):
        self.authorize(); self.tick(100)
        from team_runner.service import start_runner
        def cancel_then_start(runner, data, **kwargs):
            goal_intent.change(runner, desired_state='stopped')
            return start_runner(runner, data, **kwargs)
        with patch('team_runner.service.start_runner', side_effect=cancel_then_start), \
                patch.object(self.a.launcher, 'create_terminal') as create:
            self.tick(103)
        create.assert_not_called()
        self.assertFalse((self.a.state_dir / 'launch.json').exists())

    def test_known_dead_process_does_not_override_unknown_effect(self):
        self.authorize()
        atomic_json(self.a.state_dir / 'operations.json', {'effect': {'status': 'unknown'}})
        with patch('team_runner.service.start_runner') as start:
            self.tick(100); self.tick(200)
        start.assert_not_called()
        self.assertEqual(read_json(self.a.state_dir / 'goal-controller.json')['reason'], 'unknown_effect')

    def test_summary_reports_bounded_recovery_blocker(self):
        scope = self.authorize()
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': str(uuid.uuid4()),
                    'identity': self.a.context.identity(), 'authorization': scope})
        self.tick(100)
        self.assertEqual(application.summary(self.a)['observed_state'], 'recovering')
        for now in (200, 300, 400):
            self.tick(now)
        summary = application.summary(self.a)
        self.assertEqual(summary['observed_state'], 'needs_action')
        self.assertEqual(summary['blocker']['code'], 'runner_death_unconfirmed')

    def test_authorized_resume_recovers_after_crash_before_launch(self):
        old_scope = self.authorize(); nonce = str(uuid.uuid4())
        identity = self.a.context.identity()
        self.a.save_state({'goal': goal_intent.read(self.a)['goal'], 'history': [],
                           'run_id': 'saved', 'paused': True, 'stop_requested': True, 'stop_status': 'stopped'})
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': identity, 'authorization': old_scope})
        atomic_json(self.a.state_dir / 'runner-process.json', {'nonce': nonce, 'identity': identity,
                                                            'pid': 123, 'process_stamp': 'fixture'})
        (self.a.state_dir / ('launch-' + nonce + '.claim')).touch()
        stopped = goal_intent.change(self.a, desired_state='stopped')
        auth = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                    generation=stopped['generation'], epoch=stopped['epoch'])
        with patch('team_runner.project_services.departed', return_value=True), \
                patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            self.tick(100); self.tick(103)
        start.assert_called_once_with(self.a, {'resume': True}, recovery=True,
                                     expected_authorization={k: auth[k] for k in ('generation', 'epoch')})

    def test_invalid_journal_stops_with_stable_blocker(self):
        scope = self.authorize()
        atomic_json(self.a.state_dir / 'goal-controller.json', {'schema_version': 2, 'scope': scope})
        with patch('team_runner.service.start_runner') as start:
            self.tick(100); self.tick(200)
        start.assert_not_called()
        self.assertEqual(application.summary(self.a)['blocker']['code'], 'invalid_controller_journal')

    def test_recovery_wait_does_not_hide_existing_errors(self):
        self.authorize(); self.tick(100)
        self.a.save_state({'history': [], 'last_error': 'fixture failure'})
        self.assertEqual(application.summary(self.a)['blocker']['code'], 'runner_error')
        self.a.save_state({'history': [], 'recovery_issues': ['unsettled resource']})
        self.assertEqual(application.summary(self.a)['blocker']['code'], 'recovery_required')

    def test_proved_progress_starts_new_budget_not_unknown_retries(self):
        scope = self.authorize()
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': str(uuid.uuid4()),
                    'identity': self.a.context.identity(), 'authorization': scope})
        for now in (100, 200, 300, 400):
            self.tick(now)
        self.assertEqual(read_json(self.a.state_dir / 'goal-controller.json')['checks'], 3)
        self.a.save_state({'history': [{'item': 'accepted', 'outcome': 'succeeded', 'problems': []}]})
        self.tick(500)
        self.assertEqual(read_json(self.a.state_dir / 'goal-controller.json')['checks'], 0)
        self.tick(600)
        self.assertEqual(read_json(self.a.state_dir / 'goal-controller.json')['checks'], 1)
