"""Fresh-shell CLI contracts with isolated roots and fake execute effects."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from team_runner import goal_intent
from team_runner.catalog import Catalog
from team_runner.engine import Runner
from team_runner.runner_store import atomic_json, read_json
from runner_browser_fixture import prepare_checkout


FAKE_EXECUTE = '''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from team_runner import cli
path = Path(os.environ['HEADLESS_EFFECTS'])
def effect(name):
    rows = json.loads(path.read_text()) if path.exists() else []
    rows.append(name)
    path.write_text(json.dumps(rows))
class Manager:
    def __init__(self, *args): pass
    def ensure(self, identifier):
        effect('manager:' + identifier)
        return {'state': 'running', 'confirmed': True}
class Services:
    def __init__(self, *args): pass
    def action(self, project, action, identifier, **kwargs):
        effect('service:' + identifier)
        return {'status': 'done'}
    def status(self, project): return {'state': 'running', 'confirmed': True}
def start(*args, **kwargs):
    effect('runner')
    return {'status': 'starting', 'nonce': 'fixture'}
with patch('team_runner.service_manager.Manager', Manager), patch('team_runner.goal_execute.ProjectServices', Services), patch('team_runner.service.start_runner', start):
    raise SystemExit(cli.main(sys.argv[1:]))
'''


class AgentHeadless(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'config/team-runner', self.root / 'state/team-runner')
        self.project = self.catalog.project('Headless fixture')['id']
        self.repo = self.root / 'checkout'; prepare_checkout(self.repo)
        self.workspace = self.catalog.workspace(self.project, {'path': str(self.repo), 'orca_id': 'fixture',
                                                              'orca_instance': 'instance'})['id']
        self.a = Runner(self.catalog.context(self.project, self.workspace))
        self.scope = ['--project', self.project, '--workspace', self.workspace]
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('TEAM_RUNNER_')}
        self.env.update(XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                        HEADLESS_EFFECTS=str(self.root / 'effects.json'))

    def cli(self, *args, fake=False, env=None, code=0, json_output=True):
        command = [sys.executable, '-c', FAKE_EXECUTE] if fake else [sys.executable, '-m', 'team_runner']
        result = subprocess.run([*command, *args], env={**self.env, **(env or {})}, cwd='/',
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        if json_output:
            self.assertEqual(result.stderr, '')
            return json.loads(result.stdout)  # Rejects prefixes, suffixes, or a second document.
        return result

    def execute(self, identifier, fake=False, code=0, **kwargs):
        return self.cli('execute', *self.scope, '--goal', 'feat/goal-fixture', '--request-id', identifier,
                        '--json', fake=fake, code=code, **kwargs)

    def receipt(self, status='pending'):
        identifier = str(uuid.uuid4())
        command = {'schema_version': 1, 'request_id': identifier, 'action': 'execute',
                   'scope': {'identity': self.a.context.identity(), 'generation': None, 'attempt': None},
                   'payload': {'goal': 'feat/goal-fixture'}}
        path = self.a.state_dir / 'ui/commands' / (identifier + '.json')
        atomic_json(path, {'schema_version': 1, 'id': identifier, 'action': 'execute', 'command': command,
                          'status': status, 'result': None, 'error': None})
        return identifier, path

    def test_fresh_shell_discovers_scope_goals_status_and_messages(self):
        self.assertIn(self.project, self.cli('project', 'list'))
        self.assertIn(self.workspace, self.cli('workspace', 'list', '--project', self.project))
        goals = self.cli('goals', *self.scope, '--json')
        self.assertIn('feat/goal-fixture', goals['goals'])
        status = self.cli('status', *self.scope, '--json')
        self.assertEqual(status['identity'], self.a.context.identity())
        self.assertIn('observed_state', status)
        detail = self.cli('status', *self.scope, '--json', '--detail')
        self.assertIn('state', detail)
        messages = self.cli('messages', *self.scope, '--json')
        self.assertEqual(messages['items'], [])
        self.assertFalse((self.root / 'effects.json').exists())
        self.assertFalse((self.catalog.state_root / 'manager/instance.json').exists())

    def test_operation_show_timeout_and_unknown_exit_contracts(self):
        identifier, path = self.receipt()
        before = path.read_bytes()
        shown = self.cli('operation', 'show', *self.scope, '--request-id', identifier, '--json', code=3)
        self.assertEqual(shown['status'], 'pending')
        result = self.cli('operation', 'wait', *self.scope, '--request-id', identifier,
                          '--timeout', '0.03', '--json', code=4)
        self.assertEqual(result['wait_status'], 'timeout')
        self.assertEqual(result['operation'], shown)
        self.assertEqual(path.read_bytes(), before)
        record = read_json(path); record['status'] = 'unknown'; atomic_json(path, record)
        blocked = self.cli('operation', 'wait', *self.scope, '--request-id', identifier, '--json', code=3)
        self.assertEqual(blocked['wait_status'], 'blocked')
        self.assertEqual(blocked['operation']['status'], 'unknown')
        invalid = self.cli('operation', 'wait', *self.scope, '--request-id', identifier,
                           '--timeout', 'nan', '--json', code=2)
        self.assertEqual(invalid['error']['code'], 'invalid_timeout')

    def test_lost_client_response_duplicate_execute_returns_same_receipt_no_new_effect(self):
        identifier = str(uuid.uuid4())
        first = self.execute(identifier, fake=True)  # Client can lose this reply; persisted receipt remains.
        effects = read_json(self.root / 'effects.json')
        self.assertEqual([entry.split(':')[0] for entry in effects], ['manager', 'service', 'runner'])
        path = self.a.state_dir / 'ui/commands' / (identifier + '.json')
        before = path.read_bytes()
        second = self.execute(identifier)  # New unpatched shell must return before touching effects.
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(read_json(self.root / 'effects.json'), effects)
        shown = self.cli('operation', 'show', *self.scope, '--request-id', identifier, '--json')
        self.assertEqual(shown, first)
        conflict = self.cli('execute', *self.scope, '--goal', 'feat/goal-fixture', '--no-final',
                            '--request-id', identifier, '--json', code=2)
        self.assertEqual(conflict['error']['code'], 'request_conflict')
        self.assertFalse((self.catalog.state_root / 'manager/instance.json').exists())

    def test_messages_cursor_is_deterministic_and_does_not_consume(self):
        self.a.save_state({'run_id': 'fixture-run', 'history': [], 'current': {'attempt_dir': '/fixture/attempt',
                           'messages': [{'id': 'q1', 'type': 'question', 'body': 'First'},
                                        {'id': 'q2', 'type': 'question', 'body': 'Second'}]}})
        before = self.a.state_file.read_bytes()
        first = self.cli('messages', *self.scope, '--limit', '1', '--json')
        self.assertEqual(first['items'][0]['id'], 'q1')
        self.assertTrue(first['has_more'])
        second = self.cli('messages', *self.scope, '--after', first['next_cursor'], '--limit', '1', '--json')
        repeated = self.cli('messages', *self.scope, '--after', first['next_cursor'], '--limit', '1', '--json')
        self.assertEqual(second, repeated)
        self.assertEqual(second['items'][0]['id'], 'q2')
        self.assertEqual(self.a.state_file.read_bytes(), before)

    def test_child_cannot_execute_service_or_control(self):
        attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        self.a.context.snapshot(attempt)
        self.a.save_state({'history': [], 'current': {'attempt_dir': str(attempt)}})
        child = self.a.context.environment(attempt)
        denied = self.execute(str(uuid.uuid4()), env=child, code=2)
        self.assertIn('child', denied['error']['message'])
        stopped = self.cli('stop', *self.scope, '--json', '--request-id', str(uuid.uuid4()),
                           '--generation', 'null', '--attempt', str(attempt), '--revision', 'stale', env=child, code=2)
        self.assertIn('child', stopped['error']['message'])
        service = self.cli('service', 'start', '--project', self.project, env=child, code=1, json_output=False)
        self.assertEqual(service.stdout, '')
        self.assertIn('child', service.stderr)
        self.assertFalse((self.catalog.state_root / 'manager/instance.json').exists())
        self.assertFalse((self.catalog.state_root / 'projects' / self.project / 'service/instance.json').exists())

    def test_delayed_stop_and_reply_do_not_mutate_new_attempt(self):
        goal_intent.authorize(self.a, {'goal': 'feat/goal-fixture'}, str(uuid.uuid4()))
        old_attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        new_attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        self.a.context.snapshot(old_attempt); self.a.context.snapshot(new_attempt)
        self.a.save_state({'run_id': 'fixture-run', 'goal': 'feat/goal-fixture', 'history': [],
                          'current': {'attempt_dir': str(old_attempt), 'messages': [{'id': 'question', 'type': 'question'}]}})
        old = self.cli('status', *self.scope, '--json')
        state = self.a.load_state(); state['current']['attempt_dir'] = str(new_attempt); self.a.save_state(state)
        before = self.a.state_file.read_bytes()
        for action, values in (('stop', []), ('reply', ['question', 'answer'])):
            identifier = str(uuid.uuid4())
            result = self.cli(action, *values, *self.scope, '--json', '--request-id', identifier,
                              '--generation', old['generation'], '--attempt', old['current']['attempt'],
                              '--revision', old['revision'], code=2)
            self.assertIn(result['error']['code'], ('stale_revision', 'stale_scope'))
            self.assertFalse((self.a.state_dir / 'commands' / (identifier + '.json')).exists())
        self.assertEqual(self.a.state_file.read_bytes(), before)
        self.assertFalse((self.root / 'effects.json').exists())

    def test_goal_wait_reads_completion_despite_unknown_execute_receipt(self):
        import test_goal_completion
        from team_runner import goal_completion
        test_goal_completion.GoalCompletion.prepare(self, final=False)
        goal_completion.record(self.a, self.state)
        identifier, path = self.receipt('unknown')
        record = read_json(path)
        intent = goal_intent.read(self.a)
        record['authorization'] = {key: intent[key] for key in ('generation', 'epoch')}
        atomic_json(path, record)
        before = path.read_bytes()
        result = self.cli('operation', 'wait', *self.scope, '--request-id', identifier,
                          '--until', 'goal', '--timeout', '0', '--json')
        self.assertEqual(result['wait_status'], 'succeeded')
        self.assertEqual(result['summary']['observed_state'], 'queue_completed')
        self.assertEqual(result['summary']['final_acceptance']['status'], 'not_required')
        self.assertEqual(result['operation']['status'], 'unknown')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / 'effects.json').exists())

    def test_json_argument_error_does_not_echo_sensitive_argument(self):
        result = self.cli('status', *self.scope, '--json', '--unknown', 'SECRET_FIXTURE_ARGUMENT', code=2)
        self.assertEqual(result['error']['code'], 'invalid_request')
        self.assertNotIn('SECRET_FIXTURE_ARGUMENT', json.dumps(result))
