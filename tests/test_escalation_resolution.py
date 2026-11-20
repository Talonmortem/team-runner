"""Explicit operator decisions settle only the addressed, owned escalation."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from unittest.mock import patch

import test_platform as fixtures
import test_team_graph as graph_fixtures
from team_runner import application, autonomy, event_scope, goal_intent, protocol, runner_ui
from team_runner.cli import main
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

    def command(self, message_id='budget-request', *, resume=False):
        view = protocol.summary(self.a)
        return {'schema_version': 1, 'request_id': str(uuid.uuid4()), 'action': 'resolve-escalation',
                'scope': {'identity': view['identity'], 'generation': view['generation'],
                          'attempt': view['current']['attempt'], 'revision': view['revision']},
                'payload': {'message_id': message_id, 'resume': resume,
                            'text': 'Approved this repair; keep acceptance gates.'}}

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

    def resume_command(self, *, open_child=False):
        attempt, root = self.prepare()
        goal = application.goals(self.a)[0]
        goal_intent.authorize(self.a, {'goal': goal}, str(uuid.uuid4()))
        _, child = self.delegate(attempt, root)
        if not open_child:
            self.finish(attempt, child)
            self.assertEqual(self.a.launcher.registry(attempt)['graph_calls'][0]['status'], 'completed')
            self.assertEqual(self.a.launcher.liveness(root['dispatch_id']), 'live')
        state = self.a.load_state()
        state.update(goal=goal, run_id=root['run_id'], mailbox='parent-mailbox')
        state['current'].setdefault('messages', []).append({'id': 'budget-request', 'type': 'escalation', 'run_id': root['run_id'],
            'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
                        **({} if open_child else {'handle': root['terminal'], 'stopReason': 'finished'})}})
        self.a.save_state(state)
        command = self.command()
        command['payload'].update(resume=True, run_id=root['run_id'])
        return root, command

    def test_resume_sends_guidance_to_same_dispatch_once(self):
        root, command = self.resume_command()
        def send(*args, **kwargs):
            job = read_json(self.a.state_dir / 'operations.json')['escalation-resume:' + command['request_id']]
            self.assertEqual(job['status'], 'pending')
            self.assertEqual(job['request_id'], args[-1])
            self.assertFalse(self.escalation().get('closed'))
            return self.a.launcher.orca(*args, **kwargs)

        with patch.object(self.a, 'orca', side_effect=send) as call:
            receipt = application.submit_v1(self.a, command)
            self.assertEqual(receipt['status'], 'succeeded', receipt)
            self.assertEqual(receipt['result']['delivery'], 'accepted')
            self.assertEqual(application.submit_v1(self.a, command), receipt)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[:8], ('orchestration', 'send', '--run', root['run_id'],
                          '--to', 'dispatch:' + root['dispatch_id'], '--type', 'status'))
        self.assertEqual(call.call_args.kwargs['mailbox'], 'parent-mailbox')
        self.assertTrue(self.escalation()['closed'])

    def escalation(self, state=None):
        return next(m for m in (state or self.a.load_state())['current']['messages'] if m['id'] == 'budget-request')

    def test_resume_reconciles_exact_receipt_without_resending(self):
        _, command = self.resume_command()
        transport = self.a.launcher.orca
        delivered = {}
        receipt_variant = None

        def call(*args, **kwargs):
            if args[:2] == ('orchestration', 'send'):
                self.assertFalse(delivered)
                delivered[args[-1]] = transport(*args, **kwargs)
                raise RuntimeError('response lost')
            if args[:2] == ('orchestration', 'request-show'):
                if receipt_variant is None:
                    return {'state': 'absent'}
                result = copy.deepcopy(delivered[args[-1]])
                if receipt_variant in ('run_id', 'to_handle'):
                    result['message'][receipt_variant] = 'foreign'
                elif receipt_variant == 'requestId':
                    result['mutation']['requestId'] = 'foreign'
                return {'state': 'completed', 'receipt': {'ok': True, 'result': result}}
            return transport(*args, **kwargs)

        with patch.object(self.a, 'orca', side_effect=call), \
                patch.object(self.a.launcher, 'reconcile_attempt', return_value=[]):
            receipt = application.submit_v1(self.a, command)
            self.assertEqual(receipt['status'], 'unknown', receipt)
            state = self.a.load_state()
            follow_up = dict(self.command(), payload=command['payload'])
            with self.assertRaisesRegex(protocol.ContractError, 'Reconcile the unconfirmed effect'):
                application.submit_v1(self.a, follow_up)
            for receipt_variant in (None, 'run_id', 'to_handle', 'requestId'):
                with self.subTest(receipt=receipt_variant):
                    self.a.recover(state, technical_only=True)
                    self.assertIn('control_uncertain', state)
                    self.assertFalse(self.escalation(state).get('closed'))
            original_request = next(iter(delivered))
            with self.assertRaisesRegex(ValueError, 'original receipt'):
                self.a.recover(state, original_request, technical_only=True)
            receipt_variant = 'exact'
            self.a.recover(state, technical_only=True)
        self.assertNotIn('control_uncertain', state)
        self.assertTrue(self.escalation(state)['closed'])
        self.assertEqual(protocol.operation(self.a, command['request_id']), receipt)
        self.assertEqual(application.submit_v1(self.a, command), receipt)

    def test_owner_interrupts_after_send_and_recovers_original_request(self):
        _, command = self.resume_command()

        class OwnerExited(BaseException):
            pass

        def checkpoint(stage):
            if stage == 'send:after_effect':
                self.assertFalse(self.escalation().get('closed'))
                raise OwnerExited

        with patch('team_runner.runner_operations.checkpoint', side_effect=checkpoint), self.assertRaises(OwnerExited):
            application.submit_v1(self.a, command)
        state = self.a.load_state()
        self.a.process_controls(state)
        interrupted = protocol.operation(self.a, command['request_id'])
        self.assertEqual(interrupted['status'], 'unknown')
        with patch.object(self.a, 'orca', wraps=self.a.orca) as call:
            self.a.recover(state, technical_only=True)
        self.assertTrue(self.escalation(state)['closed'])
        self.assertFalse(any(c.args[:2] == ('orchestration', 'send') for c in call.call_args_list))
        self.assertEqual(application.submit_v1(self.a, command), interrupted)

    def test_foreign_immediate_receipt_requires_reconciliation(self):
        _, command = self.resume_command()
        transport = self.a.orca

        def call(*args, **kwargs):
            result = transport(*args, **kwargs)
            if args[:2] == ('orchestration', 'send'):
                self.assertFalse(self.escalation().get('closed'))
                result['message']['to_handle'] = 'dispatch:other'
            return result

        with patch.object(self.a, 'orca', side_effect=call):
            receipt = application.submit_v1(self.a, command)
        self.assertEqual(receipt['status'], 'unknown')
        op = read_json(self.a.state_dir / 'operations.json')['escalation-resume:' + command['request_id']]
        self.assertEqual(op['status'], 'unknown')
        self.assertFalse(self.escalation().get('closed'))
        self.a.recover(self.a.load_state(), technical_only=True)
        self.assertTrue(self.escalation()['closed'])

    def test_reconcile_confirms_orca_follow_up_in_same_terminal_without_resending(self):
        root, command = self.resume_command()
        transport = self.a.orca
        follow_up = 'follow-up-dispatch'
        receipt = {}
        source = {'id': root['dispatch_id'], 'runId': root['run_id'],
                  'assigneeHandle': root['terminal'], 'processIncarnation': 'registered-process',
                  'status': 'failed'}
        target = dict(source, id=follow_up, followUpOf=root['dispatch_id'], status='dispatched')

        def call(*args, **kwargs):
            if args[:2] == ('orchestration', 'send'):
                self.assertFalse(receipt, 'must not resend the decision')
                receipt.update(transport(*args, **kwargs))
                receipt['message'].update(to_handle='dispatch:' + follow_up,
                                          from_handle=kwargs['mailbox'], type=args[7],
                                          subject=args[9], body=args[11])
                return receipt
            if args[:2] == ('orchestration', 'request-show') and args[-1] == receipt['mutation']['requestId']:
                return {'state': 'completed', 'receipt': receipt}
            if args[:2] == ('orchestration', 'worker-show'):
                if args[-1] == root['dispatch_id']:
                    return {'dispatch': source}
                if args[-1] == follow_up:
                    return {'dispatch': target}
            return transport(*args, **kwargs)

        with patch.object(self.a, 'orca', side_effect=call), \
                patch.object(self.a.launcher, 'reconcile_attempt', return_value=[]):
            result = application.submit_v1(self.a, command)
            self.assertEqual(result['status'], 'unknown')
            for field, value in (('followUpOf', 'unrelated-dispatch'),
                                 ('processIncarnation', 'different-process'), ('runId', 'other-run')):
                with self.subTest(field=field), patch.dict(target, {field: value}):
                    state = self.a.load_state()
                    self.a.recover(state, technical_only=True)
                    self.assertFalse(self.escalation(state).get('closed'))
                    self.assertEqual(state.get('control_uncertain'), command['request_id'])
            state = self.a.load_state()
            self.a.recover(state, technical_only=True)
            self.assertTrue(self.escalation(state)['closed'])
            self.assertNotIn('control_uncertain', state)
            self.assertEqual(state['current']['dispatch_id'], root['dispatch_id'])
            self.a.recover(state, technical_only=True)
            self.assertFalse(state['recovery_issues'])
        self.assertEqual(application.submit_v1(self.a, command), result)

    def test_recovery_registers_only_the_live_follow_up_on_original_terminal(self):
        from team_runner.runner_entry import boot_id, process_stamp

        attempt, root = self.prepare()
        launcher = self.a.launcher
        journal = launcher.journal(attempt)
        entries = read_json(journal)
        key = root['id'] + '-terminal'
        launch = entries[key]
        terminal = launch['result']['terminal']
        marker = launch['evidence']
        atomic_json(Path(marker['marker']), {'nonce': marker['nonce'], 'terminal': terminal,
                    'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid()), 'boot_id': boot_id()})
        follow_up = 'follow-up'
        source = {'id': root['dispatch_id'], 'runId': root['run_id'], 'taskId': root['task_id'],
                  'assigneeHandle': root['terminal'], 'status': 'failed',
                  'processIncarnation': f"{terminal['ptyId']}:{terminal['incarnationId']}"}
        target = dict(source, id=follow_up, taskId='next-task', status='dispatched', followUpOf=root['dispatch_id'])
        proof = {'dispatch': target, 'terminal': terminal, 'terminalResource': {'ownerDispatchId': follow_up},
                 'observation': {'status': 'live', 'exactWorker': True}}
        operation = str(uuid.uuid4())

        with patch.object(launcher, 'orca', side_effect=lambda *a, **k: (
                {'dispatch': source} if a[-1] == source['id'] else proof)) as call:
            with patch.dict(target, {'processIncarnation': 'other-process'}):
                self.assertIsNone(launcher.recover_coordinator_follow_up(attempt, source['id'], follow_up, operation))
                self.assertEqual(len(launcher.registry(attempt)['agents']), 1)
            record = launcher.recover_coordinator_follow_up(attempt, source['id'], follow_up, operation)
            self.assertEqual(record['terminal_launch_key'], key)
            self.assertEqual(record['task_id'], 'next-task')
            self.assertEqual(launcher.registry(attempt)['agents'][0]['status'], 'superseded')
            self.assertEqual(launcher.recover_coordinator_follow_up(attempt, source['id'], follow_up, operation), record)
            self.assertEqual(call.call_count, 4)
            self.assertTrue(all(c.args[:2] == ('orchestration', 'worker-show') for c in call.call_args_list))

    def test_paused_open_child_and_unknown_effect_prevent_delivery(self):
        root, command = self.resume_command(open_child=True)
        with patch.object(self.a, 'orca', wraps=self.a.orca) as call:
            self.assertEqual(application.submit_v1(self.a, command)['status'], 'failed')
            state = self.a.load_state()
            attempt = state['current']['attempt_dir']
            child = next(agent for agent in self.a.launcher.registry(attempt)['agents'] if agent['id'] != root['id'])
            self.finish(Path(attempt), child)
            atomic_json(self.a.state_dir / 'operations.json', {'unsettled': {'status': 'unknown'}})
            with self.assertRaisesRegex(protocol.ContractError, 'Reconcile the unconfirmed effect'):
                application.submit_v1(self.a, self.command(resume=True))
            (self.a.state_dir / 'operations.json').unlink()
            goal_intent.change(self.a, desired_state='paused')
            self.assertEqual(application.submit_v1(self.a, self.command(resume=True))['status'], 'failed')
        self.assertFalse(any(c.args[:2] == ('orchestration', 'send') for c in call.call_args_list))

    def test_new_resume_required_and_accepted_legacy_command_is_local(self):
        self.setup_escalations()
        command = self.command()
        del command['payload']['resume']
        with self.assertRaisesRegex(protocol.ContractError, 'explicit boolean resume'):
            application.submit_v1(self.a, command)
        with self.assertRaisesRegex(protocol.ContractError, 'explicit boolean resume'):
            application.submit_v1(self.a, dict(command, payload=dict(command['payload'], resume=0)))
        state = self.a.load_state()
        path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
        atomic_json(path, {'schema_version': 1, 'id': command['request_id'], 'action': command['action'],
                           'command': command, 'guard': application._execution_scope(self.a, state), 'status': 'pending'})
        with patch.object(self.a, 'orca', wraps=self.a.orca) as call:
            self.a.process_controls(state)
        call.assert_not_called()
        receipt = protocol.operation(self.a, command['request_id'])
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertTrue(self.escalation()['closed'])
        self.assertEqual(application.submit_v1(self.a, command), receipt)

    def test_cli_and_api_share_the_command_receipt(self):
        self.setup_escalations()
        command = self.command()
        path = self.root / 'resolution.json'
        atomic_json(path, command)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('team_runner.cli.Catalog', return_value=self.catalog):
            self.assertEqual(main(['command', '--project', self.a.context.project_id,
                                   '--workspace', self.a.context.workspace_id, '--file', str(path)]), 0)
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt['status'], 'succeeded')
        server, token = runner_ui.make_server(self.catalog)
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            workspace = f'/api/projects/{self.a.context.project_id}/workspaces/{self.a.context.workspace_id}'

            def post(value):
                return urlopen(Request(url + workspace + '/commands', data=json.dumps(value).encode(),
                               headers={'Origin': url, 'Authorization': 'Bearer ' + token,
                                        'Content-Type': 'application/json'}), timeout=3)

            with post(command) as response:
                self.assertEqual(json.load(response), receipt)
            with self.assertRaises(HTTPError) as stale:
                post(dict(command, request_id=str(uuid.uuid4())))
            self.assertEqual(json.load(stale.exception)['error']['code'], 'stale_revision')
            state = self.a.load_state()
            second = dict(self.escalation(state), id='api-decision', closed=False)
            second.pop('resolution')
            state['current']['messages'].append(second)
            self.a.save_state(state)
            with post(self.command('api-decision')) as response:
                self.assertEqual(json.load(response)['status'], 'succeeded')
            self.assertTrue(self.a.load_state()['current']['messages'][-1]['closed'])
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

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
