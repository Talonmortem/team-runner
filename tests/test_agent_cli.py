"""Headless CLI contract; isolated persistence and injected observation only."""
import contextlib
import io
import json
import os
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, cli, protocol
from team_runner.runner_store import atomic_json, lock


class AgentCLI(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_cleanup_defaults_to_preview_in_explicit_workspace(self):
        with patch('team_runner.retention.cleanup', return_value={'apply': False}) as prune:
            code, data = self.command('cleanup')
        self.assertEqual(code, 0)
        self.assertFalse(data['apply'])
        self.assertEqual(prune.call_args.args[0].context.identity(), self.a.context.identity())
        self.assertEqual(prune.call_args.kwargs, {'apply': False, 'days': 7, 'legacy_root': None})

    def command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, 'Catalog', return_value=self.catalog), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main([*arguments, '--project', self.a.context.project_id,
                             '--workspace', self.a.context.workspace_id])
        self.assertEqual(errors.getvalue(), '')
        return code, json.loads(output.getvalue())

    def scope(self, view=None):
        view = view or protocol.summary(self.a)
        return ['--generation', view['generation'] or 'null', '--attempt',
                view['current']['attempt'] or 'null', '--revision', view['revision']]

    def test_json_argument_error_is_structured_without_echoing_values(self):
        code, error = self.command('status', '--json', '--unknown-secret', 'fixture-secret')
        self.assertEqual(code, 2)
        self.assertEqual(error['error']['code'], 'invalid_request')
        self.assertNotIn('fixture-secret', json.dumps(error))

    def test_summary_detail_and_legacy_are_same_readonly_projection(self):
        before = list(self.a.state_dir.rglob('*'))
        code, data = self.command('status', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(data, protocol.summary(self.a))
        self.assertEqual(self.command('status', '--json', '--detail'), (0, application.state_view(self.a)))
        self.assertEqual(self.command('status', '--summary'), (0, data))
        self.assertEqual(list(self.a.state_dir.rglob('*')), before)

    def test_guarded_control_replay_and_stale_scope(self):
        scope = self.scope()
        identifier = str(uuid.uuid4())
        command = ['pause', '--json', '--request-id', identifier, *scope]
        code, first = self.command(*command)
        self.assertEqual(code, 0)
        self.assertEqual(first['status'], 'succeeded')
        self.a.save_state({'history': [], 'current': {'item': 'new'}})
        self.assertEqual(self.command(*command), (0, first))
        code, error = self.command('pause', '--json', '--request-id', str(uuid.uuid4()), *scope)
        self.assertEqual(code, 2)
        self.assertEqual(error['error']['code'], 'stale_revision')
        self.assertFalse(self.a.load_state().get('paused'))

    def test_scope_is_required_without_implicit_refresh(self):
        with patch.object(application, 'submit_v1') as submit:
            code, data = self.command('stop', '--json', '--request-id', str(uuid.uuid4()))
        self.assertEqual(code, 2)
        self.assertEqual(data['error']['code'], 'invalid_scope')
        submit.assert_not_called()

    def test_operation_show_legacy_and_pending_exit_codes(self):
        identifier = str(uuid.uuid4())
        with lock(self.a.state_dir / 'runner.lock'):
            code, receipt = self.command('pause', '--json', '--request-id', identifier, *self.scope())
        self.assertEqual(code, 3)
        self.assertEqual(receipt['status'], 'pending')
        self.assertEqual(self.command('operation', 'show', '--json', '--request-id', identifier), (3, receipt))
        self.assertEqual(self.command('operation', identifier), (0, receipt))
        self.assertEqual(self.command('operation', 'show', '--json')[0], 2)
        self.assertEqual(self.command('operation', identifier, '--request-id', identifier, '--json')[0], 2)
        self.assertEqual(self.command('operation', 'show', '--request-id', identifier, '--timeout', '1', '--json')[0], 2)

    def test_wait_forwards_bounds_and_maps_terminal_states(self):
        identifier = str(uuid.uuid4())
        for state, expected in [('succeeded', 0), ('blocked', 3), ('timeout', 4)]:
            with patch('team_runner.agent_wait.wait', return_value={'wait_status': state}) as wait:
                code, data = self.command('operation', 'wait', '--request-id', identifier,
                                          '--until', 'goal', '--timeout', '0', '--json')
            self.assertEqual(code, expected)
            self.assertEqual(data, {'wait_status': state})
            self.assertEqual(wait.call_args.kwargs, {'until': 'goal', 'timeout': 0})
        with patch('team_runner.agent_wait.wait', return_value={'wait_status': 'timeout'}) as wait:
            self.command('operation', 'wait', '--request-id', identifier, '--json')
        self.assertEqual(wait.call_args.kwargs['timeout'], 30)

    def test_messages_reader_and_invalid_cursor_are_json(self):
        self.a.save_state({'history': [], 'run_id': 'r', 'current': {'item': 'one', 'messages': [
            {'id': 'q', 'run_id': 'r', 'type': 'question', 'body': 'hello'}]}})
        code, data = self.command('messages', '--json', '--limit', '1')
        self.assertEqual(code, 0)
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(self.command('messages', '--json', '--after', 'invalid')[0], 2)
        self.assertEqual(self.command('messages', '--json', '--limit', '1001')[0], 2)

    def test_legacy_reply_run_id_disambiguates_same_message_id(self):
        self.a.save_state({'history': [], 'run_id': 'parent', 'current': {
            'item': 'one', 'messages': [
                {'id': 'q', 'run_id': 'first-run', 'type': 'question', 'body': 'first'},
                {'id': 'q', 'run_id': 'second-run', 'type': 'question', 'body': 'second'}]}})
        with patch('team_runner.engine.Runner.effect') as effect:
            code, rejected = self.command('reply', 'q', 'answer')
            self.assertEqual(code, 1)
            self.assertIn('ambiguous', rejected['error'])
            effect.assert_not_called()
            identifier = str(uuid.uuid4())
            code, receipt = self.command('reply', 'q', 'answer', '--run-id', 'second-run',
                                         '--request-id', identifier)
            self.assertEqual(code, 0)
            self.assertEqual(receipt['data']['run_id'], 'second-run')
            self.assertEqual(receipt['status'], 'done')
            self.assertEqual(effect.call_count, 1)
            arguments = effect.call_args.args
            self.assertEqual(arguments[arguments.index('--run') + 1], 'second-run')
            messages = self.a.load_state()['current']['messages']
            self.assertFalse(messages[0].get('answered'))
            self.assertTrue(messages[1]['answered'])
            self.assertEqual(self.command('reply', 'q', 'answer', '--run-id', 'second-run',
                                          '--request-id', identifier), (0, receipt))
            self.assertEqual(effect.call_count, 1)

    def test_child_cannot_use_execute_or_operation_wait(self):
        attempt = fixtures.Platform.attempt(self, self.a)
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}):
            code, data = self.command('operation', 'wait', '--request-id', str(uuid.uuid4()), '--json')
            self.assertEqual(code, 2)
            self.assertIn('child attempt', data['error']['message'])
            self.assertEqual(self.command('status', '--json')[0], 0)


if __name__ == '__main__':
    unittest.main()
