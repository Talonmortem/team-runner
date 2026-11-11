"""Explicit detached manager; observes durable service authorization, never goals."""
import argparse
import fcntl
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .catalog import Catalog, identifier
from .project_services import ProjectServices, departed
from .runner_entry import boot_id, process_stamp
from .runner_store import atomic_json, lock, read_json


def _held(path):
    """Read-only lock observation: a filename alone proves nothing."""
    try:
        with Path(path).open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def _valid_watchdog(value, identity):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 1 or value.get('identity') != identity:
        return False
    if (type(value.get('epoch')) is not int or value['epoch'] < 0
            or type(value.get('attempts')) is not int or not 0 <= value['attempts'] <= 3
            or type(value.get('checks')) is not int or not 0 <= value['checks'] <= 21
            or value.get('phase') not in ('observing', 'starting', 'suppressed', 'needs_action')):
        return False
    deadline = value.get('next_check_at')
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline < 0:
        return False
    try:
        identifier(value.get('incident'))
        if 'operation_id' in value:
            identifier(value['operation_id'])
    except ValueError:
        return False
    return True


class Manager:
    def __init__(self, catalog, services=None, clock=time.time, spawn=None, wait_timeout=5):
        self.catalog = catalog
        self.services = services if services is not None else ProjectServices(catalog)
        self.clock, self.wait_timeout = clock, wait_timeout
        self.directory = catalog.state_root / 'manager'
        self.spawn = spawn if spawn is not None else self._spawn
        self.children = []

    @staticmethod
    def _guard():
        if os.environ.get('TEAM_RUNNER_ATTEMPT'):
            raise ValueError('child attempts cannot manage services')

    def _roots(self):
        return {'config_root': str(self.catalog.config_root.resolve()),
                'state_root': str(self.catalog.state_root.resolve())}

    def status(self):
        try:
            record = read_json(self.directory / 'instance.json', {})
        except (ValueError, OSError):
            return {'state': 'unknown', 'confirmed': False, 'blocker': 'invalid manager instance'}
        if not isinstance(record, dict):
            return {'state': 'unknown', 'confirmed': False, 'blocker': 'invalid manager instance'}
        if not record:
            return {'state': 'stopped', 'confirmed': not _held(self.directory / 'instance.lock')}
        if record.get('roots') != self._roots():
            return {**record, 'state': 'unknown', 'confirmed': False, 'blocker': 'manager roots mismatch'}
        if record.get('schema_version') != 1:
            return {**record, 'state': 'unknown', 'confirmed': False, 'blocker': 'unsupported manager schema'}
        nonce = record.get('nonce')
        try:
            identifier(nonce)
        except ValueError:
            return {**record, 'state': 'unknown', 'confirmed': False, 'blocker': 'invalid manager nonce'}
        try:
            claim = read_json(self.directory / (nonce + '.claim'), {})
        except (ValueError, OSError):
            claim = {}  # Exclusive claim may exist before its atomic identity publication.
        held = _held(self.directory / 'instance.lock')
        matching = (record.get('pid') and record.get('process_stamp') and
                    record.get('boot_id') == boot_id() and
                    process_stamp(record['pid']) == record['process_stamp'] and
                    all(claim.get(key) == record.get(key) for key in ('nonce', 'pid', 'process_stamp', 'boot_id', 'roots')))
        if held and matching and record.get('state') in ('running', 'stopping'):
            stopping = read_json(self.directory / 'stop.json', {}).get('nonce') == nonce
            return {**record, 'state': 'stopping' if stopping else record['state'], 'confirmed': True}
        if not held and (departed(record) or record.get('no_effect') is True):
            return {**record, 'state': 'stopped', 'confirmed': True}
        return {**record, 'state': 'unknown', 'confirmed': False,
                'blocker': 'manager owner unconfirmed; retain nonce, do not replay spawn'}

    def _spawn(self, nonce):
        env = {key: value for key, value in os.environ.items() if not key.startswith('TEAM_RUNNER_')}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get('PYTHONPATH', '')
        child = subprocess.Popen([sys.executable, '-m', 'team_runner.service_manager', '--nonce', nonce,
                                  '--config-root', str(self.catalog.config_root),
                                  '--state-root', str(self.catalog.state_root)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 cwd='/', start_new_session=True, close_fds=True, env=env)
        self.children = [p for p in self.children if p.poll() is None]
        self.children.append(child)
        threading.Thread(target=child.wait, daemon=True).start()
        return child

    def ensure(self, request_id):
        self._guard()
        identifier(request_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_path = self.directory / 'commands' / (request_id + '.json')
        with lock(self.directory / 'launch.lock'):
            previous = read_json(receipt_path, {})
            if previous and previous.get('action') != 'start':
                raise ValueError('manager request_id conflict')
            observed = self.status()
            if previous:
                if observed.get('nonce') != previous.get('nonce'):
                    return {**previous, 'state': 'superseded', 'confirmed': False}
                return observed
            if observed['state'] == 'running' and observed['confirmed']:
                atomic_json(receipt_path, {'action': 'start', 'request_id': request_id, 'nonce': observed['nonce']})
                return observed
            if not observed['confirmed'] or observed['state'] != 'stopped':
                atomic_json(receipt_path, {'action': 'start', 'request_id': request_id,
                                          'nonce': observed.get('nonce')})
                return observed
            nonce = str(uuid.uuid4())
            record = {'schema_version': 1, 'nonce': nonce, 'roots': self._roots(), 'state': 'starting', 'pid': None,
                      'process_stamp': None, 'boot_id': boot_id(), 'request_id': request_id}
            with lock(self.directory / 'state.lock'):
                atomic_json(self.directory / 'instance.json', record)
                atomic_json(receipt_path, {'action': 'start', 'request_id': request_id, 'nonce': nonce})
                try:
                    child = self.spawn(nonce)
                except OSError:
                    record.update(state='error', no_effect=True, blocker='manager process creation failed')
                    atomic_json(self.directory / 'instance.json', record)
                    return self.status()
                record.update(pid=child.pid, process_stamp=process_stamp(child.pid))
                atomic_json(self.directory / 'instance.json', record)
        deadline = time.monotonic() + self.wait_timeout
        while True:
            observed = self.status()
            if observed.get('nonce') != nonce or observed['confirmed'] or time.monotonic() >= deadline:
                return observed
            time.sleep(.02)

    def stop(self, request_id):
        self._guard()
        identifier(request_id)
        receipt_path = self.directory / 'commands' / (request_id + '.json')
        with lock(self.directory / 'state.lock'):
            previous = read_json(receipt_path, {})
            if previous and previous.get('action') != 'stop':
                raise ValueError('manager request_id conflict')
            observed = self.status()
            if previous:
                return {**previous, 'state': observed['state'] if observed.get('nonce') == previous.get('nonce') else 'superseded'}
            receipt = {'action': 'stop', 'request_id': request_id, 'nonce': observed.get('nonce')}
            atomic_json(receipt_path, receipt)
            if observed.get('nonce'):
                atomic_json(self.directory / 'stop.json', receipt)
            return {**observed, 'stop_requested': bool(observed.get('nonce'))}

    def tick(self, project_id):
        """One bounded persisted observation; unknown service effects are never replayed."""
        self._guard()
        from . import service_intent
        directory = self.catalog.state_root / 'projects' / identifier(project_id) / 'service'
        path = directory / 'watchdog.json'
        with lock(directory / 'watchdog.lock', blocking=False):
            identity = {'project_id': project_id, **self._roots()}
            try:
                previous = read_json(path, {})
            except (ValueError, OSError):
                previous = None
            if (path.exists() and not _valid_watchdog(previous, identity)):
                value = {'schema_version': 1, 'identity': identity, 'epoch': 0,
                         'incident': str(uuid.uuid4()), 'attempts': 0, 'checks': 0,
                         'next_check_at': 0, 'phase': 'needs_action', 'blocker': 'invalid_watchdog_journal'}
                atomic_json(path, value)
                return value
            if previous.get('blocker') == 'invalid_watchdog_journal':
                return previous
            try:
                intent = service_intent.read(self.catalog, project_id)
            except (ValueError, OSError):
                value = {**(previous or {'schema_version': 1, 'identity': identity, 'epoch': 0,
                          'incident': str(uuid.uuid4()), 'attempts': 0, 'checks': 0, 'next_check_at': 0}),
                         'phase': 'needs_action', 'blocker': 'invalid service authorization'}
                atomic_json(path, value)
                return value
            if not intent or intent.get('desired_state') != 'running':
                value = {**previous, 'phase': 'suppressed'}
                if previous:
                    atomic_json(path, value)
                return value
            epoch = intent['epoch']
            value = previous if previous.get('epoch') == epoch else {
                'schema_version': 1, 'identity': identity, 'epoch': epoch, 'incident': str(uuid.uuid4()),
                'attempts': 0, 'checks': 0, 'next_check_at': 0, 'phase': 'observing'}
            if value.get('phase') == 'needs_action' or self.clock() < value.get('next_check_at', 0):
                return value
            value['checks'] += 1
            policy = intent.get('policy', {})
            max_attempts = policy.get('max_attempts', 3)
            max_checks = policy.get('max_checks', 20)
            backoff = policy.get('backoff_seconds', 1)
            def save(phase, blocker=None):
                value.update(phase=phase, blocker=blocker,
                             next_check_at=self.clock() + min(60, backoff * 2 ** min(value['attempts'], 6)))
                atomic_json(path, value)
                return value
            try:
                observed = self.services.observe(project_id)
            except (ValueError, OSError, RuntimeError):
                return save('needs_action' if value['checks'] > max_checks else 'observing',
                            'service observation unavailable')
            operation_id = value.get('operation_id')
            operation = read_json(directory / 'commands' / (operation_id + '.json'), {}) if operation_id else {}
            if operation_id and observed.get('state') == 'running' and operation.get('nonce') != observed.get('nonce'):
                return save('needs_action', 'service incarnation does not match recovery operation')
            if observed.get('confirmed') and observed.get('state') == 'running':
                value['observed_incarnation'] = observed.get('nonce')
                # A healthy service ends the incident; its later death is a new incident.
                value.update(attempts=0, checks=0, incident=str(uuid.uuid4()))
                value.pop('operation_id', None)
                return save('observing')
            if value['checks'] > max_checks:
                return save('needs_action', 'service observation budget exhausted')
            record = self.services.record(project_id)
            if operation_id:
                proven_no_effect = (operation.get('nonce') == record.get('nonce')
                                    and record.get('no_effect') is True and not record.get('pid')
                                    and record.get('state') == 'error')
                if operation.get('status') not in ('done', 'error') and not proven_no_effect:
                    return save('observing', 'service operation outcome unconfirmed; no replay')
                value.pop('operation_id')
            death = (not record or departed(record) or
                     (record.get('state') == 'error' and not record.get('pid')
                      and record.get('no_effect') is True))
            if not observed.get('confirmed') or observed.get('state') not in ('stopped', 'error') or not death or _held(directory / 'instance.lock'):
                return save('observing', 'service incarnation death unconfirmed')
            if value['attempts'] >= max_attempts:
                return save('needs_action', 'service restart budget exhausted')
            if not service_intent.permitted(self.catalog, project_id, epoch):
                return save('suppressed', 'service authorization changed')
            value['attempts'] += 1
            operation_id = str(uuid.uuid5(uuid.UUID(value['incident']), str(value['attempts'])))
            value['operation_id'] = operation_id
            save('starting')  # Durable ID precedes the only effect call.
            try:
                self.services.action(project_id, 'start', operation_id, expected_service_epoch=epoch)
            except Exception:
                return save('observing', 'service operation outcome unconfirmed; no replay')
            return save('observing')

    def run(self, nonce, poll_interval=.2):
        identifier(nonce)
        self._guard()
        workers = {}
        offset = 0
        with lock(self.directory / 'instance.lock', blocking=False):
            with lock(self.directory / 'state.lock'):
                record = read_json(self.directory / 'instance.json', {})
                if record.get('schema_version') != 1 or record.get('nonce') != nonce or record.get('roots') != self._roots():
                    raise ValueError('manager launch identity mismatch')
                claim_path = self.directory / (nonce + '.claim')
                fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                record.update(pid=os.getpid(), process_stamp=process_stamp(os.getpid()), boot_id=boot_id(), state='running')
                atomic_json(claim_path, record)
                atomic_json(self.directory / 'instance.json', record)
            try:
                while read_json(self.directory / 'stop.json', {}).get('nonce') != nonce:
                    projects = list(self.catalog.read()['projects'])
                    if projects:
                        offset %= len(projects)
                        projects = projects[offset:] + projects[:offset]
                        offset += 1
                    for project_id in projects:
                        workers = {key: worker for key, worker in workers.items() if worker.is_alive()}
                        if project_id in workers or len(workers) >= 8:
                            continue
                        def work(selected=project_id):
                            try:
                                self.tick(selected)
                            except Exception:
                                pass  # Tick persists actionable state; a busy project is retried next poll.
                        worker = threading.Thread(target=work, daemon=True)
                        workers[project_id] = worker
                        worker.start()
                    time.sleep(poll_interval)
            finally:
                with lock(self.directory / 'state.lock'):
                    record.update(state='stopping')
                    atomic_json(self.directory / 'instance.json', record)
                deadline = time.monotonic() + self.wait_timeout
                for worker in workers.values():
                    worker.join(max(0, deadline - time.monotonic()))
                with lock(self.directory / 'state.lock'):
                    record.update(state='stopped')
                    atomic_json(self.directory / 'instance.json', record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--config-root', required=True)
    parser.add_argument('--state-root', required=True)
    args = parser.parse_args()
    Manager(Catalog(Path(args.config_root), Path(args.state_root))).run(args.nonce)


if __name__ == '__main__':
    main()
