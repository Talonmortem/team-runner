"""Shared local team library, independent of workspace and attempt snapshots."""
import copy
import uuid
from pathlib import Path

from .runner_store import atomic_json, lock, read_json


class TeamProfiles:
    def __init__(self, config_root):
        self.path = Path(config_root) / 'team-profiles.json'

    def read(self):
        return read_json(self.path, {'version': 1, 'profiles': []})

    def update(self, data, validate):
        if not isinstance(data, dict) or set(data) - {'action', 'id', 'name', 'config'}:
            raise ValueError('invalid team profile request')
        action = data.get('action')
        if action not in ('create', 'save', 'rename', 'delete'):
            raise ValueError('unknown team profile action')
        with lock(self.path.with_suffix('.lock')):
            library = self.read()
            entry = next((p for p in library['profiles'] if p['id'] == data.get('id')), None)
            if action == 'create':
                entry = {'id': str(uuid.uuid4())}
            elif entry is None:
                raise ValueError('unknown team profile')
            if action in ('create', 'rename'):
                name = data.get('name')
                if not isinstance(name, str) or not name.strip() or len(name) > 160 or any(ord(c) < 32 for c in name):
                    raise ValueError('invalid team profile name')
                if any(p['name'] == name.strip() and p['id'] != entry['id'] for p in library['profiles']):
                    raise ValueError('team profile name already exists')
                entry['name'] = name.strip()
            if action in ('create', 'save'):
                cfg = validate(data.get('config'))
                if cfg.get('version') != 3:
                    raise ValueError('team profiles require version=3')
                entry['config'] = cfg
            if action == 'create':
                library['profiles'].append(entry)
            elif action == 'delete':
                library['profiles'].remove(entry)
            atomic_json(self.path, library)
            return copy.deepcopy(library)
