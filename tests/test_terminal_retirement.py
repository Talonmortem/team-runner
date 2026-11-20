import subprocess
import uuid
from unittest.mock import Mock, patch

from team_runner import runner_operations as operations
from team_runner.runner_launch import Launcher
from team_runner.service import watch_runner_exit
import test_agent_cli
from test_goal_runner import Fixture, launch, runner, store


class TerminalRetirement(Fixture):
    terminal = {'handle': 'owned', 'incarnationId': 'exact', 'ptyId': 'pty-owned',
                'worktreeId': 'fixture', 'tabId': 'tab', 'leafId': 'leaf'}
    neighbor = {'handle': 'user', 'incarnationId': 'user-incarnation',
                'ptyId': 'pty-user', 'worktreeId': 'fixture', 'tabId': 'tab', 'leafId': 'other'}

    def receipt(self, directory, key):
        marker = directory / (key + '.entry.json')
        nonce = key + ':' + str(directory)
        store.atomic_json(marker, {'nonce': nonce, 'terminal': self.terminal,
                                   'pid': 405, 'process_stamp': 'exact-stamp'})
        journal = launch.journal(directory)
        store.atomic_json(journal, {key: {'args': ['terminal', 'create'], 'status': 'completed',
                                        'result': {'terminal': self.terminal},
                                        'evidence': {'marker': str(marker), 'nonce': nonce}}})
        return journal, marker, nonce

    def test_timeout_leaves_close_unconfirmed_and_never_replays(self):
        path, _, _ = self.receipt(self.root, 'agent-terminal')
        call = Mock(side_effect=[{'terminal': self.terminal}, subprocess.TimeoutExpired('terminal close', 1)])
        args = (path, 'agent-close', call, 'owned', 'exact', 'fixture')
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'close receipt unconfirmed'):
                operations.close_terminal(*args)
        self.assertEqual(store.read_json(path)['agent-close']['status'], 'unknown')
        self.assertEqual([c.args for c in call.call_args_list if c.args[:2] == ('terminal', 'close')],
                         [('terminal', 'close', '--terminal', 'owned')])

    def test_refuses_foreign_terminal_under_reused_handle(self):
        path, _, _ = self.receipt(self.root, 'agent-terminal')
        call = Mock(return_value={'terminal': {**self.neighbor, 'handle': 'owned'}})
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            operations.close_terminal(path, 'agent-close', call, 'owned', 'exact', 'fixture')
        call.assert_called_once_with('terminal', 'show', '--terminal', 'owned', mailbox=None)

    def test_missing_nested_mailbox_keeps_other_incarnation_with_same_pty(self):
        path, _, _ = self.receipt(self.root, 'mailbox')
        other = {**self.neighbor, 'ptyId': self.terminal['ptyId'], 'leafId': self.terminal['leafId']}
        listing = {'terminals': [other], 'truncated': False,
                   'hostScope': {'hostIds': ['local'], 'omittedHostIds': []}}
        call = Mock(side_effect=[RuntimeError('stale'), listing])
        with patch('team_runner.project_services.departed', return_value=True):
            proof = operations.close_terminal(path, 'mailbox-close', call, 'owned', 'exact', 'fixture',
                                              allow_absent=True)
        self.assertEqual(proof['terminal'], self.terminal)
        self.assertEqual([c.args[:2] for c in call.call_args_list], [('terminal', 'show'), ('terminal', 'list')])
        self.assertNotIn('mailbox-close', store.read_json(path))

    def test_close_reply_requires_process_exit_and_ignores_unrelated_hosts(self):
        path, _, _ = self.receipt(self.root, 'agent-terminal')
        call = Mock(side_effect=[{'terminal': self.terminal},
                                {'close': {'handle': 'owned', 'tabId': 'tab',
                                           'ptyKilled': True}}])
        with patch('team_runner.project_services.departed', return_value=False), \
                patch('team_runner.runner_operations.time.monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, 'process exit unconfirmed'):
                operations.close_terminal(path, 'agent-close', call, 'owned', 'exact', 'fixture')
        self.assertEqual(store.read_json(path)['agent-close']['status'], 'unknown')
        call.side_effect = None
        call.return_value = {'terminals': [self.neighbor], 'truncated': False,
                             'hostScope': {'hostIds': ['local'], 'omittedHostIds': ['ssh:unrelated']}}
        with patch('team_runner.project_services.departed', return_value=True):
            self.assertEqual(operations.reconcile(path, call), [])
        self.assertEqual(store.read_json(path)['agent-close']['stage'], 'close_verified')
        self.assertEqual(call.call_args_list[1].args, ('terminal', 'close', '--terminal', 'owned'))

    def test_runner_and_mailbox_retire_after_runner_process_exit(self):
        path, marker, nonce = self.receipt(self.root / 'startup', 'runner-run')
        store.atomic_json(self.root / 'startup.json', {'status': 'idle', 'nonce': 'run'})
        events = []

        def departed(_marker):
            events.append('process exited' if events else 'process running')
            return len(events) > 1

        def close(path, key, *_args):
            events.append(key)
            return {'terminal': self.terminal}

        with patch('team_runner.project_services.departed', side_effect=departed), \
                patch('team_runner.service.time.sleep'), \
                patch.object(runner, 'close_main_mailbox', side_effect=lambda st: events.append('mailbox')), \
                patch.object(operations, 'close_terminal', side_effect=close):
            watch_runner_exit(runner, marker, nonce)
        self.assertEqual(events, ['process running', 'process exited', 'runner-run-close', 'mailbox'])
        self.assertEqual(store.read_json(self.root / 'startup.json')['close_evidence'],
                         {'terminal': self.terminal})

    def test_live_target_after_ack_keeps_cleanup_unconfirmed(self):
        path, _, _ = self.receipt(self.root, 'agent-terminal')
        listing = {'terminals': [self.terminal, self.neighbor], 'truncated': False,
                   'hostScope': {'hostIds': ['local'], 'omittedHostIds': []}}
        call = Mock(side_effect=[{'terminal': self.terminal},
                                {'close': {'handle': 'owned', 'tabId': 'tab', 'ptyKilled': True}},
                                listing])
        with patch('team_runner.project_services.departed', return_value=True), \
                patch('team_runner.runner_operations.time.monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, 'terminal or process exit unconfirmed'):
                operations.close_terminal(path, 'agent-close', call, 'owned', 'exact', 'fixture')
        self.assertEqual(store.read_json(path)['agent-close']['status'], 'unknown')


class UnmarkedMailboxStop(Fixture):
    command = test_agent_cli.AgentCLI.command
    scope = test_agent_cli.AgentCLI.scope
    terminal = TerminalRetirement.terminal

    def prepare(self):
        self.a = runner
        attempt, st = self.attempt()
        st['current']['failed'] = True
        runner.save_state(st)
        launch.save_registry(attempt, {'agents': [{'status': 'released', 'dispatch_id': 'owned'}],
                                      'runs': [{'status': 'ready', 'run_id': 'child', 'mailbox': 'owned',
                                                'incarnation': 'exact'}], 'fenced': False})
        objective = 'goal-runner child ' + attempt.name
        journal = launch.journal(attempt)
        store.atomic_json(journal, {
            'mailbox': {'args': ['terminal', 'create', '--worktree', 'id:fixture'], 'status': 'completed',
                        'attempt': str(attempt), 'result': {'terminal': self.terminal},
                        'evidence': {'marker': str(attempt / 'mailbox.entry.json'), 'nonce': f'mailbox:{attempt}'}},
            'child-run': {'args': ['orchestration', 'run-create', '--objective', objective],
                          'status': 'completed', 'attempt': str(attempt), 'mailbox': 'owned',
                          'result': {'run': {'id': 'child', 'coordinator_handle': 'owned'}}},
        })
        return attempt, journal

    def control(self, action):
        return self.command(action, '--json', '--request-id', str(uuid.uuid4()), *self.scope())

    def listing(self, *rows, truncated=False):
        return {'terminals': list(rows), 'truncated': truncated,
                'hostScope': {'hostIds': ['local'], 'omittedHostIds': []}}

    def orca_fixture(self, *, before=None, after=None, observed=None, acknowledgement=None):
        self.closes = []
        before = self.listing({**self.terminal, 'tabId': 'synthetic'}) if before is None else before
        after = self.listing(TerminalRetirement.neighbor) if after is None else after
        observed = self.terminal if observed is None else observed
        acknowledgement = ({'close': {'handle': 'owned', 'tabId': 'tab', 'ptyKilled': True}}
                           if acknowledgement is None else acknowledgement)

        def call(*args, **kwargs):
            if args[:2] == ('worktree', 'show'):
                return {'worktree': {'id': 'fixture', 'instanceId': 'instance',
                                     'hostId': 'local', 'path': str(runner.repo)}}
            if args[:2] == ('terminal', 'list'):
                return after if self.closes else before
            if args[:2] == ('terminal', 'show'):
                self.assertEqual(args[2:], ('--terminal', 'owned'))
                return {'terminal': observed}
            if args[:2] == ('terminal', 'close'):
                self.closes.append(args[2:])
                if isinstance(acknowledgement, Exception):
                    raise acknowledgement
                return acknowledgement
            self.fail(f'unexpected Orca call {args}')

        return call

    def test_guarded_stop_saves_proof_then_retry_uses_saved_settings(self):
        attempt, journal = self.prepare()
        saved = store.read_json(attempt / 'config.json')
        with patch.object(Launcher, 'orca', side_effect=self.orca_fixture()):
            self.assertEqual(self.control('stop')[1]['status'], 'succeeded')
            self.assertEqual(self.closes, [('--terminal', 'owned')])
            close = store.read_json(journal)['mailbox-close']
            self.assertEqual(close['stage'], 'close_verified')
            self.assertEqual(close['result']['close'], {'handle': 'owned', 'tabId': 'tab', 'ptyKilled': True})
            self.assertEqual(launch.registry(attempt)['runs'][0]['status'], 'closed')
            self.assertEqual(self.control('retry')[1]['status'], 'succeeded')
        self.assertEqual(runner.load_state()['current']['retry_config'], saved)
        self.assertEqual(runner.load_state()['current']['attempt'], 2)

    def test_automatic_stop_keeps_marker_requirement(self):
        attempt, journal = self.prepare()
        with patch.object(Launcher, 'orca') as orca:
            self.assertFalse(launch.stop_attempt(attempt))
            orca.assert_not_called()
        self.assertNotIn('mailbox-close', store.read_json(journal))

    def test_preflight_blocks_terminal_replacement_and_incomplete_inventory(self):
        cases = [
            {'observed': {**self.terminal, 'incarnationId': 'replacement'}},
            {'observed': {**self.terminal, 'tabId': 'replacement-tab'}},
            {'before': self.listing({**self.terminal, 'handle': 'foreign'})},
            {'before': self.listing(self.terminal, truncated=True)},
        ]
        for options in cases:
            with self.subTest(options=options):
                _, journal = self.prepare()
                with patch.object(Launcher, 'orca', side_effect=self.orca_fixture(**options)):
                    self.assertEqual(self.control('stop')[1]['status'], 'unknown')
                self.assertEqual(self.closes, [])
                self.assertNotIn('mailbox-close', store.read_json(journal))

    def test_unsettled_operations_block_fallback(self):
        _, journal = self.prepare()
        ops = store.read_json(journal)
        ops['unconfirmed'] = {'status': 'unknown'}
        store.atomic_json(journal, ops)
        with patch.object(Launcher, 'orca') as orca:
            self.assertEqual(self.control('stop')[1]['status'], 'unknown')
            orca.assert_not_called()
        self.assertNotIn('mailbox-close', store.read_json(journal))

    def test_fallback_requires_previously_settled_executors(self):
        attempt, journal = self.prepare()
        reg = launch.registry(attempt)
        reg['agents'][0]['status'] = 'active'
        launch.save_registry(attempt, reg)
        with patch.object(Launcher, 'liveness', return_value='exited'), patch.object(Launcher, 'orca') as orca:
            self.assertEqual(self.control('stop')[1]['status'], 'unknown')
            orca.assert_not_called()
        self.assertNotIn('mailbox-close', store.read_json(journal))

    def test_foreign_child_run_receipt_blocks_fallback(self):
        _, journal = self.prepare()
        ops = store.read_json(journal)
        ops['child-run']['result']['run']['coordinator_handle'] = 'foreign'
        store.atomic_json(journal, ops)
        with patch.object(Launcher, 'orca', side_effect=self.orca_fixture()):
            receipt = self.control('stop')[1]
            self.assertEqual(receipt['status'], 'unknown')
            self.assertIn('child Run creation receipt', receipt['error']['message'])
        self.assertEqual(self.closes, [])
        self.assertNotIn('mailbox-close', store.read_json(journal))

    def test_missing_pty_stop_ack_and_close_timeout_block_retry_without_replay(self):
        for acknowledgement in ({'close': {'handle': 'owned', 'tabId': 'tab', 'ptyKilled': False}},
                                subprocess.TimeoutExpired('terminal close', 1)):
            with self.subTest(acknowledgement=acknowledgement):
                attempt, journal = self.prepare()
                with patch.object(Launcher, 'orca', side_effect=self.orca_fixture(acknowledgement=acknowledgement)), \
                        patch.object(operations.time, 'monotonic', side_effect=[0, 3]):
                    self.assertEqual(self.control('stop')[1]['status'], 'unknown')
                    self.assertEqual(self.control('reconcile')[1]['status'], 'unknown')
                    with self.assertRaisesRegex(ValueError, 'stop unconfirmed'):
                        runner.apply_control(runner.load_state(), 'retry', {})
                    self.assertEqual(self.closes, [('--terminal', 'owned')])
                self.assertEqual(store.read_json(journal)['mailbox-close']['status'], 'unknown')
                self.assertEqual(launch.registry(attempt)['runs'][0]['status'], 'ready')
                self.assertEqual(runner.load_state()['stop_status'], 'unknown')

    def test_reconcile_saved_ack_after_incomplete_inventory(self):
        attempt, journal = self.prepare()
        residual = {**TerminalRetirement.neighbor, 'ptyId': self.terminal['ptyId']}
        with patch.object(Launcher, 'orca', side_effect=self.orca_fixture(after=self.listing(truncated=True))), \
                patch.object(operations.time, 'monotonic', side_effect=[0, 3]):
            self.assertEqual(self.control('stop')[1]['status'], 'unknown')
        self.assertTrue(store.read_json(journal)['mailbox-close']['result']['close']['ptyKilled'])
        with patch.object(Launcher, 'orca', return_value=self.listing(residual)) as orca:
            self.assertTrue(operations.reconcile(journal, orca))
        with patch.object(Launcher, 'orca', return_value=self.listing()) as orca:
            self.assertEqual(self.control('reconcile')[1]['status'], 'succeeded')
            self.assertEqual([c.args[:2] for c in orca.call_args_list], [('terminal', 'list')])
        self.assertEqual(self.closes, [('--terminal', 'owned')])
        self.assertEqual(launch.registry(attempt)['runs'][0]['status'], 'closed')
        self.assertEqual(runner.load_state()['stop_status'], 'stopped')
