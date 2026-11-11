from unittest.mock import patch
from test_goal_runner import Fixture, runner, store


class MailboxRecovery(Fixture):
    def setUp(self):
        super().setUp()
        self.attempt_dir, self.state = self.attempt()
        self.state.update(mailbox='old', mailbox_incarnation='old-inc',
                          mailbox_dir=str(self.root / 'mailboxes/main'))
        runner.save_state(self.state)
        store.atomic_json(self.attempt_dir / 'agents.json', {'agents': [
            {'role': 'coordinator', 'mailbox': 'old', 'run_id': 'parent'},
            {'role': 'tester', 'mailbox': 'child', 'run_id': 'child-run'}], 'runs': []})
        self.owner = 'old'
        self.fresh = {'handle': 'new', 'incarnationId': 'new-inc', 'worktreeId': 'fixture',
                      'tabId': 'tab', 'leafId': 'pane'}

    def transport(self, *args, **kwargs):
        if args[:2] == ('terminal', 'show'):
            if args[-1] == 'old':
                exc = RuntimeError('stale')
                exc.receipt = {'error': {'code': 'terminal_handle_stale'}}
                raise exc
            return {'terminal': self.fresh}
        if args[:2] in (('orchestration', 'run-show'), ('orchestration', 'run-current')):
            return {'run': {'id': 'parent', 'coordinator_handle': self.owner, 'legacy': 0}}
        raise AssertionError(args)

    def bind(self, st, key, *args, **kwargs):
        self.assertEqual(args, ('orchestration', 'run-use', '--id', 'parent'))
        self.assertEqual(kwargs['mailbox'], 'new')
        self.owner = 'new'
        return {'run': {'id': 'parent'}}

    def test_stale_rebind_preserves_run_snapshot_and_child_mailbox(self):
        before = (self.attempt_dir / 'config.json').read_bytes()
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh) as create, \
                patch.object(runner, 'effect', side_effect=self.bind):
            self.assertEqual(runner.ensure_run(self.state), 'parent')
            runner.ensure_run(self.state)
            self.assertEqual(create.call_count, 1)
        self.assertEqual(self.state['mailbox'], 'new')
        agents = runner.launcher.registry(self.attempt_dir)['agents']
        self.assertEqual([a['mailbox'] for a in agents], ['new', 'child'])
        self.assertEqual((self.attempt_dir / 'config.json').read_bytes(), before)

    def test_foreign_owner_blocks_creation(self):
        self.owner = 'foreign'
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal') as create:
            with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                runner.ensure_run(self.state)
            create.assert_not_called()

    def test_unknown_operations_block_recovery(self):
        store.atomic_json(self.root / 'operations.json', {'effect': {'status': 'unknown'}})
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal') as create:
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                runner.ensure_run(self.state)
            create.assert_not_called()

    def test_unconfirmed_rebind_does_not_publish_new_mailbox(self):
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh), \
                patch.object(runner, 'effect', side_effect=RuntimeError('unknown bind')):
            with self.assertRaisesRegex(RuntimeError, 'unknown bind'):
                runner.ensure_run(self.state)
        self.assertEqual(runner.load_state()['mailbox'], 'old')

    def test_mismatched_replacement_identity_is_rejected(self):
        self.fresh['worktreeId'] = 'foreign'
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh), \
                patch.object(runner, 'effect') as effect:
            with self.assertRaisesRegex(RuntimeError, 'identity unconfirmed'):
                runner.ensure_run(self.state)
            effect.assert_not_called()

    def test_live_mismatch_is_not_replaced(self):
        with patch.object(runner.launcher, 'orca', return_value={'terminal': self.fresh}), \
                patch.object(runner.launcher, 'create_terminal') as create:
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                runner.ensure_run(self.state)
            create.assert_not_called()

    def test_recorded_replacement_recovers_after_binding_before_state_save(self):
        self.owner = 'new'
        store.atomic_json(runner.launcher.journal(self.state['mailbox_dir']), {
            'replacement-old': {'status': 'completed', 'result': {'terminal': self.fresh}}})
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh), \
                patch.object(runner, 'effect', side_effect=self.bind):
            runner.ensure_run(self.state)
        self.assertEqual(self.state['mailbox'], 'new')
