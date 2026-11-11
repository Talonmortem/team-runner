"""Real isolated services; no registered goals or Orca effects."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from team_runner.catalog import Catalog
from team_runner import project_services as services, runner_ui
from team_runner.runner_entry import process_stamp
from team_runner.runner_store import atomic_json, lock, read_json
from runner_browser_fixture import prepare_checkout


class Services(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'config', self.root / 'state')
        self.manager = services.ProjectServices(self.catalog, timeout=.2, wait_timeout=3)
        self.projects = [self.catalog.project(name)['id'] for name in ('Alpha', 'Beta')]
        self.workspaces = []
        for n, project in enumerate(self.projects):
            repo = self.root / str(n)
            prepare_checkout(repo)
            self.workspaces.append(self.catalog.workspace(project, {'path': str(repo), 'orca_id': 'fixture', 'orca_instance': 'instance'})['id'])
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.projects:
            self.manager.action(p, 'stop', str(uuid.uuid4()))
        for child in self.manager.children:
            child.wait(timeout=5)
        self.temp.cleanup()

    def action(self, action, project=None, request_id=None):
        return self.manager.action(project or self.projects[0], action, request_id or str(uuid.uuid4()))

    def prefix(self, n=0):
        return f'/api/projects/{self.projects[n]}/workspaces/{self.workspaces[n]}'

    def test_concurrent_start_uuid_and_restart_journal(self):
        p = self.projects[0]
        request_id = str(uuid.uuid4())
        with patch.object(self.manager, 'spawn', wraps=self.manager.spawn) as spawn:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda n: self.action('start', request_id=request_id if n < 4 else str(uuid.uuid4())), range(8)))
            self.assertEqual(spawn.call_count, 1)
        self.assertTrue(all(r['status'] == 'done' for r in results))
        self.assertEqual(results[0], results[1])
        with self.assertRaises(ValueError): self.action('stop', request_id=request_id)
        before = self.manager.status(p)
        result = self.action('restart')
        self.assertNotEqual(before['nonce'], result['result']['nonce'])
        self.assertEqual([s['phase'] for s in result['stages']], ['stopping', 'stopped', 'starting'])
        self.assertEqual(self.action('start', request_id=request_id), results[0])
        self.assertEqual(len(self.manager.children), 1)

    def test_cli_port_and_service_lifecycle_share_the_http_manager(self):
        from team_runner.cli import main
        def cli(argv):
            output = io.StringIO()
            with patch('team_runner.cli.Catalog', return_value=self.catalog), redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            return json.loads(output.getvalue())
        project = cli(['project', 'add', '--name', 'CLI', '--service-port', '0'])['id']
        self.projects.append(project)
        self.assertEqual(cli(['project', 'update', '--project', project, '--service-port', '0'])['service_port'], 0)
        request_id = str(uuid.uuid4())
        args = ['service', 'start', '--project', project, '--request-id', request_id]
        first = cli(args)
        self.assertEqual(first, cli(args))
        self.assertEqual(cli(['service', 'status', '--project', project])['state'], 'running')
        self.assertNotEqual(cli(['service', 'restart', '--project', project])['result']['nonce'], first['result']['nonce'])
        self.assertEqual(cli(['service', 'stop', '--project', project])['result']['state'], 'stopped')
        self.assertEqual(cli(['service', 'reconcile', '--project', project])['result']['state'], 'stopped')

    def test_two_projects_isolation_and_manager_restart(self):
        for p in self.projects: self.assertEqual(self.action('start', p)['status'], 'done')
        a, b = [self.manager.status(p) for p in self.projects]
        self.assertNotEqual(a['port'], b['port'])
        control = {'request_id': str(uuid.uuid4())}
        self.assertEqual(self.manager.forward(self.projects[0], self.prefix() + '/pause', control)[0], 200)
        self.assertTrue(self.manager.forward(self.projects[0], self.prefix() + '/state')[1]['state']['paused'])
        self.assertNotIn('paused', self.manager.forward(self.projects[1], self.prefix(1) + '/state')[1]['state'])
        other = services.ProjectServices(self.catalog)
        self.assertEqual(other.status(self.projects[0])['nonce'], a['nonce'])
        with lock(self.catalog.state_dir(self.projects[0], self.workspaces[0]) / 'runner.lock'):
            self.action('restart')
            from team_runner.runner_store import running
            self.assertTrue(running(self.catalog.state_dir(self.projects[0], self.workspaces[0])))
        self.action('stop')
        self.assertEqual(other.status(self.projects[1])['nonce'], b['nonce'])
        self.assertEqual(other.status(self.projects[1])['state'], 'running')

    def test_port_validation_legacy_default_and_busy_port(self):
        p = self.projects[0]
        for port in (-1, 65536, True, '12', 1.5):
            with self.assertRaises(ValueError): self.catalog.project(project_id=p, values={'service_port': port})
        raw = self.catalog.read(); del raw['projects'][p]['service_port']; atomic_json(self.catalog.path, raw)
        self.assertEqual(self.catalog.read()['projects'][p]['service_port'], 0)
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            port = occupied.getsockname()[1]
            self.catalog.project(project_id=p, values={'service_port': port})
            result = self.action('start')
            self.assertEqual(result['status'], 'error')
            self.assertIsNone(result['result']['port'])
        started = self.action('start')['result']
        self.assertEqual(started['port'], port)
        self.catalog.project(project_id=p, values={'service_port': 0})
        self.assertEqual(self.manager.status(p)['port'], port)

    def test_auth_scope_and_secrets(self):
        self.action('start')
        record = self.manager.record(self.projects[0])
        self.assertEqual(services.request(record, '/api/health')[1]['identity'], services.identity(record))
        for path in ('/', '/app.js', '/api/projects', self.prefix(1) + '/state',
                     self.prefix().replace(self.workspaces[0], self.workspaces[1]) + '/state'):
            self.assertEqual(services.request(record, path)[0], 400)
        self.assertEqual(services.request(record, '/api/shutdown', {'identity': {}})[0], 400)
        for headers in ({'Host': 'foreign'}, {'Authorization': 'Bearer wrong'}, {'Origin': 'http://foreign'}):
            conn = http.client.HTTPConnection('127.0.0.1', record['port'], timeout=2)
            base = f"http://127.0.0.1:{record['port']}"
            conn.request('POST', self.prefix() + '/pause', json.dumps({'request_id': str(uuid.uuid4())}),
                         {'Authorization': 'Bearer ' + record['token'], 'Origin': base, 'Content-Type': 'application/json', **headers})
            response = conn.getresponse(); self.assertEqual(response.status, 403); response.read(); conn.close()
        self.assertNotIn(record['token'], json.dumps(self.manager.all()))
        for path in self.manager.directory(self.projects[0]).rglob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.manager.directory(self.projects[0]).stat().st_mode & 0o077, 0)

    def test_unknown_intent_blocks_spawn_and_reconcile_never_replays(self):
        directory = self.manager.directory(self.projects[0])
        atomic_json(directory / 'instance.json', {'project_id': self.projects[0], 'nonce': str(uuid.uuid4()), 'state': 'starting'})
        with patch.object(self.manager, 'spawn') as spawn:
            self.assertEqual(self.action('start')['status'], 'unknown')
            self.assertFalse(self.action('reconcile')['result']['confirmed'])
            self.assertEqual(self.action('restart')['status'], 'unknown')
            spawn.assert_not_called()

    def test_crash_after_spawn_before_ready_and_stale_pid(self):
        def spawn(*_):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.1)'], start_new_session=True)
            self.manager.children.append(child)
            return child
        with patch.object(self.manager, 'spawn', side_effect=spawn) as create:
            result = self.action('start')
            self.assertEqual(create.call_count, 1)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['result']['state'], 'error')
        self.assertEqual(self.action('start')['result']['state'], 'running')
        self.action('stop')
        record = self.manager.record(self.projects[0])
        record.update(state='unknown', pid=os.getpid(), process_stamp='stale-incarnation')
        atomic_json(self.manager.directory(self.projects[0]) / 'instance.json', record)
        with patch('team_runner.project_services.os.kill') as kill:
            self.assertEqual(self.action('reconcile')['result']['state'], 'stopped')
            kill.assert_not_called()

    def test_start_timeout_late_ready_never_creates_duplicate(self):
        # A live child with no readiness publication is not permission to spawn again.
        def sleeper(*_):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
            self.manager.children.append(child)
            return child
        self.manager.wait_timeout = .1
        with patch.object(self.manager, 'spawn', side_effect=sleeper) as spawn:
            request_id = str(uuid.uuid4())
            first = self.action('start', request_id=request_id)
            self.assertEqual(first['status'], 'unknown')
            self.assertEqual(self.action('start', request_id=request_id), first)
            self.assertEqual(self.action('start')['status'], 'unknown')
            self.assertEqual(spawn.call_count, 1)
        child = self.manager.children[-1]
        # Test-owned process, never a PID recovered from persisted service metadata.
        child.terminate(); child.wait(timeout=5)
        self.assertEqual(self.action('reconcile')['result']['state'], 'stopped')
        self.manager.wait_timeout = 3
        self.assertEqual(self.action('start')['result']['state'], 'running')

    def test_same_project_checkouts_are_independent_and_nonce_is_single_use(self):
        project = self.projects[0]
        repo = self.root / 'another-checkout'; prepare_checkout(repo)
        workspace = self.catalog.workspace(project, {'path': str(repo), 'orca_id': 'second', 'orca_instance': 'instance'})
        self.action('start')
        self.manager.forward(project, self.prefix() + '/pause', {'request_id': str(uuid.uuid4())})
        path = f"/api/projects/{project}/workspaces/{workspace['id']}/state"
        self.assertNotIn('paused', self.manager.forward(project, path)[1]['state'])
        nonce = self.manager.status(project)['nonce']
        self.action('stop')
        with self.assertRaises(FileExistsError): services.run_service(self.catalog, project, nonce)
        with self.assertRaises(ValueError): services.run_service(self.catalog, project, str(uuid.uuid4()))
        self.assertEqual(self.manager.status(project)['state'], 'stopped')

    def test_shutdown_drains_accepted_requests_before_unlocking(self):
        project = self.projects[0]
        directory = self.manager.directory(project)
        nonce = str(uuid.uuid4())
        atomic_json(directory / 'instance.json', {'project_id': project, 'nonce': nonce, 'state': 'starting', 'token': 'fixture-secret'})
        accepted, release = threading.Event(), threading.Event()
        original = runner_ui.state_view
        def slow(runner):
            accepted.set()
            release.wait(5)
            return original(runner)
        with patch.object(runner_ui, 'state_view', side_effect=slow):
            child = threading.Thread(target=services.run_service, args=(self.catalog, project, nonce))
            child.start()
            try:
                deadline = time.monotonic() + 3
                while self.manager.status(project)['state'] != 'running' and time.monotonic() < deadline: time.sleep(.02)
                record = self.manager.record(project)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending = pool.submit(services.request, record, self.prefix() + '/state', timeout=4)
                    self.assertTrue(accepted.wait(2))
                    stopping = pool.submit(self.action, 'stop')
                    try:
                        time.sleep(.15)
                        self.assertFalse(stopping.done())
                        self.assertTrue(services.held(directory / 'instance.lock'))
                    finally:
                        release.set()
                    self.assertEqual(pending.result(timeout=3)[0], 200)
                    self.assertEqual(stopping.result(timeout=3)['result']['state'], 'stopped')
            finally:
                release.set()
                child.join(timeout=4)
                self.assertFalse(child.is_alive())

    def test_health_requires_exact_identity_not_just_pid_or_port(self):
        self.action('start')
        record = self.manager.record(self.projects[0])
        original = services.request
        def foreign(record, path, data=None, **kw):
            code, value = original(record, path, data, **kw)
            if path == '/api/health': value['identity']['nonce'] = str(uuid.uuid4())
            return code, value
        with patch.object(services, 'request', side_effect=foreign), patch.object(self.manager, 'spawn') as spawn:
            self.assertEqual(self.manager.status(self.projects[0])['state'], 'unknown')
            self.assertEqual(self.action('restart')['status'], 'unknown')
            self.assertEqual(self.action('start')['status'], 'unknown')
            spawn.assert_not_called()
        self.assertEqual(self.manager.status(self.projects[0])['nonce'], record['nonce'])

    def test_lost_shutdown_response_restart_does_not_spawn(self):
        self.action('start')
        original = services.request
        def lost(record, path, data=None, **kw):
            result = original(record, path, data, **kw)
            if path == '/api/shutdown': raise services.ServiceUnavailable('lost response')
            return result
        request_id = str(uuid.uuid4())
        with patch.object(services, 'request', side_effect=lost), patch.object(self.manager, 'spawn') as spawn:
            first = self.action('restart', request_id=request_id)
            self.assertEqual(first['status'], 'unknown')
            self.assertEqual(self.action('restart', request_id=request_id), first)
            spawn.assert_not_called()
        for child in self.manager.children: child.wait(timeout=5)
        self.assertEqual(self.action('reconcile')['result']['state'], 'stopped')

    def test_lost_start_response_is_durable_and_not_replayed(self):
        request_id = str(uuid.uuid4())
        with patch.object(self.manager, '_wait', side_effect=services.ServiceUnavailable('lost ready response')):
            self.assertEqual(self.action('start', request_id=request_id)['status'], 'unknown')
        deadline = time.monotonic() + 3
        while self.manager.status(self.projects[0])['state'] != 'running' and time.monotonic() < deadline: time.sleep(.05)
        with patch.object(self.manager, 'spawn') as spawn:
            self.assertEqual(self.action('start', request_id=request_id)['status'], 'unknown')
            self.assertEqual(self.action('reconcile')['result']['state'], 'running')
            spawn.assert_not_called()

    def test_public_http_proxy_offline_and_restart(self):
        server, token = runner_ui.make_server(self.catalog, services=self.manager)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        def public(path, data=None):
            return services.request({'port': server.server_port, 'token': token}, path, data)
        try:
            self.assertFalse(public(self.prefix() + '/state')[1]['confirmed'])
            self.assertEqual(public(self.prefix() + '/config')[0], 200)
            self.assertEqual(public(self.prefix() + '/profile')[0], 200)
            self.assertEqual(public(f'/api/projects/{self.projects[0]}/service/start', {})[0], 400)
            self.assertEqual(public(f'/api/projects/{self.projects[0]}/service/start', {'request_id': str(uuid.uuid4())})[1]['status'], 'done')
            before = self.manager.status(self.projects[0])
            secret = self.manager.record(self.projects[0])['token']
            self.assertNotIn(secret, json.dumps(public('/api/services')))
            self.assertTrue(public(self.prefix() + '/state')[1]['confirmed'])
            server.shutdown(); server.server_close(); thread.join()
            server, token = runner_ui.make_server(self.catalog, services=self.manager)
            thread = threading.Thread(target=server.serve_forever); thread.start()
            self.assertEqual(self.manager.status(self.projects[0])['nonce'], before['nonce'])
            self.assertTrue(public(self.prefix() + '/state')[1]['confirmed'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_proxy_post_connection_loss_never_retries(self):
        self.action('start')
        original = services.request
        calls = []
        def lost(record, path, data=None, **kw):
            result = original(record, path, data, **kw)
            if data is not None:
                calls.append(path)
                raise services.ServiceUnavailable('lost post reply')
            return result
        with patch.object(services, 'request', side_effect=lost), self.assertRaises(services.ServiceUnavailable):
            self.manager.forward(self.projects[0], self.prefix() + '/pause', {'request_id': str(uuid.uuid4())})
        self.assertEqual(calls, [self.prefix() + '/pause'])
        self.assertTrue(self.manager.forward(self.projects[0], self.prefix() + '/state')[1]['state']['paused'])

    def test_unresponsive_project_does_not_block_other_project(self):
        self.action('start', self.projects[1])
        with socket.socket() as stalled:
            stalled.bind(('127.0.0.1', 0)); stalled.listen(16)
            record = {'project_id': self.projects[0], 'nonce': str(uuid.uuid4()), 'state': 'running',
                      'port': stalled.getsockname()[1], 'token': 'fixture', 'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid())}
            atomic_json(self.manager.directory(self.projects[0]) / 'instance.json', record)
            start = time.monotonic()
            rows = self.manager.all()
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(rows[0]['state'], 'unknown')
            self.assertFalse(rows[0]['workspaces'][0]['confirmed'])
            self.assertEqual(rows[1]['state'], 'running')
            with patch.object(services.os, 'kill') as kill:
                self.assertEqual(self.action('stop')['status'], 'unknown')
                kill.assert_not_called()


if __name__ == '__main__': unittest.main()
