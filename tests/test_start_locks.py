"""Catalog lock is brief; checkout and durable startup guard transport waits."""
import unittest
import uuid
from unittest.mock import patch
import test_platform as fixtures
from team_runner import application, goal_intent
from team_runner.catalog import Context
from team_runner.runner_store import lock, read_json
from team_runner.service import start_runner


class StartLocks(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def authorize(self):
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        return goal_intent.read(self.a)['options']

    def assert_catalog_free_and_address_guarded(self):
        with lock(self.catalog.config_root / 'catalog.lock', blocking=False):
            pass
        self.catalog.project('Other project remains editable', project_id=self.p2)
        with self.assertRaisesRegex(ValueError, 'active or unknown'):
            self.catalog.workspace(self.p1, {'orca_id': 'changed'}, self.a.context.workspace_id)
        with self.assertRaises(RuntimeError):
            self.catalog.disable(self.p1, self.a.context.workspace_id)

    def test_catalog_free_during_verification_and_terminal_creation(self):
        options = self.authorize()
        def verify(*args):
            self.assert_catalog_free_and_address_guarded()
        def create(*args, **kwargs):
            self.assert_catalog_free_and_address_guarded()
            return {'handle': 'fixture'}
        with patch.object(Context, 'verify', side_effect=verify), \
                patch.object(self.a.launcher, 'create_terminal', side_effect=create) as effect:
            self.assertEqual(start_runner(self.a, options)['status'], 'starting')
            effect.assert_called_once()

    def test_address_change_before_reservation_is_rejected(self):
        options = self.authorize()
        original = application.start_options
        def validate(*args, **kwargs):
            value = original(*args, **kwargs)
            self.catalog.workspace(self.p1, {'orca_id': 'changed'}, self.a.context.workspace_id)
            return value
        with patch.object(application, 'start_options', side_effect=validate), \
                patch.object(self.a.launcher, 'create_terminal') as effect:
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                start_runner(self.a, options)
            effect.assert_not_called()
        self.assertFalse((self.a.state_dir / 'startup.json').exists())

    def test_pause_during_verification_prevents_creation(self):
        options = self.authorize()
        with patch.object(Context, 'verify', side_effect=lambda *_: goal_intent.change(self.a, desired_state='paused')), \
                patch.object(self.a.launcher, 'create_terminal') as effect:
            with self.assertRaisesRegex(ValueError, 'authorization changed'):
                start_runner(self.a, options)
            effect.assert_not_called()
        self.assertEqual(read_json(self.a.state_dir / 'startup.json')['status'], 'cancelled')

    def test_pre_effect_verification_failure_is_cancelled(self):
        options = self.authorize()
        with patch.object(Context, 'verify', side_effect=ValueError('fixture identity unavailable')), \
                patch.object(self.a.launcher, 'create_terminal') as effect:
            with self.assertRaisesRegex(ValueError, 'identity unavailable'):
                start_runner(self.a, options)
            effect.assert_not_called()
        self.assertFalse((self.a.state_dir / 'startup.json').exists())
        receipts = list((self.a.state_dir / 'start-reservations').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(read_json(receipts[0])['status'], 'cancelled')

    def test_crash_after_reservation_cannot_replay_start(self):
        options = self.authorize()
        class Crash(BaseException):
            pass
        with patch.object(Context, 'verify', side_effect=Crash) as observe, \
                patch.object(self.a.launcher, 'create_terminal') as effect:
            with self.assertRaises(Crash):
                start_runner(self.a, options)
            with self.assertRaisesRegex(ValueError, 'unconfirmed'):
                start_runner(self.a, options)
            self.assertEqual(observe.call_count, 1)
            effect.assert_not_called()
        self.assertEqual(read_json(self.a.state_dir / 'startup.json')['status'], 'starting')

    def test_manual_service_stop_during_verification_prevents_runner_launch(self):
        from team_runner import service_intent
        options = self.authorize()
        permission = service_intent.authorize(self.catalog, self.p1, str(uuid.uuid4()))
        def verify(*args):
            service_intent.cancel(self.catalog, self.p1, str(uuid.uuid4()))
        with patch.object(Context, 'verify', side_effect=verify), \
                patch.object(self.a.launcher, 'create_terminal') as effect:
            with self.assertRaisesRegex(ValueError, 'service authorization changed'):
                start_runner(self.a, options, expected_service_epoch=permission['epoch'])
            effect.assert_not_called()
        self.assertEqual(read_json(self.a.state_dir / 'startup.json')['status'], 'cancelled')

    def test_old_service_epoch_is_rejected_before_reservation(self):
        from team_runner import service_intent
        options = self.authorize()
        permission = service_intent.authorize(self.catalog, self.p1, str(uuid.uuid4()))
        service_intent.cancel(self.catalog, self.p1, str(uuid.uuid4()))
        with patch.object(Context, 'verify') as observe, patch.object(self.a.launcher, 'create_terminal') as effect:
            with self.assertRaisesRegex(ValueError, 'service authorization changed'):
                start_runner(self.a, options, expected_service_epoch=permission['epoch'])
            observe.assert_not_called(); effect.assert_not_called()
        self.assertFalse((self.a.state_dir / 'startup.json').exists())
