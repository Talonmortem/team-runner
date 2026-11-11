"""Intent cancellation and unknown startup boundaries using isolated fixtures."""
import copy
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_intent, protocol
from team_runner.runner_store import atomic_json, lock
from team_runner.service import start_runner


class GoalIntentBoundaries(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def authorize(self):
        return goal_intent.authorize(
            self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))

    def test_queued_pause_cannot_cancel_resumed_epoch_of_same_generation(self):
        first = self.authorize()
        with lock(self.a.state_dir / 'runner.lock'):
            view = protocol.summary(self.a)
            command = {
                'schema_version': 1, 'request_id': str(uuid.uuid4()), 'action': 'pause',
                'scope': {'identity': view['identity'], 'generation': view['generation'],
                          'attempt': view['current']['attempt'], 'revision': view['revision']},
                'payload': {},
            }
            self.assertEqual(application.submit_v1(self.a, command)['status'], 'pending')
            paused = goal_intent.change(self.a, desired_state='paused')
            resumed = goal_intent.authorize(
                self.a, {'resume': True}, str(uuid.uuid4()),
                generation=paused['generation'], epoch=paused['epoch'])
            self.assertEqual(resumed['generation'], first['generation'])
            self.assertGreater(resumed['epoch'], first['epoch'])
            before = (self.a.state_dir / 'goal-intent.json').read_bytes()
            with patch.object(self.a, 'apply_control', wraps=self.a.apply_control) as apply:
                self.a.process_controls(self.a.load_state())
                apply.assert_not_called()
        receipt = protocol.operation(self.a, command['request_id'])
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['error']['code'], 'stale_scope')
        self.assertEqual((self.a.state_dir / 'goal-intent.json').read_bytes(), before)
        self.assertTrue(goal_intent.permitted(self.a, resumed['generation'], resumed['epoch']))
        self.assertFalse(self.a.load_state().get('paused'))

    def test_reauthorization_does_not_replay_interrupted_or_unknown_start(self):
        self.authorize()
        for status in ('starting', 'unknown'):
            with self.subTest(status=status):
                path = self.a.state_dir / 'startup.json'
                atomic_json(path, {'status': status, 'nonce': str(uuid.uuid4())})
                before = path.read_bytes()
                paused = goal_intent.change(self.a, desired_state='paused')
                resumed = goal_intent.authorize(
                    self.a, {'resume': True}, str(uuid.uuid4()),
                    generation=paused['generation'], epoch=paused['epoch'])
                self.assertTrue(goal_intent.permitted(self.a, resumed['generation'], resumed['epoch']))
                with patch.object(self.a.launcher, 'create_terminal') as create, \
                        patch.object(self.a, 'orca') as transport:
                    with self.assertRaisesRegex(ValueError, 'start unconfirmed'):
                        start_runner(self.a, {'resume': True})
                    create.assert_not_called()
                    transport.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.a.state_dir / 'launch.json').exists())

    def test_foreign_or_unsupported_intent_rejects_mutations_without_snapshot_changes(self):
        self.authorize()
        attempt = fixtures.Platform.attempt(self, self.a)
        snapshots = {path.name: path.read_bytes() for path in attempt.iterdir() if path.is_file()}
        original = goal_intent.read(self.a)
        for change, code in (({'identity': self.b.context.identity()}, 'identity_mismatch'),
                             ({'schema_version': 2}, 'unsupported_version')):
            with self.subTest(change=change):
                invalid = copy.deepcopy(original)
                invalid.update(change)
                path = self.a.state_dir / 'goal-intent.json'
                atomic_json(path, invalid)
                before = path.read_bytes()
                state_before = self.a.load_state()
                for mutation in (
                    lambda: goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                                  generation=original['generation'], epoch=original['epoch']),
                    lambda: goal_intent.change(self.a, desired_state='stopped'),
                ):
                    with self.assertRaises(protocol.ContractError) as error:
                        mutation()
                    self.assertEqual(error.exception.code, code)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.a.load_state(), state_before)
                self.assertEqual({p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}, snapshots)
