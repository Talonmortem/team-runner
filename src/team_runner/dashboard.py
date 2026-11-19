"""Independent loopback dashboard. Workspace reads and dashboard sessions are separate."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import webbrowser

from . import application, dashboard_view, orca
from .dashboard_hoster import Hoster
from .dashboard_usage import UsageCollector
from .engine import Runner
from .infra_observer import Observer
from .project_services import ProjectServices
from .protocol import ContractError
from .runner_store import lock

_ASSETS = Path(__file__).with_name('dashboard-ui')
_TYPES = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/style.css': ('style.css', 'text/css')}
_WORKSPACE = re.compile(r'/api/projects/([^/]+)/workspaces/([^/]+)/(.*)')
_ERRORS = (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError)


class UsagePoller:
    def __init__(self, catalog, collector, interval):
        self.catalog, self.collector, self.interval = catalog, collector, interval
        self.value = {'collected_at': None, 'current': {}, 'history': {}, 'status': 'collecting'}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.poll, name='dashboard-usage', daemon=True)

    def poll(self):
        while not self.stop.is_set():
            try:
                self.value = {**self.collector.collect(self.catalog), 'status': 'ready'}
            except _ERRORS:
                self.value = {**self.value, 'status': 'unavailable'}
            self.stop.wait(self.interval)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)


def _projects(catalog, enable_actions):
    return {'enable_actions': enable_actions, 'projects': {
        project_id: {'id': project_id, 'name': project['name'], 'workspaces': {
            workspace_id: {'id': workspace_id, **{key: row[key] for key in ('name', 'path', 'disabled')}}
            for workspace_id, row in project['workspaces'].items()}}
        for project_id, project in catalog.read()['projects'].items()}}


def make_server(catalog, port=0, *, session_dir=None, enable_actions=False, services=None,
                usage_collector=None, model=None, watch_interval=10, usage_interval=60):
    if session_dir is None:
        session_dir = catalog.state_root.parent / 'team-runner-dashboard' / (
            'control' if enable_actions else 'preview')
    session_dir = Path(session_dir).resolve()
    if any(session_dir.is_relative_to(root) for root in (catalog.state_root, catalog.config_root)):
        raise ValueError('Dashboard sessions require a directory outside Team Runner roots')
    if enable_actions and os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ValueError('Child attempts cannot enable dashboard actions')
    token = secrets.token_urlsafe(32)
    services = services or ProjectServices(catalog)
    observer = Observer(catalog)
    hoster = Hoster(catalog, session_dir, enable_actions=enable_actions, model=model, interval=watch_interval)
    usage = UsagePoller(catalog, usage_collector or UsageCollector(), usage_interval)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def send(self, status, value, content_type='application/json'):
            body = json.dumps(value, ensure_ascii=False).encode() if content_type == 'application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True

        def authorized(self, mutate):
            host = f'127.0.0.1:{self.server.server_port}'
            accepted = (self.headers.get('Host') == host and
                        (not self.path.startswith('/api/') or
                         secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token))
                        and (not mutate or self.headers.get('Origin') == 'http://' + host))
            if not accepted:
                self.send(403, {'error': {'code': 'forbidden', 'message': 'Dashboard authorization failed'}})
            return accepted

        def body(self):
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 128_000:
                raise ValueError('Expected a JSON body up to 128 KB')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            return data

        def workspace(self, runner, route, mutate):
            if route == 'state' and not mutate:
                return dashboard_view.view(runner, services, observer, enable_actions=enable_actions)
            if route == 'commands' and mutate:
                if not enable_actions:
                    raise ContractError('actions_disabled', 'Escalation delivery requires --enable-actions')
                command = self.body()
                payload = command.get('payload')
                if (command.get('action') != 'resolve-escalation' or not isinstance(payload, dict)
                        or payload.get('resume') is not True):
                    raise ContractError('action_unavailable', 'Dashboard command must send an escalation decision')
                return application.submit_v1(runner, command)
            if route == 'focus' and mutate:
                if not enable_actions:
                    raise ContractError('actions_disabled', 'Terminal switching requires --enable-actions')
                return dashboard_view.focus(runner, self.body())
            if route == 'dialogs' and not mutate:
                return {'dialogs': hoster.dialogs(runner)}
            match = re.fullmatch(r'dialogs/([^/]+)(/cancel)?', route)
            if match:
                dialog_id, cancel = match.groups()
                if mutate:
                    data = self.body()
                    if cancel:
                        if data:
                            raise ValueError('Cancellation takes an empty JSON object')
                        return hoster.cancel(runner, dialog_id)
                    if not {'text', 'request_id'} <= set(data) or set(data) - {
                            'text', 'scope', 'message_id', 'request_id'}:
                        raise ValueError('Expected text, request_id and optional scope/message_id')
                    return hoster.chat(runner, dialog_id, **data)
                if not cancel:
                    return hoster.dialog(runner, dialog_id)
            raise ContractError('not_found', 'Dashboard route not found')

        def handle_request(self, mutate=False):
            if not self.authorized(mutate):
                return
            try:
                if not mutate and self.path in _TYPES:
                    name, mime = _TYPES[self.path]
                    self.send(200, (_ASSETS / name).read_bytes(), mime)
                    return
                if not mutate and self.path == '/api/projects':
                    result = _projects(catalog, enable_actions)
                elif not mutate and self.path == '/api/usage':
                    result = usage.value
                elif match := _WORKSPACE.fullmatch(self.path):
                    project, workspace, route = match.groups()
                    runner = Runner(catalog.context(project, workspace, allow_disabled=not mutate))
                    result = self.workspace(runner, route, mutate)
                else:
                    raise ContractError('not_found', 'Dashboard route not found')
                self.send(200, result)
            except ContractError as exc:
                status = {'not_found': 404, 'dialog_not_found': 404, 'actions_disabled': 403,
                          'stale_revision': 409, 'stale_scope': 409, 'identity_mismatch': 409}.get(exc.code, 400)
                self.send(status, {'error': exc.as_dict()})
            except _ERRORS as exc:
                message = 'Local command timed out' if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
                self.send(400, {'error': ContractError('invalid_request', message).as_dict()})

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(mutate=True)

    class Server(ThreadingHTTPServer):
        daemon_threads = False

        def server_close(self):
            try:
                if usage.thread.is_alive():
                    usage.close()
                super().server_close()
                hoster.close()
            finally:
                if self.session_lock:
                    self.session_lock.__exit__(None, None, None)
                    self.session_lock = None

    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_lock = lock(session_dir / 'dashboard.lock', blocking=False)
    session_lock.__enter__()
    try:
        server = Server(('127.0.0.1', port), Handler)
    except BaseException:
        session_lock.__exit__(None, None, None)
        raise
    server.session_lock = session_lock
    try:
        hoster.start()
        usage.thread.start()
    except BaseException:
        server.server_close()
        raise
    return server, token


def serve(catalog, port=0, *, open_browser=True, session_dir=None, enable_actions=False):
    server, token = make_server(catalog, port, session_dir=session_dir, enable_actions=enable_actions)
    url = f'http://127.0.0.1:{server.server_port}/#token={token}'
    print(url, flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if open_browser:
            worktree = os.environ.get('ORCA_WORKTREE_ID')
            if worktree:
                try:
                    orca.call('tab', 'create', '--worktree', 'id:' + worktree, '--url', url, '--environment', 'local', timeout=15)
                except _ERRORS:
                    print('Orca browser unavailable. Open the local URL manually.', flush=True)
            else:
                webbrowser.open(url)
        while thread.is_alive():
            thread.join(.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
