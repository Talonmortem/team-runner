"""Versioned projections are read-only, conservative and shared by transports."""
import contextlib
import io
import json
from pathlib import Path
import threading
import unittest
import urllib.request
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, cli, protocol, runner_ui
from team_runner.runner_store import lock, atomic_json, enqueue


class Protocol(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def command(self, *arguments):
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main([*arguments, '--project', self.a.context.project_id,
                             '--workspace', self.a.context.workspace_id])
        return code, json.loads(out.getvalue())

    def http(self, route):
        server, token = runner_ui.make_server(self.catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ctx = self.a.context
            url = f'http://127.0.0.1:{server.server_port}/api/projects/{ctx.project_id}/workspaces/{ctx.workspace_id}/{route}'
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_cli_http_parity_without_service_or_state_creation(self):
        with patch('team_runner.project_services.ProjectServices.forward', side_effect=AssertionError('RPC')), \
                patch.object(self.a, 'orca', side_effect=AssertionError('Orca')):
            before = self.a.state_dir.exists()
            code, summary = self.command('status', '--summary')
            self.assertEqual(code, 0)
            self.assertEqual(summary, self.http('summary'))
            self.assertEqual(summary, application.summary(self.a))
            code, goals = self.command('goals')
            self.assertEqual(goals, self.http('goals'))
            self.assertEqual(self.a.state_dir.exists(), before)
            self.assertEqual(summary['desired_state'], 'paused')
            self.assertEqual(summary['compatibility'], 'legacy')

    def test_idle_summary_matches_versioned_fixture(self):
        result = protocol.summary(self.a)
        result['identity'] = '<workspace identity>'
        result['revision'] = '<opaque revision>'
        fixture = Path(__file__).with_name('fixtures') / 'protocol-v1' / 'idle-summary.json'
        self.assertEqual(result, json.loads(fixture.read_text()))

    def test_revision_changes_and_torn_read_has_no_actions(self):
        before = protocol.summary(self.a)
        self.a.save_state({'history': [], 'paused': True})
        self.assertNotEqual(before['revision'], protocol.summary(self.a)['revision'])
        capture = protocol._capture
        calls = []
        def changing(runner):
            value = capture(runner)
            value['state']['counter'] = len(calls)
            calls.append(True)
            return value
        with patch.object(protocol, '_capture', side_effect=changing):
            result = protocol.summary(self.a)
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['available_actions'], [])
        self.assertEqual(result['blocker']['code'], 'observation_changed')

    def test_starting_cannot_offer_another_start_and_question_can_pause(self):
        from team_runner import goal_intent
        goal = application.goals(self.a)[0]
        goal_intent.authorize(self.a, {'goal': goal}, str(uuid.uuid4()))
        atomic_json(self.a.state_dir / 'startup.json', {'status': 'starting'})
        view = protocol.summary(self.a)
        self.assertEqual(view['observed_state'], 'starting')
        self.assertEqual(view['available_actions'], [{'action': 'stop'}])
        self.a.save_state({'goal': goal, 'history': [], 'current': {'item': 'one',
                          'messages': [{'id': 'q', 'type': 'question', 'run_id': 'r'}]}})
        with lock(self.a.state_dir / 'runner.lock'):
            view = protocol.summary(self.a)
        self.assertEqual(view['blocker']['code'], 'answer_required')
        self.assertIn({'action': 'pause'}, view['available_actions'])

    def test_stopped_attempt_offers_retry_instead_of_answering_closed_resources(self):
        from team_runner import goal_intent
        goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        attempt = fixtures.Platform.attempt(self, self.a)
        goal_intent.change(self.a, desired_state='stopped')
        self.a.save_state({'history': [], 'stop_status': 'stopped', 'stop_requested': True,
                          'current': {'attempt_dir': str(attempt), 'messages': [
                              {'id': 'q', 'run_id': 'r', 'type': 'question'}]}})
        atomic_json(self.a.state_dir / 'recovery.json', {'status': 'needs_action', 'next_check_at': 99})
        view = protocol.summary(self.a)
        self.assertEqual(view['recovery']['status'], 'observing')
        self.assertIsNone(view['recovery']['next_check'])
        self.assertEqual(view['observed_state'], 'stopped')
        self.assertIsNone(view['blocker'])
        self.assertEqual(view['available_actions'], [{'action': 'retry'}])
        self.a.save_state({'history': [], 'current': {'attempt_dir': str(attempt), 'failed': True}})
        goal_intent.change(self.a, desired_state='paused')
        self.assertEqual(protocol.summary(self.a)['available_actions'], [{'action': 'stop'}, {'action': 'retry'}])

    def test_force_retry_available_only_for_known_stalled_attempt(self):
        from team_runner import goal_intent
        goal = application.goals(self.a)[0]
        goal_intent.authorize(self.a, {'goal': goal}, str(uuid.uuid4()))
        attempt = fixtures.Platform.attempt(self, self.a)
        state = {'goal': goal, 'current': {'item': 'one', 'attempt': 3, 'attempt_dir': str(attempt)}}
        self.a.save_state(state)
        self.assertIn({'action': 'retry'}, protocol.summary(self.a)['available_actions'])
        for change in ({'control_uncertain': 'lost'}, {'recovery_issues': ['unknown resource']}):
            self.a.save_state(dict(state, **change))
            self.assertNotIn({'action': 'retry'}, protocol.summary(self.a)['available_actions'])
        for change in ({'gate_id': 'g'}, {'messages': [{'type': 'question'}]}):
            self.a.save_state(dict(state, current=dict(state['current'], **change)))
            self.assertNotIn({'action': 'retry'}, protocol.summary(self.a)['available_actions'])
        self.a.save_state(state)
        with lock(self.a.state_dir / 'runner.lock'):
            self.assertNotIn({'action': 'retry'}, protocol.summary(self.a)['available_actions'])

    def test_legacy_final_history_is_not_current_acceptance(self):
        self.a.save_state({'history': [{'item': 'GOAL-FINAL-REVIEW', 'problems': []}]})
        result = protocol.summary(self.a)
        self.assertEqual(result['final_acceptance']['status'], 'legacy_unverified')
        self.assertNotEqual(result['observed_state'], 'completed')

    def test_escalation_requires_review_and_stop_without_unsupported_reply(self):
        escalation = {'id': 'e', 'run_id': 'run', 'type': 'escalation', 'body': 'Need intervention'}
        self.a.save_state({'history': [], 'run_id': 'run', 'current': {'item': 'T1', 'messages': [escalation]}})
        result = protocol.summary(self.a)
        self.assertEqual(result['blocker']['code'], 'escalation_required')
        self.assertIn('diagnostics', result['blocker']['message'])
        self.assertEqual(result['available_actions'], [{'action': 'stop'}])
        self.assertEqual(result['blocker']['actions'], ['stop'])

    def test_question_reply_remains_available_alongside_escalation(self):
        escalation = {'id': 'e', 'run_id': 'run', 'type': 'escalation'}
        question = {'id': 'q', 'run_id': 'run', 'type': 'question'}
        self.a.save_state({'history': [], 'current': {'item': 'T1', 'messages': [escalation, question]}})
        result = protocol.summary(self.a)
        self.assertEqual(result['blocker']['code'], 'answer_required')
        self.assertIn({'action': 'reply'}, result['available_actions'])
        question['answered'] = True
        self.a.save_state({'history': [], 'current': {'item': 'T1', 'messages': [escalation, question]}})
        result = protocol.summary(self.a)
        self.assertEqual(result['blocker']['code'], 'escalation_required')
        self.assertNotIn({'action': 'reply'}, result['available_actions'])
        self.a.save_state({'history': [], 'control_uncertain': 'unknown', 'current': {'item': 'T1', 'messages': [escalation]}})
        result = protocol.summary(self.a)
        self.assertEqual(result['blocker']['code'], 'unknown_effect')
        self.assertEqual(result['available_actions'], [{'action': 'reconcile'}])

    def test_old_recovery_incident_does_not_block_prepared_retry(self):
        state = {'history': [], 'goal': application.goals(self.a)[0],
                 'current': {'item': 'one', 'attempt': 2, 'retry_config': {'version': 2}}}
        self.a.save_state(state)
        incident = self.a.state_dir / 'recovery.json'
        atomic_json(incident, {'status': 'needs_action', 'target': '/old/attempt',
                              'reason': 'old launch failed', 'next_check_at': 99})
        before = incident.read_bytes()
        with patch.object(self.a, 'orca', side_effect=AssertionError('read only')):
            result = protocol.summary(self.a)
        self.assertIsNone(result['blocker'])
        self.assertEqual(result['observed_state'], 'paused')
        self.assertIn({'action': 'start'}, result['available_actions'])
        self.assertEqual(result['recovery']['status'], 'observing')
        self.assertIsNone(result['recovery']['next_check'])
        self.assertEqual(incident.read_bytes(), before)
        for extra, code in (({'control_uncertain': 'unknown'}, 'unknown_effect'),
                            ({'recovery_issues': ['current problem']}, 'recovery_required')):
            self.a.save_state({**state, **extra})
            self.assertEqual(protocol.summary(self.a)['blocker']['code'], code)
        self.a.save_state(state)
        for target in ('one', None):
            atomic_json(incident, {'status': 'needs_action', 'target': target})
            self.assertEqual(protocol.summary(self.a)['blocker']['code'], 'recovery_required')

    def test_unknown_effect_dominates_other_actions(self):
        self.a.save_state({'history': [], 'current': {'item': 'x', 'failed': True}, 'control_uncertain': 'lost'})
        result = protocol.summary(self.a)
        self.assertEqual(result['blocker']['code'], 'unknown_effect')
        self.assertEqual(result['available_actions'], [{'action': 'reconcile'}])

    def test_unconfirmed_stop_explains_resource_problem_and_safe_action(self):
        reason = 'final_reviewer_critical: launch process identity no longer live'
        self.a.save_state({'history': [], 'stop_requested': True, 'stop_status': 'unknown',
                          'recovery_issues': [reason], 'current': {'item': 'one'}})
        before = (self.a.state_dir / 'state.json').read_bytes()
        with patch.object(self.a, 'orca', side_effect=AssertionError('read must not call Orca')):
            result = protocol.summary(self.a)
        self.assertEqual(result['blocker']['code'], 'unknown_effect')
        self.assertIn(reason, result['blocker']['message'])
        self.assertIn('Сверить с Orca', result['blocker']['message'])
        self.assertEqual(result['blocker']['facts']['stop_status'], 'unknown')
        self.assertEqual(result['available_actions'], [{'action': 'reconcile'}])
        self.assertEqual((self.a.state_dir / 'state.json').read_bytes(), before)

    def test_operation_lookup_preserves_legacy_receipt_and_no_replay(self):
        identifier = str(uuid.uuid4())
        record = enqueue(self.a.state_dir / 'ui', 'start', {'goal': 'feat/x'}, identifier)
        record['status'] = 'applying'
        path = self.a.state_dir / 'ui/commands' / (identifier + '.json')
        atomic_json(path, record)
        before = path.read_bytes()
        code, result = self.command('operation', identifier)
        self.assertEqual(code, 0)
        self.assertEqual(result, self.http('operations/' + identifier))
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['scope']['generation'])
        self.assertEqual(before, path.read_bytes())
        self.assertFalse((self.a.state_dir / 'goal-intent.json').exists())

    def test_unsupported_version_and_identity_have_structured_errors(self):
        path = self.a.state_dir / 'goal-intent.json'
        atomic_json(path, {'schema_version': 9})
        code, result = self.command('status', '--summary')
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'unsupported_version')
        atomic_json(path, {'schema_version': 1, 'identity': self.b.context.identity()})
        with self.assertRaises(protocol.ContractError) as error:
            protocol.summary(self.a)
        self.assertEqual(error.exception.code, 'identity_mismatch')

    def test_operation_ids_cannot_escape_scope_and_duplicates_are_ambiguous(self):
        with self.assertRaises(protocol.ContractError) as error:
            protocol.operation(self.a, '../state')
        self.assertEqual(error.exception.code, 'invalid_request_id')
        identifier = str(uuid.uuid4())
        enqueue(self.a.state_dir, 'pause', {}, identifier)
        enqueue(self.a.state_dir / 'ui', 'start', {}, identifier)
        with self.assertRaises(protocol.ContractError) as error:
            protocol.operation(self.a, identifier)
        self.assertEqual(error.exception.code, 'ambiguous_legacy_operation')

    def test_invalid_cli_selection_keeps_versioned_error_envelope(self):
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(['status', '--summary', '--project', 'missing', '--workspace', 'missing'])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['error']['code'], 'invalid_request')
        self.assertFalse(result['error']['retryable'])

    def test_blocker_and_actions_fixture(self):
        self.a.save_state({'history': [], 'control_uncertain': 'lost-request'})
        result = protocol.summary(self.a)
        fixture = Path(__file__).with_name('fixtures') / 'protocol-v1' / 'unknown-blocker.json'
        self.assertEqual(result['blocker'], json.loads(fixture.read_text()))
        self.assertEqual(result['available_actions'], [{'action': 'reconcile'}])
