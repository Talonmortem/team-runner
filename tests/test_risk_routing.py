"""Risk role compatibility and final acceptance, without live workers."""
import copy
import contextlib
import io
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from test_goal_runner import Fixture, HTTP, runner, launch, store, config
from test_mkspec import mkspec

NEW = {'tester_complex', 'final_reviewer_critical', 'escalation_expert'}


class RiskRoles(Fixture):
    def test_primary_replacement_cannot_overlap_reserve_executor(self):
        attempt, _ = self.attempt()
        launch.save_registry(attempt, {'fenced': False, 'runs': [], 'agents': [
            {'role': 'implementer_standard', 'variant': 'reserve', 'function': 'f',
             'dispatch_id': 'prior', 'status': 'active', 'task_id': 'old'}]})
        with patch.object(launch, 'observe_agent', return_value={'dispatch': {'status': 'dispatched'}}), \
                patch.object(launch, 'child_run') as child, patch.object(launch, '_launch') as start:
            with self.assertRaisesRegex(RuntimeError, 'complete or stop'):
                launch.dispatch_role(attempt, 'implementer_medium', 'spec', 'title', self.root, function='f')
            with self.assertRaisesRegex(RuntimeError, 'complete or stop'):
                launch.launch(attempt, 'implementer_complex', 'new', 'run', 'title', self.root, function='f')
            child.assert_not_called()
            start.assert_not_called()

    def test_retry_freezes_reserves_unless_refresh_requested(self):
        for legacy in (False, True):
            for refresh in (False, True):
                with self.subTest(legacy=legacy, refresh=refresh):
                    attempt, st = self.attempt()
                    original = config.defaults()
                    original['reserves']['tester'] = dict(original['roles']['tester'], model='old-reserve')
                    if legacy:
                        original = {'version': 1, 'roles': original['roles']}
                    store.atomic_json(attempt / 'config.json', original)
                    before = (attempt / 'config.json').read_bytes()
                    saved = config.defaults()
                    saved['reserves']['tester'] = dict(saved['roles']['tester'], model='new-reserve')
                    store.atomic_json(runner.context.settings_dir / 'config.json', saved)
                    st['stop_status'] = 'stopped'
                    runner.apply_control(st, 'retry', {'refresh': refresh})
                    self.assertEqual(st['current']['retry_config'], saved if refresh else original)
                    self.assertEqual((attempt / 'config.json').read_bytes(), before)

    def test_defaults_legacy_layers_and_snapshot(self):
        cfg = config.defaults()
        self.assertEqual(set(cfg['roles']), set(config.ROLES))
        for role in ('tester_complex', 'final_reviewer', 'escalation'):
            self.assertEqual((cfg['roles'][role]['model'], cfg['roles'][role]['effort']), ('gpt-6.1-sol', 'high'))
        for role in ('final_reviewer_critical', 'escalation_expert'):
            self.assertEqual(cfg['roles'][role]['model'], 'claude-opus-5-5')
        legacy = {'version': 1, 'roles': copy.deepcopy(cfg['roles'])}
        for role in NEW:
            del legacy['roles'][role]
        legacy['roles']['final_reviewer']['model'] = 'old-review-model'
        store.atomic_json(self.root / 'config.json', legacy)
        loaded = config.load_config(self.root, layers=({'tester_complex': {'model': 'project-sol'}},
                                                     {'tester_complex': {'effort': 'high'}},))
        self.assertEqual(loaded['roles']['final_reviewer']['model'], 'old-review-model')
        self.assertEqual(loaded['roles']['tester_complex']['model'], 'project-sol')
        self.assertEqual(loaded['roles']['tester_complex']['effort'], 'high')
        self.assertEqual(config.settings_update(legacy, loaded), loaded)
        attempt, _ = self.attempt()
        store.atomic_json(attempt / 'config.json', legacy)
        before = (attempt / 'config.json').read_bytes()
        projections = config.projections(self.root, runner.load_state(), {})
        self.assertIsNone(projections['tester_complex']['snapshot'])
        self.assertEqual(projections['final_reviewer']['snapshot']['model'], 'old-review-model')
        with patch.object(launch, 'child_run') as child, patch.object(launch, 'orca') as orca:
            for role in NEW:
                with self.assertRaisesRegex(ValueError, 'absent.*snapshot'):
                    launch.dispatch_role(attempt, role, 'spec', 'title', self.root)
            child.assert_not_called()
            orca.assert_not_called()
        self.assertEqual(config.snapshot_role(attempt, 'final_reviewer')['model'], 'old-review-model')
        self.assertEqual((attempt / 'config.json').read_bytes(), before)

    def test_cli_and_catalog_accept_new_roles(self):
        from team_runner.cli import parser
        from team_runner.catalog import roles
        for role in NEW:
            parsed = parser().parse_args(['launch', role, '--spec', 'spec.md', '--title', 'test'])
            self.assertEqual(parsed.role, role)
            self.assertEqual(roles({role: {'effort': 'high'}}), {role: {'effort': 'high'}})

    def test_dispatch_uses_snapshot_settings(self):
        attempt, _ = self.attempt()
        for role in ('tester_complex', 'final_reviewer', 'final_reviewer_critical', 'escalation_expert'):
            with self.subTest(role=role), patch.object(launch, '_launch', side_effect=lambda a, r, rec, *x: rec):
                rec = launch.launch(attempt, role, role, 'run', 'title', self.root)
                self.assertEqual(rec['requested'], config.defaults()['roles'][role])
                argv = launch.argv(rec['requested'], self.root)
                self.assertIn(rec['requested']['model'], argv)
            store.atomic_json(attempt / 'agents.json', {'agents': [], 'runs': [], 'fenced': False})

    def test_reuse_boundaries(self):
        cfg = config.defaults()['roles']['tester_complex']
        old = dict(dispatch_id='old', status='active', role='tester_complex', function='f', requested=cfg,
                   connection_stamp=launch.connection_stamp(cfg))
        reg = {'agents': [old]}
        with patch.object(launch, 'observe_agent', return_value={'dispatch': {'status': 'completed'}}), \
                patch.object(launch, 'liveness', return_value='live'), patch.object(launch, 'verify_terminal'):
            self.assertIs(launch.reuse_source(reg, 'tester_complex', cfg, 'f', 'old', 50), old)
            for role in ('final_reviewer', 'final_reviewer_critical', 'escalation', 'escalation_expert', 'completion_checker', 'tester'):
                with self.assertRaises(ValueError):
                    launch.reuse_source(reg, role, cfg, 'f', 'old', 50)
            for function, percent in [('other', 30), ('f', 51), ('f', None)]:
                with self.assertRaises(ValueError):
                    launch.reuse_source(reg, 'tester_complex', cfg, function, 'old', percent)
            with self.assertRaises(ValueError):
                launch.reuse_source(reg, 'tester_complex', {**cfg, 'effort': 'xhigh'}, 'f', 'old', 40)
            with self.assertRaises(ValueError):
                launch.reuse_source({'agents': []}, 'tester_complex', cfg, 'f', 'old', 40)

    def test_spec_role_validation_before_output(self):
        attempt, _ = self.attempt()
        templates = self.root / 'templates'; templates.mkdir()
        for name in ('reviewer', 'tester-checks', 'tester-gates'):
            (templates / (name + '.md')).write_text('test')
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}), patch.object(mkspec, 'HERE', str(templates)), contextlib.redirect_stdout(io.StringIO()):
            for name, role in [('reviewer', 'final_reviewer_critical'), ('tester-checks', 'tester_complex')]:
                mkspec.main([name, role, '--role', role])
                self.assertIn(json.dumps({role: config.defaults()['roles'][role]}),
                              (attempt / 'specs' / (role + '.md')).read_text())
            for name, role in [('tester-gates', 'tester_complex'), ('reviewer', 'tester'), ('reviewer', 'missing')]:
                with self.assertRaisesRegex(SystemExit, 'incompatible'):
                    mkspec.main([name, 'invalid', '--role', role])
                self.assertFalse((attempt / 'specs/invalid.md').exists())
            cfg = config.defaults(); del cfg['roles']['tester_complex']
            store.atomic_json(attempt / 'config.json', cfg)
            target = attempt / 'specs/existing.md'; target.write_text('preserve')
            with self.assertRaisesRegex(SystemExit, 'absent.*snapshot'):
                mkspec.main(['tester-checks', 'existing', '--role', 'tester_complex'])
            self.assertEqual(target.read_text(), 'preserve')


