"""Completion requires current full tracking and runner-owned final proof."""
import copy
import uuid
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import test_platform as fixtures
from team_runner import goal_completion as completion, goal_intent
from team_runner.runner_store import atomic_json, read_json


class GoalCompletion(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def prepare(self, final=True):
        goal = 'feat/goal-fixture'
        auth = goal_intent.authorize(self.a, {'goal': goal, 'final': final}, str(uuid.uuid4()))
        self.a.authorization = {k: auth[k] for k in ('generation', 'epoch')}
        self.goal = self.a.repo / goal
        self.root_path = self.goal / 'Tasks.json'
        self.feature_path = self.goal / 'feat-one/Tasks.json'
        root = read_json(self.root_path)
        for feat in root['queue']:
            feat['status'] = 'completed'
        atomic_json(self.root_path, root)
        feature = read_json(self.feature_path)
        for item in feature['queue']:
            item['status'] = 'completed'
        atomic_json(self.feature_path, feature)
        self.state = {'goal': goal, 'history': []}
        if final:
            evidence = self.a.attempt_profile()['evidence_dir']
            self.report = self.goal / evidence / 'goal-final-review.md'
            self.report.parent.mkdir(parents=True, exist_ok=True)
            self.report.write_text('Final acceptance evidence')
            for item in feature['queue']:
                artifact = self.feature_path.parent / evidence / item['id'].lower() / 'acceptance.md'
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text('Item acceptance evidence')
            self.state['history'].append({'item': completion.FINAL_ID, 'outcome': 'succeeded',
                'problems': [], 'dispatch_id': 'final-dispatch',
                'completion_generation': auth['generation'],
                'tracking_fingerprint': completion.tracking_fingerprint(self.a, goal),
                'source_proof': completion.source_proof(self.a, goal)})
        self.a.save_state(self.state)

    def test_current_final_proof_and_idempotent_record(self):
        self.prepare()
        receipt = completion.record(self.a, self.state)
        self.assertEqual(receipt['status'], 'completed')
        path = self.a.state_dir / 'goal-completion.json'
        before = path.stat().st_mtime_ns
        self.assertEqual(completion.read(self.a), receipt)
        self.assertEqual(completion.record(self.a, self.state), receipt)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_saved_final_false_is_only_queue_completed(self):
        self.prepare(final=False)
        receipt = completion.record(self.a, self.state)
        self.assertEqual(receipt['status'], 'queue_completed')
        self.assertFalse(receipt['final_accepted'])
        self.assertEqual(completion.read(self.a), receipt)

    def test_missing_nested_file_never_completes(self):
        self.prepare(final=False)
        self.feature_path.unlink()
        self.assertEqual(completion.record(self.a, self.state), {})

    def test_incomplete_and_unresolved_dependencies_never_complete(self):
        for change in ({'status': 'blocked'}, {'depends_on': ['missing']}):
            with self.subTest(change=change):
                self.prepare(final=False) if not hasattr(self, 'state') else None
                feature = read_json(self.feature_path)
                original = copy.deepcopy(feature)
                feature['queue'][0].update(change)
                atomic_json(self.feature_path, feature)
                self.assertEqual(completion.record(self.a, self.state), {})
                atomic_json(self.feature_path, original)

    def test_tracking_changes_invalidate_read_without_writes(self):
        self.prepare()
        completion.record(self.a, self.state)
        path = self.a.state_dir / 'goal-completion.json'
        before = path.read_bytes()
        feature = read_json(self.feature_path)
        feature['queue'][0]['task'] = 'changed requirement'
        atomic_json(self.feature_path, feature)
        self.assertEqual(completion.read(self.a), {})
        self.assertEqual(completion.record(self.a, self.state), {})
        self.assertEqual(path.read_bytes(), before)

    def test_stale_final_history_or_generation_cannot_complete(self):
        self.prepare()
        for field, value in [('completion_generation', str(uuid.uuid4())),
                             ('tracking_fingerprint', 'stale'), ('outcome', 'failed'),
                             ('problems', ['not accepted'])]:
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state['history'][-1][field] = value
                self.assertEqual(completion.record(self.a, state), {})

    def test_current_skipped_and_uncertain_state_cannot_complete(self):
        self.prepare(final=False)
        for field, value in [('current', {'item': 'pending'}), ('skipped', ['T1']),
                             ('control_uncertain', True), ('stop_requested', True)]:
            with self.subTest(field=field):
                self.assertEqual(completion.record(self.a, {**self.state, field: value}), {})

    def test_only_runner_authorization_can_record(self):
        self.prepare()
        self.a.authorization = None
        self.assertEqual(completion.record(self.a, self.state), {})
        self.assertFalse((self.a.state_dir / 'goal-completion.json').exists())

    def test_reader_does_not_repair_missing_receipt(self):
        self.prepare()
        self.assertEqual(completion.read(self.a), {})
        self.assertFalse((self.a.state_dir / 'goal-completion.json').exists())
        self.assertTrue(completion.record(self.a, self.state))

    def test_foreign_receipt_is_invalid(self):
        self.prepare()
        receipt = completion.record(self.a, self.state)
        receipt['identity'] = self.b.context.identity()
        atomic_json(self.a.state_dir / 'goal-completion.json', receipt)
        self.assertEqual(completion.read(self.a), {})

    def test_runner_repairs_receipt_after_accepted_history_crash(self):
        self.prepare()
        values = dict(goal=self.state['goal'], mode='auto', final=True, items=None,
                      config=None, model=None)
        with patch.object(self.a, 'ensure_run', return_value='fixture-run'), \
                patch.object(self.a, 'process_controls'), patch.object(self.a, 'notify'), \
                patch.object(self.a, 'start_coordinator') as dispatch:
            self.assertEqual(self.a.cmd_run(SimpleNamespace(**values)), 0)
            dispatch.assert_not_called()
        self.assertEqual(completion.read(self.a)['status'], 'completed')

    def test_source_change_without_tracking_change_invalidates_completion(self):
        self.prepare()
        source = self.a.repo / 'source.py'
        source.write_text('original')
        subprocess.run(['git', '-C', str(self.a.repo), 'add', 'source.py'], check=True)
        self.state['history'][-1]['source_proof'] = completion.source_proof(self.a, self.state['goal'])
        self.a.save_state(self.state)
        completion.record(self.a, self.state)
        source.write_text('changed')
        self.assertEqual(completion.read(self.a), {})
        self.assertEqual(completion.record(self.a, self.state), {})

    def test_new_untracked_source_invalidates_but_ignored_file_does_not(self):
        self.prepare()
        ignore = self.a.repo / '.gitignore'
        ignore.write_text('ignored.tmp\n')
        self.state['history'][-1]['source_proof'] = completion.source_proof(self.a, self.state['goal'])
        self.a.save_state(self.state)
        completion.record(self.a, self.state)
        (self.a.repo / 'ignored.tmp').write_text('runtime cache')
        self.assertTrue(completion.read(self.a))
        (self.a.repo / 'new_source.py').write_text('new code')
        self.assertEqual(completion.read(self.a), {})

    def test_ignored_acceptance_artifact_changes_and_deletion_invalidate(self):
        self.prepare()
        (self.a.repo / '.gitignore').write_text('**/acceptance.md\n**/goal-final-review.md\n')
        self.state['history'][-1]['source_proof'] = completion.source_proof(self.a, self.state['goal'])
        self.a.save_state(self.state)
        completion.record(self.a, self.state)
        before = self.report.read_text()
        self.report.write_text('changed final evidence')
        self.assertEqual(completion.read(self.a), {})
        self.report.write_text(before)
        self.assertTrue(completion.read(self.a))
        self.report.unlink()
        self.assertEqual(completion.read(self.a), {})

    def test_runtime_state_inside_checkout_does_not_recurse(self):
        self.prepare()
        from types import SimpleNamespace
        state = self.a.repo / '.runtime-state'
        wrapped = SimpleNamespace(repo=self.a.repo, state_dir=state / 'workspace',
                                  context=SimpleNamespace(state_root=state),
                                  attempt_profile=self.a.attempt_profile)
        before = completion.source_proof(wrapped, self.state['goal'])
        state.mkdir()
        (state / 'receipt.json').write_text('runtime projection')
        self.assertEqual(completion.source_proof(wrapped, self.state['goal']), before)

    def test_source_mode_symlink_and_deleted_tracked_file_change_proof(self):
        self.prepare()
        source = self.a.repo / 'script.py'
        source.write_text('source')
        link = self.a.repo / 'source-link'
        link.symlink_to('script.py')
        subprocess.run(['git', '-C', str(self.a.repo), 'add', 'script.py', 'source-link'], check=True)
        before = completion.source_proof(self.a, self.state['goal'])
        source.chmod(0o755)
        self.assertNotEqual(completion.source_proof(self.a, self.state['goal']), before)
        source.chmod(0o644)
        self.assertEqual(completion.source_proof(self.a, self.state['goal']), before)
        link.unlink(); link.symlink_to('other.py')
        self.assertNotEqual(completion.source_proof(self.a, self.state['goal']), before)
        link.unlink(); link.symlink_to('script.py')
        source.unlink()
        self.assertNotEqual(completion.source_proof(self.a, self.state['goal']), before)

    def test_source_mutation_during_final_verification_is_not_accepted(self):
        self.prepare()
        state = {'goal': self.state['goal'], 'history': [], 'current': {
            'item': completion.FINAL_ID, 'dispatch_id': 'new-final'}}
        self.a.save_state(state)
        def verify():
            (self.a.repo / 'changed_during_acceptance.py').write_text('changed')
            return []
        with patch.object(self.a, 'poll_children'), \
                patch.object(self.a, 'child_acceptance_problems', return_value=([], [])), \
                patch.object(self.a, 'git', return_value='fixture-head'), \
                patch.object(self.a, 'notify'):
            self.assertFalse(self.a.finish_item(SimpleNamespace(goal=state['goal']), state,
                             {'outcome': 'succeeded', 'body': 'report'}, verify))
        self.assertTrue(state['current']['failed'])
        self.assertEqual(completion.read(self.a), {})
