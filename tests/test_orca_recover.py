#!/usr/bin/env python3
"""Offline tests for team/orca_recover.py using fixtures only.

No live Orca, no network, no account use: rollout parsing, dispatch classification, nudge
budget, manual-acceptance recording and ledger idempotency are exercised against temporary
files. Run: python3 team/tests/test_orca_recover.py ; exit code 0 = all passed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(os.path.dirname(HERE), "src", "team_runner", "orca_recover.py")
spec = importlib.util.spec_from_file_location("orca_recover", MODULE_PATH)
recover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recover)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def write_rollout(path: str, *, dispatch: str, complete: bool, error: str | None,
                  model: str = "gpt-5.6-luna", effort: str = "xhigh") -> None:
    lines = [
        {"timestamp": "2026-09-20T00:00:00.000Z", "type": "session_meta",
         "payload": {"id": "sess", "cwd": "/tmp/wt"}},
        {"timestamp": "2026-09-20T00:00:01.000Z", "type": "turn_context",
         "payload": {"model": model, "effort": effort}},
        {"timestamp": "2026-09-20T00:00:02.000Z", "type": "response_item",
         "payload": {"type": "message", "text": f"preamble dispatch {dispatch}"}},
    ]
    if complete:
        lines.append({"timestamp": "2026-09-20T00:00:03.000Z", "type": "event_msg",
                      "payload": {"type": "task_complete",
                                  "error": {"message": error} if error else None}})
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def test_rollout_parsing_and_classification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sessions = os.path.join(tmp, "sessions", "2026", "09", "20")
        os.makedirs(sessions)

        broken = os.path.join(sessions, "rollout-provider-error.jsonl")
        write_rollout(broken, dispatch="ctx_aaaaaaaaaaaa", complete=True,
                      error="unexpected status 502 Bad Gateway: Previous response owner account "
                            "is unavailable; retry later., url: http://localhost:2455/x")
        facts = recover.rollout_facts(broken)
        check("provider error extracted", "502 Bad Gateway" in (facts["provider_error"] or ""))
        check("effective model parsed", facts["model"] == "gpt-5.6-luna")
        check("effective effort parsed", facts["effort"] == "xhigh")
        row = {"dispatchStatus": "dispatched", "projection": {"liveness": {"verdict": "live"}}}
        check("provider error beats a live liveness verdict",
              recover.classify(row, facts) == "provider_error_turn_ended")

        quiet = os.path.join(sessions, "rollout-turn-ended.jsonl")
        write_rollout(quiet, dispatch="ctx_bbbbbbbbbbbb", complete=True, error=None)
        facts_quiet = recover.rollout_facts(quiet)
        check("clean turn end has no provider error", facts_quiet["provider_error"] is None)
        check("turn ended without worker_done is its own class",
              recover.classify(row, facts_quiet) == "turn_ended_without_worker_done")

        working = os.path.join(sessions, "rollout-working.jsonl")
        write_rollout(working, dispatch="ctx_cccccccccccc", complete=False, error=None)
        facts_working = recover.rollout_facts(working)
        check("live worker mid-turn is only 'working_unproven'",
              recover.classify(row, facts_working) == "working_unproven")
        check("settled dispatch outranks everything",
              recover.classify({"dispatchStatus": "completed"}, facts_working) == "settled")
        check("stopped dispatch is classified stopped",
              recover.classify({"dispatchStatus": "failed"}, facts_working) == "stopped")

        found = recover.find_rollout("ctx_bbbbbbbbbbbb", sessions_root=os.path.join(tmp, "sessions"))
        check("rollout located by dispatch id", found == quiet, f"got {found}")
        check("unknown dispatch has no rollout",
              recover.find_rollout("ctx_zzzzzzzzzzzz", sessions_root=os.path.join(tmp, "sessions")) is None)


def run_cli(state_file: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, TEAM_RUNNER_RECOVERY_STATE=state_file)
    return subprocess.run([sys.executable, MODULE_PATH, *args], shell=False,
                          capture_output=True, text=True, env=env)


def test_state_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt = os.path.join(tmp, "attempt")
        state = os.path.join(tmp, "state")
        home = os.path.join(tmp, "home")
        override = os.path.join(tmp, "override.json")
        base_env = {"HOME": home, "XDG_STATE_HOME": state,
                    "TEAM_RUNNER_ATTEMPT": attempt,
                    "TEAM_RUNNER_MATERIALS": os.path.join(attempt, "materials")}
        cases = (
            ("attempt", {}, "run_x", os.path.join(attempt, "recovery-state.json")),
            ("override", {"TEAM_RUNNER_RECOVERY_STATE": override}, "run_x", override),
            ("xdg", {"TEAM_RUNNER_ATTEMPT": ""}, "run_x",
             os.path.join(state, "team-runner", "runs", "run_x", "recovery-state.json")),
            ("home", {"TEAM_RUNNER_ATTEMPT": "", "XDG_STATE_HOME": ""}, None,
             os.path.join(home, ".local", "state", "team-runner", "runs", "unbound",
                          "recovery-state.json")),
        )
        for name, env, run, expected in cases:
            with patch.dict(os.environ, {**base_env, **env}, clear=True):
                recover.save_state(run, {"ledger": {"delivered": True}})
                check(f"{name} recovery state uses expected path",
                      recover.state_path(run) == expected and os.path.isfile(expected))
                check(f"{name} recovery state round trips",
                      recover.load_state(run) == {"ledger": {"delivered": True}})
        with patch.dict(os.environ, {**base_env, "TEAM_RUNNER_ATTEMPT":
                                    os.path.join(tmp, "second-attempt")}, clear=True):
            check("new attempt does not reuse the previous attempt ledger",
                  recover.load_state("run_x")["ledger"] == {})


def test_ledger_idempotency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "state.json")
        first = run_cli(state_file, "ledger", "--run", "run_x", "--mark", "delivery_1",
                        "--value", '{"verdict":"PASS"}')
        check("first mark succeeds", first.returncode == 0 and json.loads(first.stdout)["marked"])
        second = run_cli(state_file, "ledger", "--run", "run_x", "--mark", "delivery_1",
                         "--value", '{"verdict":"PASS"}')
        payload = json.loads(second.stdout)
        check("repeated result is refused, not re-processed",
              second.returncode == 3 and payload["marked"] is False, second.stdout)
        seen = run_cli(state_file, "ledger", "--run", "run_x", "--seen", "delivery_1")
        check("seen reports already-processed with exit 3", seen.returncode == 3)
        unseen = run_cli(state_file, "ledger", "--run", "run_x", "--seen", "delivery_2")
        check("unknown key is not seen", unseen.returncode == 0)


def test_nudge_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "state.json")
        body = os.path.join(tmp, "body.txt")
        with open(body, "w", encoding="utf-8") as handle:
            handle.write("please check your inbox")
        state = {"nudges": {"ctx_a:idle": recover.MAX_INFRA_RECOVERY_ATTEMPTS},
                 "ledger": {}, "acceptances": [], "binds": 0}
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        result = run_cli(state_file, "nudge", "--run", "run_x", "--dispatch", "ctx_a",
                         "--terminal", "term_x", "--reason", "idle", "--body-file", body)
        payload = json.loads(result.stdout)
        check("exhausted nudge budget blocks another reminder",
              result.returncode == 2 and payload["sent"] is False, result.stdout)
        check("no Orca call is attempted once the budget is spent", "term_x" not in result.stderr)


def test_manual_acceptance_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "state.json")
        report = os.path.join(tmp, "review.md")
        with open(report, "w", encoding="utf-8") as handle:
            handle.write("# review\nVerdict: APPROVE WITH NOTES\n")
        first = run_cli(state_file, "accept", "--run", "run_x", "--dispatch", "ctx_r",
                        "--report", report, "--sha", "512363d", "--reason", "delivery 502")
        payload = json.loads(first.stdout)
        check("manual acceptance is recorded", first.returncode == 0 and payload["accepted"])
        check("record states delivery failed", payload["delivery"] == "failed")
        check("record never claims worker_done", "not fabricated" in payload["worker_done"])
        check("record pins the reviewed SHA", payload["reviewed_sha"] == "512363d")
        again = run_cli(state_file, "accept", "--run", "run_x", "--dispatch", "ctx_r",
                        "--report", report, "--sha", "512363d", "--reason", "delivery 502")
        check("repeated acceptance is idempotent", json.loads(again.stdout)["duplicate"] is True)
        missing = run_cli(state_file, "accept", "--run", "run_x", "--dispatch", "ctx_r",
                          "--report", os.path.join(tmp, "nope.md"), "--sha", "x", "--reason", "y")
        check("acceptance of a missing report is refused", missing.returncode == 1)


def test_state_command_separates_verdict() -> None:
    source = open(MODULE_PATH, encoding="utf-8").read()
    check("state output marks content verdict as not derivable",
          '"content_verdict": "not_derivable_from_dispatch_state"' in source)
    check("no shell is used anywhere", "shell=True" not in source and "os.system" not in source)


if __name__ == "__main__":
    test_state_paths()
    test_rollout_parsing_and_classification()
    test_ledger_idempotency()
    test_nudge_budget()
    test_manual_acceptance_record()
    test_state_command_separates_verdict()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
