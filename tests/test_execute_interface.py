"""CLI/HTTP execute parity uses fake manager, service and launch effects only."""
import contextlib
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import Mock, patch

import test_platform as fixtures
from test_goal_execute import ManagerFixture, Services
from team_runner import application, cli, goal_intent, runner_ui
from team_runner.protocol import ContractError


class ExecuteInterface(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def command(self, request, *extra):
        output = io.StringIO()
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(output):
            status = cli.main(['execute', '--project', self.a.context.project_id,
                               '--workspace', self.a.context.workspace_id,
                               '--request-id', request, '--json', *extra])
        return status, json.loads(output.getvalue())

    @contextlib.contextmanager
    def effects(self):
        manager, services = ManagerFixture(), Services()
        with patch('team_runner.service_manager.Manager', return_value=manager), \
             patch('team_runner.goal_execute.ProjectServices', return_value=services), \
             patch('team_runner.service.start_runner', return_value={'status': 'starting', 'nonce': 'fixture'}) as start:
            yield manager, services, start

    @contextlib.contextmanager
    def http(self):
        transport = Mock()
        server, token = runner_ui.make_server(self.catalog, services=transport)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        path = f'/api/projects/{self.a.context.project_id}/workspaces/{self.a.context.workspace_id}/execute'
        def request(data, authenticated=True):
            headers = {'Content-Type': 'application/json', 'Origin': origin}
            if authenticated:
                headers['Authorization'] = 'Bearer ' + token
            req = urllib.request.Request(origin + path, data=json.dumps(data).encode(), headers=headers)
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.code, json.load(response)
        try:
            yield request, transport
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_same_request_cli_http_receipt_and_effect_parity(self):
        identifier = str(uuid.uuid4())
        options = {'goal': application.goals(self.a)[0], 'fast': False}
        with self.effects() as (manager, services, start), self.http() as (request, transport):
            code, first = self.command(identifier, '--goal', options['goal'], '--no-fast')
            self.assertEqual(code, 0)
            http_code, second = request({'request_id': identifier, 'options': options})
            self.assertEqual(http_code, 200)
            self.assertEqual(first, second)
            self.assertEqual(len(manager.requests), 1)
            self.assertEqual(len(services.actions), 1)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(transport.mock_calls, [])
            policy = goal_intent.read(self.a)
            self.assertEqual(policy['options']['mode'], 'auto')
            self.assertTrue(policy['options']['final'])
            self.assertFalse(policy['options']['fast'])
            self.assertEqual(first['result']['status'], 'accepted')

    def test_resume_saved_policy_and_scope(self):
        with self.effects():
            _, first = self.command(str(uuid.uuid4()), '--goal', application.goals(self.a)[0],
                                    '--mode', 'confirm', '--no-final', '--no-recovery')
            paused = goal_intent.change(self.a, desired_state='paused')
            code, resumed = self.command(str(uuid.uuid4()), '--resume', '--generation', paused['generation'],
                                         '--epoch', str(paused['epoch']))
            self.assertEqual(code, 0)
            saved = goal_intent.read(self.a)
            self.assertEqual(saved['options']['mode'], 'confirm')
            self.assertFalse(saved['options']['final'])
            self.assertFalse(saved['policy']['recovery'])
            self.assertEqual(resumed['result']['generation'], first['result']['generation'])

    def test_structural_errors_reject_before_effects_and_match_transports(self):
        identifier = str(uuid.uuid4())
        with self.effects() as (manager, services, start), self.http() as (request, _):
            cli_code, cli_result = self.command(identifier, '--resume', '--goal', 'invalid')
            http_code, http_result = request({'request_id': identifier, 'options': {'resume': True, 'goal': 'invalid'}})
            self.assertEqual((cli_code, http_code), (2, 400))
            self.assertEqual(cli_result, http_result)
            for data in ({'request_id': identifier, 'options': {}, 'extra': 1},
                         {'request_id': identifier, 'options': {'goal': 'x'}, 'epoch': True},
                         {'request_id': identifier, 'options': {'goal': 'x', 'fast': 'yes'}},
                         {'request_id': 'bad', 'options': {'goal': 'x'}}):
                self.assertEqual(request(data)[0], 400)
            self.assertEqual(manager.requests, [])
            self.assertEqual(services.actions, [])
            start.assert_not_called()
            self.assertFalse((self.a.state_dir / 'goal-intent.json').exists())

    def test_child_and_disabled_workspace_denied_before_effect(self):
        data = {'request_id': str(uuid.uuid4()), 'options': {'goal': application.goals(self.a)[0]}}
        with self.effects() as (manager, services, start):
            with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': '/fixture/attempt'}):
                with self.assertRaisesRegex(ContractError, 'Child attempts'):
                    application.execute(self.a, data)
            self.catalog.disable(self.a.context.project_id, self.a.context.workspace_id)
            with self.assertRaises(ValueError):
                application.execute(self.a, data)
            self.assertEqual(manager.requests, [])
            self.assertEqual(services.actions, [])
            start.assert_not_called()

    def test_http_auth_and_same_id_conflict(self):
        identifier = str(uuid.uuid4())
        options = {'goal': application.goals(self.a)[0]}
        with self.effects() as (_, _, start), self.http() as (request, _):
            self.assertEqual(request({'request_id': identifier, 'options': options}, False)[0], 403)
            self.assertEqual(request({'request_id': identifier, 'options': options})[0], 200)
            code, result = request({'request_id': identifier, 'options': {**options, 'final': False}})
            self.assertEqual(code, 400)
            self.assertEqual(result['error']['code'], 'request_conflict')
            self.assertEqual(start.call_count, 1)

    def test_simultaneous_cli_http_execute_shares_single_launch(self):
        from concurrent.futures import ThreadPoolExecutor
        identifier = str(uuid.uuid4())
        goal = application.goals(self.a)[0]
        entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
        with self.effects() as (manager, services, start), self.http() as (request, transport):
            def launch(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('fixture release missing')
                return {'status': 'starting', 'nonce': 'fixture'}
            start.side_effect = launch
            original_execute = runner_ui.execute
            def admit(*args, **kwargs):
                admitted.set()
                return original_execute(*args, **kwargs)
            with patch.object(runner_ui, 'execute', side_effect=admit), ThreadPoolExecutor(max_workers=2) as pool:
                cli_call = pool.submit(self.command, identifier, '--goal', goal)
                self.assertTrue(entered.wait(2))
                http_call = pool.submit(request, {'request_id': identifier, 'options': {'goal': goal}})
                self.assertTrue(admitted.wait(2))
                release.set()
                code, cli_result = cli_call.result(timeout=4)
                http_code, http_result = http_call.result(timeout=4)
            self.assertEqual((code, http_code), (0, 200))
            self.assertEqual(cli_result, http_result)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(len(services.actions), 1)
            self.assertEqual(len(manager.requests), 1)
            self.assertEqual(transport.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
