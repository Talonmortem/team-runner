"""Template controls remain scoped and do not rewrite active snapshots."""
import contextlib
import io
import json
import threading
from unittest.mock import patch
import urllib.error

import test_goal_runner as fixtures
from test_goal_runner import Fixture, config, runner, store, ui
from team_runner import cli


class TemplateHTTP(Fixture):
    def setUp(self):
        super().setUp()
        store.atomic_json(runner.repo / 'feat/goal-fixture/Tasks.json', {'kind': 'goal', 'queue': []})
        self.server, self.token = ui.make_server(self.catalog, project_id=runner.context.project_id)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close_server)

    close_server = fixtures.HTTP.close_server
    request = fixtures.HTTP.request

    def test_auto_replan_save_reload_and_snapshot_isolation(self):
        attempt, _ = self.attempt()
        before = (attempt / 'config.json').read_bytes()
        cfg = self.request('/api/config')['config']
        self.assertFalse(cfg['auto_continue_after_replan'])
        for enabled in (True, False):
            cfg['auto_continue_after_replan'] = enabled
            saved = self.request('/api/config', cfg)
            self.assertIs(saved['auto_continue_after_replan'], enabled)
            self.assertIs(self.request('/api/config')['config']['auto_continue_after_replan'], enabled)
            self.assertEqual((attempt / 'config.json').read_bytes(), before)
        cfg['auto_continue_after_replan'] = 'true'
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/config', cfg)
        self.assertFalse(self.request('/api/config')['config']['auto_continue_after_replan'])

    def test_templates_are_drafts_and_v1_save_preserves_reserves(self):
        attempt, _ = self.attempt()
        before = (attempt / 'config.json').read_bytes()
        data = self.request('/api/config')
        self.assertEqual([t['id'] for t in data['templates']], ['current', 'cloudru', 'openai-only'])
        self.assertFalse(data['templates'][1]['available'])
        self.assertFalse((runner.context.settings_dir / 'config.json').exists())
        self.assertTrue(data['templates'][2]['available'])
        cfg = data['templates'][2]['config']
        cfg['reserves']['tester'] = dict(cfg['roles']['tester'])
        cfg['reserves']['tester']['model'] = 'reserve-model'
        self.request('/api/config', cfg)
        legacy = {'version': 1, 'roles': cfg['roles']}
        legacy['roles']['tester']['model'] = 'changed-primary'
        saved = self.request('/api/config', legacy)
        self.assertEqual(saved['reserves']['tester']['model'], 'reserve-model')
        self.assertEqual((attempt / 'config.json').read_bytes(), before)
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/config', data['templates'][1]['config'])


class TemplateCLI(Fixture):
    def command(self, *args):
        out = io.StringIO()
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(out):
            status = cli.main(['template', *args, '--project', runner.context.project_id,
                               '--workspace', runner.context.workspace_id])
        self.assertEqual(status, 0)
        return json.loads(out.getvalue())

    def test_list_show_apply_and_snapshot_isolation(self):
        attempt, _ = self.attempt()
        before = (attempt / 'config.json').read_bytes()
        self.assertEqual(len(self.command('list')), 3)
        shown = self.command('show', 'openai-only')
        self.assertFalse((runner.context.settings_dir / 'config.json').exists())
        self.assertEqual(self.command('apply', 'openai-only'), shown)
        self.assertEqual(store.read_json(runner.context.settings_dir / 'config.json'), shown)
        self.assertEqual((attempt / 'config.json').read_bytes(), before)

    def test_explicit_workspace_required_and_reserve_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(['template', 'list'])
        args = cli.parser().parse_args(['launch', 'tester', '--spec', 's', '--title', 't',
                                       '--variant', 'reserve', '--reason', 'primary stopped'])
        self.assertEqual((args.variant, args.reason), ('reserve', 'primary stopped'))
