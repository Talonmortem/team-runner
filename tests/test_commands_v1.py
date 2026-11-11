"""Admission, owner guards, durable crash receipts and immutable results."""
import copy
import contextlib
import io
import json
import threading
import urllib.request
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, protocol, cli, runner_ui
from team_runner.runner_store import atomic_json, read_json, lock


class CommandsV1(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def command(self, action='pause', payload=None):
        view = protocol.summary(self.a)
        return {'schema_version': 1, 'request_id': str(uuid.uuid4()), 'action': action,
                'scope': {'identity': view['identity'], 'generation': view['generation'],
                          'attempt': view['current']['attempt'], 'revision': view['revision']},
                'payload': payload or {}}

    def test_gate_confirmation_survives_healthy_watchdog_poll_only(self):
        state = {'run_id': 'run', 'history': [], 'current': {'item': 'one', 'gate_id': 'gate'}}
        self.a.save_state(state)
        path = self.a.context.state_root / 'projects' / self.a.context.project_id / 'service/watchdog.json'
        healthy = {'phase': 'observing', 'attempts': 0, 'checks': 0, 'blocker': None,
                   'observed_incarnation': 'service', 'epoch': 1, 'incident': 'first', 'next_check_at': 1}
        atomic_json(path, healthy)
        command = self.command('resolve', {'choice': 'start'})
        atomic_json(path, {**healthy, 'incident': 'second', 'next_check_at': 10})
        with patch.object(self.a, 'effect') as effect:
            result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'succeeded')
        effect.assert_called_once()
        for change in ({'phase': 'needs_action'}, {'blocker': 'unavailable'},
                       {'observed_incarnation': 'other'}, {'epoch': 2}):
            self.a.save_state(state)
            atomic_json(path, healthy)
            command = self.command('resolve', {'choice': 'start'})
            atomic_json(path, {**healthy, **change})
            with self.assertRaises(protocol.ContractError) as error:
                application.submit_v1(self.a, command)
            self.assertEqual(error.exception.code, 'stale_revision')
        atomic_json(path, healthy)
        command = self.command('resolve', {'choice': 'start'})
        self.a.save_state({**state, 'current': {'item': 'one', 'gate_id': 'different'}})
        with self.assertRaises(protocol.ContractError):
            application.submit_v1(self.a, command)

    def test_reply_run_scope_disambiguates_identical_child_message_ids(self):
        self.a.save_state({'run_id': 'owner', 'history': [], 'current': {'item': 'one', 'messages': [
            {'id': 'q', 'run_id': 'child-a', 'type': 'question'},
            {'id': 'q', 'run_id': 'child-b', 'type': 'question'}]}})
        with patch.object(self.a, 'effect') as effect:
            ambiguous = application.submit_v1(self.a, self.command('reply', {'message_id': 'q', 'text': 'yes'}))
            self.assertEqual(ambiguous['status'], 'failed')
            effect.assert_not_called()
            result = application.submit_v1(self.a, self.command('reply',
                {'message_id': 'q', 'run_id': 'child-b', 'text': 'yes'}))
        self.assertEqual(result['status'], 'succeeded')
        args = effect.call_args.args
        self.assertEqual(args[args.index('--run') + 1], 'child-b')
        messages = self.a.load_state()['current']['messages']
        self.assertNotIn('answered', messages[0])
        self.assertTrue(messages[1]['answered'])

    def test_exact_retry_returns_immutable_receipt_conflict_rejected(self):
        command = self.command()
        result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(self.a.load_state()['paused'])
        self.a.save_state({'history': [], 'current': {'item': 'new'}})
        self.assertEqual(application.submit_v1(self.a, command), result)
        other = copy.deepcopy(command); other['action'] = 'stop'
        with self.assertRaises(protocol.ContractError) as error:
            application.submit_v1(self.a, other)
        self.assertEqual(error.exception.code, 'request_conflict')
        path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
        before = path.read_bytes()
        with lock(self.a.state_dir / 'runner.lock'):
            self.a.process_controls(self.a.load_state())
        self.assertEqual(path.read_bytes(), before)

    def test_force_retry_receipt_identifies_one_extra_prepared_attempt(self):
        from team_runner import goal_intent
        goal = application.goals(self.a)[0]
        goal_intent.authorize(self.a, {'goal': goal}, str(uuid.uuid4()))
        attempt = fixtures.Platform.attempt(self, self.a)
        state = self.a.load_state()
        state.update(goal=goal, options={'goal': goal})
        state['current']['attempt'] = 3
        self.a.save_state(state)
        before = (attempt / 'config.json').read_bytes()
        command = self.command('retry', {'force': True, 'allow_extra_attempt': True})
        result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'succeeded', result)
        prepared = result['result']['prepared']
        view = protocol.summary(self.a)
        self.assertEqual(prepared['generation'], view['generation'])
        self.assertEqual(prepared['epoch'], view['epoch'])
        self.assertEqual(prepared['attempt'], view['current']['attempt'])
        self.assertEqual(view['desired_state'], 'paused')
        self.assertEqual(self.a.load_state()['current']['attempt'], 4)
        self.assertEqual((attempt / 'config.json').read_bytes(), before)
        self.assertEqual(application.submit_v1(self.a, command), result)
        self.assertEqual(self.a.load_state()['current']['attempt'], 4)

    def test_stale_revision_identity_and_attempt_have_no_effect(self):
        for field in ('revision', 'identity', 'attempt', 'generation'):
            command = self.command()
            command['scope'][field] = 'wrong'
            with patch.object(self.a, 'apply_control') as apply:
                with self.assertRaises(protocol.ContractError):
                    application.submit_v1(self.a, command)
                apply.assert_not_called()
            self.assertFalse((self.a.state_dir / 'recovery-settings.json').exists())
            self.assertFalse(list((self.a.state_dir / 'commands').glob('*.json')))

    def test_delayed_command_cannot_pause_new_attempt_or_change_intent(self):
        with lock(self.a.state_dir / 'runner.lock'):
            command = self.command()
            self.assertEqual(application.submit_v1(self.a, command)['status'], 'pending')
            state = {'history': [], 'current': {'item': 'new'}}
            self.a.save_state(state)
            self.a.process_controls(state)
        result = protocol.operation(self.a, command['request_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'stale_scope')
        self.assertFalse(self.a.load_state().get('paused'))
        self.assertFalse((self.a.state_dir / 'recovery-settings.json').exists())

    def test_generation_change_between_enqueue_and_consume_rejected(self):
        with lock(self.a.state_dir / 'runner.lock'):
            command = self.command()
            application.submit_v1(self.a, command)
            from team_runner import goal_intent
            goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
            self.a.process_controls(self.a.load_state())
        self.assertEqual(protocol.operation(self.a, command['request_id'])['error']['code'], 'stale_scope')

    def test_crashed_running_receipt_is_unknown_never_replayed(self):
        with lock(self.a.state_dir / 'runner.lock'):
            command = self.command()
            application.submit_v1(self.a, command)
            path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
            record = read_json(path); record['status'] = 'running'; atomic_json(path, record)
            with patch.object(self.a, 'apply_control') as apply:
                self.a.process_controls(self.a.load_state())
                self.a.process_controls(self.a.load_state())
                apply.assert_not_called()
        self.assertEqual(protocol.operation(self.a, command['request_id'])['status'], 'unknown')
        self.assertEqual(self.a.load_state()['control_uncertain'], command['request_id'])
        self.assertEqual(application.submit_v1(self.a, command)['status'], 'unknown')

    def test_internal_payload_and_version_rejected_before_persistence(self):
        for patching in ({'payload': {'_target': 'other'}}, {'schema_version': 2},
                         {'action': 'retry', 'payload': {'force': 'yes'}}):
            command = self.command(); command.update(patching)
            with self.assertRaises(protocol.ContractError):
                application.submit_v1(self.a, command)
        self.assertFalse(list((self.a.state_dir / 'commands').glob('*.json')))

    def test_unknown_receipt_preserved_when_reconciliation_has_applied_evidence(self):
        with lock(self.a.state_dir / 'runner.lock'):
            command = self.command()
            application.submit_v1(self.a, command)
            with patch.object(self.a, 'apply_control', side_effect=OSError('lost response')):
                self.a.process_controls(self.a.load_state())
        path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
        before = path.read_bytes()
        state = self.a.load_state(); state['applied_commands'] = [command['request_id']]
        self.a.save_state(state)
        self.a.recover(state)
        self.assertNotIn('control_uncertain', state)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_owner_v1_terminal_receipts_do_not_block_new_commands(self):
        for old_status, projected in (('done', 'succeeded'), ('error', 'failed')):
            with lock(self.a.state_dir / 'runner.lock'):
                command = self.command()
                application.submit_v1(self.a, command)
                path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
                record = read_json(path)
                record.update(status=old_status, error='legacy failure' if old_status == 'error' else None)
                atomic_json(path, record)
                before = path.read_bytes()
            result = protocol.operation(self.a, command['request_id'])
            self.assertEqual(result['status'], projected)
            if old_status == 'error':
                self.assertEqual(result['error']['message'], 'legacy failure')
            self.assertEqual(application.submit_v1(self.a, self.command())['status'], 'succeeded')
            self.assertEqual(path.read_bytes(), before)

    def interrupted_stop(self):
        with lock(self.a.state_dir / 'runner.lock'):
            command = self.command('stop')
            application.submit_v1(self.a, command)
            path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
            record = read_json(path)
            record.update(status='unknown', error='runner interrupted during command')
            atomic_json(path, record)
            state = self.a.load_state()
            state['control_uncertain'] = command['request_id']
            self.a.save_state(state)
        return command, path, state

    def test_reconcile_finishes_exact_interrupted_stop_without_rewriting_receipt(self):
        command, path, state = self.interrupted_stop()
        before = path.read_bytes()
        self.a.recover(state)
        self.assertNotIn('control_uncertain', state)
        self.assertEqual(state['stop_status'], 'stopped')
        self.assertIn(command['request_id'], state['applied_commands'])
        self.assertEqual(path.read_bytes(), before)

    def test_interrupted_stop_uses_durable_post_intent_guard(self):
        from team_runner import goal_intent
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        command = self.command('stop')
        with patch.object(self.a, 'apply_control', side_effect=OSError('interrupted')):
            result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'unknown')
        path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
        record = read_json(path)
        self.assertGreater(record['stop_guard']['epoch'], record['guard']['epoch'])
        before = path.read_bytes()
        state = self.a.load_state()
        self.a.recover(state)
        self.assertNotIn('control_uncertain', state)
        self.assertEqual(state['stop_status'], 'stopped')
        self.assertEqual(path.read_bytes(), before)

    def test_reconcile_does_not_finish_stop_for_another_attempt(self):
        command, path, state = self.interrupted_stop()
        state['current'] = {'item': 'other'}
        self.a.save_state(state)
        with patch.object(self.a, 'apply_control') as apply:
            self.a.recover(state)
            apply.assert_not_called()
        self.assertEqual(state['control_uncertain'], command['request_id'])

    def test_reconcile_keeps_stop_blocked_while_effect_is_unknown(self):
        command, path, state = self.interrupted_stop()
        before = path.read_bytes()
        with patch('team_runner.engine.operations.reconcile', return_value=['effect unknown']), \
                patch.object(self.a, 'apply_control') as apply:
            self.a.recover(state)
            apply.assert_not_called()
        self.assertEqual(state['control_uncertain'], command['request_id'])
        self.assertEqual(path.read_bytes(), before)

    def test_failed_reconcile_preserves_original_unknown_stop(self):
        command, path, state = self.interrupted_stop()
        with patch.object(self.a, 'apply_control', side_effect=RuntimeError('cannot reconcile')):
            result = application.submit_v1(self.a, self.command('reconcile'))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.a.load_state()['control_uncertain'], command['request_id'])

    def test_http_and_cli_submit_same_envelope_returns_same_receipt(self):
        command = self.command()
        server, token = runner_ui.make_server(self.catalog, project_id=self.a.context.project_id)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            ctx = self.a.context
            origin = f'http://127.0.0.1:{server.server_port}'
            url = origin + f'/api/projects/{ctx.project_id}/workspaces/{ctx.workspace_id}/commands'
            request = urllib.request.Request(url, data=json.dumps(command).encode(),
                        headers={'Authorization': 'Bearer ' + token, 'Origin': origin,
                                 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request) as response:
                http_result = json.load(response)
            path = self.root / 'command.json'; path.write_text(json.dumps(command))
            with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(io.StringIO()) as out:
                result = cli.main(['command', '--file', str(path), '--project', ctx.project_id,
                                   '--workspace', ctx.workspace_id])
            self.assertEqual(result, 0)
            self.assertEqual(http_result, json.loads(out.getvalue()))
            self.assertEqual(http_result['status'], 'succeeded')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_unconfirmed_stop_is_unknown_even_when_owner_raises_value_error(self):
        command = self.command('stop')
        def unconfirmed(state, *args):
            state['stop_status'] = 'unknown'
            raise ValueError('stop unconfirmed')
        with patch.object(self.a, 'apply_control', side_effect=unconfirmed):
            result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error']['code'], 'unknown_effect')

    def test_concurrent_identical_request_has_one_application(self):
        from concurrent.futures import ThreadPoolExecutor
        command = self.command()
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait(timeout=5)
            return application.submit_v1(self.a, command)
        with patch.object(self.a, 'apply_control', wraps=self.a.apply_control) as effect:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(effect.call_count, 1)

    def test_foreign_versioned_receipt_is_not_reported_as_local_success(self):
        command = self.command()
        application.submit_v1(self.a, command)
        path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
        foreign = self.b.state_dir / 'commands' / path.name
        atomic_json(foreign, read_json(path))
        before = foreign.read_bytes()
        with self.assertRaises(protocol.ContractError) as error:
            protocol.operation(self.b, command['request_id'])
        self.assertEqual(error.exception.code, 'identity_mismatch')
        self.assertEqual(foreign.read_bytes(), before)

    def test_command_and_operation_fixtures_preserve_versioned_shapes(self):
        from pathlib import Path
        root = Path(__file__).with_name('fixtures') / 'protocol-v1'
        command = json.loads((root / 'pause-command.json').read_text())
        view = protocol.summary(self.a)
        command['scope'].update(identity=view['identity'], revision=view['revision'])
        result = application.submit_v1(self.a, command)
        result['scope'].update(identity='<workspace identity>', revision='<opaque revision>')
        self.assertEqual(result, json.loads((root / 'pause-operation.json').read_text()))

    def test_versioned_settings_do_not_rewrite_current_snapshot(self):
        attempt = fixtures.Platform.attempt(self, self.a)
        snapshot = (attempt / 'config.json').read_bytes()
        for action, payload in [('configure', {'reset': True}), ('recovery', {'enabled': True})]:
            command = self.command(action, payload)
            result = application.submit_v1(self.a, command)
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(application.submit_v1(self.a, command), result)
        self.assertEqual((attempt / 'config.json').read_bytes(), snapshot)
