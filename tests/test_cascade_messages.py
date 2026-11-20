"""Isolated messaging storage, delivery receipts and history recovery."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from team_runner import cascade_messages as mail, runner_operations
from team_runner.cascade_store import Session
from team_runner.runner_operations import UncertainOperation
from team_runner.runner_store import atomic_json
from team_runner.verification import digest


class OrcaFixture:
    """Installed Orca 1.4.265 full record shapes and inbox SQL semantics."""

    inbox_window = (64, 'desc')

    def __init__(self, state, database):
        self.coordinator = state['features']['a']['roles']['coordinator']
        self.origin = state['features']['a']['roles']['implementer']
        self.manager = state['manager']['mailbox']
        self.database = database
        self.db = self.connection()
        self.db.execute('CREATE TABLE messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT,'
                        ' id TEXT UNIQUE, run_id TEXT, to_handle TEXT, record TEXT)')
        self.db.execute('CREATE TABLE receipts (request_id TEXT PRIMARY KEY, record TEXT)')
        self.calls = []
        self.attempted = []
        self.replies = {}
        self.stale = set()
        self.pending = set()
        self.accepted_only = False

    def connection(self):
        return sqlite3.connect(self.database, isolation_level=None)

    def receipt(self, request):
        row = self.db.execute('SELECT record FROM receipts WHERE request_id = ?', (request,)).fetchone()
        return json.loads(row[0]) if row else None

    def store_receipt(self, request, result):
        self.db.execute('INSERT INTO receipts VALUES (?, ?)', (request, json.dumps(result)))

    def insert(self, run, to, **row):
        record = dict(id=f'msg_{uuid4()}', run_id=run, to_handle=to, **row)
        self.db.execute('INSERT INTO messages (id, run_id, to_handle, record) VALUES (?, ?, ?, ?)',
                        (record['id'], run, to, json.dumps(record)))
        return record

    def question(self, role, body, *, read=False):
        row = self.insert(role['run'], f"run:{role['run']}", type='question', body=body, read=int(read),
                          from_handle=f"dispatch:{role['dispatch']}", payload=json.dumps({
                              'taskId': role['task'], 'dispatchId': role['dispatch'],
                              'question': body, 'options': ['keep', 'change'],
                          }))
        row['thread_id'] = row['id']
        self.db.execute('UPDATE messages SET record = ? WHERE id = ?', (json.dumps(row), row['id']))
        return row

    def reconcile(self, session, operation):
        reply = self.call('orchestration', 'request-show', '--request', operation['request_id'],
                          mailbox=operation['mailbox'])
        if reply['state'] == 'completed':
            return reply['receipt']
        if reply['state'] == 'pending':
            return None
        raise UncertainOperation('absent request')

    def call(self, group, verb, *args, mailbox=None):
        assert group == 'orchestration'
        self.calls.append((verb, args, mailbox))
        flags = dict(zip(args[::2], args[1::2]))
        if verb == 'run-show':
            return {'run': {'id': flags['--id'], 'legacy': 0,
                            'coordinator_handle': self.coordinator['terminal']}}
        if verb == 'worker-show':
            role = next(row for row in (self.origin, self.coordinator) if row['dispatch'] == flags['--dispatch'])
            terminal = role['terminal_identity']
            return {'dispatch': {'id': role['dispatch'], 'status': 'completed'
                    if role['dispatch'] in self.stale else 'dispatched', 'runId': role['run'],
                    'taskId': role['task'], 'assigneeHandle': role['terminal'],
                    'assigneePaneKey': f"{terminal['tabId']}:{terminal['leafId']}",
                    'processIncarnation': f"{terminal['ptyId']}:{terminal['incarnationId']}"},
                    'observation': {'status': 'live', 'exactWorker': True},
                    'terminal': dict(terminal)}
        if verb == 'inbox':
            rows = self.db.execute(
                'SELECT sequence, record FROM messages WHERE to_handle = ? ORDER BY sequence DESC LIMIT ?',
                (flags['--terminal'], int(flags['--limit'])),
            ).fetchall()
            return {'messages': [dict(json.loads(row), sequence=seq) for seq, row in rows],
                    'count': len(rows)}
        if verb == 'request-show':
            request = flags['--request']
            record = self.receipt(request)
            if request in self.pending:
                return dict(requestId=request, state='pending')
            return dict(requestId=request, state='completed', receipt=record) \
                if record else dict(requestId=request, state='absent')

        self.attempted.append((verb, flags))
        request = flags['--retry-request']
        self.pending.discard(request)
        if self.receipt(request):
            raise AssertionError('completed requests must be recovered by receipt, not replayed')
        run = flags['--run']
        mutation = {'requestId': request}
        if verb == 'send':
            destination = flags['--to']
            warnings = []
            if mailbox == self.coordinator['terminal'] and run != self.coordinator['run']:
                raise RuntimeError('dispatch_run_mismatch')
            if destination == f"dispatch:{self.coordinator['dispatch']}":
                assert run == self.coordinator['run']
                warnings = [{'code': 'recipient_run_bound_redirect', 'recipient': destination}]
                run = self.origin['run']
                destination = f'run:{run}'
            sender = flags.get('--from', mailbox)
            if mailbox == self.manager and mailbox.startswith('orca_session_id:'):
                assert '--from' not in flags
            row = self.insert(run, destination, type=flags['--type'], body=flags['--body'],
                              subject=flags['--subject'], from_handle=sender)
            result = dict(message=row, mutation=mutation, warnings=warnings)
        elif verb == 'ask' and '--question' in flags:
            assert run == self.coordinator['run']
            assert flags['--to'] == f'run:{run}'
            assert flags['--from'] == self.coordinator['terminal']
            assert flags['--options'] == 'keep,change'
            row = self.question(self.coordinator, flags['--question'])
            result = dict(messageId=row['id'], threadId=row['id'], pending=True, mutation=mutation)
        elif verb == 'reply':
            question_id = flags['--id']
            source = json.loads(self.db.execute('SELECT record FROM messages WHERE id = ?',
                                               (question_id,)).fetchone()[0])
            assert run == source['run_id']
            row = self.insert(run, source['from_handle'], from_handle=f'run:{run}',
                              body=flags['--body'], thread_id=question_id, type='status')
            self.replies[question_id] = row
            result = dict(message=row, mutation=mutation)
        elif verb == 'ask' and '--resume' in flags:
            question_id = flags['--resume']
            source = self.replies[question_id]
            assert run == source['run_id']
            result = dict(messageId=question_id, threadId=question_id, answer=source['body'],
                          answerMessageId=source['id'], timedOut=False, cancelled=False,
                          connectionLost=False, mutation=mutation)
            if self.accepted_only:
                self.store_receipt(request, dict(messageId=question_id, threadId=question_id, answer=None,
                                                 accepted=True, timedOut=False, cancelled=False, mutation=mutation))
                raise InterruptedError('lost final ask resume acknowledgement')
        else:
            raise AssertionError((verb, flags))
        self.store_receipt(request, result)
        return result


def assignment(role, run):
    terminal = 'term_' + role
    return dict(role=role, run=run, task='task_' + role, dispatch='dispatch_' + role, terminal=terminal,
                terminal_identity=dict(handle=terminal, ptyId='pty_' + role,
                                       worktreeId='child-worktree', incarnationId='instance_' + role,
                                       tabId='tab_' + role, leafId='leaf_' + role))


class CascadeMessagesTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        context = SimpleNamespace(state_dir=root, state_root=root, checkout_lock=root / 'checkout.lock',
                                  lease_path=root / 'lease.json', identity=lambda: {'workspace': 'fixture'})
        self.initial = {
            'execution': str(uuid4()), 'status': 'active', 'identity': context.identity(),
            'manager': {'mailbox': 'orca_session_id:manager'}, 'manager_run': 'parent',
            'snapshot': {}, 'snapshot_digest': digest({}), 'operations': {}, 'questions': {},
            'features': {'a': {'attempt': 'attempt-a', 'run': 'child', 'roles': {
                'coordinator': assignment('coordinator', 'parent'),
            }}},
        }
        self.initial['features']['a']['roles']['coordinator']['checkout'] = str(root)
        backend_state = dict(self.initial, features={'a': {'roles': {
            'coordinator': self.initial['features']['a']['roles']['coordinator'],
            'implementer': assignment('implementer', 'child'),
        }}})
        self.orca = OrcaFixture(backend_state, root / 'orca.sqlite')
        self.addCleanup(self.orca.db.close)
        self.session = Session(None, context, self.initial['execution'], self.orca)
        atomic_json(self.session.registry, self.initial)
        atomic_json(context.lease_path, self.session.lease)
        self.anchor = mail.ensure_anchor(self.session, 'a')
        with self.session.transaction() as state:
            state['features']['a']['roles']['implementer'] = self.orca.origin

    def question(self):
        return self.orca.question(self.orca.origin, 'First line\nNext line: `id`', read=True)['id']

    def state(self):
        return self.session.read()

    def inject(self, module, stage):
        def fail(checkpoint):
            if checkpoint == stage:
                raise InterruptedError(stage)
        return patch.object(module, 'checkpoint', side_effect=fail)

    def test_parent_dispatch_bootstraps_child_anchor_after_lost_receipt(self):
        # Return the fixture to a newly registered child Run with no senders.
        with self.session.transaction() as state:
            feature = state['features']['a']
            feature.pop('inbox_anchor')
            feature.pop('checkpoint')
            feature['roles'].pop('implementer')
            state['operations'].clear()
        self.orca.db.execute('DELETE FROM messages')
        self.orca.db.execute('DELETE FROM receipts')
        self.orca.attempted.clear()

        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [mail.ANCHOR_PENDING])
        attempted = self.orca.attempted[0][1]
        self.assertEqual((attempted['--run'], attempted['--to'], attempted['--from']),
                         ('parent', 'run:parent', self.orca.coordinator['terminal']))
        with self.inject(runner_operations, 'send:after_effect'):
            with self.assertRaises(InterruptedError):
                mail.resume_manager(self.session)
        role_address = 'dispatch:' + self.orca.coordinator['dispatch']
        relayed = self.orca.attempted[1][1]
        self.assertEqual((relayed['--run'], relayed['--to']), ('parent', role_address))
        self.assertNotIn('--from', relayed)
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [])
        expected = self.state()['features']['a']['inbox_anchor']
        self.assertEqual(expected['run_id'], 'child')
        self.assertEqual(self.state()['operations']['mail:a:anchor']['status'], 'calling')
        mail.resume_manager(self.session)
        self.assertEqual(self.state()['features']['a']['inbox_anchor'], expected)
        self.assertEqual(self.state()['operations']['mail:a:anchor']['status'], 'completed')
        self.assertEqual(len(self.orca.attempted), 2)

    @unittest.skipUnless(hasattr(os, 'fork'), 'process crash fixture requires fork')
    def test_process_exits_before_saving_read_and_after_orca_accepts_ask(self):
        original = self.question()
        for stage in ('cascade-inbox:after_read', 'ask:after_effect'):
            pid = os.fork()
            if pid == 0:
                try:
                    self.orca.db = self.orca.connection()
                    os.environ['TEAM_RUNNER_CRASH_AT'] = stage
                    mail.resume_coordinator(self.session, 'a')
                finally:
                    os._exit(87)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 86)
        self.assertNotIn('parent_message_id', self.state()['questions'][f'child:{original}'])
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [original])
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [original])
        self.assertEqual(self.orca.db.execute(
            "SELECT COUNT(*) FROM messages WHERE to_handle = 'run:parent'"
        ).fetchone()[0], 1)

    def test_inbox_read_interruption_and_lost_ask_keep_one_forward(self):
        message = self.question()
        with self.inject(mail, 'cascade-inbox:after_read'):
            with self.assertRaises(InterruptedError):
                mail.resume_coordinator(self.session, 'a')
        self.assertEqual(self.state()['features']['a']['checkpoint'], self.anchor)
        self.assertFalse(self.state()['questions'])
        with self.inject(runner_operations, 'ask:after_effect'):
            with self.assertRaises(InterruptedError):
                mail.resume_coordinator(self.session, 'a')
        state = self.state()
        self.assertIn(f'child:{message}', state['questions'])
        self.assertEqual(state['features']['a']['checkpoint']['id'], message)
        self.assertNotIn('parent_message_id', state['questions'][f'child:{message}'])
        for _ in range(2):
            self.assertEqual(mail.resume_coordinator(self.session, 'a'), [message])
        report, = mail.resume_manager(self.session)
        self.assertEqual(report['id'], self.state()['questions'][f'child:{message}']['parent_message_id'])
        self.assertEqual(sum(verb == 'ask' for verb, _ in self.orca.attempted), 1)

    def test_reply_send_and_original_reply_recover_without_repeating_answer(self):
        original = self.question()
        mail.resume_coordinator(self.session, 'a')
        parent = self.state()['questions'][f'child:{original}']['parent_message_id']
        answer = 'keep\nTrailing text'
        mail.save_answer(self.session, parent, answer)
        with self.inject(mail, 'cascade-answer:after_manager_reply'):
            with self.assertRaises(InterruptedError):
                mail.resume_manager(self.session)
        self.assertEqual(self.state()['operations'][self.state()['questions'][f'child:{original}']['wake']]['status'], 'intent')
        with self.inject(runner_operations, 'send:after_effect'):
            with self.assertRaises(InterruptedError):
                mail.resume_manager(self.session)
        with self.assertRaisesRegex(ValueError, 'already saved'):
            mail.save_answer(self.session, parent, 'different')
        mail.resume_manager(self.session)
        with self.inject(runner_operations, 'reply:after_effect'):
            with self.assertRaises(InterruptedError):
                mail.resume_coordinator(self.session, 'a')
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [])
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [])
        outgoing = [args for verb, args in self.orca.attempted if verb == 'reply']
        self.assertEqual([(args['--run'], args['--body']) for args in outgoing],
                         [('parent', answer), ('child', answer)])
        self.assertEqual(sum(verb == 'send' for verb, _ in self.orca.attempted), 2)

    def test_preliminary_ask_resume_receipt_uses_exact_saved_reply(self):
        original = self.question()
        mail.resume_coordinator(self.session, 'a')
        parent = self.state()['questions'][f'child:{original}']['parent_message_id']
        mail.save_answer(self.session, parent, 'keep')
        mail.resume_manager(self.session)
        self.orca.accepted_only = True
        with self.assertRaises(InterruptedError):
            mail.resume_coordinator(self.session, 'a')
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [])
        retrieval = next(row for row in self.state()['operations'].values()
                         if '--resume' in row['args'])
        self.assertEqual(retrieval['result']['reconciledFromAnswer'], self.orca.replies[parent]['id'])
        self.assertTrue(self.state()['questions'][f'child:{original}']['delivered'])
        self.assertEqual(sum('--resume' in args for _, args in self.orca.attempted), 1)

    def test_coordinator_can_use_durable_wake_before_manager_recovers_receipt(self):
        original = self.question()
        mail.resume_coordinator(self.session, 'a')
        parent = self.state()['questions'][f'child:{original}']['parent_message_id']
        mail.save_answer(self.session, parent, 'keep')
        with self.inject(runner_operations, 'send:after_effect'):
            with self.assertRaises(InterruptedError):
                mail.resume_manager(self.session)
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [])
        question = self.state()['questions'][f'child:{original}']
        self.assertEqual(question['wake_observed']['run_id'], 'child')
        self.assertEqual(self.state()['operations'][question['wake']]['status'], 'calling')
        mail.resume_manager(self.session)
        self.assertEqual(self.state()['operations'][question['wake']]['status'], 'completed')
        self.assertEqual(sum(verb == 'send' for verb, _ in self.orca.attempted), 2)

    def test_window_grows_with_global_sequence_gaps_and_read_question(self):
        original = self.question()
        for _ in range(40):
            self.orca.insert('elsewhere', 'run:elsewhere', type='status')
            self.orca.insert('child', 'run:child', type='status')
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [original])
        limits = [dict(zip(args[::2], args[1::2]))['--limit']
                  for verb, args, _ in self.orca.calls if verb == 'inbox']
        self.assertIn('64', limits)
        self.assertEqual(len(self.state()['features']['a']['inbox_events']), 41)

    def test_window_gap_preserves_checkpoint_and_makes_no_mutation(self):
        self.question()
        for _ in range(self.orca.inbox_window[0] + 1):
            self.orca.insert('child', 'run:child', type='status')
        with self.assertRaises(mail.InboxHistoryGap) as error:
            mail.resume_coordinator(self.session, 'a')
        self.assertEqual(error.exception.details['run'], 'child')
        self.assertEqual(error.exception.details['anchor'], self.anchor)
        self.assertEqual(error.exception.details['limit'], self.orca.inbox_window[0])
        self.assertIsNotNone(error.exception.details['oldest'])
        self.assertEqual(self.state()['features']['a']['checkpoint'], self.anchor)
        self.assertEqual(len(self.orca.attempted), 1)  # anchor only

    def test_unknown_or_stale_send_blocks_without_followup(self):
        original = self.question()
        mail.resume_coordinator(self.session, 'a')
        parent = self.state()['questions'][f'child:{original}']['parent_message_id']
        mail.save_answer(self.session, parent, 'keep')
        self.orca.stale.add(self.orca.coordinator['dispatch'])
        with self.assertRaisesRegex(UncertainOperation, 'stale'):
            mail.resume_manager(self.session)
        self.assertFalse(self.orca.replies)
        self.orca.stale.clear()
        with self.inject(mail, 'cascade-answer:after_manager_reply'):
            with self.assertRaises(InterruptedError):
                mail.resume_manager(self.session)
        wake_key = self.state()['questions'][f'child:{original}']['wake']
        with self.session.transaction() as state:
            state['operations'][wake_key]['status'] = 'calling'
        with self.assertRaisesRegex(UncertainOperation, 'absent'):
            mail.resume_manager(self.session)
        self.assertEqual(mail.resume_coordinator(self.session, 'a'), [original])
        self.assertEqual(sum(verb == 'send' for verb, _ in self.orca.attempted), 1)
        self.assertEqual(set(self.orca.replies), {parent})

    def test_pending_send_reuses_original_request_only_for_active_dispatch(self):
        original = self.question()
        mail.resume_coordinator(self.session, 'a')
        parent = self.state()['questions'][f'child:{original}']['parent_message_id']
        mail.save_answer(self.session, parent, 'keep')
        with self.inject(runner_operations, 'send:before_effect'):
            with self.assertRaises(InterruptedError):
                mail.resume_manager(self.session)
        wake = self.state()['questions'][f'child:{original}']['wake']
        request_id = self.state()['operations'][wake]['request_id']
        self.orca.pending.add(request_id)
        self.orca.stale.add(self.orca.coordinator['dispatch'])
        with self.assertRaisesRegex(UncertainOperation, 'stale'):
            mail.resume_manager(self.session)
        self.assertEqual(sum(verb == 'send' for verb, _ in self.orca.attempted), 1)
        self.orca.stale.clear()
        mail.resume_manager(self.session)
        sent = [flags for verb, flags in self.orca.attempted if verb == 'send']
        self.assertEqual(sent[1]['--retry-request'], request_id)

    def test_changed_recipient_blocks_checkpoint(self):
        original = self.orca.call('orchestration', 'inbox', '--terminal', 'run:child', '--limit', '1')
        original['messages'][0]['to_handle'] = 'run:other'
        with patch.object(self.orca, 'call', return_value=original):
            with self.assertRaisesRegex(mail.InboxHistoryGap, 'ordering changed'):
                mail.resume_coordinator(self.session, 'a')
        self.assertEqual(self.state()['features']['a']['checkpoint'], self.anchor)

    def test_unconfirmed_contract_blocks_processing(self):
        self.question()
        self.orca.inbox_window = None
        with self.assertRaisesRegex(mail.InboxHistoryGap, 'semantics unconfirmed'):
            mail.resume_coordinator(self.session, 'a')
        self.assertEqual(len(self.orca.attempted), 1)


if __name__ == '__main__':
    unittest.main()
