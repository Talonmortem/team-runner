"""Observer service agents and deterministic, preconditioned file repair."""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from . import memory_admission
from . import runner_operations as operations
from .infra_observer import STAGES, digest, file_digest
from .runner_launch import Launcher
from .runner_store import atomic_json, lock, read_json


class ObserverLauncher(Launcher):
    def __init__(self, context, observer=None):
        super().__init__(context)
        self.observer = observer
        self.stage = None

    def _launch(self, *args, permitted=None, **kwargs):
        def allowed():
            state = self.observer.load() if self.observer else {}
            return (self.stage == 'result' or state.get('desired') == 'running' and not state.get('paused')
                    and (self.stage == 'diagnose' or state.get('config', {}).get('mode') != 'diagnostic')) and (permitted is None or permitted())
        return super()._launch(*args, permitted=allowed, **kwargs)

    def argv(self, config, state_dir, runtime=None):
        if config['agent'] != 'codex':
            raise ValueError('observer requires a Codex filesystem sandbox')
        command = super().argv(config, state_dir, runtime)
        attempt = Path(state_dir).resolve()
        work = Path(self.workspace) if getattr(self, 'workspace', None) else attempt / 'work' / self.stage
        work.mkdir(parents=True, exist_ok=True)
        (attempt / 'reports').mkdir(exist_ok=True)
        command += ['--cd', str(work), '-c', 'projects={' + json.dumps(str(work)) + '={trust_level="trusted"}}',
                    '-c', 'sandbox_workspace_write.exclude_slash_tmp=true',
                    '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true']
        for index, value in enumerate(command):
            if value.startswith('sandbox_workspace_write.writable_roots='):
                command[index] = 'sandbox_workspace_write.writable_roots=' + json.dumps(
                    [str(work), str(attempt / 'reports'), str(Path.home() / '.config/orca')])
        return command


