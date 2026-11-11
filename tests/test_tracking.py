import json
import tempfile
import unittest
from pathlib import Path

from team_runner.tracking import Tracking


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def steps(*statuses):
    return {"queue": [{"id": f"step-{n}", "status": s, "depends_on": [f"step-{n - 1}"] if n > 1 else []}
                      for n, s in enumerate(statuses, 1)]}


class TrackingTest(unittest.TestCase):
    def goal(self, features, root_status=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        goal = Path(tmp.name)
        queue = []
        prev = None
        for fid, data in features:
            queue.append({"id": fid, "path": fid, "depends_on": [prev] if prev else [],
                          "status": (root_status or {}).get(fid, "not_started")})
            write(goal / fid / "Tasks.json", data)
            prev = fid
        write(goal / "Tasks.json", {"queue": queue})
        return Tracking(goal)

    def test_step_ids_repeated_across_features_resolve_within_their_feature(self):
        tr = self.goal([("f1", steps("completed", "not_started")), ("f2", steps("not_started", "not_started")),
                        ("f3", steps("not_started"))])
        feat, _, item = tr.next_item()
        self.assertEqual(tr.key(feat, item), "f1/step-2")

    def test_feature_dependency_blocks_next_feature_until_completed(self):
        tr = self.goal([("f1", steps("completed", "not_started")), ("f2", steps("not_started"))])
        self.assertIsNone(tr.next_item(skipped={"f1/step-2"}))
        tr = self.goal([("f1", steps("completed", "completed")), ("f2", steps("not_started"))])
        feat, _, item = tr.next_item()
        self.assertEqual(tr.key(feat, item), "f2/step-1")
        tr = self.goal([("f1", steps("completed", "not_started")), ("f2", steps("not_started"))],
                       root_status={"f1": "completed"})
        feat, _, item = tr.next_item()
        self.assertEqual(tr.key(feat, item), "f1/step-2")

    def test_qualified_and_bare_lookup(self):
        tr = self.goal([("f1", steps("completed", "not_started")), ("f2", steps("not_started", "not_started", "x"))])
        self.assertEqual(tr.find("f2/step-1")[0]["id"], "f2")
        self.assertEqual(tr.canonical("step-3"), "f2/step-3")
        with self.assertRaisesRegex(ValueError, "ambiguous item step-1: use one of f1/step-1, f2/step-1"):
            tr.resolve("step-1")
        self.assertEqual(tr.find("step-1")[0]["id"], "f1")  # legacy state: first match
        self.assertIsNone(tr.resolve("missing"))
        idx = tr.status_index()
        self.assertEqual((idx["f1/step-1"], idx["f2/step-1"], idx["step-3"]), ("completed", "not_started", "x"))
        self.assertNotIn("step-1", idx)

    def test_only_and_skipped_use_qualified_keys(self):
        tr = self.goal([("f1", steps("not_started", "not_started")), ("f2", steps("not_started"))])
        self.assertIsNone(tr.next_item(only={"step-1"}))
        feat, _, item = tr.next_item(only={"f1/step-1"})
        self.assertEqual(tr.key(feat, item), "f1/step-1")
        self.assertIsNone(tr.next_item(skipped={"f1/step-1"}))

    def test_unresolved_dependency_is_not_satisfied(self):
        tr = self.goal([("f1", {"queue": [{"id": "a", "status": "not_started", "depends_on": ["nowhere"]}]})])
        self.assertIsNone(tr.next_item())


if __name__ == "__main__":
    unittest.main()
