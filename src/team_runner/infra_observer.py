"""Explicit infrastructure observation and bounded, independently checked repairs.

Observer state is never a product Runner state. Only its dedicated registered
workspace owns agents; observed workspaces are read-only inputs.
"""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .catalog import identifier
from .runner_store import atomic_json, lock, read_json

MODES = ('diagnostic', 'approval', 'auto')
STAGES = {'diagnose': 'explorer', 'candidate': 'implementer_complex',
          'review': 'final_reviewer', 'result': 'tester'}
KINDS = ('runner', 'test', 'environment', 'dependency', 'tool')
TERMINAL = ('closed', 'suppressed', 'blocked')
SEMANTIC_INTERVAL = 3600


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('repair inputs require existing regular files')
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Observer:
    def __init__(self, catalog, clock=time.time, backend=None):
        self.catalog, self.clock, self.backend = catalog, clock, backend
        self.directory = catalog.state_root / 'observer'
        self.path = self.directory / 'state.json'

    def load(self):
        return read_json(self.path, {'schema_version': 1, 'revision': 0, 'config': None,
                                    'paused': False, 'desired': 'stopped', 'incidents': {},
                                    'cursors': {}, 'summary': [], 'summary_omitted': 0, 'semantic_at': 0,
                                    'semantic_pending': False, 'active': None})

    def scope(self, state=None):
        state = self.load() if state is None else state
        return digest({'roots': [str(self.catalog.config_root.resolve()), str(self.catalog.state_root.resolve())],
                       'revision': state['revision'], 'config': state['config']})

    def status(self):
        state = self.load()
        from .observer_service import ObserverProcess
        return {'schema_version': 1, 'scope': self.scope(state), 'revision': state['revision'],
                'configured': state['config'] is not None, 'config': state['config'],
                'paused': state['paused'], 'desired': state['desired'], 'active': state['active'],
                'incident_count': len(state['incidents']), 'process': ObserverProcess(self.catalog).status(),
                'last_observed_at': state.get('last_observed_at'), 'blocker': state.get('blocker')}

    def validate_config(self, value):
        from . import runner_config
        if not isinstance(value, dict) or set(value) != {'project_id', 'workspace_id', 'projects', 'mode', 'repair_roots', 'roles'}:
            raise ValueError('observer configuration requires project_id/workspace_id/projects/mode/repair_roots/roles')
        ctx = self.catalog.context(value['project_id'], value['workspace_id'])
        if not (ctx.repo / 'src/team_runner/infra_observer.py').is_file():
            raise ValueError('observer execution workspace must be a registered Team Runner checkout')
        if value['mode'] not in MODES:
            raise ValueError('invalid observer mode')
        if (not isinstance(value['projects'], list) or not value['projects']
                or len(set(value['projects'])) != len(value['projects'])):
            raise ValueError('explicit unique observed project IDs required')
        catalog = self.catalog.read()['projects']
        for project in value['projects']:
            if identifier(project) not in catalog or project == ctx.project_id:
                raise ValueError('unknown observed project or recursive observation')
        if set(value['roles']) != set(STAGES):
            raise ValueError('all four independent service roles required')
        defaults = runner_config.defaults()
        for stage, settings in value['roles'].items():
            if settings.get('agent') != 'codex':
                raise ValueError('observer service currently requires Codex filesystem sandbox')
            defaults['roles'][STAGES[stage]] = settings
        runner_config.validate(defaults, repo=ctx.repo, config_root=ctx.config_root.parent)
        if not isinstance(value['repair_roots'], list) or not value['repair_roots']:
            raise ValueError('explicit infrastructure repair roots required')
        for row in value['repair_roots']:
            if not isinstance(row, dict) or set(row) != {'path', 'kind', 'shared'} or row['kind'] not in KINDS or type(row['shared']) is not bool:
                raise ValueError('repair roots require path/kind/shared')
            path = Path(row['path'])
            if not path.is_absolute() or not path.exists() or path.is_symlink() or path.resolve() != path:
                raise ValueError('repair roots require canonical existing absolute paths')
            if path == Path('/') or self.directory.resolve().is_relative_to(path):
                raise ValueError('observer policy/state cannot be a repair root')
        current = read_json(ctx.state_dir / 'state.json', {})
        if current and current.get('owner') != 'infra-observer':
            raise ValueError('execution workspace already owns a product Runner state')
        return copy.deepcopy(value)

    def command(self, action, request_id, scope, payload=None):
        if os.environ.get('TEAM_RUNNER_ATTEMPT'):
            raise ValueError('child attempts cannot control observer policy or lifecycle')
        identifier(request_id)
        payload = payload or {}
        args = {'action': action, 'scope': scope, 'payload': payload}
        receipt = self.directory / 'commands' / (request_id + '.json')
        with lock(self.directory / 'control.lock'):
            old = read_json(receipt)
            if old:
                if old['args'] != args:
                    raise ValueError('observer request ID conflict')
                return old['result']
            state = self.load()
            recorded = state.get('command_receipts', {}).get(request_id)
            if recorded:
                if recorded['args'] != args:
                    raise ValueError('observer request ID conflict')
                return recorded['result']
            if scope != self.scope(state):
                raise ValueError('stale observer scope')
            if action == 'configure':
                if state['active']:
                    raise ValueError('cannot reconfigure an unsettled incident')
                value = self.validate_config(payload)
                if state['config'] and (value['project_id'], value['workspace_id']) != (state['config']['project_id'], state['config']['workspace_id']):
                    if read_json(self.context(state).state_dir / 'state.json', {}).get('current'):
                        raise ValueError('execution workspace cannot change with retained attempts')
                state.update(config=value, paused=False)
            elif not state['config']:
                raise ValueError('observer not configured')
            elif action == 'mode':
                if payload.get('mode') not in MODES or set(payload) != {'mode'}:
                    raise ValueError('invalid observer mode')
                state['config']['mode'] = payload['mode']
                # Scope-bound approval cannot survive a policy transition.
                for incident in state['incidents'].values():
                    incident.pop('approval', None)
            elif action == 'approve':
                if set(payload) != {'incident', 'plan'}:
                    raise ValueError('incident and exact plan digest required')
                incident = state['incidents'].get(payload['incident'])
                if (not incident or incident['status'] != 'approval' or not incident.get('plan')
                        or payload['plan'] != digest(incident['plan'])
                        or incident['plan'].get('evidence') != digest(incident['evidence'])):
                    raise ValueError('stale incident approval')
                incident['approval'] = {'plan': payload['plan'], 'policy': digest(state['config']),
                                        'request_id': request_id, 'at': self.clock(), 'evidence': digest(incident['evidence'])}
                incident['status'] = 'ready'
            elif action == 'start':
                state.update(desired='running', paused=False)
            elif action == 'pause':
                state['paused'] = True
            elif action == 'stop':
                state.update(desired='stopped', paused=True)
            else:
                raise ValueError('invalid observer action')
            state['revision'] += 1
            result = {'action': action, 'request_id': request_id, 'scope': self.scope(state), 'revision': state['revision']}
            # State transition and command receipt commit together. Losing the
            # convenience receipt file never replays or strands a command.
            state.setdefault('command_receipts', {})[request_id] = {'args': args, 'result': result}
            atomic_json(self.path, state)
            atomic_json(receipt, {'args': args, 'result': result})
            return result

    def context(self, state=None):
        config = (self.load() if state is None else state)['config']
        return self.catalog.context(config['project_id'], config['workspace_id'])

    def incidents(self):
        return {'scope': self.scope(), 'incidents': list(self.load()['incidents'].values())}

    def _incident(self, state, source, kind, evidence):
        key = digest({'source': source, 'kind': kind})
        existing = next((i for i in state['incidents'].values() if i['key'] == key and i['status'] != 'closed'), None)
        if existing:
            if digest(evidence) not in existing['evidence_hashes']:
                existing['evidence'].append(evidence)
                existing['evidence_hashes'].append(digest(evidence))
                existing['updated_at'] = self.clock()
                existing['new_evidence'] = True
                state['revision'] += 1
                if existing['status'] in ('suppressed', 'approval', 'ready'):
                    existing['status'] = 'detected'
                existing.pop('approval', None)
            return existing
        incident = {'id': str(uuid.uuid4()), 'key': key, 'source': source, 'kind': kind,
                    'status': 'detected', 'evidence': [evidence], 'evidence_hashes': [digest(evidence)],
                    'created_at': self.clock(), 'updated_at': self.clock(), 'attempts': 0,
                    'actions': [], 'results': [], 'diagnosis': None, 'repair_scope': None}
        state['incidents'][incident['id']] = incident
        return incident

    @staticmethod
    def _summary(path, value):
        if not isinstance(value, dict):
            return 'unknown'
        if path.name == 'state.json':
            cur = value.get('current')
            cur = cur if isinstance(cur, dict) else {}
            return {**{k: value.get(k, 'unknown') for k in ('paused', 'blocker', 'last_error', 'control_uncertain')},
                    'current': {k: cur.get(k, 'unknown') for k in ('attempt_dir', 'failed', 'dispatch_id', 'resource_waits')}}
        if path.name == 'agents.json':
            agents = value.get('agents')
            return {'fenced': value.get('fenced', 'unknown'), 'agents': [
                {k: a.get(k, 'unknown') for k in ('id', 'dispatch_id', 'status', 'role', 'activity', 'runtime_state', 'error')}
                for a in agents if isinstance(a, dict)]} if isinstance(agents, list) else 'unknown'
        if path.name == 'network-recovery.json':
            sessions = value.get('sessions')
            return {'sessions': [{k: v.get(k, 'unknown') for k in ('dispatch', 'status', 'failure', 'failures', 'scope', 'reason')}
                                 for v in sessions.values() if isinstance(v, dict)]} if isinstance(sessions, dict) else 'unknown'
        if path.name == 'state-time.json':
            return {k: value.get(k, 'unknown') for k in ('last', 'seconds')}
        if path.name == 'coordinator-events.json':
            events = value.get('events')
            return {'events': [{k: event[k] for k in (
                'id', 'run_id', 'message_id', 'type', 'created_at', 'dispatch',
                'processed_at', 'classification', 'active', 'status', 'error') if k in event}
                if isinstance(event, dict) else 'unknown' for event in events[-30:]]} if isinstance(events, list) else 'unknown'
        return {k: value.get(k, 'unknown') for k in ('id', 'attempt', 'inputs', 'result', 'status', 'started_at', 'created_at', 'resource_wait', 'check_id', 'waiting_at')}

    @staticmethod
    def _activity_value(path, value):
        if path.name == 'state-time.json' and isinstance(value, dict):
            last = value.get('last')
            return {k: last.get(k, 'unknown') for k in ('kind', 'scope')} if isinstance(last, dict) else 'unknown'
        return value

    def _changed(self, state, path, source):
        key = str(path)
        previous = state['cursors'].get(key, {})
        try:
            stat = path.stat()
        except OSError:
            if previous.get('unknown'):
                return None
            state['cursors'][key] = {'unknown': True, 'value': 'unknown'}
            return {'source': source, 'path': key, 'value': 'unknown', 'at': self.clock()}
        stamp = [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size]
        if previous.get('stamp') == stamp:
            return None
        try:
            value = self._summary(path, read_json(path))
        except (OSError, ValueError, TypeError, AttributeError):
            value = 'unknown'
        state['cursors'][key] = {'stamp': stamp, 'value': value}
        if 'value' in previous and self._activity_value(path, previous['value']) == self._activity_value(path, value):
            return None
        return {'source': source, 'path': key, 'value': value, 'at': self.clock()}

    @staticmethod
    def _time(value):
        if type(value) in (int, float):
            return value / 1000 if value > 100000000000 else value
        if isinstance(value, str):
            try:
                return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
        return None

    def observe(self, state):
        catalog = self.catalog.read()['projects']
        activity = []
        for project_id in state['config']['projects']:
            project = catalog.get(project_id)
            if project is None:
                continue
            for workspace_id, workspace in project['workspaces'].items():
                source = {'project': project_id, 'workspace': workspace_id}
                base = self.catalog.state_dir(project_id, workspace_id)
                changed = self._changed(state, base / 'state.json', source)
                if changed:
                    activity.append(changed)
                value = state['cursors'].get(str(base / 'state.json'), {}).get('value')
                current = value.get('current', {}) if isinstance(value, dict) else {}
                attempt = current.get('attempt_dir')
                paths = [base / 'network-recovery.json', base / 'state-time.json']
                if isinstance(attempt, str) and attempt != 'unknown':
                    attempt = Path(attempt)
                    if attempt.resolve().parent != (base / 'attempts').resolve():
                        activity.append({'source': source, 'value': 'unknown attempt identity', 'at': self.clock()})
                        continue
                    paths += [attempt / 'agents.json', attempt / 'coordinator-events.json']
                    paths += sorted((attempt / 'verification').glob('*.json'))
                for path in paths:
                    change = self._changed(state, path, source)
                    if change:
                        activity.append(change)
                    record = state['cursors'].get(str(path), {}).get('value')
                    if not isinstance(record, dict):
                        continue
                    if path.name == 'network-recovery.json' and change:
                        for session in record['sessions']:
                            fact = session.get('failure')
                            if not isinstance(fact, dict) or not fact.get('id') or not fact.get('error'):
                                continue
                            at = self._time(fact['id'])
                            if at is None or not 0 <= self.clock() - at <= 600:
                                continue  # Unknown event time never becomes a recent failure.
                            key = digest({'source': source, 'error': fact['error']})
                            history = state.setdefault('failure_history', {})
                            rows = [r for r in history.get(key, []) if self.clock() - r['at'] <= 600]
                            if not any(r['id'] == fact['id'] for r in rows):
                                rows.append({'id': fact['id'], 'at': at, 'event': fact})
                            history[key] = rows
                            if len(rows) >= 2:
                                self._incident(state, source, 'infrastructure:' + key, {'failures': rows})
                    if path.parent.name != 'verification' or path.name.endswith(('.preflight.json', '.fixtures.json', '.resource-wait.json')):
                        continue
                    started = self._time(record.get('started_at'))
                    if record.get('status') == 'running' and started is not None and self.clock() - started > 300:
                        self._incident(state, {**source, 'check': str(path)}, 'long_check',
                                       {'path': str(path), 'started_at': started, 'reason': 'diagnostic signal only; useful work must not be interrupted'})
                    if change and isinstance(record.get('inputs'), dict) and started is not None:
                        preflight_path = Path(str(path) + '.preflight.json')
                        proof_change = self._changed(state, preflight_path, source)
                        proof = state['cursors'].get(str(preflight_path), {}).get('value')
                        if not isinstance(proof, dict) or proof.get('result') != 'PASS' or not 0 <= self.clock() - started <= 600:
                            continue
                        check_key = digest({'source': source, 'inputs': record['inputs']})
                        history = state.setdefault('check_history', {})
                        rows = [r for r in history.get(check_key, []) if self.clock() - r['at'] <= 600]
                        if not any(r['path'] == str(path) and r['execution'] == started for r in rows):
                            rows.append({'at': started, 'path': str(path), 'execution': started})
                        history[check_key] = rows
                        if len(rows) >= 3:
                            self._incident(state, source, 'repeated_check:' + check_key,
                                           {'confirmed_inputs': record['inputs'], 'executions': rows})
        if activity:
            pending = state['summary'] + activity
            state['summary_omitted'] = state.get('summary_omitted', 0) + max(0, len(pending) - 100)
            state['summary'] = pending[-100:]
            state['semantic_pending'] = True
        if state['semantic_pending'] and self.clock() - state['semantic_at'] >= SEMANTIC_INTERVAL:
            self._incident(state, {'projects': state['config']['projects']}, 'semantic_review',
                           {'summary': copy.deepcopy(state['summary']),
                            'omitted_summary_rows': state.get('summary_omitted', 0), 'unavailable': 'unknown'})
            state.update(semantic_at=self.clock(), semantic_pending=False, summary=[], summary_omitted=0)
        state['last_observed_at'] = self.clock()

    def tick(self):
        # Commands can pause during slow transport. No control lock is held while dispatching.
        with lock(self.directory / 'tick.lock', blocking=False):
            with lock(self.directory / 'control.lock'):
                state = self.load()
                if not state['config']:
                    return state
                self.observe(state)
                atomic_json(self.path, state)
            from .observer_workflow import Workflow
            backend = self.backend or Workflow(self)
            backend.advance()
            return self.load()
