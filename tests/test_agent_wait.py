"""Read-only waits use persisted receipts and fake monotonic time."""
import math
import unittest
import uuid
from unittest.mock import patch
import test_platform as fixtures
from team_runner import agent_wait
from team_runner.protocol import ContractError
from team_runner.runner_store import atomic_json, read_json


class Clock:
    def __init__(self, callback=lambda: None):
        self.now = 0
        self.sleeps = []
        self.callback = callback

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.callback()


class AgentWait(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def receipt(self, status='pending', generation=None, action='execute'):
        identifier = str(uuid.uuid4())
        command = {'schema_version': 1, 'request_id': identifier, 'action': action,
                   'scope': {'identity': self.a.context.identity(), 'generation': None, 'attempt': None}, 'payload': {}}
        record = {'schema_version': 1, 'id': identifier, 'action': action, 'command': command,
                  'status': status, 'result': None, 'error': None}
        if generation:
            record['authorization'] = {'generation': generation, 'epoch': 1}
        path = self.a.state_dir / 'ui' / 'commands' / (identifier + '.json')
        atomic_json(path, record)
        return identifier, path

    def summary(self, generation, state='running', confirmed=True, blocker=None):
        return {'schema_version': 1, 'identity': self.a.context.identity(), 'generation': generation,
                'observed_state': state, 'confirmed': confirmed, 'blocker': blocker}

    def wait(self, identifier, **kwargs):
        clock = kwargs.pop('fixture_clock', Clock())
        return agent_wait.wait(self.a, identifier, clock=clock.clock, sleep=clock.sleep, **kwargs)

    def test_operation_success_is_not_goal_completion(self):
        identifier, path = self.receipt('succeeded')
        before = path.read_bytes()
        with patch('team_runner.protocol.summary', side_effect=AssertionError('unneeded goal read')):
            result = self.wait(identifier)
        self.assertEqual(result['wait_status'], 'succeeded')
        self.assertIsNone(result['summary'])
        self.assertEqual(path.read_bytes(), before)

    def test_zero_timeout_returns_latest_pending_receipt_without_sleep(self):
        identifier, path = self.receipt()
        clock = Clock()
        result = self.wait(identifier, timeout=0, fixture_clock=clock)
        self.assertEqual(result['wait_status'], 'timeout')
        self.assertEqual(result['operation']['status'], 'pending')
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(read_json(path)['status'], 'pending')

    def test_pending_receipt_can_complete_between_observations(self):
        identifier, path = self.receipt()
        def publish():
            record = read_json(path); record['status'] = 'succeeded'; atomic_json(path, record)
        clock = Clock(publish)
        result = self.wait(identifier, timeout=3, fixture_clock=clock)
        self.assertEqual(result['wait_status'], 'succeeded')
        self.assertEqual(clock.sleeps, [.2])

    def test_timeout_is_finite_and_does_not_write_or_lock(self):
        identifier, path = self.receipt()
        clock = Clock()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        with patch('team_runner.runner_store.atomic_json', side_effect=AssertionError('write')), \
                patch('team_runner.runner_store.lock', side_effect=AssertionError('lock')), \
                patch.object(self.a, 'orca', side_effect=AssertionError('RPC')):
            result = self.wait(identifier, timeout=.45, fixture_clock=clock)
        self.assertEqual(result['wait_status'], 'timeout')
        self.assertAlmostEqual(sum(clock.sleeps), .45)
        self.assertTrue(all(0 < delay <= .2 for delay in clock.sleeps))
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_invalid_timeout_and_target_rejected_before_observation(self):
        identifier = str(uuid.uuid4())
        with patch('team_runner.protocol.operation', side_effect=AssertionError('observation')):
            for timeout in (-1, 3601, 10**1000, math.inf, -math.inf, math.nan, True, '3', None):
                with self.subTest(timeout=timeout), self.assertRaises(ContractError):
                    self.wait(identifier, timeout=timeout)
            with self.assertRaises(ContractError):
                self.wait(identifier, until='attempt')

    def test_failed_and_unknown_operation_are_blocked(self):
        for status in ('failed', 'unknown'):
            with self.subTest(status=status):
                identifier, _ = self.receipt(status)
                result = self.wait(identifier)
                self.assertEqual(result['wait_status'], 'blocked')
                self.assertEqual(result['blocker']['code'], 'operation_' + status)

    def test_completed_goal_uses_unknown_receipts_authorized_generation(self):
        generation = str(uuid.uuid4()); identifier, path = self.receipt('unknown', generation)
        before = path.read_bytes()
        for state in ('completed', 'queue_completed'):
            with self.subTest(state=state), patch('team_runner.protocol.summary', return_value=self.summary(generation, state)):
                result = self.wait(identifier, until='goal')
            self.assertEqual(result['wait_status'], 'succeeded')
            self.assertEqual(result['generation'], generation)
            self.assertEqual(result['operation']['status'], 'unknown')
            self.assertEqual(path.read_bytes(), before)

    def test_another_generation_cannot_complete_old_wait(self):
        generation = str(uuid.uuid4()); identifier, _ = self.receipt('succeeded', generation)
        with patch('team_runner.protocol.summary', return_value=self.summary(str(uuid.uuid4()), 'completed')):
            result = self.wait(identifier, until='goal')
        self.assertEqual(result['wait_status'], 'blocked')
        self.assertEqual(result['blocker']['code'], 'generation_changed')

    def test_torn_completion_observation_never_succeeds(self):
        generation = str(uuid.uuid4()); identifier, _ = self.receipt('succeeded', generation)
        with patch('team_runner.protocol.summary', return_value=self.summary(generation, 'completed', confirmed=False)):
            result = self.wait(identifier, until='goal', timeout=0)
        self.assertEqual(result['wait_status'], 'timeout')

    def test_pre_authorization_pending_is_not_blocked_by_legacy_pause(self):
        identifier, _ = self.receipt()
        with patch('team_runner.protocol.summary', return_value=self.summary(None, 'paused')):
            result = self.wait(identifier, until='goal', timeout=0)
        self.assertEqual(result['wait_status'], 'timeout')

    def test_pending_execute_is_not_blocked_by_runner_not_running(self):
        generation = str(uuid.uuid4()); identifier, _ = self.receipt('running', generation)
        summary = self.summary(generation, 'needs_action', blocker={'code': 'runner_not_running'})
        with patch('team_runner.protocol.summary', return_value=summary):
            result = self.wait(identifier, until='goal', timeout=.25)
        self.assertEqual(result['wait_status'], 'timeout')

    def test_accepted_startup_is_not_prematurely_blocked(self):
        generation = str(uuid.uuid4()); identifier, _ = self.receipt('succeeded', generation)
        nonce = str(uuid.uuid4())
        atomic_json(self.a.state_dir / 'startup.json', {'status': 'starting', 'nonce': nonce})
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                                                     'authorization': {'generation': generation}})
        summary = self.summary(generation, 'needs_action', blocker={'code': 'runner_not_running'})
        with patch('team_runner.protocol.summary', return_value=summary):
            result = self.wait(identifier, until='goal', timeout=0)
        self.assertEqual(result['wait_status'], 'timeout')

    def test_goal_decisions_and_cancellation_are_blocked(self):
        generation = str(uuid.uuid4()); identifier, _ = self.receipt('succeeded', generation)
        for state in ('paused', 'stopped', 'failed', 'unknown', 'needs_action'):
            with self.subTest(state=state), patch('team_runner.protocol.summary', return_value=self.summary(generation, state)):
                result = self.wait(identifier, until='goal')
            self.assertEqual(result['wait_status'], 'blocked')
            self.assertEqual(result['blocker']['code'], 'goal_' + state)

    def test_goal_wait_requires_execute(self):
        identifier, _ = self.receipt('succeeded', action='pause')
        with self.assertRaisesRegex(ContractError, 'requires an execute'):
            self.wait(identifier, until='goal')

    def test_missing_generation_never_binds_to_current_goal(self):
        identifier, _ = self.receipt('succeeded')
        with patch('team_runner.protocol.summary', return_value=self.summary(str(uuid.uuid4()), 'completed')):
            result = self.wait(identifier, until='goal')
        self.assertEqual(result['wait_status'], 'blocked')
        self.assertEqual(result['blocker']['code'], 'goal_generation_unavailable')

    def test_interrupted_client_changes_no_receipt(self):
        identifier, path = self.receipt()
        before = path.read_bytes()
        with self.assertRaises(KeyboardInterrupt):
            agent_wait.wait(self.a, identifier, sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(path.read_bytes(), before)

    def test_real_completion_projection_succeeds_without_mutating_unknown_receipt(self):
        import test_goal_completion
        from team_runner import goal_completion, goal_intent
        test_goal_completion.GoalCompletion.prepare(self, final=True)
        goal_completion.record(self.a, self.state)
        generation = goal_intent.read(self.a)['generation']
        identifier, path = self.receipt('unknown', generation)
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        with patch.object(self.a, 'orca', side_effect=AssertionError('RPC')):
            result = self.wait(identifier, until='goal', timeout=0)
        self.assertEqual(result['wait_status'], 'succeeded')
        self.assertEqual(result['summary']['final_acceptance']['status'], 'accepted')
        self.assertEqual(result['operation']['status'], 'unknown')
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
