"""Read-only validation and durable snapshots for Queue.json version 1."""
from graphlib import CycleError, TopologicalSorter
import json
from pathlib import Path, PureWindowsPath
import re

from .runner_store import atomic_json, read_json
from .verification import digest

NAME = 'Queue.json'
VERSION = 1
SECTIONS = frozenset({
    'result', 'input contracts', 'change boundaries', 'dependencies',
    'acceptance criteria', 'checks', 'expected evidence',
})
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')


def _fields(value, required, optional, label):
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError(f'{label}: required fields: {", ".join(sorted(required))}')
    extras = value.keys() - required - optional
    if extras:
        raise ValueError(f'{label}: unsupported fields: {", ".join(sorted(extras))}')


def _strings(value, label, *, nonempty=False):
    if (not isinstance(value, list) or nonempty and not value
            or any(not isinstance(part, str) or not part.strip() for part in value)):
        raise ValueError(f'{label}: expected a list of nonempty strings')


def _entry(value, seen, label):
    identifier = value['id']
    if not isinstance(identifier, str) or not ID.fullmatch(identifier):
        raise ValueError(f'{label}: invalid ID {identifier!r}')
    if identifier.lower() in seen:
        raise ValueError(f'{label}: duplicate ID {identifier}')
    seen.add(identifier.lower())
    _strings(value['depends_on'], f'{label} {identifier} depends_on')
    return identifier


def _path(base, value, label):
    if (not isinstance(value, str) or not value.strip() or Path(value).is_absolute()
            or PureWindowsPath(value).drive or '\\' in value or '..' in Path(value).parts):
        raise ValueError(f'{label}: expected a confined relative POSIX path')
    result = (base / value).resolve()
    if not result.is_relative_to(base):
        raise ValueError(f'{label}: path escapes {base}: {value}')
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON field: {key}')
        result[key] = value
    return result


def _check_card(path, body):
    filled = set()
    current = None
    fence = None
    for line in body.splitlines():
        line = line.strip()
        marker = re.match(r'(`{3,}|~{3,})', line)
        if fence:
            if line.rstrip(fence[0]) == '' and len(line) >= len(fence):
                fence = None
            elif line and current:
                filled.add(current)
        elif marker:
            fence = marker[0]
        elif line.startswith('#') and not line.startswith('###'):
            match = re.fullmatch(r'##\s+(.+?)\s*#*', line)
            current = match[1].casefold() if match else None
        elif line and current:
            filled.add(current)
    missing = SECTIONS - filled
    if missing:
        raise ValueError(f'{path}: missing or empty card sections: {", ".join(sorted(missing))}')


def read(goal_dir):
    """Validate source files and return the exact execution payload."""
    from .tracking import Tracking

    directory = Path(goal_dir).resolve()
    checkout = next((parent for parent in (directory, *directory.parents)
                     if (parent / '.git').exists()), directory)
    path = _path(directory, NAME, NAME)
    try:
        queue = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise ValueError(f'{path}: {exc}') from exc
    _fields(queue, {'schema_version', 'queue'}, set(), NAME)
    if type(queue['schema_version']) is not int or queue['schema_version'] != VERSION:
        raise ValueError(f'{NAME}: unsupported schema_version {queue["schema_version"]!r}')
    features = queue['queue']
    if not isinstance(features, list) or not features:
        raise ValueError(f'{NAME}: queue must be a nonempty feature list')
    feature_ids, feature_paths = set(), set()
    cards, item_keys, card_paths = {}, set(), set()
    for feature in features:
        _fields(feature, {'id', 'path', 'depends_on', 'items'}, {'stage_gate'}, 'feature')
        fid = _entry(feature, feature_ids, 'feature')
        feature_dir = _path(directory, feature['path'], fid)
        if feature_dir == directory or not feature_dir.is_dir() or feature_dir in feature_paths:
            raise ValueError(f'{fid}: feature path must name a distinct existing subdirectory')
        feature_paths.add(feature_dir)
        items = feature['items']
        if not isinstance(items, list) or not items:
            raise ValueError(f'{fid}: items must be a nonempty list')
        local = set()
        for item in items:
            _fields(item, {'id', 'task', 'depends_on', 'step_card'},
                    {'owner', 'check_scope', 'gate_roots', 'gate_command'}, fid)
            key = f'{fid}/{_entry(item, local, fid)}'
            item_keys.add(key)
            for name in ('task', 'owner'):
                if name in item and (not isinstance(item[name], str) or not item[name].strip()):
                    raise ValueError(f'{key}: {name} must be a nonempty string')
            check_scope = item.get('check_scope', 'item')
            if check_scope == 'stage':
                _strings(item.get('gate_roots'), f'{key} gate_roots', nonempty=True)
                _strings(item.get('gate_command'), f'{key} gate_command', nonempty=True)
                for root in item['gate_roots']:
                    _path(checkout, root, f'{key} gate_roots')
                if len(set(item['gate_roots'])) != len(item['gate_roots']):
                    raise ValueError(f'{key}: duplicate gate roots')
            elif check_scope != 'item' or {'gate_roots', 'gate_command'} & item.keys():
                raise ValueError(f'{key}: invalid check scope or stage parameters')
            card = _path(feature_dir, item['step_card'], key)
            if card.suffix != '.md' or not card.is_file() or card in card_paths:
                raise ValueError(f'{key}: expected a distinct existing Markdown step card')
            card_paths.add(card)
            body = card.read_bytes().decode('utf-8')
            _check_card(card, body)
            cards[key] = body
        if 'stage_gate' in feature and not any(item['id'] == feature['stage_gate']
                                               and item.get('check_scope') == 'stage' for item in items):
            raise ValueError(f'{fid}: stage_gate must name a stage item in this feature')
    feature_keys = {feature['id'] for feature in features}
    graph = {}
    for feature in features:
        fid = feature['id']
        graph[fid] = [Tracking.key(feature, item) for item in feature['items']]
        for dependency in feature['depends_on']:
            if dependency not in feature_keys:
                raise ValueError(f'{fid}: unknown feature dependency {dependency}')
        for item in feature['items']:
            key = Tracking.key(feature, item)
            dependencies = list(feature['depends_on'])
            for dependency in item['depends_on']:
                target = Tracking.dependency_key(feature, {'queue': feature['items']}, dependency)
                if target not in feature_keys and target not in item_keys:
                    raise ValueError(f'{key}: unknown dependency {dependency}')
                dependencies.append(target)
            graph[key] = dependencies
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError('dependency cycle: ' + ' → '.join(exc.args[1])) from exc
    return {'queue': queue, 'cards': cards}


def snapshot_path(runner, goal):
    return runner.state_dir / 'queue-snapshots' / (digest(goal) + '.json')


def load_snapshot(runner, goal):
    path = snapshot_path(runner, goal)
    if not path.exists():
        return None
    record = read_json(path)
    if (not isinstance(record, dict) or set(record) != {'goal', 'identity', 'payload', 'sha256'}
            or record['goal'] != goal or record['identity'] != runner.context.identity()
            or record['sha256'] != digest(record['payload'])):
        raise ValueError(f'{goal}: invalid Runner queue snapshot')
    return record['payload']


def save_snapshot(runner, goal, payload):
    """Called only under the existing start and checkout locks."""
    saved = load_snapshot(runner, goal)
    if saved is not None:
        if saved != payload:
            raise ValueError(f'{goal}: queue snapshot changed during start')
        return
    atomic_json(snapshot_path(runner, goal), {
        'goal': goal, 'identity': runner.context.identity(),
        'payload': payload, 'sha256': digest(payload),
    })
