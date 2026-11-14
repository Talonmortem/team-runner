"""Version 3 team graphs. IDs, rather than semantic roles, identify agents."""
import copy
import math
import re

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('invalid graph ID')
    return value


def text(value, limit=16000):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('invalid graph text')


def migrate(config):
    if config.get('version') == 3:
        return copy.deepcopy(config)
    nodes, edges, positions = [], [], {}
    for i, (role, cfg) in enumerate(config['roles'].items()):
        nodes.append(dict(id=role, name=role, role=role, instructions='',
                          primary=copy.deepcopy(cfg), reserve=copy.deepcopy(config.get('reserves', {}).get(role)),
                          group=None))
        positions[role] = {'x': 80 if role == 'coordinator' else 420 + ((i - 1) // 6) * 310,
                           'y': 260 if role == 'coordinator' else 50 + ((i - 1) % 6) * 140}
        if role != 'coordinator':
            edges.append(dict(id='delegate_' + role, source='coordinator', target=role,
                              type='delegation', label='', instructions='', max_passes=3))
    return dict(version=3, root='coordinator', nodes=nodes, edges=edges, groups=[],
                layout=positions, auto_continue_after_replan=config.get('auto_continue_after_replan', False))


def validate(config, catalog, repo, config_root, allow_unavailable_fast=False):
    from .runner_config import ROLES, validate_settings
    if set(config) - {'auto_continue_after_replan'} != {'version', 'root', 'nodes', 'edges', 'groups', 'layout'}:
        raise ValueError('unsupported graph configuration fields')
    if type(config.get('auto_continue_after_replan', False)) is not bool:
        raise ValueError('auto_continue_after_replan must be boolean')
    for key in ('nodes', 'edges', 'groups'):
        if not isinstance(config[key], list) or len(config[key]) > 512:
            raise ValueError('graph requires bounded lists')
    ids = set()
    by_kind = {}
    for key in ('nodes', 'edges', 'groups'):
        by_kind[key] = set()
        for item in config[key]:
            if not isinstance(item, dict):
                raise ValueError('invalid graph item')
            ident = identifier(item.get('id'))
            if ident in ids:
                raise ValueError('duplicate graph ID')
            ids.add(ident)
            by_kind[key].add(ident)
    nodes, groups = by_kind['nodes'], by_kind['groups']
    if config['root'] not in nodes:
        raise ValueError('missing root agent')
    coordinators = []
    settings = []
    for n in config['nodes']:
        if set(n) != {'id', 'name', 'role', 'instructions', 'primary', 'reserve', 'group'}:
            raise ValueError('unsupported node fields')
        text(n['name'], 160)
        if not n['name'].strip() or n['role'] not in ROLES:
            raise ValueError('invalid node name/role')
        text(n['instructions'])
        if n['group'] is not None and n['group'] not in groups:
            raise ValueError('unknown group')
        if n['role'] == 'coordinator':
            coordinators.append(n['id'])
        settings.append((n['id'], n['primary']))
        if n['reserve'] is not None:
            settings.append((n['id'] + ' reserve', n['reserve']))
    if coordinators != [config['root']]:
        raise ValueError('exactly one root coordinator required')
    validate_settings(settings, catalog, repo, config_root, allow_unavailable_fast)
    for g in config['groups']:
        if set(g) != {'id', 'name', 'collapsed'} or type(g['collapsed']) is not bool:
            raise ValueError('invalid group')
        text(g['name'], 160)
        if not g['name'].strip():
            raise ValueError('empty group name')
    for e in config['edges']:
        if set(e) != {'id', 'source', 'target', 'type', 'label', 'instructions', 'max_passes'}:
            raise ValueError('unsupported edge fields')
        if e['source'] not in nodes or e['target'] not in nodes:
            raise ValueError('dangling edge endpoint')
        if e['type'] not in ('delegation', 'rework'):
            raise ValueError('unknown edge type')
        if type(e['max_passes']) is not int or e['max_passes'] <= 0:
            raise ValueError('edge limit must be positive')
        text(e['label'], 160)
        text(e['instructions'])
    reached = {config['root']}
    while True:
        extra = {e['target'] for e in config['edges'] if e['type'] == 'delegation' and e['source'] in reached}
        if extra <= reached:
            break
        reached |= extra
    if reached != nodes:
        raise ValueError('all agents must be reachable by delegation from root')
    if not isinstance(config['layout'], dict) or set(config['layout']) != nodes:
        raise ValueError('layout must contain every agent')
    for position in config['layout'].values():
        if (not isinstance(position, dict) or set(position) != {'x', 'y'}
                or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e7 for v in position.values())):
            raise ValueError('invalid position')
    return copy.deepcopy(config)


def node(config, ident):
    return next(n for n in config['nodes'] if n['id'] == ident)


def role_settings(config):
    """Diagnostics only. Dispatch always selects a node/edge explicitly."""
    if config.get('version') != 3:
        return config
    return {'roles': {n['id']: n['primary'] for n in config['nodes']},
            'reserves': {n['id']: n['reserve'] for n in config['nodes']}}
