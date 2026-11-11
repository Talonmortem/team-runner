"""Secret isolation, effort pins, and explicit reserve lifecycle."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from team_runner.runner_cloud import cloud_key, overlay, main, isolated_state
from team_runner.runner_launch import Launcher
from team_runner.runner_store import atomic_json


class CloudLaunch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.key = self.home / 'repo/sber/AI/.env'
        self.key.parent.mkdir(parents=True)
        p = patch.object(Path, 'home', return_value=self.home)
        p.start()
        self.addCleanup(p.stop)

    def test_dotenv_is_data_and_only_cloud_api_is_read(self):
        for text, expected in [('CLOUD_API=plain\n', 'plain'),
                               ('export CLOUD_API="quoted value" # comment\n', 'quoted value'),
                               ("OTHER=$(false)\nCLOUD_API='$(touch /tmp/forbidden)'\n", '$(touch /tmp/forbidden)')]:
            self.key.write_text(text)
            self.assertEqual(cloud_key(), expected)
        for text in ('OTHER=secret', 'CLOUD_API=', 'CLOUD_API=""', 'CLOUD_API="broken'):
            self.key.write_text(text)
            with self.assertRaises(ValueError):
                cloud_key()
        self.key.unlink()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            cloud_key()

    def test_child_exec_only_receives_key_in_environment(self):
        self.key.write_text('CLOUD_API=fixture-secret')
        with patch('team_runner.runner_config.sber_allowed', return_value=True), \
                patch('sys.argv', ['runner_cloud', '--standalone']), patch('os.execvpe') as execute, \
                patch.dict(os.environ, {'OPENCODE_CONFIG_CONTENT': json.dumps(overlay({
                    'model': 'claude-opus-4.8', 'effort': 'medium'})).replace('max_completion_tokens', 'max_tokens'),
                    'XDG_STATE_HOME': str(self.home / 'state')}):
            main()
        command, argv, environment = execute.call_args.args
        self.assertEqual(environment['CLOUD_API'], 'fixture-secret')
        self.assertNotIn('fixture-secret', json.dumps(argv))
        self.assertEqual(command, 'opencode2')
        body = json.loads(environment['OPENCODE_CONFIG_CONTENT'])['agents']['build']['request']['body']
        self.assertNotIn('max_tokens', body)
        self.assertEqual(body['max_completion_tokens'], 24576)

    def test_tui_preferences_are_private_and_pin_effort(self):
        shared = self.home / 'state'
        (shared / 'opencode').mkdir(parents=True)
        old = shared / 'opencode/model.json'
        old.write_text('{"variant":{"cloudru/claude-opus-4.8":"high"}}')
        states = []
        for effort in ('medium', 'high', 'default'):
            env = {'XDG_STATE_HOME': str(shared), 'OPENCODE_CONFIG_CONTENT': json.dumps(overlay({
                'model': 'claude-opus-4.8', 'effort': effort}))}
            isolated_state(env)
            states.append(env['XDG_STATE_HOME'])
            prefs = json.loads((Path(states[-1]) / 'opencode/model.json').read_text())
            self.assertEqual(prefs['variant'], {'cloudru/claude-opus-4.8': effort} if effort != 'default' else {})
        self.assertEqual(len(set(states)), 3)
        self.assertEqual(json.loads(old.read_text())['variant']['cloudru/claude-opus-4.8'], 'high')

    def test_private_tui_state_preserves_child_cli_attempt_namespace(self):
        import subprocess
        from types import SimpleNamespace
        from team_runner.catalog import Catalog
        from team_runner.cli import selection

        catalog = Catalog(self.home / 'config/team-runner', self.home / 'state/team-runner')
        repo = self.home / 'repo/sber/fixture'
        repo.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        project = catalog.project('Fixture')['id']
        workspace = catalog.workspace(project, {'path': str(repo), 'orca_id': 'fixture',
                                                'orca_instance': 'instance'})['id']
        context = catalog.context(project, workspace)
        attempt = context.state_dir / 'attempts/fixture'
        context.snapshot(attempt)
        atomic_json(context.state_dir / 'state.json', {'current': {'attempt_dir': str(attempt)}})
        environment = context.environment(attempt)
        environment['OPENCODE_CONFIG_CONTENT'] = json.dumps(overlay({
            'model': 'claude-opus-4.8', 'effort': 'high'}))
        isolated_state(environment)
        first = Path(environment['XDG_STATE_HOME'])
        self.assertEqual(environment['TEAM_RUNNER_STATE_ROOT'], str(catalog.state_root))
        # A nested OpenCode launch must not nest the runner namespace either.
        isolated_state(environment)
        self.assertEqual(Path(environment['XDG_STATE_HOME']).parent, first.parent)
        with patch.dict(os.environ, environment, clear=True):
            runner = selection(SimpleNamespace(cmd='status', project=None, workspace=None), Catalog())
            self.assertEqual(runner.context.state_dir, context.state_dir)
            self.assertEqual(runner.context.check_attempt(attempt)['identity'], context.identity())
            with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(self.home / 'foreign')}):
                with self.assertRaisesRegex(ValueError, 'outside workspace namespace'):
                    selection(SimpleNamespace(cmd='status', project=None, workspace=None), Catalog())

    def test_model_variant_pins_and_thinking(self):
        for model, effort in [('claude-opus-4.8', 'high'), ('claude-sonnet-4.6', 'high'),
                             ('gpt-5.5', 'xhigh'), ('deepseek-v4.1-flash', 'high'), ('glm-5.2', 'high')] + [(model, 'medium') for model in
                                 ('claude-opus-4.8', 'claude-sonnet-4.6', 'gpt-5.5',
                                  'deepseek-v4.1-flash', 'glm-5.2')]:
            content = overlay({'model': model, 'effort': effort})
            self.assertEqual(content['agents']['build']['model'], 'cloudru/' + model + '#' + effort)
            self.assertEqual(content['providers']['cloudru']['models'][model]['limit']['output'], 24576)
            variant = content['providers']['cloudru']['models'][model]['variants'][0]
            self.assertEqual(variant['settings']['reasoningEffort'], effort)
            if model.startswith(('deepseek', 'glm')):
                self.assertEqual(variant['body']['thinking']['type'], 'enabled')
        for effort in ('low', 'invalid'):
            with self.assertRaises(ValueError):
                overlay({'model': 'gpt-5.5', 'effort': effort})
        with self.assertRaises(ValueError):
            overlay({'model': 'glm-5.2', 'effort': 'xhigh'})

    def test_reserve_requires_reason_fresh_session_and_prior_completion(self):
        launcher = Launcher(None)
        for reason, reuse in [(None, None), ('', None), ('why', 'prior')]:
            with self.assertRaises(ValueError):
                launcher.validate_variant('reserve', reason, reuse)
        launcher.validate_variant('reserve', 'why', None)
        previous = {'role': 'implementer_standard', 'status': 'active', 'dispatch_id': 'd'}
        with patch.object(launcher, 'observe_agent', return_value={'dispatch': {'status': 'dispatched'}}):
            with self.assertRaisesRegex(RuntimeError, 'complete or stop'):
                launcher.reserve_guard({'agents': [previous]}, 'implementer_medium', None)
        with patch.object(launcher, 'observe_agent', return_value={'dispatch': {'status': 'completed'}}):
            launcher.reserve_guard({'agents': [previous]}, 'implementer_medium', None)

    def test_connection_stamp_changes_with_key_without_exposing_it(self):
        from types import SimpleNamespace
        launcher = Launcher(SimpleNamespace(config_root=self.home / '.config/team-runner', repo=self.key.parent))
        cfg = {'agent': 'opencode2', 'profile': ''}
        self.key.write_text('CLOUD_API=first-secret')
        first = launcher.connection_stamp(cfg)
        self.key.write_text('CLOUD_API=second-secret')
        second = launcher.connection_stamp(cfg)
        self.assertNotEqual(first, second)
        self.assertNotIn('secret', first + second)

    def test_unknown_operation_blocks_launch(self):
        launcher = Launcher(None)
        atomic_json(self.home / 'operations.json', {'pending': {'status': 'unknown'}})
        with self.assertRaisesRegex(RuntimeError, 'reconcile'):
            launcher.check_operations(self.home)


if __name__ == '__main__':
    unittest.main()
