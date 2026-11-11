"""Service recovery blockers visible without RPC/process effects."""
import unittest
import uuid
from unittest.mock import patch
import test_platform as fixtures
from team_runner import application, service_intent
from team_runner.runner_store import atomic_json


class WatchdogSummary(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_current_watchdog_blocker_visible_read_only(self):
        intent = service_intent.authorize(self.catalog, self.p1, str(uuid.uuid4()))
        path = service_intent.directory(self.catalog, self.p1) / 'watchdog.json'
        atomic_json(path, {'epoch': intent['epoch'], 'phase': 'needs_action',
                          'blocker': 'service restart budget exhausted'})
        before = {p: p.read_bytes() for p in self.catalog.state_root.rglob('*.json')}
        with patch('team_runner.service_manager.Manager.ensure') as ensure, \
                patch('team_runner.project_services.ProjectServices.observe') as observe:
            summary = application.summary(self.a)
            ensure.assert_not_called(); observe.assert_not_called()
        self.assertEqual(summary['observed_state'], 'needs_action')
        self.assertEqual(summary['blocker']['code'], 'service_recovery_required')
        self.assertIn('budget exhausted', summary['blocker']['message'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.catalog.state_root.rglob('*.json')})
        service_intent.cancel(self.catalog, self.p1, str(uuid.uuid4()))
        self.assertNotEqual((application.summary(self.a)['blocker'] or {}).get('code'), 'service_recovery_required')