class Workflow:
    def __init__(self, observer, launcher=None):
        self.observer = observer
        self.launcher = launcher

    def _save(self, state):
        atomic_json(self.observer.path, state)

    def _permitted(self, state):
        return state['desired'] == 'running' and not state['paused']

    def _cleanup(self, context, attempt):
        memory_admission.queue.cancel(context.state_dir, attempt)
        launcher = self.launcher or ObserverLauncher(context, self.observer)
        reg = launcher.registry(attempt)
        if any(a.get('status') not in ('released', 'stopped', 'superseded') for a in reg.get('agents', [])):
            return False
        if reg.get('stop_status') == 'stopped':
            return True
        if operations.reconcile(launcher.journal(attempt), launcher.orca):
            return False
        journal = read_json(launcher.journal(attempt), {})
        closed = journal.get('mailbox-close', {})
        for run in reg.get('runs', []):
            if (closed.get('status') == 'completed'
                    and closed.get('args') == ['terminal', 'close', '--terminal', run.get('mailbox')]
                    and closed.get('evidence', {}).get('incarnation') == run.get('incarnation')):
                run['status'] = 'closed'
        launcher.save_registry(attempt, reg)
        return launcher.stop_attempt(attempt)

    @staticmethod
    def _compact(value, items=12, chars=2048, depth=8):
        if isinstance(value, str):
            return value if len(value) <= chars else {
                'excerpt': value[:chars], 'omitted_characters': len(value) - chars,
                'sha256': digest(value), 'unavailable': 'full text omitted; use full_evidence reference if needed'}
        if isinstance(value, (dict, list)) and depth == 0:
            return {'omitted_items': len(value), 'sha256': digest(value), 'unavailable': 'nested detail omitted'}
        if isinstance(value, dict):
            pairs = list(value.items())
            result = {k[:chars]: Workflow._compact(v, items, chars, depth - 1) for k, v in pairs[:items]}
            if len(pairs) > items:
                result['_omitted_fields'] = len(pairs) - items
            if any(len(k) > chars for k, _ in pairs[:items]):
                result['_omitted_key_characters'] = True
            return result
        if isinstance(value, list):
            result = [Workflow._compact(v, items, chars, depth - 1) for v in value[-items:]]
            if len(value) > items:
                result.insert(0, {'omitted_items': len(value) - items, 'unavailable': 'older items omitted'})
            return result
        return value

    def _input(self, state, incident, attempt):
        evidence = incident.get('evidence', [])
        archive = attempt / 'observer-evidence.json'
        atomic_json(archive, {'incident_id': incident['id'], 'evidence': evidence, 'policy': state['config']})
        # Legacy incidents have no input cursor. Their most recent evidence is
        # sufficient for the initial compact view; the immutable archive retains all.
        start = min(incident.get('input_evidence_count', 0), max(0, len(evidence) - 1))
        if 'input_evidence_count' not in incident:
            start = max(0, len(evidence) - 1)
        recent = copy.deepcopy(evidence[start:])
        for item in recent:
            if not isinstance(item, dict) or not isinstance(item.get('summary'), list):
                continue
            for row in item['summary']:
                if isinstance(row, dict) and Path(row.get('path', '')).name == 'coordinator-events.json':
                    row['value'] = self.observer._summary(Path(row['path']), row.get('value'))
        view = {
            'incident': {k: incident.get(k, 'unknown') for k in (
                'id', 'kind', 'source', 'status', 'attempts', 'created_at', 'updated_at')},
            'evidence': recent,
            'previous': {'diagnosis': incident.get('diagnosis', 'unknown'), 'attempt': incident.get('attempt', 'unknown')},
            'policy': state['config'],
        }
        reference = {'path': str(archive), 'sha256': file_digest(archive), 'incident_id': incident['id'],
                     'evidence_count': len(evidence), 'selected_from_index': start,
                     'sections': ['evidence', 'policy'],
                     'history_path': str(self.observer.path),
                     'omitted_history': {k: len(incident.get(k, [])) for k in ('actions', 'stage_history', 'results')}}
        # Bound the actual pretty-printed UTF-8 transport, including legacy giant
        # values. Omissions stay explicit and never alter approval/evidence digests.
        items, chars = 12, 2048
        while True:
            result = {**{k: self._compact(v, items, chars) for k, v in view.items()}, 'full_evidence': reference,
                      'unavailable': 'omitted details are unknown; retrieve only relevant archive indices if needed'}
            if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) <= 64 * 1024:
                return result
            items = max(1, items // 2)
            chars = max(32, chars // 2)

    def _snapshot(self, state, incident):
        context = self.observer.context(state)
        existing = read_json(context.state_dir / 'state.json', {})
        if existing and existing.get('owner') != 'infra-observer':
            raise ValueError('execution workspace owned by another runner')
        current = existing.get('current') or {}
        if current:
            previous = state['incidents'].get(current.get('incident'))
            if not previous or previous['id'] != incident['id'] and previous['status'] not in ('closed', 'suppressed', 'blocked'):
                raise ValueError('previous service attempt unsettled')
            reg = read_json(Path(current['attempt_dir']) / 'agents.json', {})
            if any(a.get('status') not in ('released', 'stopped', 'superseded') for a in reg.get('agents', [])):
                raise ValueError('previous service agents unsettled')
            if not self._cleanup(context, Path(current['attempt_dir'])):
                raise RuntimeError('previous service mailbox cleanup unknown; retain attempt')
        if incident.get('stages'):
            incident.setdefault('stage_history', []).append(copy.deepcopy(incident['stages']))
        attempt = context.state_dir / 'attempts' / str(uuid.uuid4())
        attempt.mkdir(parents=True, mode=0o700)
        compact_input = self._input(state, incident, attempt)
        for key in ('plan', 'candidate', 'candidate_digest', 'review', 'application', 'approval', 'blocker',
                    'result_memory_deferred'):
            incident.pop(key, None)
        incident.update(diagnosis=None, repair_scope=None)
        context.snapshot(attempt)
        config = {'version': 2, 'roles': {STAGES[k]: copy.deepcopy(v) for k, v in state['config']['roles'].items()}, 'reserves': {}}
        atomic_json(attempt / 'config.json', config)
        atomic_json(attempt / 'observer-input.json', compact_input)
        incident['input_evidence_count'] = len(incident.get('evidence', []))
        incident.update(attempt=str(attempt), frozen={name: file_digest(attempt / name) for name in ('context.json', 'config.json', 'observer-input.json', 'observer-evidence.json')},
                        stages={}, stage='diagnose', status='diagnosing')
        atomic_json(context.state_dir / 'state.json', {'owner': 'infra-observer',
                    'current': {'attempt_dir': str(attempt), 'incident': incident['id']}})
        state['active'] = incident['id']

    def _intact(self, incident):
        attempt = Path(incident['attempt'])
        return all(file_digest(attempt / name) == value for name, value in incident['frozen'].items())

    def _spec(self, state, incident, stage, output):
        attempt = Path(incident['attempt'])
        schemas = {
            'diagnose': {'action': 'suppress|repair', 'diagnosis': 'evidence-based explanation; unavailable inputs unknown',
                         'scope': 'runner|test|environment|dependency|tool', 'files': ['/absolute/allowed/existing/file'],
                         'reason': 'measurable infrastructure defect and expected outcome'},
            'candidate': {'files': [{'path': '/absolute/existing/file', 'before': 'SHA256 from frozen plan',
                                     'artifact': 'relative path under candidate stage work directory', 'after': 'SHA256 of replacement'}],
                          'explanation': 'infrastructure-only fix; no weakened checks'},
            'review': {'verdict': 'PASS|FAIL', 'candidate': 'candidate digest', 'correctness': 'independent findings',
                       'checks': [{'argv': ['command'], 'exit_code': 0, 'evidence': 'relative/path'}],
                       'safe': True, 'users_absent': True, 'users_evidence': 'exact read-only resource/user inventory'},
            'result': {'verdict': 'PASS|FAIL', 'candidate': 'candidate digest', 'correctness': True,
                       'fault_removed': True, 'before': 1, 'after': 0, 'metric': 'specific measurable failure/count/duration',
                       'checks': [{'argv': ['command'], 'exit_code': 0, 'evidence': 'relative/path'}]}}
        return ('Infrastructure observer service stage: ' + stage + '.\n'
                'Read repository by absolute path: ' + str(self.observer.context(state).repo) + '\n'
                'ALL repository files, product workspaces, running agents and settings are READ ONLY. '
                'Write artifacts ONLY under your attempt. No shell/cwd tricks, no commands that modify source/environment. '
                'Never interrupt useful checks, child waits or silent agents. Signals are hypotheses, not proven defects. '
                'No product logic changes, weakened acceptance/tests, self-policy/budget changes or observer code repair. '
                'No new runtime dependencies. Do not send to product agents or read their mailboxes. '
                'Candidate stage prepares replacement artifacts under its current stage working directory; use paths relative to TEAM_RUNNER_ATTEMPT. The service alone applies approved files later. '
                'Reviewer must independently inspect exact candidate and verify correctness in an isolated disposable copy '
                'under the attempt if execution is needed; do not run tests against live resources. '
                'For shared resources users_absent requires concrete complete inventory; unknown means false. '
                'Result stage checks applied files independently and demonstrates actual improvement or fault elimination. '
                'Return a failed report if any required proof is unavailable.\n'
                'Frozen input: ' + str(attempt / 'observer-input.json') + '\n'
                'Read this compact input first. Do not reread entire history or producer logs. '
                'Only if a specific diagnosis needs omitted detail, retrieve the relevant evidence index/source '
                'from its full_evidence archive; omissions are unknown, never proof of health.\n'
                'Current plan/candidate/apply evidence: ' + json.dumps({k: incident.get(k) for k in ('plan', 'candidate', 'application')}) + '\n'
                'Write JSON to ' + str(output) + ' using schema ' + json.dumps(schemas[stage]) + '\n'
                'Include output path relative to TEAM_RUNNER_ATTEMPT in worker_report checks. '
                'Do not report succeeded before producing the output. Verification evidence paths are relative to attempt.\n')

    def _dispatch(self, state, incident, stage, launcher):
        attempt = Path(incident['attempt'])
        row = incident['stages'].get(stage)
        if row:
            return row
        context = self.observer.context(state)

        def permitted():
            fresh = self.observer.load()
            active = fresh['incidents'].get(fresh['active'], {})
            return (active.get('id') == incident['id'] and active.get('attempt') == str(attempt)
                    and (self._permitted(fresh) or stage == 'result' and not active.get('result_memory_deferred'))
                    and (stage in ('diagnose', 'result') or fresh['config']['mode'] != 'diagnostic'))

        try:
            operation = f"observer:{incident['id']}:{incident['attempts']}:{stage}"
            with memory_admission.queue.hold(context.state_dir, attempt, operation, permitted=permitted):
                return self._dispatch_admitted(state, incident, stage, launcher, permitted)
        except memory_admission.WaitingMemory:
            if stage == 'result':
                # Post-apply verification may run immediately after stop. Once
                # deferred, it needs renewed permission before a new process.
                incident['result_memory_deferred'] = True
                self._save_merged(incident)
                if not self._permitted(self.observer.load()):
                    memory_admission.queue.cancel(context.state_dir, attempt)
            raise
        except memory_admission.AdmissionCancelled:
            return None

    def _dispatch_admitted(self, state, incident, stage, launcher, permitted):
        attempt = Path(incident['attempt'])
        work = attempt / 'work' / (str(incident['attempts']) + '-' + stage)
        work.mkdir(parents=True, exist_ok=True)
        output = work / 'result.json'
        spec = self._spec(state, incident, stage, output)
        spec_path = attempt / ('stage-' + str(incident['attempts']) + '-' + stage + '.md')
        # The digest is outside the sandbox's writable attempt.
        spec_path.write_text(spec)
        row = {'status': 'dispatching', 'function': stage + ':' + str(incident['attempts']), 'spec': file_digest(spec_path), 'output': str(output), 'at': self.observer.clock()}
        incident['stages'][stage] = row
        incident['actions'].append({'stage': stage, 'status': 'dispatching', 'spec': str(spec_path), 'spec_sha256': row['spec'], 'output': str(output), 'repair_attempt': incident['attempts']})
        self._save_merged(incident)
        # Recheck user intent immediately before transport. _launch checks it again
        # before terminal creation and worker-start through its permitted callback.
        if not permitted():
            incident['stages'].pop(stage)
            self._save_merged(incident)
            return None
        launcher.stage = stage
        launcher.workspace = work
        record = launcher.dispatch_role(attempt, STAGES[stage], spec,
                                        'infra observer ' + stage, attempt, function=row['function'],
                                        admitted=True, permitted=permitted)
        incident.pop('result_memory_deferred', None)
        row.update(status='running', agent=record['id'], dispatch=record['dispatch_id'])
        self._save_merged(incident)
        return row

    def _save_merged(self, incident):
        with lock(self.observer.directory / 'control.lock'):
            latest = self.observer.load()
            current = latest['incidents'][incident['id']]
            if current.get('evidence_hashes') != incident.get('evidence_hashes'):
                incident['evidence'] = current['evidence']
                incident['evidence_hashes'] = current['evidence_hashes']
                incident['new_evidence'] = True
                incident['status'] = 'detected'
                incident.pop('approval', None)
            else:
                # Approval belongs to control commands, never to the stale worker
                # copy. A downgrade's removal must not be restored here.
                approval = current.get('approval')
                if (approval and approval.get('policy') == digest(latest['config'])
                        and incident.get('plan') and approval.get('plan') == digest(incident['plan'])
                        and approval.get('evidence') == digest(incident['evidence'])):
                    incident['approval'] = approval
                    if incident['status'] == 'approval' and current['status'] == 'ready':
                        incident['status'] = 'ready'
                else:
                    incident.pop('approval', None)
            latest['incidents'][incident['id']] = incident
            self._save(latest)

    def _restore_dispatch(self, incident, row, launcher):
        attempt = Path(incident['attempt'])
        issues = operations.reconcile(launcher.journal(attempt), launcher.orca)
        if issues:
            return None
        with lock(attempt / 'agents.lock'):
            reg = launcher.registry(attempt)
            agents = [a for a in reg['agents'] if a.get('function') == row['function']]
            if len(agents) != 1:
                return None
            agent = agents[0]
            journal = read_json(launcher.journal(attempt), {})
            terminal_op = journal.get(agent['id'] + '-terminal', {})
            terminal = (terminal_op.get('result') or {}).get('terminal') or {}
            if not agent.get('terminal') and terminal_op.get('status') == 'completed':
                if terminal.get('worktreeId') != launcher.context.orca_id or not terminal.get('handle') or not terminal.get('incarnationId'):
                    raise ValueError('reconciled service terminal identity mismatch')
                agent.update(terminal=terminal['handle'], incarnation=terminal['incarnationId'], terminal_owned=True)
            dispatch_op = journal.get(agent['id'] + '-dispatch', {})
            if not agent.get('dispatch_id') and dispatch_op.get('status') == 'completed':
                args = dispatch_op.get('args', [])
                for flag, expected in (('--task', agent['task_id']), ('--run', agent['run_id']), ('--terminal', agent.get('terminal'))):
                    if flag not in args or args[args.index(flag) + 1] != expected:
                        raise ValueError('reconciled service dispatch arguments mismatch')
                dispatched = (dispatch_op.get('result') or {}).get('dispatchId')
                if not dispatched:
                    raise ValueError('reconciled dispatch receipt lacks dispatch identity')
                candidate = dict(agent, dispatch_id=dispatched)
                data = launcher.observe_agent(candidate)
                observed = data.get('dispatch') or {}
                if (observed.get('id'), observed.get('taskId'), observed.get('runId')) != (dispatched, agent['task_id'], agent['run_id']):
                    raise ValueError('reconciled worker identity mismatch')
                agent.update(dispatch_id=dispatched, status='active', launch_confirmation=dispatch_op['result'])
                for request in reg.get('launch_requests', []):
                    if (request.get('task_id') == agent['task_id']
                            and request.get('params', {}).get('function') == row['function']):
                        request['status'] = 'done'
            if not agent.get('dispatch_id'):
                return None
            if not any(a.get('status') in ('unknown', 'launching') for a in reg['agents']):
                reg['fenced'] = False
            launcher.save_registry(attempt, reg)
            return dict(agent)

    def _poll(self, incident, row, launcher):
        attempt = Path(incident['attempt'])
        # Reconcile only recorded effects, never call a missing dispatch a failure.
        issues = operations.reconcile(launcher.journal(attempt), launcher.orca)
        if issues:
            incident['blocker'] = 'unknown operation; reconciliation required: ' + '; '.join(issues)
            return None
        reg = launcher.registry(attempt)
        agent = next((a for a in reg['agents'] if a['id'] == row.get('agent')), None)
        if not agent:
            incident['blocker'] = 'dispatch registration unknown; no replay'
            return None
        data = launcher.observe_agent(agent)
        dispatch = data.get('dispatch') or {}
        if dispatch.get('id') != agent['dispatch_id'] or dispatch.get('taskId') != agent['task_id'] or dispatch.get('runId') != agent['run_id']:
            raise ValueError('observer report dispatch identity mismatch')
        # A local artifact alone is never a completion receipt.
        payload = read_json(attempt / 'reports' / (agent['id'] + '-summary.json'))
        completion = read_json(attempt / 'reports' / (agent['id'] + '-summary.completion.json'), {})
        done = completion.get('worker_done', {})
        if done.get('status') != 'completed':
            if done:
                operations.reconcile(attempt / 'reports' / (agent['id'] + '-summary.completion.json'), launcher.orca)
            return None
        if dispatch.get('status') not in ('completed', 'failed'):
            return None
        if not isinstance(payload, dict):
            raise ValueError('completed service worker lacks report')
        spec_path = attempt / ('stage-' + str(incident['attempts']) + '-' + incident['stage'] + '.md')
        if file_digest(spec_path) != row['spec']:
            raise ValueError('immutable stage task changed')
        args = done.get('args', [])
        for flag, expected in (('--from', agent['terminal']), ('--task-id', agent['task_id']), ('--dispatch-id', agent['dispatch_id'])):
            if flag not in args or args[args.index(flag) + 1] != expected:
                raise ValueError('completion receipt lifecycle mismatch')
        output = Path(row['output'])
        if output.relative_to(attempt).as_posix() not in payload.get('checks', []):
            raise ValueError('service report does not attest output artifact')
        with lock(attempt / 'agents.lock'):
            reg = launcher.registry(attempt)
            registered = next(a for a in reg['agents'] if a['id'] == agent['id'])
            journal = read_json(launcher.journal(attempt), {})
            closed = journal.get(agent['id'] + '-close', {})
            if (closed.get('status') == 'completed'
                    and closed.get('args') == ['terminal', 'close', '--terminal', registered.get('terminal')]
                    and closed.get('evidence', {}).get('incarnation') == registered.get('incarnation')):
                operations.terminal_evidence(closed, launcher.orca)
                registered.update(status='released', terminal_closed=True)
                if not any(a.get('status') in ('unknown', 'launching') for a in reg['agents']):
                    reg['fenced'] = False
                launcher.save_registry(attempt, reg)
            if registered['status'] != 'released':
                launcher.release_agent(registered, attempt,
                                       {'dispatchId': registered['dispatch_id'], 'outcome': payload['outcome']})
                reg = launcher.registry(attempt)
                next(a for a in reg['agents'] if a['id'] == agent['id']).update(status='released')
                launcher.save_registry(attempt, reg)
        row.update(status='completed', outcome=payload['outcome'], output_sha256=file_digest(output),
                   report_sha256=file_digest(attempt / 'reports' / (agent['id'] + '-summary.json')))
        if payload['outcome'] != 'succeeded':
            return {'failed': True, 'summary': payload.get('summary')}
        return read_json(output)

    def _allowed(self, state, path):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError('repair requires canonical existing regular files')
        ctx = self.observer.context(state)
        protected = [self.observer.directory, ctx.config_root, ctx.state_root,
                     ctx.repo / 'feat/infra-observer']
        if any(path.is_relative_to(p.resolve()) for p in protected):
            raise ValueError('observer state/policy/budgets cannot be repaired')
        if path.is_relative_to(ctx.repo / 'src/team_runner'):
            raise ValueError('observer running code requires separate controlled restart')
        for root in state['config']['repair_roots']:
            base = Path(root['path'])
            if path == base or base.is_dir() and path.is_relative_to(base):
                return root
        raise ValueError('file outside infrastructure repair allowlist')

    def _plan(self, state, incident, output):
        if output.get('action') == 'suppress':
            incident.update(status='suppressed', diagnosis=output.get('diagnosis', 'unknown'))
            return
        if (output.get('action') != 'repair' or output.get('scope') not in ('runner', 'test', 'environment', 'dependency', 'tool')
                or not isinstance(output.get('diagnosis'), str) or not output.get('files')
                or not isinstance(output['files'], list) or len(set(output['files'])) != len(output['files'])):
            raise ValueError('invalid diagnosis/repair plan')
        files = []
        for name in output['files']:
            root = self._allowed(state, name)
            if root['kind'] != output['scope']:
                raise ValueError('diagnosed repair kind does not match allowlist')
            files.append({'path': name, 'before': file_digest(name), 'root': root})
        base = Path(incident['attempt']) / 'input-base'
        base.mkdir(exist_ok=True)
        for index, row in enumerate(files):
            snapshot = base / str(index)
            snapshot.write_bytes(Path(row['path']).read_bytes())
            if file_digest(snapshot) != row['before']:
                raise ValueError('source changed while freezing repair input')
            incident['frozen'][str(snapshot.relative_to(Path(incident['attempt'])))] = row['before']
            row['snapshot'] = str(snapshot)
        incident.update(diagnosis=output['diagnosis'], repair_scope=output['scope'],
                        plan={'files': files, 'reason': output.get('reason', 'unknown'),
                              'evidence': digest(incident['evidence']), 'policy': digest(state['config'])})
        if state['config']['mode'] == 'diagnostic':
            incident['status'] = 'suppressed'
        elif state['config']['mode'] == 'approval':
            incident['status'] = 'approval'
        else:
            incident['status'] = 'ready'

    def _approval(self, state, incident):
        approval = incident.get('approval') or {}
        return (approval.get('plan') == digest(incident['plan']) and approval.get('policy') == digest(state['config'])
                and approval.get('evidence') == digest(incident['evidence'])
                and incident['plan']['evidence'] == digest(incident['evidence']))

    def _checks(self, attempt, checks):
        if not isinstance(checks, list) or not checks:
            return False
        for row in checks:
            if not isinstance(row, dict) or row.get('exit_code') != 0 or not isinstance(row.get('argv'), list) or not row['argv']:
                return False
            p = Path(row.get('evidence', ''))
            if p.is_absolute() or '..' in p.parts or not (attempt / p).resolve().is_relative_to(attempt.resolve()) or not (attempt / p).is_file():
                return False
        return True

    def _candidate(self, state, incident, output):
        rows = output.get('files')
        if not isinstance(rows, list) or len(rows) != len(incident['plan']['files']):
            raise ValueError('candidate must exactly match approved file scope')
        attempt = Path(incident['attempt'])
        expected = {r['path']: r for r in incident['plan']['files']}
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'path', 'before', 'after', 'artifact'}:
                raise ValueError('invalid replacement artifact')
            if row['path'] in seen or row['path'] not in expected or row['before'] != expected[row['path']]['before']:
                raise ValueError('candidate input scope/hash mismatch')
            seen.add(row['path'])
            artifact = Path(row['artifact'])
            if artifact.is_absolute() or '..' in artifact.parts or not (attempt / artifact).resolve().is_relative_to(Path(incident['stages']['candidate']['output']).parent.resolve()):
                raise ValueError('replacement must be under attempt/candidate')
            if file_digest(attempt / artifact) != row['after'] or row['after'] == row['before']:
                raise ValueError('candidate replacement hash mismatch or no change')
            self._allowed(state, row['path'])
        incident['candidate'] = {'files': copy.deepcopy(rows), 'explanation': output.get('explanation', 'unknown')}
        incident['candidate_digest'] = digest(incident['candidate'])

    def _users_absent(self, state, incident):
        # All registered workspace agents are inspected, not merely selected projects.
        # The observer's own completed/read-only workers are excluded by exact identity.
        for project_id, project in self.observer.catalog.read()['projects'].items():
            for workspace_id, workspace in project['workspaces'].items():
                if (project_id, workspace_id) == (state['config']['project_id'], state['config']['workspace_id']):
                    continue
                base = self.observer.catalog.state_dir(project_id, workspace_id)
                try:
                    document = read_json(base / 'state.json')
                    if document is None:
                        if (base / 'attempts').exists():
                            return False
                        continue
                    if not isinstance(document, dict):
                        return False
                    current = document.get('current')
                    if current is None:
                        continue
                    if not isinstance(current, dict):
                        return False
                    attempt = current.get('attempt_dir')
                    if not attempt or Path(attempt).resolve().parent != (base / 'attempts').resolve():
                        return False
                    context = self.observer.catalog.context(project_id, workspace_id, allow_disabled=True)
                    snapshot = read_json(Path(attempt) / 'context.json', {})
                    if not isinstance(snapshot, dict) or snapshot.get('identity') != context.identity():
                        return False
                    reg = read_json(Path(attempt) / 'agents.json')
                    if (not isinstance(reg, dict) or not isinstance(reg.get('agents'), list)
                            or any(not isinstance(a, dict) or a.get('status') not in ('released', 'stopped', 'superseded', 'active', 'waiting_child', 'launching', 'unknown', 'ready') for a in reg['agents'])):
                        return False
                except (OSError, ValueError, TypeError):
                    return False
                related = any(Path(r['path']).is_relative_to(Path(workspace['path'])) for r in incident['candidate']['files'])
                shared = any(r['root']['shared'] for r in incident['plan']['files'])
                if (related or shared) and any(a.get('status') not in ('released', 'stopped', 'superseded') for a in reg.get('agents', [])):
                    return False
        return True

    def _apply(self, state, incident):
        from . import heavy_resource
        if not self._permitted(state) or state['config']['mode'] == 'diagnostic':
            return
        if state['config']['mode'] == 'approval' and not self._approval(state, incident):
            incident['status'] = 'approval'
            return
        if incident['plan']['evidence'] != digest(incident['evidence']):
            incident.pop('approval', None)
            incident.update(status='detected', new_evidence=True)
            return
        if digest(incident['candidate']) != incident.get('candidate_digest'):
            raise ValueError('candidate identity changed')
        review = incident.get('review', {})
        if review.get('safe') is not True or review.get('users_absent') is not True or not review.get('users_evidence'):
            incident.update(status='approval', blocker='application safety or affected users unknown')
            return
        if not self._users_absent(state, incident):
            incident.update(status='waiting_safe_point', blocker='affected registered agents active or unknown')
            return
        attempt = Path(incident['attempt'])
        application = incident.get('application')
        if application:
            # A crash after replacement is reconciled from all exact outputs;
            # a partial/unknown application is never automatically repeated.
            observed = [{'path': row['path'], 'sha256': file_digest(row['path'])} for row in incident['candidate']['files']]
            if all(r['sha256'] == expected['after'] for r, expected in zip(observed, incident['candidate']['files'])):
                application.update(status='confirmed', evidence=observed)
                incident.update(stage='result', status='checking_result')
            else:
                incident.update(status='blocked', blocker='application outcome unknown or partial; no replay')
            return
        with ExitStack() as stack:
            held = set()
            for row in incident['plan']['files']:
                self._allowed(state, row['path'])
                # Share the existing checkout ownership lock with ordinary Runner.
                for project_id, project in self.observer.catalog.read()['projects'].items():
                    for workspace_id, workspace in project['workspaces'].items():
                        if Path(row['path']).is_relative_to(Path(workspace['path'])):
                            context = self.observer.catalog.context(project_id, workspace_id, allow_disabled=True)
                            key = str(context.checkout_lock)
                            if key not in held:
                                held.add(key)
                                stack.enter_context(lock(context.checkout_lock, blocking=False))
            if any(r['root']['shared'] for r in incident['plan']['files']):
                if not heavy_resource.available():
                    incident.update(status='waiting_safe_point', blocker='shared heavy resource busy or cleanup unknown')
                    return
                stack.enter_context(heavy_resource.acquire())
            with lock(self.observer.directory / 'control.lock'):
                fresh = self.observer.load()
                if not self._permitted(fresh) or digest(fresh['config']) != digest(state['config']):
                    return
                if not self._users_absent(fresh, incident):
                    incident.update(status='waiting_safe_point', blocker='users changed before apply')
                    return
                for row in incident['candidate']['files']:
                    if file_digest(row['path']) != row['before'] or file_digest(attempt / row['artifact']) != row['after']:
                        incident.pop('approval', None)
                        incident.update(status='blocked', blocker='inputs or candidate changed before apply')
                        return
                incident['application'] = {'status': 'applying', 'candidate': digest(incident['candidate']), 'at': self.observer.clock()}
                fresh['incidents'][incident['id']] = incident
                self._save(fresh)  # Intent before effects; interruption never replays.
                for row in incident['candidate']['files']:
                    target = Path(row['path'])
                    fd, temp = tempfile.mkstemp(prefix='.' + target.name, dir=target.parent)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write((attempt / row['artifact']).read_bytes()); stream.flush(); os.fsync(stream.fileno())
                        os.chmod(temp, target.stat().st_mode & 0o777)
                        os.replace(temp, target)
                        directory = os.open(target.parent, os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    finally:
                        if os.path.exists(temp):
                            os.unlink(temp)
                incident['application'].update(status='confirmed', evidence=[{'path': r['path'], 'sha256': file_digest(r['path'])} for r in incident['candidate']['files']])
                incident.update(stage='result', status='checking_result', blocker=None)
                fresh['incidents'][incident['id']] = incident
                self._save(fresh)

    def advance(self):
        observed = self.observer.load()
        if observed['config']:
            context = self.observer.context(observed)
            current = read_json(context.state_dir / 'state.json', {}).get('current') or {}
            previous = observed['incidents'].get(current.get('incident'))
            if current.get('attempt_dir') and previous and (previous['status'] in ('closed', 'suppressed', 'blocked') or observed['desired'] == 'stopped'
                    and previous.get('stage') != 'result'
                    and not any(r.get('status') in ('running', 'dispatching') for r in previous.get('stages', {}).values())):
                if not self._cleanup(context, Path(current['attempt_dir'])):
                    if previous['status'] in ('closed', 'suppressed', 'blocked'):
                        previous['blocker'] = 'service cleanup unknown; no new attempt'
                        self._save_merged(previous)
                    with lock(self.observer.directory / 'control.lock'):
                        latest = self.observer.load(); latest['cleanup_pending'] = True; self._save(latest)
                    return
                with lock(self.observer.directory / 'control.lock'):
                    latest = self.observer.load(); latest['cleanup_pending'] = False; self._save(latest)
                if observed['desired'] == 'stopped' and previous['status'] not in ('closed', 'suppressed', 'blocked'):
                    previous['new_evidence'] = True
                    self._save_merged(previous)
        with lock(self.observer.directory / 'control.lock'):
            state = self.observer.load()
            if not state['config']:
                return
            active = state['incidents'].get(state['active']) if state['active'] else None
            if active and active['status'] in ('closed', 'suppressed', 'blocked'):
                state['active'] = None
                active = None
            if not active:
                if not self._permitted(state):
                    self._save(state); return
                active = next((i for i in state['incidents'].values() if i['status'] in ('detected', 'ready')), None)
                if not active:
                    self._save(state); return
                if 'attempt' not in active:
                    self._snapshot(state, active)
                else:
                    state['active'] = active['id']
            self._save(state)
        context = self.observer.context(state)
        launcher = self.launcher or ObserverLauncher(context, self.observer)
        launcher.stage = active.get('stage')
        incident = copy.deepcopy(active)
        try:
            if not self._intact(incident):
                raise ValueError('immutable observer snapshots changed')
            if incident.get('application', {}).get('status') == 'applying':
                # Reconciliation is mandatory even while paused/stopped/downgraded.
                observed = [file_digest(r['path']) for r in incident['candidate']['files']]
                if observed == [r['after'] for r in incident['candidate']['files']]:
                    incident['application']['status'] = 'confirmed'
                    incident.update(stage='result', status='checking_result')
                else:
                    incident.update(status='blocked', blocker='application outcome unknown or partial; no replay')
                    self._save_merged(incident); return
            if incident.get('new_evidence') and not any(r.get('status') in ('running', 'dispatching') for r in incident['stages'].values()):
                incident.pop('new_evidence', None)
                incident.pop('approval', None)
                incident.pop('plan', None)
                incident.pop('attempt', None)
                incident.update(status='detected')
                with lock(self.observer.directory / 'control.lock'):
                    latest = self.observer.load()
                    latest['incidents'][incident['id']] = incident
                    self._snapshot(latest, incident)
                    self._save(latest)
                return
            if incident['status'] in ('ready', 'waiting_safe_point'):
                if state['config']['mode'] == 'diagnostic' or not self._permitted(state):
                    return
                if state['config']['mode'] == 'approval' and not self._approval(state, incident):
                    incident['status'] = 'approval'; self._save_merged(incident); return
                if incident.get('review'):
                    self._apply(state, incident)
                    self._save_merged(incident); return
                if incident['attempts'] >= 2:
                    incident.update(status='blocked', blocker='repair attempt budget exhausted')
                    self._save_merged(incident); return
                incident['attempts'] += 1
                incident.update(stage='candidate', status='preparing_candidate')
                self._save_merged(incident)
            stage = incident['stage']
            row = incident['stages'].get(stage)
            if not row:
                if not self._permitted(self.observer.load()) and stage != 'result':
                    return
                self._dispatch(state, incident, stage, launcher)
                return
            if row['status'] == 'dispatching':
                # Recover exact registered launch; never dispatch a second request.
                restored = self._restore_dispatch(incident, row, launcher)
                if restored:
                    row.update(status='running', agent=restored['id'], dispatch=restored['dispatch_id'])
                else:
                    incident['blocker'] = 'dispatch outcome unknown; explicit reconciliation required'
                    self._save_merged(incident); return
            output = self._poll(incident, row, launcher)
            if output is None:
                self._save_merged(incident); return
            if output.get('failed'):
                incident['results'].append({'stage': stage, 'result': output, 'at': self.observer.clock()})
                if stage in ('candidate', 'review') and incident['attempts'] < 2:
                    incident.setdefault('stage_history', []).append(copy.deepcopy(incident['stages']))
                    incident['stages'].pop('candidate', None); incident['stages'].pop('review', None)
                    incident.pop('candidate', None); incident.pop('review', None)
                    incident.update(status='ready', stage='candidate')
                else:
                    incident.update(status='blocked', blocker='independent stage failed; repair not accepted')
            elif incident.get('new_evidence'):
                pass  # Settle exact worker before starting a fresh diagnosis.
            elif stage == 'diagnose':
                self._plan(state, incident, output)
            elif stage == 'candidate':
                self._candidate(state, incident, output)
                incident.update(stage='review', status='reviewing')
            elif stage == 'review':
                if output.get('verdict') != 'PASS' or output.get('candidate') != digest(incident['candidate']) or not self._checks(Path(incident['attempt']), output.get('checks')):
                    incident.update(status='blocked', blocker='independent candidate review did not pass')
                else:
                    incident['review'] = output
                    incident.update(status='ready')
            elif stage == 'result':
                current_outputs = all(file_digest(r['path']) == r['after'] and file_digest(Path(incident['attempt']) / r['artifact']) == r['after'] for r in incident['candidate']['files'])
                correct = (current_outputs and output.get('verdict') == 'PASS' and output.get('candidate') == digest(incident['candidate'])
                           and output.get('correctness') is True and bool(output.get('metric'))
                           and self._checks(Path(incident['attempt']), output.get('checks'))
                           and (output.get('fault_removed') is True or
                                type(output.get('before')) in (int, float) and type(output.get('after')) in (int, float)
                                and output['after'] < output['before']))
                incident['results'].append({'stage': 'result', 'result': output, 'at': self.observer.clock()})
                incident.update(status='closed' if correct else 'blocked',
                                blocker=None if correct else 'correctness/improvement unconfirmed; repair not successful')
            self._save_merged(incident)
        except memory_admission.WaitingMemory:
            return
        except (OSError, ValueError, RuntimeError) as exc:
            incident['blocker'] = str(exc)
            if isinstance(exc, ValueError):
                incident['status'] = 'blocked'
            self._save_merged(incident)
