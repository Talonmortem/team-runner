import subprocess
from unittest.mock import Mock, patch

from team_runner import runner_operations as operations
from team_runner.service import watch_runner_exit
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
