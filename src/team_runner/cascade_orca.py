"""Local Orca transport for the native cascade."""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit
import uuid

from . import agent_runtime, orca, runner_config, runner_operations
from .catalog import git_root
from .runner_entry import boot_id, process_stamp
from .runner_launch import Launcher
from .runner_store import atomic_json, read_json


ROLE_SETTINGS = {
    'coordinator': 'coordinator',
    'implementer': 'implementer_standard',
    'tester': 'tester',
    'reviewer': 'final_reviewer',
}
TERMINAL_FIELDS = ('handle', 'incarnationId', 'ptyId', 'worktreeId', 'tabId', 'leafId')
STOPPED_DISPATCH = ('completed', 'failed', 'stopped', 'fenced', 'abandoned')
METHODS = {
    'run-create': 'runCreate',
    'task-create': 'taskCreate',
    'worker-start': 'workerStart',
    'worker-stop': 'workerStop',
    'send': 'send',
    'reply': 'reply',
    'ask': 'ask',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _git(checkout, *args):
    return subprocess.run(
        ['git', '-C', str(checkout), *args], capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()


def _version(binary):
    return subprocess.run([binary, '--version'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def _binary(command):
    path = shutil.which(command)
    require(path, f'{command} is unavailable')
    return str(Path(path).absolute())


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _codex_home():
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser().resolve()


def _connection(checkout, cfg):
    """Fingerprint the applicable Codex connection files and environment without retaining their values."""
    home = _codex_home()
    config_path = home / 'config.toml'
    contents = config_path.read_bytes() if config_path.is_file() else b''
    system_config = Path('/etc/codex/config.toml')
    global_config = tomllib.loads(system_config.read_text()) if system_config.is_file() else {}
    user_config = tomllib.loads(contents.decode())
    config = global_config | user_config
    default = config.get('model_provider', 'openai')
    provider = cfg['provider'] or default
    providers = global_config.get('model_providers', {}) | user_config.get('model_providers', {})
    settings = providers.get(provider, {})
    persisted = [str(home), sha256(contents).hexdigest()]
    for name in ('auth.json', 'config.toml', 'requirements.toml'):
        system = Path('/etc/codex') / name
        user = home / name
        for path in (system, user):
            persisted.append(sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    layers = []
    for root in (*reversed(checkout.parents), checkout):
        candidate = root / '.codex/config.toml'
        if candidate.is_file() and candidate.resolve() != config_path:
            layer = candidate.read_bytes()
            layers.append(sha256(layer).hexdigest())
            # Supporting a named or project-local connection profile requires resolving
            # its effective settings exactly as Codex does, including project trust.
            layer_settings = tomllib.loads(layer.decode())
            managed = {'model_provider', 'model_providers', 'profile', 'profiles', 'permissions',
                       'sandbox_mode', 'sandbox_workspace_write', 'approval_policy',
                       'approvals_reviewer', 'service_tier'}
            require(not managed.intersection(layer_settings),
                    'cascade cannot reproduce project-local Codex connection or access overrides')
    require(not config.get('profile') and not config.get('permissions'),
            'cascade cannot reproduce a configured Codex profile or permissions profile')
    require(config.get('sandbox_mode', 'workspace-write') == 'workspace-write',
            'cascade requires the Runner workspace-write sandbox')
    # Launcher explicitly supplies this policy. A global setting is overridden by
    # Runner for every ordinary Codex launch as well.
    provider_env = settings.get('env_key')
    env = {name: value for name, value in os.environ.items()
           if name == provider_env or name.startswith('OPENAI_') or (name.startswith('CODEX_') and name.endswith('_API_KEY'))}
    persisted.extend((layers, env))
    return provider, _digest(persisted)


def _container_connection(provider, checkout):
    """Pass only the selected localhost provider to the disposable Codex home."""
    settings = {}
    for path in (Path('/etc/codex/config.toml'), _codex_home() / 'config.toml'):
        if path.is_file():
            settings.update(tomllib.loads(path.read_text()).get('model_providers', {}))
    selected = dict(settings.get(provider, {}))
    endpoint = urlsplit(selected.get('base_url', ''))
    credential = selected.get('env_key', '')
    require(endpoint.scheme == 'http' and endpoint.hostname in ('localhost', '127.0.0.1')
            and endpoint.port and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', credential) and os.environ.get(credential)
            and selected.get('requires_openai_auth') is False,
            'container verifier requires an explicit localhost Codex provider and its environment credential')
    fields = {'name', 'base_url', 'env_key', 'wire_api', 'requires_openai_auth', 'supports_websockets',
              'model_catalog_url'}
    require(set(selected) <= fields, 'unsupported Codex provider configuration for container verifier')
    for name in ('base_url', 'model_catalog_url'):
        if name not in selected:
            continue
        url = urlsplit(selected[name])
        require((url.scheme, url.hostname, url.port) == (endpoint.scheme, endpoint.hostname, endpoint.port)
                and not (url.username or url.password or url.query or url.fragment),
                'container verifier provider URLs must share one localhost endpoint')
        selected[name] = urlunsplit((url.scheme, '127.0.0.1:2455', url.path, '', ''))
    common = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    projects = sorted({str(checkout), str(common.parent)})
    config = ['check_for_update_on_startup = false', 'web_search = "disabled"',
              '[tui]', 'terminal_title = ["app-name", "spinner", "status"]']
    for project in projects:
        config.extend(('[projects.' + json.dumps(project) + ']', 'trust_level = "untrusted"'))
    config.append('[model_providers.' + json.dumps(provider) + ']')
    config.extend(name + ' = ' + json.dumps(value) for name, value in sorted(selected.items()))
    return {'credential': credential, 'port': endpoint.port, 'config': '\n'.join(config) + '\n'}


def _scope_rows(result, field):
    scope = result.get('hostScope') or {}
    rows = result.get(field)
    require(result.get('truncated') is False and scope.get('hostIds') == ['local']
            and isinstance(scope.get('omittedHostIds'), list) and 'local' not in scope['omittedHostIds']
            and isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
            f'incomplete local Orca {field} inventory')
    if 'totalCount' in result:
        require(result['totalCount'] == len(rows), f'incomplete local Orca {field} inventory')
    return rows


def _terminal_identity(terminal):
    require(all(isinstance(terminal.get(field), str) and terminal[field] for field in TERMINAL_FIELDS),
            'Orca terminal identity is incomplete')
    return {field: terminal[field] for field in TERMINAL_FIELDS}


def _pane(terminal):
    return terminal['tabId'] + ':' + terminal['leafId']


def _flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def _process_tree(root):
    children = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(path.name))
        except (OSError, ValueError, IndexError):
            continue
    pending = [root]
    seen = set()
    while pending:
        pid = pending.pop()
        if pid not in seen:
            seen.add(pid)
            yield pid
            pending.extend(children.get(pid, ()))


def _codex_pid(root, launch):
    expected = [os.fsencode(arg) for arg in launch['codex_argv'][1:]]
    for pid in _process_tree(root):
        process = Path('/proc') / str(pid)
        try:
            actual = (process / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            binary = (process / 'exe').resolve(strict=True)
            if (len(actual) <= len(expected) or actual[-len(expected):] != expected
                    or binary.name != 'codex' or (process / 'cwd').resolve() != Path(launch['checkout'])):
                continue
            guard = launch.get('guard')
            if not guard:
                return pid
            mounts = [line.split() for line in (process / 'mountinfo').read_text().splitlines()]
            read_only = {os.fsdecode(entry[4].replace('\\040', ' ').replace('\\011', '\t'))
                         for entry in mounts if 'ro' in entry[5].split(',')}
            if set(guard['protected']) <= read_only:
                return pid
        except (OSError, ValueError, IndexError):
            continue
    return None


class OrcaBackend:
    def __init__(self, transport=None):
        self.transport = transport or orca.call

    def call(self, *args, mailbox=None):
        # The recorded address is used to verify the caller, never exported over the
        # actual caller's credentials. Orca authorizes every protected mutation.
        if mailbox is not None:
            require(mailbox in (os.environ.get('ORCA_TERMINAL_HANDLE'),
                                'orca_session_id:' + os.environ.get('ORCA_AGENT_SESSION_ID', '')),
                    'Orca caller differs from the journaled identity')
        require(not os.environ.get('ORCA_PAIRING_CODE')
                and os.environ.get('ORCA_ENVIRONMENT', 'local') == 'local',
                'cascade requires the local Orca host')
        options = {'timeout': 90} if self.transport is orca.call and args[:2] == (
            'orchestration', 'worker-start'
        ) else {}
        return self.transport(*args, mailbox=None, **options)

    def manager_identity(self, context):
        worktree = context.verify(self.call)
        require(git_root(Path.cwd()) == context.repo, 'cascade caller is outside the registered checkout')
        session = os.environ.get('ORCA_AGENT_SESSION_ID')
        if session:
            status = self.call('status').get('caller') or {}
            require(status.get('live') is True and status.get('orcaSessionId') == session,
                    'Orca did not authenticate this native Chat session')
            places = self.call('agent', 'show', session).get('places', [])
            matches = [p for p in places if p.get('orcaSessionId') == session
                       and p.get('kind') == 'chat' and p.get('agent') == 'codex'
                       and p.get('hostId') == 'local' and p.get('worktreeId') == context.orca_id]
            require(len(places) == len(matches) == 1, 'native Chat workspace is ambiguous or different')
            return {'kind': 'chat', 'session': session, 'mailbox': 'orca_session_id:' + session,
                    'worktree': context.orca_id, 'incarnation': worktree['instanceId']}
        handle = os.environ.get('ORCA_TERMINAL_HANDLE')
        pane = os.environ.get('ORCA_PANE_KEY')
        require(handle and pane, 'cascade requires a live Orca Codex session')
        terminal = self._terminal(handle, context.orca_id, context.repo)
        require(_pane(terminal) == pane, 'Orca caller pane mismatch')
        places = _scope_rows(self.call('agent', 'list', '--worktree', 'id:' + context.orca_id), 'agents')
        matches = [p for p in places if p.get('paneKey') == pane and p.get('kind') == 'terminal'
                   and p.get('hostId') == 'local' and p.get('worktreeId') == context.orca_id
                   and p.get('agent') == 'codex']
        require(len(matches) == 1 and matches[0].get('orcaSessionId'),
                'Orca terminal Codex session is absent or ambiguous')
        session = matches[0]['orcaSessionId']
        places = self.call('agent', 'show', session).get('places', [])
        require(len(places) == 1 and places[0].get('paneKey') == pane
                and places[0].get('worktreeId') == context.orca_id,
                'Orca terminal Codex session has multiple or different panes')
        return {'kind': 'terminal', 'session': session, 'mailbox': handle, 'worktree': context.orca_id,
                'incarnation': worktree['instanceId'], 'terminal_identity': terminal}

    def preflight(self, context):
        manager = self.manager_identity(context)
        require(self.inbox_window,
                'Orca inbox history contract has not been verified for this runtime version')
        return manager

    @property
    def inbox_window(self):
        # Installed 1.4.265: orchestration.inbox -> getAllMessagesForHandle, exact
        # to_handle and optional run_id predicate, sequence DESC LIMIT, includes read.
        runtime = self.call('status').get('runtime') or {}
        if (runtime.get('reachable') is True and runtime.get('appVersion') == '1.4.265'
                and 'orchestration.ask-returns-at-once.v1' in runtime.get('capabilities', ())
                and _version(_binary(orca.executable())) == '1.4.265'):
            return (4096, 'desc')
        return None

    def role_snapshots(self, context):
        config = runner_config.load_config(
            context.settings_dir, layers=context.role_layers, repo=context.repo,
            config_root=context.config_root.parent,
        )
        require(config['version'] != 3, 'cascade does not support graph role routing')
        require(not any((config.get('reserves') or {}).values()), 'cascade does not support reserve roles')
        binary = _binary('codex')
        version = _version(binary)
        runtime = agent_runtime.validate(context.profile['runtime'])
        image = os.environ.get('TEAM_RUNNER_CASCADE_VERIFIER_IMAGE')
        if image:
            from .cascade_container import image_version, resolve_image
            image = resolve_image(image)
            require(image_version(image) == version, 'verifier image Codex version differs from the host')
        snapshots = {}
        for role, key in ROLE_SETTINGS.items():
            settings = dict(config['roles'][key])
            require(settings['agent'] == 'codex' and not settings['profile'] and not settings.get('fast')
                    and settings.get('model') and settings.get('effort'),
                    f'{role}: cascade requires local Codex, empty profile, explicit model and effort, and standard tier')
            provider, connection = _connection(context.repo, settings)
            settings['provider'] = provider
            settings.pop('fast', None)
            runner_config.validate_role_scope(settings, context.repo)
            catalog = runner_config.connections(repo=context.repo)
            require(provider in catalog['base_providers'], f'{role}: Codex provider is unavailable')
            Launcher(context).argv(settings, context.state_dir, runtime)
            snapshots[role] = {'settings': settings, 'runtime': runtime, 'binary': binary, 'version': version,
                               'connection': connection, 'tier': 'default',
                               'binary_sha256': sha256(Path(binary).read_bytes()).hexdigest()}
            if image and role in ('tester', 'reviewer'):
                _container_connection(provider, context.repo)
                snapshots[role]['verifier_image'] = image
        return snapshots

    def _terminal(self, handle, worktree_id, checkout):
        terminal = self.call('terminal', 'show', '--terminal', handle).get('terminal') or {}
        require(terminal.get('handle') == handle and terminal.get('worktreeId') == worktree_id
                and terminal.get('worktreePath') == str(checkout) and terminal.get('executionHostId') == 'local'
                and terminal.get('connected') is True and terminal.get('writable') is True
                and terminal.get('orphaned') is False, 'Orca terminal is not live in the exact checkout')
        return _terminal_identity(terminal)

    def _run(self, run_id, owner):
        run = self.call('orchestration', 'run-show', '--id', run_id).get('run') or {}
        require(self._run_matches(run, run_id, owner),
                f'Orca Run {run_id} coordinator mismatch')
        return run

    def _run_current(self, run_id, owner):
        run = self.call('orchestration', 'run-current', mailbox=owner).get('run') or {}
        require(self._run_matches(run, run_id, owner),
                f'Orca Run {run_id} is not currently bound to this caller')
        return run

    @staticmethod
    def _run_matches(run, run_id, owner):
        # Orca exposes native bindings only through the authenticated current-run
        # lookup. The Run's public record omits the coordinator's session ID.
        handle = None if owner.startswith('orca_session_id:') else owner
        return ('coordinator_handle' in run and run['coordinator_handle'] == handle
                and run.get('id') == run_id and bool(run_id) and run.get('legacy') == 0)

    @staticmethod
    def _intent(state, key, args, mailbox=None, **evidence):
        table = state['operations']
        if key in table:
            current = table[key]
            require(current['args'] == list(args) and current['mailbox'] == mailbox
                    and current.get('evidence', {}) == evidence, 'cascade operation identity changed: ' + key)
            if (state['status'] == 'stopping' and current['status'] == 'cancelled'
                    and args[:2] in (('orchestration', 'worker-stop'), ('terminal', 'close'),
                                     ('docker', 'container-rm'))):
                current['status'] = 'intent'
            return
        table[key] = {'args': list(args), 'mailbox': mailbox, 'request_id': str(uuid.uuid4()),
                      'status': 'intent', 'evidence': evidence}

    def ensure_manager(self, session):
        state = session.read()
        owner = state['manager']['mailbox']
        require(self.inbox_window, 'Orca inbox history contract is unconfirmed')
        require(self.manager_identity(session.context) == state['manager'], 'cascade manager identity mismatch')
        if state['manager_run']:
            self._run_current(state['manager_run'], owner)
            return state['manager_run']
        return self._ensure_run(session, 'manager', owner)

    def ensure_run(self, session, fid):
        state = session.read()
        feature = state['features'][fid]
        coordinator = feature['roles']['coordinator']
        self.assignment_identity(session, state, feature, coordinator)
        require(self.inbox_window, 'Orca inbox history contract is unconfirmed')
        owner = coordinator['terminal']
        if feature.get('run'):
            self._run_current(feature['run'], owner)
            return feature['run']
        return self._ensure_run(session, fid, owner)

    def _ensure_run(self, session, target, owner):
        objective = f'Team Runner cascade {session.execution}/{target}'
        key = 'run:' + target
        args = ('orchestration', 'run-create', '--objective', objective)
        if not owner.startswith('orca_session_id:'):
            args += ('--from', owner)
        with session.transaction() as state:
            self._intent(state, key, args, owner)
        result = session.effect(key, validate=lambda data: self._validate_run_create(data, owner, objective))
        run_id = result['run']['id']
        self._run_current(run_id, owner)
        with session.transaction() as state:
            if target == 'manager':
                state['manager_run'] = run_id
            else:
                state['features'][target]['run'] = run_id
        return run_id

    def _validate_run_create(self, result, owner, objective):
        run = result.get('run') or {}
        require(self._run_matches(run, run.get('id'), owner) and run.get('objective') == objective,
                'Orca Run creation receipt is not for this coordinator')
        observed = self._run_current(run['id'], owner)
        require(observed.get('objective') == objective, 'bound Orca Run objective changed')

    def ensure_attempt(self, session, fid):
        state = session.read()
        feature = state['features'][fid]
        if feature.get('checkout'):
            self._checkout(session, feature)
            return feature['checkout']
        parent = session.context.verify(self.call)
        name = 'cascade-' + feature['attempt']
        # Explicit --branch disables Orca's configurable prefix and normalizes
        # recovery to one exact ref even if the user changes that setting.
        branch = 'team-runner/' + name
        repo_id = parent['repoId']
        # A plain repo import has no explicit projectId on the main checkout.
        # Orca assigns its children to the implicit repo project.
        evidence = {'repo_id': repo_id, 'project': parent.get('projectId') or 'repo:' + repo_id,
                    'parent': parent['id'], 'branch': 'refs/heads/' + branch,
                    'sha': feature['base_sha'], 'marker': session.execution + '/' + fid + '/' + feature['attempt']}
        key = 'worktree:' + fid
        args = ('worktree', 'create', '--repo', 'id:' + evidence['repo_id'], '--parent-worktree',
                'id:' + evidence['parent'], '--base-branch', evidence['sha'], '--branch', branch,
                '--name', name, '--comment', evidence['marker'], '--setup', 'skip')
        with session.transaction() as latest:
            self._intent(latest, key, args, **evidence)
        result = session.effect(
            key, validate=lambda r: self._created_worktree(session, evidence, r.get('worktree') or {}),
        )
        worktree = result['worktree']
        with session.transaction() as latest:
            latest['features'][fid].update(checkout=worktree['path'], worktree=evidence | {
                'id': worktree['id'], 'incarnation': worktree['instanceId'], 'path': worktree['path'],
            })
        return worktree['path']

    def _check_worktree(self, session, worktree, expected, sha=None):
        path = Path(worktree.get('path') or '/nonexistent')
        require(path.is_absolute() and path.resolve() == path and worktree.get('id') and worktree.get('instanceId')
                and worktree.get('hostId') == 'local' and worktree.get('isArchived') is False
                and worktree.get('isBare') is False and worktree.get('isMainWorktree') is False
                and worktree.get('repoId') == expected['repo_id'] and worktree.get('projectId') == expected['project']
                and worktree.get('parentWorktreeId') == expected['parent']
                and worktree.get('comment') == expected['marker']
                and worktree.get('branch') == expected['branch'], 'Orca worktree identity, lineage or branch mismatch')
        require(git_root(path) == path and _git(path, 'symbolic-ref', 'HEAD') == expected['branch']
                and _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
                == _git(session.context.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'),
                'Orca worktree is not the expected Git checkout')
        if sha is not None:
            require(worktree.get('head') == sha and _git(path, 'rev-parse', 'HEAD') == sha,
                    'Orca worktree base or HEAD differs from the assignment')
        return path

    def _created_worktree(self, session, expected, receipt):
        self._check_worktree(session, receipt, expected, expected['sha'])
        current = self.call('worktree', 'show', '--worktree', 'id:' + receipt['id']).get('worktree') or {}
        self._check_worktree(session, current, expected, expected['sha'])
        require(all(current.get(k) == receipt[k] for k in ('path', 'id', 'instanceId')),
                'Orca worktree creation identity changed')

    def _recover_worktree(self, session, evidence):
        inventory = self.call('worktree', 'list', '--repo', 'id:' + evidence['repo_id'], '--limit', '4096')
        rows = _scope_rows(inventory, 'worktrees')
        marked = [r for r in rows if r.get('comment') == evidence['marker'] or r.get('branch') == evidence['branch']]
        require(len(marked) == 1, 'worktree creation outcome unknown: exact attempt inventory is absent or ambiguous')
        self._created_worktree(session, evidence, marked[0])
        return {'worktree': marked[0]}

    def _checkout(self, session, feature, sha=None):
        expected = feature['worktree']
        current = self.call('worktree', 'show', '--worktree', 'id:' + expected['id']).get('worktree') or {}
        require(current.get('instanceId') == expected['incarnation'] and current.get('id') == expected['id']
                and current.get('path') == feature['checkout'] == expected['path'],
                'cascade worktree address or incarnation changed')
        return self._check_worktree(session, current, expected, sha)

    def _task(self, run, task, spec):
        result = self.call('orchestration', 'task-list', '--run', run)
        tasks = result.get('tasks') or []
        matches = [row for row in tasks if row.get('id') == task]
        require(result.get('runId') == run and len(matches) == 1
                and matches[0].get('run_id') == run and matches[0].get('spec') == spec,
                'Orca Task differs from the assigned specification')
        return matches[0]

    def _assignment_spec(self, session, state, fid, feature, role):
        definition = next(row for row in state['snapshot']['queue']['queue'] if row['id'] == fid)
        directory = Path(feature['directory'])
        card = state['snapshot']['cards'][fid + '/' + definition['items'][0]['id']]
        artifact = directory / 'materials' / 'step.md'
        require(artifact.read_text(encoding='utf-8') == card, 'frozen step card changed')
        resume = f'team-runner cascade resume --execution {session.execution} --feature {fid} --dispatch <your-dispatch>'
        report = f'team-runner cascade report --execution {session.execution} --feature {fid} --dispatch <your-dispatch> --input FILE'
        sha = (feature.get('candidate') or {}).get('sha') if role in ('tester', 'reviewer') else feature['base_sha']
        guidance = {
            'coordinator': f'On your first turn and after every wake or manual return, run {resume}. '
                           'After advancing ready work, end your turn and wait for Orca. Check the outcomes before reporting.',
            'implementer': 'Implement the step card and create a local candidate commit. Report its exact HEAD.',
            'tester': 'Use the frozen clean candidate. The coordinator runs the frozen commands. Wait for their '
                      'check-*.json receipts in the attempt and cite them in your report.',
            'reviewer': 'Review the frozen clean candidate against the card. Cite your evidence in your report.',
        }
        return '\n'.join((
            f'Cascade execution {session.execution}, feature {fid}, attempt {feature["attempt"]}, role {role}.',
            f'Checkout: {feature["checkout"]}. Assigned SHA: {sha}.',
            'Use TEAM_RUNNER_ATTEMPT as the directory for these paths.',
            'Read materials/step.md, materials/context.json and materials/checks.json before working.',
            'Apply materials/project-AGENTS.md when present and the relevant rules and role material in materials/.',
            guidance[role],
            'Use the Dispatch ID from the Orca preamble. Wait for the assignment to be registered if it is still starting.',
            f'Report once with {report}. JSON fields: role, outcome, sha, summary, evidence (attempt-relative paths).',
        ))

    def _materials(self, session, state, fid, feature):
        definition = next(row for row in state['snapshot']['queue']['queue'] if row['id'] == fid)
        materials = Path(feature['directory']) / 'materials'
        materials.mkdir(parents=True, exist_ok=True)
        card = state['snapshot']['cards'][fid + '/' + definition['items'][0]['id']]
        path = materials / 'step.md'
        if not path.exists():
            path.write_text(card, encoding='utf-8')
        require(path.read_text(encoding='utf-8') == card, 'frozen step card changed')
        frozen = session.directory / 'snapshot/materials'
        for source in frozen.rglob('*'):
            target = materials / source.relative_to(frozen)
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                original = source.read_bytes()
                if not target.exists():
                    target.write_bytes(original)
                require(target.read_bytes() == original, 'frozen rule changed: ' + target.name)
        context = read_json(session.directory / 'snapshot/context.json')
        context.update(profile=state['profile'], attempt=feature['attempt'], checkout=feature['checkout'],
                       feature=definition, role_settings=state['role_settings'])
        for name, value in (('context.json', context), ('checks.json', state['profile']['checks'])):
            path = materials / name
            if not path.exists():
                atomic_json(path, value)
            require(read_json(path) == value, f'frozen {name} changed')

    def _launch_plan(self, session, state, fid, feature, role, checkout):
        frozen = state['role_settings'][role]
        settings = frozen['settings']
        provider, connection = _connection(checkout, settings)
        _validate_launch(frozen, provider, connection)
        child = replace(session.context, repo=checkout, orca_id=feature['worktree']['id'],
                        orca_instance=feature['worktree']['incarnation'])
        runtime = agent_runtime.prepare(frozen['runtime'])
        attempt = Path(feature['directory'])
        artifacts = attempt / 'artifacts' / role
        artifacts.mkdir(parents=True, exist_ok=True)
        env = child.environment(attempt)
        env.update(
            TEAM_RUNNER_STATE_ROOT=str(session.context.state_root),
            TEAM_RUNNER_CASCADE_EXECUTION=session.execution,
            TEAM_RUNNER_CASCADE_FEATURE=fid,
            TEAM_RUNNER_CASCADE_ROLE=role,
            TEAM_RUNNER_CASCADE_ARTIFACTS=str(artifacts),
            PYTHONPATH=str(Path(__file__).resolve().parent.parent),
        )
        if runtime['go_cache']:
            env['GOCACHE'] = runtime['go_cache']
        writable = attempt
        candidate = feature.get('candidate')
        verifier = role in ('tester', 'reviewer')
        if verifier:
            require(candidate, 'verifier requires a frozen candidate')
            writable = artifacts
            env['TMPDIR'] = str(artifacts)
        argv = Launcher(child).argv(settings, writable, runtime)
        argv[0] = frozen['binary']
        argv.extend(('--no-daemon', '-c', 'service_tier=' + json.dumps(frozen['tier'])))
        codex_argv = list(argv)
        guard = before = None
        if verifier and frozen.get('verifier_image'):
            from . import cascade_container, cascade_verification
            guard = cascade_container.guard(candidate, artifacts, frozen['verifier_image'],
                                            'team-runner-verifier-' + uuid.uuid4().hex)
            before = cascade_verification.boundary(checkout, candidate)
            # Docker confines the Codex process, its editor and its subprocesses.
            codex_argv = ['/usr/local/bin/codex', '-s', 'danger-full-access', '-a', 'never',
                          '--model', settings['model'], '-c', 'model_provider=' + json.dumps(provider),
                          '-c', 'model_reasoning_effort=' + json.dumps(settings['effort']),
                          '-c', 'features.multi_agent=false', '--disable', 'tui_workspace', '--no-daemon',
                          '-c', 'service_tier=' + json.dumps(frozen['tier'])]
            container = {'guard': guard, 'codex_argv': codex_argv,
                         'provider': _container_connection(provider, checkout)}
            argv = [sys.executable, '-m', 'team_runner.cascade_role', session.execution, fid,
                    '--container', json.dumps(container)]
        elif verifier:
            from . import cascade_verification
            # Immutable inputs and authoritative receipts stay outside all writable mounts.
            writable_roots = [artifacts]
            cache = runtime['go_cache']
            if cache:
                writable_roots.append(cache)
            codex_home = _codex_home()
            require(codex_home.is_dir(), 'Codex home is unavailable for verifier')
            # Codex creates session logs; configuration and auth files remain immutable.
            codex_dirs = ('sessions', 'tmp', 'log')
            for name in codex_dirs:
                path = codex_home / name
                require(path.is_dir() and path.resolve() == path, f'Codex verifier directory unavailable: {name}')
                writable_roots.append(path)
            guard = cascade_verification.probe_readonly(
                checkout, candidate, writable_roots, codex_argv=codex_argv, environment=os.environ | env,
            )
            before = cascade_verification.boundary(checkout, candidate)
            protected = cascade_verification.readonly_argv(argv, checkout, writable_roots, candidate=candidate, role=True)
            argv = [sys.executable, '-m', 'team_runner.cascade_role', session.execution, fid, '--', *protected]
        return {'argv': argv, 'codex_argv': codex_argv, 'environment': env, 'runtime': frozen,
                'guard': guard, 'before': before,
                'sha': candidate['sha'] if verifier else feature['base_sha']}

    def ensure_role(self, session, fid, role):
        state = session.read()
        feature = state['features'][fid]
        assigned = feature['roles'].get(role)
        if assigned and assigned.get('dispatch'):
            self._dispatch(assigned)
            return assigned
        coordinator = role == 'coordinator'
        if coordinator:
            owner = state['manager']['mailbox']
            run = state['manager_run']
            require(self.manager_identity(session.context) == state['manager'], 'cascade manager identity mismatch')
        else:
            lead = feature['roles']['coordinator']
            self.assignment_identity(session, state, feature, lead)
            owner = lead['terminal']
            run = feature['run']
        self._run_current(run, owner)
        candidate = feature.get('candidate') or {}
        checkout = self._checkout(session, feature, feature['base_sha']
                                  if role in ('coordinator', 'implementer') else candidate['sha'])
        self._materials(session, state, fid, feature)
        spec = self._assignment_spec(session, state, fid, feature, role)
        prefix = f'{fid}:{role}'
        if not assigned:
            plan = self._launch_plan(session, state, fid, feature, role, checkout)
            with session.transaction() as state:
                feature = state['features'][fid]
                assigned = feature['roles'].setdefault(role, {
                    'execution': session.execution, 'feature': fid, 'attempt': feature['attempt'],
                    'role': role, 'run': run, 'checkout': str(checkout), 'worktree': feature['worktree']['id'],
                    'nonce': str(uuid.uuid4()), 'launch': plan, 'directory': feature['directory'],
                    'task': None, 'terminal': None, 'dispatch': None,
                    **({'readonly': plan['guard'], 'before': plan['before']} if plan['guard'] else {}),
                })
        plan = assigned['launch']
        require(assigned['run'] == run and assigned['checkout'] == str(checkout)
                and assigned['attempt'] == feature['attempt'], 'cascade assignment identity changed')
        provider, connection = _connection(checkout, plan['runtime']['settings'])
        _validate_launch(plan['runtime'], provider, connection)
        args = ('orchestration', 'task-create', '--run', run, '--task-title', f'{fid}: {role}', '--spec', spec)
        if not owner.startswith('orca_session_id:'):
            args += ('--from', owner)
        key = prefix + ':task'
        with session.transaction() as state:
            self._intent(state, key, args, owner)
        result = session.effect(key, validate=lambda data: self._task_receipt(data, run, spec))
        task = result['task']['id']
        attempt = Path(feature['directory'])
        launch = attempt / (role + '.launch.json')
        marker = attempt / (role + '.entry.json')
        attestation = attempt / (role + '.ready.json')
        launch_record = plan | {'worktree': assigned['worktree'], 'nonce': assigned['nonce'],
                                'checkout': str(checkout), 'entry': str(marker), 'ready': str(attestation)}
        if not launch.exists():
            atomic_json(launch, launch_record)
        require(read_json(launch) == launch_record, 'cascade launcher inputs changed')
        env = plan['environment']
        command = ['env', *[name + '=' + value for name, value in env.items()], sys.executable,
                   '-m', 'team_runner.runner_entry', str(marker), assigned['nonce'], '--',
                   sys.executable, '-m', 'team_runner.cascade_orca_entry', str(launch), _digest(launch_record)]
        terminal_key = prefix + ':terminal'
        args = ('terminal', 'create', '--worktree', 'id:' + assigned['worktree'], '--title',
                f'Cascade {fid} {role}', '--command', shlex.join(command))
        with session.transaction() as state:
            state['features'][fid]['roles'][role]['task'] = task
            self._intent(state, terminal_key, args, owner, marker=str(marker), nonce=assigned['nonce'],
                         checkout=str(checkout), worktree=assigned['worktree'], ready=str(attestation),
                         launch_digest=_digest(launch_record), launch=str(launch))
        result = session.effect(terminal_key,
                                validate=lambda data: self._terminal_receipt(data, assigned))
        terminal = _terminal_identity(result['terminal'])
        dispatch_key = prefix + ':dispatch'
        with session.transaction() as state:
            state['features'][fid]['roles'][role].update(terminal=terminal['handle'], terminal_identity=terminal)
        # If the original call reached worker-start, inspect it without waiting for
        # an idle prompt: the worker may already be executing that assignment.
        prior = session.read()['operations'].get(dispatch_key)
        if not prior or prior['status'] == 'intent':
            self._ready(session.read()['operations'][terminal_key], terminal, wait=True)
            self._checkout(session, feature, plan['sha'])
            if plan['guard']:
                from .cascade_verification import boundary
                require(boundary(checkout, candidate) == plan['before'],
                        'candidate manifest changed before dispatch')
        args = ('orchestration', 'worker-start', '--task', task, '--run', run, '--terminal', terminal['handle'],
                '--worktree', 'id:' + assigned['worktree'], '--timeout-ms', '60000')
        if not owner.startswith('orca_session_id:'):
            args += ('--from', owner)
        container = 'image' in (plan.get('guard') or {})
        with session.transaction() as state:
            extra = {'container': True} if container else {}
            self._intent(state, dispatch_key, args, owner, terminal=terminal, spec=spec, **extra)
        request = session.read()['operations'][dispatch_key]['request_id']
        result = session.effect(dispatch_key, validate=lambda data: self._dispatch_receipt(
            data, run, task, terminal, request if container else None,
        ))
        with session.transaction() as state:
            assigned = state['features'][fid]['roles'][role]
            assigned.update(dispatch=result['dispatchId'], process_incarnation=terminal['ptyId'] + ':' + terminal['incarnationId'])
        return dict(assigned)

    def _task_receipt(self, result, run, spec):
        task = result.get('task') or {}
        require(task.get('id') and task.get('run_id') == run and task.get('spec') == spec,
                'Task receipt differs from the saved operation')
        self._task(run, task['id'], spec)

    def _terminal_receipt(self, result, assignment):
        terminal = result.get('terminal') or {}
        # Headless Orca can return a terminal before its leaf is attached.
        # Resolve the panel through an exact live read of the same process.
        required = tuple(field for field in TERMINAL_FIELDS if field != 'leafId')
        require(all(isinstance(terminal.get(field), str) and terminal[field] for field in required),
                'Orca terminal creation receipt lacks a stable process identity')
        observed = self._terminal(terminal['handle'], assignment['worktree'], assignment['checkout'])
        require(all(observed[field] == terminal[field] for field in required)
                and terminal.get('leafId') in (None, observed['leafId']),
                'terminal create returned a different panel or incarnation')
        result['terminal'] = terminal | observed

    def _ready(self, operation, terminal, *, wait=False):
        evidence = operation['evidence']
        launch = None
        container = False
        if wait:
            launch = read_json(evidence['launch'], {})
            require(_digest(launch) == evidence['launch_digest'], 'cascade launch description changed')
            container = 'image' in (launch.get('guard') or {})
            if container:
                deadline = time.monotonic() + 12
                while not Path(evidence['ready']).is_file() and time.monotonic() < deadline:
                    time.sleep(.1)
            else:
                self._wait_tui(terminal)
        observed = self._terminal(terminal['handle'], evidence['worktree'], evidence['checkout'])
        marker = read_json(evidence['marker'], {})
        ready = read_json(evidence['ready'], {})
        require(observed == terminal and marker.get('terminal') == terminal
                and marker.get('nonce') == evidence['nonce'] == ready.get('nonce')
                and ready.get('launch_digest') == evidence['launch_digest']
                and type(marker.get('pid')) is int and marker.get('boot_id') == boot_id()
                and marker.get('process_stamp') and process_stamp(marker['pid']) == marker['process_stamp']
                and ready.get('pid') == marker['pid'],
                'expected terminal wrapper/process is not confirmed')
        if wait:
            if container:
                self._open_restricted(launch, terminal, marker['pid'])
                self._wait_tui(terminal)
            screen = self.call('terminal', 'read', '--terminal', terminal['handle'], '--screen').get('terminal') or {}
            require(screen.get('source') == 'screen' and
                    'account/read failed' not in ' '.join(screen.get('tail', [])).lower(),
                    'Codex startup screen unavailable or account read failed')
            if container:
                from .cascade_container import ready
                confirmed = ready(marker['pid'], launch)
            else:
                confirmed = _codex_pid(marker['pid'], launch) is not None
            require(confirmed,
                    'expected Codex argv, process and read-only mounts are unconfirmed')
        return marker

    def _wait_tui(self, terminal):
        result = self.call('terminal', 'wait', '--terminal', terminal['handle'],
                           '--for', 'tui-idle', '--timeout-ms', '45000')
        require((result.get('wait') or {}).get('satisfied') is True, 'Codex startup not confirmed')

    def _open_restricted(self, launch, terminal, pid):
        from .cascade_container import ready

        deadline = time.monotonic() + 45
        attested = submitted = False
        composer_since = None
        while time.monotonic() < deadline:
            attested = attested or ready(pid, launch)
            if attested:
                screen = self.call('terminal', 'read', '--terminal', terminal['handle'], '--screen').get('terminal') or {}
                lines = [line.strip() for line in screen.get('tail', [])]
                if screen.get('source') == 'screen':
                    if ('› Ask Codex to do anything' in lines
                            and any('OpenAI Codex (v' in line for line in lines)):
                        if composer_since is None:
                            composer_since = time.monotonic()
                        elif time.monotonic() - composer_since >= 1:
                            return
                    else:
                        composer_since = None
                    if not submitted and 'Folder access' in lines and '› 1. Open restricted' in lines:
                        require(self._terminal(terminal['handle'], terminal['worktreeId'], launch['checkout'])
                                == terminal and ready(pid, launch), 'restricted Codex startup changed')
                        request = str(uuid.uuid5(uuid.UUID(launch['nonce']), 'open-restricted'))
                        self.call('terminal', 'send', '--terminal', terminal['handle'], '--enter',
                                  '--text', ' ', '--retry-request', request)
                        submitted = True
            time.sleep(.25)
        raise ValueError('container Codex restricted startup was not confirmed')

    @staticmethod
    def container_input_accepted(data, request, terminal):
        prompt = data.get('prompt') or {}
        return (bool(request) and data.get('state') == 'outcome_unknown'
                and prompt.get('requestId') == request and prompt.get('provider') == 'codex'
                and prompt.get('stages') == ['input_accepted']
                and prompt.get('processIncarnation') == terminal['incarnationId'])

    def _dispatch_receipt(self, data, run, task, terminal, container_request=None):
        require(data.get('dispatchId') and data.get('runId') == run and data.get('taskId') == task,
                'worker-start receipt differs from the exact Task, Run and terminal')
        result = self._dispatch({'dispatch': data['dispatchId'], 'run': run, 'task': task, 'terminal': terminal['handle'],
                                 'worktree': terminal['worktreeId'], 'terminal_identity': terminal})
        # Orca may see input written without seeing the container UI start the
        # turn. Preserve the exact binding so its authenticated report can
        # settle it. Never submit or replay the prompt from this receipt.
        waiting = (self.container_input_accepted(data, container_request, terminal)
                   and result['dispatch'].get('status') == 'pending'
                   and result['worker'].get('state') == 'start_unknown')
        require(data.get('state') == 'ready' or result['worker'].get('state') in ('ready', 'succeeded', 'failed')
                or waiting,
                'Orca did not confirm task delivery')

    def _dispatch(self, assignment):
        result = self.call('orchestration', 'worker-show', '--dispatch', assignment['dispatch'])
        dispatch = result.get('dispatch') or {}
        worker = result.get('worker') or {}
        terminal = assignment['terminal_identity']
        require(dispatch.get('id') == assignment['dispatch']
                and dispatch.get('runId') == assignment['run'] and dispatch.get('taskId') == assignment['task']
                and dispatch.get('assigneeHandle') == assignment['terminal']
                and dispatch.get('assigneePaneKey') == _pane(terminal)
                and dispatch.get('processIncarnation') == terminal['ptyId'] + ':' + terminal['incarnationId']
                and worker.get('dispatchId') == assignment['dispatch']
                and worker.get('agentTerminalHandle') == assignment['terminal']
                and worker.get('worktreeId') == assignment['worktree'],
                'Orca Dispatch does not match the exact assignment and launch process')
        return result

    def assignment_identity(self, session, state, feature, assignment):
        require(assignment['execution'] == session.execution and assignment['attempt'] == feature['attempt']
                and assignment['checkout'] == feature['checkout']
                and assignment['terminal'] == os.environ.get('ORCA_TERMINAL_HANDLE')
                and os.environ.get('TEAM_RUNNER_ATTEMPT') == feature['directory']
                and os.environ.get('TEAM_RUNNER_PROJECT') == session.context.project_id
                and os.environ.get('TEAM_RUNNER_WORKSPACE') == session.context.workspace_id
                and os.environ.get('TEAM_RUNNER_CASCADE_EXECUTION') == session.execution
                and os.environ.get('TEAM_RUNNER_CASCADE_ROLE') == assignment['role']
                and os.environ.get('TEAM_RUNNER_CASCADE_FEATURE') == assignment['feature'],
                'caller differs from the registered cascade assignment')
        checkout = self._checkout(session, feature)
        require(git_root(Path.cwd()) == checkout and self._terminal(assignment['terminal'], assignment['worktree'], checkout)
                == assignment['terminal_identity'] and os.environ.get('ORCA_PANE_KEY') == _pane(assignment['terminal_identity']),
                'cascade worker terminal or checkout changed')
        result = self._dispatch(assignment)
        if assignment['role'] == 'coordinator' and feature.get('run'):
            self._run_current(feature['run'], assignment['terminal'])
        return result

    def role_done(self, session, assignment):
        result = self._dispatch(assignment)
        operation = session.read()['operations'].get(f"{assignment['feature']}:report:{assignment['dispatch']}")
        return (bool(operation and operation['status'] == 'completed')
                and self.validate_report(session, assignment, operation['result'])
                and result['dispatch'].get('status') == 'completed' and result['worker'].get('state') == 'succeeded')

    def validate_report(self, session, assignment, data):
        message = data.get('message') or {}
        payload = message.get('payload')
        if isinstance(payload, str):
            payload = json.loads(payload)
        require(message.get('id') and message.get('type') == 'worker_done'
                and message.get('from_handle') == assignment['terminal']
                and message.get('to_handle') == 'run:' + assignment['run'] and message.get('run_id') == assignment['run']
                and isinstance(payload, dict) and payload.get('taskId') == assignment['task']
                and payload.get('dispatchId') == assignment['dispatch']
                and payload.get('outcome') in ('succeeded', 'failed'), 'Orca worker_done receipt identity mismatch')
        self._dispatch(assignment)
        return message

    def reconcile(self, session, operation):
        from . import cascade_messages
        from .cascade import _recover_check

        args = operation['args']
        evidence = operation.get('evidence', {})
        mailbox = operation.get('mailbox')
        if args[:2] == ['verification', 'run']:
            return _recover_check(session, operation)
        if args[:2] == ['worktree', 'create']:
            return self._recover_worktree(session, evidence)
        if args[:2] == ['terminal', 'create']:
            marker = read_json(evidence['marker'], {})
            terminal = _terminal_identity(marker.get('terminal') or {})
            raw = {'terminal': terminal, 'recovered': True}
            try:
                self._ready(operation, terminal)
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                if session.read()['status'] != 'stopping':
                    raise
                self._closed(session, operation | {'result': raw})
            return raw
        if args[:2] == ['terminal', 'close']:
            return self._closed(session, evidence['launch'], require_container=False)
        if args[:2] == ['docker', 'container-rm']:
            return self._reap_container(evidence['launch'])
        require(args[0] == 'orchestration' and args[1] in METHODS, 'unknown cascade operation contract')
        receipt = session.call('orchestration', 'request-show', '--request', operation['request_id'], mailbox=mailbox)
        request_matches = (receipt.get('requestId') == operation['request_id']
                           and receipt.get('method') == 'orchestration.' + METHODS[args[1]])
        if receipt.get('state') == 'pending' and request_matches:
            if session.read()['status'] == 'active' and self._can_resume_pending(session, operation):
                return None
        if receipt.get('state') != 'completed':
            raise runner_operations.UncertainOperation(
                f"Orca request {operation['request_id']} is {receipt.get('state', 'unknown')}"
            )
        require(request_matches and isinstance(receipt.get('receipt'), dict),
                'Orca request scope or result is unknown')
        raw = runner_operations.receipt_result(receipt['receipt'])
        run = _flag(args, '--run')
        verb = args[1]
        if verb == 'run-create':
            self._validate_run_create(raw, mailbox, _flag(args, '--objective'))
        elif verb == 'task-create':
            self._task_receipt(raw, run, _flag(args, '--spec'))
        elif verb == 'worker-start':
            request = operation['request_id'] if evidence.get('container') else None
            self._dispatch_receipt(raw, run, _flag(args, '--task'), evidence['terminal'], request)
        elif verb == 'worker-stop':
            self._stopped(raw, _flag(args, '--dispatch'), evidence.get('external', False))
        elif operation.get('expected') is not None:
            raw = cascade_messages.recover_retrieval(session, operation, raw)
            cascade_messages.verify_operation(session, operation, raw)
        elif verb == 'send' and _flag(args, '--type') == 'worker_done':
            dispatch = _flag(args, '--dispatch-id')
            assignment = self._assigned(session, dispatch)
            payload = self.validate_report(session, assignment, raw)['payload']
            payload = json.loads(payload) if isinstance(payload, str) else payload
            require(raw['message'].get('body') == _flag(args, '--body')
                    and raw['message'].get('subject') == _flag(args, '--subject')
                    and payload['outcome'] == _flag(args, '--outcome')
                    and (raw.get('mutation') or {}).get('requestId') == operation['request_id'],
                    'reconciled worker_done receipt does not match the submitted report')
        else:
            raise runner_operations.UncertainOperation('Orca mutation receipt has no cascade validation contract')
        return raw

    def _can_resume_pending(self, session, operation):
        """Confirm scope before Orca compares the original payload hash on retry."""
        from .cascade_messages import ANCHOR_REQUEST_SUBJECT

        argv = operation['args']
        mailbox = operation['mailbox']
        state = session.read()
        run = _flag(argv, '--run')
        actor = None
        if mailbox == state['manager']['mailbox']:
            require(self.manager_identity(session.context) == state['manager'], 'Orca manager identity changed')
            actor = state['manager_run']
        else:
            matches = [(f, a) for f in state['features'].values() for a in f['roles'].values()
                       if mailbox and a.get('terminal') == mailbox and a.get('dispatch')]
            require(len(matches) == 1, 'original Orca caller assignment is missing or ambiguous')
            feature, assignment = matches[0]
            self.assignment_identity(session, state, feature, assignment)
            require(self._dispatch(assignment)['dispatch'].get('status') == 'dispatched',
                    'original Orca caller Dispatch is no longer active')
            actor = feature['run'] if assignment['role'] == 'coordinator' else assignment['run']
            if argv[1] == 'ask':
                require(run == assignment['run'], 'pending ask would use a different parent Run')
                actor = run
            expected = (operation.get('expected') or {}).get('message') or {}
            if argv[1] == 'send' and expected.get('subject') == ANCHOR_REQUEST_SUBJECT:
                require(assignment['role'] == 'coordinator' and run == assignment['run']
                        and _flag(argv, '--to') == 'run:' + run,
                        'pending anchor request is outside the assigned parent Run')
                actor = run
        if argv[1] == 'run-create':
            return actor is None
        if argv[1] in ('task-create', 'worker-start', 'reply', 'send'):
            require(actor and actor == run, 'pending operation is outside the active Run')
        elif argv[1] != 'ask':
            return False
        if argv[1] in ('task-create', 'worker-start'):
            self._run_current(run, mailbox)
        if argv[1] == 'worker-start':
            terminal = operation['evidence']['terminal']
            feature = next(f for f in state['features'].values()
                           if f.get('worktree', {}).get('id') == terminal['worktreeId'])
            self._checkout(session, feature)
            require(self._terminal(terminal['handle'], terminal['worktreeId'], feature['checkout']) == terminal,
                    'pending worker-start terminal has changed')
            self._task(run, _flag(argv, '--task'), operation['evidence']['spec'])
        target = _flag(argv, '--to')
        expected = (operation.get('expected') or {}).get('message') or {}
        if not target:
            target = expected.get('to_handle')
        if target and target.startswith('dispatch:'):
            assignment = self._assigned(session, target.removeprefix('dispatch:'))
            require(assignment['run'] == run and self._dispatch(assignment)['dispatch'].get('status') == 'dispatched',
                    'pending delivery target is no longer its original Dispatch')
        if argv[1] == 'reply' and not (target and target.startswith('dispatch:')):
            return False
        return True

    def _assigned(self, session, dispatch):
        matches = [a for f in session.read()['features'].values() for a in f['roles'].values()
                   if a.get('dispatch') == dispatch]
        require(dispatch and len(matches) == 1, 'Orca Dispatch is not in this cascade')
        return matches[0]

    def _stopped(self, result, dispatch, external=False):
        state = result.get('state')
        external_fenced = (external and state == 'stop_unknown' and result.get('processAction') == 'none'
                           and result.get('lastError') == 'The worker terminal is external; no terminal was closed.')
        require(result.get('dispatchId') == dispatch and not result.get('helperStopError')
                and (state in ('stopped', 'succeeded', 'failed') or external_fenced),
                f'Orca worker-stop response for {dispatch} is unresolved')
        return result

    def _stop_worker(self, session, operation):
        args = (*operation['args'], '--retry-request', operation['request_id'])
        try:
            return session.call(*args, mailbox=operation['mailbox'])
        except RuntimeError as exc:
            # Orca fences a supervised Dispatch before declining to close its
            # external terminal. The launcher must independently prove local exit.
            receipt = getattr(exc, 'receipt', {})
            result = receipt.get('result') or {}
            if (receipt.get('ok') is True and operation['evidence']['external']
                    and result.get('state') == 'stop_unknown'):
                return self._stopped(result, operation['evidence']['dispatch'], True)
            raise

    def _terminal_container(self, launch):
        evidence = launch['evidence']
        if evidence.get('launch'):
            plan = read_json(evidence['launch'])
            require(_digest(plan) == evidence['launch_digest'], 'cascade launch description changed')
            guard = (plan.get('guard') or {})
            if 'image' in guard:
                return guard
        return None

    def _departed_wrapper(self, launch):
        from .project_services import departed

        evidence = launch['evidence']
        terminal = _terminal_identity(launch['result']['terminal'])
        marker = read_json(evidence['marker'], {})
        require(marker.get('terminal') == terminal and marker.get('nonce') == evidence['nonce']
                and departed(marker), f"terminal {terminal['handle']} process exit is unconfirmed")
        return marker

    def _reap_container(self, launch):
        from .cascade_container import reap

        self._departed_wrapper(launch)
        guard = self._terminal_container(launch)
        require(guard is not None, 'cleanup requires a frozen verifier container')
        return reap(guard)

    def _closed(self, session, launch, *, require_container=True):
        terminal = _terminal_identity(launch['result']['terminal'])
        marker = self._departed_wrapper(launch)
        guard = self._terminal_container(launch) if require_container else None
        if guard:
            from .cascade_container import absent

            require(absent(guard), f"verifier container {guard['name']} exit is unconfirmed")
        rows = _scope_rows(self.call('terminal', 'list', '--worktree', 'id:' + terminal['worktreeId'],
                                     '--limit', '4096', '--include-visual-layouts'), 'terminals')
        require(not any(row.get('handle') == terminal['handle'] or row.get('incarnationId') == terminal['incarnationId']
                        or (row.get('tabId'), row.get('leafId')) == (terminal['tabId'], terminal['leafId'])
                        for row in rows), f"terminal {terminal['handle']} panel remains present")
        return {'terminal': terminal, 'process': {k: marker[k] for k in ('pid', 'process_stamp', 'boot_id')}}

    def _inventory(self, run):
        """Resource accounting includes terminal-less or externally adopted Dispatches."""
        cursor = None
        seen = set()
        while True:
            args = ['orchestration', 'worker-list', '--run', run, '--limit', '100']
            if cursor:
                args += ['--cursor', cursor]
            page = self.call(*args)
            workers = page.get('workers')
            require(isinstance(workers, list) and all(row.get('runId') == run and row.get('dispatchId')
                                                     for row in workers),
                    'Orca Run inventory contains unexpected dispatch data')
            yield from workers
            pagination = page.get('page') or {}
            require(type(pagination.get('hasMore')) is bool, 'Orca Run inventory pagination is unconfirmed')
            if not pagination['hasMore']:
                break
            cursor = pagination.get('nextCursor')
            require(cursor and cursor not in seen, 'Orca Run inventory pagination is incomplete')
            seen.add(cursor)

    def _unassigned_terminal(self, terminal):
        # terminal show has no resource owner field. Enumerate the explicitly
        # named local Runs and reject any current worker using this exact pane.
        cursor = None
        seen = set()
        while True:
            args = ['orchestration', 'run-list', '--limit', '100']
            if cursor:
                args += ['--cursor', cursor]
            page = self.call(*args)
            require(isinstance(page.get('runs'), list) and 'nextCursor' in page,
                    'complete Run inventory unavailable for pre-dispatch terminal')
            for run in page['runs']:
                require(isinstance(run.get('id'), str) and run['id'] not in seen,
                        'Run inventory changed during terminal ownership verification')
                seen.add(run['id'])
                for worker in self._inventory(run['id']):
                    resource = worker.get('resource') or {}
                    require(terminal['handle'] not in (worker.get('agentTerminalHandle'), resource.get('terminalHandle'))
                            and resource.get('endpointIncarnation') != terminal['incarnationId'],
                            'pre-dispatch terminal has an Orca owner')
            next_cursor = page['nextCursor']
            if next_cursor is None:
                return
            require(isinstance(next_cursor, str) and next_cursor != cursor,
                    'Run inventory cursor is missing or did not advance')
            cursor = next_cursor

    def cleanup(self, session, stopping=False, operator=False):
        state = session.read()
        registered = {}
        owner = state['manager']['mailbox']
        try:
            authorized = self.manager_identity(session.context) == state['manager']
            if authorized and state.get('manager_run'):
                self._run_current(state['manager_run'], owner)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            authorized = False
        mutation_mailbox = owner
        if operator and not authorized:
            # On the verified local runtime, exact workspace reads and these
            # stop/close methods require the same workspace permission. The
            # recorded Run and resource reads below prove the authorized scope.
            require(stopping and self.inbox_window, 'operator Orca workspace authority unavailable')
            session.context.verify(self.call)
            authorized = True
            mutation_mailbox = None

        # Recover operation outcomes even when a receipt was saved immediately
        # before termination fenced the assignment registry update.
        runs = {state['manager_run']: (owner, f'Team Runner cascade {session.execution}/manager')} if state['manager_run'] else {}
        launches = {}
        for key, operation in state['operations'].items():
            if operation['status'] == 'cancelled':
                continue
            args = operation['args']
            if operation['status'] == 'intent' and args[:2] in (
                ['orchestration', 'worker-stop'], ['terminal', 'close'], ['docker', 'container-rm']
            ):
                continue
            require(operation['status'] == 'completed', f'unsettled cascade effect: {key}')
            if args[:2] == ['orchestration', 'run-create']:
                runs[operation['result']['run']['id']] = (operation['mailbox'], _flag(args, '--objective'))
            elif args[:2] == ['terminal', 'create']:
                launches[key.removesuffix(':terminal')] = operation
            elif args[:2] == ['orchestration', 'worker-start']:
                prefix = key.removesuffix(':dispatch')
                terminal = operation['evidence']['terminal']
                dispatch = operation['result']['dispatchId']
                require(dispatch not in registered, 'duplicate cascade Dispatch in saved inventory')
                registered[dispatch] = {
                    'dispatch': dispatch, 'run': _flag(args, '--run'), 'task': _flag(args, '--task'),
                    'terminal': terminal['handle'], 'terminal_identity': terminal,
                    'worktree': terminal['worktreeId'], 'prefix': prefix,
                }

        for run, (expected_owner, objective) in runs.items():
            require(self._run(run, expected_owner).get('objective') == objective, 'Orca Run objective changed')
            dispatches = [row['dispatchId'] for row in self._inventory(run)]
            expected = {d for d, record in registered.items() if record['run'] == run}
            require(len(dispatches) == len(set(dispatches)) and set(dispatches) == expected,
                    f'Orca Run {run} has unaccounted or missing Dispatches')

        proofs = []
        proven_dispatches = set()
        # Child Run workers before their coordinator, whose stop can also return
        # helper accounting. The manager's own terminal is never cascade-owned.
        manager_run = state['manager_run']
        bindings = {record['prefix']: record for record in registered.values()}
        for prefix, launch in sorted(launches.items(), key=lambda pair:
                                     bindings.get(pair[0], {}).get('run') == manager_run):
            record = bindings.get(prefix)
            try:
                exit_proof = self._closed(session, launch)
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                exit_proof = None
            stop_proof = None
            terminal = _terminal_identity(launch['result']['terminal'])
            if record is not None:
                current = self._dispatch(record)
                dispatch = record['dispatch']
                resource = current.get('terminalResource') or {}
                external = (resource.get('ownershipState') == 'external'
                            and resource.get('ownerDispatchId') == dispatch)
                if (resource.get('ownershipState') == 'user_owned' or
                        resource.get('retainedReason') in ('keep_open', 'kept_open', 'user_kept_open', 'user_requested')):
                    require(exit_proof, f'worker {dispatch}: user-kept terminal may still run')
                fenced = (current['dispatch'].get('status') in STOPPED_DISPATCH
                          or bool(current['dispatch'].get('capabilityRevokedAt')))
                key = prefix + ':stop'
                previous = state['operations'].get(key)
                if previous and previous['status'] != 'completed':
                    previous = None
                if previous:
                    stop_proof = self._stopped(previous['result'], dispatch, previous['evidence']['external'])
                elif not fenced or (not exit_proof and not external):
                    require(authorized, f'worker {dispatch}: Orca mutation authority unavailable')
                    with session.transaction(stopping=stopping) as latest:
                        key = self._cleanup_key(latest, key, mutation_mailbox)
                        self._intent(latest, key, ('orchestration', 'worker-stop', '--dispatch', dispatch),
                                     mutation_mailbox, dispatch=dispatch, external=external)
                    operation = session.read()['operations'][key]
                    stop_proof = session.effect(
                        key, stopping=stopping, invoke=lambda: self._stop_worker(session, operation),
                        validate=lambda result: self._stopped(result, dispatch, external),
                    )
                    current = self._dispatch(record)
                    fenced = (current['dispatch'].get('status') in STOPPED_DISPATCH
                              or bool(current['dispatch'].get('capabilityRevokedAt')))
                    try:
                        exit_proof = self._closed(session, launch)
                    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                        exit_proof = None
                require(fenced, f'worker {dispatch} Dispatch is not fenced')
                if stop_proof:
                    require(all(helper.get('dispatchId') in proven_dispatches
                                for helper in stop_proof.get('helpers', [])),
                            f'worker {dispatch} has unaccounted helper exits')
                require(exit_proof or external,
                        f'worker {dispatch}: Orca stop has not proved runner process exit')
            if not exit_proof:
                require(authorized, f"terminal {terminal['handle']}: Orca mutation authority unavailable")
                guard = self._terminal_container(launch)
                # An adopted terminal may stay external after its Dispatch settles.
                # A launch with an orphaned container can also have an already
                # closed pane or a live pane whose original wrapper has exited.
                panel_closed = False
                if guard:
                    try:
                        self._closed(session, launch, require_container=False)
                        panel_closed = True
                    except ValueError:
                        pass
                if not panel_closed:
                    try:
                        self._ready(launch, terminal)
                    except ValueError:
                        if not guard:
                            raise
                        self._departed_wrapper(launch)
                        require(self._terminal(terminal['handle'], terminal['worktreeId'], guard['checkout'])
                                == terminal, 'orphaned verifier pane changed')
                    if record is None:
                        self._unassigned_terminal(terminal)
                    else:
                        resource = (self._dispatch(record).get('terminalResource') or {})
                        require(resource.get('ownershipState') == 'external'
                                and resource.get('ownerDispatchId') == record['dispatch']
                                and resource.get('retainedReason') not in ('keep_open', 'kept_open', 'user_kept_open',
                                                                          'user_requested'),
                                'settled terminal is no longer externally owned by the exact assignment')
                    key = prefix + ':close'
                    with session.transaction(stopping=stopping) as latest:
                        key = self._cleanup_key(latest, key, mutation_mailbox)
                        self._intent(latest, key, ('terminal', 'close', '--terminal', terminal['handle']),
                                     mutation_mailbox, launch=launch)
                    session.effect(key, stopping=stopping,
                                   validate=lambda result: self._validate_close(result, terminal))
                if guard:
                    key = prefix + ':container-rm'
                    with session.transaction(stopping=stopping) as latest:
                        key = self._cleanup_key(latest, key, mutation_mailbox)
                        self._intent(latest, key, ('docker', 'container-rm', guard['name']),
                                     mutation_mailbox, launch=launch)
                    session.effect(key, stopping=stopping, invoke=lambda: self._reap_container(launch))
                exit_proof = self._closed(session, launch)
            proofs.append({'prefix': prefix, 'dispatch': record['dispatch'] if record else None,
                           'worker_stop': stop_proof, 'exit': exit_proof})
            if record is not None:
                proven_dispatches.add(record['dispatch'])
        require(all(record['prefix'] in launches for record in registered.values()), 'missing cascade launch proof')
        return {'resources_exited': True, 'runs': sorted(runs), 'terminals': proofs}

    @staticmethod
    def _cleanup_key(state, key, mailbox):
        existing = state['operations'].get(key)
        if state['status'] == 'stopping' and existing and existing['mailbox'] != mailbox:
            if existing['status'] in ('intent', 'cancelled'):
                existing['status'] = 'cancelled'
            return key + (':operator' if mailbox is None else ':manager')
        return key

    @staticmethod
    def _validate_close(result, terminal):
        close = result.get('close') or {}
        require(close.get('handle') == terminal['handle'] and close.get('tabId') == terminal['tabId']
                and close.get('ptyKilled') is True, 'terminal close receipt differs from the original terminal')


def _validate_launch(frozen, provider, connection):
    require(provider == frozen['settings']['provider'] and connection == frozen['connection'],
            'Codex provider, access or connection inputs changed')
    binary = frozen['binary']
    require(sha256(Path(binary).read_bytes()).hexdigest() == frozen['binary_sha256']
            and _version(binary) == frozen['version'], 'Codex client changed since the cascade snapshot')