class RiskHTTP(HTTP):
    def test_full_payload_can_replace_invalid_saved_configuration(self):
        store.atomic_json(runner.context.settings_dir / 'config.json', {'version': 999})
        self.assertEqual(self.request('/api/config', config.defaults()), config.defaults())

    def test_legacy_payload_preserves_new_saved_settings(self):
        cfg = config.defaults()
        cfg['roles']['tester_complex']['model'] = 'saved-complex'
        self.request('/api/config', cfg)
        legacy = {'version': 1, 'roles': copy.deepcopy(cfg['roles'])}
        for role in NEW:
            del legacy['roles'][role]
        legacy['roles']['tester']['effort'] = 'high'
        self.request('/api/config', legacy)
        read = self.request('/api/config')['config']
        self.assertEqual(read['roles']['tester_complex']['model'], 'saved-complex')
        self.assertEqual(read['roles']['tester']['effort'], 'high')


class FinalAcceptance(Fixture):
    def setUp(self):
        super().setUp()
        self.args = SimpleNamespace(goal='feat/goal')
        self.goal = runner.repo / self.args.goal
        self.goal_data = {'queue': [{'id': 'F', 'path': 'f', 'status': 'completed'}]}
        self.feature = {'queue': [{'id': 'T1', 'status': 'completed'}]}
        store.atomic_json(self.goal / 'Tasks.json', self.goal_data)
        store.atomic_json(self.goal / 'f/Tasks.json', self.feature)
        self.artifact = self.goal / 'f/evidence/t1/acceptance.md'
        self.artifact.parent.mkdir(parents=True); self.artifact.write_text('evidence')
        self.report = self.goal / 'evidence/goal-final-review.md'
        self.report.parent.mkdir(); self.report.write_text('report')

    def verify(self):
        return runner.verify_final(self.args, 'old')

    def test_complete_and_missing_artifacts(self):
        self.assertEqual(self.verify(), [])
        self.artifact.unlink()
        self.assertIn('missing accepted artifact', '\n'.join(self.verify()))
        self.report.unlink()
        self.assertIn('missing accepted report', '\n'.join(self.verify()))

    def test_tracking_must_be_present_readable_and_complete(self):
        path = self.goal / 'f/Tasks.json'
        path.unlink()
        self.assertIn('missing feature tracking', '\n'.join(self.verify()))
        path.write_text('broken')
        self.assertIn('invalid tracking', '\n'.join(self.verify()))
        self.feature['queue'][0]['status'] = 'in_progress'
        store.atomic_json(path, self.feature)
        self.assertIn('F/T1: expected completed', self.verify())
        self.goal_data['queue'][0]['status'] = 'in_progress'
        store.atomic_json(self.goal / 'Tasks.json', self.goal_data)
        self.assertIn('feature F: expected completed', self.verify())
        (self.goal / 'Tasks.json').unlink()
        self.assertIn('invalid tracking', '\n'.join(self.verify()))

    def test_profile_commit_and_clean_requirements(self):
        profile = copy.deepcopy(runner.attempt_profile())
        profile['acceptance'] = {'require_commit': True, 'require_clean': True}
        with patch.object(runner, 'attempt_profile', return_value=profile), patch.object(runner, 'git', return_value=''), patch.object(runner, 'dirty_paths', return_value=['dirty']):
            problems = '\n'.join(self.verify())
            self.assertIn('missing accepted report', problems)
            self.assertIn('missing accepted artifact', problems)
            self.assertIn('uncommitted changes', problems)
        with patch.object(runner, 'attempt_profile', return_value=profile), patch.object(runner, 'git', side_effect=lambda *a: '' if a[0] == 'diff' else 'old'), patch.object(runner, 'dirty_paths', return_value=[]):
            self.assertIn('no new commits', '\n'.join(self.verify()))
        with patch.object(runner, 'attempt_profile', return_value=profile), patch.object(runner, 'git', side_effect=lambda *a: '' if a[0] == 'diff' else 'new'), patch.object(runner, 'dirty_paths', return_value=[]):
            self.assertEqual(self.verify(), [])

    def test_commit_profile_rejects_index_only_or_modified_artifacts(self):
        profile = copy.deepcopy(runner.attempt_profile())
        profile['acceptance']['require_commit'] = True
        for exists, diff in [('', ''), ('tracked', 'modified')]:
            with self.subTest(exists=exists, diff=diff), patch.object(runner, 'attempt_profile', return_value=profile), patch.object(runner, 'git', side_effect=lambda *a: exists if a[0] == 'ls-tree' else diff if a[0] == 'diff' else 'new'):
                self.assertIn('missing accepted report', '\n'.join(self.verify()))

    def test_unborn_head_and_malformed_item_block_completion(self):
        profile = copy.deepcopy(runner.attempt_profile())
        profile['acceptance']['require_commit'] = True
        with patch.object(runner, 'attempt_profile', return_value=profile), patch.object(runner, 'git', side_effect=subprocess.CalledProcessError(128, 'git')):
            self.assertIn('missing final commit', self.verify())
        self.feature['queue'][0]['id'] = 123
        store.atomic_json(self.goal / 'f/Tasks.json', self.feature)
        self.assertIn('invalid tracking', '\n'.join(self.verify()))
