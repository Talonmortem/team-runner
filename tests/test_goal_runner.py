"""Offline regression checks: no agents, accounts or working goal queues."""
import copy
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

TEAM = Path(__file__).resolve().parents[1] / 'src/team_runner'
sys.path.insert(0, str(TEAM.parent))
sys.path.insert(0, str(TEAM))
from team_runner import runner_config as config
from team_runner import runner_launch as launch
from team_runner import runner_store as store
from team_runner import runner_ui as ui
from team_runner.engine import Runner
from team_runner.catalog import Catalog, Context, PROFILE
from unittest.mock import Mock
runner = Runner(Context('unused', 'unused', TEAM, TEAM, TEAM, TEAM, 'fixture', 'instance', PROFILE, ({}, {})))
launch = runner.launcher
CATALOG = {'profiles': {'named': {'provider': 'deepseek', 'providers': ['deepseek', 'codex-lb']}},
           'base_providers': ['codex-lb', 'deepseek'], 'providers': ['codex-lb', 'deepseek']}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        import subprocess
        repo = base / 'repo'; repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)], check=True)
        self.catalog = Catalog(base / 'config', base / 'state')
        project = self.catalog.project('Fixture')
        ws = self.catalog.workspace(project['id'], {'path':str(repo),'orca_id':'fixture','orca_instance':'instance'})
        ctx = self.catalog.context(project['id'], ws['id'])
        self.root = ctx.state_dir
        self.root.mkdir(parents=True)
        self.prefix = '/api/projects/' + project['id'] + '/workspaces/' + ws['id']
        self.patches = [patch.object(runner, 'context', ctx), patch.object(launch,'context',ctx),
                        patch.object(runner, 'repo', repo),
                        patch.object(runner, 'state_dir', self.root),
                        patch.object(runner, 'state_file', self.root / 'state.json'),
                        patch.object(runner, 'log_file', self.root / 'runner.log'),
                        patch.object(config, 'connections', return_value=CATALOG),
                        patch.object(ui, 'Runner', return_value=runner)]
        for p in self.patches:
            p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def attempt(self):
        attempt = self.root / 'attempts/a'
        if not (attempt / 'context.json').exists(): runner.context.snapshot(attempt)
        store.atomic_json(attempt / 'config.json', config.defaults())
        st = {'history': [], 'run_id': 'parent', 'current': {'item': 'X', 'attempt': 1,
              'attempt_dir': str(attempt), 'dispatch_id': 'owned', 'task_id': 'task',
              'start_head': 'head', 'dirty_before': []}}
        runner.save_state(st)
        return attempt, st


