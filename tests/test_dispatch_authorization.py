"""Cancellation remains effective after entering the runner process."""
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_intent


class DispatchAuthorization(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_cancelled_epoch_cannot_create_mailbox_or_run(self):
        receipt = goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        self.a.authorization = {k: receipt[k] for k in ('generation', 'epoch')}
        paused = goal_intent.change(self.a, desired_state='paused')
        goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                              generation=paused['generation'], epoch=paused['epoch'])
        with patch.object(self.a.launcher, 'create_terminal') as terminal, patch.object(self.a, 'effect') as effect:
            with self.assertRaisesRegex(ValueError, 'authorization changed'):
                self.a.ensure_run({'goal': receipt['goal'], 'history': []})
            terminal.assert_not_called(); effect.assert_not_called()

    def test_cancelled_epoch_cannot_launch_prepared_coordinator(self):
        from types import SimpleNamespace
        receipt = goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        self.a.authorization = {k: receipt[k] for k in ('generation', 'epoch')}
        state = {'history': [], 'run_id': 'fixture', 'current': {'item': 'x', 'attempt_dir': '/fixture', 'task_id': 't'}}
        goal_intent.change(self.a, desired_state='stopped')
        with patch.object(self.a, 'process_controls'), patch.object(self.a.launcher, 'launch') as launch:
            with self.assertRaisesRegex(ValueError, 'authorization changed'):
                self.a.start_coordinator(SimpleNamespace(mode='auto'), state, 'x', 'spec', 'title')
            launch.assert_not_called()

    def test_recovery_toggle_does_not_cancel_authorized_execution(self):
        from team_runner.recovery import configure
        receipt = goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        self.a.authorization = {k: receipt[k] for k in ('generation', 'epoch')}
        for enabled in (False, True):
            configure(self.a, enabled)
            with self.a.launch_permission():
                self.assertEqual(goal_intent.read(self.a)['epoch'], receipt['epoch'])
            self.assertEqual(goal_intent.read(self.a)['policy']['recovery'], enabled)
