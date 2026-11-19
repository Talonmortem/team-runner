import uuid
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

    def test_orphaned_mailbox_projection_requires_process_and_inventory_evidence(self):
        old = {**self.fresh, 'handle': 'old', 'incarnationId': 'old-inc', 'ptyId': 'old-pty',
               'orphaned': True, 'connected': False, 'writable': False}
        journal = runner.launcher.journal(self.state['mailbox_dir'])
        launch = {'args': ['terminal', 'create'], 'status': 'completed', 'result': {'terminal': old}}
        store.atomic_json(journal, {'mailbox': launch})

        def transport(*args, **kwargs):
            if args == ('terminal', 'show', '--terminal', 'old'):
                return {'terminal': old}
            return self.transport(*args, **kwargs)

        with patch.object(runner.launcher, 'orca', side_effect=transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh) as create, \
                patch.object(runner, 'effect', side_effect=self.bind), \
                patch('team_runner.runner_operations.missing_terminal_evidence', side_effect=[None, {'terminal': old}]) as proof:
            self.assertEqual(runner.ensure_run(self.state), 'parent')
            create.assert_not_called()
            self.assertEqual(runner.ensure_run(self.state), 'parent')
            create.assert_called_once()
        self.assertEqual(proof.call_args.args[0], launch)
        self.assertEqual(self.state['mailbox'], 'new')

    def test_recorded_replacement_recovers_after_binding_before_state_save(self):
        self.owner = 'new'
        store.atomic_json(runner.launcher.journal(self.state['mailbox_dir']), {
            'replacement-old': {'status': 'completed', 'result': {'terminal': self.fresh}}})
        with patch.object(runner.launcher, 'orca', side_effect=self.transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh), \
                patch.object(runner, 'effect', side_effect=self.bind):
            runner.ensure_run(self.state)
        self.assertEqual(self.state['mailbox'], 'new')

    def test_confirmed_refusal_allows_new_command_after_mailbox_replacement(self):
        self.state['current']['messages'] = [{'id': 'q', 'type': 'question', 'run_id': 'parent'}]
        runner.save_state(self.state)
        original_effect = runner.effect
        sent = []

        def transport(*args, **kwargs):
            if args[:2] == ('orchestration', 'reply'):
                sent.append((kwargs['mailbox'], args[-1]))
                if kwargs['mailbox'] == 'old':
                    exc = RuntimeError('stable pane required')
                    exc.receipt = {'ok': False, 'error': {'code': 'stable_pane_required'}}
                    raise exc
                return {'message': {'id': 'answer'}}
            return self.transport(*args, **kwargs)

        def effect(st, key, *args, **kwargs):
            if args[:2] == ('orchestration', 'run-use'):
                return self.bind(st, key, *args, **kwargs)
            return original_effect(st, key, *args, **kwargs)

        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        data = {'message_id': 'q', 'run_id': 'parent', 'text': 'answer'}
        with patch.object(runner.launcher, 'orca', side_effect=transport), \
                patch.object(runner.launcher, 'create_terminal', return_value=self.fresh), \
                patch.object(runner, 'effect', side_effect=effect):
            refused = runner.submit_control('reply', data, first)
            self.assertEqual(refused['status'], 'error')
            self.assertFalse(runner.load_state()['current']['messages'][0].get('closed'))
            runner.ensure_run(runner.load_state())
            self.assertEqual(runner.submit_control('reply', data, first), refused)
            accepted = runner.submit_control('reply', data, second)
            self.assertEqual(accepted['status'], 'done')
            self.assertEqual(runner.submit_control('reply', data, second), accepted)

        original_key = 'reply:parent:q'
        journal = store.read_json(runner.state_dir / 'operations.json')
        self.assertEqual(journal[original_key]['status'], 'rejected')
        retry = journal[original_key + ':' + second]
        self.assertEqual(retry['evidence']['previous'], original_key)
        self.assertEqual(sent, [('old', journal[original_key]['request_id']), ('new', retry['request_id'])])
        self.assertNotEqual(sent[0][1], sent[1][1])
        self.assertTrue(runner.load_state()['current']['messages'][0]['answered'])
