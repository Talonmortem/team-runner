"""OpenCode 2 / Cloud.ru boundaries; isolated fixtures, no model calls."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from team_runner import runner_config as config, runner_ui
from team_runner.catalog import Catalog
from team_runner.engine import Runner
from team_runner.runner_store import atomic_json


class OpenCode2(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / 'home'
        self.home.mkdir()
        key = self.home / 'repo/sber/AI/.env'
        key.parent.mkdir(parents=True)
        key.write_text('CLOUD_API=isolated-fixture-key')
        codex = self.home / '.codex'
        codex.mkdir()
        (codex / 'config.toml').write_text('[model_providers.codex-lb]\n[model_providers.deepseek]\n')
        self.xdg = self.home / '.config'
        self.connection = self.xdg / 'opencode/opencode.json'
        atomic_json(self.connection, {'model': 'cloudru/model-b', 'providers': {'cloudru': {
            'settings': {'baseURL': 'private-endpoint', 'apiKey': 'private-token'},
            'env': ['PRIVATE_TOKEN'], 'models': {'model-a': {}, 'model-b': {}}}}})
        for p in (patch.object(Path, 'home', return_value=self.home),
                  patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.xdg),
                                          'CODEX_HOME': str(self.home / '.codex'),
                                          'XDG_DATA_HOME': str(self.home / '.local/share')}, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.catalog = Catalog(self.xdg / 'team-runner', self.home / '.local/state/team-runner')
        self.project = self.catalog.project('OpenCode fixture')['id']
        self.inside = self.home / 'repo/sber/project'
        self.outside = self.home / 'repo/other'
        self.contexts = [self.attach(p) for p in (self.inside, self.outside)]
        self.ctx = self.contexts[0]
        self.runner = Runner(self.ctx)
        self.launcher = self.runner.launcher
        self.role = {'agent': 'opencode2', 'provider': 'cloudru', 'profile': '',
                     'model': 'model-b', 'effort': 'default'}

    def attach(self, repo):
        repo.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        ws = self.catalog.workspace(self.project, {'path': str(repo), 'orca_id': repo.name,
                                                    'orca_instance': 'fixture'})
        return self.catalog.context(self.project, ws['id'])

    def configured(self, role='coordinator'):
        result = config.defaults()
        result['roles'][role] = dict(self.role)
        return result

    def attempt(self, ctx=None):
        ctx = ctx or self.ctx
        attempt = ctx.state_dir / 'attempts/test'
        ctx.snapshot(attempt)
        atomic_json(attempt / 'config.json', self.configured('tester'))
        atomic_json(ctx.state_dir / 'state.json', {'current': {'attempt_dir': str(attempt)}})
        return attempt

    def test_scope_resolves_paths_and_symlink_escape(self):
        lookalike = self.home / 'repo/sber-other'
        lookalike.mkdir()
        escape = self.home / 'repo/sber/escape'
        escape.symlink_to(self.outside, target_is_directory=True)
        self.assertTrue(config.sber_allowed(self.inside))
        for repo in (None, self.outside, lookalike, escape, self.inside / 'missing'):
            with self.subTest(repo=repo):
                self.assertFalse(config.sber_allowed(repo))
                with self.assertRaisesRegex(ValueError, '~/repo/sber'):
                    config.validate_role_scope(self.role, repo)

    def test_catalog_scoped_and_secret_free(self):
        allowed = config.connections(repo=self.inside)
        self.assertIn('opencode2', allowed['runtimes'])
        self.assertEqual(allowed['opencode2']['models'], ['model-a', 'model-b'])
        self.assertEqual(allowed['opencode2']['default_model'], 'model-b')
        outside = config.connections(repo=self.outside)
        self.assertNotIn('opencode2', outside['runtimes'])
        self.assertNotIn('opencode2', outside)
        for secret in ('private-token', 'private-endpoint', 'PRIVATE_TOKEN'):
            self.assertNotIn(secret, json.dumps(allowed))

    def test_cli_switch_defaults_and_explicit_model(self):
        for overrides, expected in (({'agent': 'opencode2'}, 'model-b'),
                                    ({'agent': 'opencode2', 'model': 'model-a'}, 'model-a')):
            result = config.load_config(self.ctx.settings_dir, overrides=overrides, repo=self.inside)
            self.assertEqual(result['roles']['coordinator'], {**self.role, 'model': expected, 'effort': 'high'})
            self.assertEqual(result['roles']['tester'], config.defaults()['roles']['tester'])
        with self.assertRaises(ValueError):
            config.load_config(self.ctx.settings_dir, overrides={'agent': 'opencode2'}, repo=self.outside)

    def test_invalid_runtime_settings_rejected(self):
        for field, value in (('effort', 'invalid'), ('profile', 'named'), ('provider', 'openai'),
                             ('model', 'unregistered'), ('model', 'cloudru/model-b')):
            cfg = self.configured()
            cfg['roles']['coordinator'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                config.validate(cfg, repo=self.inside)

    def test_launcher_pins_model_private_server_and_no_subagents(self):
        argv = self.launcher.argv(self.role, self.ctx.state_dir)
        self.assertEqual(argv[0], 'env')
        self.assertIn('team_runner.runner_cloud', argv)
        self.assertIn('--standalone', argv)
        self.assertNotIn('--model', argv)
        self.assertNotIn('--effort', argv)
        overlay = json.loads(next(v.split('=', 1)[1] for v in argv if v.startswith('OPENCODE_CONFIG_CONTENT=')))
        self.assertEqual(overlay['model'], 'cloudru/model-b')
        self.assertEqual(overlay['default_agent'], 'build')
        self.assertEqual(overlay['agents']['build']['model'], 'cloudru/model-b')
        self.assertEqual(overlay['experimental']['subagent_depth'], 0)

    def test_outside_dispatch_rejected_before_effects(self):
        ctx = self.contexts[1]
        launcher = Runner(ctx).launcher
        attempt = self.attempt(ctx)
        with patch.object(launcher, 'child_run') as child, patch.object(launcher, 'effect') as effect, \
                patch.object(launcher, 'orca') as orca:
            with self.assertRaisesRegex(ValueError, '~/repo/sber'):
                launcher.dispatch_role(attempt, 'tester', 'spec', 'title', ctx.state_dir)
            child.assert_not_called()
            effect.assert_not_called()
            orca.assert_not_called()
        self.assertFalse((attempt / 'agents.json').exists())

    def test_launch_uses_frozen_snapshot_after_settings_change(self):
        attempt = self.attempt()
        before = (attempt / 'config.json').read_bytes()
        changed = self.configured('tester')
        changed['roles']['tester']['model'] = 'model-a'
        atomic_json(self.ctx.settings_dir / 'config.json', changed)
        with patch.object(self.launcher, '_launch', side_effect=lambda a, r, rec, *args: rec):
            record = self.launcher.launch(attempt, 'tester', 'task', 'run', 'title', self.ctx.state_dir)
        self.assertEqual(record['requested'], self.role)
        self.assertEqual((attempt / 'config.json').read_bytes(), before)
        self.assertEqual(config.load_config(self.ctx.settings_dir, repo=self.inside)['roles']['tester']['model'], 'model-a')

    def test_connection_stamp_tracks_config_auth_project_and_environment(self):
        previous = self.launcher.connection_stamp(self.role)
        paths = [self.connection, self.home / '.local/share/opencode/auth.json',
                 self.inside / 'opencode.json']
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"changed":"private-token"}')
            current = self.launcher.connection_stamp(self.role)
            self.assertNotEqual(current, previous)
            self.assertNotIn('private-token', current)
            previous = current
        with patch.dict(os.environ, {'CLOUDRU_API_KEY': 'another-private-token'}):
            self.assertNotEqual(self.launcher.connection_stamp(self.role), previous)

    def test_connection_stamp_tracks_arbitrary_provider_environment(self):
        previous = self.launcher.connection_stamp(self.role)
        with patch.dict(os.environ, {'PRIVATE_TOKEN': 'first-secret'}):
            first = self.launcher.connection_stamp(self.role)
            self.assertNotEqual(first, previous)
            with patch.dict(os.environ, {'PRIVATE_TOKEN': 'second-secret'}):
                self.assertNotEqual(self.launcher.connection_stamp(self.role), first)
        (self.inside / 'opencode.jsonc').write_text('// local provider\n{"key":"{env:CUSTOM_KEY}"}')
        previous = self.launcher.connection_stamp(self.role)
        with patch.dict(os.environ, {'CUSTOM_KEY': 'third-secret'}):
            self.assertNotEqual(self.launcher.connection_stamp(self.role), previous)

    def test_workspace_and_project_inheritance_cannot_escape_scope(self):
        with self.assertRaises(ValueError):
            self.catalog.workspace(self.project, {'roles': {'tester': self.role}}, self.contexts[1].workspace_id)
        with self.assertRaises(ValueError):
            self.catalog.project(project_id=self.project, values={'roles': {'tester': self.role}})
        only = self.catalog.project('Allowed only')['id']
        ws = self.catalog.workspace(only, {'path': str(self.inside), 'orca_id': 'inside', 'orca_instance': 'fixture'})
        self.catalog.project(project_id=only, values={'roles': {'tester': self.role}})
        with self.assertRaises(ValueError):
            self.catalog.workspace(only, {'path': str(self.outside), 'orca_id': 'outside', 'orca_instance': 'fixture'})
        ctx = self.catalog.context(only, ws['id'])
        loaded = config.load_config(ctx.settings_dir, layers=ctx.role_layers, repo=ctx.repo)
        self.assertEqual(loaded['roles']['tester'], self.role)

    def test_http_get_and_post_follow_selected_workspace(self):
        server, token = runner_ui.make_server(self.catalog, project_id=self.project)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        def request(ctx, data=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                path = f'/api/projects/{ctx.project_id}/workspaces/{ctx.workspace_id}/config'
                conn.request('POST' if data is not None else 'GET', path,
                             json.dumps(data) if data is not None else None,
                             {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                              'Origin': f'http://127.0.0.1:{server.server_port}'})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        for ctx, allowed in zip(self.contexts, (True, False)):
            status, body = request(ctx)
            self.assertEqual(status, 200)
            self.assertEqual('opencode2' in body['connections']['runtimes'], allowed)
            status, body = request(ctx, self.configured())
            self.assertEqual(status, 200 if allowed else 400)
        self.assertFalse((self.contexts[1].settings_dir / 'config.json').exists())
        status, body = request(self.ctx)
        self.assertEqual(status, 200)
        self.assertEqual(body['config']['roles']['coordinator'], self.role)


if __name__ == '__main__':
    unittest.main()
