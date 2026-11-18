"""Detached observer process; existing manager owns service lifecycle."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .catalog import Catalog
from .runner_entry import boot_id, process_stamp
from .runner_store import atomic_json, lock, read_json
from .service_manager import Manager


class ObserverProcess(Manager):
    def __init__(self, catalog, **kwargs):
        super().__init__(catalog, **kwargs)
        self.directory = catalog.state_root / 'observer' / 'process'

    def _spawn(self, nonce):
        env = {k: v for k, v in os.environ.items() if not k.startswith('TEAM_RUNNER_')}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get('PYTHONPATH', '')
        child = subprocess.Popen([sys.executable, '-m', 'team_runner.observer_service', '--nonce', nonce,
                                  '--config-root', str(self.catalog.config_root), '--state-root', str(self.catalog.state_root)],
                                 cwd='/', env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        self.children.append(child)
        threading.Thread(target=child.wait, daemon=True).start()
        return child

    def run(self, nonce, poll_interval=10):
        from .infra_observer import Observer
        observer = Observer(self.catalog)
        with lock(self.directory / 'instance.lock', blocking=False):
            with lock(self.directory / 'state.lock'):
                record = read_json(self.directory / 'instance.json', {})
                if record.get('nonce') != nonce or record.get('roots') != self._roots():
                    raise ValueError('observer process identity mismatch')
                claim = self.directory / (nonce + '.claim')
                fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
                record.update(state='running', pid=os.getpid(), process_stamp=process_stamp(os.getpid()), boot_id=boot_id())
                atomic_json(claim, record); atomic_json(self.directory / 'instance.json', record)
            while True:
                try:
                    state = observer.tick()
                    active = state['incidents'].get(state['active']) if state['active'] else None
                    # No new effect starts after stop. An already dispatched worker
                    # and application/result receipt must settle before owner exit.
                    unsettled = state.get('cleanup_pending') or active and (active.get('application', {}).get('status') == 'applying'
                        or any(s.get('status') in ('running', 'dispatching') for s in active.get('stages', {}).values())
                        or active.get('status') == 'checking_result')
                    if state['desired'] == 'stopped' and not unsettled:
                        break
                except Exception as exc:
                    with lock(observer.directory / 'control.lock'):
                        state = observer.load(); state['blocker'] = 'observer tick unavailable: ' + type(exc).__name__
                        atomic_json(observer.path, state)
                time.sleep(poll_interval)
            with lock(self.directory / 'state.lock'):
                record.update(state='stopped'); atomic_json(self.directory / 'instance.json', record)


def lifecycle_tick(catalog):
    """Manager-only service ownership; durable authorization, no goal launch."""
    from .infra_observer import Observer
    observer = Observer(catalog)
    state = observer.load()
    if not state['config'] or state['desired'] != 'running':
        return
    process = ObserverProcess(catalog)
    observed = process.status()
    if observed.get('state') == 'running' or not observed.get('confirmed') or observed.get('state') != 'stopped':
        return
    with lock(observer.directory / 'lifecycle.lock', blocking=False):
        journal = read_json(observer.directory / 'lifecycle.json', {'scope': observer.scope(state), 'attempts': 0})
        if journal['scope'] != observer.scope(state):
            journal = {'scope': observer.scope(state), 'attempts': 0}
        if journal['attempts'] >= 2:
            return
        journal['attempts'] += 1
        request_id = str(uuid.uuid4())
        journal['request_id'] = request_id
        atomic_json(observer.directory / 'lifecycle.json', journal)
        process.ensure(request_id)


def cli(args, catalog):
    from .infra_observer import Observer
    observer = Observer(catalog)
    if args.action == 'status':
        return observer.status()
    if args.action == 'incidents':
        return observer.incidents()
    if not args.request_id or not args.scope:
        raise ValueError('observer mutations require --request-id and current --scope from status')
    payload = {}
    if args.action == 'configure':
        if not args.file:
            raise ValueError('configure requires --file JSON')
        payload = read_json(args.file)
    elif args.action == 'mode':
        payload = {'mode': args.mode}
    elif args.action == 'approve':
        payload = {'incident': args.incident, 'plan': args.plan}
    result = observer.command(args.action, args.request_id, args.scope, payload)
    if args.action == 'start':
        result['process'] = ObserverProcess(catalog).ensure(args.request_id)
    return result


def main():
    parser = argparse.ArgumentParser()
    for name in ('nonce', 'config-root', 'state-root'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    ObserverProcess(Catalog(Path(args.config_root), Path(args.state_root))).run(args.nonce)


if __name__ == '__main__':
    main()
