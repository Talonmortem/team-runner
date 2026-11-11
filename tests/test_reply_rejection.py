from unittest.mock import patch
from test_goal_runner import Fixture, runner, launch, store
from team_runner import runner_operations as operations


class ReplyRejection(Fixture):
    def test_inactive_reply_is_terminal_error_not_unknown(self):
        _, st = self.attempt()
        st['current']['messages'] = [{'id': 'q', 'type': 'question', 'run_id': 'parent'}]
        runner.save_state(st)
        exc = RuntimeError('question closed')
        exc.receipt = {'ok': False, 'error': {'code': 'dispatch_inactive'}}
        with patch.object(runner, 'orca', side_effect=exc) as call:
            result = runner.submit_control('reply', {'message_id': 'q', 'run_id': 'parent', 'text': 'answer'})
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('control_uncertain', runner.load_state())
            self.assertTrue(runner.load_state()['current']['messages'][0]['closed'])
            self.assertNotIn('answered', runner.load_state()['current']['messages'][0])
            op = store.read_json(runner.state_dir / 'operations.json')['reply:parent:q']
            self.assertEqual(op['status'], 'rejected')
            with self.assertRaises(operations.RejectedOperation):
                operations.continue_operation(runner.state_dir / 'operations.json', op['request_id'], runner.orca)
            runner.submit_control('reply', {'message_id': 'q', 'run_id': 'parent', 'text': 'answer'})
            self.assertEqual(call.call_count, 1)

    def test_existing_rejected_reply_unfences_controls_despite_child_cleanup_issue(self):
        attempt, st = self.attempt()
        st.update(control_uncertain='command')
        st['current']['messages'] = [{'id': 'q', 'type': 'question', 'run_id': 'parent'}]
        launch.save_registry(attempt, {'agents': [{'role': 'coordinator', 'dispatch_id': 'owned',
                                                  'status': 'unknown'}], 'runs': []})
        store.atomic_json(runner.state_dir / 'commands/command.json', {
            'id': 'command', 'action': 'reply', 'status': 'unknown',
            'data': {'message_id': 'q', 'run_id': 'parent', 'text': 'answer'}})
        store.atomic_json(runner.state_dir / 'operations.json', {'reply:parent:q': {
            'request_id': 'request', 'status': 'unknown', 'error': 'question closed',
            'args': ['orchestration', 'reply', '--id', 'q', '--run', 'parent'],
            'failure_receipt': {'ok': False, 'error': {'code': 'dispatch_inactive'}}}})
        with patch.object(runner, 'orca') as call, \
                patch.object(launch, 'reconcile_attempt', return_value=['child cleanup required']):
            runner.recover(st)
            call.assert_not_called()
        self.assertNotIn('control_uncertain', st)
        self.assertEqual(st['recovery_issues'], ['child cleanup required'])
        self.assertEqual(store.read_json(runner.state_dir / 'commands/command.json')['status'], 'error')
        self.assertTrue(st['current']['messages'][0]['closed'])

    def test_transport_failure_remains_unknown(self):
        path = self.root / 'operations.json'
        with patch.object(runner, 'orca', side_effect=RuntimeError('connection lost')):
            with self.assertRaises(operations.UncertainOperation):
                operations.mutation(path, 'reply', runner.orca, 'orchestration', 'reply', '--id', 'q')
        self.assertEqual(store.read_json(path)['reply']['status'], 'unknown')

    def test_lost_inactive_receipt_closes_question_as_history(self):
        _, st = self.attempt()
        st['current']['messages'] = [{'id': 'q', 'type': 'question', 'run_id': 'parent'}]
        path = runner.state_dir / 'operations.json'
        store.atomic_json(path, {'reply:parent:q': {
            'request_id': 'request', 'status': 'unknown',
            'args': ['orchestration', 'reply', '--id', 'q', '--run', 'parent']}})
        receipt = {'state': 'completed', 'receipt': {'ok': False, 'error': {'code': 'dispatch_inactive'}}}
        with patch.object(runner, 'orca', return_value=receipt) as call, \
                patch.object(launch, 'reconcile_attempt', return_value=[]):
            runner.recover(st, technical_only=True)
        self.assertEqual(call.call_count, 1)
        self.assertTrue(st['current']['messages'][0]['closed'])
        self.assertEqual(store.read_json(path)['reply:parent:q']['status'], 'rejected')
        self.assertNotIn('reply:parent:q', str(st['recovery_issues']))

    def test_accepted_done_closes_only_owned_same_run_questions(self):
        attempt, st = self.attempt()
        launch.save_registry(attempt, {'agents': [
            {'role': 'coordinator', 'dispatch_id': 'owned', 'run_id': 'parent', 'status': 'unknown'}], 'runs': []})
        st['current']['messages'] = [
            {'id': 'closed', 'type': 'question', 'run_id': 'parent', 'payload': {'dispatchId': 'owned'}},
            {'id': 'other-run', 'type': 'question', 'run_id': 'other', 'payload': {'dispatchId': 'owned'}},
            {'id': 'missing-dispatch', 'type': 'question', 'run_id': 'parent'},
            {'id': 'foreign', 'type': 'question', 'run_id': 'parent', 'payload': {'dispatchId': 'foreign'}},
            {'id': 'done', 'type': 'worker_done', 'run_id': 'parent',
             'payload': '{"dispatchId":"owned","outcome":"failed"}'},
            {'id': 'foreign-done', 'type': 'worker_done', 'run_id': 'parent',
             'payload': {'dispatchId': 'foreign', 'outcome': 'succeeded'}}]
        runner.save_state(st)
        with patch.object(launch, 'reconcile_attempt', return_value=[]), patch.object(runner, 'orca') as call:
            runner.recover(st, technical_only=True)
        call.assert_not_called()
        questions = st['current']['messages'][:4]
        self.assertTrue(questions[0]['closed'])
        self.assertTrue(all(not m.get('closed') for m in questions[1:]))
        with self.assertRaisesRegex(ValueError, 'question closed'):
            runner.apply_control(st, 'reply', {'message_id': 'closed', 'run_id': 'parent', 'text': 'late'})

    def test_unpersisted_done_cannot_close_question(self):
        attempt, st = self.attempt()
        st['current']['messages'] = [{'id': 'q', 'type': 'question', 'run_id': 'parent',
                                      'payload': {'dispatchId': 'owned'}}]
        runner.save_state(st)
        st['current']['messages'].append({'id': 'done', 'type': 'worker_done', 'run_id': 'parent',
                                          'payload': {'dispatchId': 'owned', 'outcome': 'succeeded'}})
        runner.close_completed_questions(st, {'agents': [{'run_id': 'parent', 'dispatch_id': 'owned'}]})
        self.assertFalse(st['current']['messages'][0].get('closed'))
