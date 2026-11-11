"""Malformed durable authorization fails closed without repairing history."""
import copy
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_intent, protocol
from team_runner.runner_store import atomic_json


class IntentSchema(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def authorize(self):
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        return goal_intent.read(self.a)

    def test_missing_is_legacy_but_empty_persisted_document_is_invalid(self):
        path = self.a.state_dir / 'goal-intent.json'
        self.assertEqual(goal_intent.read(self.a), {})
        self.assertFalse(path.exists())
        for value in ({}, [], None, False, 0, ''):
            with self.subTest(value=value):
                atomic_json(path, value)
                with self.assertRaises(protocol.ContractError):
                    goal_intent.read(self.a)
        path.write_text('{broken')
        with self.assertRaises(protocol.ContractError) as error:
            goal_intent.read(self.a)
        self.assertEqual(error.exception.code, 'invalid_intent')

    def test_malformed_fields_block_all_intent_operations_and_preserve_snapshots(self):
        original = self.authorize()
        attempt = fixtures.Platform.attempt(self, self.a)
        snapshots = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
        path = self.a.state_dir / 'goal-intent.json'
        cases = [('generation', 'wrong'), ('epoch', True), ('epoch', 0),
                 ('desired_state', 'unknown'), ('goal', '../other'), ('options', []),
                 ('options.goal', '/elsewhere'), ('options.final', 'yes'),
                 ('options.mode', []), ('options.config', {}), ('policy', {}),
                 ('policy.mode', 'confirm'), ('policy.final', 1), ('policy.recovery', 1),
                 ('policy.max_attempts', 4), ('initial_options', {}),
                 ('originating_request_id', 'bad'), ('requests', []), ('requests', {})]
        identifier = original['originating_request_id']
        cases += [(f'requests.{identifier}', []),
                  (f'requests.{identifier}.request', {}),
                  (f'requests.{identifier}.request.epoch', 3),
                  (f'requests.{identifier}.result', []),
                  (f'requests.{identifier}.result.epoch', True),
                  (f'requests.{identifier}.result.request_id', str(uuid.uuid4()))]
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                value = copy.deepcopy(original)
                target = value
                keys = field.split('.')
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = bad
                atomic_json(path, value)
                before = path.read_bytes()
                with patch.object(self.a, 'orca') as effects:
                    for action in (lambda: goal_intent.read(self.a),
                                   lambda: goal_intent.permitted(self.a, original['generation'], 1),
                                   lambda: goal_intent.change(self.a, desired_state='stopped'),
                                   lambda: goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()))):
                        with self.assertRaises(protocol.ContractError):
                            action()
                    effects.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual({p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}, snapshots)

    def test_historical_receipt_remains_valid_after_pause_and_resume(self):
        first = self.authorize()
        paused = goal_intent.change(self.a, desired_state='paused')
        goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                              generation=paused['generation'], epoch=paused['epoch'])
        result = goal_intent.read(self.a)
        self.assertEqual(result['epoch'], 3)
        self.assertEqual(result['requests'][first['originating_request_id']],
                         first['requests'][first['originating_request_id']])
