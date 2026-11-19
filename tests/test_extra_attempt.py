"""Manual authorization permits one extra attempt, never automatic retries."""
import uuid
from unittest.mock import patch
from test_goal_runner import Fixture, runner, launch, store
from team_runner import recovery


class ExtraAttempt(Fixture):
    def stopped(self):
        attempt, st = self.attempt()
        st['current']['attempt'] = 3
        st.update(stop_status='stopped', stop_requested=True, paused=True)
        runner.save_state(st)
        return attempt, st

    def test_default_and_non_boolean_do_not_override_limit(self):
        for data in ({}, {'allow_extra_attempt': 'true'}, {'force': True}):
            attempt, st = self.stopped()
            with self.assertRaisesRegex(ValueError, 'three failed attempts'):
                runner.apply_control(st, 'retry', data)
            self.assertEqual(st['current']['attempt'], 3)

    def test_authorization_preserves_snapshot_and_history_without_dispatch(self):
        attempt, st = self.stopped()
        snapshot = (attempt / 'config.json').read_bytes()
        command = str(uuid.uuid4())
        with patch.object(launch, 'orca') as orca:
            result = runner.submit_control('retry', {'allow_extra_attempt': True}, command)
            duplicate = runner.submit_control('retry', {'allow_extra_attempt': True}, command)
            orca.assert_not_called()
        self.assertEqual(result['status'], 'done')
        self.assertEqual(duplicate, result)
        st = runner.load_state()
        self.assertEqual(st['current']['attempt'], 4)
        self.assertEqual(st['current']['manual_retry']['command_id'], command)
        self.assertEqual(st['attempts'][-1]['attempt'], 3)
        self.assertEqual(st['current']['retry_config'], store.read_json(attempt / 'config.json'))
        self.assertEqual((attempt / 'config.json').read_bytes(), snapshot)
        self.assertNotIn('dispatch_id', st['current'])
        self.assertEqual(recovery.settings(runner)['intent'], 'paused')
        self.assertIsNone(recovery.recovery_view(runner)['reason'])
        # The next stopped attempt again requires explicit authorization.
        st['current']['attempt_dir'] = str(attempt)
        st['stop_status'] = 'stopped'
        runner.save_state(st)
        with self.assertRaisesRegex(ValueError, 'three failed attempts'):
            runner.apply_control(st, 'retry', {})

    def test_unknown_effect_or_unstopped_executor_blocks_override(self):
        for kind in ('operation', 'agent', 'run', 'stop'):
            attempt, st = self.stopped()
            store.atomic_json(launch.journal(attempt), {})
            launch.save_registry(attempt, {'agents': [], 'runs': []})
            if kind == 'operation':
                store.atomic_json(launch.journal(attempt), {'dispatch': {'status': 'unknown'}})
            elif kind == 'agent':
                launch.save_registry(attempt, {'agents': [{'status': 'active'}], 'runs': []})
            elif kind == 'run':
                launch.save_registry(attempt, {'agents': [], 'runs': [{'status': 'ready'}]})
            else:
                st['stop_status'] = 'unknown'
            for option in ('allow_extra_attempt', 'reset_attempts'):
                with self.subTest(kind=kind, option=option), self.assertRaises(ValueError):
                    runner.apply_control(st, 'retry', {option: True})
                self.assertEqual(st['current']['attempt'], 3)

    def test_reset_preserves_history_and_continuation(self):
        from types import SimpleNamespace

        attempt, state = self.stopped()
        state['mailbox'] = 'main'
        command = str(uuid.uuid4())
        runner.apply_control(state, 'retry', {'reset_attempts': True}, command)
        current = state['current']
        self.assertEqual(current['attempt'], 1)
        self.assertEqual(current['attempt_reset'], {'previous_attempt': 3, 'command_id': command, 'reason': 'unspecified'})
        self.assertEqual(current['retry_context_from'], str(attempt))
        self.assertEqual(state['attempts'][-1]['attempt'], 3)
        self.assertEqual(current['retry_config'], store.read_json(attempt / 'config.json'))

        args = SimpleNamespace(mode='auto', config=None)
        with patch.object(runner, 'effect', return_value={'task': {'id': 'fresh-task'}}) as create, \
                patch.object(launch, 'launch', return_value={'dispatch_id': 'fresh-dispatch'}), \
                patch.object(runner, 'process_controls'), patch.object(runner, 'notify'):
            runner.start_coordinator(args, state, 'X', 'spec', 'title')
        self.assertIn('продолжение прошлой попытки', create.call_args.args[create.call_args.args.index('--spec') + 1])

    def test_reset_records_explicit_reason_and_validates_before_changing_intent(self):
        attempt, _ = self.stopped()
        recovery.set_intent(runner, 'stopped')
        with self.assertRaisesRegex(ValueError, 'reset_reason requires reset_attempts'):
            runner.submit_control('retry', {'reset_reason': 'task'})
        self.assertEqual(recovery.settings(runner)['intent'], 'stopped')
        self.assertEqual(runner.load_state()['current']['attempt_dir'], str(attempt))
        result = runner.submit_control('retry', {'reset_attempts': True, 'reset_reason': 'infrastructure'})
        self.assertEqual(result['status'], 'done')
        self.assertEqual(runner.load_state()['current']['attempt_reset']['reason'], 'infrastructure')
