"""Tasks.json tracking adapter.

Step IDs are local to their feature (``step-1`` may exist in every feature), so the
runner addresses an item by its qualified key ``<feature id>/<step id>``. A bare step
ID is accepted only when it is unique across the goal; legacy state written before
qualified keys may still carry an ambiguous bare ID and resolves to the first match,
which is what such state was dispatched and verified against.
"""
import json
from pathlib import Path

SEP = "/"


class Tracking:
    def __init__(self, goal_dir):
        self.goal_dir = Path(goal_dir).resolve()
        self.checkout = next((p for p in (self.goal_dir, *self.goal_dir.parents) if (p / '.git').exists()), self.goal_dir)
        self.goal = json.loads((goal_dir / "Tasks.json").read_text())

    def features(self):
        for f in self.goal["queue"]:
            path = (self.goal_dir / f["path"] / "Tasks.json").resolve()
            if not path.is_relative_to(self.goal_dir):
                raise ValueError("feature tracking escapes goal directory")
            if path.exists():
                yield f, path, json.loads(path.read_text())

    @staticmethod
    def key(feat, t):
        return f"{feat['id']}{SEP}{t['id']}"

    def items(self):
        for feat, path, data in self.features():
            for t in data.get("queue", []):
                yield self.key(feat, t), feat, path, t

    def status_index(self):
        """Statuses by feature ID, qualified item key and unique bare step ID."""
        idx = {f["id"]: f["status"] for f in self.goal["queue"]}
        bare = {}
        for key, _, _, t in self.items():
            idx[key] = t["status"]
            bare.setdefault(t["id"], []).append(t["status"])
        idx.update({i: s[0] for i, s in bare.items() if len(s) == 1 and i not in idx})
        return idx

    def resolve(self, item_id, strict=True):
        """Return (key, feat, path, item) or None; an ambiguous bare ID raises when strict."""
        matches = []
        for key, feat, path, t in self.items():
            if key == item_id:
                return key, feat, path, t
            if t["id"] == item_id:
                matches.append((key, feat, path, t))
        if len(matches) > 1 and strict:
            raise ValueError(f"ambiguous item {item_id}: use one of " + ", ".join(m[0] for m in matches))
        return matches[0] if matches else None

    def canonical(self, item_id, strict=True):
        found = self.resolve(item_id, strict)
        return found[0] if found else item_id

    def find(self, item_id, strict=False):
        found = self.resolve(item_id, strict)
        return found[1:] if found else None

    def item_done(self, item):
        from .verification import gate_valid
        return item.get('status') == 'completed' and gate_valid(self.checkout, item)

    def feature_done(self, feature_id):
        for feat, _, data in self.features():
            if feat["id"] == feature_id:
                queue = data.get("queue", [])
                required_gate = feat.get('stage_gate')
                if required_gate and not any(t['id'] == required_gate and t.get('check_scope') == 'stage' for t in queue):
                    return False
                gates = [t for t in queue if t.get("check_scope") == "stage"]
                if gates:
                    return all(self.item_done(t) for t in queue)
                return feat["status"] == "completed" or bool(queue) and all(self.item_done(t) for t in queue)
        return any(f["id"] == feature_id and f["status"] == "completed" and not f.get('stage_gate')
                   for f in self.goal["queue"])

    def dependency_done(self, feat, data, dep):
        local = {t["id"]: self.item_done(t) for t in data.get("queue", [])}
        if dep in local:
            return local[dep]
        if any(f["id"] == dep for f in self.goal["queue"]):
            return self.feature_done(dep)
        found = self.resolve(dep, strict=False) if SEP in dep else None
        return bool(found) and self.item_done(found[3])

    @staticmethod
    def user_blocked(t):
        return "пользовател" in (t.get("owner") or "").lower()

    def next_item(self, only=None, skipped=()):
        for feat, path, data in self.features():
            if not all(self.feature_done(d) for d in feat.get("depends_on", [])):
                continue
            for t in data.get("queue", []):
                key = self.key(feat, t)
                if self.item_done(t) or key in skipped:
                    continue
                if only is not None and key not in only:
                    continue
                if self.user_blocked(t):
                    continue
                if all(self.dependency_done(feat, data, d) for d in t.get("depends_on", [])):
                    return feat, path, t
        return None

    def blocked_report(self):
        out = []
        for key, _, _, t in self.items():
            if t["status"] != "completed" and self.user_blocked(t):
                out.append(f"{key} (владелец: {t.get('owner')})")
        return out
