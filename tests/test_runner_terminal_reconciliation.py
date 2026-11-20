"""Runner terminal observation uses disposable state and a fake Orca transport."""
import os
import subprocess
import unittest
import uuid
from unittest.mock import Mock, patch

import test_platform as fixtures
from team_runner import goal_execute, goal_intent, goal_scope, recovery, runner_terminal
from team_runner import runner_operations as operations
from team_runner.protocol import ContractError
from team_runner.runner_entry import boot_id, process_stamp
from team_runner.runner_store import atomic_json, read_json


class RunnerTerminalReconciliation(unittest.TestCase):
    def setUp(self):
        fixtures.Platform.setUp(self)
        self.state_dir = self.a.state_dir
        self.startup = self.state_dir / 'startup.json'
        self.launch = self.state_dir / 'launch.json'
        self.journal = self.a.launcher.journal(self.state_dir / 'startup')
        self.nonce = str(uuid.uuid4())
        self.key = 'runner-' + self.nonce
        self.absence = self.key + '-absence'
        self.marker = self.journal.parent / (self.key + '.entry.json')
        self.identity = self.a.context.identity()
        self.worktree = self.a.context.orca_id
        self.terminal = {'handle': 'runner-terminal', 'incarnationId': 'runner-incarnation',
                         'ptyId': 'runner-pty', 'worktreeId': self.worktree}
        self.process = {'pid': os.getpid(), 'process_stamp': 'previous-incarnation', 'boot_id': boot_id()}
        self.marker_data = {'nonce': self.key + ':' + str(self.journal.parent.resolve()),
                            'terminal': self.terminal, **self.process}
        self.create = {'status': 'completed',
                       'args': ['terminal', 'create', '--worktree', 'id:' + self.worktree],
                       'evidence': {'marker': str(self.marker), 'nonce': self.marker_data['nonce']},
                       'result': {'terminal': self.terminal}}
        self.idle = {'status': 'idle', 'nonce': self.nonce}
        self.request = {'nonce': self.nonce, 'identity': self.identity}
        atomic_json(self.startup, self.idle)
        atomic_json(self.launch, self.request)
        atomic_json(self.journal, {self.key: self.create})
        atomic_json(self.marker, self.marker_data)
        self.fixture = self.a.launcher.orca
        self.a.launcher.orca = Mock(side_effect=self.transport)
        self.listing = {'terminals': [], 'truncated': False,
                        'hostScope': {'hostIds': ['local'], 'omittedHostIds': []}}
        self.error = None

    def transport(self, group, command, *args, **kwargs):
        if (group, command) == ('worktree', 'show'):
            return self.fixture(group, command, *args, **kwargs)
        self.assertEqual((group, command, *args), ('terminal', 'list', '--worktree', 'id:' + self.worktree))
        self.assertEqual(kwargs['timeout'], runner_terminal.INVENTORY_TIMEOUT)
        if self.error:
            raise self.error
        return self.listing

    def test_retries_orca_in_waiting_gate_without_touching_queue_or_mailbox(self):
        recovery.configure(self.a, True)
        recovery.set_intent(self.a, 'running')
        state = {'current': {'item': 'T1', 'gate_id': 'pending', 'attempt': 1},
                 'mailbox': 'untouched', 'runner_status': 'idle'}
        self.a.save_state(state)
        incident = {'target': recovery.target(state), 'status': 'needs_action'}
        incident_path = self.state_dir / 'recovery.json'
        atomic_json(incident_path, incident)
        now = 100
        supervisor = recovery.Supervisor(self.catalog, self.a.context.project_id, clock=lambda: now)
        self.error = subprocess.TimeoutExpired('orca', 5)
        with patch('team_runner.service.start_runner') as start:
            supervisor.tick(self.a)
            self.assertEqual(read_json(self.startup), self.idle)
            self.assertNotIn(self.absence, read_json(self.journal))
            self.error = None
            self.a.launcher.orca.reset_mock()
            now += recovery.TERMINAL_CHECK_INTERVAL - 1
            supervisor.tick(self.a)
            self.a.launcher.orca.assert_not_called()
            now += 1
            supervisor.tick(self.a)
            start.assert_not_called()
        row = read_json(self.journal)[self.absence]
        self.assertEqual(row['evidence'], {'identity': self.identity, 'nonce': self.nonce, 'create_key': self.key})
        self.assertEqual(row['result']['process'], self.process)
        self.assertEqual(read_json(self.startup)['close_evidence'], row['result'])
        self.assertEqual(self.a.load_state(), state)
        self.assertEqual(read_json(incident_path), incident)
        self.assertFalse((self.state_dir / 'commands').exists())

    def test_visible_or_incomplete_inventory_proves_nothing(self):
        for listing in (
            dict(self.listing, terminals=[self.terminal]),
            dict(self.listing, terminals=[dict(self.terminal, handle='other')]),
            dict(self.listing, terminals=[dict(self.terminal, incarnationId='other')]),
            dict(self.listing, truncated=True),
            dict(self.listing, hostScope={'hostIds': ['local'], 'omittedHostIds': ['remote']}),
            dict(self.listing, terminals=[None]),
            dict(self.listing, terminals=[{}]),
            {'terminals': []},
        ):
            with self.subTest(listing=listing):
                self.listing = listing
                runner_terminal.reconcile(self.a)
                self.assertEqual(read_json(self.startup), self.idle)
                self.assertEqual(read_json(self.journal), {self.key: self.create})

    def test_mismatched_or_live_origin_skips_transport(self):
        for path, value in (
            (self.launch, dict(self.request, nonce=str(uuid.uuid4()))),
            (self.launch, dict(self.request, identity={})),
            (self.marker, dict(self.marker_data, nonce='different')),
            (self.marker, dict(self.marker_data, terminal=dict(self.terminal, worktreeId='foreign'))),
            (self.marker, dict(self.marker_data, terminal=dict(self.terminal, ptyId='foreign'))),
            (self.marker, dict(self.marker_data, process_stamp=process_stamp(os.getpid()))),
            (self.state_dir / 'runner-process.json', dict(self.process, identity=self.identity,
                                                          nonce=self.nonce, pid=123)),
        ):
            with self.subTest(path=path, value=value):
                before = read_json(path)
                atomic_json(path, value)
                runner_terminal.reconcile(self.a)
                self.a.launcher.orca.assert_not_called()
                self.assertEqual(read_json(self.startup), self.idle)
                if before is None:
                    path.unlink()
                else:
                    atomic_json(path, before)

    def test_interrupted_projection_recovers_from_journal_without_orca(self):
        atomic_json(self.startup, dict(self.idle, cleanup_error='Orca unavailable'))
        with patch.object(runner_terminal, '_project', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                runner_terminal.reconcile(self.a)
        rows = read_json(self.journal)
        self.assertIn(self.absence, rows)
        self.assertIn('cleanup_error', read_json(self.startup))
        self.a.launcher.orca.reset_mock()
        runner_terminal.reconcile(self.a)
        runner_terminal.reconcile(self.a)
        self.assertEqual(read_json(self.startup), dict(self.idle, close_evidence=rows[self.absence]['result']))
        self.assertEqual(read_json(self.journal), rows)
        self.a.launcher.orca.assert_not_called()
        self.assertEqual(operations.reconcile(self.journal, self.a.launcher.orca), [])
        self.a.launcher.orca.assert_not_called()

    def test_existing_verified_close_receipt_is_restored_without_inventory(self):
        close_key = self.key + '-close'
        receipt = {'status': 'completed', 'stage': 'close_verified',
                   'evidence': {'launch': self.key}, 'result': {'terminal': self.terminal}}
        rows = {self.key: self.create, close_key: receipt}
        atomic_json(self.journal, rows)
        runner_terminal.reconcile(self.a)
        self.assertEqual(read_json(self.startup)['close_evidence'], receipt['result'])
        self.assertEqual(read_json(self.journal), rows)
        self.a.launcher.orca.assert_not_called()

    def test_new_nonce_during_inventory_does_not_inherit_proof(self):
        new_nonce = str(uuid.uuid4())
        new_startup = {'status': 'idle', 'nonce': new_nonce}

        def replace(*args, **kwargs):
            result = self.transport(*args, **kwargs)
            if args[:2] == ('terminal', 'list'):
                atomic_json(self.launch, dict(self.request, nonce=new_nonce))
                runner_terminal.mark_idle(self.state_dir, new_nonce)
            return result

        atomic_json(self.startup, dict(self.idle, cleanup_error='Orca unavailable'))
        self.a.launcher.orca.side_effect = replace
        runner_terminal.reconcile(self.a)
        self.assertEqual(read_json(self.startup), new_startup)
        self.assertEqual(read_json(self.journal), {self.key: self.create})

    def test_idle_transition_preserves_same_nonce_cleanup_fields(self):
        starting = dict(self.idle, status='running', cleanup_error='interrupted',
                        close_evidence={'process': self.process})
        atomic_json(self.startup, starting)
        runner_terminal.mark_idle(self.state_dir, self.nonce)
        self.assertEqual(read_json(self.startup), dict(starting, status='idle'))

    def test_scope_replacement_requires_bound_observation_and_keeps_unknown_close_fenced(self):
        goal = 'feat/goal-fixture'
        feature_path = self.a.repo / goal / 'feat-one/Tasks.json'
        feature = read_json(feature_path)
        feature['queue'][0]['status'] = 'completed'
        atomic_json(feature_path, feature)
        opts = {'goal': goal, 'items': 'feature/T1', 'mode': 'auto', 'final': False}
        goal_intent.authorize(self.a, opts, str(uuid.uuid4()))
        goal_intent.change(self.a, desired_state='paused')
        intent = goal_intent.read(self.a)
        opts = intent['options']
        scope = {k: intent[k] for k in ('generation', 'epoch')}
        atomic_json(self.launch, dict(self.request, options=opts, authorization=scope))
        (self.state_dir / ('launch-' + self.nonce + '.claim')).touch()
        unknown_path = goal_execute._shared_path(self.a, scope['generation'], scope['epoch'] - 1, 1)
        atomic_json(unknown_path, {**scope, 'epoch': scope['epoch'] - 1, 'identity': self.identity,
                                  'options': opts, 'service_epoch': 1, 'status': 'unknown', 'phase': 'launch'})
        state = {'goal': goal, 'history': [{'item': 'feature/T1', 'dispatch_id': 'accepted-t1',
                                            'outcome': 'succeeded', 'problems': []}]}
        close = {'status': 'unknown', 'evidence': {'launch': self.key, 'terminal': self.terminal}}
        close_key = self.key + '-close'
        atomic_json(self.journal, {self.key: self.create, close_key: close})
        runner_terminal.reconcile(self.a)
        rows = read_json(self.journal)
        self.assertEqual(rows.pop(close_key), close)
        self.assertIn(self.absence, rows)
        with self.assertRaisesRegex(ContractError, 'previous launch and operations'):
            goal_scope._settled(self.a, intent, state)
        atomic_json(self.journal, {self.key: self.create})
        with self.assertRaisesRegex(ContractError, 'previous execute'):
            goal_scope._settled(self.a, intent, state)
        bound = rows[self.absence]
        wrong = dict(bound, evidence=dict(bound['evidence'], nonce=str(uuid.uuid4())))
        atomic_json(self.journal, {self.key: self.create, self.absence: wrong})
        with self.assertRaisesRegex(ContractError, 'previous execute'):
            goal_scope._settled(self.a, intent, state)
        atomic_json(self.journal, rows)
        goal_scope._settled(self.a, intent, state)
