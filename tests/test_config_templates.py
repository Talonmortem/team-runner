"""Built-in drafts, configuration compatibility and immutable reserve lookup."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from team_runner import runner_config as config


class Templates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'repo/sber/app'
        self.repo.mkdir(parents=True)
        self.catalog = {'profiles': {}, 'base_providers': ['codex-lb'],
                        'opencode2': {'models': ['claude-opus-4.8', 'gpt-5.5',
                            'claude-sonnet-4.6', 'deepseek-v4.1-flash', 'glm-5.2']}}
        self.home_patch = patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_current_draft_efforts_and_independent_copies(self):
        cfg = config.get_template('current')
        self.assertEqual(cfg['roles']['implementer_medium']['model'], 'gpt-6.1-terra')
        self.assertEqual(cfg['roles']['implementer_medium']['effort'], 'xhigh')
        self.assertEqual(cfg['roles']['implementer_standard']['model'], 'gpt-6-luna')
        self.assertEqual(set(r['effort'] for r in cfg['roles'].values()), {'high', 'xhigh'})
        self.assertTrue(all(v is None for v in cfg['reserves'].values()))
        cfg['roles']['tester']['effort'] = 'low'
        self.assertEqual(config.get_template('current')['roles']['tester']['effort'], 'xhigh')

    def test_openai_only_mapping_and_reload(self):
        cfg = config.template_config('OpenAI-only', self.home, self.catalog)
        self.assertEqual(cfg['template'], 'openai-only')
        self.assertEqual(set(cfg['roles']), set(config.ROLES))
        for role in cfg['roles'].values():
            self.assertEqual((role['agent'], role['provider']), ('codex', 'codex-lb'))
            self.assertTrue(role['model'].startswith('gpt-'))
        self.assertEqual(cfg['roles']['coordinator']['model'], 'gpt-6.1-sol')
        self.assertEqual(cfg['roles']['coordinator']['effort'], 'medium')
        self.assertTrue(all(v is None for v in cfg['reserves'].values()))
        (self.home / 'config.json').write_text(json.dumps(cfg))
        self.assertEqual(config.load_config(self.home, catalog=self.catalog), cfg)
        self.assertEqual(config.defaults()['roles']['coordinator']['agent'], 'claude')

    def test_cloud_mapping_reserves_and_scope(self):
        cfg = config.template_config('Cloud.ru', self.repo, self.catalog)
        self.assertEqual(cfg['roles']['implementer_medium']['model'], 'claude-opus-4.8')
        for role in ('explorer', 'implementer_standard', 'completion_checker'):
            self.assertEqual(cfg['reserves'][role]['model'], 'claude-opus-4.8')
        for role in ('tester', 'tester_complex'):
            self.assertEqual(cfg['roles'][role]['model'], 'gpt-5.5')
            self.assertEqual(cfg['reserves'][role]['model'], 'claude-opus-4.8')
        self.assertEqual({v['model'] for group in ('roles', 'reserves')
                          for v in cfg[group].values() if v},
                         {'claude-opus-4.8', 'gpt-5.5', 'deepseek-v4.1-flash'})
        self.assertEqual(cfg['reserves']['tester']['effort'], 'medium')
        self.assertIsNone(cfg['reserves']['coordinator'])
        for role, primary in cfg['roles'].items():
            self.assertEqual(primary['effort'], 'high' if role in ('coordinator', 'planner') else 'medium')
            reserve = cfg['reserves'][role]
            if reserve:
                self.assertEqual(reserve['effort'], 'xhigh' if role == 'planner' else 'medium')
        cfg['reserves']['completion_checker']['model'] = 'deepseek-v4.1-flash'
        cfg['reserves']['completion_checker']['effort'] = 'xhigh'
        with self.assertRaisesRegex(ValueError, 'requires medium or high'):
            config.validate(cfg, self.catalog, self.repo)
        cfg = config.get_template('cloudru')
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            config.template_config('cloudru', self.home, self.catalog)
        current = config.get_template('current')
        current['reserves']['tester'] = cfg['roles']['tester']
        with self.assertRaisesRegex(ValueError, 'repo/sber'):
            config.validate(current, self.catalog, self.home)

    def test_v1_load_and_save_preserve_overrides_reserves(self):
        old = config.get_template('current')
        old = {'version': 1, 'roles': old['roles']}
        del old['roles']['implementer_medium']
        old['roles']['implementer_standard']['effort'] = 'medium'
        (self.home / 'config.json').write_text(json.dumps(old))
        cfg = config.load_config(self.home, catalog=self.catalog)
        self.assertIsNone(cfg['template'])
        self.assertEqual(cfg['roles']['implementer_medium']['effort'], 'medium')
        cfg['reserves']['tester'] = dict(cfg['roles']['tester'], model='custom')
        updated = config.settings_update(old, cfg, self.catalog)
        self.assertEqual(updated['reserves'], cfg['reserves'])
        (self.home / 'config.json').write_text(json.dumps(updated))
        loaded = config.load_config(self.home, overrides={'effort': 'low'}, catalog=self.catalog)
        self.assertEqual(loaded['reserves'], cfg['reserves'])
        self.assertEqual(loaded['roles']['coordinator']['effort'], 'low')

    def test_auto_replan_legacy_reload_and_validation(self):
        cfg = config.get_template('current')
        self.assertFalse(cfg['auto_continue_after_replan'])
        del cfg['auto_continue_after_replan']
        config.validate(cfg, self.catalog)
        (self.home / 'config.json').write_text(json.dumps(cfg))
        self.assertFalse(config.load_config(self.home, catalog=self.catalog)['auto_continue_after_replan'])
        for value in (True, False):
            cfg['auto_continue_after_replan'] = value
            (self.home / 'config.json').write_text(json.dumps(cfg))
            self.assertIs(config.load_config(self.home, catalog=self.catalog)['auto_continue_after_replan'], value)
            legacy = {'version': 1, 'roles': cfg['roles']}
            self.assertIs(config.settings_update(legacy, cfg, self.catalog)['auto_continue_after_replan'], value)
        for value in ('true', 1, None, {}):
            cfg['auto_continue_after_replan'] = value
            with self.assertRaisesRegex(ValueError, 'must be boolean'):
                config.validate(cfg, self.catalog)
            (self.home / 'config.json').write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, 'must be boolean'):
                config.load_config(self.home, catalog=self.catalog)

    def test_snapshot_reserve_never_uses_defaults(self):
        old = {'version': 1, 'roles': config.defaults()['roles']}
        (self.home / 'config.json').write_text(json.dumps(old))
        with self.assertRaisesRegex(ValueError, 'absent'):
            config.snapshot_role(self.home, 'tester', 'reserve')
        new = config.get_template('cloudru')
        (self.home / 'config.json').write_text(json.dumps(new))
        self.assertEqual(config.snapshot_role(self.home, 'tester', 'reserve'), new['reserves']['tester'])
        with self.assertRaisesRegex(ValueError, 'absent'):
            config.snapshot_role(self.home, 'coordinator', 'reserve')


if __name__ == '__main__':
    unittest.main()
