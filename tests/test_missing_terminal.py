"""Missing panes require positive process and exact ownership evidence."""
import os
from unittest.mock import patch
import test_closed_tester_recovery as closed
from test_goal_runner import Fixture, runner, launch, store
from team_runner import runner_operations as operations
from team_runner.runner_entry import process_stamp
from team_runner.project_services import departed


class MissingTerminal(Fixture):
    coordinator = closed.ClosedTesterRecovery.coordinator
    closed_tester = closed.ClosedTesterRecovery.closed_tester
    def missing(self):
        attempt, st, terminal, data = self.closed_tester()
        data['dispatch']['terminationReason'] = None
        data['worker'].update(state='abandoned', stage='terminal_missing')
        data['projection']['liveness']['verdict'] = 'unverifiable'
        data['observation'] = {'status': 'missing', 'exactWorker': False}
        marker = attempt / 'tester-terminal.entry.json'
        store.atomic_json(marker, {'nonce': 'nonce', 'terminal': terminal, 'pid': os.getpid(),
                                  'process_stamp': process_stamp(os.getpid())})
        journal = store.read_json(launch.journal(attempt))
        journal['tester-terminal'].update(args=['terminal', 'create'], evidence={'marker':str(marker), 'nonce':'nonce'})
        store.atomic_json(launch.journal(attempt), journal)
        agent = launch.registry(attempt)['agents'][1]
        return attempt, st, terminal, data, agent, journal['tester-terminal']

    def listing(self, terminals=None, **extra):
        return {'terminals': terminals or [], 'truncated': False,
                'hostScope': {'hostIds': ['local'], 'omittedHostIds': []}, **extra}

    def test_exact_missing_dead_process_is_settled_without_effect(self):
        attempt, st, terminal, data, agent, op = self.missing()
        with patch('team_runner.project_services.departed', return_value=True), patch.object(launch,'orca',return_value=self.listing()) as call:
            self.assertTrue(launch.confirm_missing_terminal(agent,data,attempt))
        self.assertTrue(agent['terminal_closed'])
        self.assertIn('work_incomplete',agent)
        self.assertEqual(call.call_args.args[:2], ('terminal','list'))
        self.assertEqual(call.call_count,1)

    def test_alive_process_or_incomplete_listing_never_proves_death(self):
        attempt, st, terminal, data, agent, op = self.missing()
        with patch.object(launch,'orca') as call:
            self.assertFalse(launch.confirm_missing_terminal(agent,data,attempt))
            call.assert_not_called()
        for listing in (self.listing([terminal]), self.listing(truncated=True), {'terminals':[]},
                        self.listing(hostScope={'hostIds':['local'],'omittedHostIds':['remote']})):
            with self.subTest(listing=listing), patch('team_runner.project_services.departed',return_value=True), patch.object(launch,'orca',return_value=listing):
                self.assertFalse(launch.confirm_missing_terminal(agent,data,attempt))

    def test_active_missing_dispatch_requires_explicit_stop_and_proven_death(self):
        attempt, st, terminal, data, agent, op = self.missing()
        data['dispatch']['status'] = 'dispatched'
        with patch.object(launch, 'lifecycle') as abandon, patch.object(launch, 'orca', return_value=self.listing()):
            self.assertFalse(launch.confirm_missing_terminal(agent, data, attempt))
            self.assertFalse(launch.confirm_missing_terminal(agent, data, attempt, stop_active=True))
            abandon.assert_not_called()
            with patch('team_runner.project_services.departed', return_value=True):
                self.assertTrue(launch.confirm_missing_terminal(agent, data, attempt, stop_active=True))
            abandon.assert_called_once_with(agent, 'worker-abandon')
        self.assertTrue(agent['terminal_closed'])

    def test_failed_abandon_does_not_mark_missing_terminal_closed(self):
        attempt, st, terminal, data, agent, op = self.missing()
        data['dispatch']['status'] = 'dispatched'
        with patch('team_runner.project_services.departed', return_value=True), patch.object(launch, 'orca', return_value=self.listing()), patch.object(launch, 'lifecycle', side_effect=RuntimeError('unknown effect')):
            with self.assertRaises(RuntimeError):
                launch.confirm_missing_terminal(agent, data, attempt, stop_active=True)
        self.assertFalse(agent.get('terminal_closed'))

    def test_wrong_owner_incarnation_and_marker_are_rejected(self):
        for kind in ('owner','incarnation','marker','unknown'):
            attempt, st, terminal, data, agent, op = self.missing()
            if kind=='owner':data['terminalResource']['ownerDispatchId']='foreign'
            if kind=='incarnation':data['dispatch']['processIncarnation']='foreign'
            if kind=='marker':
                marker=store.read_json(op['evidence']['marker']);marker['nonce']='foreign';store.atomic_json(op['evidence']['marker'],marker)
            if kind=='unknown':data['worker']['state']='outcome_unknown'
            with self.subTest(kind=kind),patch('team_runner.project_services.departed',return_value=True),patch.object(launch,'orca') as call:
                self.assertFalse(launch.confirm_missing_terminal(agent,data,attempt))
                call.assert_not_called()

    def test_unknown_operation_blocks_missing_terminal_cleanup(self):
        attempt, st, terminal, data, agent, op = self.missing()
        journal = store.read_json(launch.journal(attempt))
        journal['unknown'] = {'status': 'unknown', 'request_id': 'unknown'}
        store.atomic_json(launch.journal(attempt), journal)
        with patch.object(launch, 'orca') as call:
            with self.assertRaises((RuntimeError, ValueError)):
                launch.confirm_missing_terminal(agent, data, attempt)
            call.assert_not_called()

    def test_missing_mailbox_is_settled_without_close_replay(self):
        attempt, st, terminal, data, agent, op = self.missing()
        store.atomic_json(launch.journal(attempt),{'mailbox':op})
        run={'mailbox':terminal['handle'],'incarnation':terminal['incarnationId']}
        with patch('team_runner.project_services.departed',return_value=True),patch.object(launch,'orca',return_value=self.listing()) as call:
            launch.close_mailbox(attempt,run)
        self.assertIn('close_evidence',run)
        self.assertEqual(call.call_count,1)

    def test_changed_boot_is_positive_even_when_pid_and_stamp_reused(self):
        with patch('team_runner.project_services.boot_id',return_value='new'),patch('team_runner.project_services.process_stamp',return_value='same'):
            self.assertTrue(departed({'pid':os.getpid(),'process_stamp':'same','boot_id':'old'}))

    def test_reused_pane_with_new_identity_does_not_close_replacement(self):
        attempt, st, terminal, data, agent, op = self.missing()
        replacement = {**terminal, 'handle': 'replacement', 'incarnationId': 'new'}
        store.atomic_json(launch.journal(attempt), {'mailbox': op})
        run = {'mailbox': terminal['handle'], 'incarnation': terminal['incarnationId']}
        with patch('team_runner.project_services.departed', return_value=True), patch.object(launch, 'orca', return_value=self.listing([replacement])) as call:
            launch.close_mailbox(attempt, run)
        self.assertIn('close_evidence', run)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[:2], ('terminal', 'list'))

    def test_reused_pane_with_old_or_incomplete_identity_refuses_proof(self):
        attempt, st, terminal, data, agent, op = self.missing()
        for identity in ({'handle': terminal['handle'], 'incarnationId': 'new'},
                         {'handle': 'replacement', 'incarnationId': terminal['incarnationId']},
                         {'handle': None, 'incarnationId': 'new'},
                         {'handle': 'replacement', 'incarnationId': None}):
            replacement = {**terminal, **identity}
            with self.subTest(identity=identity), patch('team_runner.project_services.departed', return_value=True), patch.object(launch, 'orca', return_value=self.listing([replacement])):
                self.assertIsNone(operations.missing_terminal_evidence(op, launch.orca, terminal['worktreeId']))