class Configuration(Fixture):
    def test_precedence_and_only_coordinator_override(self):
        store.atomic_json(self.root / 'config.json', {'roles': {'tester': {'model': 'test-model'},
            'coordinator': {'effort': 'medium'}}})
        result = config.load_config(self.root, overrides={'effort': 'high', 'model': 'custom'})
        self.assertEqual(result['roles']['tester']['model'], 'test-model')
        self.assertEqual(result['roles']['coordinator']['model'], 'custom')
        self.assertEqual(result['roles']['coordinator']['effort'], 'high')

    def test_switch_to_codex_uses_sol_high(self):
        role = config.load_config(self.root, overrides={'agent': 'codex'})['roles']['coordinator']
        self.assertEqual((role['model'], role['effort'], role['profile']), ('gpt-6.1-sol', 'high', ''))

    def test_profile_uses_its_provider_unless_explicitly_overridden(self):
        cfg = config.load_config(self.root, overrides={'agent':'codex','profile':'named'})
        self.assertEqual(cfg['roles']['coordinator']['provider'],'')
        cfg = config.load_config(self.root, overrides={'agent':'codex','profile':'named','provider':'codex-lb'})
        self.assertEqual(cfg['roles']['coordinator']['provider'],'codex-lb')

    def test_explicit_config_and_invalid_fields(self):
        with self.assertRaises(ValueError):
            config.load_config(self.root, self.root / 'missing')
        cfg = config.defaults()
        cfg['roles']['tester']['api_key'] = 'never-copy'
        with self.assertRaises(ValueError):
            config.validate(cfg)
        cfg = config.defaults(); cfg['roles']['tester']['provider'] = 'unregistered'
        with self.assertRaises(ValueError):
            config.validate(cfg)

    def test_connection_catalog_does_not_publish_secrets(self):
        (self.root / 'config.toml').write_text('[model_providers.private]\nbase_url="secret-url"\nexperimental_bearer_token="secret"\n')
        (self.root / 'named.config.toml').write_text('model_provider="private"\n')
        # Separate import avoids replacing the shared fixture mock.
        spec = importlib.util.spec_from_file_location('team_runner.catalog_config', TEAM / 'runner_config.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        catalog = module.connections(self.root)
        self.assertIn('named', catalog['profiles'])
        self.assertNotIn('secret', json.dumps(catalog))

    def test_snapshot_immutable_across_settings_changes(self):
        attempt, _ = self.attempt()
        saved = config.defaults(); saved['roles']['tester']['model'] = 'next'
        store.atomic_json(self.root / 'config.json', saved)
        self.assertEqual(config.load_config(self.root)['roles']['tester']['model'], 'next')
        self.assertEqual(store.read_json(attempt / 'config.json')['roles']['tester']['model'], 'gpt-6-luna')

    def test_claude_settings_reference_only(self):
        settings = self.root / 'settings.json'; settings.write_text('{"env":{"KEY":"secret"}}')
        cfg = config.defaults(); cfg['roles']['coordinator']['profile'] = str(settings)
        self.assertNotIn('secret', json.dumps(config.validate(cfg)))
        self.assertIn(str(settings), launch.argv(cfg['roles']['coordinator'], self.root))


class LaunchAndStop(Fixture):
    def fake_orca(self, *args, **kwargs):
        if args[:2] == ('worktree', 'show'):
            return {'worktree':{'id':'fixture','instanceId':'instance','path':str(runner.repo),'hostId':'local'}}
        if args[:2] == ('terminal', 'create'):
            self.command = args[args.index('--command') + 1]
            return {'terminal': {'handle': 'term_own', 'incarnationId': 'inc', 'worktreeId':'fixture'}}
        if args[:2] == ('terminal', 'show'):
            return {'terminal': {'handle': 'term_own', 'incarnationId': 'inc', 'worktreeId':'fixture'}}
        if args[:2] == ('terminal', 'wait'):
            return {'wait': {'satisfied': True}}
        if args[:2] == ('terminal', 'read'):
            return {'terminal': {'source': 'screen', 'tail': ['Ready']}}
        if args[:2] == ('orchestration', 'worker-start'):
            self.assertIn('--terminal', args)
            return {'dispatchId': 'owned'}
        return {}

    def test_both_adapters_are_supervised_and_record_unknown_actual(self):
        for agent in ('codex', 'claude'):
            attempt, _ = self.attempt()
            attempt = self.root / 'attempts' / agent
            runner.context.snapshot(attempt)
            runner.save_state({'current':{'attempt_dir':str(attempt)}})
            cfg = config.defaults()
            cfg['roles']['coordinator'] = config.load_config(self.root, overrides={'agent': agent})['roles']['coordinator']
            store.atomic_json(attempt / 'config.json', cfg)
            with patch.object(launch, 'orca', side_effect=self.fake_orca):
                rec = launch.launch(attempt, 'coordinator', 'task', 'run', 'title', self.root)
            self.assertIsNone(rec['observed'])
            self.assertIn(agent, shlex.split(self.command))
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', self.command)
            self.assertEqual(launch.registry(attempt)['agents'][0]['dispatch_id'], 'owned')

    def test_profile_provider_passed_as_argv(self):
        c = config.defaults()['roles']['tester']; c.update(profile='named', provider='deepseek')
        argv = launch.argv(c, self.root)
        self.assertEqual(argv[argv.index('--profile') + 1], 'named')
        self.assertIn('model_provider="deepseek"', argv)

    def test_codex_account_read_failure_prevents_dispatch_and_replay(self):
        for satisfied in (True, False):
            with self.subTest(satisfied=satisfied):
                attempt, _ = self.attempt()
                launch.save_registry(attempt, {'fenced': False, 'runs': [], 'agents': []})
                def orca(*args, **kwargs):
                    if args[:2] == ('terminal', 'wait'):
                        return {'wait': {'satisfied': satisfied}}
                    if args[:2] == ('terminal', 'read'):
                        self.assertIn('--screen', args)
                        return {'terminal': {'source': 'screen', 'tail': ['Error: account/read failed: unavailable']}}
                    return self.fake_orca(*args, **kwargs)
                with patch.object(launch, 'orca', side_effect=orca) as call:
                    with self.assertRaisesRegex(RuntimeError, 'account/read failed'):
                        launch.launch(attempt, 'tester', 'task', 'run', 'title', self.root)
                    count = call.call_count
                    with self.assertRaisesRegex(RuntimeError, 'fenced'):
                        launch.launch(attempt, 'tester', 'task', 'run', 'title', self.root)
                    self.assertEqual(call.call_count, count)
                self.assertFalse(any(c.args[:2] == ('orchestration', 'worker-start') for c in call.call_args_list))
                reg = launch.registry(attempt)
                self.assertTrue(reg['fenced'])
                self.assertEqual(reg['agents'][0]['status'], 'unknown')
                self.assertEqual(reg['agents'][0]['error'], 'Codex startup: account/read failed')

    def test_codex_screen_fallback_does_not_diagnose_historical_failure(self):
        for source in ('stream', 'screen-unavailable', None):
            with self.subTest(source=source):
                attempt, _ = self.attempt()
                launch.save_registry(attempt, {'fenced': False, 'runs': [], 'agents': []})
                def orca(*args, **kwargs):
                    if args[:2] == ('terminal', 'read'):
                        return {'terminal': {'source': source, 'tail': ['account/read failed']}}
                    return self.fake_orca(*args, **kwargs)
                with patch.object(launch, 'orca', side_effect=orca) as call:
                    with self.assertRaisesRegex(RuntimeError, 'startup screen unavailable'):
                        launch.launch(attempt, 'tester', 'task', 'run', 'title', self.root)
                self.assertFalse(any(c.args[:2] == ('orchestration', 'worker-start') for c in call.call_args_list))

    def test_ambiguous_launch_fences_and_never_replays(self):
        attempt, _ = self.attempt()
        with patch.object(launch, 'orca', side_effect=RuntimeError('transport')) as call:
            with self.assertRaises(RuntimeError):
                launch.launch(attempt, 'tester', 'task', 'run', 'title', self.root)
            with self.assertRaises(RuntimeError):
                launch.launch(attempt, 'tester', 'task', 'run', 'title', self.root)
        self.assertEqual(call.call_count, 1)
        self.assertTrue(launch.registry(attempt)['fenced'])

    def test_only_registered_dispatch_stopped_and_unknown_blocks_retry(self):
        attempt, st = self.attempt()
        launch.save_registry(attempt, {'fenced': False, 'runs': [], 'agents': [
            {'dispatch_id': 'owned', 'status': 'active'}]})
        with patch.object(launch, 'liveness', return_value='live'), patch.object(launch, 'orca', return_value={}) as call:
            with self.assertRaisesRegex(RuntimeError, 'stop not confirmed'):
                runner.apply_control(st, 'stop', {})
        self.assertTrue(any(c.args[:4] == ('orchestration', 'worker-stop', '--dispatch', 'owned') for c in call.call_args_list))
        self.assertEqual(st['stop_status'], 'unknown')
        with self.assertRaises(ValueError):
            runner.apply_control(st, 'retry', {'force': True})
        with patch.object(launch, 'liveness', return_value='exited'):
            runner.apply_control(st, 'stop', {})
        self.assertEqual(st['stop_status'], 'stopped')
        runner.apply_control(st, 'retry', {})
        self.assertEqual(st['current']['attempt'], 2)
        self.assertNotIn('dispatch_id', st['current'])
        self.assertNotIn('task_id', st['current'])

    def test_unknown_liveness_never_stops(self):
        attempt, _ = self.attempt()
        launch.save_registry(attempt, {'fenced': False, 'runs': [], 'agents': [{'dispatch_id':'own','status':'active'}]})
        with patch.object(launch, 'liveness', return_value='unverifiable'), patch.object(launch, 'orca', return_value={}) as call:
            self.assertFalse(launch.stop_attempt(attempt))
        self.assertEqual([c.args[:2] for c in call.call_args_list], [('orchestration', 'worker-show')])

    def test_liveness_ignores_old_limit_text_in_terminal_history(self):
        data = {'projection': {'liveness': {'verdict': 'live'}},
                'terminal': {'tail': ["You've hit your usage limit", 'Working']}}
        with patch.object(launch, 'orca', return_value=data):
            self.assertEqual(launch.liveness('owned'), 'live')

    def test_stopped_attempt_cannot_launch(self):
        attempt, _ = self.attempt()
        self.assertTrue(launch.stop_attempt(attempt))
        with patch.object(launch, 'orca') as call, self.assertRaises(RuntimeError):
            launch.launch(attempt, 'tester', 't', 'r', 'title', self.root)
        call.assert_not_called()

    def test_external_terminal_stop_fences_then_closes_only_owned_process(self):
        attempt, _ = self.attempt()
        record = {'dispatch_id': 'own', 'status': 'active', 'terminal': 'term-own', 'terminal_owned': True,
                  'incarnation': 'inc'}
        launch.save_registry(attempt, {'fenced':False, 'runs':[], 'agents':[record]})
        calls = []
        def orca(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('orchestration','worker-show'):
                return {'terminalResource': {'ownershipState':'external','ownerDispatchId':'own'},
                        'terminal': {'handle':'term-own','incarnationId':'inc', 'worktreeId':'fixture'}}
            if args[:2] == ('terminal','list'):
                return {'terminals':[{'handle':'unrelated'}]}
            return {}
        with patch.object(launch,'orca',side_effect=orca), patch.object(launch,'liveness',return_value='live'):
            self.assertTrue(launch.stop_attempt(attempt))
        self.assertTrue(any(c[:4] == ('orchestration','worker-abandon','--dispatch','own') for c in calls))
        self.assertIn(('terminal','close','--terminal','term-own'),calls)
        self.assertNotIn(('terminal','close','--terminal','unrelated'),calls)

    def test_changed_terminal_identity_is_never_closed(self):
        record = {'dispatch_id':'own','terminal':'term-own','terminal_owned':True,'incarnation':'old'}
        data = {'terminalResource':{'ownershipState':'external','ownerDispatchId':'own'},
                'terminal':{'handle':'term-own','incarnationId':'new'}}
        with patch.object(launch,'orca') as call, self.assertRaises(RuntimeError):
            launch.close_registered_terminal(record,data)
        call.assert_not_called()

    def test_all_roles_use_their_own_snapshot_settings(self):
        attempt, _ = self.attempt()
        cfg = config.defaults()
        for role in config.ROLES:
            cfg['roles'][role]['model'] = 'selected-' + role
        store.atomic_json(attempt / 'config.json', cfg)
        with patch.object(launch,'orca',side_effect=self.fake_orca):
            for role in config.ROLES:
                rec = launch.launch(attempt,role,'task-' + role,'run',role,self.root,function=role)
                self.assertIn('selected-' + role,shlex.split(self.command))
                self.assertEqual(rec['role'],role)



class Controls(Fixture):
    def test_runner_lock_excludes_another_process(self):
        with store.lock(self.root / 'runner.lock'):
            self.assertTrue(store.running(self.root))
            with self.assertRaises(RuntimeError):
                with store.lock(self.root / 'runner.lock', blocking=False):
                    pass
        self.assertFalse(store.running(self.root))

    def test_duplicate_command_exactly_once(self):
        key = str(uuid.uuid4())
        a = runner.submit_control('pause', {}, key)
        b = runner.submit_control('pause', {}, key)
        self.assertEqual(a, b)
        self.assertTrue(runner.load_state()['paused'])
        with self.assertRaises(ValueError):
            runner.submit_control('stop', {}, key)

    def test_pause_preserves_current_and_retry_refreshes(self):
        attempt, st = self.attempt()
        runner.apply_control(st, 'pause', {})
        self.assertEqual(st['current']['dispatch_id'], 'owned')
        cfg = config.defaults(); cfg['roles']['tester']['model'] = 'new-model'
        store.atomic_json(runner.context.settings_dir / 'config.json', cfg)
        st['stop_status'] = 'stopped'
        runner.apply_control(st, 'retry', {'refresh': True})
        self.assertEqual(st['current']['retry_config']['roles']['tester']['model'], 'new-model')

    def test_legacy_active_dispatch_is_not_launched_again(self):
        st = {'history': [], 'run_id': 'r', 'current': {'item':'X','dispatch_id':'old','task_id':'task'}}
        args = type('Args', (), {'mode':'auto'})()
        with patch.object(launch, 'launch') as call:
            self.assertEqual(runner.start_coordinator(args, st, 'X', 'spec', 'title'), 'start')
        call.assert_not_called()
        self.assertNotIn('requested', st['current'])

    def test_mail_persisted_before_ack_and_replay_deduplicated(self):
        _, st = self.attempt()
        message = {'id':'msg','type':'question','body':'Question?'}
        rec = {'messages':[message], 'deliveryId':'delivery'}
        runner.persist_messages(st, rec, 'r')
        def ack(*args, **kwargs):
            self.assertEqual(runner.load_state()['current']['messages'][0]['id'], 'msg')
            return {'messages':[message]}
        with patch.object(runner, 'orca', side_effect=ack):
            runner.ack_delivery(st, 'delivery', 'r')
        self.assertEqual(len(st['current']['messages']), 1)

    def test_gate_and_reply_commands(self):
        _, st = self.attempt(); st['current']['gate_id'] = 'gate'
        st['current']['messages'] = [{'id':'q','run_id':'parent','type':'question','body':'?'}]
        with patch.object(runner, 'orca', return_value={}) as call:
            runner.apply_control(st, 'resolve', {'choice':'skip'})
            runner.apply_control(st, 'reply', {'message_id':'q','text':'answer'})
            runner.apply_control(st, 'reply', {'message_id':'q','text':'answer'})
        self.assertEqual(call.call_count, 2)
        self.assertTrue(st['current']['messages'][0]['answered'])

    def test_crashed_command_not_replayed(self):
        rec = store.enqueue(self.root, 'stop'); rec['status'] = 'applying'
        store.atomic_json(self.root / 'commands' / (rec['id']+'.json'), rec)
        st = {'history': []}
        with patch.object(runner, 'apply_control') as call:
            runner.process_controls(st)
        call.assert_not_called()
        self.assertEqual(st['control_uncertain'], rec['id'])

    def test_stale_attempt_control_rejected(self):
        _, st = self.attempt()
        with self.assertRaises(ValueError):
            runner.apply_control(st, 'stop', {'_target':'old-attempt'})
        self.assertNotIn('stop_requested',st)

    def test_persisted_completion_survives_ack_then_crash(self):
        _, st = self.attempt()
        st['current']['messages'] = [{'id':'done','type':'worker_done','body':'accepted',
            'payload':{'dispatchId':'owned','outcome':'succeeded'}}]
        with patch.object(runner,'orca') as call:
            self.assertEqual(runner.wait_coordinator(st)['outcome'],'succeeded')
        call.assert_not_called()



class HTTP(Fixture):
    def test_disconnected_client_does_not_trigger_another_response(self):
        for error in (BrokenPipeError, ConnectionResetError, TimeoutError):
            for failed_write in (1, 2):
                with self.subTest(error=error, failed_write=failed_write):
                    handler = object.__new__(self.server.RequestHandlerClass)
                    handler.request_version = 'HTTP/1.1'
                    handler.command = 'GET'
                    handler.requestline = 'GET / HTTP/1.1'
                    handler.path = '/'
                    handler.headers = {'Host': f'127.0.0.1:{self.server.server_port}'}
                    handler.server = self.server
                    handler.scope = Mock()
                    handler.wfile = Mock()
                    handler.wfile.write.side_effect = [None] * (failed_write - 1) + [error()]
                    handler.do_GET()
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(handler.wfile.write.call_count, failed_write)

    def setUp(self):
        super().setUp()
        store.atomic_json(runner.repo / 'feat/goal-fixture/Tasks.json', {'kind':'goal','queue':[]})
        self.server, self.token = ui.make_server(self.catalog, project_id=runner.context.project_id)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, data=None, token=True, origin=True):
        headers = {'Content-Type':'application/json'}
        if token: headers['Authorization'] = 'Bearer ' + self.token
        if origin: headers['Origin'] = self.url
        req = urllib.request.Request(self.url + self.prefix + path.removeprefix('/api'), headers=headers,
            data=json.dumps(data).encode() if data is not None else None)
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def test_token_origin_and_no_shell_endpoint(self):
        for token, origin in ((False, True), (True, False)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/pause', {'request_id':str(uuid.uuid4())}, token, origin)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/shell', {'cmd':'touch /tmp/no'})
        self.assertEqual(error.exception.code, 404)

    def test_state_read_does_not_consume_mail(self):
        with patch.object(runner, 'orca') as call:
            self.request('/api/state'); self.request('/api/state')
        call.assert_not_called()

    def test_config_save_reset_and_idempotent_pause(self):
        cfg = config.defaults(); cfg['roles']['tester']['model'] = 'configured'
        self.request('/api/config', cfg)
        self.assertEqual(self.request('/api/config')['config']['roles']['tester']['model'], 'configured')
        self.request('/api/config', {'reset':True})
        req = {'request_id':str(uuid.uuid4())}
        self.assertEqual(self.request('/api/pause', req), self.request('/api/pause', req))

    def test_server_restart_preserves_state_without_orca_calls(self):
        attempt, state = self.attempt()
        runner.apply_control(state, 'pause', {})
        other, token = ui.make_server(self.catalog)
        thread = threading.Thread(target=other.serve_forever,daemon=True); thread.start()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{other.server_port}{self.prefix}/state',
                                         headers={'Authorization':'Bearer '+token})
            with patch.object(runner,'orca') as call, urllib.request.urlopen(req) as response:
                restored = json.load(response)
            self.assertEqual(restored['state']['current']['dispatch_id'],'owned')
            self.assertTrue(restored['state']['paused'])
            call.assert_not_called()
        finally:
            other.shutdown(); other.server_close(); thread.join()

    def test_unavailable_orca_start_is_unknown_and_not_repeated(self):
        req = {'request_id':str(uuid.uuid4()),'goal':'feat/goal-fixture'}
        with patch.object(runner,'orca',side_effect=RuntimeError('runtime_unavailable')) as call:
            first = self.request('/api/start',req)
            self.assertEqual(first['status'],'unknown')
            self.assertEqual(self.request('/api/start',req),first)
            self.assertEqual(call.call_count,1)
            # Verification is read-only; a fresh explicit request may retry it.
            # The unknown receipt itself remains immutable and never replays.
            req['request_id'] = str(uuid.uuid4())
            second = self.request('/api/start',req)
            self.assertEqual(second['status'],'unknown')
            self.assertEqual(self.request('/api/start',req),second)
        self.assertEqual(call.call_count,2)
        self.assertTrue(all(c.args[:2] == ('worktree','show') for c in call.call_args_list))
        self.assertIsNone(store.read_json(self.root/'startup.json'))
        self.assertIsNone(store.read_json(self.root/'launch.json'))
        reservations = list((self.root/'start-reservations').glob('*.json'))
        self.assertEqual(len(reservations),2)
        self.assertTrue(all(store.read_json(path)['status'] == 'cancelled' for path in reservations))

    def test_lost_terminal_create_response_blocks_fresh_start(self):
        req = {'request_id':str(uuid.uuid4()),'goal':'feat/goal-fixture'}
        def transport(*args, **kwargs):
            if args[:2] == ('terminal','create'):
                raise RuntimeError('response_lost')
            return LaunchAndStop.fake_orca(self,*args,**kwargs)
        with patch.object(runner,'orca',side_effect=transport) as call:
            first = self.request('/api/start',req)
            self.assertEqual(first['status'],'unknown')
            self.assertEqual(self.request('/api/start',req),first)
            count = call.call_count
            fresh = self.request('/api/start',{**req,'request_id':str(uuid.uuid4())})
            self.assertEqual(fresh['status'],'unknown')
            self.assertEqual(call.call_count,count)
        self.assertEqual(sum(c.args[:2] == ('terminal','create') for c in call.call_args_list),1)
        self.assertEqual(store.read_json(self.root/'startup.json')['status'],'unknown')

    def test_ui_uses_the_cli_selected_config_file(self):
        custom = self.root / 'custom.json'
        store.atomic_json(custom,config.defaults())
        runner.save_state({'history':[],'options':{'config':str(custom)}})
        cfg = self.request('/api/config')['config']
        cfg['roles']['tester']['model'] = 'custom-model'
        self.request('/api/config',cfg)
        self.assertEqual(store.read_json(custom)['roles']['tester']['model'],'custom-model')
        self.assertFalse((self.root/'config.json').exists())

    def test_duplicate_start_does_not_create_two_terminals(self):
        request = {'request_id':str(uuid.uuid4()), 'goal':'feat/goal-fixture','mode':'confirm'}
        with patch.object(runner, 'orca', side_effect=LaunchAndStop.fake_orca.__get__(self)) as call:
            a = self.request('/api/start', request); b = self.request('/api/start', request)
        self.assertEqual(a, b); self.assertEqual(sum(c.args[:2] == ('terminal','create') for c in call.call_args_list), 1)
        with patch.object(runner, 'orca') as call:
            result = self.request('/api/start', {**request,'request_id':str(uuid.uuid4())})
        self.assertEqual(result['status'], 'error'); call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
