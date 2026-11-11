"""Optional fast requests survive configuration layers and immutable snapshots."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_runner import runner_config as config
from team_runner.runner_launch import Launcher


class FastConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = {'profiles': {}, 'base_providers': ['codex-lb', 'openai'],
                        'fast_models': {'codex': ['gpt-6.1-sol'],
                                        'claude': ['claude-opus-5-5'], 'opencode2': []}}
        self.launcher = Launcher(SimpleNamespace(repo=self.root, config_root=self.root))

    def codex(self, fast=None):
        result = dict(agent='codex', profile='', provider='openai', model='gpt-6.1-sol', effort='high')
        if fast is not None:
            result['fast'] = fast
        return result

    def test_metadata_is_read_only_and_filters_unadvertised_models(self):
        data = {'identity': 'SECRET', 'models': [
            {'slug': 'yes', 'service_tiers': [{'id': 'priority'}], 'token': 'SECRET'},
            {'slug': 'no'}, {'slug': 'bad\nname', 'service_tiers': [{'id': 'priority'}]}, None]}
        path = self.root / 'models_cache.json'
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        result = config.connections(home=self.root)
        self.assertEqual(result['fast_models']['codex'], ['yes'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(before, path.read_bytes())
        path.write_text('broken')
        self.assertEqual(config.connections(home=self.root)['fast_models']['codex'], [])

    def test_optional_booleans_and_reserves_roundtrip(self):
        value = config.defaults()
        original = copy.deepcopy(value)
        self.assertEqual(config.validate(value, self.catalog), original)
        self.assertNotIn('fast', value['roles']['coordinator'])
        value['roles']['coordinator'] = self.codex(True)
        value['reserves']['coordinator'] = self.codex(False)
        self.assertEqual(config.validate(value, self.catalog), value)
        (self.root / 'config.json').write_text(json.dumps(value))
        self.assertEqual(config.load_config(self.root, catalog=self.catalog), value)
        self.assertIs(config.snapshot_role(self.root, 'coordinator')['fast'], True)
        self.assertIs(config.snapshot_role(self.root, 'coordinator', 'reserve')['fast'], False)
        overridden = config.load_config(self.root, overrides={'fast': False}, catalog=self.catalog)
        self.assertIs(overridden['roles']['coordinator']['fast'], False)
        self.assertIs(config.snapshot_role(self.root, 'coordinator')['fast'], True)

    def test_invalid_type_model_runtime_and_provider_fail_closed(self):
        for invalid in (0, 1, 'true', None, [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'boolean'):
                config.validate_fast(dict(self.codex(), fast=invalid), self.catalog)
        for value in (dict(self.codex(True), model='unknown'),
                      dict(self.codex(False), provider='unknown-provider'),
                      dict(self.codex(True), agent='opencode2')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'unsupported'):
                config.validate_fast(value, self.catalog)

    def test_argv_on_off_and_omitted_are_distinct(self):
        with patch.object(config, 'connections', return_value=self.catalog):
            for enabled, tier in ((True, 'priority'), (False, 'default')):
                argv = self.launcher.argv(self.codex(enabled), self.root)
                self.assertIn('service_tier=' + json.dumps(tier), argv)
                claude = dict(agent='claude', profile='', provider='current', model='claude-opus-5-5', effort='high', fast=enabled)
                argv = self.launcher.argv(claude, self.root)
                self.assertEqual(argv[-2:], ['--fast-mode', 'on' if enabled else 'off'])
            self.assertFalse(any('service_tier' in arg for arg in self.launcher.argv(self.codex(), self.root)))
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                self.launcher.argv(dict(self.codex(True), model='unknown'), self.root)

    def test_model_change_clears_only_inherited_incompatible_fast(self):
        value = config.defaults()
        value['roles']['coordinator'] = self.codex(True)
        (self.root / 'config.json').write_text(json.dumps(value))
        result = config.load_config(self.root, overrides={'model': 'unknown'}, catalog=self.catalog)
        self.assertNotIn('fast', result['roles']['coordinator'])
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            config.load_config(self.root, overrides={'model': 'unknown', 'fast': True}, catalog=self.catalog)

    def test_layers_and_projection_preserve_false(self):
        with patch.object(config, 'connections', return_value=self.catalog):
            result = config.projections(self.root, {'options': {'fast': False}}, {},
                                        layers=({'coordinator': self.codex(True)},))
        self.assertIs(result['coordinator']['saved']['fast'], True)
        self.assertIs(result['coordinator']['next']['fast'], False)
        self.assertEqual(result['coordinator']['sources']['fast'], 'CLI --fast')
        self.assertIsNone(result['coordinator']['snapshot'])

    def test_profile_provider_capability_and_layers(self):
        catalog = dict(self.catalog, profiles={'known': {'provider': 'codex-lb'},
                                               'unknown': {'provider': 'other'}})
        self.assertTrue(config.fast_supported(dict(self.codex(), provider='', profile='known'), catalog))
        self.assertFalse(config.fast_supported(dict(self.codex(), provider='', profile='unknown'), catalog))
        result = config.load_config(self.root, catalog=catalog, layers=(
            {'coordinator': self.codex(True)}, {'coordinator': {'model': 'unknown'}}))
        self.assertNotIn('fast', result['roles']['coordinator'])

    def test_claude_known_unsupported_platform_does_not_offer_fast(self):
        with patch.dict(os.environ, {'CLAUDE_CODE_USE_BEDROCK': '1'}):
            self.assertEqual(config.connections(home=self.root)['fast_models']['claude'], [])
        path = self.root / 'claude.json'
        path.write_text(json.dumps({'env': {'CLAUDE_CODE_USE_VERTEX': '1', 'SECRET': 'never published'}}))
        cfg = dict(agent='claude', model='claude-opus-5-5', profile=str(path))
        self.assertFalse(config.fast_supported(cfg, self.catalog))


if __name__ == '__main__':
    unittest.main()
