"""Recovery controls stay separate from model templates and attempt snapshots."""
import contextlib
import io
import json
import threading
from unittest.mock import patch
import urllib.error
import urllib.request

import test_template_ui_cli as fixtures
from test_goal_runner import Fixture, runner, store, ui
from team_runner import cli
from team_runner import __version__


class RecoveryHTTP(Fixture):
    def setUp(self):
        super().setUp()
        store.atomic_json(runner.repo / 'feat/goal-fixture/Tasks.json', {'kind': 'goal', 'queue': []})
        self.server, self.token = ui.make_server(self.catalog, project_id=runner.context.project_id,
                                               instance={'token': 'fixture-token'})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close_server)

    close_server = fixtures.TemplateHTTP.close_server
    request = fixtures.TemplateHTTP.request

    def test_health_reports_running_package_version(self):
        request = urllib.request.Request(self.url + '/api/health',
                                         headers={'Authorization': 'Bearer ' + self.token})
        with urllib.request.urlopen(request) as response:
            value = json.load(response)
        self.assertEqual(value['version'], __version__)
        self.assertEqual(value['state'], 'running')

    def test_toggle_is_independent_of_templates_and_snapshots(self):
        attempt, _ = self.attempt()
        snapshot = (attempt / 'config.json').read_bytes()
        self.assertFalse(self.request('/api/recovery')['enabled'])
        self.assertTrue(self.request('/api/recovery', {'enabled': True})['enabled'])
        cfg = self.request('/api/config')['templates'][2]['config']
        self.request('/api/config', cfg)
        self.request('/api/config', {'reset': True})
        self.assertTrue(self.request('/api/state')['recovery']['enabled'])
        self.assertEqual((attempt / 'config.json').read_bytes(), snapshot)
        self.assertFalse(self.request('/api/recovery', {'enabled': False})['enabled'])

    def test_toggle_validates_arguments_and_does_not_touch_orca(self):
        with patch.object(runner, 'orca') as call:
            for data in ({}, {'enabled': 1}, {'enabled': 'yes'}, {'enabled': True, 'resume': True}):
                with self.assertRaises(urllib.error.HTTPError):
                    self.request('/api/recovery', data)
            self.request('/api/recovery', {'enabled': True})
            self.request('/api/state')
        call.assert_not_called()

    def test_inactive_reply_is_history_not_current_operation(self):
        attempt, _ = self.attempt()
        op = {'status': 'unknown', 'args': ['orchestration', 'reply'],
              'failure_receipt': {'ok': False, 'error': {'code': 'dispatch_inactive'}}}
        store.atomic_json(runner.launcher.journal(attempt), {'reply:q': op})
        view = self.request('/api/state')
        self.assertEqual(view['operations'], [])
        self.assertEqual(view['operation_history'][0]['key'], 'reply:q')


class RecoveryCLI(Fixture):
    def command(self, action):
        out = io.StringIO()
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(out):
            status = cli.main(['recovery', action, '--project', runner.context.project_id,
                               '--workspace', runner.context.workspace_id])
        self.assertEqual(status, 0)
        return json.loads(out.getvalue())

    def test_status_on_off_preserve_snapshot(self):
        attempt, _ = self.attempt()
        before = (attempt / 'config.json').read_bytes()
        self.assertFalse(self.command('status')['enabled'])
        self.assertTrue(self.command('on')['enabled'])
        self.assertTrue(self.command('status')['enabled'])
        self.assertFalse(self.command('off')['enabled'])
        self.assertEqual((attempt / 'config.json').read_bytes(), before)
