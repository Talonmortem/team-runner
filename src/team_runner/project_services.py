"""Explicit detached project services. Durable intents; never replay unknown effects."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import errno
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid

from .catalog import identifier
from . import service_intent
from .runner_entry import process_stamp, boot_id
from .runner_store import atomic_json, enqueue, lock, read_json


class ServiceUnavailable(RuntimeError):
    pass


def held(path):
    try:
        with lock(path, blocking=False):
            return False
    except RuntimeError:
        return True


def departed(record):
    """Positive evidence that the recorded incarnation cannot still execute."""
    pid, stamp = record.get('pid'), record.get('process_stamp')
    if not pid or not stamp:
        return False
    current_boot = boot_id()
    if record.get('boot_id') and current_boot and record['boot_id'] != current_boot:
        return True
    observed = process_stamp(pid)
    if observed is not None:
        if observed != stamp:
            return True
        try:
            return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False


def identity(record):
    return {key: record.get(key) for key in ('project_id', 'nonce', 'pid', 'process_stamp')}


def request(record, path, data=None, timeout=2):
    """One request, no redirects, proxies or retry (including POST)."""
    conn = http.client.HTTPConnection('127.0.0.1', record['port'], timeout=timeout)
    try:
        conn.request('GET' if data is None else 'POST', path,
                     body=None if data is None else json.dumps(data).encode(),
                     headers={'Authorization': 'Bearer ' + record['token'],
                              'Origin': f"http://127.0.0.1:{record['port']}",
                              'Content-Type': 'application/json'})
        response = conn.getresponse()
        result = json.loads(response.read())
        return response.status, result
    except (OSError, ValueError, http.client.HTTPException):
        # Exception text may contain credentials or arbitrary server content.
        raise ServiceUnavailable('project service unavailable; outcome unconfirmed') from None
    finally:
        conn.close()


class ProjectServices:
    def __init__(self, catalog, timeout=2, wait_timeout=5):
        self.catalog, self.timeout, self.wait_timeout = catalog, timeout, wait_timeout
        self.children = []

    def directory(self, project_id):
        if identifier(project_id) not in self.catalog.read()['projects']:
            raise ValueError('unknown project')
        path = self.catalog.state_root / 'projects' / project_id / 'service'
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    def record(self, project_id):
        return read_json(self.directory(project_id) / 'instance.json', {})

    def observe(self, project_id):
        directory = self.directory(project_id)
        with lock(directory / 'state.lock'):
            record = read_json(directory / 'instance.json', {})
            confirmed = False
            state = record.get('state', 'stopped')
            error = record.get('last_error')
            if record.get('port') and record.get('token'):
                try:
                    code, health = request(record, '/api/health', timeout=self.timeout)
                    if code == 200 and health.get('identity') == identity(record):
                        state, confirmed, error = health['state'], True, None
                except ServiceUnavailable:
                    pass
            if not confirmed:
                locked = held(directory / 'instance.lock')
                if not locked and (not record or state in ('stopped', 'error') or departed(record)):
                    state = 'error' if state == 'error' else 'stopped'
                    confirmed = True
                elif state not in ('starting', 'stopping'):
                    state = 'unknown'
                if not confirmed:
                    error = error or 'service identity unavailable; reconcile required'
            return {**record, 'project_id': project_id, 'state': state, 'confirmed': confirmed,
                    'last_error': error, 'configured_port': self.catalog.read()['projects'][project_id]['service_port']}

    def status(self, project_id):
        value = self.observe(project_id)
        return {key: value.get(key) for key in ('project_id', 'nonce', 'pid', 'process_stamp', 'port',
                                               'configured_port', 'state', 'confirmed', 'last_error')}

    def forward(self, project_id, path, data=None):
        record = self.observe(project_id)
        if not record['confirmed'] or record['state'] != 'running':
            raise ServiceUnavailable('project service is not running; explicit start or reconcile required')
        return request(record, path, data, timeout=self.timeout)

    def summary(self, project_id):
        result = self.status(project_id)
        try:
            if result['state'] != 'running' or not result['confirmed']:
                raise ServiceUnavailable()
            code, summary = self.forward(project_id, '/api/summary')
            if code != 200:
                raise ServiceUnavailable()
            result['workspaces'] = summary
        except ServiceUnavailable:
            result['workspaces'] = workspace_summary(self.catalog, project_id, confirmed=False)
        return result

    def all(self):
        ids = list(self.catalog.read()['projects'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(self.summary, ids))

    def spawn(self, project_id, nonce):
        # Explicit roots also support embedded/test catalogs; no inherited workspace.
        env = {k: v for k, v in os.environ.items() if not k.startswith('TEAM_RUNNER_')}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get('PYTHONPATH', '')
        child = subprocess.Popen([sys.executable, '-m', 'team_runner', '_service', '--project', project_id,
                                  '--nonce', nonce, '--config-root', str(self.catalog.config_root),
                                  '--state-root', str(self.catalog.state_root)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True, env=env, cwd='/')
        self.children = [p for p in self.children if p.poll() is None]
        self.children.append(child)
        threading.Thread(target=child.wait, daemon=True).start()
        return child

    @contextmanager
    def _authorization_fence(self, project_id, expected):
        if expected is None:
            yield
            return
        from .goal_intent import permitted
        from .protocol import ContractError
        identity = expected['identity']
        if identity.get('project_id') != project_id:
            raise ContractError('identity_mismatch', 'Service authorization belongs to another project')
        ctx = self.catalog.context(project_id, identity['workspace_id'])
        if ctx.identity() != identity:
            raise ContractError('identity_mismatch', 'Service authorization identity changed')
        runner = SimpleNamespace(context=ctx, state_dir=ctx.state_dir)
        with lock(ctx.state_dir / 'goal-intent.lock'):
            if not permitted(runner, expected['generation'], expected['epoch']):
                raise ContractError('authorization_cancelled', 'Goal authorization changed before service spawn')
            yield

    @contextmanager
    def _service_fence(self, project_id, epoch):
        from .protocol import ContractError
        with lock(self.directory(project_id) / 'intent.lock'):
            if not service_intent.permitted(self.catalog, project_id, epoch):
                raise ContractError('authorization_cancelled', 'Service authorization changed before spawn')
            yield

    def _start(self, project_id, operation, save, expected_authorization=None, expected_service_epoch=None):
        before = self.record(project_id)
        status = self.observe(project_id)
        if status['confirmed'] and status['state'] == 'running':
            with self._authorization_fence(project_id, expected_authorization), self._service_fence(project_id, expected_service_epoch):
                operation.update(phase='adopted', nonce=status['nonce'])
                save()
            return self.status(project_id)
        if not status['confirmed'] or status['state'] not in ('stopped', 'error'):
            raise ServiceUnavailable('previous service outcome unconfirmed; reconcile required')
        directory = self.directory(project_id)
        nonce = str(uuid.uuid4())
        operation.update(phase='starting', nonce=nonce)
        save()
        with self._authorization_fence(project_id, expected_authorization), self._service_fence(project_id, expected_service_epoch), lock(directory / 'state.lock'):
            current = read_json(directory / 'instance.json', {})
            if current != before:
                raise ServiceUnavailable('service incarnation changed before spawn; reconcile required')
            record = {'project_id': project_id, 'nonce': nonce, 'state': 'starting', 'pid': None,
                      'process_stamp': None, 'port': None, 'token': secrets.token_urlsafe(32), 'last_error': None}
            atomic_json(directory / 'instance.json', record)
            # If the controller dies after spawn, child can still publish. No retry on timeout.
            try:
                child = self.spawn(project_id, nonce)
            except OSError:
                record.update(state='error', no_effect=True, last_error='could not create service process')
                atomic_json(directory / 'instance.json', record)
                operation['no_effect'] = True
                save()
                return {**record, 'confirmed': True}
            record.update(pid=child.pid, process_stamp=process_stamp(child.pid))
            atomic_json(directory / 'instance.json', record)
        result = self._wait(project_id, {'running', 'error', 'stopped'})
        if result['confirmed'] and result['state'] == 'stopped':
            with lock(directory / 'state.lock'):
                record = read_json(directory / 'instance.json')
                record.update(state='error', last_error='service process exited before readiness was confirmed')
                atomic_json(directory / 'instance.json', record)
            result.update(state=record['state'], last_error=record['last_error'])
        return result

    def _wait(self, project_id, states):
        deadline = time.monotonic() + self.wait_timeout
        while True:
            result = self.status(project_id)
            if result['confirmed'] and result['state'] in states:
                return result
            if time.monotonic() >= deadline:
                result.update(state='unknown', confirmed=False, last_error='service transition unconfirmed; reconcile required')
                directory = self.directory(project_id)
                with lock(directory / 'state.lock'):
                    record = read_json(directory / 'instance.json')
                    # Preserve a late ready/stopped publication, but never repeat its effect.
                    if record['state'] in ('starting', 'stopping'):
                        record.update(state='unknown', last_error=result['last_error'])
                        atomic_json(directory / 'instance.json', record)
                return result
            time.sleep(.05)

    def _stop(self, project_id, operation, save):
        record = self.observe(project_id)
        if record['confirmed'] and record['state'] in ('stopped', 'error'):
            return self.status(project_id)
        if not record['confirmed'] or record['state'] != 'running':
            raise ServiceUnavailable('cannot authenticate service; reconcile required; no process was killed')
        operation.update(phase='stopping', nonce=record['nonce'])
        save()
        code, result = request(record, '/api/shutdown', {'identity': identity(record)}, timeout=self.timeout)
        if code != 200 or result.get('identity') != identity(record):
            raise ServiceUnavailable('shutdown outcome unconfirmed; reconcile required')
        return self._wait(project_id, {'stopped', 'error'})

    def action(self, project_id, action, request_id, *, expected_authorization=None, expected_service_epoch=None):
        from .protocol import ContractError
        if action not in ('start', 'stop', 'restart', 'reconcile'):
            raise ValueError('unknown service action')
        identifier(request_id)
        directory = self.directory(project_id)
        previous = read_json(directory / 'commands' / (request_id + '.json'))
        if previous:
            if previous['action'] != action:
                raise ContractError('request_conflict', 'Service request ID already used')
            if expected_service_epoch is not None and previous.get('data', {}).get('service_epoch') != expected_service_epoch:
                raise ContractError('request_conflict', 'Service request scope changed')
            if previous['status'] != 'pending':
                return previous
            if 'service_epoch' not in previous.get('data', {}) and action != 'reconcile':
                raise ContractError('service_resume_required', 'Legacy service request has no saved permission')
        if expected_service_epoch is None and action != 'reconcile':
            receipt = (service_intent.cancel(self.catalog, project_id, request_id) if action == 'stop'
                       else service_intent.authorize(self.catalog, project_id, request_id, action=action))
            expected_service_epoch = receipt['epoch']
        data = {'service_epoch': expected_service_epoch} if action != 'reconcile' else {}
        with lock(directory / 'operations.lock'):
            operation = enqueue(directory, action, data, request_id=request_id)
            path = directory / 'commands' / (request_id + '.json')
            if operation['status'] != 'pending':
                return operation
            def save():
                if operation.get('phase') and (not operation.get('stages') or operation['stages'][-1]['phase'] != operation['phase']):
                    operation.setdefault('stages', []).append({'phase': operation['phase'], 'nonce': operation.get('nonce')})
                atomic_json(path, operation)
            operation['status'] = 'applying'
            save()
            try:
                if action in ('stop', 'restart'):
                    intent = service_intent.read(self.catalog, project_id)
                    if intent.get('epoch') != expected_service_epoch:
                        raise ContractError('authorization_cancelled', 'Service permission changed before shutdown')
                if action == 'reconcile':
                    result = self.status(project_id)
                elif action == 'start':
                    result = self._start(project_id, operation, save, expected_authorization, expected_service_epoch)
                else:
                    result = self._stop(project_id, operation, save)
                    if action == 'restart' and result['confirmed'] and result['state'] in ('stopped', 'error'):
                        operation['phase'] = 'stopped'
                        save()
                        result = self._start(project_id, operation, save, expected_authorization, expected_service_epoch)
                operation.update(result=result, status='done' if result['confirmed'] else 'unknown')
                if result['state'] == 'error':
                    operation.update(status='error', error=result['last_error'])
            except ContractError as exc:
                operation.update(status='error', error=str(exc), error_code=exc.code)
            except (ServiceUnavailable, OSError, ValueError) as exc:
                operation.update(status='unknown', error=str(exc))
            save()
            return operation


def workspace_summary(catalog, project_id, confirmed=True):
    from .engine import Runner
    from .runner_ui import state_view
    result = []
    for workspace_id, workspace in catalog.read()['projects'][project_id]['workspaces'].items():
        st = read_json(catalog.state_dir(project_id, workspace_id) / 'state.json', {})
        cur = st.get('current') or {}
        row = {'workspace_id': workspace_id, 'name': workspace['name'], 'confirmed': confirmed,
               'goal': st.get('goal'), 'item': cur.get('item'), 'error': st.get('last_error'),
               'waiting': not st.get('stop_requested', False) and not cur.get('failed', False) and
                          (bool(cur.get('gate_id') and not cur.get('gate_resolved')) or
                           any(m.get('type') == 'question' and not m.get('answered') and not m.get('closed') for m in cur.get('messages', [])))}
        try:
            view = state_view(Runner(catalog.context(project_id, workspace_id, allow_disabled=True)))
            row.update(view['health'])
            row.update(completed=sum(q.get('status') == 'completed' for q in view['queue']), total=len(view['queue']))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row.update(state='error', error=str(exc), completed=None, total=None)
        result.append(row)
    return result


def run_service(catalog, project_id, nonce):
    from .runner_ui import make_server
    manager = ProjectServices(catalog)
    identifier(nonce)
    directory = manager.directory(project_id)
    with lock(directory / 'instance.lock', blocking=False):
        with lock(directory / 'state.lock'):
            record = read_json(directory / 'instance.json', {})
            if record.get('nonce') != nonce or record.get('project_id') != project_id:
                raise ValueError('service launch identity/nonce mismatch')
            fd = os.open(directory / (nonce + '.claim'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            record.update(pid=os.getpid(), process_stamp=process_stamp(os.getpid()))
            atomic_json(directory / 'instance.json', record)
        server = None
        supervisor = None
        try:
            port = catalog.read()['projects'][project_id]['service_port']
            server, _ = make_server(catalog, port, project_id=project_id, instance=record)
            with lock(directory / 'state.lock'):
                record.update(port=server.server_port, state='running', last_error=None)
                atomic_json(directory / 'instance.json', record)
            from .recovery import Supervisor
            supervisor = Supervisor(catalog, project_id)
            supervisor.start()
            server.serve_forever(poll_interval=.05)
        except Exception as exc:
            with lock(directory / 'state.lock'):
                message = (f'configured service port {port} is already in use' if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE
                           else 'project service failed to bind or serve')
                record.update(state='error', last_error=message)
                atomic_json(directory / 'instance.json', record)
            raise
        finally:
            if supervisor is not None:
                supervisor.stop()
            if server is not None:
                server.server_close()  # Non-daemon request threads drain before releasing lifetime lock.
                with lock(directory / 'state.lock'):
                    if record['state'] != 'error':
                        record.update(state='stopped', last_error=None)
                        atomic_json(directory / 'instance.json', record)
