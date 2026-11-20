"""Execution queue adapters for frozen Queue.json and legacy Tasks.json.

Step IDs are local to their feature (``step-1`` may exist in every feature), so the
runner addresses an item by its qualified key ``<feature id>/<step id>``. A bare step
ID is accepted only when it is unique across the goal; legacy state written before
qualified keys may still carry an ambiguous bare ID and resolves to the first match,
which is what such state was dispatched and verified against.
"""
from pathlib import Path
from . import queue_plan
from .runner_store import decode_json
from .verification import digest

SEP = "/"


class Tracking:
    def __init__(self, goal_dir, history=(), *, payload=None, legacy=False):
        self.goal_dir = Path(goal_dir).resolve()
        self.checkout = next((p for p in (self.goal_dir, *self.goal_dir.parents) if (p / '.git').exists()), self.goal_dir)
        self.payload = payload
        if self.payload is None and not legacy and (self.goal_dir / queue_plan.NAME).exists():
            self.payload = queue_plan.read(self.goal_dir)
        self.snapshot_sha256 = digest(self.payload) if self.payload is not None else None
        if self.payload is None:
            path = self.goal_dir / 'Tasks.json'
            self.goal = decode_json(path.read_text(), path)
        else:
            self.goal = self.payload['queue']
        self._features = list(self._read_features())
        self.history = history

    def card(self, key):
        return self.payload['cards'][key] if self.payload is not None else None

    def features(self):
        return iter(self._features)

    def _read_features(self):
        for f in self.goal["queue"]:
            if self.payload is not None:
                yield f, self.goal_dir / queue_plan.NAME, {'queue': f['items']}
                continue
            path = (self.goal_dir / f["path"] / "Tasks.json").resolve()
            if not path.is_relative_to(self.goal_dir):
                raise ValueError("feature tracking escapes goal directory")
            if path.exists():
                yield f, path, decode_json(path.read_text(), path)

    @staticmethod
    def key(feat, t):
        return f"{feat['id']}{SEP}{t['id']}"

    def items(self):
        for feat, path, data in self.features():
            for t in data.get("queue", []):
                yield self.key(feat, t), feat, path, t

    def status_index(self):
        """Statuses by feature ID, qualified item key and unique bare step ID."""
        idx = {f["id"]: "completed" if self.feature_done(f["id"]) else "pending" for f in self.goal["queue"]}
        bare = {}
        for key, _, _, t in self.items():
            idx[key] = self.item_status(key)
            bare.setdefault(t["id"], []).append(idx[key])
        idx.update({i: s[0] for i, s in bare.items() if len(s) == 1 and i not in idx})
        return idx

    def resolve(self, item_id, strict=True):
        """Return (key, feat, path, item) or None; an ambiguous bare ID raises when strict."""
        qualified, matches = [], []
        for key, feat, path, t in self.items():
            if key == item_id:
                qualified.append((key, feat, path, t))
            elif t["id"] == item_id:
                matches.append((key, feat, path, t))
        if len(qualified) > 1 and strict:
            raise ValueError(f'duplicate item identity: {item_id}')
        if qualified:
            return qualified[0]
        if len(matches) > 1 and strict:
            raise ValueError(f"ambiguous item {item_id}: use one of " + ", ".join(m[0] for m in matches))
        return matches[0] if matches else None

    def canonical(self, item_id, strict=True):
        found = self.resolve(item_id, strict)
        return found[0] if found else item_id

    def find(self, item_id, strict=False):
        found = self.resolve(item_id, strict)
        return found[1:] if found else None

    def contract(self, feat, item):
        def planned(record):
            result = {k: v for k, v in record.items()
                      if k not in ('status', 'progress', 'remaining', 'evidence', 'gate_evidence', 'queue', 'items')}
            if isinstance(result.get('verification'), dict):
                result['verification'] = {k: v for k, v in result['verification'].items()
                                          if k not in ('status', 'evidence')}
            return result

        contract = {'feature': planned(feat), 'item': planned(item)}
        if self.snapshot_sha256 is not None:
            contract['queue_sha256'] = self.snapshot_sha256
        return contract

    def acceptance(self, key, history=None):
        from .verification import gate_valid

        try:
            found = self.resolve(key)
        except ValueError:
            return None
        if not found:
            return None
        key, feat, _, item = found
        for entry in reversed(self.history if history is None else history):
            identifier = entry.get('item')
            if identifier not in (key, item['id']):
                continue
            dispatch = entry.get('dispatch_id')
            if not isinstance(dispatch, str) or not dispatch or entry.get('outcome') != 'succeeded' or entry.get('problems') != []:
                continue
            if 'item_contract' in entry:
                if (identifier != key or not entry.get('attempt_id')
                        or entry['item_contract'] != self.contract(feat, item)):
                    continue
                proof = entry.get('stage_evidence')
                candidate = dict(item, gate_evidence=proof)
            else:
                if self.payload is not None:
                    continue
                try:
                    if self.canonical(identifier) != key:
                        continue
                except ValueError:
                    continue
                candidate = item
            if self.payload is not None and item.get('check_scope') == 'stage':
                if entry.get('attempt_dir') and gate_valid(self.checkout, candidate, attempt=entry['attempt_dir']):
                    return entry
            elif gate_valid(self.checkout, candidate):
                return entry
        return None

    def item_done(self, key):
        return self.acceptance(key) is not None

    def item_status(self, key):
        return 'completed' if self.item_done(key) else 'pending'

    @staticmethod
    def missing_gate(feat, queue):
        required = feat.get('stage_gate')
        return required and not any(t['id'] == required and t.get('check_scope') == 'stage' for t in queue)

    def feature_done(self, feature_id):
        for feat, _, data in self.features():
            if feat["id"] == feature_id:
                queue = data.get("queue", [])
                return (bool(queue) and not self.missing_gate(feat, queue)
                        and all(self.item_done(self.key(feat, t)) for t in queue))
        return False

    @staticmethod
    def dependency_key(feat, data, dep):
        if any(t['id'] == dep for t in data.get('queue', [])):
            return f"{feat['id']}{SEP}{dep}"
        return dep

    def dependency_done(self, feat, data, dep):
        key = self.dependency_key(feat, data, dep)
        if key == dep and any(f["id"] == dep for f in self.goal["queue"]):
            return self.feature_done(dep)
        return SEP in key and self.item_done(key)

    @staticmethod
    def user_blocked(t):
        return "пользовател" in (t.get("owner") or "").lower()

    def next_item(self, only=None, skipped=()):
        for feat, path, data in self.features():
            if not all(self.feature_done(d) for d in feat.get("depends_on", [])):
                continue
            for t in data.get("queue", []):
                key = self.key(feat, t)
                if self.item_done(key) or key in skipped:
                    continue
                if only is not None and key not in only:
                    continue
                if self.user_blocked(t):
                    continue
                if all(self.dependency_done(feat, data, d) for d in t.get("depends_on", [])):
                    return feat, path, t
        return None

    def blocked_report(self, only=None, skipped=()):
        out = []
        for feat, _, data in self.features():
            items = data.get('queue', [])
            if only is None and not items:
                out.append(f"{feat['id']}: пустая очередь фичи")
            if only is None and self.missing_gate(feat, items):
                out.append(f"{feat['id']}: отсутствует обязательный stage gate {feat['stage_gate']}")
            for item in items:
                key = self.key(feat, item)
                if only is not None and key not in only:
                    continue
                waiting = [dep for dep in feat.get('depends_on', []) if not self.feature_done(dep)]
                waiting += [dep for dep in item.get('depends_on', []) if not self.dependency_done(feat, data, dep)]
                if self.item_done(key):
                    if only is None and waiting:
                        out.append(f"{key}: ждёт приёмки " + ', '.join(waiting))
                    continue
                reason = ('пропущен' if key in skipped else f"владелец: {item.get('owner')}" if self.user_blocked(item)
                          else 'ждёт приёмки ' + ', '.join(waiting) if waiting else 'ожидает приёмки')
                out.append(f'{key}: {reason}')
        if only is None:
            present = {feat['id'] for feat, _, _ in self.features()}
            out.extend(f"{feat['id']}: отсутствует описание фичи" for feat in self.goal['queue']
                       if feat['id'] not in present)
        return out


def for_goal(runner, goal, state=None):
    state = runner.load_state() if state is None else state
    history = state.get('history', []) if state.get('goal') == goal else ()
    payload = queue_plan.load_snapshot(runner, goal)
    if state.get('goal') == goal and state.get('queue_sha256') and (
            payload is None or digest(payload) != state['queue_sha256']):
        raise ValueError(f'{goal}: saved Runner queue snapshot is missing or changed')
    directory = runner.repo / goal
    legacy = (payload is None and state.get('goal') == goal and (directory / 'Tasks.json').is_file()
              and bool(history or state.get('current') or state.get('run_id')))
    return Tracking(directory, history, payload=payload, legacy=legacy)
