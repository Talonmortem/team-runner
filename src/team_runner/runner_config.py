"""Secret-free role configuration, shared by the CLI and HTTP panel (Python 3.11+)."""
import copy
import json
import os
from pathlib import Path
import re
import tomllib

DEFAULTS = Path(__file__).with_name('runner-defaults.json')
ROLES = ('coordinator', 'planner', 'explorer', 'implementer_standard',
         'implementer_medium', 'implementer_complex', 'tester', 'final_reviewer', 'escalation', 'completion_checker',
         'tester_complex', 'final_reviewer_critical', 'escalation_expert')
FIELDS = {'agent', 'profile', 'provider', 'model', 'effort'}
OPTIONAL_FIELDS = {'fast'}
_CLAUDE_NO_FAST = ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                   'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_DISABLE_FAST_MODE')
EFFORTS = {'codex': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
           'claude': ('low', 'medium', 'high', 'xhigh', 'max'),
           'opencode2': ('default', 'medium', 'high', 'xhigh')}


def sber_allowed(repo):
    if repo is None:
        return False
    try:
        checkout = Path(repo).expanduser().resolve(strict=True)
        return checkout.is_dir() and checkout.is_relative_to((Path.home() / 'repo/sber').resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def validate_role_scope(cfg, repo, catalog=None):
    provider = cfg.get('provider')
    if cfg.get('agent') == 'codex' and not provider:
        catalog = catalog if catalog is not None else connections(repo=repo)
        provider = ((catalog.get('profiles', {}).get(cfg.get('profile'), {}) or {}).get('provider')
                    if cfg.get('profile') else catalog.get('current_provider'))
        if provider is None:
            home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
            base = tomllib.loads((home / 'config.toml').read_text()) if (home / 'config.toml').exists() else {}
            profile_name = cfg.get('profile')
            data = base.get('profiles', {}).get(profile_name, {}) if profile_name else {}
            # Profile names are references, never arbitrary paths here.
            if profile_name and Path(profile_name).name == profile_name:
                source = home / (profile_name + '.config.toml')
                if source.exists():
                    data = tomllib.loads(source.read_text())
            provider = data.get('model_provider', base.get('model_provider'))
    if (cfg.get('agent') == 'opencode2' or provider == 'cloudru'
            or str(cfg.get('model', '')).startswith('cloudru/')) and not sber_allowed(repo):
        raise ValueError('OpenCode 2 and Cloud.ru require a checkout under ~/repo/sber')


def opencode_config_path(config_root=None):
    return Path(config_root or os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'opencode/opencode.json'


def opencode_connection(config_root=None):
    try:
        data = json.loads(opencode_config_path(config_root).read_text())
        providers = data.get('providers', data.get('provider', {}))
        cloud = providers.get('cloudru', {})
        models = sorted(model for model in cloud.get('models', {})
                        if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}', model))
        current = data.get('model', '')
        current = current.removeprefix('cloudru/') if isinstance(current, str) and current.startswith('cloudru/') else ''
        return {'providers': ['cloudru'], 'models': models,
                'default_model': current if current in models else (models[0] if models else '')}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'providers': ['cloudru'], 'models': [], 'default_model': ''}


def defaults():
    return json.loads(DEFAULTS.read_text())


def templates():
    return [{'id': 'current', 'name': 'Текущие агенты'}, {'id': 'cloudru', 'name': 'Cloud.ru'},
            {'id': 'openai-only', 'name': 'OpenAI-only'}]


def get_template(name):
    name = {'Текущие агенты': 'current', 'Cloud.ru': 'cloudru', 'OpenAI-only': 'openai-only'}.get(name, name)
    if name not in {'current', 'cloudru', 'openai-only'}:
        raise ValueError('unknown built-in template')
    cfg = defaults()
    cfg['template'] = name
    if name == 'current':
        return cfg
    if name == 'openai-only':
        for role in ('coordinator', 'final_reviewer_critical', 'escalation_expert'):
            cfg['roles'][role] = {'agent': 'codex', 'profile': '', 'provider': 'codex-lb',
                                  'model': 'gpt-6.1-sol',
                                  'effort': 'medium' if role == 'coordinator' else 'high'}
        return cfg
    models = {'opus': 'claude-opus-4.8', 'gpt': 'gpt-5.5',
              'flash': 'deepseek-v4.1-flash'}
    assignments = {
        'coordinator': ('opus', None), 'planner': ('opus', 'gpt'),
        'explorer': ('flash', 'opus'), 'implementer_standard': ('flash', 'opus'),
        'implementer_medium': ('opus', 'gpt'), 'implementer_complex': ('opus', 'gpt'),
        'tester': ('gpt', 'opus'), 'tester_complex': ('gpt', 'opus'),
        'final_reviewer': ('opus', 'gpt'), 'final_reviewer_critical': ('opus', 'gpt'),
        'escalation': ('gpt', 'opus'), 'escalation_expert': ('opus', 'gpt'),
        'completion_checker': ('flash', 'opus')}
    def settings(model, role):
        return {'agent': 'opencode2', 'profile': '', 'provider': 'cloudru',
                'model': models[model], 'effort': ('xhigh' if model == 'gpt' else 'high')
                if role in ('coordinator', 'planner') else 'medium'}
    for role, (primary, reserve) in assignments.items():
        cfg['roles'][role] = settings(primary, role)
        cfg['reserves'][role] = settings(reserve, role) if reserve else None
    return cfg


def template_config(name, repo=None, catalog=None, config_root=None):
    return validate(get_template(name), catalog, repo, config_root)


def fast_models(home):
    """Read only capability metadata; never expose cache identity or credentials."""
    models = set()
    try:
        data = json.loads((Path(home) / 'models_cache.json').read_text())
        for entry in data.get('models', []):
            if not isinstance(entry, dict):
                continue
            slug = entry.get('slug')
            tiers = entry.get('service_tiers', [])
            if (isinstance(slug, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}', slug)
                    and isinstance(tiers, list) and any(isinstance(tier, dict)
                    and tier.get('id') == 'priority' for tier in tiers)):
                models.add(slug)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # Explicit IDs only: Claude's aliases can switch to a different model.
    # https://code.claude.com/docs/en/fast-mode (2026-10-17)
    return {'codex': sorted(models),
            'claude': ([] if any(os.environ.get(key, '').lower() in ('1', 'true')
                                  for key in _CLAUDE_NO_FAST) else
                       ['claude-opus-4-8', 'claude-opus-5', 'claude-opus-5-5']),
            'opencode2': []}


def fast_supported(cfg, catalog=None):
    """Whether the selected runtime/model exposes a fast toggle, not account entitlement."""
    catalog = catalog if catalog is not None else connections()
    if cfg.get('agent') == 'codex':
        profile = catalog.get('profiles', {}).get(cfg.get('profile'), {})
        provider = cfg.get('provider') or profile.get('provider') or catalog.get('current_provider') or 'openai'
        if provider not in catalog.get('fast_providers', ['openai', 'codex-lb']):
            return False
    if cfg.get('agent') == 'claude' and cfg.get('profile'):
        try:
            settings = json.loads(Path(cfg['profile']).expanduser().read_text())
            environment = settings.get('env', {})
            if any(str(environment.get(key, '')).lower() in ('1', 'true') for key in _CLAUDE_NO_FAST):
                return False
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    return cfg.get('model') in catalog.get('fast_models', {}).get(cfg.get('agent'), [])


def validate_fast(cfg, catalog=None):
    if 'fast' not in cfg:
        return
    if type(cfg['fast']) is not bool:
        raise ValueError('fast must be a boolean')
    if not fast_supported(cfg, catalog):
        raise ValueError('fast is unsupported for this runtime/model')


def merge_role(current, values, catalog=None):
    """A connection/model change must not inherit an incompatible fast request."""
    changed = any(k in values and values[k] != current.get(k)
                  for k in ('agent', 'model', 'provider', 'profile'))
    current.update(values)
    if changed and 'fast' in current and 'fast' not in values and not fast_supported(current, catalog):
        current.pop('fast')


def connections(home=None, repo=None, config_root=None):
    home = Path(home or os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    base = tomllib.loads((home / 'config.toml').read_text()) if (home / 'config.toml').exists() else {}
    profiles = dict(base.get('profiles', {}))
    for path in home.glob('*.config.toml'):
        profiles[path.name[:-len('.config.toml')]] = tomllib.loads(path.read_text())
    # Only publish names and provider references; never connection URLs, environment or credentials.
    providers = set(base.get('model_providers', {})) | {'openai'}
    for data in profiles.values():
        providers.update(data.get('model_providers', {}))
    result = {'profiles': {name: {'provider': data.get('model_provider', base.get('model_provider')),
                                'providers': sorted(set(base.get('model_providers', {})) |
                                                    set(data.get('model_providers', {})) | {'openai'})}
                         for name, data in profiles.items()},
            'providers': sorted(providers), 'current_provider': base.get('model_provider'),
            'base_providers': sorted(set(base.get('model_providers', {})) | {'openai'}),
            'efforts': EFFORTS, 'fast_models': fast_models(home), 'fast_providers': ['openai', 'codex-lb']}
    allowed = sber_allowed(repo)
    result['runtimes'] = ['codex', 'claude'] + (['opencode2'] if allowed else [])
    if allowed:
        result['opencode2'] = opencode_connection(config_root)
    else:
        result['providers'] = [p for p in result['providers'] if p != 'cloudru']
        result['base_providers'] = [p for p in result['base_providers'] if p != 'cloudru']
        result['profiles'] = {name: entry for name, entry in result['profiles'].items()
                              if entry['provider'] != 'cloudru'}
        if result['current_provider'] == 'cloudru':
            result['current_provider'] = None
        for entry in result['profiles'].values():
            entry['providers'] = [p for p in entry['providers'] if p != 'cloudru']
    return result


def validate(config, catalog=None, repo=None, config_root=None, *, allow_unavailable_fast=False):
    catalog = catalog if catalog is not None else connections(repo=repo, config_root=config_root)
    if not isinstance(config, dict) or type(config.get('version')) is not int or config['version'] not in (1, 2):
        raise ValueError('configuration requires version=1 or version=2')
    expected = {'version', 'roles'} if config['version'] == 1 else {'version', 'template', 'roles', 'reserves'}
    optional = {'auto_continue_after_replan'} if config['version'] == 2 else set()
    if set(config) - optional != expected:
        raise ValueError('unsupported configuration fields')
    if type(config.get('auto_continue_after_replan', False)) is not bool:
        raise ValueError('auto_continue_after_replan must be boolean')
    legacy_roles = set(ROLES) - {'implementer_medium'}
    if (not isinstance(config['roles'], dict) or
            (set(config['roles']) != set(ROLES) and
             not (config['version'] == 1 and set(config['roles']) in
                  (legacy_roles, legacy_roles - {'tester_complex', 'final_reviewer_critical', 'escalation_expert'})))):
        raise ValueError('configuration must contain exactly the supported roles')
    settings = list(config['roles'].items())
    if config['version'] == 2:
        if config['template'] not in ('current', 'cloudru', 'openai-only', None):
            raise ValueError('unknown built-in template')
        if not isinstance(config['reserves'], dict) or set(config['reserves']) != set(ROLES):
            raise ValueError('configuration must contain reserves for all supported roles')
        settings += [(role + ' reserve', cfg) for role, cfg in config['reserves'].items() if cfg is not None]
    for role, cfg in settings:
        if not isinstance(cfg, dict) or not FIELDS <= set(cfg) or set(cfg) - FIELDS - OPTIONAL_FIELDS:
            raise ValueError(f'{role}: expected {sorted(FIELDS)}')
        if any(not isinstance(v, str) or len(v) > 512 or any(ord(c) < 32 for c in v) for v in (cfg[k] for k in FIELDS)):
            raise ValueError(f'{role}: invalid field')
        if allow_unavailable_fast:
            if "fast" in cfg and type(cfg["fast"]) is not bool:
                raise ValueError("fast must be a boolean")
        else:
            validate_fast(cfg, catalog)
        agent = cfg['agent']
        validate_role_scope(cfg, repo, catalog)
        if agent not in EFFORTS or cfg['effort'] not in EFFORTS[agent]:
            raise ValueError(f'{role}: unsupported runtime/effort')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}', cfg['model']):
            raise ValueError(f'{role}: invalid model ID')
        if agent == 'codex':
            if cfg['profile'] and cfg['profile'] not in catalog['profiles']:
                raise ValueError(f'{role}: unknown Codex profile')
            allowed = (catalog['profiles'][cfg['profile']]['providers'] if cfg['profile']
                       else catalog['base_providers'])
            if cfg['provider'] and cfg['provider'] not in allowed:
                raise ValueError(f'{role}: provider is not registered in this connection')
        elif agent == 'opencode2':
            if cfg['profile'] or cfg['provider'] != 'cloudru':
                raise ValueError(f'{role}: OpenCode 2 requires provider=cloudru and an empty profile')
            connection = catalog.get('opencode2') or opencode_connection(config_root)
            if cfg['model'] not in connection['models']:
                raise ValueError(f'{role}: unknown Cloud.ru model')
            from .runner_cloud import overlay
            overlay(cfg)  # Validate the requested API parameters as well as the variant name.
        else:
            if cfg['provider'] != 'current':
                raise ValueError(f'{role}: Claude uses provider=current and optional settings path')
            if cfg['profile']:
                path = Path(cfg['profile']).expanduser()
                if not path.is_absolute() or not path.is_file():
                    raise ValueError(f'{role}: settings must reference an existing absolute file')
                # Validate without retaining or publishing the contents (may contain credentials).
                if not isinstance(json.loads(path.read_text()), dict):
                    raise ValueError(f'{role}: settings must be a JSON object')
    return copy.deepcopy(config)


def settings_update(payload, current, catalog=None, repo=None, config_root=None):
    """Legacy role-only saves preserve newer roles and explicit reserves."""
    if (isinstance(payload, dict) and set(payload) == {'version', 'roles'}
            and payload['version'] == 1 and isinstance(payload['roles'], dict)
            and not set(payload['roles']) - set(ROLES)):
        payload = {**copy.deepcopy(current), 'version': 2,
                   'template': current.get('template'),
                   'roles': {**copy.deepcopy(current['roles']), **payload['roles']},
                   'reserves': copy.deepcopy(current.get('reserves', dict.fromkeys(ROLES)))}
    return validate(payload, catalog, repo=repo, config_root=config_root)


def snapshot_role(attempt, role, variant='primary'):
    if variant not in ('primary', 'reserve'):
        raise ValueError('unsupported role variant')
    snapshot = json.loads((Path(attempt) / 'config.json').read_text())
    roles = snapshot.get('reserves', {}) if variant == 'reserve' else snapshot['roles']
    if role not in roles or roles[role] is None:
        raise ValueError(f"role {role!r} is absent from this attempt snapshot; configure a new attempt")
    return roles[role]


def load_config(state_dir, path=None, overrides=None, catalog=None, layers=(), repo=None, config_root=None,
                *, allow_unavailable_fast=False):
    cfg = defaults()
    for layer in layers:
        for role, fields in layer.items():
            merge_role(cfg["roles"][role], fields, catalog)
    source = Path(path).expanduser() if path else Path(state_dir) / 'config.json'
    if source.exists():
        saved = json.loads(source.read_text())
        version = saved.get('version', 1)
        if version not in (1, 2) or set(saved) - ({'version', 'roles'} if version == 1 else {'version', 'template', 'roles', 'reserves', 'auto_continue_after_replan'}):
            raise ValueError('unsupported configuration')
        cfg['auto_continue_after_replan'] = saved.get('auto_continue_after_replan', False)
        if version == 2 and saved.get('template') not in ('current', 'cloudru', 'openai-only', None):
            raise ValueError('unknown built-in template')
        cfg['template'] = saved.get('template')
        for role, values in saved.get('roles', {}).items():
            if role not in ROLES or not isinstance(values, dict) or set(values) - FIELDS - OPTIONAL_FIELDS:
                raise ValueError('unknown role or setting')
            merge_role(cfg['roles'][role], values, catalog)
            if 'profile' in values and 'provider' not in values and cfg['roles'][role]['agent'] == 'codex':
                cfg['roles'][role]['provider'] = ''
        if version == 2:
            reserves = saved.get('reserves', {})
            if not isinstance(reserves, dict) or set(reserves) - set(ROLES):
                raise ValueError('unknown reserve role')
            for role, values in reserves.items():
                cfg['reserves'][role] = copy.deepcopy(values)
    if ('implementer_medium' not in (saved.get('roles', {}) if source.exists() else {})
            and not any('implementer_medium' in layer for layer in layers)):
        cfg['roles']['implementer_medium'] = copy.deepcopy(cfg['roles']['implementer_standard'])
    if not source.exists() and path:
        raise ValueError(f'configuration file does not exist: {path}')
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    c = cfg['roles']['coordinator']
    previous = dict(c)
    if overrides.get('agent') and overrides['agent'] != c['agent']:
        c.update(agent=overrides['agent'], profile='', provider='', model='gpt-6.1-sol', effort='high')
        if c['agent'] == 'claude':
            c.update(provider='current', model='claude-opus-5-5', effort='high')
        elif c['agent'] == 'opencode2':
            c.update(provider='cloudru', model=opencode_connection(config_root)['default_model'], effort='high')
    if 'profile' in overrides and 'provider' not in overrides and c['agent'] == 'codex':
        c['provider'] = ''
    c.update(overrides)
    if ('fast' not in overrides and any(c.get(k) != previous.get(k) for k in ('agent', 'model', 'provider', 'profile'))
            and not fast_supported(c, catalog)):
        c.pop('fast', None)
    return validate(cfg, catalog, repo=repo, config_root=config_root,
                    allow_unavailable_fast=allow_unavailable_fast)


def projections(state_dir, state, resources, layers=(), repo=None, config_root=None):
    """Saved/next/snapshot/facts stay separate; drafts only live in the browser."""
    options = state.get('options', {})
    saved = load_config(state_dir, options.get('config'), layers=layers, repo=repo, config_root=config_root,
                        allow_unavailable_fast=True)
    overrides = {k: options[k] for k in FIELDS | OPTIONAL_FIELDS if options.get(k) is not None}
    error = None
    try:
        effective = load_config(state_dir, options.get('config'), overrides, layers=layers, repo=repo, config_root=config_root)
    except ValueError as exc:
        error = str(exc)
        effective = {'roles': {r: dict.fromkeys(FIELDS) for r in ROLES}}
    attempt = (state.get('current') or {}).get('attempt_dir')
    from .runner_store import read_json
    snapshot = read_json(Path(attempt) / 'config.json', {}) if attempt else {}
    source = Path(options.get('config') or Path(state_dir) / 'config.json')
    raw = read_json(source, {}).get('roles', {})
    result = {}
    for role in ROLES:
        sources = {k: ('saved: ' + str(source) if k in raw.get(role, {}) else 'workspace/project/defaults') for k in FIELDS | OPTIONAL_FIELDS}
        if role == 'coordinator':
            for k in FIELDS | OPTIONAL_FIELDS:
                if k in overrides:
                    sources[k] = 'CLI --' + k
                elif effective['roles'][role].get(k) != saved['roles'][role].get(k):
                    sources[k] = 'CLI runtime/profile defaults'
        agents = [a for a in resources.get('agents', []) if a.get('role') == role]
        result[role] = {'error': error, 'saved': saved['roles'][role], 'next': effective['roles'][role],
                        'sources': sources, 'snapshot': snapshot.get('roles', {}).get(role),
                        'reserve_saved': saved.get('reserves', {}).get(role),
                        'reserve_next': effective.get('reserves', {}).get(role),
                        'reserve_snapshot': snapshot.get('reserves', {}).get(role),
                        'facts': [{'dispatch_id': a.get('dispatch_id'),
                                   'variant': a.get('variant', 'primary'),
                                   'values': {k: (a.get('observed') or {}).get(k) for k in FIELDS | OPTIONAL_FIELDS},
                                   'launch': a.get('launch_confirmation')} for a in agents]}
    return result
