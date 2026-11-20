"""Markdown queue validation, launch snapshots, and attempt-local stage evidence."""
import contextlib
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, cli, goal_intent, goal_scope, protocol, queue_plan, service, verification
from team_runner.runner_store import atomic_json, read_json
from team_runner.tracking import for_goal


CARD = '# Item\n\n' + '\n'.join(f'## {name.title()}\nOriginal {name}.\n' for name in sorted(queue_plan.SECTIONS))


class MarkdownQueue(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def prepare(self):
        self.goal = 'feat/markdown-goal'
        self.directory = self.a.repo / self.goal
        self.file = self.directory / 'Queue.json'
        self.card = self.directory / 'feature/steps/one.md'
        self.card.parent.mkdir(parents=True)
        self.card.write_text(CARD, encoding='utf-8')
        self.card.with_name('two.md').write_text(CARD, encoding='utf-8')
        self.progress = self.directory / 'feature/Progress.md'
        self.progress.write_text('# Progress\n', encoding='utf-8')
        self.queue = {'schema_version': 1, 'queue': [{'id': 'core', 'path': 'feature', 'depends_on': [],
            'items': [{'id': 'one', 'task': 'First item', 'depends_on': [], 'step_card': 'steps/one.md'},
                      {'id': 'two', 'task': 'Second item', 'depends_on': ['one'], 'step_card': 'steps/two.md'}]}]}
        atomic_json(self.file, self.queue)
        return self.queue['queue'][0]['items']

    def test_read_only_validation_and_legacy_discovery(self):
        self.prepare()
        before = {p: p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['validate-queue', str(self.directory)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['items'], ['core/one', 'core/two'])
        self.assertEqual(result['sha256'], verification.digest(queue_plan.read(self.directory)))
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.rglob('*') if p.is_file()})
        self.assertFalse(queue_plan.snapshot_path(self.a, self.goal).exists())
        self.assertIn(self.goal, application.goals(self.a))
        legacy = for_goal(self.a, 'feat/goal-fixture')
        self.assertEqual(legacy.next_item()[2]['id'], 'T1')

    def test_invalid_source_is_rejected_before_terminal_or_snapshot(self):
        self.prepare()
        mutations = [
            (lambda q: q.update(schema_version=True), 'schema_version'),
            (lambda q: q['queue'].append(q['queue'][0]), 'duplicate ID'),
            (lambda q: q['queue'][0].update(path='../other'), 'confined relative'),
            (lambda q: q['queue'][0].update(stage_gate='one'), 'stage_gate'),
            (lambda q: q['queue'][0]['items'][1].update(id='ONE'), 'duplicate ID'),
            (lambda q: q['queue'][0]['items'][0].update(status='completed'), 'unsupported fields'),
            (lambda q: q['queue'][0]['items'][0].update(depends_on=['missing']), 'unknown dependency'),
            (lambda q: q['queue'][0]['items'][0].update(depends_on=['core']), 'dependency cycle'),
            (lambda q: q['queue'][0]['items'][0].update(step_card='steps/gone.md'), 'existing Markdown'),
        ]
        with patch.object(self.a.launcher, 'create_terminal') as launch:
            for change, reason in mutations:
                with self.subTest(reason=reason):
                    queue = copy.deepcopy(self.queue)
                    change(queue)
                    atomic_json(self.file, queue)
                    with self.assertRaisesRegex(ValueError, reason):
                        service.start_runner(self.a, {'goal': self.goal})
            atomic_json(self.file, self.queue)
            self.card.write_text('# Card\n```md\n## Result\nfake heading\n```\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'card sections'):
                queue_plan.read(self.directory)
            outside = self.root / 'outside.md'
            outside.write_text(CARD)
            self.card.unlink()
            self.card.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'path escapes'):
                queue_plan.read(self.directory)
            launch.assert_not_called()
        self.assertFalse(queue_plan.snapshot_path(self.a, self.goal).exists())

    def test_local_dependency_precedes_feature_and_global_item_requires_qualification(self):
        items = self.prepare()
        items[0]['id'] = 'core'
        items[1]['depends_on'] = ['core']
        other = self.directory / 'other'
        other.mkdir()
        (other / 'item.md').write_text(CARD)
        self.queue['queue'].append({'id': 'other', 'path': 'other', 'depends_on': [],
                                    'items': [{'id': 'third', 'task': 'Third', 'step_card': 'item.md',
                                               'depends_on': ['core/two']}]})
        atomic_json(self.file, self.queue)
        self.assertEqual(list(queue_plan.read(self.directory)['cards']), ['core/core', 'core/two', 'other/third'])
        self.queue['queue'][1]['items'][0]['depends_on'] = ['two']
        atomic_json(self.file, self.queue)
        with self.assertRaisesRegex(ValueError, 'unknown dependency two'):
            queue_plan.read(self.directory)

    def test_start_pins_original_and_scope_change_keeps_acceptance(self):
        self.prepare()
        options = {'goal': self.goal, 'mode': 'auto', 'final': False, 'items': 'core/one'}
        auth = goal_intent.authorize(self.a, options, str(uuid.uuid4()))
        path = queue_plan.snapshot_path(self.a, self.goal)
        expected = queue_plan.read(self.directory)

        def terminal(*args, **kwargs):
            self.assertEqual(queue_plan.load_snapshot(self.a, self.goal), expected)
            return {'handle': 'fixture-runner'}

        def ensure_run(state):
            state['run_id'] = 'frozen-run'
            return 'frozen-run'

        with patch.object(self.a.launcher, 'create_terminal', side_effect=terminal):
            launch = service.start_runner(self.a, options)
        pinned = path.read_bytes()
        self.file.write_text('invalid edited queue', encoding='utf-8')
        self.card.write_text('edited card', encoding='utf-8')
        self.card.with_name('two.md').write_text('edited dependent card', encoding='utf-8')
        self.progress.write_text('updated journal', encoding='utf-8')
        seen = []

        def report(state):
            cur = state['current']
            seen.append(cur['item'])
            self.assertEqual((Path(cur['attempt_dir']) / 'step-card.md').read_text(), CARD)
            artifact = self.directory / 'feature/evidence' / cur['item'].split('/')[-1] / 'acceptance.md'
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('checked')
            return {'outcome': 'succeeded', 'body': 'verified'}

        def run_to_exhaustion(nonce):
            with patch.object(self.a, 'ensure_run', side_effect=ensure_run), \
                    patch.object(self.a, 'coordinator_task',
                                 side_effect=lambda _, cur, *args: cur.update(task_id='fixture-task')), \
                    patch.object(self.a, 'process_controls'), \
                    patch.object(self.a, 'wait_coordinator', side_effect=report), patch.object(self.a, 'notify'), \
                    patch.object(self.a, 'dirty_paths', return_value=[]), \
                    patch.object(self.a, 'git', return_value='head'), \
                    patch.object(self.a.launcher, 'launch', side_effect=lambda *a, **kw: {'dispatch_id': str(uuid.uuid4())}), \
                    patch.object(self.a, 'poll_children'), patch.object(self.a, 'settle_children', return_value=True):
                self.assertEqual(service.run_process(self.a, nonce), 0)

        run_to_exhaustion(launch['nonce'])
        self.assertEqual(seen, ['core/one'])
        self.assertEqual(protocol.summary(self.a)['progress']['completed'], 1)
        self.assertEqual(application.state_view(self.a)['queue'][0]['status'], 'completed')
        prepared = goal_scope.prepare(self.a, 'core/one,core/two', str(uuid.uuid4()),
                                      generation=auth['generation'], epoch=auth['epoch'])
        goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                              generation=prepared['generation'], epoch=prepared['epoch'])
        with patch.object(self.a.launcher, 'create_terminal', side_effect=terminal):
            resumed = service.start_runner(self.a, {'resume': True})
            self.assertEqual(for_goal(self.a, self.goal).next_item()[2]['id'], 'two')
            run_to_exhaustion(resumed['nonce'])
            resumed = service.start_runner(self.a, {'resume': True})
            run_to_exhaustion(resumed['nonce'])
        self.assertEqual(seen, ['core/one', 'core/two'])
        self.assertEqual(protocol.summary(self.a)['progress']['completed'], 2)
        self.assertEqual(path.read_bytes(), pinned)
        self.assertEqual(self.progress.read_text(), 'updated journal')

    def test_pending_required_items_block_final_review(self):
        self.prepare()
        with patch.object(self.a.launcher, 'create_terminal', return_value={'handle': 'fixture-runner'}):
            launch = service.start_runner(self.a, {'goal': self.goal, 'mode': 'auto', 'final': True,
                                                   'items': 'core/two'})
        with patch.object(self.a, 'ensure_run', return_value='fixture-run'), \
                patch.object(self.a, 'process_controls'), patch.object(self.a, 'notify'), \
                patch.object(self.a, 'start_coordinator') as coordinator:
            self.assertEqual(service.run_process(self.a, launch['nonce']), 1)
            coordinator.assert_not_called()
        blocker = protocol.summary(self.a)['blocker']
        self.assertEqual(blocker['code'], 'queue_incomplete')
        self.assertIn('core/two: ждёт приёмки one', blocker['message'])
        self.assertEqual(protocol.summary(self.a)['progress']['completed'], 0)

    def test_stage_proof_must_belong_to_the_attempt_and_remain_valid(self):
        items = self.prepare()
        command = ['/bin/true']
        (self.a.repo / 'input.txt').write_text('frozen input')
        items[0].update(check_scope='stage', gate_roots=['input.txt'], gate_command=command)
        self.queue['queue'][0]['stage_gate'] = 'one'
        atomic_json(self.file, self.queue)
        attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        attempt.mkdir(parents=True)
        manifest = verification.create(self.a.repo, ['input.txt'], attempt / 'manifest.json')
        verification.run_check(self.a.repo, attempt / 'manifest.json', manifest['sha256'], command,
                               'fixture', {}, str(attempt), attempt / 'result.json')
        proof = {'path': 'result.json', 'manifest': 'manifest.json', 'sha256': manifest['sha256']}
        declaration = attempt / 'stage-gate.json'
        atomic_json(declaration, {**proof, 'path': str(attempt / 'result.json')})
        tracking = for_goal(self.a, self.goal)
        current = {'attempt_dir': str(attempt), 'item': 'core/one', 'dispatch_id': 'stage-dispatch'}
        item = tracking.resolve('core/one')[3]
        candidate, scope = self.a.stage_candidate(tracking, item, current)
        self.assertFalse(verification.gate_valid(self.a.repo, candidate, current=True, **scope))
        atomic_json(declaration, proof)
        result = read_json(attempt / 'result.json')
        atomic_json(attempt / 'result.json', {**result, 'attempt': 'different'})
        candidate, scope = self.a.stage_candidate(tracking, item, current)
        self.assertFalse(verification.gate_valid(self.a.repo, candidate, current=True, **scope))
        atomic_json(attempt / 'result.json', result)
        state = {'goal': self.goal, 'current': current, 'history': []}
        self.a.save_state(state)
        with patch.object(self.a, 'poll_children'), patch.object(self.a, 'settle_children', return_value=True), \
                patch.object(self.a, 'git', return_value='head'), patch.object(self.a, 'notify'):
            self.assertTrue(self.a.finish_item(SimpleNamespace(goal=self.goal), state,
                            {'outcome': 'succeeded', 'body': 'stage proof'}, lambda: []))
        tracking = for_goal(self.a, self.goal)
        self.assertTrue(tracking.item_done('core/one'))
        self.assertEqual(tracking.next_item()[2]['id'], 'two')
        self.assertEqual(state['history'][0]['stage_evidence']['path'], 'result.json')
        (attempt / 'result.json.log').write_text('tampered')
        self.assertFalse(for_goal(self.a, self.goal).item_done('core/one'))
