"""Loopback-only control panel. Reads persisted state; never consumes Orca mail."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import re
import threading
import subprocess
from .engine import Runner
from .catalog import Catalog
from .application import goals, config_path, state_view, tail, submit_command, CONTROL_ACTIONS, summary, operation, ContractError, submit_v1, configuration, configuration_capabilities, execute
from . import __version__
from pathlib import Path

from . import runner_config as config
from .runner_store import atomic_json, lock, read_json, running

ASSETS = Path(__file__).with_name('runner-ui')


def make_server(catalog, port=0, *, project_id=None, instance=None, services=None):
    from .project_services import ProjectServices, ServiceUnavailable, identity, workspace_summary
    token = instance['token'] if instance else secrets.token_urlsafe(32)
    services = services or ProjectServices(catalog)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Never log token URLs or request bodies.

        def send(self, status, value, content_type='application/json'):
            body = json.dumps(value, ensure_ascii=False).encode() if content_type == 'application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                # The browser disconnected; a second error response cannot reach it.
                self.close_connection = True

        def authorized(self, mutate=False):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                self.send(403, {'error': 'invalid host'})
                return False
            if self.path.startswith('/api/'):
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    self.send(403, {'error': 'invalid session token'})
                    return False
                if mutate and self.headers.get('Origin') != 'http://' + host:
                    self.send(403, {'error': 'invalid origin'})
                    return False
            return True

        def scope(self):
            if project_id is None:
                return
            if self.path in ('/api/health', '/api/shutdown', '/api/summary'):
                return
            match = re.fullmatch(r'/api/projects/([^/]+)/workspaces/([^/]+)/([^/]+(?:/[^/]+)?)', self.path)
            if not match or match[1] != project_id or match[3] in ('workspace', 'disable'):
                raise ValueError('project service scope violation')

        def proxy(self, data=None):
            match = re.fullmatch(r'/api/projects/([^/]+)/workspaces/([^/]+)/([^/]+(?:/[^/]+)?)', self.path)
            if project_id is not None or not match or match[3] in ('workspace', 'disable'):
                return False
            # Validate both IDs locally; then the project service repeats the check.
            catalog.context(match[1], match[2], allow_disabled=True)
            if self.command == 'POST' and match[3] in ('config/capabilities', 'config', 'profile', 'execute', 'commands'):
                return False  # Settings and cold execute use the shared local application.
            if self.command == 'GET' and (match[3] in ('summary', 'goals', 'config', 'profile') or match[3].startswith('operations/')):
                return False  # Persisted reads never need a running service.
            try:
                code, value = services.forward(match[1], self.path, data)
            except ServiceUnavailable:
                if self.command != 'GET' or match[3] != 'state':
                    raise
                value = state_view(Runner(catalog.context(match[1], match[2], allow_disabled=True)))
                value['confirmed'] = False
                value['service'] = services.status(match[1])
                self.send(200, value)
                return True
            if self.command == 'GET' and match[3] == 'state' and code == 200:
                value['confirmed'] = True
            self.send(code, value)
            return True

        def selected(self):
            match = re.fullmatch(r'/api/projects/([^/]+)/workspaces/([^/]+)/([^/]+(?:/[^/]+)?)', self.path)
            if not match:
                raise ValueError('workspace-scoped URL required')
            runner = Runner(catalog.context(match[1], match[2], allow_disabled=True))
            if self.command == 'POST' and catalog.read()['projects'][match[1]]['workspaces'][match[2]]['disabled']:
                raise ValueError('workspace disabled')
            return runner, '/api/' + match[3]

        def do_GET(self):
            if not self.authorized():
                return
            try:
                self.scope()
                if project_id is not None and self.path == '/api/health':
                    self.send(200, {'identity': identity(instance), 'version': __version__,
                                    'state': 'stopping' if self.server.stopping else 'running'})
                    return
                if self.server.stopping:
                    self.send(503, {'error': 'service stopping'})
                    return
                if project_id is not None and self.path == '/api/summary':
                    self.send(200, workspace_summary(catalog, project_id))
                    return
                if project_id is None and self.path == '/api/services':
                    self.send(200, services.all())
                    return
                match = re.fullmatch(r'/api/projects/([^/]+)/service', self.path)
                if project_id is None and match:
                    self.send(200, services.summary(match[1]))
                    return
                if self.proxy():
                    return
                if self.path == '/api/projects':
                    self.send(200, catalog.read())
                    return
                match = re.fullmatch(r'/api/projects/([^/]+)(/workspaces)?', self.path)
                if match:
                    project = catalog.read()['projects'][match[1]]
                    self.send(200, project['workspaces'] if match[2] else project)
                    return
                if self.path.startswith('/api/') and not self.path.startswith('/api/projects/'):
                    self.send(404, {'error': 'workspace-scoped URL required'})
                    return
                if self.path.startswith('/api/projects/'):
                    runner, path = self.selected()
                else:
                    path = self.path
                if path == '/api/state':
                    self.send(200, state_view(runner))
                elif path == '/api/summary':
                    self.send(200, summary(runner))
                elif path == '/api/goals':
                    self.send(200, {'schema_version': 1, 'identity': runner.context.identity(), 'goals': goals(runner)})
                elif path.startswith('/api/operations/'):
                    self.send(200, operation(runner, path.removeprefix('/api/operations/')))
                elif path == '/api/recovery':
                    from .recovery import recovery_view
                    self.send(200, recovery_view(runner))
                elif path == '/api/profile':
                    self.send(200, {'profile': runner.context.profile, 'identity': runner.context.identity()})
                elif path == '/api/config':
                    self.send(200, configuration(runner))
                elif self.path == '/favicon.ico':
                    self.send(204, b'', 'image/x-icon')
                elif self.path in ('/', '/app.js', '/style.css'):
                    name = 'index.html' if self.path == '/' else self.path[1:]
                    mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8',
                            'style.css': 'text/css; charset=utf-8'}[name]
                    self.send(200, (ASSETS / name).read_bytes(), mime)
                else:
                    self.send(404, {'error': 'not found'})
            except ContractError as exc:
                self.send(400, {'schema_version': 1, 'error': exc.as_dict()})
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                if '/workspaces/' in self.path and re.search(r'/(summary|goals|commands|execute|operations/[^/]+)$', self.path):
                    self.send(400, {'schema_version': 1, 'error': ContractError('invalid_request', str(exc)).as_dict()})
                else:
                    self.send(400, {'error': str(exc)})

        def do_POST(self):
            if not self.authorized(mutate=True):
                return
            try:
                self.scope()
                if self.server.stopping:
                    self.send(503, {'error': 'service stopping'})
                    return
                if self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('JSON required')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('invalid request size')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('object required')
                if project_id is not None and self.path == '/api/shutdown':
                    if data.get('identity') != identity(instance):
                        raise ValueError('service identity mismatch')
                    self.server.stopping = True
                    directory = services.directory(project_id)
                    with lock(directory / 'state.lock'):
                        instance.update(state='stopping')
                        atomic_json(directory / 'instance.json', instance)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    self.send(200, {'identity': identity(instance), 'state': 'stopping'})
                    return
                match = re.fullmatch(r'/api/projects/([^/]+)/service/(start|stop|restart|reconcile)', self.path)
                if project_id is None and match:
                    if set(data) != {'request_id'}:
                        raise ValueError('request_id required; no other service arguments allowed')
                    self.send(200, services.action(match[1], match[2], data['request_id']))
                    return
                if self.proxy(data):
                    return
                if self.path == '/api/projects':
                    self.send(200, catalog.project(name=data.get('name'), values=data))
                    return
                match = re.fullmatch(r'/api/projects/([^/]+)(/workspaces)?', self.path)
                if match:
                    result = catalog.workspace(match[1], data) if match[2] else catalog.project(project_id=match[1], values=data)
                    self.send(200, result)
                    return
                runner, path = self.selected()
                if path == '/api/recovery':
                    import uuid
                    identifier = data.pop('request_id', None) or str(uuid.uuid4())
                    record = submit_command(runner, 'recovery', data, identifier)
                    if record['status'] != 'done':
                        raise ValueError(record.get('error') or 'recovery operation not completed')
                    self.send(200, record['result'])
                    return
                if path in ('/api/profile', '/api/workspace', '/api/disable'):
                    ctx = runner.context
                    if path == '/api/disable': result = catalog.disable(ctx.project_id, ctx.workspace_id)
                    else: result = catalog.workspace(ctx.project_id, {'profile': data} if path == '/api/profile' else data, ctx.workspace_id)
                    self.send(200, result)
                    return
                if path == '/api/config/capabilities':
                    self.send(200, configuration_capabilities(runner, data))
                    return
                if path == '/api/config':
                    import uuid
                    identifier = data.pop('request_id', None) or str(uuid.uuid4())
                    record = submit_command(runner, 'configure', data, identifier)
                    if record['status'] != 'done':
                        raise ValueError(record.get('error') or 'configuration operation not completed')
                    self.send(200, record['result'])
                    return
                if path == '/api/execute':
                    self.send(200, execute(runner, data))
                    return
                if path == '/api/commands':
                    self.send(200, submit_v1(runner, data))
                    return
                action = path.removeprefix('/api/')
                if action in CONTROL_ACTIONS or action in ('start', 'focus'):
                    request_id = data.pop('request_id', None)
                    result = submit_command(runner, action, data, request_id)
                else:
                    self.send(404, {'error': 'unknown operation'})
                    return
                self.send(200, result)
            except ContractError as exc:
                self.send(400, {'schema_version': 1, 'error': exc.as_dict()})
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
                if '/workspaces/' in self.path and re.search(r'/(summary|goals|commands|execute|operations/[^/]+)$', self.path):
                    self.send(400, {'schema_version': 1, 'error': ContractError('invalid_request', str(exc)).as_dict()})
                else:
                    self.send(400, {'error': str(exc)})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = project_id is None
    server.stopping = False
    return server, token


def serve(catalog, port=0, open_browser=True):
    with lock(catalog.state_root / 'ui.lock', blocking=False):
        from .project_services import ProjectServices
        services = ProjectServices(catalog)
        services.all()  # Authenticated discovery only; never starts/replays a service or runner.
        server, token = make_server(catalog, port, services=services)
        url = f'http://127.0.0.1:{server.server_port}/#token={token}'
        print(url, flush=True)
        # Orca waits for navigation: HTTP must already respond before tab creation.
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True)
        thread.start()
        try:
            if open_browser:
                try:
                    __import__('team_runner.orca', fromlist=['call']).call('tab', 'create', '--url', url)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    # Timeout arguments can contain the session token; do not print them.
                    detail = 'browser open timed out' if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
                    print(f'Orca browser unavailable: {detail}. Open the local URL manually.', flush=True)
            while thread.is_alive():
                thread.join(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
