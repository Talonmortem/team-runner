"""The dashboard hoster reads real fixture state and uses fake effect boundaries."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch
import uuid

from team_runner import application, goal_intent
from team_runner.dashboard_hoster import Hoster, scope_of
from team_runner.protocol import summary
from team_runner.runner_store import lock, read_json

from test_goal_execute import ManagerFixture, Services
import test_platform as fixtures


def identifier():
    return str(uuid.uuid4())


class DashboardHoster(unittest.TestCase):
    def setUp(self):
        fixtures.Platform.setUp(self)
        self.directory = self.root / 'dashboard'
        self.dialog_id = identifier()
        self.model = Mock(return_value='Фикстура')
        self.hoster = Hoster(self.catalog, self.directory, enable_actions=True, model=self.model)
        self.addCleanup(self.hoster.close)
        self.attempt = fixtures.Platform.attempt(self, self.a)
        self.goal = application.goals(self.a)[0]
        goal_intent.authorize(self.a, {'goal': self.goal}, identifier())

    def setup_agent(self, *, questions=()):
        agent = {'id': 'agent-a', 'role': 'coordinator', 'node_id': 'lead',
                 'dispatch_id': 'ctx_a', 'run_id': 'run-a', 'status': 'active',
                 'activity': 'working', 'working_evidence': {'dispatch': 'ctx_a',
                     'source': 'exact fresh agent_status', 'at': datetime.now(timezone.utc).timestamp()}}
        self.a.launcher.save_registry(self.attempt, {'agents': [agent], 'runs': []})
        state = self.a.load_state()
        state['goal'] = self.goal
        state['current']['dispatch_id'] = 'ctx_a'
        state['current']['messages'] = [{'id': q, 'type': 'question', 'run_id': 'run-a',
            'payload': {'dispatchId': 'ctx_a'}, 'body': 'Fixture question'} for q in questions]
        self.a.save_state(state)
        return state

    def chat(self, text, **kwargs):
        kwargs.setdefault('scope', scope_of(summary(self.a)))
        return self.hoster.chat(self.a, self.dialog_id, text, **kwargs)

    def test_codex_lb_receives_selected_facts_and_cannot_execute(self):
        received = []
        class FakeLB(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"/pause"}\n\n'
                                 b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')

        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeLB)
        thread = Thread(target=server.serve_forever)
        thread.start()
        codex = self.root / 'codex'
        codex.mkdir()
        (codex / 'config.toml').write_text('[model_providers.codex-lb]\n'
            f'base_url="http://127.0.0.1:{server.server_port}"\nwire_api="responses"\n')
        try:
            hoster = Hoster(self.catalog, self.directory, enable_actions=True)
            with patch.dict(os.environ, {'CODEX_HOME': str(codex), 'CODEX_LB_API_KEY': 'fixture'}):
                report = hoster.chat(self.a, self.dialog_id, 'Расскажи, что значит /pause')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(report['messages'][-1]['text'], '/pause')
        self.assertEqual(received[0][0], '/responses')
        request = received[0][1]
        self.assertEqual(request['tools'], [])
        self.assertIs(request['store'], False)
        facts = json.loads(request['input'])['team_runner']
        self.assertEqual(facts['identity'], self.a.context.identity())
        self.assertNotIn('messages', facts)
        self.assertFalse((self.a.state_dir / 'commands').exists())
        self.assertEqual(hoster.dialogs(self.b), [])

    def test_watch_survives_closed_client_and_restart_without_model(self):
        state = self.setup_agent()
        report = self.chat('Понаблюдай')
        self.assertEqual(report['watch']['status'], 'active')
        self.assertIn('работает', report['messages'][-2]['text'])
        self.assertIn('Блокер', report['messages'][-1]['text'])
        self.hoster.close()

        # The replacement process observes its persisted subscription by itself.
        self.hoster = Hoster(self.catalog, self.directory, model=self.model, interval=.01)
        self.addCleanup(self.hoster.close)
        self.hoster.start()
        state = self.setup_agent(questions=('q1',))
        path = self.hoster.path(self.a, self.dialog_id)
        deadline = monotonic() + 3
        while read_json(path)['watch']['last']['agents'][0]['state'] != 'waiting' and monotonic() < deadline:
            sleep(.01)
        changed = read_json(path)
        self.assertEqual(changed['watch']['last']['agents'][0]['state'], 'waiting')
        self.assertEqual(changed['watch']['last']['blocker'], 'Answer the pending agent question')
        state['history'] = [{'attempt_dir': str(self.attempt), 'outcome': 'succeeded', 'problems': []}]
        state['current'] = None
        self.a.save_state(state)
        deadline = monotonic() + 3
        while read_json(path)['watch']['status'] == 'active' and monotonic() < deadline:
            sleep(.01)
        result = read_json(path)
        self.assertEqual(result['watch']['status'], 'finished')
        self.assertIn('succeeded', result['messages'][-1]['text'])
        self.model.assert_not_called()

    def test_manual_cancel_ends_subscription(self):
        self.setup_agent()
        self.assertEqual(self.chat('понаблюдай')['watch']['status'], 'active')
        result = self.hoster.cancel(self.a, self.dialog_id)
        self.assertEqual(result['watch']['status'], 'cancelled')
        self.hoster.tick()
        self.assertEqual(result, self.hoster.dialog(self.a, self.dialog_id))
        self.model.assert_not_called()

    def test_stale_revision_foreign_checkout_and_preview_have_no_effects(self):
        self.setup_agent()
        stale = scope_of(summary(self.a))
        state = self.a.load_state()
        state['extra'] = 'changed'
        self.a.save_state(state)
        rejected = self.chat('/pause', scope=stale)['messages'][-1]['operation']
        self.assertEqual(rejected['error']['code'], 'stale_revision')
        foreign = self.chat('/pause', scope=scope_of(summary(self.b)))['messages'][-1]['operation']
        self.assertEqual(foreign['error']['code'], 'identity_mismatch')
        preview = Hoster(self.catalog, self.root / 'preview', model=self.model)
        result = preview.chat(self.a, identifier(), '/pause', scope=scope_of(summary(self.a)))
        self.assertEqual(result['messages'][-1]['operation']['error']['code'], 'actions_disabled')
        self.assertEqual(state, self.a.load_state())
        self.assertFalse((self.a.state_dir / 'commands').exists())

    def test_reply_requires_one_pending_question(self):
        self.setup_agent(questions=('q1',))
        rejected = self.chat('Ответь: продолжайте')['messages'][-1]['operation']
        self.assertEqual(rejected['error']['code'], 'ambiguous_question')
        self.setup_agent(questions=('q1', 'q2'))
        with lock(self.a.state_dir / 'runner.lock'):
            receipt = self.chat('Ответь агенту: продолжайте', message_id='q2')['messages'][-1]['operation']
            self.assertEqual(receipt['status'], 'pending')
        record = read_json(self.a.state_dir / 'commands' / (receipt['request_id'] + '.json'))
        self.assertEqual(record['command']['payload'], {'message_id': 'q2', 'run_id': 'run-a', 'text': 'продолжайте'})

    def test_lost_receipt_is_observed_without_replay_in_old_scope(self):
        self.setup_agent()
        rid = identifier()
        with lock(self.a.state_dir / 'runner.lock'):
            submit_v1 = application.submit_v1

            def lost(*args):
                submit_v1(*args)
                raise TimeoutError('lost HTTP receipt')

            with patch('team_runner.application.submit_v1', side_effect=lost) as mock:
                original = self.chat('/pause', request_id=rid)['messages'][-1]['operation']
                self.assertEqual(original['status'], 'pending')
                # Scope changed before the client recovered the response.
                state = self.a.load_state()
                state['current'] = None
                self.a.save_state(state)
                repeated = self.chat('/pause', request_id=rid)['messages'][-1]['operation']
                self.assertEqual(original, repeated)
                mock.assert_called_once()

    def test_missing_receipt_remains_unknown_across_revisions_and_restarts(self):
        self.setup_agent()
        rid = identifier()
        with lock(self.a.state_dir / 'runner.lock'):
            with patch('team_runner.application.submit_v1', side_effect=TimeoutError) as submit:
                original = self.chat('/pause', request_id=rid)['messages'][-1]['operation']
                self.assertEqual(original['status'], 'unknown')
                submit.assert_called_once()
            state = self.a.load_state()
            state['extra'] = True
            self.a.save_state(state)
            self.hoster = Hoster(self.catalog, self.directory, enable_actions=True)
            self.addCleanup(self.hoster.close)
            with patch('team_runner.application.submit_v1') as submit:
                repeated = self.chat('/pause', request_id=rid)['messages'][-1]['operation']
                self.assertEqual(repeated, original)
                second = self.chat('/pause')['messages'][-1]['operation']
                self.assertEqual(second['error']['code'], 'previous_action_unsettled')
                submit.assert_not_called()

    def test_resume_checks_revision_again_before_effect_and_uses_saved_options(self):
        goal_intent.change(self.a, desired_state='paused')
        intent = goal_intent.read(self.a)
        services, manager = Services(), ManagerFixture()
        start = Mock(return_value={'status': 'starting'})
        execute = application.execute

        def change_then_execute(runner, data):
            state = runner.load_state()
            state['extra'] = 'changed before execute'
            runner.save_state(state)
            return execute(runner, data, services=services, manager=manager, start=start)

        with patch('team_runner.application.execute', side_effect=change_then_execute):
            rejected = self.chat('Продолжи')['messages'][-1]['operation']
        self.assertEqual(rejected['error']['code'], 'stale_revision')
        self.assertEqual(intent, goal_intent.read(self.a))
        self.assertEqual(services.actions, [])
        start.assert_not_called()

        def saved_execute(runner, data):
            return execute(runner, data, services=services, manager=manager, start=start)

        with patch('team_runner.application.execute', side_effect=saved_execute):
            result = self.chat('/resume')['messages'][-1]['operation']
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(start.call_args.args[1], intent['options'])
        self.assertEqual(len(services.actions), 1)

    def test_guarded_resume_supports_legacy_saved_options(self):
        fixtures.Platform.attempt(self, self.b)
        state = self.b.load_state()
        goal = application.goals(self.b)[0]
        options = {'goal': goal, 'mode': 'confirm', 'final': False}
        self.b.save_state({**state, 'goal': goal, 'options': options})
        self.assertEqual(goal_intent.read(self.b), {})
        services, manager = Services(), ManagerFixture()
        start = Mock(return_value={'status': 'starting'})
        execute = application.execute

        def saved_execute(runner, data):
            return execute(runner, data, services=services, manager=manager, start=start)

        with patch('team_runner.application.execute', side_effect=saved_execute):
            result = self.hoster.chat(self.b, self.dialog_id, '/resume', scope=scope_of(summary(self.b)))
        self.assertEqual(result['messages'][-1]['operation']['status'], 'succeeded')
        self.assertEqual(start.call_args.args[1], options)
        self.assertEqual(len(services.actions), 1)
