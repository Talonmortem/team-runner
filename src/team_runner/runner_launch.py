import json

import os

from pathlib import Path

import shlex

import subprocess

import sys

import uuid

import hashlib

import datetime as dt

from . import runner_operations as operations

from .runner_store import atomic_json, lock, read_json


from .orca import call, executable
from . import runner_config
class Launcher:
    def __init__(self, context):
        self.context = context

    def orca(self, *args, **kwargs):
        return call(*args, context=self.context, **kwargs)


    def argv(self, config, state_dir, runtime=None):
        runner_config.validate_role_scope(config, self.context.repo)
        runner_config.validate_fast(config)
        if config['agent'] == 'opencode2':
            from .runner_cloud import overlay
            if config['provider'] != 'cloudru' or config['profile']:
                raise ValueError('OpenCode 2 requires cloudru and empty profile')
            if config['model'] not in runner_config.opencode_connection(self.context.config_root.parent)['models']:
                raise ValueError('unknown Cloud.ru model')
            return ['env', 'OPENCODE_CONFIG_CONTENT=' + json.dumps(overlay(config)),
                    sys.executable, '-m', 'team_runner.runner_cloud', '--standalone']
        if config['agent'] == 'codex':
            from .agent_runtime import validate
            cache = validate(runtime or {})['go_cache']
            result = ['codex', '-s', 'workspace-write', '-a', 'on-request']
            if config['profile']:
                result += ['--profile', config['profile']]
            if config['provider']:
                result += ['-c', 'model_provider=' + json.dumps(config['provider'])]
            result += ['--model', config['model'], '-c', 'model_reasoning_effort=' + json.dumps(config['effort']),
                       '-c', 'check_for_update_on_startup=false', '-c', 'features.multi_agent=false',
                       '-c', 'sandbox_workspace_write.network_access=true', '-c',
                       'sandbox_workspace_write.writable_roots=' + json.dumps(
                           [str(Path.home() / '.config/orca'), str(Path(state_dir).resolve()), '/tmp'] +
                           ([cache] if cache else []))]
            if 'fast' in config:
                result += ['-c', 'service_tier=' + json.dumps('priority' if config['fast'] else 'default')]
            return result
        if config['agent'] != 'claude':
            raise ValueError('unsupported runtime')
        result = ['claude', '--model', config['model'], '--effort', config['effort']]
        if config['profile']:
            result += ['--settings', str(Path(config['profile']).expanduser())]
        if 'fast' in config:
            result += ['--fast-mode', 'on' if config['fast'] else 'off']
        return result


    def check_operations(self, attempt):
        if any(op.get('status') not in ('completed', 'rejected')
               for op in read_json(self.journal(attempt), {}).values()):
            raise RuntimeError('operation outcome unknown; reconcile first')


    def check_launch_recovery(self, reg):
        if any(a.get('status') == 'restart_ready'
               or (a.get('replacement_of') and a.get('status') == 'ready')
               for a in reg['agents']):
            raise RuntimeError('terminal recovery pending; supervised continuation required')


    def preflight(self, cfg):
        if cfg['agent'] == 'opencode2':
            from .runner_cloud import cloud_key, overlay
            overlay(cfg)
            cloud_key()


    def validate_variant(self, variant, reason, reuse):
        if variant not in ('primary', 'reserve'):
            raise ValueError('unknown launch variant')
        if variant == 'reserve':
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('reserve requires a reason')
            if reuse:
                raise ValueError('reserve requires a fresh session')


    def reserve_guard(self, reg, role, function):
        for previous in reg['agents']:
            previous_role = previous.get('role', '')
            same_executor = role.startswith('implementer_') and previous_role.startswith('implementer_')
            if (previous_role != role and not same_executor) or (function and previous.get('function') and previous.get('function') != function):
                continue
            if previous.get('status') in ('released', 'stopped', 'superseded'):
                continue
            if not previous.get('dispatch_id'):
                raise RuntimeError('previous executor unconfirmed; reconcile first')
            data = self.observe_agent(previous)
            if data.get('dispatch', {}).get('status') != 'completed':
                raise RuntimeError('previous executor must complete or stop before replacement')
            if data.get('worker', {}).get('state') in ('outcome_unknown', 'failed', 'starting'):
                raise RuntimeError('previous executor outcome unknown; reconcile first')


    def registry_path(self, attempt):
        return Path(attempt) / 'agents.json'

    def sequential_guard(self, attempt, reg, role, task_id=None):
        """The coordinator stays alive; only one child may execute at a time.

        Called under agents.lock, including immediately before registration.
        Read immutable policy, never the mutable workspace profile.
        """
        policy = read_json(Path(attempt) / 'context.json', {}).get('profile', {}).get('workflow', {})
        if role == 'coordinator' or not policy.get('sequential_agents', False):
            return
        for agent in reg['agents']:
            if agent.get('role') == 'coordinator' or (task_id and agent.get('task_id') == task_id):
                continue
            if agent.get('status') in ('released', 'stopped', 'superseded'):
                continue
            if not agent.get('dispatch_id'):
                raise RuntimeError('sequential execution: previous worker unconfirmed; reconcile first')
            observed = self.observe_agent(agent)
            if (observed.get('dispatch', {}).get('status') != 'completed'
                    or observed.get('worker', {}).get('state') in ('outcome_unknown', 'starting')):
                raise RuntimeError('sequential execution: previous worker must complete or stop first')


    def registry(self, attempt):
        return read_json(self.registry_path(attempt), {'fenced': False, 'agents': [], 'runs': []})


    def save_registry(self, attempt, value):
        atomic_json(self.registry_path(attempt), value)


    def journal(self, attempt):
        return Path(attempt) / 'operations.json'


    def effect(self, attempt, key, *args, mailbox=None, evidence=None):
        return operations.mutation(self.journal(attempt), key, self.orca, *args, mailbox=mailbox, evidence=evidence)


    def connection_stamp(self, cfg):
        """Compare connection inputs without persisting credentials or connection contents."""
        home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        claude_home = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
        paths = ([home / 'config.toml', home / 'auth.json', home / (cfg['profile'] + '.config.toml')] if cfg['agent'] == 'codex'
                 else [claude_home / 'settings.json', claude_home / '.credentials.json',
                       Path(cfg['profile']).expanduser()] if cfg['profile'] else
                 [claude_home / 'settings.json', claude_home / '.credentials.json'])
        if cfg['agent'] == 'opencode2':
            config_dir = runner_config.opencode_config_path(self.context.config_root.parent).parent
            data_dir = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'opencode'
            paths = [config_dir / name for name in ('opencode.json', 'opencode.jsonc', 'config.json')]
            paths.append(data_dir / 'auth.json')
            from .runner_cloud import key_path
            paths.append(key_path())
            for parent in (self.context.repo, *self.context.repo.parents):
                paths.extend(parent / name for name in ('opencode.json', 'opencode.jsonc',
                                                       '.opencode/opencode.json', '.opencode/opencode.jsonc'))
            if os.environ.get('OPENCODE_CONFIG'):
                paths.append(Path(os.environ['OPENCODE_CONFIG']).expanduser())
            if os.environ.get('OPENCODE_CONFIG_DIR'):
                paths.extend(Path(os.environ['OPENCODE_CONFIG_DIR']).expanduser() / name
                             for name in ('opencode.json', 'opencode.jsonc'))
        digest = hashlib.sha256()
        inputs = []
        for path in paths:
            digest.update(str(path).encode())
            content = path.read_bytes() if path.is_file() else b'<absent>'
            digest.update(content)
            inputs.append(content)
        for name, value in sorted(os.environ.items()):
            # OpenCode can reference arbitrary environment names in provider env
            # lists or {env:NAME} values, including JSONC sources. Conservative
            # name matching avoids publishing or interpreting credential values.
            referenced = cfg['agent'] == 'opencode2' and any(name.encode() in content for content in inputs)
            if referenced or name.startswith(('ANTHROPIC_', 'CLAUDE_', 'OPENAI_', 'CODEX_', 'OPENCODE_', 'CLOUDRU_', 'CLOUD_RU_', 'ORCA_ENVIRONMENT', 'ORCA_PAIRING_CODE')):
                digest.update((name + '=' + value).encode())
        return digest.hexdigest()


    def create_terminal(self, attempt, key, command, title, mailbox=None, transport=None):
        # Stable nonce/path are persisted before terminal creation; never retry create.
        marker = Path(attempt) / (key + '.entry.json')
        nonce = key + ':' + str(Path(attempt).resolve())
        wrapper = ['env', *[k + '=' + v for k, v in self.context.environment().items()], sys.executable, '-m', 'team_runner.runner_entry', str(marker), nonce, '--', *command]
        self.context.verify(transport or self.orca)
        return operations.mutation(self.journal(attempt), key, transport or self.orca, 'terminal', 'create', '--worktree', 'id:' + self.context.orca_id, '--title', title,
                      '--command', shlex.join(wrapper), mailbox=mailbox,
                      evidence={'marker': str(marker), 'nonce': nonce})['terminal']


    def verify_terminal(self, record, data):
        terminal = data.get('terminal') or {}
        resource = data.get('terminalResource') or {}
        errors = []
        if not record.get('terminal_owned') or not record.get('incarnation'):
            errors.append('recorded ownership/incarnation missing')
        if not terminal:
            errors.append('Orca returned no terminal')
        for field, actual, expected in (
                ('handle', terminal.get('handle'), record.get('terminal')),
                ('incarnation', terminal.get('incarnationId'), record.get('incarnation')),
                ('worktree', terminal.get('worktreeId'), self.context.orca_id),
                ('ownerDispatchId', resource.get('ownerDispatchId'), record.get('dispatch_id'))):
            if actual != expected:
                errors.append(f'{field}: expected {expected!r}, observed {actual!r}')
        if errors:
            raise RuntimeError('registered terminal identity/owner mismatch or legacy identity missing: ' + '; '.join(errors))


    def reuse_source(self, reg, role, cfg, function, reuse, context_percent):
        if role not in ('implementer_standard', 'implementer_medium', 'implementer_complex', 'tester', 'tester_complex'):
            raise ValueError('role requires a fresh session')
        if not function or not isinstance(context_percent, (int, float)) or not 0 <= context_percent <= 50:
            raise ValueError('reuse requires function and known coordinator context reading <= 50%')
        old = next((a for a in reg['agents'] if a.get('dispatch_id') == reuse), None)
        if not old or old.get('status') != 'active' or old.get('superseded_by'):
            raise ValueError('reuse requires the latest live session in this attempt')
        if old.get('role') != role or old.get('function') != function:
            raise ValueError('reuse role/function mismatch')
        if old.get('requested') != cfg or old.get('connection_stamp') != self.connection_stamp(cfg):
            raise ValueError('reuse settings/connection mismatch')
        data = self.observe_agent(old)
        if data.get('dispatch', {}).get('status') != 'completed' or self.liveness(reuse, old.get('mailbox')) != 'live':
            raise ValueError('reuse requires completed Dispatch and confirmed live terminal')
        self.verify_terminal(old, data)
        return old


    def _launch(self, attempt, reg, record, title, state_dir, retry_of=None, permitted=None):
        self.sequential_guard(attempt, reg, record['role'], record['task_id'])
        cfg = record['requested']
        record.setdefault('launch_title', title)
        record.setdefault('retry_of', retry_of)
        self.save_registry(attempt, reg)
        runner_config.validate_role_scope(cfg, self.context.repo)
        try:
            if not record.get('terminal'):
                runtime = self.prepare_runtime(attempt)
                env = self.context.environment(attempt)
                if runtime['go_cache']:
                    env['GOCACHE'] = runtime['go_cache']
                command = ['env', *[k + '=' + v for k, v in env.items()], *self.argv(cfg, state_dir, runtime)]
                terminal = self.create_terminal(attempt, record['id'] + '-terminal', command, title, record.get('mailbox'))
                record.update(terminal=terminal['handle'], terminal_owned=True, incarnation=terminal.get('incarnationId'))
                self.save_registry(attempt, reg)
            wait = self.orca('terminal', 'wait', '--terminal', record['terminal'], '--for', 'tui-idle',
                        '--timeout-ms', '60000', timeout=70, mailbox=record.get('mailbox'))
            terminal = self.orca('terminal', 'show', '--terminal', record['terminal'], mailbox=record.get('mailbox')).get('terminal') or {}
            if not record.get('incarnation') or terminal.get('handle') != record['terminal'] or terminal.get('incarnationId') != record['incarnation'] or terminal.get('worktreeId') != self.context.orca_id:
                raise RuntimeError('terminal identity changed before Dispatch; refusing injection')
            if cfg['agent'] == 'codex' and not record.get('reuse'):
                screen = self.orca('terminal', 'read', '--terminal', record['terminal'], '--screen',
                                   mailbox=record.get('mailbox')).get('terminal') or {}
                # Stream fallback includes old output; it cannot establish startup health.
                if screen.get('source') != 'screen':
                    raise RuntimeError('Codex startup screen unavailable; refusing injection')
                if 'account/read failed' in ' '.join(screen.get('tail', [])).lower():
                    raise RuntimeError('Codex startup: account/read failed')
            if not wait.get('wait', wait).get('satisfied'):
                raise RuntimeError('agent startup not confirmed')
            if permitted is not None and not permitted():
                raise RuntimeError('launch continuation cancelled by user intent')
            options = ['--retry-of', retry_of] if retry_of else []
            res = self.effect(attempt, record['id'] + '-dispatch', 'orchestration', 'worker-start',
                         '--task', record['task_id'], '--run', record['run_id'], '--terminal', record['terminal'],
                         '--worktree', 'id:' + self.context.orca_id, '--timeout-ms', '60000', *options, mailbox=record.get('mailbox'))
            record.update(dispatch_id=res['dispatchId'], status='active', launch_confirmation=res)
            if record.get('reuse'):
                old = next(a for a in reg['agents'] if a.get('dispatch_id') == record['reuse'])
                old.update(status='superseded', superseded_by=record['dispatch_id'])
            self.save_registry(attempt, reg)
            return dict(record)
        except Exception as exc:
            record.update(status='unknown', error=str(exc))
            reg['fenced'] = True
            self.save_registry(attempt, reg)
            raise


    def launch(self, attempt, role, task_id, run_id, title, state_dir, mailbox=None, retry_of=None,
               function=None, reuse=None, context_percent=None, keep_session=False, variant='primary', reason=None):
        attempt = Path(attempt)
        self.context.check_attempt(attempt)
        from .runner_config import snapshot_role
        self.validate_variant(variant, reason, reuse)
        cfg = snapshot_role(attempt, role, variant)
        runner_config.validate_role_scope(cfg, self.context.repo)
        self.preflight(cfg)
        with lock(attempt / 'agents.lock'):
            reg = self.registry(attempt)
            if reg['fenced'] or any(a['status'] in ('launching', 'unknown') for a in reg['agents']):
                raise RuntimeError('attempt fenced or launch unconfirmed; reconcile first')
            self.check_operations(attempt)
            existing = next((a for a in reg['agents'] if a.get('task_id') == task_id and a.get('status') in ('ready', 'active')), None)
            if existing:
                if (existing['role'] != role or existing['requested'] != cfg
                        or existing.get('variant', 'primary') != variant or existing.get('reason') != reason
                        or existing.get('connection_stamp') != self.connection_stamp(cfg)):
                    raise ValueError('existing task launch settings mismatch')
                if existing['status'] == 'active':
                    return dict(existing)
                self.check_launch_recovery(reg)
                return self._launch(attempt, reg, existing, title, state_dir, retry_of)
            self.check_launch_recovery(reg)
            self.sequential_guard(attempt, reg, role, task_id)
            if variant == 'reserve' or (role.startswith('implementer_') and not reuse):
                self.reserve_guard(reg, role, function)
            old = self.reuse_source(reg, role, cfg, function, reuse, context_percent) if reuse else None
            record = dict(id=str(uuid.uuid4()), role=role, requested=cfg, observed=None,
                          variant=variant, reason=reason,
                          task_id=task_id, run_id=run_id, mailbox=mailbox, status='launching',
                          attempt=str(attempt), function=function, keep_session=keep_session, connection_stamp=self.connection_stamp(cfg),
                          reuse=reuse, context_reading={'percent': context_percent, 'source': 'coordinator',
                                           'reported_by': os.environ.get('ORCA_TERMINAL_HANDLE'),
                                           'at': dt.datetime.now(dt.timezone.utc).isoformat()} if reuse else None)
            if old:
                record.update({k: old[k] for k in ('terminal', 'terminal_owned', 'incarnation')})
            reg['agents'].append(record)
            self.save_registry(attempt, reg)
            return self._launch(attempt, reg, record, title, state_dir, retry_of)


    def rework_coordinator(self, attempt, dispatch_id, task_id, run_id, state_dir):
        """A new acceptance Dispatch in the same owned coordinator session/attempt."""
        attempt = Path(attempt)
        self.context.check_attempt(attempt)
        runner_config.validate_role_scope(runner_config.snapshot_role(attempt, 'coordinator'), self.context.repo)
        with lock(attempt / 'agents.lock'):
            reg = self.registry(attempt)
            if reg['fenced'] or any(a['status'] in ('launching', 'unknown') for a in reg['agents']):
                raise RuntimeError('attempt fenced or launch unconfirmed; reconcile first')
            self.check_operations(attempt)
            existing = next((a for a in reg['agents'] if a.get('task_id') == task_id), None)
            if existing:
                if (existing.get('role') != 'coordinator' or existing.get('reuse') != dispatch_id
                        or existing.get('status') != 'active'):
                    raise RuntimeError('acceptance Dispatch unconfirmed; reconcile first')
                return dict(existing)
            old = next((a for a in reg['agents'] if a.get('dispatch_id') == dispatch_id), None)
            cfg = read_json(attempt / 'config.json')['roles']['coordinator']
            if (not old or old.get('role') != 'coordinator' or old.get('status') != 'active'
                    or old.get('superseded_by') or old.get('requested') != cfg
                    or old.get('connection_stamp') != self.connection_stamp(cfg)
                    or old.get('run_id') != run_id):
                raise RuntimeError('coordinator session/settings changed; explicit retry required')
            data = self.observe_agent(old)
            if (data.get('dispatch', {}).get('status') != 'completed'
                    or self.liveness(dispatch_id, old.get('mailbox')) != 'live'):
                raise RuntimeError('coordinator completion/live session unconfirmed')
            self.verify_terminal(old, data)
            record = dict(id=str(uuid.uuid4()), role='coordinator', requested=cfg, observed=None,
                          task_id=task_id, run_id=run_id, mailbox=old.get('mailbox'), status='launching',
                          attempt=str(attempt), reuse=dispatch_id, acceptance_rework=True,
                          connection_stamp=old['connection_stamp'])
            record.update({k: old[k] for k in ('terminal', 'terminal_owned', 'incarnation')})
            reg['agents'].append(record)
            self.save_registry(attempt, reg)
            return self._launch(attempt, reg, record, 'acceptance correction', state_dir)


    def child_run(self, attempt):
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.registry(attempt)
            if reg['fenced']:
                raise RuntimeError('attempt fenced: new runs forbidden')
            if reg['runs']:
                run = reg['runs'][0]
                if run.get('status') == 'ready':
                    return run
                if run.get('status') != 'mailbox_ready':
                    raise RuntimeError('child run creation unconfirmed')
            else:
                run = {'status': 'creating'}
                reg['runs'].append(run)
                self.save_registry(attempt, reg)
            try:
                if not run.get('mailbox'):
                    terminal = self.create_terminal(attempt, 'mailbox', ['cat'], 'goal runner mailbox')
                    run.update(mailbox=terminal['handle'], incarnation=terminal.get('incarnationId'))
                    self.save_registry(attempt, reg)
                r = self.effect(attempt, 'child-run', 'orchestration', 'run-create', '--objective',
                           'goal-runner child ' + Path(attempt).name, mailbox=run['mailbox'])['run']
                run.update(run_id=r['id'], status='ready')
                self.save_registry(attempt, reg)
                return dict(run)
            except Exception:
                reg['fenced'] = True
                self.save_registry(attempt, reg)
                raise


    def prepare_runtime(self, attempt):
        from .agent_runtime import prepare
        snapshot = read_json(Path(attempt) / 'context.json', {})
        return prepare(snapshot.get('profile', {}).get('runtime', {}))

    def dispatch_role(self, attempt, role, spec, title, state_dir, function=None, reuse=None, context_percent=None, keep_session=False, variant='primary', reason=None):
        self.context.check_attempt(attempt)
        self.prepare_runtime(attempt)
        if role == 'coordinator':
            raise ValueError('coordinator is launched by run')
        if keep_session and role not in ('implementer_standard', 'implementer_medium', 'implementer_complex', 'tester', 'tester_complex'):
            raise ValueError('only reusable roles support --keep-session')
        from .runner_config import snapshot_role
        self.validate_variant(variant, reason, reuse)
        cfg = snapshot_role(attempt, role, variant)
        runner_config.validate_role_scope(cfg, self.context.repo)
        self.preflight(cfg)
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.registry(attempt)
            if reg['fenced'] or any(a.get('status') in ('launching', 'unknown') for a in reg['agents']):
                raise RuntimeError('attempt fenced or launch unconfirmed; reconcile first')
            self.check_operations(attempt)
            self.check_launch_recovery(reg)
            self.sequential_guard(attempt, reg, role)
            if variant == 'reserve' or (role.startswith('implementer_') and not reuse):
                self.reserve_guard(reg, role, function)
        run = self.child_run(attempt)
        spec = ('Role: ' + role + '. Variant: ' + variant + '. Reason: ' + (reason or '') + '. Launch settings (authoritative): ' + json.dumps(cfg) +
                '\nUse this snapshot instead of static model defaults. No nested agents.\n'
                'Transport: arbitrary message text only via python3 -m team_runner.orca_msg send --body-file FILE '
                '(or quoted heredoc stdin); never interpolate text into shell --body. '
                'Completion: use python3 -m team_runner.worker_report --input FILE --from HANDLE '
                '--task-id TASK --dispatch-id DISPATCH with exact injected lifecycle IDs. '
                'Run --help for the small JSON schema. This generates report/check/file artifacts and sends worker_done '
                'once; no additional narrative report is needed. Link detailed evidence instead of copying it.\n\n' + spec)
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.registry(attempt)
            if reg['fenced']:
                raise RuntimeError('attempt fenced')
            self.check_launch_recovery(reg)
            if reuse:
                self.reuse_source(reg, role, cfg, function, reuse, context_percent)
            params = dict(role=role, spec=spec, title=title, function=function, reuse=reuse, context_percent=context_percent, keep_session=keep_session, variant=variant, reason=reason)
            pending = [r for r in reg.get('launch_requests', []) if r['status'] != 'done']
            if pending and pending[0]['params'] != params:
                raise RuntimeError('unfinished role launch has different parameters; reconcile first')
            request = pending[0] if pending else {'id': str(uuid.uuid4()), 'params': params, 'status': 'creating'}
            if not pending:
                reg.setdefault('launch_requests', []).append(request)
            self.save_registry(attempt, reg)
            try:
                task = self.effect(attempt, 'task-' + request['id'], 'orchestration', 'task-create',
                              '--run', run['run_id'], '--task-title', title, '--spec', spec, mailbox=run['mailbox'])['task']
                request.update(task_id=task['id'], status='ready')
                self.save_registry(attempt, reg)
            except Exception:
                reg['fenced'] = True
                self.save_registry(attempt, reg)
                raise
        record = self.launch(attempt, role, task['id'], run['run_id'], title, state_dir, run['mailbox'],
                        function=function, reuse=reuse, context_percent=context_percent, keep_session=keep_session, variant=variant, reason=reason)
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.registry(attempt)
            next(r for r in reg['launch_requests'] if r['id'] == request['id'])['status'] = 'done'
            self.save_registry(attempt, reg)
        return record


    def liveness(self, dispatch_id, mailbox=None):
        try:
            data = self.orca('orchestration', 'worker-show', '--dispatch', dispatch_id, mailbox=mailbox)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return 'unknown'
        # Never infer liveness from an arbitrary string elsewhere in the receipt.
        projection = data.get('projection', {}).get('liveness')
        if isinstance(projection, dict):
            projection = projection.get('verdict')
        return projection or data.get('observation', {}).get('status', 'unknown')


    def observe_agent(self, record):
        data = self.orca('orchestration', 'worker-show', '--dispatch', record['dispatch_id'],
                    mailbox=record.get('mailbox'))
        provider = data.get('projection', {}).get('provider') or {}
        # Projection names the CLI as provider.id; it is NOT Codex's model_provider.
        effective = ((data.get('worker') or {}).get('startOptions') or {}).get('launch', {}).get('effective') or {}
        record['runtime_state'] = data.get('worker', {}).get('state')
        record['observed'] = {'agent': provider.get('id'), 'model': provider.get('model'),
                              'provider': None, 'effort': effective.get('effort'), 'source': 'Orca worker-show'}
        return data


    def restore_terminal_panel(self, attempt, record, data):
        """Restore a detached live panel once; never restart or replay an agent.

        UI effects have a separate durable journal so an uncertain focus change
        cannot block worker lifecycle operations. Unknown switches are not retried.
        Caller holds agents.lock, as for the lifecycle journal.
        """
        if (record.get('status') != 'active' or record.get('superseded_by')
                or not record.get('terminal_owned') or not record.get('incarnation')
                or not record.get('terminal') or not record.get('dispatch_id')):
            return
        live = data.get('projection', {}).get('liveness')
        if isinstance(live, dict):
            live = live.get('verdict')
        if (live or data.get('observation', {}).get('status')) != 'live':
            return
        self.verify_terminal(record, data)

        def show():
            observed = self.orca('terminal', 'show', '--terminal', record['terminal'],
                                 mailbox=record.get('mailbox'))
            self.verify_terminal(record, {**data, 'terminal': observed.get('terminal')})
            return observed['terminal']

        terminal = show()
        record['panel_status'] = 'unknown'
        if terminal.get('orphaned') is False:
            record['panel_status'] = 'visible'
            record.pop('panel_error', None)
            return
        if terminal.get('orphaned') is not True:
            return
        record['panel_status'] = 'orphaned'
        if (terminal.get('connected') is not True or terminal.get('writable') is not True
                or terminal.get('exitCause')):
            return
        journal = Path(attempt) / 'panel-operations.json'
        key = record['id'] + '-panel-switch'
        if key in read_json(journal, {}):
            raise RuntimeError('terminal panel restoration already attempted; restore panel manually')
        operations.mutation(journal, key,
                            self.orca, 'terminal', 'switch', '--terminal', record['terminal'],
                            mailbox=record.get('mailbox'),
                            evidence={'terminal': record['terminal'], 'incarnation': record['incarnation'],
                                      'worktree': self.context.orca_id})
        if show().get('orphaned') is not False:
            raise RuntimeError('terminal panel restoration unconfirmed; automatic switch will not repeat')
        record['panel_status'] = 'visible'
        record.pop('panel_error', None)


    def close_registered_terminal(self, record, data):
        """Custom argv terminals are external to Orca, but owned by this runner.

        Never substitute close for an uncertain managed release. Only close our exact
        recorded terminal when Orca positively attests external ownership and dispatch.
        """
        self.verify_terminal(record, data)
        resource = data.get('terminalResource') or {}
        if resource.get('ownershipState') != 'external':
            return False
        terminal = data.get('terminal') or {}
        if (not record.get('terminal_owned') or terminal.get('handle') != record.get('terminal')
                or resource.get('ownerDispatchId') != record['dispatch_id']
                or (record.get('incarnation') and terminal.get('incarnationId') != record['incarnation'])):
            raise RuntimeError('registered terminal identity no longer matches; refusing close')
        if record.get('attempt'):
            self.effect(record['attempt'], record['id'] + '-close', 'terminal', 'close', '--terminal', record['terminal'],
                   mailbox=record.get('mailbox'), evidence={'terminal': record['terminal'], 'incarnation': record['incarnation']})
        else:
            self.orca('terminal', 'close', '--terminal', record['terminal'], mailbox=record.get('mailbox'))
        remaining = self.orca('terminal', 'list', mailbox=record.get('mailbox')).get('terminals')
        if remaining is None or any(t.get('handle') == record['terminal'] for t in remaining):
            raise RuntimeError('terminal close unconfirmed')
        record['terminal_closed'] = True
        return True


    def release_agent(self, record, attempt=None, accepted_done=None):
        if attempt:
            record.setdefault('attempt', str(attempt))
            record.setdefault('id', str(uuid.uuid4()))
            reg = self.registry(attempt)
            registered = next((a for a in reg['agents'] if a.get('dispatch_id') == record['dispatch_id']), None)
            if registered is None:
                raise RuntimeError('release target not registered')
            registered.update(id=record['id'], attempt=str(attempt))
            self.save_registry(attempt, reg)
        data = self.observe_agent(record)
        status = (data.get('dispatch') or {}).get('status')
        failed_report = (status == 'failed' and accepted_done
                         and accepted_done.get('dispatchId') == record['dispatch_id']
                         and accepted_done.get('outcome') == 'failed')
        if status != 'completed' and not failed_report:
            raise RuntimeError('release requires accepted worker_done')
        if data.get('terminalResource', {}).get('ownershipState') == 'external':
            # There is no Orca-owned release to substitute here: the launcher owns this terminal.
            self.close_registered_terminal(record, data)
        else:
            self.lifecycle(record, 'worker-release')
            if self.liveness(record['dispatch_id'], record.get('mailbox')) != 'exited':
                raise RuntimeError('release unconfirmed')
        record['status'] = 'released'


    def confirm_operator_closed(self, record, data, attempt):
        """Recognize a durable operator close without closing any replacement pane."""
        dispatch = data.get('dispatch') or {}
        worker = data.get('worker') or {}
        resource = data.get('terminalResource') or {}
        projection = (data.get('projection') or {}).get('liveness') or {}
        if (dispatch.get('status') != 'failed'
                or dispatch.get('terminationReason') != 'operator_close'
                or worker.get('stage') != 'process_exited'
                or not isinstance(projection, dict) or projection.get('verdict') != 'exited'):
            return False
        op = read_json(self.journal(attempt), {}).get(record.get('id', '') + '-terminal', {})
        terminal = (op.get('result') or {}).get('terminal') or {}
        incarnation = terminal.get('incarnationId')
        process = f"{terminal.get('ptyId')}:{incarnation}"
        if (op.get('status') != 'completed' or not record.get('terminal_owned') or not incarnation
                or not terminal.get('ptyId') or incarnation != record.get('incarnation')
                or terminal.get('handle') != record.get('terminal')
                or terminal.get('worktreeId') != self.context.orca_id
                or dispatch.get('id') != record.get('dispatch_id')
                or dispatch.get('assigneeHandle') != record.get('terminal')
                or dispatch.get('processIncarnation') != process
                or worker.get('worktreeId') != self.context.orca_id
                or resource.get('ownershipState') != 'external'
                or resource.get('ownerDispatchId') != record.get('dispatch_id')
                or resource.get('terminalHandle') != record.get('terminal')
                or resource.get('worktreeId') != self.context.orca_id
                or resource.get('endpointIncarnation') != process):
            return False
        self.check_operations(attempt)
        observed = data.get('terminal')
        if observed and any(observed.get(k) != terminal.get(k) for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId')):
            return False
        terminals = self.orca('terminal', 'list', '--worktree', 'id:' + self.context.orca_id,
                              mailbox=record.get('mailbox')).get('terminals')
        if not isinstance(terminals, list):
            return False
        for t in terminals:
            if (t.get('handle') == record['terminal'] or t.get('incarnationId') == incarnation
                    or t.get('ptyId') == terminal['ptyId']):
                if any(t.get(k) != terminal.get(k) for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId')):
                    return False
        record.pop('cleanup_error', None)
        record.pop('error', None)
        record.update(terminal_closed=True, close_evidence='Orca operator_close; original process exited',
                      work_incomplete='operator_close without worker_done')
        return True


    def confirm_missing_terminal(self, record, data, attempt, *, stop_active=False):
        """A vanished terminal is settled only by durable ownership and process proof."""
        if data.get('terminal') is not None:
            return False
        dispatch, worker, resource = (data.get(k) or {} for k in ('dispatch', 'worker', 'terminalResource'))
        op = read_json(self.journal(attempt), {}).get(record.get('id', '') + '-terminal', {})
        terminal = (op.get('result') or {}).get('terminal') or {}
        incarnation = terminal.get('incarnationId')
        process = f"{terminal.get('ptyId')}:{incarnation}"
        if (dispatch.get('status') not in (('completed', 'failed', 'dispatched') if stop_active else ('completed', 'failed'))
                or dispatch.get('id') != record.get('dispatch_id')
                or dispatch.get('assigneeHandle') != record.get('terminal')
                or dispatch.get('processIncarnation') != process
                or not record.get('terminal_owned') or not incarnation
                or terminal.get('handle') != record.get('terminal')
                or incarnation != record.get('incarnation')
                or worker.get('worktreeId') != self.context.orca_id
                or worker.get('state') == 'outcome_unknown'
                or resource.get('ownershipState') != 'external'
                or resource.get('ownerDispatchId') != record.get('dispatch_id')
                or resource.get('terminalHandle') != record.get('terminal')
                or resource.get('worktreeId') != self.context.orca_id
                or resource.get('endpointIncarnation') != process):
            return False
        self.check_operations(attempt)
        proof = operations.missing_terminal_evidence(op, self.orca, self.context.orca_id)
        if not proof:
            return False
        if dispatch.get('status') == 'dispatched':
            # Explicit stop only: process death is proven, but Orca still owns
            # an active Dispatch. Journal its cancellation before local settlement.
            record.setdefault('attempt', str(attempt))
            self.lifecycle(record, 'worker-abandon')
        record.update(terminal_closed=True, close_evidence=proof,
                      work_incomplete='terminal disappeared; completion must be verified separately')
        record.pop('error', None)
        record.pop('cleanup_error', None)
        return True


    def stop_attempt(self, attempt):
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.registry(attempt)
            reg['fenced'] = True
            reg['stop_requested'] = True
            self.save_registry(attempt, reg)
            confirmed = True
            for a in reg['agents']:
                if a.get('status') in ('stopped', 'released', 'superseded'):
                    continue
                a.setdefault('attempt', str(attempt))
                a.setdefault('id', str(uuid.uuid4()))
                self.save_registry(attempt, reg)
                d = a.get('dispatch_id')
                try:
                    if not d:
                        ops = read_json(self.journal(attempt), {})
                        dispatch_op = ops.get(a.get('id', '') + '-dispatch')
                        if dispatch_op and dispatch_op.get('status') != 'rejected':
                            raise RuntimeError('Dispatch receipt missing; reconcile before stop')
                        entry = ops.get(a.get('id', '') + '-terminal')
                        if not entry:
                            if a.get('replacement_of') and not a.get('terminal'):
                                self.check_operations(attempt)
                                a.update(status='stopped', terminal_closed=True)
                                self.save_registry(attempt, reg)
                                continue
                            raise RuntimeError('launch receipt missing; reconcile before stop')
                        if not dispatch_op:
                            self.check_operations(attempt)
                            proof = self.closed_pre_dispatch(a, entry)
                            if proof:
                                a.update(status='stopped', terminal_closed=True, close_evidence=proof)
                                a.pop('error', None)
                                a.pop('cleanup_error', None)
                                self.save_registry(attempt, reg)
                                continue
                        terminal = operations.terminal_evidence(entry, self.orca)['terminal']
                        self.effect(attempt, a['id'] + '-close', 'terminal', 'close', '--terminal', terminal['handle'],
                               mailbox=a.get('mailbox'), evidence={'terminal':terminal['handle'], 'incarnation':terminal['incarnationId']})
                        a.update(status='stopped', terminal_closed=True)
                        self.save_registry(attempt, reg)
                        continue
                    live = self.liveness(d, a.get('mailbox'))
                    if live not in ('live', 'exited'):
                        data = self.orca('orchestration', 'worker-show', '--dispatch', d, mailbox=a.get('mailbox'))
                        if self.confirm_missing_terminal(a, data, attempt, stop_active=True):
                            a['status'] = 'stopped'
                            self.save_registry(attempt, reg)
                            continue
                        observation = data.get('observation') or {}
                        # After runtime restore the projection may stay unverifiable
                        # while the exact external process is positively observed live.
                        if (data.get('dispatch', {}).get('status') in ('dispatched', 'completed', 'failed')
                                and observation.get('status') == 'live'
                                and observation.get('exactWorker') is True
                                and data.get('terminalResource', {}).get('ownershipState') == 'external'):
                            self.verify_terminal(a, data)
                            if data['dispatch']['status'] == 'dispatched':
                                self.lifecycle(a, 'worker-abandon')
                            self.close_registered_terminal(a, data)
                            a.update(status='stopped')
                            a.pop('error', None)
                            a.pop('cleanup_error', None)
                            self.save_registry(attempt, reg)
                            continue
                        # Missing status telemetry does not prevent explicit cleanup of
                        # our exact external terminal after a settled worker has exited.
                        if (data.get('dispatch', {}).get('status') not in ('completed', 'failed')
                                or observation.get('status') != 'exited'
                                or observation.get('exactWorker') is not True
                                or data.get('terminalResource', {}).get('ownershipState') != 'external'):
                            raise RuntimeError('liveness unconfirmed')
                        self.close_registered_terminal(a, data)
                        a.update(status='stopped')
                        a.pop('error', None)
                        a.pop('cleanup_error', None)
                        self.save_registry(attempt, reg)
                        continue
                    if live == 'live' or a.get('terminal_owned'):
                        data = self.orca('orchestration', 'worker-show', '--dispatch', d, mailbox=a.get('mailbox'))
                        if live == 'exited' and self.confirm_operator_closed(a, data, attempt):
                            a['status'] = 'stopped'
                            a.pop('error', None)
                            a.pop('cleanup_error', None)
                            self.save_registry(attempt, reg)
                            continue
                        external = data.get('terminalResource', {}).get('ownershipState') == 'external'
                        # A fenced Dispatch can be exited while its external terminal is still live.
                        if external:
                            self.verify_terminal(a, data)
                        if live == 'live':
                            self.lifecycle(a, 'worker-abandon' if external else 'worker-stop')
                        if external:
                            self.close_registered_terminal(a, data)
                    if not a.get('terminal_closed') and self.liveness(d, a.get('mailbox')) != 'exited':
                        raise RuntimeError('stop not confirmed')
                    a['status'] = 'stopped'
                    a.pop('error', None)
                    a.pop('cleanup_error', None)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    a.update(status='unknown', error=str(exc))
                    confirmed = False
                self.save_registry(attempt, reg)
            for run in reg['runs'] if confirmed else []:
                if run.get('status') == 'closed':
                    continue
                try:
                    if not run.get('mailbox') or run.get('status') == 'creating':
                        raise RuntimeError('mailbox creation unconfirmed')
                    self.close_mailbox(attempt, run)
                    run['status'] = 'closed'
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    run['error'] = str(exc)
                    confirmed = False
            reg['stop_status'] = 'stopped' if confirmed else 'unknown'
            self.save_registry(attempt, reg)
            return confirmed


    def lifecycle(self, record, verb):
        if record.get('attempt'):
            return self.effect(record['attempt'], record['id'] + '-' + verb, 'orchestration', verb,
                          '--dispatch', record['dispatch_id'], mailbox=record.get('mailbox'))
        return self.orca('orchestration', verb, '--dispatch', record['dispatch_id'], mailbox=record.get('mailbox'))


    def close_mailbox(self, attempt, run):
        op = read_json(self.journal(attempt), {}).get('mailbox', {})
        terminal = (op.get('result') or {}).get('terminal') or {}
        if (terminal.get('handle') == run.get('mailbox') and terminal.get('incarnationId') == run.get('incarnation')):
            self.check_operations(attempt)
            proof = operations.missing_terminal_evidence(op, self.orca, self.context.orca_id)
            if proof:
                run['close_evidence'] = proof
                return
        terminal = self.orca('terminal', 'show', '--terminal', run['mailbox'])['terminal']
        if not run.get('incarnation') or terminal.get('incarnationId') != run['incarnation'] or terminal.get('worktreeId') != self.context.orca_id:
            raise RuntimeError('mailbox identity unknown/mismatch')
        self.effect(attempt, 'mailbox-close', 'terminal', 'close', '--terminal', run['mailbox'],
               evidence={'terminal': run['mailbox'], 'incarnation': run['incarnation']})


    def reconcile_attempt(self, attempt, request_id=None, accepted_reports=None):
        with lock(Path(attempt) / 'agents.lock'):
            if request_id:
                operations.continue_operation(self.journal(attempt), request_id, self.orca)
            issues = operations.reconcile(self.journal(attempt), self.orca)
            reg = self.registry(attempt)
            ops = read_json(self.journal(attempt), {})
            for a in reversed(reg['agents']):
                if a.get('status') in ('released', 'stopped', 'superseded'):
                    continue
                try:
                    terminal = ops.get(a.get('id', '') + '-terminal', {})
                    if not a.get('terminal') and terminal.get('status') == 'completed':
                        t = terminal['result']['terminal']
                        a.update(terminal=t['handle'], incarnation=t.get('incarnationId'), terminal_owned=True)
                    dispatch = ops.get(a.get('id', '') + '-dispatch', {})
                    if not a.get('dispatch_id') and dispatch.get('status') == 'completed':
                        a.update(dispatch_id=dispatch['result']['dispatchId'], launch_confirmation=dispatch['result'])
                    closed = ops.get(a.get('id', '') + '-close', {})
                    if closed.get('status') == 'completed':
                        operations.terminal_evidence(closed, self.orca)
                        a.update(status='stopped' if reg.get('stop_requested') else 'released', terminal_closed=True)
                        a.pop('error', None)
                        a.pop('cleanup_error', None)
                        continue
                    if not a.get('dispatch_id'):
                        if not dispatch:
                            self.check_operations(attempt)
                            if a.get('replacement_of') and not terminal and not a.get('terminal'):
                                a.update(status='ready')
                                continue
                            proof = self.closed_pre_dispatch(a, terminal)
                            if proof:
                                if reg.get('stop_requested'):
                                    a.update(status='stopped', terminal_closed=True, close_evidence=proof)
                                elif a.get('replacement_of'):
                                    raise RuntimeError('pre-Dispatch terminal replacement budget exhausted')
                                else:
                                    a.update(status='restart_ready', terminal_closed=True, close_evidence=proof)
                                a.pop('error', None)
                                a.pop('cleanup_error', None)
                                continue
                        if a.get('terminal') and terminal.get('status') == 'completed':
                            observed = operations.terminal_evidence(terminal, self.orca)['terminal']
                            if (observed.get('handle') != a.get('terminal')
                                    or observed.get('incarnationId') != a.get('incarnation')
                                    or observed.get('worktreeId') != self.context.orca_id):
                                raise RuntimeError('recorded launch terminal identity changed')
                            a.update(status='ready')
                            a.pop('error', None)
                            a.pop('cleanup_error', None)
                            continue
                        raise RuntimeError('no registered Dispatch receipt; ownership unknown')
                    data = self.observe_agent(a)
                    if (self.confirm_missing_terminal(a, data, attempt)
                            or self.confirm_operator_closed(a, data, attempt)):
                        a['status'] = 'stopped'
                        a.pop('error', None)
                        a.pop('cleanup_error', None)
                        continue
                    if data.get('terminalResource', {}).get('ownershipState') != 'external' and self.liveness(a['dispatch_id'], a.get('mailbox')) == 'exited':
                        if any(ops.get(a.get('id', '') + '-' + v, {}).get('status') == 'completed' for v in ('worker-stop', 'worker-release')):
                            a['status'] = 'stopped' if reg.get('stop_requested') else 'released'
                            continue
                    self.verify_terminal(a, data)
                    # Technical recovery may settle an exact owned process death even
                    # when no worker_done could be delivered. Acceptance remains separate.
                    observation = data.get('observation') or {}
                    if (accepted_reports is not None and data.get('dispatch', {}).get('status') == 'failed'
                            and observation.get('status') == 'exited' and observation.get('exactWorker') is True
                            and data.get('worker', {}).get('stage') == 'process_exited'
                            and data.get('worker', {}).get('state') != 'outcome_unknown'
                            and data.get('terminalResource', {}).get('ownershipState') == 'external'):
                        a.update(status='active')
                        a.pop('error', None)
                        a.pop('cleanup_error', None)
                        continue
                    report = (accepted_reports or {}).get(a.get('dispatch_id')) or {}
                    if (data.get('dispatch', {}).get('status') == 'failed'
                            and report.get('dispatchId') == a.get('dispatch_id')
                            and report.get('outcome') == 'failed'):
                        a.update(status='active')
                        a.pop('error', None)
                        a.pop('cleanup_error', None)
                        continue
                    if data.get('dispatch', {}).get('status') not in ('dispatched', 'completed') or data.get('worker', {}).get('state') in ('outcome_unknown', 'failed', 'starting'):
                        raise RuntimeError('Dispatch startup/settlement not confirmed')
                    if a.get('replacement_of'):
                        for request in reg.get('launch_requests', []):
                            if request.get('task_id') == a.get('task_id'):
                                request['status'] = 'done'
                    if a.get('reuse'):
                        old = next(x for x in reg['agents'] if x.get('dispatch_id') == a['reuse'])
                        old.update(status='superseded', superseded_by=a['dispatch_id'])
                    a.update(status='active')
                    a.pop('error', None)
                    a.pop('cleanup_error', None)
                except Exception as exc:
                    a.update(status='unknown', error=str(exc))
                    issues.append(f"{a.get('role')} Dispatch {a.get('dispatch_id')}: {exc}")
            for request in reg.get('launch_requests', []):
                op = ops.get('task-' + request['id'], {})
                if request['status'] != 'done' and op.get('status') == 'completed':
                    request.update(task_id=op['result']['task']['id'], status='ready')
            for run in reg['runs']:
                if ops.get('mailbox-close', {}).get('status') == 'completed':
                    run['status'] = 'closed'
                if run.get('status') == 'creating':
                    t, r = ops.get('mailbox', {}), ops.get('child-run', {})
                    if t.get('status') == 'completed':
                        run.update(mailbox=t['result']['terminal']['handle'], incarnation=t['result']['terminal'].get('incarnationId'))
                    if r.get('status') == 'completed':
                        run.update(run_id=r['result']['run']['id'], status='ready')
                    elif not r and run.get('mailbox'):
                        operations.terminal_evidence(t, self.orca)
                        run['status'] = 'mailbox_ready'
                    else:
                        issues.append('child run receipt missing')
            reg['recovery_issues'] = issues
            reg['fenced'] = bool(issues or reg.get('stop_requested'))
            self.save_registry(attempt, reg)
            return issues


    def closed_pre_dispatch(self, record, op):
        terminal = (op.get('result') or {}).get('terminal') or {}
        if (not record.get('terminal_owned') or not record.get('incarnation')
                or terminal.get('handle') != record.get('terminal')
                or terminal.get('incarnationId') != record.get('incarnation')):
            return None
        return operations.closed_launch_evidence(op, self.orca, self.context.orca_id)


    def resume_ready_launches(self, attempt, state_dir, permitted=None):
        """Continue exact live launches or replace a proved dead pre-Dispatch once."""
        attempt = Path(attempt)
        self.context.check_attempt(attempt)
        with lock(attempt / 'agents.lock'):
            reg = self.registry(attempt)
            if reg.get('fenced') or reg.get('stop_requested'):
                raise RuntimeError('attempt fenced; launch continuation forbidden')
            self.check_operations(attempt)
            for record in list(reg['agents']):
                if record.get('status') not in ('ready', 'restart_ready') or record.get('dispatch_id'):
                    continue
                if permitted is not None and not permitted():
                    raise RuntimeError('launch continuation cancelled by user intent')
                if record.get('acceptance_rework'):
                    continue
                ops = read_json(self.journal(attempt), {})
                if record['status'] == 'restart_ready':
                    if (record.get('replacement_of') or record['id'] + '-dispatch' in ops
                            or not self.closed_pre_dispatch(record, ops.get(record['id'] + '-terminal', {}))):
                        raise RuntimeError('pre-Dispatch terminal replacement not proved')
                    replacement = {k: v for k, v in record.items() if k not in (
                        'id', 'terminal', 'incarnation', 'terminal_owned', 'terminal_closed',
                        'close_evidence', 'status', 'error', 'cleanup_error')}
                    replacement.update(id=str(uuid.uuid4()), status='ready', replacement_of=record['id'])
                    record.update(status='superseded', superseded_by=replacement['id'])
                    reg['agents'].append(replacement)
                    self.save_registry(attempt, reg)
                    record = replacement
                op = read_json(self.journal(attempt), {}).get(record['id'] + '-terminal', {})
                if record.get('replacement_of') and not op and not record.get('terminal'):
                    if permitted is not None and not permitted():
                        raise RuntimeError('launch continuation cancelled by user intent')
                    self._launch(attempt, reg, record, record.get('launch_title', record['role']),
                                 state_dir, record.get('retry_of'), permitted=permitted)
                    for request in reg.get('launch_requests', []):
                        if request.get('task_id') == record.get('task_id'):
                            request['status'] = 'done'
                    self.save_registry(attempt, reg)
                    continue
                if op.get('status') != 'completed':
                    raise RuntimeError('launch terminal receipt missing')
                terminal = operations.terminal_evidence(op, self.orca)['terminal']
                if (terminal.get('handle') != record.get('terminal')
                        or terminal.get('incarnationId') != record.get('incarnation')
                        or terminal.get('worktreeId') != self.context.orca_id):
                    raise RuntimeError('launch terminal identity changed')
                self._launch(attempt, reg, record, record.get('launch_title', record['role']),
                             state_dir, record.get('retry_of'), permitted=permitted)
                if record.get('replacement_of'):
                    for request in reg.get('launch_requests', []):
                        if request.get('task_id') == record.get('task_id'):
                            request['status'] = 'done'
                    self.save_registry(attempt, reg)
