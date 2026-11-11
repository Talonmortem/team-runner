"""Read-only stable cursors, including retained duplicate attempt snapshots."""
import copy
import os
import unittest
from unittest.mock import patch
import uuid

import test_platform as fixtures
from team_runner import agent_messages, application, goal_intent
from team_runner.protocol import ContractError


class AgentMessages(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def prepare(self):
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        self.a.context.snapshot(attempt)
        state = {'run_id': 'fixture-run', 'history': [], 'current': {'attempt_dir': str(attempt), 'item': 'T1',
                 'messages': [{'run_id': 'child-run', 'id': str(i), 'body': 'message ' + str(i)} for i in range(4)]}}
        self.a.save_state(state)
        return state, attempt

    def test_pagination_determinism_and_no_writes_or_rpc(self):
        self.prepare()
        before = {str(p): p.read_bytes() for p in self.a.state_dir.rglob('*') if p.is_file()}
        with patch.object(self.a, 'orca', side_effect=AssertionError('RPC forbidden')), \
                patch.object(self.a.launcher, 'orca', side_effect=AssertionError('RPC forbidden')), \
                patch.object(self.a, 'save_state', side_effect=AssertionError('writes forbidden')):
            first = agent_messages.messages(self.a, limit=2)
            self.assertEqual(first, agent_messages.messages(self.a, limit=2))
            second = agent_messages.messages(self.a, first['next_cursor'], 2)
            final = agent_messages.messages(self.a, second['next_cursor'], 2)
        self.assertEqual([m['id'] for m in first['items'] + second['items']], ['0', '1', '2', '3'])
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(final['items'], [])
        self.assertEqual(final['next_cursor'], second['next_cursor'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.a.state_dir.rglob('*') if p.is_file()})

    def test_historical_cursor_union_no_loss_with_duplicate_snapshots(self):
        state, _ = self.prepare()
        first = agent_messages.messages(self.a, limit=1)
        original = copy.deepcopy(state['current'])
        shorter = copy.deepcopy(original); shorter['messages'] = shorter['messages'][:2]
        original['messages'][0]['answered'] = True
        original['messages'].append({'id': '0', 'run_id': 'another-child', 'body': 'distinct'})
        state['history'] = [shorter, original, copy.deepcopy(shorter)]
        state['current'] = {'attempt_dir': str(self.a.state_dir / 'attempts' / str(uuid.uuid4())), 'item': 'T2',
                            'messages': [{'id': 'next', 'run_id': 'child-run'}]}
        self.a.save_state(state)
        remaining = agent_messages.messages(self.a, first['next_cursor'])
        self.assertEqual([(m['run_id'], m['id']) for m in remaining['items']],
                         [('child-run', '1'), ('child-run', '2'), ('child-run', '3'), ('another-child', '0')])
        self.assertEqual(agent_messages.messages(self.a)['items'][0]['id'], 'next')
        self.assertFalse(remaining['has_more'])

    def test_foreign_generation_run_and_malformed_cursor_rejected(self):
        state, _ = self.prepare()
        cursor = agent_messages.messages(self.a)['next_cursor']
        with self.assertRaisesRegex(ContractError, 'another workspace'):
            agent_messages.messages(self.b, cursor)
        for invalid in ('!', 'x' * 16385, 'e30', 123, ''):
            with self.subTest(cursor=invalid), self.assertRaises(ContractError):
                agent_messages.messages(self.a, invalid)
        for limit in (0, 1001, True, 1.5, '10'):
            with self.subTest(limit=limit), self.assertRaises(ContractError):
                agent_messages.messages(self.a, limit=limit)
        state['run_id'] = 'new-run'; self.a.save_state(state)
        with self.assertRaisesRegex(ContractError, 'another generation or run'):
            agent_messages.messages(self.a, cursor)
        state['run_id'] = 'fixture-run'; self.a.save_state(state)
        intent = goal_intent.read(self.a)
        with patch.object(goal_intent, 'read', return_value=dict(intent, generation=str(uuid.uuid4()))):
            with self.assertRaisesRegex(ContractError, 'another generation or run'):
                agent_messages.messages(self.a, cursor)

    def test_child_restricted_to_exact_current_attempt(self):
        state, attempt = self.prepare()
        old = agent_messages.messages(self.a)['next_cursor']
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}):
            self.assertEqual(len(agent_messages.messages(self.a)['items']), 4)
        state['history'].append(state['current'])
        next_attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        self.a.context.snapshot(next_attempt)
        state['current'] = {'attempt_dir': str(next_attempt), 'messages': []}
        self.a.save_state(state)
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}), self.assertRaises(ContractError):
            agent_messages.messages(self.a)
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(next_attempt)}):
            self.assertEqual(agent_messages.messages(self.a)['items'], [])
            with self.assertRaisesRegex(ContractError, 'historical'):
                agent_messages.messages(self.a, old)

    def test_append_after_page_end_remains_visible_once(self):
        state, _ = self.prepare()
        first = agent_messages.messages(self.a)
        state['current']['messages'].append({'id': 'new', 'run_id': 'child-run', 'body': 'arrived later'})
        self.a.save_state(state)
        appended = agent_messages.messages(self.a, first['next_cursor'])
        self.assertEqual([item['id'] for item in appended['items']], ['new'])
        self.assertEqual(agent_messages.messages(self.a, appended['next_cursor'])['items'], [])

    def test_empty_current_and_offset_bounds(self):
        self.assertEqual(agent_messages.messages(self.a)['items'], [])
        self.prepare()
        response = agent_messages.messages(self.a)
        cursor = agent_messages._decode(response['next_cursor'])
        cursor['offset'] = 5
        with self.assertRaisesRegex(ContractError, 'offset'):
            agent_messages.messages(self.a, agent_messages._encode(cursor))
