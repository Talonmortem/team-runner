"""A closed checking process is cleaned up, but its work remains unaccepted."""
from unittest.mock import patch

import test_acceptance_rework as acceptance
from test_goal_runner import Fixture, runner, launch, store


class ClosedTesterRecovery(Fixture):
    coordinator = acceptance.AcceptanceRework.coordinator
    finish = acceptance.AcceptanceRework.finish
    def closed_tester(self, retained=False):
        attempt, st = self.coordinator()
        terminal = {'handle': 'tester-term', 'incarnationId': 'tester-inc',
                    'ptyId': 'tester-pty', 'worktreeId': runner.context.orca_id}
        agent = dict(id='tester', role='tester', function='acceptance', dispatch_id='check-old',
                     status='active', terminal=terminal['handle'], incarnation=terminal['incarnationId'],
                     terminal_owned=True)
        reg = launch.registry(attempt)
        reg['agents'].append(agent)
        # A previously successful check must not satisfy the new verification.
        reg['agents'].append(dict(role='tester', function='acceptance', dispatch_id='stale-check', status='released'))
        launch.save_registry(attempt, reg)
        st['current']['messages'] = [{'type': 'worker_done', 'payload': {'dispatchId': 'stale-check', 'outcome': 'succeeded'}}]
        runner.save_state(st)
        store.atomic_json(launch.journal(attempt), {'tester-terminal': {
            'status': 'completed', 'result': {'terminal': terminal}}})
        process = 'tester-pty:tester-inc'
        data = {'dispatch': {'id': 'check-old', 'status': 'failed', 'terminationReason': 'operator_close',
                             'assigneeHandle': terminal['handle'], 'processIncarnation': process},
                'worker': {'stage': 'process_exited', 'worktreeId': runner.context.orca_id},
                'projection': {'liveness': {'verdict': 'exited'}},
                'terminal': terminal if retained else None,
                'terminalResource': {'ownershipState': 'external', 'ownerDispatchId': 'check-old',
                    'terminalHandle': terminal['handle'], 'worktreeId': runner.context.orca_id,
                    'endpointIncarnation': process}}
        return attempt, st, terminal, data

    def finish_closed(self, st, terminal, data):
        with patch.object(launch, 'observe_agent', return_value=data), patch.object(launch, 'orca', return_value={'terminals': [terminal] if data['terminal'] else []}):
            return self.finish(st, [])

    def test_closed_tester_requires_fresh_matching_report_and_normal_acceptance(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                attempt, st, terminal, data = self.closed_tester(retained)
                accepted, settle, release = self.finish_closed(st, terminal, data)
                self.assertFalse(accepted)
                settle.assert_not_called()
                release.assert_not_called()
                self.assertIn('check-old', st['current']['acceptance_rework']['problems'][0])
                self.assertFalse(launch.registry(attempt)['fenced'])
                self.assertEqual(launch.registry(attempt)['agents'][1]['status'], 'stopped')
                # Restart after the problem was saved; coordinator and mailbox are retained.
                st = runner.load_state()
                with patch.object(runner, 'process_controls'), patch.object(runner, 'effect', return_value={'task': {'id': 'repair'}}), patch.object(launch, 'rework_coordinator', return_value={'dispatch_id': 'coord-new'}), patch.object(runner, 'notify'):
                    self.assertTrue(runner.rework_acceptance(st))
                accepted, settle, _ = self.finish(st, [])
                self.assertFalse(accepted)  # A new coordinator report alone is insufficient.
                settle.assert_not_called()
                self.assertEqual(len(st['current']['required_child_reports']), 1)
                reg = launch.registry(attempt)
                reg['agents'].append(dict(role='tester', function='acceptance', dispatch_id='check-new', status='released'))
                launch.save_registry(attempt, reg)
                st['current']['messages'].append({'type': 'worker_done', 'payload': {'dispatchId': 'check-new', 'outcome': 'succeeded'}})
                runner.save_state(st)
                accepted, _, _ = self.finish(st, ['acceptance.md still missing'])
                self.assertFalse(accepted)
                self.assertEqual(st['history'][-1]['problems'], ['acceptance.md still missing'])
                accepted, settle, _ = self.finish(st, [])
                self.assertTrue(accepted)
                settle.assert_called_once()

    def test_foreign_identity_and_unknown_cleanup_do_not_rework(self):
        for mutation in ('identity', 'operation'):
            with self.subTest(mutation=mutation):
                attempt, st, terminal, data = self.closed_tester()
                if mutation == 'identity':
                    data['dispatch']['processIncarnation'] = 'tester-pty:foreign'
                else:
                    journal = store.read_json(launch.journal(attempt))
                    journal['tester-close'] = {'status': 'unknown'}
                    store.atomic_json(launch.journal(attempt), journal)
                accepted, _, _ = self.finish_closed(st, terminal, data)
                self.assertFalse(accepted)
                self.assertNotIn('acceptance_rework', st['current'])
                self.assertTrue(st['current']['failed'])
                self.assertIn('tester Dispatch check-old', st['current']['problems'][0])

    def test_wrong_function_or_failed_report_does_not_satisfy_recheck(self):
        attempt, st, terminal, data = self.closed_tester()
        self.finish_closed(st, terminal, data)
        reg = launch.registry(attempt)
        reg['agents'].extend([dict(role='tester', function='other', dispatch_id='wrong-function', status='released'),
                              dict(role='tester', function='acceptance', dispatch_id='failed-new', status='released')])
        launch.save_registry(attempt, reg)
        st['current']['messages'].extend([{'type': 'worker_done', 'payload': {'dispatchId': 'wrong-function', 'outcome': 'succeeded'}},
                                         {'type': 'worker_done', 'payload': {'dispatchId': 'failed-new', 'outcome': 'failed'}}])
        runner.save_state(st)
        self.assertTrue(runner.child_acceptance_problems(st)[0])

    def test_repeated_close_preserves_limit_and_report_requirement(self):
        _, st, terminal, data = self.closed_tester()
        st['current']['acceptance_reworks'] = 2
        for _ in range(2):
            self.assertFalse(self.finish_closed(st, terminal, data)[0])
        self.assertNotIn('acceptance_rework', st['current'])
        self.assertEqual(len(st['current']['required_child_reports']), 1)
        self.assertTrue(st['current']['failed'])

    def test_new_attempt_archives_old_recovery_error(self):
        _, st = self.coordinator()
        st.update(stop_status='stopped', recovery_issues=['old tester cleanup'], last_error='old failure')
        runner.apply_control(st, 'retry', {})
        self.assertNotIn('recovery_issues', st)
        self.assertNotIn('last_error', st)
        self.assertEqual(st['attempts'][-1]['recovery_issues'], ['old tester cleanup'])

    def test_current_reconciliation_error_replaces_stale_error(self):
        _, st = self.coordinator()
        st.update(last_error='stale failure', recovery_issues=['stale cleanup'])
        with patch.object(launch, 'reconcile_attempt', return_value=['current identity mismatch']), patch.object(runner, 'child_acceptance_problems', return_value=([], [])), patch.object(runner, 'orca'), patch.object(launch, 'orca'):
            runner.recover(st)
        self.assertEqual(st['recovery_issues'], ['current identity mismatch'])
        self.assertEqual(st['current']['recovery_issues'], st['recovery_issues'])
        self.assertNotIn('last_error', st)
        self.assertEqual(st['recovery_history'][-1]['last_error'], 'stale failure')

    def test_restart_after_new_dispatch_does_not_launch_twice(self):
        attempt, st, terminal, data = self.closed_tester()
        self.finish_closed(st, terminal, data)
        repair = st['current']['acceptance_rework']
        task_result = {'task': {'id': 'repair-task'}}
        def interrupted(*args):
            reg = launch.registry(attempt)
            reg['agents'][0].update(status='superseded', superseded_by='coord-new')
            reg['agents'].append(dict(role='coordinator', dispatch_id='coord-new', task_id='repair-task',
                                      reuse='owned', status='active', terminal='term'))
            launch.save_registry(attempt, reg)
            raise KeyboardInterrupt('interrupted after Dispatch receipt, before state save')
        with patch.object(runner, 'process_controls'), patch.object(runner, 'orca', return_value=task_result), patch.object(launch, 'rework_coordinator', side_effect=interrupted), patch.object(runner, 'notify'):
            with self.assertRaises(KeyboardInterrupt):
                runner.rework_acceptance(st)
        st = runner.load_state()
        self.assertFalse(st['current'].get('failed'))
        with patch.object(runner, 'process_controls'), patch.object(runner, 'orca') as remote, patch.object(launch, '_launch') as dispatch, patch.object(runner, 'notify'):
            self.assertTrue(runner.rework_acceptance(st))
        remote.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(st['current']['dispatch_id'], 'coord-new')
        self.assertEqual(st['current']['acceptance_reworks'], 1)
        self.assertNotIn('acceptance_rework', st['current'])

    def test_next_item_archives_previous_attempt_error(self):
        from types import SimpleNamespace
        from team_runner import runner_config as config
        attempt, st = self.coordinator()
        st['history'].append(dict(st['current']))
        st.update(current=None, recovery_issues=['previous cleanup'], last_error='previous failure')
        args = SimpleNamespace(mode='auto', config=None, **{k: None for k in config.FIELDS})
        with patch.object(runner, 'git', return_value='head'), patch.object(runner, 'dirty_paths', return_value=[]), patch.object(runner, 'effect', return_value={'task': {'id': 'next-task'}}), patch.object(launch, 'launch', return_value={'dispatch_id': 'next-dispatch'}), patch.object(runner, 'process_controls'), patch.object(runner, 'notify'):
            runner.start_coordinator(args, st, 'next-item', 'spec', 'title')
        self.assertNotIn('recovery_issues', st)
        self.assertNotIn('last_error', st)
        self.assertEqual(st['recovery_history'][-1]['attempt_dir'], str(attempt))
        self.assertEqual(st['recovery_history'][-1]['issues'], ['previous cleanup'])

    def test_reconcile_finishes_confirmed_rework_but_preserves_unknown_effect(self):
        for status in ('completed', 'unknown'):
            with self.subTest(status=status):
                attempt, st = self.coordinator()
                st['current'].update(failed=True, acceptance_rework_error='interrupted', acceptance_rework={
                    'number': 1, 'dispatch_id': 'owned', 'problems': ['missing check']})
                runner.save_state(st)
                reg = launch.registry(attempt)
                reg['agents'][0].update(status='superseded', superseded_by='new')
                reg['agents'].append(dict(role='coordinator', dispatch_id='new', task_id='repair-task',
                                          reuse='owned', status='active', terminal='term'))
                launch.save_registry(attempt, reg)
                store.atomic_json(runner.state_dir / 'operations.json', {'acceptance-rework:owned': {
                    'status': status, 'args': ['orchestration', 'task-create'], 'request_id': 'request',
                    'result': {'task': {'id': 'repair-task'}}}})
                with patch.object(launch, 'reconcile_attempt', return_value=[]), patch.object(runner, 'orca', return_value={'state': 'unknown'}):
                    runner.recover(st)
                self.assertEqual(bool(st['recovery_issues']), status == 'unknown')
                self.assertEqual(bool(st['current'].get('failed')), status == 'unknown')
                self.assertEqual('acceptance_rework' in st['current'], status == 'unknown')
                if status == 'completed':
                    self.assertEqual(st['current']['acceptance_reworks'], 1)
                    self.assertEqual(st['current']['task_id'], 'repair-task')
