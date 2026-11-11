"""Sequential dispatch uses settled outcomes, not terminal liveness."""
import unittest
from unittest.mock import patch

import test_platform as fixtures
from team_runner.catalog import profile
from team_runner.runner_store import atomic_json, read_json


class SequentialAgents(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt

    def prepare(self):
        attempt = self.attempt(self.a)
        snapshot = read_json(attempt / 'context.json')
        snapshot['profile']['workflow']['sequential_agents'] = True
        atomic_json(attempt / 'context.json', snapshot)
        return attempt

    def test_blocks_second_role_before_task_creation_then_allows_settled_worker(self):
        attempt = self.prepare()
        launcher = self.a.launcher
        first = launcher.dispatch_role(attempt, 'tester', 'first', 'first', self.a.state_dir)
        with patch.object(launcher, 'effect', wraps=launcher.effect) as effect:
            with self.assertRaisesRegex(RuntimeError, 'sequential execution'):
                launcher.dispatch_role(attempt, 'final_reviewer', 'second', 'second', self.a.state_dir)
            effect.assert_not_called()
        path = launcher.orca.root / 'transport.json'
        transport = read_json(path)
        transport['workers'][first['dispatch_id']]['done'] = True
        atomic_json(path, transport)
        second = launcher.dispatch_role(attempt, 'final_reviewer', 'second', 'second', self.a.state_dir)
        self.assertNotEqual(first['dispatch_id'], second['dispatch_id'])

    def test_snapshot_policy_coordinator_and_unknown_outcome(self):
        attempt = self.prepare()
        launcher = self.a.launcher
        reg = {'agents': [{'role': 'coordinator', 'status': 'active'},
                          {'role': 'tester', 'status': 'active', 'dispatch_id': 'child'}]}
        launcher.sequential_guard(attempt, reg, 'coordinator')
        with patch.object(launcher, 'observe_agent', return_value={}) as observed:
            with self.assertRaisesRegex(RuntimeError, 'sequential execution'):
                launcher.sequential_guard(attempt, reg, 'final_reviewer')
            self.assertEqual(observed.call_count, 1)
        reg['agents'][1]['status'] = 'stopped'
        launcher.sequential_guard(attempt, reg, 'tester')

    def test_direct_launch_recovery_cannot_bypass_guard(self):
        attempt = self.prepare()
        launcher = self.a.launcher
        reg = {'agents': [{'role': 'tester', 'status': 'ready'}]}
        with patch.object(launcher, 'create_terminal') as create:
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                launcher._launch(attempt, reg, {'role': 'final_reviewer', 'task_id': 'new'}, 'new', self.a.state_dir)
            create.assert_not_called()

    def test_profile_boolean_validation_and_legacy(self):
        self.assertTrue(profile({'workflow': {'sequential_agents': True}})['workflow']['sequential_agents'])
        with self.assertRaises(ValueError):
            profile({'workflow': {'sequential_agents': 1}})
        attempt = self.attempt(self.a)
        self.a.launcher.sequential_guard(attempt, {'agents': [{'role': 'tester'}]}, 'tester')
