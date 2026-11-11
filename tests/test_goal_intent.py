"""Authorization persistence and cancellation fences, without live processes."""
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_intent, protocol
from team_runner.runner_store import atomic_json


class GoalIntent(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def options(self):
        return {'goal': application.goals(self.a)[0]}

    def test_cold_defaults_persist_before_any_effect(self):
        with patch.object(self.a, 'orca') as effect:
            result = goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
            effect.assert_not_called()
        value = goal_intent.read(self.a)
        self.assertEqual(value['policy'], {'mode': 'auto', 'final': True, 'recovery': True, 'max_attempts': 3})
        self.assertEqual(value['generation'], result['generation'])
        self.assertEqual(value['desired_state'], 'running')
        self.assertFalse((self.a.state_dir / 'launch.json').exists())
        self.assertTrue(goal_intent.permitted(self.a, result['generation'], result['epoch']))

    def test_lost_response_repeat_does_not_undo_pause(self):
        identifier = str(uuid.uuid4()); options = self.options()
        first = goal_intent.authorize(self.a, options, identifier)
        goal_intent.change(self.a, desired_state='paused')
        self.assertEqual(goal_intent.authorize(self.a, options, identifier), first)
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'paused')
        self.assertFalse(goal_intent.permitted(self.a, first['generation'], first['epoch']))
        with self.assertRaises(protocol.ContractError):
            goal_intent.authorize(self.a, options, str(uuid.uuid4()))

    def test_resume_preserves_policy_and_requires_current_cancellation_epoch(self):
        first = goal_intent.authorize(self.a, {**self.options(), 'mode': 'confirm', 'final': False, 'recovery': False}, str(uuid.uuid4()))
        value = goal_intent.change(self.a, desired_state='stopped')
        with self.assertRaises(protocol.ContractError):
            goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()), generation=first['generation'], epoch=first['epoch'])
        resumed = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()), generation=value['generation'], epoch=value['epoch'])
        self.assertEqual(goal_intent.read(self.a)['policy'], {'mode': 'confirm', 'final': False, 'recovery': False, 'max_attempts': 3})
        self.assertGreater(resumed['epoch'], value['epoch'])

    def test_legacy_read_is_not_authorization_and_resume_keeps_options(self):
        options = {**self.options(), 'mode': 'confirm', 'final': False}
        self.a.save_state({'goal': options['goal'], 'options': options, 'history': []})
        atomic_json(self.a.state_dir / 'recovery-settings.json', {'enabled': False, 'intent': 'paused'})
        self.assertEqual(goal_intent.read(self.a), {})
        result = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()))
        self.assertEqual(goal_intent.read(self.a)['options'], options)
        self.assertFalse(goal_intent.read(self.a)['policy']['recovery'])
        self.assertTrue(result['generation'])

    def test_concurrent_cold_requests_share_generation(self):
        options = self.options()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: goal_intent.authorize(self.a, options, str(uuid.uuid4())), range(2)))
        self.assertEqual(results[0]['generation'], results[1]['generation'])
        self.assertEqual(len(goal_intent.read(self.a)['requests']), 2)

    def test_conflict_and_foreign_identity_are_rejected(self):
        identifier = str(uuid.uuid4()); options = self.options()
        goal_intent.authorize(self.a, options, identifier)
        with self.assertRaises(protocol.ContractError):
            goal_intent.authorize(self.a, {**options, 'final': False}, identifier)
        atomic_json(self.b.state_dir / 'goal-intent.json', goal_intent.read(self.a))
        with self.assertRaises(protocol.ContractError):
            goal_intent.read(self.b)

    def test_recovery_settings_follow_authoritative_intent_after_torn_mirror(self):
        from team_runner.recovery import settings, set_intent, configure
        goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
        set_intent(self.a, 'paused')
        atomic_json(self.a.state_dir / 'recovery-settings.json', {'enabled': False, 'intent': 'running'})
        self.assertEqual(settings(self.a), {'enabled': True, 'intent': 'paused'})
        configure(self.a, False)
        self.assertEqual(settings(self.a), {'enabled': False, 'intent': 'paused'})

    def test_cancel_before_terminal_effect_is_confirmed_not_unknown(self):
        from team_runner.service import start_runner
        from team_runner.catalog import Catalog
        from team_runner.runner_store import read_json
        goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
        options = goal_intent.read(self.a)['options']
        with patch.object(Catalog, 'claim', side_effect=lambda ctx: goal_intent.change(self.a, desired_state='stopped')), \
                patch.object(self.a.launcher, 'create_terminal') as create:
            with self.assertRaisesRegex(ValueError, 'authorization changed'):
                start_runner(self.a, options)
            create.assert_not_called()
        self.assertEqual(read_json(self.a.state_dir / 'startup.json')['status'], 'cancelled')
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'stopped')

    def test_late_launch_from_previous_epoch_cannot_claim_nonce(self):
        from team_runner.service import run_process
        first = goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
        nonce = str(uuid.uuid4())
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'options': goal_intent.read(self.a)['options'],
                    'authorization': {'generation': first['generation'], 'epoch': first['epoch']}})
        paused = goal_intent.change(self.a, desired_state='paused')
        goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()), generation=paused['generation'], epoch=paused['epoch'])
        with patch.object(self.a, 'orca') as effect:
            with self.assertRaisesRegex(ValueError, 'authorization expired'):
                run_process(self.a, nonce)
            effect.assert_not_called()
        self.assertFalse((self.a.state_dir / ('launch-' + nonce + '.claim')).exists())

    def test_cold_resume_uses_durable_options_before_state_exists(self):
        from team_runner.service import start_runner
        from team_runner.runner_store import read_json
        goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
        self.assertFalse(self.a.state_file.exists())
        with patch.object(self.a.launcher, 'create_terminal', return_value={'handle': 'fixture'}) as create:
            result = start_runner(self.a, {'resume': True})
            self.assertEqual(result['status'], 'starting')
            self.assertEqual(create.call_count, 1)
        self.assertEqual(read_json(self.a.state_dir / 'launch.json')['options'], goal_intent.read(self.a)['options'])

    def test_legacy_launch_cannot_bypass_new_intent_policy(self):
        from team_runner.service import run_process
        goal_intent.authorize(self.a, self.options(), str(uuid.uuid4()))
        nonce = str(uuid.uuid4())
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'options': {**self.options(), 'mode': 'confirm', 'final': False}})
        with patch.object(self.a, 'cmd_run') as run:
            with self.assertRaisesRegex(ValueError, 'lacks goal authorization'):
                run_process(self.a, nonce)
            run.assert_not_called()
        self.assertFalse((self.a.state_dir / ('launch-' + nonce + '.claim')).exists())
