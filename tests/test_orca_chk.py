#!/usr/bin/env python3
"""Offline tests for team/orca_chk.py message rendering.

Run: python3 team/tests/test_orca_chk.py. Exit code 0 = all passed. No Orca runtime is needed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("orca_chk", os.path.join(os.path.dirname(HERE), "src", "team_runner", "orca_chk.py"))
orca_chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orca_chk)

FAILURES = []


def check(name, condition):
    if not condition:
        FAILURES.append(name)


heartbeat = {"type": "heartbeat", "from_handle": "term_26794ca0-e9cc", "subject": "alive",
             "payload": json.dumps({"taskId": "task_1", "dispatchId": "ctx_1", "phase": "implementing"})}
done = {"id": "msg_9", "type": "worker_done", "from_handle": "term_c151f455-8c80", "subject": "FIX3 PASS",
        "body": "three sentences", "payload": json.dumps({"taskId": "task_2", "dispatchId": "ctx_2", "outcome": "succeeded"})}
status_null = {"id": "msg_5", "type": "status", "from_handle": "term_x", "subject": "progress", "body": "b", "payload": None}
broken = {"id": "msg_6", "type": "question", "from_handle": "term_y", "subject": "q", "body": "?", "payload": "not json"}

lines = orca_chk.format_messages({"messages": [heartbeat, done, status_null, broken]})
check("heartbeat is one line with phase", lines[0] == "heartbeat term_26794ca0 implementing")
check("worker_done header carries ids and outcome",
      lines[1] == "msg_9 worker_done term_c151f455 FIX3 PASS taskId=task_2 dispatchId=ctx_2 outcome=succeeded")
check("worker_done body printed", lines[2] == "three sentences")
check("null payload tolerated", lines[3] == "msg_5 status term_x progress" and lines[4] == "b")
check("non-json payload tolerated", lines[5] == "msg_6 question term_y q" and lines[6] == "?")
check("empty result", orca_chk.format_messages({}) == [])
check("heartbeat without phase falls back to subject",
      orca_chk.format_messages({"messages": [{"type": "heartbeat", "subject": "alive"}]}) == ["heartbeat ? alive"])

if FAILURES:
    print("FAIL:", ", ".join(FAILURES))
    sys.exit(1)
print("ok: orca_chk tests passed")
