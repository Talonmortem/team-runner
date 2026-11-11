"""Persistent project/workspace identities; canonical checkout ownership."""
from contextlib import contextmanager
from dataclasses import dataclass
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .runner_store import atomic_json, read_json, lock, running


def roots():
    return (Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'team-runner',
            Path(os.environ.get('TEAM_RUNNER_STATE_ROOT',
                                Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'team-runner')))


def identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid project/workspace ID') from None
    return value


def git_root(value):
    path = Path(value).expanduser().resolve(strict=True)
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True, check=True)
    if Path(result.stdout.strip()).resolve() != path:
        raise ValueError('workspace must be an existing Git checkout root')
    return path


PROFILE = {'instructions': [], 'requirements': [], 'tracking_root': 'feat', 'evidence_dir': 'evidence',
           'acceptance': {'require_commit': False, 'require_clean': False}, 'checks': [],
           'workflow': {'allow_commits': False}, 'constraints': [], 'runtime': {'go_cache': None}}


def profile(value):
    if not isinstance(value, dict) or set(value) - set(PROFILE):
        raise ValueError('unknown profile field')
    result = copy.deepcopy(PROFILE)
    result.update(value)
    from .agent_runtime import validate as validate_runtime
    result['runtime'] = validate_runtime(result['runtime'])
    for key in ('instructions', 'requirements', 'constraints'):
        if not isinstance(result[key], list) or any(not isinstance(x, str) for x in result[key]):
            raise ValueError(key + ' must be a list of strings')
    for key in ('tracking_root', 'evidence_dir'):
        p = Path(result[key])
        if not result[key] or p.is_absolute() or '..' in p.parts:
            raise ValueError(key + ' must be a relative directory')
    for key, allowed in [('acceptance', {'require_commit', 'require_clean'}), ('workflow', {'allow_commits', 'sequential_agents'})]:
        if not isinstance(result[key], dict) or set(result[key]) - allowed or any(type(v) is not bool for v in result[key].values()):
            raise ValueError('invalid ' + key)
        result[key] = {**PROFILE[key], **result[key]}
    if result['acceptance']['require_commit'] and not result['workflow']['allow_commits']:
        raise ValueError('commit acceptance requires allow_commits')
    if not isinstance(result['checks'], list):
        raise ValueError('checks must be a list')
    for check in result['checks']:
        if (not isinstance(check, dict) or not {'cwd', 'argv'} <= set(check) or set(check) - {'cwd', 'argv', 'scope'} or
                not isinstance(check['argv'], list) or not check['argv'] or
                any(not isinstance(x, str) or not x for x in check['argv'])):
            raise ValueError('check requires cwd and argv list')
        if check.get('scope', 'item') not in ('item', 'stage'):
            raise ValueError('check scope must be item or stage')
        p = Path(check['cwd'])
        if p.is_absolute() or '..' in p.parts:
            raise ValueError('check cwd must stay in checkout')
    return result


def roles(value, repo=None, config_root=None, layers=()):
    from .runner_config import defaults, validate, merge_role, FIELDS, OPTIONAL_FIELDS, ROLES
    if not isinstance(value, dict) or set(value) - set(ROLES):
        raise ValueError('invalid role overrides')
    cfg = defaults()
    for layer in layers:
        for role, fields in layer.items():
            merge_role(cfg['roles'][role], fields)
    for role, fields in value.items():
        if not isinstance(fields, dict) or set(fields) - FIELDS - OPTIONAL_FIELDS:
            raise ValueError('invalid role settings')
        merge_role(cfg['roles'][role], fields)
    validate(cfg, repo=repo, config_root=config_root)
    return copy.deepcopy(value)


def unsettled(state_dir):
    st = read_json(Path(state_dir) / 'state.json', {})
    startup = read_json(Path(state_dir) / 'startup.json', {})
    if running(state_dir) or startup.get('status') in ('starting', 'unknown'):
        return True
    if st.get('control_uncertain') or st.get('stop_status') == 'unknown' or st.get('recovery_issues'):
        return True
    cur = st.get('current') or {}
    if cur and st.get('stop_status') != 'stopped':
        return True
    for p in [Path(state_dir) / 'operations.json', Path(state_dir) / 'startup/operations.json']:
        if any(op.get('status') != 'completed' for op in read_json(p, {}).values()):
            return True
    return False


