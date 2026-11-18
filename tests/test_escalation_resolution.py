"""Explicit operator decisions settle only the addressed, owned escalation."""
import copy
import os
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
import test_team_graph as graph_fixtures
from team_runner import application, autonomy, event_scope, goal_intent, protocol
from team_runner.runner_store import atomic_json, read_json


class EscalationResolution(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt
    prepare = graph_fixtures.GraphExecution.prepare
    delegate = graph_fixtures.GraphExecution.delegate
    finish = graph_fixtures.GraphExecution.finish

    def setup_escalations(self):
        cfg = graph_fixtures.small_graph()
        cfg['edges'][0]['max_passes'] = 1
        attempt, root = self.prepare(cfg)
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        self.a.authorization = {k: goal_intent.read(self.a)[k] for k in ('generation', 'epoch')}
        autonomy.configure(self.a, True, str(uuid.uuid4()), unlimited_passes=True)
        _, child = self.delegate(attempt, root)
        self.finish(attempt, child)
        call, _ = self.delegate(attempt, root)
        state = self.a.load_state()
        state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        escalation = {'id': 'budget-request', 'type': 'escalation', 'run_id': root['run_id'],
                      'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id']}}
        state['current']['messages'].append(escalation)
        self.a.save_state(state)
        return attempt, call, escalation

    def command(self, message_id='budget-request'):
        view = protocol.summary(self.a)
        return {'schema_version': 1, 'request_id': str(uuid.uuid4()), 'action': 'resolve-escalation',
                'scope': {'identity': view['identity'], 'generation': view['generation'],
                          'attempt': view['current']['attempt'], 'revision': view['revision']},
                'payload': {'message_id': message_id, 'text': 'Approved this repair; keep acceptance gates.'}}

    def test_explicit_resolution_unblocks_grant_and_is_durable_idempotent(self):
        attempt, call, escalation = self.setup_escalations()
        snapshot = (attempt / 'config.json').read_bytes()
        before = read_json(self.a.launcher.orca.root / 'transport.json')
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        command = self.command()
        receipt = application.submit_v1(self.a, command)
        self.assertEqual(receipt['status'], 'succeeded', receipt)
        self.assertEqual(application.submit_v1(self.a, command), receipt)
        message = next(m for m in self.a.load_state()['current']['messages'] if m['id'] == escalation['id'])
        self.assertTrue(message['closed'])
        self.assertEqual(message['resolution']['request_id'], command['request_id'])
        self.assertEqual(message['resolution']['text'], command['payload']['text'])
        self.assertEqual(read_json(self.a.launcher.orca.root / 'transport.json'), before)
        self.assertEqual((attempt / 'config.json').read_bytes(), snapshot)
        self.assertIsNotNone(autonomy.grant(self.a, attempt, call['id']))
        _, child = self.delegate(attempt, self.a.launcher.registry(attempt)['agents'][0], request=call['id'])
        self.assertIsNotNone(child)

    def test_other_escalations_questions_and_unknown_effects_remain_fences(self):
        attempt, call, escalation = self.setup_escalations()
        state = self.a.load_state()
        other = dict(escalation, id='other-decision')
        question = dict(escalation, id='business-question', type='question')
        state['current']['messages'] += [other, question]
        self.a.save_state(state)
        self.assertEqual(application.submit_v1(self.a, self.command())['status'], 'succeeded')
        state = self.a.load_state()
        self.assertEqual({m['id'] for m in event_scope.pending(state['current'], self.a.launcher.registry(attempt))},
                         {'other-decision', 'business-question'})
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        self.assertEqual(application.submit_v1(self.a, self.command('business-question'))['status'], 'failed')
        self.assertEqual(application.submit_v1(self.a, self.command('other-decision'))['status'], 'succeeded')
        state = self.a.load_state()
        next(m for m in state['current']['messages'] if m['id'] == question['id'])['answered'] = True
        self.a.save_state(state)
        atomic_json(self.a.launcher.journal(attempt), {'unknown': {'status': 'unknown'}})
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))

    def test_rejects_missing_ambiguous_unowned_and_stale_scope(self):
        attempt, _, escalation = self.setup_escalations()
        self.assertEqual(application.submit_v1(self.a, self.command('missing'))['status'], 'failed')
        state = self.a.load_state()
        original = copy.deepcopy(state)
        state['current']['messages'].append(copy.deepcopy(escalation))
        self.a.save_state(state)
        self.assertEqual(application.submit_v1(self.a, self.command())['status'], 'failed')
        original['current']['messages'][-1]['payload'] = {'dispatchId': 'foreign'}
        self.a.save_state(original)
        self.assertEqual(application.submit_v1(self.a, self.command())['status'], 'failed')
        command = self.command()
        state = self.a.load_state(); state['paused'] = True; self.a.save_state(state)
        with self.assertRaises(protocol.ContractError):
            application.submit_v1(self.a, command)

    def test_child_and_unscoped_commands_cannot_resolve_escalations(self):
        attempt, _, _ = self.setup_escalations()
        command = self.command()
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}), self.assertRaises(protocol.ContractError):
            application.submit_v1(self.a, command)
        with self.assertRaises(protocol.ContractError):
            application.submit_command(self.a, command['action'], command['payload'], str(uuid.uuid4()))
        for payload in ({'message_id': 'budget-request', 'text': ''},
                        {'message_id': 'budget-request', 'text': 'ok', 'run_id': ''}):
            invalid = dict(command, request_id=str(uuid.uuid4()), payload=payload)
            with self.assertRaises(protocol.ContractError):
                application.submit_v1(self.a, invalid)


if __name__ == '__main__':
    unittest.main()
