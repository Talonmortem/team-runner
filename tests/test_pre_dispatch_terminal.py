"""Exact pre-Dispatch terminal death is recoverable without replaying effects."""
import copy
from unittest.mock import patch

from test_goal_runner import Fixture, launch, runner, store
from runner_browser_fixture import OrcaFixture


class PreDispatchTerminal(Fixture):
    def lost(self):
        attempt, state = self.attempt()
        fake = OrcaFixture(self.root / 'transport', runner.repo)
        def interrupted(*args, **kwargs):
            if args[:2] == ('terminal', 'wait'):
                raise RuntimeError('startup interrupted')
            return fake(*args, **kwargs)
        with patch.object(launch, 'orca', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'startup interrupted'):
                launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root)
        reg = launch.registry(attempt)
        record = reg['agents'][0]
        data = store.read_json(fake.root / 'transport.json')
        data['terminals'][record['terminal']].update(orphaned=True, connected=False, writable=False,
                                                  exitCause={'kind': 'operator_close'})
        store.atomic_json(fake.root / 'transport.json', data)
        op = store.read_json(launch.journal(attempt))[record['id'] + '-terminal']
        marker = store.read_json(op['evidence']['marker'])
        marker['process_stamp'] = 'departed-incarnation'
        store.atomic_json(op['evidence']['marker'], marker)
        return attempt, fake, record, op

    def test_direct_stop_of_closed_pre_dispatch_launch_is_confirmed(self):
        attempt, fake, record, op = self.lost()
        with patch.object(launch, 'orca', side_effect=fake) as call:
            self.assertTrue(launch.stop_attempt(attempt))
        self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'stopped')
        self.assertFalse(any(c.args[:2] == ('terminal', 'close') and record['terminal'] in c.args
                             for c in call.call_args_list))

    def test_missing_terminal_requires_complete_local_absence(self):
        attempt, fake, record, op = self.lost()
        complete = {'terminals': [], 'truncated': False,
                    'hostScope': {'hostIds': ['local'], 'omittedHostIds': []}}
        for kind in ('missing', 'empty_show', 'partial', 'remote_omitted', 'foreign_handle'):
            with self.subTest(kind=kind):
                launch.save_registry(attempt, {'agents': [copy.deepcopy(record)], 'runs': [], 'fenced': True})
                listing = copy.deepcopy(complete)
                if kind == 'partial':
                    listing['truncated'] = True
                if kind == 'remote_omitted':
                    listing['hostScope']['omittedHostIds'] = ['other']
                if kind == 'foreign_handle':
                    listing['terminals'] = [{'handle': record['terminal'], 'incarnationId': 'foreign'}]
                def transport(*args, **kwargs):
                    if args[:2] == ('terminal', 'show'):
                        if kind == 'empty_show':
                            return {'terminal': None}
                        raise RuntimeError('terminal missing')
                    if args[:2] == ('terminal', 'list'):
                        return listing
                    raise AssertionError('unexpected operation')
                with patch.object(launch, 'orca', side_effect=transport):
                    issues = launch.reconcile_attempt(attempt)
                if kind in ('missing', 'empty_show'):
                    self.assertEqual(issues, [])
                    self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'restart_ready')
                else:
                    self.assertTrue(issues)

    def test_ordinary_launch_paths_cannot_bypass_supervised_replacement(self):
        attempt, fake, record, op = self.lost()
        with patch.object(launch, 'orca', side_effect=fake):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                reg = launch.registry(attempt)
                reg['agents'] = [copy.deepcopy(record)]
                reg['agents'][0]['status'] = 'ready' if replacement else 'restart_ready'
                if replacement:
                    reg['agents'][0]['replacement_of'] = 'previous'
                launch.save_registry(attempt, reg)
                with patch.object(launch, 'orca') as call:
                    with self.assertRaisesRegex(RuntimeError, 'supervised continuation'):
                        launch.launch(attempt, record['role'], record['task_id'], record['run_id'],
                                      'T1', self.root, record.get('mailbox'))
                    with self.assertRaisesRegex(RuntimeError, 'supervised continuation'):
                        launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root)
                call.assert_not_called()
                self.assertEqual(len(launch.registry(attempt)['agents']), 1)

    def test_exact_closed_terminal_settles_stop_without_replay(self):
        attempt, fake, record, op = self.lost()
        reg = launch.registry(attempt)
        reg['stop_requested'] = True
        launch.save_registry(attempt, reg)
        before = store.read_json(launch.journal(attempt))
        with patch.object(launch, 'orca', side_effect=fake) as call:
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            self.assertTrue(launch.stop_attempt(attempt))
        settled = launch.registry(attempt)
        self.assertEqual(settled['agents'][0]['status'], 'stopped')
        self.assertEqual(settled['stop_status'], 'stopped')
        self.assertEqual(store.read_json(launch.journal(attempt))[record['id'] + '-terminal'],
                         before[record['id'] + '-terminal'])
        self.assertFalse(any(c.args[:2] in (('terminal', 'create'), ('orchestration', 'worker-start'))
                             for c in call.call_args_list))

    def test_running_replaces_once_with_new_identity_and_original_journal_intact(self):
        attempt, fake, record, op = self.lost()
        with patch.object(launch, 'orca', side_effect=fake):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'restart_ready')
            launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
            launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
        reg = launch.registry(attempt)
        old, new = reg['agents']
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(new['status'], 'active')
        self.assertEqual(new['replacement_of'], old['id'])
        self.assertTrue(all(r['status'] == 'done' for r in reg['launch_requests']))
        self.assertNotEqual(new['terminal'], record['terminal'])
        self.assertEqual(store.read_json(launch.journal(attempt))[record['id'] + '-terminal'], op)
        self.assertEqual(len(store.read_json(fake.root / 'transport.json')['workers']), 1)

    def test_cancelled_or_stopped_intent_never_creates_replacement(self):
        attempt, fake, record, op = self.lost()
        with patch.object(launch, 'orca', side_effect=fake):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                launch.resume_ready_launches(attempt, self.root, permitted=lambda: False)
            reg = launch.registry(attempt)
            reg['stop_requested'] = True
            launch.save_registry(attempt, reg)
            with self.assertRaisesRegex(RuntimeError, 'fenced'):
                launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
        self.assertEqual(len(launch.registry(attempt)['agents']), 1)

    def test_foreign_identity_live_process_and_unknown_dispatch_stay_blocked(self):
        attempt, fake, record, op = self.lost()
        original = store.read_json(fake.root / 'transport.json')
        original_marker = store.read_json(op['evidence']['marker'])
        for kind in ('foreign', 'live', 'not_closed', 'unknown_dispatch'):
            with self.subTest(kind=kind):
                data = copy.deepcopy(original)
                marker = copy.deepcopy(original_marker)
                journal = {record['id'] + '-terminal': copy.deepcopy(op)}
                if kind == 'foreign':
                    data['terminals'][record['terminal']]['incarnationId'] = 'foreign'
                if kind == 'live':
                    from team_runner.runner_entry import process_stamp
                    marker['process_stamp'] = process_stamp(marker['pid'])
                if kind == 'not_closed':
                    data['terminals'][record['terminal']]['exitCause'] = {'kind': 'unknown'}
                if kind == 'unknown_dispatch':
                    journal[record['id'] + '-dispatch'] = {'status': 'unknown', 'request_id': 'unknown',
                        'args': ['orchestration', 'worker-start']}
                store.atomic_json(fake.root / 'transport.json', data)
                store.atomic_json(op['evidence']['marker'], marker)
                store.atomic_json(launch.journal(attempt), journal)
                launch.save_registry(attempt, {'agents': [copy.deepcopy(record)], 'runs': [], 'fenced': True})
                def transport(*args, **kwargs):
                    if args[:2] == ('orchestration', 'request-show'):
                        return {'state': 'absent'}
                    return fake(*args, **kwargs)
                with patch.object(launch, 'orca', side_effect=transport) as call:
                    if kind == 'live':
                        # A still-live launch may resume its original terminal, but never replace it.
                        self.assertEqual(launch.reconcile_attempt(attempt), [])
                        self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'ready')
                    else:
                        self.assertTrue(launch.reconcile_attempt(attempt))
                        self.assertFalse(launch.stop_attempt(attempt))
                self.assertFalse(any(c.args[:2] in (('terminal', 'create'), ('orchestration', 'worker-start'))
                                     for c in call.call_args_list))

    def test_crash_after_replacement_record_resumes_same_resource_slot(self):
        attempt, fake, record, op = self.lost()
        with patch.object(launch, 'orca', side_effect=fake):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            with patch.object(launch, '_launch', side_effect=RuntimeError('crash')):
                with self.assertRaisesRegex(RuntimeError, 'crash'):
                    launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
            replacement = launch.registry(attempt)['agents'][1]['id']
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
        self.assertEqual(len(launch.registry(attempt)['agents']), 2)
        self.assertEqual(launch.registry(attempt)['agents'][1]['id'], replacement)

    def test_normal_reconcile_after_replacement_reservation_crash(self):
        attempt, fake, record, op = self.lost()
        state = runner.load_state()
        state['current'] = {'item': 'X', 'attempt_dir': str(attempt)}
        runner.save_state(state)
        with patch.object(launch, 'orca', side_effect=fake), patch.object(runner, 'orca', side_effect=fake):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            with patch.object(launch, '_launch', side_effect=RuntimeError('crash')):
                with self.assertRaisesRegex(RuntimeError, 'crash'):
                    launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
            runner.recover(state)
        self.assertEqual(state['recovery_issues'], [])
        self.assertEqual(state['current']['incomplete_checks'], [])
        self.assertEqual(len(launch.registry(attempt)['agents']), 2)
        self.assertEqual(store.read_json(fake.root / 'transport.json')['workers'], {})

    def test_second_loss_exhausts_budget(self):
        attempt, fake, record, op = self.lost()
        def interrupted(*args, **kwargs):
            if args[:2] == ('terminal', 'wait'):
                raise RuntimeError('second loss')
            return fake(*args, **kwargs)
        with patch.object(launch, 'orca', side_effect=interrupted):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            with self.assertRaisesRegex(RuntimeError, 'second loss'):
                launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
        new = launch.registry(attempt)['agents'][1]
        journal = store.read_json(launch.journal(attempt))
        marker_path = journal[new['id'] + '-terminal']['evidence']['marker']
        marker = store.read_json(marker_path)
        marker['process_stamp'] = 'departed-again'
        store.atomic_json(marker_path, marker)
        data = store.read_json(fake.root / 'transport.json')
        data['terminals'][new['terminal']].update(orphaned=True, connected=False, writable=False,
                                               exitCause={'kind': 'operator_close'})
        store.atomic_json(fake.root / 'transport.json', data)
        with patch.object(launch, 'orca', side_effect=fake):
            self.assertIn('budget exhausted', ' '.join(launch.reconcile_attempt(attempt)))
        self.assertEqual(len(launch.registry(attempt)['agents']), 2)

    def test_unknown_replacement_create_never_allocates_third_resource(self):
        attempt, fake, record, op = self.lost()
        def lost_create(*args, **kwargs):
            if args[:2] == ('terminal', 'create'):
                raise RuntimeError('create result unknown')
            return fake(*args, **kwargs)
        with patch.object(launch, 'orca', side_effect=lost_create):
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
            self.assertTrue(launch.reconcile_attempt(attempt))
            with self.assertRaisesRegex(RuntimeError, 'fenced'):
                launch.resume_ready_launches(attempt, self.root, permitted=lambda: True)
        self.assertEqual(len(launch.registry(attempt)['agents']), 2)
