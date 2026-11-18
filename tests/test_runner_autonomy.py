"""Offline autonomy invariants; temporary checkouts and synthetic Orca only."""
import copy
import json
import os
from pathlib import Path
import threading
import unittest
import uuid
from unittest.mock import patch

from team_runner import autonomy, application, protocol, goal_intent, recovery, event_scope, state_time
from team_runner import graph_dispatch as graph, network_recovery as network
from team_runner.runner_store import atomic_json, read_json, lock
import test_team_graph as graph_fixtures
from test_team_graph import small_graph
import test_platform as fixtures


class Autonomy(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt
    prepare = graph_fixtures.GraphExecution.prepare
    delegate = graph_fixtures.GraphExecution.delegate
    finish = graph_fixtures.GraphExecution.finish

    def enable(self):
        goal = application.goals(self.a)[0]
        goal_intent.authorize(self.a, {'goal': goal}, str(uuid.uuid4()))
        recovery.configure(self.a, True)
        self.a.authorization = {k: goal_intent.read(self.a)[k] for k in ('generation', 'epoch')}
        return self.set_policy(True)

    def command(self, action='autonomy', payload=None):
        view = protocol.summary(self.a)
        self.assertTrue(view['confirmed'])
        return {'schema_version': 1, 'request_id': str(uuid.uuid4()), 'action': action,
                'scope': {'identity': view['identity'], 'generation': view['generation'],
                          'attempt': view['current']['attempt'], 'revision': view['revision']},
                'payload': payload or {'enabled': True}}

    def set_policy(self, enabled):
        command = self.command(payload={'enabled': enabled})
        result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'succeeded', result)
        return command, result

    def test_policy_scoped_journal_idempotence_child_and_stale_guards(self):
        attempt, root = self.prepare()
        before = (attempt / 'config.json').read_bytes()
        self.assertFalse(autonomy.policy(self.a)['enabled'])
        self.assertFalse(autonomy.policy(self.b)['enabled'])
        command, result = self.enable()
        self.assertEqual(application.submit_v1(self.a, command), result)
        self.assertEqual(len(read_json(self.a.state_dir / 'autonomy.json')['history']), 1)
        stale = self.command(payload={'enabled': False})
        self.set_policy(False)
        with self.assertRaisesRegex(ValueError, 'Refresh status'):
            application.submit_v1(self.a, stale)
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}), self.assertRaises(protocol.ContractError):
            application.submit_v1(self.a, self.command())
        with self.assertRaises(ValueError):
            application.submit_command(self.a, 'autonomy', {'enabled': True}, str(uuid.uuid4()))
        self.set_policy(True)
        intent = goal_intent.read(self.a); intent['generation'] = str(uuid.uuid4())
        with patch.object(goal_intent, 'read', return_value=intent):
            self.assertFalse(autonomy.policy(self.a)['enabled'])
        self.assertEqual((attempt / 'config.json').read_bytes(), before)

    def test_atomic_grant_concurrent_duplicate_and_restart_repair(self):
        cfg = small_graph(); cfg['edges'][0]['max_passes'] = 1
        attempt, root = self.prepare(cfg); self.enable()
        _, child = self.delegate(attempt, root); self.finish(attempt, child)
        blocked, _ = self.delegate(attempt, root)
        outputs, errors = [], []
        def grant():
            try:
                outputs.append(autonomy.grant(self.a, attempt, blocked['id']))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=grant) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(read_json(self.a.state_dir / 'autonomy-budget.json').popitem()[1]), 1)
        self.assertEqual(len(self.a.launcher.registry(attempt)['graph_grants']), 1)
        reg = self.a.launcher.registry(attempt)
        reg['graph_grants'] = {}; reg['edge_passes']['implement'] = 1
        call = next(c for c in reg['graph_calls'] if c['id'] == blocked['id'])
        call.update(status='limit'); call.pop('pass_taken')
        self.a.launcher.save_registry(attempt, reg)
        autonomy.grant(self.a, attempt, blocked['id'])
        self.assertEqual(autonomy.view(self.a)['budgets']['implement']['used'], 1)
        self.assertEqual(self.a.launcher.registry(attempt)['edge_passes']['implement'], 2)

    def test_budget_across_attempts_and_exhaustion_unknown_effect_barrier(self):
        cfg = small_graph(); cfg['edges'][0]['max_passes'] = 1
        attempt, root = self.prepare(cfg); self.enable()
        for index in range(4):
            call, child = self.delegate(attempt, root)
            if index:
                self.assertEqual(call['status'], 'limit')
                autonomy.tick(self.a, self.a.load_state())
                call, child = self.delegate(attempt, root, request=call['id'])
            self.finish(attempt, child)
        call, _ = self.delegate(attempt, root)
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        self.assertEqual(autonomy.view(self.a)['budgets']['implement'], {'used': 3, 'remaining': 0, 'limit': 3})
        attempt2, root2 = self.prepare(cfg)
        _, child = self.delegate(attempt2, root2); self.finish(attempt2, child)
        call2, _ = self.delegate(attempt2, root2)
        self.assertIsNone(autonomy.grant(self.a, attempt2, call2['id']))
        self.assertEqual(autonomy.view(self.a)['budgets']['implement']['used'], 3)
        self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'graph_edge_limit')
        # Unknown effects prevent even reserving an otherwise available edge budget.
        state = self.a.load_state(); state['current']['item'] = 'another'; self.a.save_state(state)
        atomic_json(self.a.launcher.journal(attempt2), {'unknown': {'status': 'unknown'}})
        self.assertIsNone(autonomy.grant(self.a, attempt2, call2['id']))
        self.assertEqual(autonomy.view(self.a)['budgets']['implement']['used'], 0)

    def test_event_history_waiting_parent_and_unattributed_diagnostics(self):
        attempt, root = self.prepare(); self.enable()
        outer, impl = self.delegate(attempt, root)
        check, tester = self.delegate(attempt, impl, 'check')
        state = self.a.load_state(); state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        def message(agent, name):
            return {'id': name, 'type': 'escalation', 'run_id': agent['run_id'],
                    'payload': json.dumps({'dispatchId': agent['dispatch_id'], 'taskId': agent['task_id'],
                                           'handle': agent['terminal'], 'stopReason': 'finished'}),
                    'body': 'ended without worker_done'}
        parent = message(root, 'parent'); stale = message(tester, 'stale'); unknown = dict(parent, id='unknown', payload={})
        reg = self.a.launcher.registry(attempt)
        self.assertEqual(event_scope.classify(parent, state['current'], reg)['kind'], 'waiting_child')
        self.finish(attempt, tester)
        state = self.a.load_state(); state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        state['current']['messages'] += [parent, stale, unknown]
        self.a.save_state(state)
        self.assertIsNone(protocol.summary(self.a)['blocker'])
        values = application.state_view(self.a)['state']['current']['messages']
        self.assertEqual(values[-2]['classification']['kind'], 'historical')
        self.assertEqual(values[-1]['classification']['kind'], 'unattributed')
        self.assertEqual(event_scope.pending(state['current'], self.a.launcher.registry(attempt)), [])

    def test_unlimited_passes_keep_ledger_restore_cap_and_scope(self):
        cfg = small_graph(); cfg['edges'][0]['max_passes'] = 1
        attempt, root = self.prepare(cfg); self.enable()
        snapshot = (attempt / 'config.json').read_bytes()
        command = self.command(payload={'enabled': True, 'unlimited_passes': True})
        receipt = application.submit_v1(self.a, command)
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual(application.submit_v1(self.a, command), receipt)
        self.assertTrue(autonomy.policy(self.a)['unlimited_passes'])
        self.assertFalse(autonomy.policy(self.b)['unlimited_passes'])
        for index in range(6):
            call, child = self.delegate(attempt, root)
            if index:
                autonomy.tick(self.a, self.a.load_state())
                call, child = self.delegate(attempt, root, request=call['id'])
            self.finish(attempt, child)
        self.assertEqual(autonomy.view(self.a)['budgets']['implement'],
                         {'used': 5, 'remaining': None, 'limit': None})
        ledger = read_json(self.a.state_dir / 'autonomy-budget.json')
        self.set_policy(True)
        call, _ = self.delegate(attempt, root)
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        self.assertEqual(autonomy.view(self.a)['budgets']['implement'],
                         {'used': 5, 'remaining': 0, 'limit': 3})
        self.assertEqual(read_json(self.a.state_dir / 'autonomy-budget.json'), ledger)
        self.assertEqual((attempt / 'config.json').read_bytes(), snapshot)
        application.submit_v1(self.a, self.command(payload={'enabled': True, 'unlimited_passes': True}))
        intent = goal_intent.read(self.a); intent['generation'] = str(uuid.uuid4())
        with patch.object(goal_intent, 'read', return_value=intent):
            self.assertFalse(autonomy.policy(self.a)['unlimited_passes'])
            self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))

    def test_unlimited_passes_cannot_bypass_questions_unknown_effects_or_pause(self):
        cfg = small_graph(); cfg['edges'][0]['max_passes'] = 1
        attempt, root = self.prepare(cfg); self.enable()
        application.submit_v1(self.a, self.command(payload={'enabled': True, 'unlimited_passes': True}))
        _, child = self.delegate(attempt, root); self.finish(attempt, child)
        call, _ = self.delegate(attempt, root)
        state = self.a.load_state()
        state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        self.a.save_state(state)
        with lock(self.a.state_dir / 'runner.lock'):
            view = protocol.summary(self.a)
            self.assertTrue(view['confirmed'])
            self.assertEqual(view['observed_state'], 'running')
            self.assertIsNone(view['blocker'])
            self.assertTrue(view['autonomy']['waiting_for_pass'])
            self.assertEqual(view['available_actions'], [{'action': 'pause'}, {'action': 'stop'}])
        self.assertEqual(autonomy.view(self.a)['budgets']['implement']['used'], 0)
        self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'graph_edge_limit')
        question = {'id': 'business-conflict', 'type': 'question', 'run_id': root['run_id'],
                    'payload': {'dispatchId': root['dispatch_id']}, 'body': 'conflicting requirements'}
        state['current']['messages'].append(question); self.a.save_state(state)
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        with lock(self.a.state_dir / 'runner.lock'):
            view = protocol.summary(self.a)
            self.assertEqual(view['blocker']['code'], 'answer_required')
            self.assertNotIn('waiting_for_pass', view['autonomy'])
        question['answered'] = True; self.a.save_state(state)
        atomic_json(self.a.launcher.journal(attempt), {'unknown': {'status': 'unknown'}})
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        with lock(self.a.state_dir / 'runner.lock'):
            view = protocol.summary(self.a)
            self.assertEqual(view['blocker']['code'], 'unknown_effect')
            self.assertNotIn('waiting_for_pass', view['autonomy'])
        atomic_json(self.a.launcher.journal(attempt), {})
        state['paused'] = True; self.a.save_state(state)
        self.assertIsNone(autonomy.grant(self.a, attempt, call['id']))
        self.assertEqual(autonomy.view(self.a)['budgets']['implement']['used'], 0)
        state['paused'] = False; self.a.save_state(state)
        self.assertIsNotNone(autonomy.grant(self.a, attempt, call['id']))
        self.assertEqual(autonomy.view(self.a)['budgets']['implement']['used'], 1)
        with lock(self.a.state_dir / 'runner.lock'):
            self.assertNotIn('waiting_for_pass', protocol.summary(self.a)['autonomy'])

    def test_unlimited_permission_requires_boolean_enabled_and_parent(self):
        attempt, root = self.prepare(); self.enable()
        for payload in ({'enabled': True, 'unlimited_passes': 1},
                        {'enabled': False, 'unlimited_passes': True}):
            with self.assertRaises(protocol.ContractError):
                application.submit_v1(self.a, self.command(payload=payload))
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}), self.assertRaises(protocol.ContractError):
            application.submit_v1(self.a, self.command(payload={'enabled': True, 'unlimited_passes': True}))

    def test_resumed_exact_work_closes_technical_error_but_not_business_question(self):
        attempt, root = self.prepare(); self.enable()
        _, child = self.delegate(attempt, root)
        state = self.a.load_state(); state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        message = {'id': 'network-end', 'run_id': child['run_id'], 'type': 'escalation',
                   'created_at': '2026-11-14T16:43:47Z', 'payload': {'dispatchId': child['dispatch_id'],
                   'taskId': child['task_id'], 'handle': child['terminal'], 'stopReason': 'errored'}}
        reg = self.a.launcher.registry(attempt)
        agent = next(a for a in reg['agents'] if a['id'] == child['id'])
        self.assertEqual(event_scope.classify(message, state['current'], reg)['kind'], 'active')
        data = self.a.launcher.observe_agent(child)
        data['dispatch'].update(id=child['dispatch_id'], runId=child['run_id'], taskId=child['task_id'])
        data['observation']['exactWorker'] = True
        data.setdefault('projection', {}).update(stage={'activity': 'working'}, evidence={'liveStatus': 'fresh'},
            liveness={'verdict': 'live', 'source': 'agent_status', 'observedAt': 1794674630000})
        # Fresh status with a foreign terminal is not proof of this session.
        foreign = copy.deepcopy(data); foreign['terminal']['incarnationId'] = 'other'
        with patch.object(self.a.launcher, 'orca', return_value=foreign): self.a.launcher.observe_agent(agent)
        self.assertNotIn('working_evidence', agent)
        stale = copy.deepcopy(data); stale['projection']['evidence']['liveStatus'] = 'stale'
        with patch.object(self.a.launcher, 'orca', return_value=stale): self.a.launcher.observe_agent(agent)
        self.assertNotIn('working_evidence', agent)
        with patch.object(self.a.launcher, 'orca', return_value=data): self.a.launcher.observe_agent(agent)
        self.assertEqual(event_scope.classify(message, state['current'], reg)['kind'], 'historical')
        business = dict(message, type='question')
        self.assertEqual(event_scope.classify(business, state['current'], reg)['kind'], 'active')
        newer = dict(message, created_at='2026-11-14T16:44:00Z')
        self.assertEqual(event_scope.classify(newer, state['current'], reg)['kind'], 'active')

    def network_fixture(self):
        attempt, root = self.prepare(); self.enable()
        call, child = self.delegate(attempt, root)
        state = self.a.load_state(); state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        self.a.save_state(state)
        fake = self.a.launcher.orca
        def transport(*args, **kwargs):
            result = fake(*args, **kwargs)
            if args[:2] == ('orchestration', 'worker-show'):
                d = args[args.index('--dispatch') + 1]
                result['dispatch']['id'] = d
                result['observation']['exactWorker'] = True
                if fake.workers[d]['status'] == 'exited': result['dispatch']['status'] = 'stopped'
            return result
        transport.root = fake.root
        self.a.launcher.orca = transport
        self.now = 100
        self.supervisor = recovery.Supervisor(None, 'fixture', clock=lambda: self.now)
        self.fact = {'id': 'failure-1', 'error': 'network_error', 'source': 'fixture'}
        def failure(runner, agent, data):
            return dict(self.fact) if agent['role'] != 'coordinator' else None
        self.failure_patch = patch.object(network, 'failure', side_effect=failure)
        self.failure_patch.start(); self.addCleanup(self.failure_patch.stop)
        return attempt, root, child

    def network_tick(self):
        network.supervise(self.supervisor, self.a, self.a.load_state())
        return next(iter(read_json(self.a.state_dir / 'network-recovery.json')['sessions'].values()))

    def process(self):
        with lock(self.a.state_dir / 'runner.lock'):
            self.a.process_controls(self.a.load_state())

    def test_two_resumes_then_one_replacement_same_call_budget_and_snapshot(self):
        attempt, root, child = self.network_fixture()
        before = (attempt / 'config.json').read_bytes()
        session = self.network_tick(); self.assertEqual(session['next_check_at'], 130)
        self.now = 129; self.network_tick()
        self.network_tick(); self.assertEqual(len([p for p in (self.a.state_dir / 'commands').glob('*.json') if read_json(p)['action'] == 'network-recover']), 0)
        self.now = 130; self.network_tick(); self.process()
        self.now = 150; self.network_tick()
        self.assertEqual(self.network_tick()['resumes'], 1)
        self.fact['id'] = 'failure-2'; self.now = 170
        self.assertEqual(self.network_tick()['next_check_at'], 290)
        self.now = 290; self.network_tick(); self.process()
        self.fact['id'] = 'failure-3'; self.now = 310
        self.network_tick(); self.process()
        reg = self.a.launcher.registry(attempt)
        replacement = next(a for a in reg['agents'] if a.get('network_replacement_of'))
        self.assertEqual(replacement['requested'], child['requested'])
        self.assertEqual(replacement['task_id'], child['task_id'])
        self.assertEqual(replacement['call_id'], child['call_id'])
        self.assertEqual(reg['edge_passes'], {'implement': 1})
        self.assertEqual((attempt / 'config.json').read_bytes(), before)
        self.assertNotEqual(replacement['dispatch_id'], child['dispatch_id'])
        self.assertEqual(next(a for a in reg['agents'] if a['id'] == root['id'])['status'], 'waiting_child')
        self.fact['id'] = 'failure-4'; self.now = 330
        self.assertEqual(self.network_tick()['status'], 'needs_action')
        self.assertEqual(len(reg['agents']), 3)
        state = self.a.load_state()
        self.a.persist_messages(state, {'messages': [{'id':'late-old','type':'worker_done',
            'payload': {'dispatchId':child['dispatch_id'], 'outcome':'succeeded'}}]}, child['run_id'])
        self.a.release_completed_children(attempt)
        call = next(c for c in self.a.launcher.registry(attempt)['graph_calls'] if c['id'] == child['call_id'])
        self.assertEqual(call['dispatch_id'], replacement['dispatch_id'])
        self.assertEqual(call['status'], 'running')
        self.assertNotIn(child['dispatch_id'], self.a.accepted_child_reports(attempt))

    def test_pause_during_backoff_cancels_and_open_child_never_replaced(self):
        attempt, root, child = self.network_fixture()
        self.network_tick()
        recovery.set_intent(self.a, 'paused')
        self.now = 1000; self.network_tick()
        self.assertEqual(self.network_tick()['status'], 'cancelled')
        self.assertEqual(len(self.a.launcher.registry(attempt)['agents']), 2)
        self.assertFalse(any(read_json(p)['action'] == 'network-recover' for p in (self.a.state_dir / 'commands').glob('*.json')))

    def test_resume_requires_new_failure_and_preserves_network_budget(self):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.now = 150; self.network_tick()
        self.fact['id'] = 'second'; self.now = 170; self.network_tick()
        recovery.set_intent(self.a, 'paused')
        recovery.set_intent(self.a, 'running')
        self.a.authorization = {k: goal_intent.read(self.a)[k] for k in ('generation', 'epoch')}
        self.now = 400
        self.assertEqual(self.network_tick()['status'], 'cancelled')
        self.fact['id'] = 'third'; self.now = 430
        session = self.network_tick()
        self.assertEqual(session['resumes'], 1)
        self.assertEqual(session['next_check_at'], 550)

    def test_lost_receipt_reconciles_existing_operation_without_resend(self):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130
        session = self.network_tick()
        original = self.a.launcher.orca
        sent = []
        def lose(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('orchestration', 'send'):
                sent.append(args)
                raise RuntimeError('lost receipt')
            return result
        self.a.launcher.orca = lose
        self.process()
        self.assertEqual(len(sent), 1)
        self.assertIn('control_uncertain', self.a.load_state())
        self.a.launcher.orca = original
        self.now = 150; self.network_tick(); self.process()
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertEqual(self.network_tick()['resumes'], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(protocol.operation(self.a, session['command_id'])['status'], 'succeeded')

    def test_pending_receipt_is_observed_again_without_resending(self):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick()
        original = self.a.launcher.orca
        sends = []
        pending = [True]
        def transport(*args, **kwargs):
            if args[:2] == ('orchestration', 'request-show') and pending[0]:
                return {'state': 'pending'}
            result = original(*args, **kwargs)
            if args[:2] == ('orchestration', 'send'):
                sends.append(args)
                raise RuntimeError('lost receipt')
            return result
        self.a.launcher.orca = transport
        self.process()
        self.now = 150; self.network_tick(); self.process()
        self.assertIn('control_uncertain', self.a.load_state())
        pending[0] = False
        self.now = 180; self.network_tick(); self.process()
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertEqual(len(sends), 1)

    def test_manual_reconcile_after_receipt_observations_exhausted(self):
        self.manual_network_receipt(None)

    def test_manual_reconcile_after_cancel_does_not_reauthorize_recovery(self):
        self.manual_network_receipt('paused')

    def manual_network_receipt(self, cancellation):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; session = self.network_tick()
        original = self.a.launcher.orca
        pending, sends = [True], []
        def transport(*args, **kwargs):
            if args[:2] == ('orchestration', 'request-show') and pending[0]:
                return {'state': 'pending'}
            result = original(*args, **kwargs)
            if args[:2] == ('orchestration', 'send'):
                sends.append(args)
                raise RuntimeError('lost receipt')
            return result
        self.a.launcher.orca = transport; self.process()
        for now in (150, 180, 210, 240, 270, 300, 330):
            self.now = now; self.network_tick(); self.process()
        self.assertIn('control_uncertain', self.a.load_state())
        if cancellation:
            recovery.set_intent(self.a, cancellation)
        pending[0] = False
        command = self.command(action='reconcile'); command['payload'] = {}
        result = application.submit_v1(self.a, command)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertEqual(len(sends), 1)
        self.assertEqual(read_json(self.a.state_dir / 'commands' / (session['command_id'] + '.json'))['status'], 'done')
        saved = next(iter(read_json(self.a.state_dir / 'network-recovery.json')['sessions'].values()))
        self.assertEqual(saved['status'], 'cancelled' if cancellation else 'observing')
        self.assertEqual(saved['resumes'], 1)

    def test_waiting_parent_escalation_stays_historical_after_child_settlement(self):
        attempt, root = self.prepare(); self.enable()
        _, child = self.delegate(attempt, root)
        state = self.a.load_state()
        state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        technical = {'id': 'technical-end', 'type': 'escalation', 'run_id': root['run_id'],
                     'payload': {'dispatchId': root['dispatch_id'], 'taskId': root['task_id'],
                                 'handle': root['terminal'], 'stopReason': 'finished'}}
        state['current']['messages'] = [technical]; self.a.save_state(state)
        self.assertIsNone(protocol.summary(self.a)['blocker'])
        self.finish(attempt, child)
        self.assertEqual(application.state_view(self.a)['state']['current']['messages'][0]['classification']['kind'], 'historical')
        self.assertIsNone(protocol.summary(self.a)['blocker'])
        # A later turn ending outside a child wait still requires inspection.
        state = self.a.load_state()
        fresh = dict(technical, id='fresh-end'); fresh.pop('classification', None)
        state['current']['messages'].append(fresh); self.a.save_state(state)
        self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'escalation_required')

    def test_opencode_new_user_turn_clears_old_provider_failure(self):
        import sqlite3
        from team_runner.runner_metrics import PREAMBLE
        attempt, root = self.prepare()
        _, child = self.delegate(attempt, root)
        path = self.root / 'opencode.db'
        preamble = (PREAMBLE + 'Your task ID is: ' + child['task_id'] + '\n--from ' + child['terminal']
                    + ' --type worker_done\n--task-id ' + child['task_id'] + ' --dispatch-id '
                    + child['dispatch_id'] + ' --outcome')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE session_v2 (id TEXT, directory TEXT)')
            db.execute('CREATE TABLE session_message (session_id TEXT, seq INT, type TEXT, data TEXT)')
            db.execute('INSERT INTO session_v2 VALUES (?,?)', ('sess', str(self.a.repo.resolve())))
            db.executemany('INSERT INTO session_message VALUES (?,?,?,?)', [
                ('sess', 1, 'user', json.dumps({'text': preamble})),
                ('sess', 2, 'assistant', json.dumps({'error': {'message': 'stream disconnected before completion'}}))])
        self.assertEqual(network.opencode_failure(self.a, child, path)['id'], 'sess:2')
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO session_message VALUES (?,?,?,?)',
                       ('sess', 3, 'user', json.dumps({'text': 'Continue current task'})))
        self.assertIsNone(network.opencode_failure(self.a, child, path))

    def test_lost_replacement_receipt_reconciles_inactive_registry(self):
        self.replacement_receipts(False)

    def test_external_stop_unknown_reconciliation_finishes_exact_registered_cleanup(self):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.fact['id'] = 'second'; self.now = 150; self.network_tick()
        self.now = 270; self.network_tick(); self.process()
        self.fact['id'] = 'third'; self.now = 300; session = self.network_tick()
        identifier = session['command_id']
        command_path = self.a.state_dir / 'commands' / (identifier + '.json')
        command = read_json(command_path); command['status'] = 'unknown'; atomic_json(command_path, command)
        state = self.a.load_state(); state['control_uncertain'] = identifier; self.a.save_state(state)
        path = self.a.launcher.journal(attempt); ops = read_json(path)
        stop = {'status': 'completed', 'request_id': str(uuid.uuid4()),
                'args': ['orchestration', 'worker-stop', '--dispatch', child['dispatch_id']],
                'result': {'dispatchId': child['dispatch_id'], 'state': 'stop_unknown', 'processAction': 'none'}}
        ops[child['id'] + '-worker-stop'] = stop; atomic_json(path, ops)
        data = {'_target': str(attempt), 'request': identifier, 'session': command['data']['session']}
        original = self.a.launcher.orca; calls = []
        def transport(*args, **kwargs):
            calls.append(args[:2])
            return original(*args, **kwargs)
        self.a.launcher.orca = transport
        with lock(self.a.state_dir / 'runner.lock'):
            with self.assertRaisesRegex(ValueError, 'conclusive registry'):
                network.reconcile(self.a, self.a.load_state(), data, observe_only=True)
            stop['result']['processAction'] = 'unknown'; atomic_json(path, ops)
            with self.assertRaisesRegex(ValueError, 'no-process-action'):
                network.reconcile(self.a, self.a.load_state(), data)
            stop['result']['processAction'] = 'none'; atomic_json(path, ops)
            state = self.a.load_state(); state['paused'] = True; self.a.save_state(state)
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                network.reconcile(self.a, self.a.load_state(), data)
            self.assertNotIn(('terminal', 'close'), calls)
            state['paused'] = False; self.a.save_state(state)
            network.reconcile(self.a, self.a.load_state(), data)
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertNotIn(('orchestration', 'worker-stop'), calls)
        self.assertLess(calls.index(('orchestration', 'worker-abandon')), calls.index(('terminal', 'close')))
        self.assertLess(calls.index(('terminal', 'close')), calls.index(('orchestration', 'worker-start')))
        reg = self.a.launcher.registry(attempt)
        self.assertEqual(len(reg['agents']), 3)
        self.assertEqual(reg['edge_passes'], {'implement': 1})
        self.assertEqual(read_json(command_path)['status'], 'done')
        self.assertEqual(next(iter(network.document(self.a)['sessions'].values()))['replacements'], 1)

    def test_managed_unconfirmed_stop_never_closes_or_replaces(self):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.fact['id'] = 'second'; self.now = 150; self.network_tick()
        self.now = 270; self.network_tick(); self.process()
        self.fact['id'] = 'third'; self.now = 300; self.network_tick()
        original = self.a.launcher.observe_agent
        def managed(agent):
            value = original(agent)
            value['terminalResource']['ownershipState'] = 'managed'
            return value
        with patch.object(self.a.launcher, 'observe_agent', side_effect=managed), \
                patch.object(self.a.launcher, 'liveness', return_value='unverifiable'), \
                patch.object(self.a.launcher, 'close_registered_terminal') as close:
            self.process()
            close.assert_not_called()
        self.assertEqual(len(self.a.launcher.registry(attempt)['agents']), 2)

    def test_lost_external_abandon_receipt_finishes_cleanup_without_replaying(self):
        self.lost_external_cleanup_receipt('worker-abandon')

    def test_lost_external_close_receipt_finishes_replacement_without_replaying(self):
        self.lost_external_cleanup_receipt('close')

    def test_reserved_network_replacement_continues_without_new_reservation(self):
        self.interrupted_network_replacement('reserved')

    def test_network_replacement_registry_precedes_session_journal(self):
        self.interrupted_network_replacement('journal')

    def test_lost_network_terminal_receipt_continues_same_terminal(self):
        self.interrupted_network_replacement('terminal')

    def interrupted_network_replacement(self, phase):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.fact['id'] = 'second'; self.now = 150; self.network_tick()
        self.now = 270; self.network_tick(); self.process()
        self.fact['id'] = 'third'; self.now = 300; session = self.network_tick()
        original = self.a.launcher.orca; mutations = []
        def transport(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] in (('terminal', 'create'), ('orchestration', 'worker-start')):
                mutations.append(args[:2])
                if phase == 'terminal' and args[:2] == ('terminal', 'create') and len(mutations) == 1:
                    raise RuntimeError('lost replacement terminal receipt')
            return result
        self.a.launcher.orca = transport
        error = network.operations.UncertainOperation('interrupted before terminal creation')
        def save(path, value):
            if str(path).endswith('/network-recovery.json') and any(
                    s.get('replacement') for s in value.get('sessions', {}).values()):
                raise error
            return atomic_json(path, value)
        if phase == 'reserved':
            with patch.object(self.a.launcher, '_launch', side_effect=error):
                self.process()
        elif phase == 'journal':
            with patch.object(network, 'atomic_json', side_effect=save):
                self.process()
        else:
            self.process()
        self.assertIn('control_uncertain', self.a.load_state())
        reserved = next(a for a in self.a.launcher.registry(attempt)['agents'] if a.get('network_replacement_of'))
        identifier = session['command_id']
        command = read_json(self.a.state_dir / 'commands' / (identifier + '.json'))
        data = {'_target': str(attempt), 'request': identifier, 'session': command['data']['session']}
        with lock(self.a.state_dir / 'runner.lock'):
            with self.assertRaises(ValueError):
                network.reconcile(self.a, self.a.load_state(), data, observe_only=True)
            self.assertNotIn(('orchestration', 'worker-start'), mutations)
            state = self.a.load_state(); state['paused'] = True; self.a.save_state(state)
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                network.reconcile(self.a, self.a.load_state(), data)
            self.assertNotIn(('orchestration', 'worker-start'), mutations)
            state['paused'] = False; self.a.save_state(state)
            network.reconcile(self.a, self.a.load_state(), data)
        reg = self.a.launcher.registry(attempt)
        replacement = next(a for a in reg['agents'] if a.get('network_replacement_of'))
        self.assertEqual(replacement['id'], reserved['id'])
        self.assertEqual(replacement['status'], 'active')
        self.assertEqual(len(reg['agents']), 3)
        self.assertEqual(reg['edge_passes'], {'implement': 1})
        self.assertEqual(mutations.count(('terminal', 'create')), 1)
        self.assertEqual(mutations.count(('orchestration', 'worker-start')), 1)
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertEqual(next(iter(network.document(self.a)['sessions'].values()))['replacements'], 1)

    def lost_external_cleanup_receipt(self, verb):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.fact['id'] = 'second'; self.now = 150; self.network_tick()
        self.now = 270; self.network_tick(); self.process()
        self.fact['id'] = 'third'; self.now = 300; self.network_tick()
        original = self.a.launcher.orca; mutations = []
        def transport(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] in (('orchestration', 'worker-abandon'), ('terminal', 'close'), ('orchestration', 'worker-start')):
                mutations.append(args[:2])
                if args[1] == verb and mutations.count(args[:2]) == 1:
                    raise RuntimeError('lost cleanup receipt')
            return result
        self.a.launcher.orca = transport
        self.process()
        self.assertIn('control_uncertain', self.a.load_state())
        self.now = 330; self.network_tick(); self.process()
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertEqual(mutations.count(('orchestration', 'worker-abandon')), 1)
        self.assertEqual(mutations.count(('terminal', 'close')), 1)
        self.assertEqual(mutations.count(('orchestration', 'worker-start')), 1)
        self.assertEqual(len(self.a.launcher.registry(attempt)['agents']), 3)
        self.assertEqual(self.network_tick()['replacements'], 1)

    def test_lost_replacement_then_handoff_receipts_never_resend(self):
        self.replacement_receipts(True)

    def replacement_receipts(self, lose_handoff):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.fact['id'] = 'second'; self.now = 150; self.network_tick()
        self.now = 270; self.network_tick(); self.process()
        self.fact['id'] = 'third'; self.now = 300; self.network_tick()
        original = self.a.launcher.orca
        starts, handoffs = [], []
        def transport(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('orchestration', 'worker-start'):
                starts.append(args)
                raise RuntimeError('lost worker-start receipt')
            if args[:2] == ('orchestration', 'send'):
                handoffs.append(args)
                if lose_handoff and len(handoffs) == 1:
                    raise RuntimeError('lost handoff receipt')
            return result
        self.a.launcher.orca = transport
        self.process()
        self.assertIn('control_uncertain', self.a.load_state())
        self.assertTrue(self.a.launcher.registry(attempt)['fenced'])
        self.now = 330; self.network_tick(); self.process()
        if lose_handoff:
            self.assertIn('control_uncertain', self.a.load_state())
            self.now = 350; self.network_tick(); self.process()
        self.assertNotIn('control_uncertain', self.a.load_state())
        reg = self.a.launcher.registry(attempt)
        replacement = next(a for a in reg['agents'] if a.get('network_replacement_of'))
        self.assertEqual(replacement['status'], 'active')
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(handoffs), 1)
        self.now = 360; self.network_tick(); self.process()
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(self.network_tick()['replacements'], 1)

    def test_replacement_dispatch_serializes_with_pause(self):
        self.replacement_cancellation_fence('worker-start')

    def test_replacement_handoff_serializes_with_pause(self):
        self.replacement_cancellation_fence('send')

    def replacement_cancellation_fence(self, action):
        attempt, root, child = self.network_fixture()
        self.network_tick(); self.now = 130; self.network_tick(); self.process()
        self.fact['id'] = 'second'; self.now = 150; self.network_tick()
        self.now = 270; self.network_tick(); self.process()
        self.fact['id'] = 'third'; self.now = 300; self.network_tick()
        original = self.a.launcher.orca
        attempted, committed = threading.Event(), threading.Event()
        serialized, threads = [], []
        def pause():
            attempted.set()
            goal_intent.change(self.a, desired_state='paused')
            committed.set()
        def transport(*args, **kwargs):
            if args[:2] == ('orchestration', action) and not threads:
                thread = threading.Thread(target=pause); threads.append(thread); thread.start()
                self.assertTrue(attempted.wait(1))
                serialized.append(not committed.wait(.1))
            return original(*args, **kwargs)
        self.a.launcher.orca = transport
        try:
            self.process()
        finally:
            for thread in threads: thread.join(2)
        self.assertEqual(serialized, [True], 'pause committed before the effect acceptance')
        self.assertTrue(committed.is_set())
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'paused')

    def test_waiting_parent_business_questions_remain_blockers(self):
        attempt, root = self.prepare(); self.enable()
        self.delegate(attempt, root)
        state = self.a.load_state()
        state['current'].update(dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        message = {'id': 'decision', 'type': 'question', 'run_id': root['run_id'],
                   'payload': {'dispatchId': root['dispatch_id']}, 'body': 'Owner decision required'}
        state['current'].setdefault('messages', []).append(message); self.a.save_state(state)
        self.assertEqual(len(event_scope.pending(state['current'], self.a.launcher.registry(attempt))), 1)
        self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'answer_required')
        message['type'] = 'escalation'; self.a.save_state(state)
        self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'escalation_required')

    def test_stopped_attempt_archives_exhausted_network_incident(self):
        attempt, root, child = self.network_fixture()
        self.network_tick()
        path = self.a.state_dir / 'network-recovery.json'
        journal = read_json(path)
        next(iter(journal['sessions'].values()))['status'] = 'needs_action'
        atomic_json(path, journal)
        self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'network_recovery_required')
        recovery.set_intent(self.a, 'stopped')
        state = self.a.load_state(); state.update(stop_requested=True, stop_status='stopped'); self.a.save_state(state)
        self.assertEqual(protocol.summary(self.a)['observed_state'], 'stopped')
        self.assertIsNone(protocol.summary(self.a)['blocker'])
        self.assertEqual(next(iter(read_json(path)['sessions'].values()))['status'], 'cancelled')

    def test_autonomy_journal_proves_interrupted_configuration(self):
        attempt, root = self.prepare(); self.enable()
        command = self.command(payload={'enabled': False})
        with patch.object(application, 'running', return_value=True):
            application.submit_v1(self.a, command)
        path = self.a.state_dir / 'commands' / (command['request_id'] + '.json')
        record = read_json(path)
        autonomy.configure(self.a, False, record['id'], expected=record['guard'])
        record['status'] = 'running'; atomic_json(path, record)
        self.process()
        self.assertIn('control_uncertain', self.a.load_state())
        state = self.a.load_state(); self.a.recover(state, technical_only=True)
        self.assertNotIn('control_uncertain', self.a.load_state())
        self.assertEqual(read_json(path)['status'], 'unknown')  # Immutable v1 receipt.
        self.assertFalse(autonomy.policy(self.a)['enabled'])

    def test_policy_rejects_future_version_and_non_boolean_permission(self):
        attempt, root = self.prepare(); self.enable()
        path = self.a.state_dir / 'autonomy.json'
        value = read_json(path)
        value['schema_version'] = 2; atomic_json(path, value)
        with self.assertRaises(protocol.ContractError): autonomy.policy(self.a)
        value['schema_version'] = 1; value['enabled'] = 'true'; atomic_json(path, value)
        with self.assertRaises(protocol.ContractError): autonomy.policy(self.a)

    def test_identity_mismatch_and_silence_are_not_network_failure(self):
        attempt, root = self.prepare()
        observed = self.a.launcher.observe_agent(root)
        with patch.object(network, 'codex_failure') as detect:
            self.assertIsNone(network.failure(self.a, root, observed))
            detect.assert_not_called()
            observed['terminal']['incarnationId'] = 'foreign'
            with self.assertRaises(RuntimeError): network.failure(self.a, root, observed)

    def test_network_unknown_effect_and_unconfirmed_stop_block_replacement(self):
        attempt, root, child = self.network_fixture()
        self.network_tick()
        atomic_json(self.a.launcher.journal(attempt), {'unknown': {'status': 'unknown'}})
        self.now = 130
        self.assertEqual(self.network_tick()['status'], 'needs_action')
        self.assertEqual(len(self.a.launcher.registry(attempt)['agents']), 2)

    def test_open_child_defers_parent_recovery_without_spending(self):
        attempt, root, child = self.network_fixture()
        _, tester = self.delegate(attempt, child, 'check')
        session = self.network_tick()
        self.now = 130
        session = self.network_tick()
        self.assertEqual(session['resumes'], 0)
        self.assertEqual(session['reason'], 'waiting for open child call')
        self.assertEqual(session['status'], 'waiting')
        self.assertEqual(len(self.a.launcher.registry(attempt)['agents']), 3)

    def test_fail_repair_recheck_with_limit_and_network_then_acceptance(self):
        from types import SimpleNamespace
        cfg = small_graph(); cfg['edges'][1]['max_passes'] = 1
        attempt, root = self.prepare(cfg); self.enable()
        _, impl = self.delegate(attempt, root)
        _, tester = self.delegate(attempt, impl, 'check')
        self.finish(attempt, tester, 'changes_requested', 'revise')
        with self.assertRaisesRegex(ValueError, 'new successful'):
            graph.return_edge(self.a.launcher, attempt, self.a.launcher.registry(attempt), impl, {'outcome': 'succeeded'})
        blocked, _ = self.delegate(attempt, impl, 'check')
        self.assertEqual(blocked['status'], 'limit')
        autonomy.tick(self.a, self.a.load_state())
        check, fresh = self.delegate(attempt, impl, 'check', request=blocked['id'])
        now = [100]
        supervisor = recovery.Supervisor(None, 'fixture', clock=lambda: now[0])
        fact = {'id': 'network-failure', 'error': 'network_error', 'source': 'fixture'}
        with patch.object(network, 'failure', side_effect=lambda r,a,d: fact if a['id'] == fresh['id'] else None):
            network.supervise(supervisor, self.a, self.a.load_state())
            now[0] = 130
            network.supervise(supervisor, self.a, self.a.load_state())
            self.process()
        self.assertFalse(self.a.load_state().get('control_uncertain'))
        self.finish(attempt, fresh)
        self.finish(attempt, impl)
        self.assertFalse(graph.rework_problems(self.a.launcher.registry(attempt)))
        # Track acceptance closes before the last item's report and unblocks its dependency.
        goal = self.a.repo / application.goals(self.a)[0]
        atomic_json(goal / 'Tasks.json', {'kind': 'goal', 'queue': [
            {'id': 'feature', 'path': 'feat-one', 'status': 'completed'},
            {'id': 'dependent', 'path': 'feat-two', 'status': 'not_started', 'depends_on': ['feature']}]})
        atomic_json(goal / 'feat-one/Tasks.json', {'queue': [{'id': 'T1', 'status': 'completed'}]})
        atomic_json(goal / 'feat-two/Tasks.json', {'queue': [{'id': 'T2', 'status': 'not_started'}]})
        (goal / 'feat-one/evidence/t1').mkdir(parents=True)
        (goal / 'feat-one/evidence/t1/acceptance.md').write_text('Criteria and independent recheck PASS; track accepted.')
        state = self.a.load_state()
        state['current'].update(item='feature/T1', dispatch_id=root['dispatch_id'], terminal=root['terminal'])
        state['run_id'] = root['run_id']; self.a.save_state(state)
        args = SimpleNamespace(goal=str(goal.relative_to(self.a.repo)))
        # Synthetic transport marks only this exact coordinator completed.
        transport = read_json(self.a.launcher.orca.root / 'transport.json')
        transport['workers'][root['dispatch_id']]['done'] = True
        atomic_json(self.a.launcher.orca.root / 'transport.json', transport)
        with patch.object(self.a, 'poll_children'), patch.object(self.a, 'git', return_value='head'), patch.object(self.a, 'notify'):
            self.assertTrue(self.a.finish_item(args, state, {'outcome':'succeeded','body':'accepted'},
                lambda: self.a.verify_item(args, 'feature/T1', 'head', [])))
        from team_runner.tracking import Tracking
        tr = Tracking(goal)
        self.assertEqual(tr.key(tr.next_item()[0], tr.next_item()[2]), 'dependent/T2')
        self.assertIsNone(self.a.load_state()['current'])

    def test_unconfirmed_stop_prevents_new_session(self):
        attempt, root, child = self.network_fixture()
        self.network_tick()
        path = self.a.state_dir / 'network-recovery.json'
        value = read_json(path); session = next(iter(value['sessions'].values()))
        session.update(resumes=2, status='observing', next_check_at=None)
        atomic_json(path, value)
        self.fact['id'] = 'third-failure'; self.now = 130
        self.network_tick()
        with patch.object(self.a.launcher, 'lifecycle', return_value={}):
            self.process()
        self.assertEqual(len(self.a.launcher.registry(attempt)['agents']), 2)
        self.now = 150
        self.assertEqual(self.network_tick()['status'], 'needs_action')
        self.assertIn('stop unconfirmed', self.network_tick()['reason'])

    def test_codex_failure_requires_exact_injected_session_and_new_turn(self):
        attempt, root = self.prepare()
        from team_runner.runner_metrics import PREAMBLE
        text = (PREAMBLE + '\nYour task ID is: ' + root['task_id'] + '\n' +
                '--from ' + root['terminal'] + ' --type worker_done --task-id ' + root['task_id'] +
                ' --dispatch-id ' + root['dispatch_id'] + ' --outcome succeeded')
        sessions = self.root / 'sessions'; sessions.mkdir()
        path = sessions / 'rollout-fixture.jsonl'
        records = [{'type':'session_meta','payload':{'cwd': str(self.a.repo)}},
                   {'type':'event_msg','payload':{'type':'user_message','message': 'quoted: ' + text}},
                   {'timestamp':'failure-1','type':'event_msg','payload':{'type':'task_complete','error':{'message':'stream disconnected'}}}]
        def write(): path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        write(); self.assertIsNone(network.codex_failure(self.a, root, sessions))
        records[1]['payload']['message'] = text
        write(); self.assertEqual(network.codex_failure(self.a, root, sessions)['id'], 'failure-1')
        records.append({'type':'event_msg','payload':{'type':'task_started'}})
        write(); self.assertIsNone(network.codex_failure(self.a, root, sessions))

    def test_fixture_preflight_id_conflict_selectors_and_json_semantics(self):
        from team_runner.verification import fixture_preflight, digest
        from runner_browser_check import BrowserCheck
        html = self.a.repo / 'fixture.html'; html.write_text('<p id="same"></p><div id="same"></div>')
        with self.assertRaisesRegex(ValueError, 'ID conflict'): fixture_preflight(self.a.repo, ['fixture.html'])
        html.write_text('<p id="one"></p><div id="two"></div>')
        self.assertEqual(fixture_preflight(self.a.repo, ['fixture.html'])['result'], 'PASS')
        data = self.a.repo / 'fixture.json'; data.write_text('{"id":1,"id":2}')
        with self.assertRaisesRegex(ValueError, 'key conflict'): fixture_preflight(self.a.repo, ['fixture.json'])
        self.assertEqual(digest(json.loads('{"a":1,"b":2}')), digest(json.loads('{ "b":2, "a":1 }')))
        browser = BrowserCheck(self.root, self.root, 'fixture')
        with patch.object(browser, 'evaluate', return_value=2), self.assertRaisesRegex(AssertionError, 'ambiguous selector'):
            browser.visible('.duplicate')

    def test_time_observation_ignores_non_object_verification_artifacts(self):
        attempt, root = self.prepare()
        state = self.a.load_state()
        registry = self.a.launcher.registry(attempt)
        next(a for a in registry['agents'] if a['id'] == root['id'])['activity'] = 'working'
        self.a.launcher.save_registry(attempt, registry)
        for value in ([], [{'status': 'running'}], 'running', 1, True, None):
            with self.subTest(value=value):
                atomic_json(attempt / 'verification/appendix.json', value)
                self.assertEqual(state_time.reason(self.a, state), 'agent_work')
                self.a.save_state(state)
        atomic_json(attempt / 'verification/native.json', {'status': 'running'})
        self.assertEqual(state_time.reason(self.a, state), 'checks')
        self.a.save_state(state)
        self.assertEqual(state_time.view(self.a)['last']['kind'], 'checks')

    def test_exclusive_time_and_unknown_sampling_gap(self):
        attempt, root = self.prepare()
        (self.a.state_dir / 'state-time.json').unlink()
        with patch.object(state_time, 'reason', return_value='network_wait'):
            state_time.observe(self.a, self.a.load_state(), now=100)
            state_time.observe(self.a, self.a.load_state(), now=110)
        with patch.object(state_time, 'reason', return_value='checks'):
            state_time.observe(self.a, self.a.load_state(), now=120)
            state_time.observe(self.a, self.a.load_state(), now=220)
        view = state_time.view(self.a)
        self.assertEqual(view['seconds']['network_wait'], 20)
        self.assertEqual(view['seconds']['checks'], 30)
        self.assertEqual(view['seconds']['unknown'], 70)
        self.assertEqual(view['total_seconds'], 120)


if __name__ == '__main__':
    unittest.main()
