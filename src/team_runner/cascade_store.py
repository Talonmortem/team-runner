"""Durable cascade state and the checkout-wide mutation fence."""

from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile

from . import queue_plan, runner_operations
from .catalog import identifier, unsettled
from .runner_store import atomic_json, lock, read_json
from .verification import digest


TERMINAL = frozenset(('completed', 'stopped'))


def freeze_queue(goal):
    snapshot = queue_plan.read(goal)
    for feature in snapshot['queue']['queue']:
        if (len(feature['items']) != 1 or len(feature['depends_on']) > 1
                or 'stage_gate' in feature):
            raise ValueError(f"{feature['id']}: cascade requires one item, at most one predecessor, and no stage gate")
        item = feature['items'][0]
        if item['depends_on'] or item.get('check_scope', 'item') != 'item':
            raise ValueError(f"{feature['id']}/{item['id']}: cascade requires an item check with no item dependencies")
    return snapshot


def head(checkout):
    return subprocess.run(
        ['git', '-C', str(checkout), 'rev-parse', '--verify', 'HEAD^{commit}'],
        capture_output=True, check=True, text=True, timeout=15,
    ).stdout.strip()


class Session:
    def __init__(self, catalog, context, execution, backend):
        self.catalog = catalog
        self.context = context
        self.execution = identifier(execution)
        self.backend = backend
        self.directory = context.state_dir / 'cascades' / self.execution
        self.registry = self.directory / 'execution.json'
        self.index = context.state_root / 'cascades' / (self.execution + '.json')
        self.lease = {'identity': context.identity(), 'cascade': self.execution}

    @classmethod
    def open(cls, catalog, execution, backend, project=None, workspace=None):
        path = catalog.state_root / 'cascades' / (identifier(execution) + '.json')
        identity = read_json(path)
        if identity is None:
            raise ValueError('unknown cascade execution')
        if ((project is not None and identity['project_id'] != project)
                or (workspace is not None and identity['workspace_id'] != workspace)):
            raise ValueError('cascade project/workspace mismatch')
        context = catalog.context(identity['project_id'], identity['workspace_id'], allow_disabled=True)
        if identity != context.identity():
            raise ValueError('cascade catalog identity changed')
        return cls(catalog, context, execution, backend)

    def call(self, *args, mailbox=None):
        return self.backend.call(*args, mailbox=mailbox)

    def _load(self):
        state = read_json(self.registry)
        if not isinstance(state, dict) or state.get('execution') != self.execution or state.get('identity') != self.lease['identity']:
            raise ValueError('cascade registry identity mismatch')
        if state['snapshot_digest'] != digest(state['snapshot']):
            raise ValueError('cascade queue snapshot changed')
        return state

    def read(self):
        with lock(self.context.checkout_lock):
            return self._load()

    def _check_lease(self):
        if read_json(self.context.lease_path) != self.lease:
            raise ValueError('cascade does not own this checkout lease')

    def _check_available(self):
        previous = read_json(self.context.lease_path)
        if previous == self.lease:
            return
        self.catalog.require_no_cascade(self.context)
        owner = previous['identity'] if previous else self.context.identity()
        state_dir = self.catalog.state_dir(owner['project_id'], owner['workspace_id'])
        if unsettled(state_dir) or (state_dir != self.context.state_dir and unsettled(self.context.state_dir)):
            raise ValueError('checkout belongs to an active or unknown Runner attempt')

    def initialize(self, goal, allow_commits):
        """Publish immutable initialization before authorizing any Orca mutation."""
        with lock(self.index.with_suffix('.lock')), lock(self.context.checkout_lock, blocking=False), lock(
            self.context.config_root / 'catalog.lock'
        ):
            current = self.catalog.context(self.context.project_id, self.context.workspace_id)
            if current != self.context:
                raise ValueError('catalog configuration changed during cascade start')
            index = read_json(self.index)
            if index is not None and index != self.lease['identity']:
                raise ValueError('cascade execution belongs to a different workspace')
            if self.registry.exists():
                state = self._load()
                if state['manager'] != self.backend.manager_identity(self.context):
                    raise ValueError('cascade manager identity mismatch')
                if state['status'] == 'active':
                    self._check_available()
                    atomic_json(self.context.lease_path, self.lease)
                return state

            self._check_available()
            profile = self.context.profile
            workflow = profile['workflow']
            if not allow_commits or not workflow['allow_commits']:
                raise ValueError('cascade requires --allow-commits and workflow.allow_commits')
            if workflow.get('commit_after_review'):
                raise ValueError('cascade requires the candidate commit before testing and review')
            if any(check.get('scope') == 'stage' for check in profile['checks']):
                raise ValueError('cascade does not support profile stage checks')
            snapshot = freeze_queue(goal)
            settings = self.backend.role_snapshots(self.context)
            sha = head(self.context.repo)
            manager = self.backend.preflight(self.context)

            self.directory.mkdir(parents=True, exist_ok=True)
            frozen = self.directory / 'snapshot'
            if frozen.exists():
                # An interruption before publishing the registry left no authorized effects.
                # Refuse to replace materials if the source or policy has since changed.
                saved = read_json(frozen / 'start.json')
                if saved != {'snapshot': snapshot, 'role_settings': settings, 'base_sha': sha,
                              'manager': manager, 'profile': profile}:
                    raise ValueError('incomplete cascade initialization has different inputs')
            else:
                with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                    staging = Path(temporary) / 'snapshot'
                    self.context.snapshot(staging)
                    instructions = self.context.repo / 'AGENTS.md'
                    if instructions.is_file():
                        (staging / 'materials' / 'project-AGENTS.md').write_bytes(instructions.read_bytes())
                    atomic_json(staging / 'start.json', {
                        'snapshot': snapshot, 'role_settings': settings, 'base_sha': sha,
                        'manager': manager, 'profile': profile,
                    })
                    staging.rename(frozen)

            state = {
                'schema_version': 1, 'execution': self.execution, 'status': 'active',
                'identity': self.lease['identity'], 'uid': os.geteuid(), 'manager': manager,
                'manager_run': None, 'base_sha': sha, 'profile': profile, 'role_settings': settings,
                'snapshot': snapshot, 'snapshot_digest': digest(snapshot), 'features': {},
                'operations': {}, 'questions': {}, 'child_leases': {},
            }
            atomic_json(self.index, state['identity'])
            atomic_json(self.registry, state)
            atomic_json(self.context.lease_path, self.lease)
            return state

    def claim_child(self, feature_id):
        """Exclude a separately registered Runner from a cascade-created checkout."""
        with self.transaction() as state:
            checkout = Path(state['features'][feature_id]['checkout'])
            if checkout == self.context.repo:
                return
            child = replace(self.context, repo=checkout)
            with lock(child.checkout_lock, blocking=False):
                previous = read_json(child.lease_path)
                if previous and previous != self.lease:
                    self.catalog.require_no_cascade(child)
                    owner = previous['identity']
                    if unsettled(self.catalog.state_dir(owner['project_id'], owner['workspace_id'])):
                        raise ValueError('child checkout belongs to an active or unknown Runner attempt')
                state['child_leases'][feature_id] = str(checkout)
                atomic_json(self.registry, state)
                atomic_json(child.lease_path, self.lease)

    def _release(self, path):
        if read_json(path) == self.lease:
            path.unlink()
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def transaction(self, *, stopping=False):
        with lock(self.context.checkout_lock):
            state = self._load()
            self._check_lease()
            expected = 'stopping' if stopping else 'active'
            if state['status'] != expected:
                raise ValueError(f"cascade is {state['status']}")
            yield state
            atomic_json(self.registry, state)

    def begin_stop(self):
        with lock(self.context.checkout_lock):
            state = self._load()
            if state['status'] in TERMINAL:
                return state
            self._check_lease()
            if state['status'] == 'stopping':
                return state
            state['status'] = 'stopping'
            for operation in state['operations'].values():
                if operation['status'] == 'intent':
                    operation['status'] = 'cancelled'
            atomic_json(self.registry, state)
            return state

    def finish(self, result, *, stopping=False):
        with lock(self.context.checkout_lock):
            state = self._load()
            if state['status'] not in TERMINAL:
                self._check_lease()
                if state['status'] != ('stopping' if stopping else 'active'):
                    raise ValueError(f"cascade is {state['status']}")
                if not isinstance(result, dict) or result.get('resources_exited') is not True:
                    raise ValueError('cascade termination lacks cleanup evidence')
                for op in state['operations'].values():
                    if op['status'] == 'intent' and op['args'][:2] in (
                        ['orchestration', 'worker-stop'], ['terminal', 'close'], ['docker', 'container-rm']
                    ):
                        op['status'] = 'cancelled'
                if any(op['status'] not in ('completed', 'cancelled') for op in state['operations'].values()):
                    raise ValueError('cascade has unresolved operations')
                state.update(status='stopped' if stopping else 'completed', termination=result)
                if stopping:
                    for question in state['questions'].values():
                        question['stopped'] = True
                atomic_json(self.registry, state)
                runner_operations.checkpoint('cascade-stop:after_terminal_record' if stopping
                                             else 'cascade-complete:after_terminal_record')
            if not isinstance(state.get('termination'), dict) or state['termination'].get('resources_exited') is not True:
                raise ValueError('cascade termination lacks cleanup evidence')
            for checkout in state['child_leases'].values():
                child = replace(self.context, repo=Path(checkout))
                if read_json(child.lease_path) == self.lease:
                    with lock(child.checkout_lock, blocking=False):
                        self._release(child.lease_path)
            self._release(self.context.lease_path)
            return state

    def _mark_receipt(self, key, result, validate=None):
        if not isinstance(result, dict):
            raise ValueError('Orca mutation did not return a structured receipt')
        if validate:
            validate(result)
        with lock(self.context.checkout_lock):
            state = self._load()
            self._check_lease()
            operation = state['operations'][key]
            if operation['status'] != 'calling':
                raise ValueError('Orca mutation is no longer in progress')
            operation.update(status='completed', result=result)
            operation.pop('error', None)
            atomic_json(self.registry, state)
        return result

    def effect(self, key, *, validate=None, recovery=None, stopping=False, invoke=None):
        """Fence admission, then serialize the original CLI process and reconciliation."""
        with lock(self.directory / 'effect.lock', inherited=True) as fd:
            token = runner_operations.inherited_lock.set(fd)
            try:
                with self.transaction(stopping=stopping) as state:
                    operation = state['operations'][key]
                    completed = operation['status'] == 'completed'
                    recovering = operation['status'] == 'calling'
                    if operation['status'] not in ('intent', 'calling', 'completed'):
                        raise ValueError(f"operation {key} is {operation['status']}")
                    if not completed:
                        operation['status'] = 'calling'
                if completed:
                    if validate:
                        validate(operation['result'])
                    return operation['result']
                if recovering:
                    result = recovery(operation) if recovery else self.backend.reconcile(self, operation)
                    if result is not None:
                        return self._mark_receipt(key, result, validate)
                    if stopping or operation['args'][0] != 'orchestration':
                        raise runner_operations.UncertainOperation(f"request {operation['request_id']} is pending")
                    # Re-check the fence before replaying a positively replayable pending request.
                    with self.transaction():
                        pass
                args = operation['args']
                if args[0] == 'orchestration':
                    args = [*args, '--retry-request', operation['request_id']]
                runner_operations.checkpoint(args[1] + ':before_effect')
                result = invoke() if invoke else self.call(*args, mailbox=operation['mailbox'])
                runner_operations.checkpoint(args[1] + ':after_effect')
                return self._mark_receipt(key, result, validate)
            finally:
                runner_operations.inherited_lock.reset(token)

    def settle(self, recover_local):
        """During stop, reconcile started calls without replaying unknown effects."""
        with lock(self.directory / 'effect.lock', blocking=False, inherited=True) as fd:
            token = runner_operations.inherited_lock.set(fd)
            try:
                for key, operation in self.read()['operations'].items():
                    if operation['status'] != 'calling':
                        continue
                    try:
                        if operation['args'][0] == 'verification':
                            result = recover_local(operation)
                            if result is None:
                                # The guarded process and its PID namespace hold
                                # effect.lock. No process survived acquisition.
                                result = {'result': 'INTERRUPTED', 'proof': 'verification process lock released'}
                        else:
                            result = self.backend.reconcile(self, operation)
                        if result is None:
                            raise ValueError('request is still pending')
                        if 'expected' in operation:
                            from .cascade_messages import recover_retrieval, verify_operation
                            result = recover_retrieval(self, operation, result)
                            verify_operation(self, operation, result)
                        self._mark_receipt(key, result)
                    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                        raise runner_operations.UncertainOperation(
                            f"{key}, request {operation['request_id']}: {exc}"
                        ) from exc
            finally:
                runner_operations.inherited_lock.reset(token)
