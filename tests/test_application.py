"""Shared command path preserves legacy receipts across CLI/HTTP retries."""
import contextlib
import io
import json
import uuid
import unittest
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, cli, runner_ui


class Application(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_queries_are_transport_independent(self):
        self.assertIs(runner_ui.goals, application.goals)
        self.assertIs(runner_ui.state_view, application.state_view)
        with patch.object(self.a, 'orca') as orca:
            before = self.a.state_dir.exists()
            application.goals(self.a)
            application.state_view(self.a)
            self.assertEqual(self.a.state_dir.exists(), before)
        orca.assert_not_called()

    def test_cli_run_and_http_dispatch_share_request_receipt(self):
        request = str(uuid.uuid4())
        data = {'goal': 'feat/goal-fixture', 'mode': 'confirm', 'final': False}
        with patch.object(cli, 'Catalog', return_value=self.catalog), \
                patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = cli.main(['run', '--project', self.a.context.project_id,
                               '--workspace', self.a.context.workspace_id,
                               '--goal', data['goal'], '--request-id', request])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(out.getvalue()), {'status': 'starting'})
            record = application.submit_command(self.a, 'start', data, request)
            self.assertEqual(record['status'], 'done')
            self.assertEqual(start.call_count, 1)
            with self.assertRaisesRegex(ValueError, 'another command'):
                application.submit_command(self.a, 'start', {**data, 'final': True}, request)
            self.assertEqual(start.call_count, 1)

    def test_cli_control_passes_request_id(self):
        request = str(uuid.uuid4())
        with patch.object(cli, 'Catalog', return_value=self.catalog), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = cli.main(['pause', '--project', self.a.context.project_id,
                               '--workspace', self.a.context.workspace_id, '--request-id', request])
        self.assertEqual(result, 0)
        record = json.loads(out.getvalue())
        self.assertEqual(record['id'], request)
        self.assertEqual(application.submit_command(self.a, 'pause', {}, request), record)

    def test_applying_receipt_never_replays_start(self):
        from team_runner.runner_store import atomic_json, enqueue
        request = str(uuid.uuid4())
        record = enqueue(self.a.state_dir / 'ui', 'start', {}, request)
        record['status'] = 'applying'
        atomic_json(self.a.state_dir / 'ui/commands' / (request + '.json'), record)
        with patch('team_runner.service.start_runner') as start:
            self.assertEqual(application.submit_command(self.a, 'start', {}, request), record)
        start.assert_not_called()

    def test_invalid_start_is_not_hidden_by_running_owner(self):
        from team_runner.service import start_runner
        from team_runner.runner_store import lock
        goal = application.goals(self.a)[0]
        self.a.save_state({'history': [], 'goal': goal})
        with lock(self.a.state_dir / 'runner.lock'), patch.object(self.a, 'orca') as effect:
            with self.assertRaisesRegex(ValueError, 'final must be boolean'):
                start_runner(self.a, {'goal': goal, 'final': 'yes'})
            with self.assertRaisesRegex(ValueError, 'existing goal'):
                start_runner(self.a, {'goal': 'feat/missing'})
            effect.assert_not_called()
        self.assertFalse((self.a.state_dir / 'launch.json').exists())

    def test_configuration_repeated_id_preserves_original_result(self):
        request = str(uuid.uuid4())
        record = application.submit_command(self.a, 'configure', {'reset': True}, request)
        self.assertEqual(record['status'], 'done')
        path = application.config_path(self.a)
        before = path.read_bytes()
        self.assertEqual(application.submit_command(self.a, 'configure', {'reset': True}, request), record)
        self.assertEqual(before, path.read_bytes())
        with self.assertRaises(ValueError):
            application.submit_command(self.a, 'configure', {'reset': False}, request)

    def test_unsupported_intent_refuses_legacy_mutations(self):
        from team_runner.runner_store import atomic_json
        atomic_json(self.a.state_dir / 'goal-intent.json', {'schema_version': 99})
        with patch.object(self.a, 'apply_control') as effect:
            with self.assertRaises(application.ContractError):
                application.submit_command(self.a, 'pause', {}, str(uuid.uuid4()))
            effect.assert_not_called()
        self.assertFalse((self.a.state_dir / 'recovery-settings.json').exists())

    def test_request_id_cannot_cross_legacy_journals(self):
        identifier = str(uuid.uuid4())
        application.submit_command(self.a, 'pause', {}, identifier)
        with self.assertRaises(application.ContractError) as error:
            application.submit_command(self.a, 'configure', {'reset': True}, identifier)
        self.assertEqual(error.exception.code, 'request_conflict')
        self.assertFalse(application.config_path(self.a).exists())

    def test_default_cli_status_keeps_legacy_projection(self):
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(['status', '--project', self.a.context.project_id,
                             '--workspace', self.a.context.workspace_id])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), application.legacy_status_view(self.a))
