"""Offline checks for spec output paths and attempt snapshots."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "src/team_runner/templates/mkspec.py"
spec = importlib.util.spec_from_file_location("mkspec", MODULE_PATH)
mkspec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mkspec)


class SpecPathTests(unittest.TestCase):
    def test_medium_and_reserve_use_only_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp)
            templates = attempt / 'templates'
            templates.mkdir()
            (templates / 'impl-step.md').write_text('task')
            cfg = {'roles': {'implementer_medium': {'model': 'primary'}},
                   'reserves': {'implementer_medium': {'model': 'reserve-model', 'effort': 'high'}}}
            snapshot = attempt / 'config.json'
            snapshot.write_text(json.dumps(cfg))
            with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(attempt)}), \
                    patch.object(mkspec, 'HERE', str(templates)), contextlib.redirect_stdout(io.StringIO()):
                args = ['impl-step', 'medium', '--role', 'implementer_medium']
                mkspec.main(args)
                target = attempt / 'specs/medium.md'
                self.assertIn('primary', target.read_text())
                reserve_args = args + ['--variant', 'reserve']
                for tail in ([], ['--reason', '   ']):
                    with self.assertRaisesRegex(SystemExit, 'nonempty --reason'):
                        mkspec.main(reserve_args + tail)
                mkspec.main(reserve_args + ['--reason', 'independent replacement'])
                text = target.read_text()
                self.assertIn('reserve-model', text)
                self.assertIn('independent replacement', text)
                self.assertNotIn('"primary"', text)
                for reserves in ({}, {'implementer_medium': None}):
                    cfg['reserves'] = reserves
                    snapshot.write_text(json.dumps(cfg))
                    with self.assertRaisesRegex(SystemExit, 'absent.*snapshot'):
                        mkspec.main(reserve_args + ['--reason', 'retry'])
                    self.assertEqual(target.read_text(), text)

    def test_output_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt = root / "attempt"
            attempt.mkdir()
            roles = {"final_reviewer": {"model": "snapshot-model"}}
            (attempt / "config.json").write_text(json.dumps({"roles": roles}))
            templates = root / "templates"
            templates.mkdir()
            (templates / "reviewer.md").write_text("run={RUN}\nstate={RUN_STATE}\n")
            state = root / "state"
            home = root / "home"
            override = root / "override"
            base_env = {"HOME": str(home), "XDG_STATE_HOME": str(state),
                        "TEAM_RUNNER_ATTEMPT": str(attempt), "TEAM_RUNNER_RUN": "run_x",
                        "TEAM_RUNNER_RUN_STATE": str(override),
                        "TEAM_RUNNER_MATERIALS": str(attempt / "materials")}
            cases = (
                ("attempt", {}, attempt, "run_x"),
                ("attempt-run", {"TEAM_RUNNER_RUN": ""}, attempt, "attempt"),
                ("override", {"TEAM_RUNNER_ATTEMPT": ""}, override, "run_x"),
                ("xdg", {"TEAM_RUNNER_ATTEMPT": "", "TEAM_RUNNER_RUN_STATE": ""},
                 state / "team-runner/runs/run_x", "run_x"),
                ("home", {"TEAM_RUNNER_ATTEMPT": "", "TEAM_RUNNER_RUN_STATE": "",
                          "XDG_STATE_HOME": ""},
                 home / ".local/state/team-runner/runs/run_x", "run_x"),
            )
            for name, env, expected, run in cases:
                with self.subTest(name=name), patch.dict(os.environ, {**base_env, **env}, clear=True), \
                        patch.object(mkspec, "HERE", str(templates)), \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(mkspec.main(["reviewer", name]), 0)
                    out = expected / "specs" / (name + ".md")
                    self.assertEqual(stdout.getvalue().strip(), str(out))
                    text = out.read_text()
                    self.assertIn(f"run={run}\nstate={expected}\n", text)
                    if os.environ.get("TEAM_RUNNER_ATTEMPT"):
                        self.assertIn(json.dumps(roles), text)
                    else:
                        self.assertNotIn("Authoritative launch settings", text)


if __name__ == "__main__":
    unittest.main()