@dataclass(frozen=True)
class Context:
    project_id: str
    workspace_id: str
    repo: Path
    state_dir: Path
    config_root: Path
    state_root: Path
    orca_id: str
    orca_instance: str
    profile: dict
    role_layers: tuple

    @property
    def settings_dir(self):
        return self.config_root / 'projects' / self.project_id / 'workspaces' / self.workspace_id

    @property
    def checkout_key(self):
        return hashlib.sha256(str(self.repo).encode()).hexdigest()

    @property
    def checkout_lock(self):
        return self.state_root / 'checkouts' / (self.checkout_key + '.lock')

    @property
    def lease_path(self):
        return self.state_root / 'checkouts' / (self.checkout_key + '.json')

    def identity(self):
        return {'project_id': self.project_id, 'workspace_id': self.workspace_id, 'path': str(self.repo),
                'orca_id': self.orca_id, 'orca_instance': self.orca_instance}

    def environment(self, attempt=None):
        env = {'TEAM_RUNNER_PROJECT': self.project_id, 'TEAM_RUNNER_WORKSPACE': self.workspace_id,
               'TEAM_RUNNER_STATE': str(self.state_dir), 'TEAM_RUNNER_CHECKOUT': str(self.repo),
               'XDG_CONFIG_HOME': str(self.config_root.parent), 'XDG_STATE_HOME': str(self.state_root.parent),
               'ORCA_WORKTREE_ID': self.orca_id,
               'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
        if attempt:
            env['TEAM_RUNNER_ATTEMPT'] = str(attempt)
            env['TEAM_RUNNER_MATERIALS'] = str(Path(attempt) / 'materials')
        return env

    def verify(self, transport):
        if git_root(self.repo) != self.repo:
            raise ValueError('checkout identity changed')
        wt = transport('worktree', 'show', '--worktree', 'id:' + self.orca_id).get('worktree') or {}
        if (wt.get('id') != self.orca_id or wt.get('instanceId') != self.orca_instance or
                wt.get('hostId') != 'local' or Path(wt.get('path', '/nonexistent')).resolve() != self.repo or wt.get('isArchived')):
            raise ValueError('Orca address/path/incarnation does not match registered checkout')
        return wt

    def snapshot(self, attempt):
        from .runner_store import atomic_json
        attempt = Path(attempt)
        value = {'identity': self.identity(), 'profile': copy.deepcopy(self.profile), 'instructions': []}
        for key in ('instructions', 'requirements'):
            for name in self.profile[key]:
                path = Path(name).expanduser()
                if not path.is_absolute():
                    path = self.repo / path
                body = path.read_text()
                value['instructions'].append({'kind': key, 'path': str(path.resolve()), 'text': body,
                                             'sha256': hashlib.sha256(body.encode()).hexdigest()})
        # Copy common materials so a package update cannot alter an active attempt's rules.
        import shutil
        package = Path(__file__).parent
        for name in ('GOAL_MASTER.md', 'roles-and-rules.md', 'MODELS.md', 'POLICY.md', 'worker-brief.md', 'defect-classes.md', 'roles', 'templates'):
            source = package / name
            target = attempt / 'materials' / name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        atomic_json(attempt / 'context.json', value)
        return value

    def check_attempt(self, attempt):
        attempt = Path(attempt).resolve()
        if attempt.parent != (self.state_dir / 'attempts').resolve():
            raise ValueError('attempt is outside workspace namespace')
        data = read_json(attempt / 'context.json', {})
        if data.get('identity') != self.identity():
            raise ValueError('attempt context does not match project/workspace/Orca identity')
        current = read_json(self.state_dir / 'state.json', {}).get('current') or {}
        if current.get('attempt_dir') != str(attempt):
            raise ValueError('attempt is no longer current')
        return data


class Catalog:
    def __init__(self, config_root=None, state_root=None):
        cfg, state = roots()
        self.config_root = Path(config_root or cfg).resolve()
        self.state_root = Path(state_root or state).resolve()
        self.path = self.config_root / 'catalog.json'

    def read(self):
        data = read_json(self.path, {'version': 1, 'projects': {}})
        for project in data['projects'].values():
            project.setdefault('service_port', 0)
        return data

    def state_dir(self, project_id, workspace_id):
        return self.state_root / 'projects' / identifier(project_id) / 'workspaces' / identifier(workspace_id)

    def context(self, project_id, workspace_id, allow_disabled=False):
        data = self.read()
        try:
            p = data['projects'][identifier(project_id)]
            w = p['workspaces'][identifier(workspace_id)]
        except KeyError:
            raise ValueError('unknown project/workspace') from None
        if not allow_disabled and w['disabled']:
            raise ValueError('workspace disabled')
        return Context(project_id, workspace_id, Path(w['path']), self.state_dir(project_id, workspace_id),
                       self.config_root, self.state_root, w['orca_id'], w['orca_instance'],
                       profile({**p['profile'], **w['profile'], **{key: {**p['profile'].get(key, {}), **w['profile'].get(key, {})} for key in ('acceptance', 'workflow')}}), (p['roles'], w['roles']))

    def project(self, name=None, project_id=None, values=None):
        values = dict(values or {})
        if set(values) - {'name', 'profile', 'roles', 'service_port'}:
            raise ValueError('unknown project field')
        if 'service_port' in values and (type(values['service_port']) is not int or not 0 <= values['service_port'] <= 65535):
            raise ValueError('service_port must be an integer from 0 to 65535')
        with lock(self.config_root / 'catalog.lock'):
            data = self.read()
            if project_id is None:
                project_id = str(uuid.uuid4())
                data['projects'][project_id] = {'id': project_id, 'name': name or 'Project', 'profile': {}, 'roles': {}, 'workspaces': {}, 'service_port': 0}
            p = data['projects'].get(identifier(project_id))
            if p is None:
                raise ValueError('unknown project')
            if 'profile' in values: profile(values['profile'])
            if 'roles' in values:
                workspaces = list(p['workspaces'].values())
                if not workspaces:
                    roles(values['roles'], config_root=self.config_root.parent)
                for workspace in workspaces:
                    roles(values['roles'], repo=workspace['path'], config_root=self.config_root.parent)
                    roles(workspace['roles'], repo=workspace['path'], config_root=self.config_root.parent,
                          layers=(values['roles'],))
            p.update(values)
            if not isinstance(p['name'], str) or not p['name'].strip(): raise ValueError('name required')
            atomic_json(self.path, data)
            return copy.deepcopy(p)

    def workspace(self, project_id, values, workspace_id=None):
        values = dict(values)
        allowed = {'name', 'profile', 'roles', 'orca_id', 'orca_instance'} | ({'path'} if workspace_id is None else set())
        if set(values) - allowed:
            raise ValueError('unknown/immutable workspace field; attach a new directory to change path')
        with lock(self.config_root / 'catalog.lock'):
            data = self.read()
            p = data['projects'].get(identifier(project_id))
            if p is None: raise ValueError('unknown project')
            if workspace_id is None:
                path = git_root(values.pop('path'))
                workspace_id = str(uuid.uuid4())
                w = {'id': workspace_id, 'name': path.name, 'path': str(path), 'disabled': False,
                     'profile': {}, 'roles': {}, 'orca_id': '', 'orca_instance': ''}
                p['workspaces'][workspace_id] = w
            else:
                w = p['workspaces'].get(identifier(workspace_id))
                if w is None: raise ValueError('unknown workspace')
                if ('orca_id' in values or 'orca_instance' in values) and unsettled(self.state_dir(project_id, workspace_id)):
                    raise ValueError('cannot change Orca address while active or unknown')
            if 'profile' in values: profile(values['profile'])
            if 'roles' in values or p['roles'] or w['roles']:
                roles(values.get('roles', w['roles']), repo=w['path'], config_root=self.config_root.parent,
                      layers=(p['roles'],))
            w.update(values)
            if not w['orca_id'] or not w['orca_instance']:
                from .orca import call
                rows = call('worktree', 'list').get('worktrees', [])
                rows = [row for row in rows if row.get('hostId') == 'local' and
                        Path(row.get('path', '/nonexistent')).resolve() == Path(w['path']) and
                        (not w['orca_id'] or row.get('id') == w['orca_id']) and not row.get('isArchived')]
                if len(rows) != 1:
                    raise ValueError('exact Orca address ambiguous or unavailable; provide ID and instance')
                w.update(orca_id=rows[0]['id'], orca_instance=rows[0]['instanceId'])
            for key in ('name', 'orca_id', 'orca_instance'):
                if not isinstance(w[key], str) or not w[key].strip(): raise ValueError(key + ' required')
            if w['orca_id'] in ('current', 'active'): raise ValueError('explicit Orca ID required')
            atomic_json(self.path, data)
            return copy.deepcopy(w)

    def disable(self, project_id, workspace_id):
        with lock(self.config_root / 'catalog.lock'):
            context = self.context(project_id, workspace_id, allow_disabled=True)
            with lock(context.checkout_lock, blocking=False):
                if unsettled(context.state_dir):
                    raise ValueError('active or unknown attempt; reconcile before disabling')
                data = self.read()
                w = data['projects'][project_id]['workspaces'][workspace_id]
                w['disabled'] = True
                atomic_json(self.path, data)
                return w

    def claim(self, context):
        """Caller holds checkout lock. The durable lease survives process death."""
        previous = read_json(context.lease_path)
        if previous and previous['identity'] != context.identity():
            old = previous['identity']
            if unsettled(self.state_dir(old['project_id'], old['workspace_id'])):
                raise ValueError('canonical checkout belongs to an active or unknown attempt')
        atomic_json(context.lease_path, {'identity': context.identity()})
