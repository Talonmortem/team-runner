from types import SimpleNamespace
from unittest.mock import patch

from test_goal_runner import Fixture, runner, launch, store


class AcceptanceRework(Fixture):
    def finish(self, st, problems, settled=True):
        with patch.object(runner, 'poll_children'), patch.object(runner, 'settle_children', return_value=settled) as settle, patch.object(launch, 'release_agent') as release, patch.object(runner, 'git', return_value='head'), patch.object(runner, 'notify'):
            accepted = runner.finish_item(None, st, {'outcome': 'succeeded', 'body': 'report'}, lambda: problems)
        return accepted, settle, release

    def coordinator(self):
        attempt, st = self.attempt()
        cfg = store.read_json(attempt / 'config.json')['roles']['coordinator']
        old = dict(id='old', role='coordinator', dispatch_id='owned', task_id='task',
                   run_id='parent', status='active', requested=cfg,
                   connection_stamp=launch.connection_stamp(cfg), terminal='term',
                   terminal_owned=True, incarnation='inc', mailbox='main')
        store.atomic_json(attempt / 'agents.json', {'agents': [old], 'runs': [], 'fenced': False})
        return attempt, st

    def test_rejected_acceptance_preserves_session_and_unfenced_attempt(self):
        attempt, st = self.coordinator()
        accepted, settle, release = self.finish(st, ['status must be completed', 'missing acceptance.md'])
        self.assertFalse(accepted)
        settle.assert_not_called()
        release.assert_not_called()
        self.assertNotIn('failed', st['current'])
        repair = runner.load_state()['current']['acceptance_rework']
        self.assertEqual(repair['number'], 1)
        self.assertEqual(repair['problems'], st['history'][0]['problems'])
        self.assertFalse(launch.registry(attempt)['fenced'])

    def test_limit_preserves_coordinator_and_stops(self):
        _, st = self.coordinator()
        st['current']['acceptance_reworks'] = 2
        accepted, _, release = self.finish(st, ['still invalid'])
        self.assertFalse(accepted)
        release.assert_not_called()
        self.assertTrue(st['current']['failed'])
        self.assertNotIn('acceptance_rework', st['current'])

    def test_cleanup_failure_is_not_automatically_retried(self):
        _, st = self.coordinator()
        accepted, settle, release = self.finish(st, [], settled=False)
        self.assertFalse(accepted)
        settle.assert_called_once()
        release.assert_not_called()
        self.assertTrue(st['current']['failed'])
        self.assertNotIn('acceptance_rework', st['current'])

    def test_success_releases_coordinator(self):
        _, st = self.coordinator()
        accepted, _, release = self.finish(st, [])
        self.assertTrue(accepted)
        release.assert_called_once()
        self.assertIsNone(st['current'])

    def test_stage_receipt_is_minted_only_after_children_and_release(self):
        for failure in ('child', 'settle', 'release', None):
            with self.subTest(failure=failure):
                attempt, st = self.coordinator()
                goal = runner.repo / 'feat/gate'
                store.atomic_json(goal / 'Tasks.json', {'queue': [{'id': 'E9', 'path': 'stage', 'status': 'completed'}]})
                store.atomic_json(goal / 'stage/Tasks.json', {'queue': [{'id': 'X', 'status': 'completed',
                    'check_scope': 'stage', 'gate_evidence': {'accepted': str(attempt / 'accepted.json')}}]})
                order = []
                def release(*args):
                    order.append('release')
                    if failure == 'release': raise RuntimeError('unconfirmed')
                with patch.object(runner, 'poll_children'), \
                        patch.object(runner, 'child_acceptance_problems', return_value=(['review failed'] if failure == 'child' else [], [])), \
                        patch.object(runner, 'settle_children', return_value=failure != 'settle'), \
                        patch.object(launch, 'release_agent', side_effect=release), \
                        patch.object(runner, 'git', return_value='head'), patch.object(runner, 'notify'), \
                        patch('team_runner.verification.accept_gate', side_effect=lambda *a: order.append('accept')) as accept:
                    result = runner.finish_item(SimpleNamespace(goal='feat/gate'), st,
                                                {'outcome': 'succeeded', 'body': 'report'}, lambda: [])
                self.assertEqual(result, failure is None)
                if failure is None:
                    self.assertEqual(order, ['release', 'accept'])
                else:
                    accept.assert_not_called()

    def test_new_dispatch_ignores_old_done(self):
        _, st = self.coordinator()
        self.finish(st, ['missing report'])
        st['current']['messages'] = [{'id': 'old-done', 'run_id': 'parent', 'type': 'worker_done', 'payload': {'dispatchId': 'owned', 'outcome': 'succeeded'}}]
        with patch.object(runner, 'process_controls'), patch.object(runner, 'effect', return_value={'task': {'id': 'repair-task'}}) as effect, patch.object(launch, 'rework_coordinator', return_value={'dispatch_id': 'new', 'terminal': 'term'}), patch.object(runner, 'notify'):
            self.assertTrue(runner.rework_acceptance(st))
        self.assertTrue(any(isinstance(arg, str) and 'missing report' in arg for arg in effect.call_args.args))
        self.assertEqual(st['current']['acceptance_reworks'], 1)
        self.assertNotIn('acceptance_rework', st['current'])
        receipt = {'messages': [{'id': 'new-done', 'type': 'worker_done', 'payload': {'dispatchId': 'new', 'outcome': 'failed'}, 'body': 'new report'}]}
        with patch.object(runner, 'process_controls'), patch.object(runner, 'poll_children'), patch.object(runner, 'poll_outputs'), patch.object(runner, 'orca', return_value=receipt) as check:
            self.assertEqual(runner.wait_coordinator(st)['body'], 'new report')
        check.assert_called_once()

    def test_unknown_effect_stops_without_closing_or_dispatching(self):
        _, st = self.coordinator()
        self.finish(st, ['missing report'])
        with patch.object(runner, 'process_controls'), patch.object(runner, 'effect', side_effect=RuntimeError('unconfirmed; reconcile first')), patch.object(launch, 'rework_coordinator') as dispatch, patch.object(launch, 'release_agent') as release, patch.object(runner, 'notify'):
            self.assertFalse(runner.rework_acceptance(st))
        dispatch.assert_not_called()
        release.assert_not_called()
        self.assertTrue(runner.load_state()['current']['failed'])

    def test_same_terminal_new_task_and_idempotent_recovery(self):
        attempt, st = self.coordinator()
        def start(attempt, reg, record, *args):
            record.update(status='active', dispatch_id='new')
            reg['agents'][0].update(status='superseded', superseded_by='new')
            launch.save_registry(attempt, reg)
            return dict(record)
        with patch.object(launch, 'observe_agent', return_value={'dispatch': {'status': 'completed'}}), patch.object(launch, 'liveness', return_value='live'), patch.object(launch, 'verify_terminal'), patch.object(launch, '_launch', side_effect=start) as dispatch:
            first = launch.rework_coordinator(attempt, 'owned', 'repair-task', 'parent', self.root)
            second = launch.rework_coordinator(attempt, 'owned', 'repair-task', 'parent', self.root)
        self.assertEqual(first, second)
        self.assertEqual(first['terminal'], 'term')
        self.assertEqual(first['reuse'], 'owned')
        dispatch.assert_called_once()

    def test_unconfirmed_terminal_or_dispatch_cannot_be_reused(self):
        attempt, _ = self.coordinator()
        for status, live in [('dispatched', 'live'), ('completed', 'unknown'), ('completed', 'exited')]:
            with self.subTest(status=status, live=live), patch.object(launch, 'observe_agent', return_value={'dispatch': {'status': status}}), patch.object(launch, 'liveness', return_value=live), patch.object(launch, '_launch') as dispatch:
                with self.assertRaises(RuntimeError):
                    launch.rework_coordinator(attempt, 'owned', 'repair-task', 'parent', self.root)
                dispatch.assert_not_called()

    def test_identity_mismatch_cannot_be_reused(self):
        attempt, _ = self.coordinator()
        with patch.object(launch, 'observe_agent', return_value={'dispatch': {'status': 'completed'}, 'terminal': {'incarnationId': 'changed'}}), patch.object(launch, 'liveness', return_value='live'), patch.object(launch, '_launch') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                launch.rework_coordinator(attempt, 'owned', 'repair-task', 'parent', self.root)
            dispatch.assert_not_called()

    def test_unconfirmed_launch_is_never_replayed(self):
        attempt, _ = self.coordinator()
        reg = launch.registry(attempt)
        reg['agents'].append(dict(role='coordinator', status='unknown', task_id='repair-task', reuse='owned'))
        launch.save_registry(attempt, reg)
        with patch.object(launch, '_launch') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                launch.rework_coordinator(attempt, 'owned', 'repair-task', 'parent', self.root)
            dispatch.assert_not_called()

    def test_run_returns_to_acceptance_without_advancing_queue(self):
        from team_runner import runner_config as config
        _, st = self.coordinator()
        args = SimpleNamespace(goal='feat/goal', config=None, items='X', mode='auto', final=False, handoff=None,
                               **{key: None for key in config.FIELDS})
        goal = runner.repo / args.goal
        goal.mkdir(parents=True)
        store.atomic_json(goal / 'Tasks.json', {})
        def correction(state):
            state['current'].update(dispatch_id='new', acceptance_reworks=1)
            state['current'].pop('acceptance_rework')
            return True
        with patch.object(runner, 'process_controls'), patch.object(runner, 'ensure_run', return_value='parent'), patch('team_runner.engine.Tracking') as tracking, patch.object(runner, 'start_coordinator', return_value='start'), patch.object(runner, 'wait_coordinator', return_value={'outcome': 'succeeded', 'body': 'report'}) as wait, patch.object(runner, 'verify_item', side_effect=[['missing report'], []]), patch.object(runner, 'rework_acceptance', side_effect=correction) as repair, patch.object(runner, 'poll_children'), patch.object(runner, 'settle_children', return_value=True), patch.object(launch, 'release_agent'), patch.object(runner, 'git', return_value='head'), patch.object(runner, 'notify'):
            tracking.return_value.find.return_value = ({'path': 'feature'}, None, {'id': 'X'})
            tracking.return_value.canonical.side_effect = lambda item, strict=True: item
            self.assertEqual(runner.cmd_run(args), 0)
            tracking.return_value.next_item.assert_not_called()
            repair.assert_called_once()
            self.assertEqual(wait.call_count, 2)
        self.assertIsNone(runner.load_state()['current'])

    def test_prompt_and_missing_report_without_commit_requirement(self):
        attempt, _ = self.attempt()
        profile = store.read_json(attempt / 'context.json')['profile']
        profile['acceptance']['require_commit'] = False
        profile['acceptance']['require_clean'] = False
        args = SimpleNamespace(goal='feat/goal', handoff=None)
        feature = {'path': 'feature'}
        with patch.object(runner, 'attempt_profile', return_value=profile):
            spec = runner.coordinator_spec(args, feature, {'id': 'step-1'})
            self.assertIn('exactly completed', spec)
            self.assertIn('feat/goal/feature/evidence/step-1/acceptance.md', spec)
            with patch('team_runner.engine.Tracking') as tracking, patch.object(runner, 'git', return_value='head'), patch.object(runner, 'dirty_paths', return_value=[]):
                tracking.return_value.find.return_value = (feature, None, {'id': 'step-1', 'status': 'completed'})
                problems = runner.verify_item(args, 'step-1', 'head', [])
            self.assertEqual(problems, ['нет файла feat/goal/feature/evidence/step-1/acceptance.md'])
