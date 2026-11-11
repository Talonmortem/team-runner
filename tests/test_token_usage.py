#!/usr/bin/env python3
"""Offline correctness tests for team/token-usage.py on synthetic rollout fixtures.

Checks the properties GOAL_MASTER §8 requires: sessions bound to the right dispatch/session id,
cumulative snapshots counted once (never summed), missing usage reported as unknown (not zero),
category separation, and Claude transcript deduplication plus time windows.
Run: python3 team/tests/test_token_usage.py ; exit code 0 = all passed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(os.path.dirname(HERE), "src", "team_runner", "token-usage.py")
spec = importlib.util.spec_from_file_location("token_usage", MODULE_PATH)
tu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tu)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def write_session(path: str, *, session_id: str, cwd: str, dispatch: str | None,
                  snapshots: list[tuple[int, int, int, int]], model="gpt-5.6-luna",
                  effort="xhigh", start="2026-09-20T10:00:00.000Z",
                  end="2026-09-20T10:05:00.000Z") -> None:
    """snapshots: cumulative (input, cached, output, total) records, as Codex writes them."""
    lines = [
        {"timestamp": start, "type": "session_meta",
         "payload": {"id": session_id, "timestamp": start, "cwd": cwd}},
        {"timestamp": start, "type": "turn_context", "payload": {"model": model, "effort": effort}},
    ]
    if dispatch:
        lines.append({"timestamp": start, "type": "response_item",
                      "payload": {"type": "message", "text": f"dispatch {dispatch} task_000000000000"}})
    for index, (inp, cached, out, total) in enumerate(snapshots):
        lines.append({"timestamp": end, "type": "event_msg",
                      "payload": {"type": "token_count", "info": {"total_token_usage": {
                          "input_tokens": inp, "cached_input_tokens": cached,
                          "output_tokens": out, "reasoning_output_tokens": index,
                          "total_tokens": total}}}})
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def build_fixture(tmp: str) -> tuple[str, str]:
    root = os.path.join(tmp, "sessions", "2026", "09", "20")
    os.makedirs(root)
    worktree = os.path.join(tmp, "worktree")
    write_session(os.path.join(root, "rollout-2026-09-20T10-00-00-a.jsonl"),
                  session_id="aaaaaaaa-1111", cwd=worktree, dispatch="ctx_aaaaaaaaaaaa",
                  snapshots=[(100, 40, 10, 110), (300, 120, 30, 330)])
    write_session(os.path.join(root, "rollout-2026-09-20T10-10-00-b.jsonl"),
                  session_id="bbbbbbbb-2222", cwd=worktree, dispatch="ctx_bbbbbbbbbbbb",
                  snapshots=[(500, 200, 50, 550)], model="codex-auto-review", effort="low")
    write_session(os.path.join(root, "rollout-2026-09-20T10-20-00-c.jsonl"),
                  session_id="cccccccc-3333", cwd=worktree, dispatch=None, snapshots=[])
    write_session(os.path.join(root, "rollout-2026-09-20T10-30-00-other.jsonl"),
                  session_id="dddddddd-4444", cwd=os.path.join(tmp, "other-worktree"),
                  dispatch="ctx_dddddddddddd", snapshots=[(999, 0, 999, 1998)])
    return os.path.join(tmp, "sessions"), worktree


def test_session_binding_and_snapshots() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sessions, worktree = build_fixture(tmp)
        categories = {"ctx_aaaaaaaaaaaa": "fix-work"}
        rows = tu.codex_sessions(worktree, "2026-09-20", sessions, categories)
        by_session = {row["session"]: row for row in rows}

        check("only sessions of the target worktree are counted", len(rows) == 3, str(sorted(by_session)))
        check("session from another worktree excluded", "dddddddd-4444" not in by_session)

        first = by_session["aaaaaaaa-1111"]
        check("dispatch resolved from the rollout", first["dispatch"] == "ctx_aaaaaaaaaaaa")
        check("cumulative snapshots are not summed (last wins)",
              first["usage"]["total_tokens"] == 330, str(first["usage"]))
        check("request count equals number of token_count records", first["requests"] == 2)
        check("duration computed from first/last timestamps", first["duration_s"] == 300)
        check("explicit category map wins", first["category"] == "fix-work")
        check("effective effort preserved", first["effort"] == "xhigh")

        review = by_session["bbbbbbbb-2222"]
        check("approval-review sessions are categorized separately, not hidden",
              review["category"] == "approval-review-service", review["category"])

        empty = by_session["cccccccc-3333"]
        check("session without token_count is unknown, not zero", empty["usage"] is None)
        check("session without dispatch is 'unknown'", empty["dispatch"] == "unknown")


def test_cli_reports_unknown_and_categories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sessions, worktree = build_fixture(tmp)
        cat_file = os.path.join(tmp, "cats.json")
        with open(cat_file, "w", encoding="utf-8") as handle:
            json.dump({"ctx_aaaaaaaaaaaa": "fix-work"}, handle)
        out = subprocess.run([sys.executable, MODULE_PATH, "--worktree", worktree,
                              "--sessions-root", sessions, "--since", "2026-09-20",
                              "--category-map", cat_file],
                             shell=False, capture_output=True, text=True)
        check("cli exits 0", out.returncode == 0, out.stderr)
        check("unknown usage is printed as unknown", "unknown" in out.stdout)
        unknown_row = [line for line in out.stdout.splitlines() if line.startswith("| cccccccc-3333")]
        check("session without usage has a row", len(unknown_row) == 1, out.stdout)
        check("its token columns are unknown, never 0",
              unknown_row and unknown_row[0].count("unknown") >= 5, unknown_row[0] if unknown_row else "")
        check("the unknown session is announced below the table",
              "reported as unknown, not zero" in out.stdout)
        check("category table is printed", "| fix-work |" in out.stdout, out.stdout[-400:])
        check("approval-review row is visible",
              "approval-review-service" in out.stdout)
        check("grand total sums per-session last snapshots only",
              "880" in out.stdout.replace(" ", ""), out.stdout[:600])


def test_claude_dedup_and_window() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "transcript.jsonl")
        records = [
            {"type": "assistant", "timestamp": "2026-09-19T10:00:00.000Z",
             "message": {"id": "msg_1", "model": "claude-opus-5",
                         "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                                   "cache_read_input_tokens": 1000, "output_tokens": 50}}},
            {"type": "assistant", "timestamp": "2026-09-19T10:00:01.000Z",
             "message": {"id": "msg_1", "model": "claude-opus-5",
                         "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                                   "cache_read_input_tokens": 1000, "output_tokens": 50}}},
            {"type": "assistant", "timestamp": "2026-09-20T10:00:00.000Z",
             "message": {"id": "msg_2", "model": "claude-opus-5",
                         "usage": {"input_tokens": 1, "cache_creation_input_tokens": 10,
                                   "cache_read_input_tokens": 100, "output_tokens": 5}}},
        ]
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        model, count, totals = tu.claude_usage(path)
        check("streamed duplicates counted once", count == 2, str(count))
        check("cache read summed across unique messages", totals["cache_read"] == 1100)
        _, count_after, totals_after = tu.claude_usage(path, "2026-09-20T00:00:00.000Z", None)
        check("time window isolates the corrective session", count_after == 1)
        check("windowed totals exclude earlier messages", totals_after["output"] == 5)
        _, count_before, _ = tu.claude_usage(path, None, "2026-09-20T00:00:00.000Z")
        check("before/after windows partition the transcript", count_before == 1)


if __name__ == "__main__":
    test_session_binding_and_snapshots()
    test_cli_reports_unknown_and_categories()
    test_claude_dedup_and_window()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
