"""Offline host contention and exact coordinator resource-resume contracts."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import uuid
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from team_runner import event_scope, goal_intent, heavy_resource, resource_wait
from team_runner.runner_store import atomic_json, lock, read_json
import test_platform as fixtures
import test_team_graph as graph_fixtures
import test_runner_autonomy as autonomy_fixtures


class ResourceWait(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt
    prepare = graph_fixtures.GraphExecution.prepare
    delegate = graph_fixtures.GraphExecution.delegate
    finish = graph_fixtures.GraphExecution.finish
    enable = autonomy_fixtures.Autonomy.enable
    command = autonomy_fixtures.Autonomy.command
    set_policy = autonomy_fixtures.Autonomy.set_policy
    process = autonomy_fixtures.Autonomy.process

    def fixture(self):
        attempt, root = self.prepare(); self.enable()
        self.path = self.root / 'heavy.lock'
        patcher = patch.object(heavy_resource, 'default_path', return_value=self.path)
        patcher.start(); self.addCleanup(patcher.stop)
        self.env = {'TEAM_RUNNER_ATTEMPT': str(attempt), 'ORCA_TERMINAL_HANDLE': root['terminal']}
        state = self.a.load_state(); cur = state['current']
        cur.update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        payload = {'dispatchId': root['dispatch_id'], 'taskId': root['task_id']}
        cur['messages'] = [dict(id=identifier, run_id=root['run_id'], type=kind,
                                body='Explicit technical resource wait', payload=payload)
                           for identifier, kind in (('question', 'question'), ('resource', 'escalation'),
                                                    ('business', 'escalation'))]
        cur['questions'] = ['question']; self.a.save_state(state)
        self.check = attempt / 'verification' / 'queued.json'
        proof = {'kind': 'host_heavy', 'path': str(self.path), 'waiting_at': 10,
                 'check_id': 'native-check', 'attempt': str(attempt)}
        atomic_json(self.check, {'id': 'native-check', 'attempt': str(attempt), 'status': 'waiting_resource',
                                 'result': 'UNKNOWN', 'resource_wait': proof})
        atomic_json(str(self.check) + '.resource-wait.json', proof)
        return attempt, root

    def register(self, escalations=None, identifier=None, question='question'):
        with patch.dict(os.environ, self.env):
            return resource_wait.register(self.a, question, str(self.check),
                                          ['resource'] if escalations is None else escalations,
                                          identifier or str(uuid.uuid4()))

    def native_wait(self):
        attempt, root = self.fixture()
        state = self.a.load_state(); state['current']['messages'].pop(0)
        state['current']['messages'][1]['answered'] = True
        state['current']['questions'] = []; self.a.save_state(state)
        return attempt, root, self.register(question=None)

    def test_native_wait_needs_no_root_question_and_resumes_exact_dispatch_once(self):
        attempt, root, command = self.native_wait()
        self.assertEqual(command['action'], 'resource-resume')
        state = self.a.load_state()
        technical = {'id': 'technical-end', 'run_id': root['run_id'], 'type': 'escalation',
                     'created_at': datetime.now(timezone.utc).isoformat(),
                     'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
                                 'handle': root['terminal'], 'stopReason': 'finished'}}
        state['current']['messages'].append(technical); self.a.save_state(state)
        with lock(self.path):
            self.process()
            state = self.a.load_state(); reg = self.a.launcher.registry(attempt)
            self.assertEqual(event_scope.classify(technical, state['current'], reg)['kind'], 'waiting_resource')
            self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))
        self.process(); self.process()
        state = self.a.load_state()
        self.assertTrue(state['current']['messages'][0]['closed'])
        self.assertNotIn('closed', state['current']['messages'][1])
        ops = read_json(self.a.state_dir / 'operations.json')
        self.assertEqual(len(ops), 1)
        op = ops['resource-resume:' + command['id']]
        self.assertEqual(op['status'], 'completed')
        self.assertIn('dispatch:' + root['dispatch_id'], op['args'])
        later = dict(technical, id='unrelated-later-turn')
        self.assertEqual(event_scope.classify(later, state['current'], self.a.launcher.registry(attempt))['kind'], 'active')

    def test_native_wait_does_not_override_an_unrelated_business_question(self):
        attempt, root, command = self.native_wait()
        state = self.a.load_state()
        state['current']['messages'].append(dict(id='business-question', type='question', run_id=root['run_id'],
            payload={'dispatchId': root['dispatch_id'], 'taskId': root['task_id']}))
        self.a.save_state(state); self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'pending')
        self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))

    def test_native_wait_preserves_unresolved_business_escalation(self):
        attempt, root, command = self.native_wait()
        state = self.a.load_state(); state['current']['messages'][1].pop('answered')
        self.a.save_state(state); self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'pending')
        self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))

    def test_cli_registers_native_wait_without_asking_root(self):
        from team_runner.cli import main
        attempt, root = self.fixture()
        state = self.a.load_state(); state['current']['messages'].pop(0); self.a.save_state(state)
        env = dict(self.env, TEAM_RUNNER_PROJECT=self.a.context.project_id,
                   TEAM_RUNNER_WORKSPACE=self.a.context.workspace_id)
        output = io.StringIO()
        with patch.dict(os.environ, env), patch('team_runner.cli.Catalog', return_value=self.catalog), \
                patch('team_runner.cli.Runner', return_value=self.a), redirect_stdout(output):
            result = main(['wait-resource', '--check', str(self.check), '--request-id', str(uuid.uuid4())])
        self.assertEqual(result, 0)
        command = json.loads(output.getvalue())
        self.assertEqual(command['action'], 'resource-resume')
        self.assertIsNone(command['data']['message_id'])

    def test_native_wait_cancelled_by_pause_restores_technical_end_diagnostic(self):
        attempt, root, command = self.native_wait()
        state = self.a.load_state()
        technical = {'id': 'technical-end', 'run_id': root['run_id'], 'type': 'escalation',
                     'created_at': datetime.now(timezone.utc).isoformat(),
                     'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
                                 'handle': root['terminal'], 'stopReason': 'finished'}}
        state['current']['messages'].append(technical); self.a.save_state(state)
        with lock(self.path):self.process()
        state = self.a.load_state(); state['paused'] = True; self.a.save_state(state)
        self.process(); state = self.a.load_state()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'error')
        self.assertEqual(event_scope.classify(technical, state['current'], self.a.launcher.registry(attempt))['kind'], 'active')
        self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))

    def test_linked_technical_end_survives_repeated_busy_polls_then_resumes(self):
        attempt, root, command = self.native_wait()
        state = self.a.load_state(); technical = state['current']['messages'][0]
        technical.update(created_at=datetime.now(timezone.utc).isoformat(), payload={
            'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
            'handle': root['terminal'], 'stopReason': 'finished'})
        self.a.save_state(state)
        with lock(self.path):
            self.process(); self.process()
            self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'pending')
        self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'done')
        self.assertTrue(self.a.load_state()['current']['messages'][0]['closed'])

    def test_pending_wait_does_not_hide_a_later_unrelated_turn_end(self):
        attempt, root, command = self.native_wait()
        state = self.a.load_state()
        technical = {'id': 'first-end', 'run_id': root['run_id'], 'type': 'escalation',
                     'created_at': datetime.now(timezone.utc).isoformat(),
                     'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
                                 'handle': root['terminal'], 'stopReason': 'finished'}}
        state['current']['messages'].append(technical); self.a.save_state(state)
        with lock(self.path):
            self.process()
            state = self.a.load_state(); later = dict(technical, id='unrelated-later-end')
            state['current']['messages'].append(later); self.a.save_state(state); self.process()
        self.process(); state = self.a.load_state()
        self.assertEqual(event_scope.classify(later, state['current'], self.a.launcher.registry(attempt))['kind'], 'active')
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'pending')
        self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))

    def test_wait_turn_end_with_second_precision_matches_fractional_native_wait(self):
        attempt, root = self.fixture()
        state = self.a.load_state(); state['current']['messages'].pop(0)
        state['current']['messages'][1]['answered'] = True
        self.a.save_state(state)
        proof = read_json(str(self.check) + '.resource-wait.json'); proof['waiting_at'] = 1794824600.75
        atomic_json(str(self.check) + '.resource-wait.json', proof)
        record = read_json(self.check); record['resource_wait'] = proof; atomic_json(self.check, record)
        command = self.register(question=None)
        state = self.a.load_state()
        technical = {'id': 'same-second-end', 'run_id': root['run_id'], 'type': 'escalation',
                     'created_at': datetime.fromtimestamp(1794824600, timezone.utc).isoformat(),
                     'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
                                 'handle': root['terminal'], 'stopReason': 'finished'}}
        state['current']['messages'].append(technical); self.a.save_state(state)
        with lock(self.path):self.process()
        self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'done')
        self.assertEqual(self.a.load_state()['current']['resource_waits'][command['id']]['technical_events'], ['same-second-end'])

    def test_cancellation_between_availability_checks_cancels_wait_record(self):
        attempt, root, command = self.native_wait()
        state = self.a.load_state(); technical = state['current']['messages'][0]
        technical.update(created_at=datetime.now(timezone.utc).isoformat(), payload={
            'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
            'handle': root['terminal'], 'stopReason': 'finished'})
        self.a.save_state(state)
        with lock(self.path):self.process()
        original = resource_wait.ready; count = []
        def check(runner, state, data):
            count.append(True)
            if len(count) == 2:
                state['paused'] = True; runner.save_state(state)
            return original(runner, state, data)
        with patch.object(resource_wait, 'ready', side_effect=check):self.process()
        state = self.a.load_state()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'error')
        self.assertEqual(state['current']['resource_waits'][command['id']]['status'], 'cancelled')
        self.assertEqual(event_scope.classify(technical, state['current'], self.a.launcher.registry(attempt))['kind'], 'active')
        self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))

    def test_native_wait_lost_send_receipt_settles_after_pause_without_replay(self):
        attempt, root, command = self.native_wait()
        original = self.a.orca; sends = []
        def transport(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('orchestration', 'send'):
                sends.append(args); raise RuntimeError('lost resource continuation receipt')
            return result
        self.a.orca = transport; self.process()
        self.assertEqual(self.a.load_state()['control_uncertain'], command['id'])
        self.assertNotIn('closed', self.a.load_state()['current']['messages'][0])
        self.a.orca = original; goal_intent.change(self.a, desired_state='paused')
        with lock(self.a.state_dir / 'runner.lock'):
            self.a.recover(self.a.load_state(), technical_only=True)
        state = self.a.load_state()
        self.assertNotIn('control_uncertain', state)
        self.assertTrue(state['current']['messages'][0]['closed'])
        self.assertNotIn('closed', state['current']['messages'][1])
        self.assertEqual(len(sends), 1)

    def test_busy_then_free_replies_once_and_preserves_unrelated_decisions(self):
        self.fixture(); identifier = str(uuid.uuid4())
        command = self.register(identifier=identifier)
        self.assertEqual(self.register(identifier=identifier), command)
        with lock(self.path):
            self.process()
            self.assertEqual(read_json(self.a.state_dir / 'commands' / (identifier + '.json'))['status'], 'pending')
            self.assertNotIn('answered', self.a.load_state()['current']['messages'][0])
            self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))
        self.process(); self.process()
        state = self.a.load_state(); question, resource, business = state['current']['messages']
        self.assertTrue(question['answered']); self.assertTrue(resource['closed'])
        self.assertNotIn('closed', business)
        self.assertEqual(resource['resource_resolution']['request_id'], identifier)
        operations = read_json(self.a.state_dir / 'operations.json')
        self.assertEqual(len(operations), 1)
        self.assertEqual(next(iter(operations.values()))['status'], 'completed')
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (identifier + '.json'))['status'], 'done')

    def test_pending_wait_is_cancelled_by_pause_stop_and_policy_changes(self):
        for change in ('paused', 'stop_requested', 'disabled', 'generation', 'epoch'):
            with self.subTest(change=change):
                # Each case has its own checkout, scope and command journal.
                case = ResourceWait(); case.setUp()
                try:
                    case.fixture(); command = case.register()
                    state = case.a.load_state()
                    if change in ('paused', 'stop_requested'):
                        state[change] = True; case.a.save_state(state)
                    elif change == 'disabled':
                        with lock(case.path):
                            case.set_policy(False)
                    else:
                        intent = goal_intent.read(case.a)
                        intent[change] = str(uuid.uuid4()) if change == 'generation' else intent['epoch'] + 1
                        with patch.object(goal_intent, 'read', return_value=intent):
                            case.process()
                    if change not in ('generation', 'epoch'):
                        case.process()
                    receipt = read_json(case.a.state_dir / 'commands' / (command['id'] + '.json'))
                    self.assertEqual(receipt['status'], 'error')
                    self.assertIn('cancelled', receipt['error'])
                    self.assertNotIn('answered', case.a.load_state()['current']['messages'][0])
                finally:
                    case.doCleanups()

    def test_unknown_effect_and_fenced_attempt_keep_wait_pending(self):
        attempt, root = self.fixture(); command = self.register()
        op_path = self.a.launcher.journal(attempt); before = read_json(op_path)
        ops = copy.deepcopy(before); ops['unknown'] = {'status': 'unknown'}; atomic_json(op_path, ops)
        self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'pending')
        atomic_json(op_path, before)
        reg = self.a.launcher.registry(attempt); reg['fenced'] = True; self.a.launcher.save_registry(attempt, reg)
        self.process()
        self.assertNotIn('answered', self.a.load_state()['current']['messages'][0])

    def test_resource_reacquired_before_input_keeps_original_reply_pending(self):
        self.fixture(); command = self.register()
        with patch.object(heavy_resource, 'available', side_effect=[True, False]):
            self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'pending')
        self.assertFalse(read_json(self.a.state_dir / 'operations.json', {}))
        self.process()
        self.assertTrue(self.a.load_state()['current']['messages'][0]['answered'])

    def test_only_current_coordinator_and_its_exact_messages_can_register(self):
        attempt, root = self.fixture()
        with patch.dict(os.environ, dict(self.env, ORCA_TERMINAL_HANDLE='foreign')):
            with self.assertRaisesRegex(ValueError, 'caller'):
                resource_wait.register(self.a, 'question', str(self.check), [], str(uuid.uuid4()))
        state = self.a.load_state(); state['current']['messages'][0]['payload']['taskId'] = 'foreign'
        self.a.save_state(state)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.register()

    def test_foreign_attempt_and_changed_native_evidence_are_rejected(self):
        attempt, root = self.fixture()
        outside = self.check; self.check = self.root / 'foreign.json'
        with self.assertRaises(OSError):
            self.register()
        self.check = outside
        command = self.register()
        proof = read_json(str(self.check) + '.resource-wait.json'); proof['waiting_at'] += 1
        atomic_json(str(self.check) + '.resource-wait.json', proof)
        self.process()
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'error')
        self.assertNotIn('answered', self.a.load_state()['current']['messages'][0])

    def test_plain_required_record_cannot_authorize_automatic_reply(self):
        self.fixture()
        Path(str(self.check) + '.resource-wait.json').unlink()
        with self.assertRaises(OSError):
            self.register()

    def test_lost_reply_receipt_reconciles_without_input_replay_even_after_pause(self):
        self.fixture(); command = self.register()
        original = self.a.orca; sends = []
        def transport(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('orchestration', 'reply'):
                sends.append(args); raise RuntimeError('lost reply receipt')
            return result
        self.a.orca = transport
        self.process()
        self.assertEqual(self.a.load_state()['control_uncertain'], command['id'])
        self.assertNotIn('closed', self.a.load_state()['current']['messages'][1])
        self.a.orca = original
        goal_intent.change(self.a, desired_state='paused')
        state = self.a.load_state()
        with lock(self.a.state_dir / 'runner.lock'):
            self.a.recover(state, technical_only=True)
        state = self.a.load_state()
        self.assertNotIn('control_uncertain', state)
        self.assertTrue(state['current']['messages'][0]['answered'])
        self.assertTrue(state['current']['messages'][1]['closed'])
        self.assertNotIn('closed', state['current']['messages'][2])
        self.assertEqual(len(sends), 1)

    def test_native_probe_does_not_clear_incomplete_residue(self):
        self.fixture(); self.path.touch()
        metadata = Path(str(self.path) + '.json')
        for value in ({'status': 'running', 'cleanup_inventory_complete': False}, [],
                      {'status': 'cleanup_required', 'cleanup_inventory_complete': True,
                       'processes': [{'pid': os.getpid(), 'stamp': 'unknown'}], 'ports': [12345]}):
            atomic_json(metadata, value)
            with patch.object(heavy_resource, 'listening', return_value=True):
                self.assertFalse(heavy_resource.available())
            self.assertEqual(read_json(metadata), value)
        atomic_json(metadata, {'status': 'clean'})
        self.assertTrue(heavy_resource.available())


if __name__ == '__main__':
    unittest.main()
