"""Offline Cloud.ru discovery and canonical checkout policy."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from team_runner import runner_config as config
from team_runner.catalog import Catalog


class OpenCodeConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.allowed = self.home / 'repo/sber/app'
        self.allowed.mkdir(parents=True)
        self.outside = self.home / 'repo/other'
        self.outside.mkdir(parents=True)
        self.config_root = self.home / '.config'
        source = self.config_root / 'opencode/opencode.json'
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'model': 'cloudru/org/model', 'providers': {
            'cloudru': {'name': 'secret-name', 'settings': {'apiKey': 'secret-key'},
                        'models': {'org/model': {'name': 'secret-display'}, 'model-two': {}}}}}))
        self.codex = self.home / '.codex'
        self.codex.mkdir()
        (self.codex / 'config.toml').write_text('[model_providers.codex-lb]\n[model_providers.deepseek]\n[model_providers.cloudru]\nbase_url="secret-url"\n')
        self.mock_catalog = {'profiles': {}, 'base_providers': ['cloudru', 'codex-lb', 'deepseek'],
                             'opencode2': {'models': ['org/model', 'model-two']}}
        self.role = {'agent': 'opencode2', 'profile': '', 'provider': 'cloudru',
                     'model': 'org/model', 'effort': 'default'}
        self.home_patch = patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def configuration(self):
        cfg = config.defaults()
        cfg['roles']['tester'] = dict(self.role)
        return cfg

    def test_discovery_is_secret_free_and_scoped(self):
        inside = config.connections(self.codex, self.allowed, self.config_root)
        self.assertIn('opencode2', inside['runtimes'])
        self.assertEqual(inside['opencode2']['default_model'], 'org/model')
        self.assertEqual(inside['opencode2']['models'], ['model-two', 'org/model'])
        self.assertNotIn('secret', json.dumps(inside))
        for repo in (None, self.outside, self.home / 'repo/sber/missing'):
            outside = config.connections(self.codex, repo, self.config_root)
            self.assertNotIn('opencode2', outside['runtimes'])
            self.assertNotIn('cloudru', outside['base_providers'])

    def test_canonical_scope_cannot_be_overridden_by_catalog(self):
        config.validate(self.configuration(), self.mock_catalog, self.allowed)
        link = self.allowed / 'escape'
        link.symlink_to(self.outside, target_is_directory=True)
        for repo in (None, self.outside, link):
            with self.assertRaisesRegex(ValueError, 'repo/sber'):
                config.validate(self.configuration(), self.mock_catalog, repo)
        cfg = config.defaults()
        cfg['roles']['tester'].update(agent='codex', provider='cloudru')
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            config.validate(cfg, self.mock_catalog, self.outside)
        cfg['roles']['tester']['provider'] = ''
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            config.validate(cfg, {**self.mock_catalog, 'current_provider': 'cloudru'}, self.outside)

    def test_local_codex_inherited_cloudru_is_denied_after_public_filtering(self):
        (self.codex / 'config.toml').write_text('model_provider="cloudru"\n[profiles.sber]\nmodel_provider="cloudru"\n')
        with patch.dict('os.environ', {'CODEX_HOME': str(self.codex)}):
            public = config.connections(repo=self.outside)
            self.assertIsNone(public['current_provider'])
            self.assertNotIn('sber', public['profiles'])
            for profile in ('', 'sber'):
                with self.assertRaisesRegex(ValueError, 'repo/sber'):
                    config.validate_role_scope({'agent': 'codex', 'provider': '', 'profile': profile}, self.outside)

    def test_runtime_fields_and_model_are_checked(self):
        for field, value in [('effort', 'invalid'), ('profile', 'named'), ('provider', 'current'), ('model', 'missing')]:
            cfg = self.configuration()
            cfg['roles']['tester'][field] = value
            with self.assertRaises(ValueError):
                config.validate(cfg, self.mock_catalog, self.allowed)
        cfg = config.load_config(self.home / 'state', overrides={'agent': 'opencode2'},
                                 catalog=self.mock_catalog, repo=self.allowed, config_root=self.config_root)
        self.assertEqual(cfg['roles']['coordinator'], {**self.role, 'effort': 'high'})

    def test_project_and_attached_workspaces_enforce_inheritance(self):
        for repo in (self.allowed, self.outside):
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        catalog = Catalog(self.config_root / 'team-runner', self.home / 'state')
        project = catalog.project('Fixture')
        values = {'roles': {'tester': self.role}}
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            catalog.project(project_id=project['id'], values=values)
        ws = catalog.workspace(project['id'], {'path': str(self.allowed), 'orca_id': 'inside', 'orca_instance': 'instance'})
        catalog.project(project_id=project['id'], values=values)
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            catalog.workspace(project['id'], {'path': str(self.outside), 'orca_id': 'outside', 'orca_instance': 'instance'})
        self.assertEqual(len(catalog.read()['projects'][project['id']]['workspaces']), 1)
        catalog.workspace(project['id'], values, ws['id'])
        mixed = catalog.project('Mixed')
        catalog.workspace(mixed['id'], {'path': str(self.outside), 'orca_id': 'other', 'orca_instance': 'instance'})
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            catalog.project(project_id=mixed['id'], values=values)


if __name__ == '__main__':
    unittest.main()
