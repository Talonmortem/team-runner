#!/usr/bin/env python3
"""Fill a worker spec template from $TEAM_RUNNER_MATERIALS/templates and write it to RUN_STATE/specs/<slug>.md.

usage: TEAM_RUNNER_RUN=<run> python3 $TEAM_RUNNER_MATERIALS/templates/mkspec.py <template> <slug> [--role ROLE] KEY=value ... KEY=@file ...
  <template>  name in $TEAM_RUNNER_MATERIALS/templates without .md (impl-step, tester-gates, tester-checks, reviewer)
  KEY=@file   inserts the file's text (step card, task text); RUN and RUN_STATE are filled automatically.
RUN_STATE defaults to TEAM_RUNNER_ATTEMPT, then TEAM_RUNNER_RUN_STATE, then
$XDG_STATE_HOME/team-runner/runs/<run> (XDG_STATE_HOME defaults to ~/.local/state).
Fails if a {UPPER_CASE} placeholder stays unfilled. Prints the written path.
"""
from __future__ import annotations

import os
import argparse
import json
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def fill(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        token = "{" + key + "}"
        if token not in template:
            raise SystemExit(f"template has no {token}")
        template = template.replace(token, value)
    left = sorted(set(re.findall(r"\{[A-Z][A-Z0-9_]*\}", template)))
    if left:
        raise SystemExit("unfilled placeholders: " + ", ".join(left))
    return template


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    attempt = os.environ.get("TEAM_RUNNER_ATTEMPT")
    run = os.environ.get("TEAM_RUNNER_RUN") or (os.path.basename(attempt) if attempt else None) or sys.exit("set TEAM_RUNNER_RUN or TEAM_RUNNER_ATTEMPT")
    state = attempt or os.environ.get("TEAM_RUNNER_RUN_STATE")
    if not state:
        root = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
        state = os.path.abspath(os.path.expanduser(os.path.join(root, "team-runner", "runs", run)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template', choices=('impl-step', 'tester-gates', 'tester-checks', 'reviewer'))
    parser.add_argument('slug')
    parser.add_argument('--role')
    parser.add_argument('--variant', choices=('primary', 'reserve'), default='primary')
    parser.add_argument('--reason')
    parser.add_argument('pairs', nargs='*')
    args = parser.parse_intermixed_args(argv)
    name, slug, pairs = args.template, args.slug, args.pairs
    allowed = {'impl-step': ('implementer_standard', 'implementer_medium', 'implementer_complex'),
               'tester-gates': ('tester',), 'tester-checks': ('tester', 'tester_complex'),
               'reviewer': ('final_reviewer', 'final_reviewer_critical')}
    if args.role and args.role not in allowed[name]:
        raise SystemExit(f"role {args.role!r} is incompatible with template {name!r}")
    if args.role and not attempt:
        raise SystemExit('--role requires TEAM_RUNNER_ATTEMPT to verify snapshot settings')
    if args.variant == 'reserve' and (not attempt or not args.role or not args.reason or not args.reason.strip()):
        raise SystemExit('--variant reserve requires TEAM_RUNNER_ATTEMPT, --role and nonempty --reason')
    role = args.role or {'reviewer': 'final_reviewer', 'tester-gates': 'tester',
                         'tester-checks': 'tester'}.get(name)
    with open(os.path.join(HERE, name + ".md"), encoding="utf-8") as fh:
        template = fh.read()
    values = {"RUN": run, "RUN_STATE": state, "CHECKOUT": os.environ.get("TEAM_RUNNER_CHECKOUT", os.getcwd())}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"expected KEY=value, got {pair}")
        if value.startswith("@"):
            with open(value[1:], encoding="utf-8") as fh:
                value = fh.read().strip()
        values[key] = value
    for auto in ("RUN", "RUN_STATE", "CHECKOUT"):
        if "{" + auto + "}" not in template:
            values.pop(auto)
    text = fill(template, values)
    if attempt:
        with open(os.path.join(attempt, "config.json"), encoding="utf-8") as snapshot:
            config = json.load(snapshot)
        roles = config.get('reserves', {}) if args.variant == 'reserve' else config['roles']
        if role and role not in roles:
            raise SystemExit(f"role {role!r} is absent from this attempt snapshot; configure a new attempt")
        if role and not roles[role]:
            raise SystemExit(f"reserve for role {role!r} is absent from this attempt snapshot; configure a new attempt")
        selected = {role: roles[role]} if role else roles
        text = "Authoritative launch settings (override static role defaults): " + json.dumps(selected) + "\n\n" + text
        if args.variant == 'reserve':
            text = 'Variant: reserve. Reason: ' + args.reason.strip() + '\nFresh session; confirm previous worker completed/stopped and reconcile unknown operations before launch.\n\n' + text
    out = os.path.join(state, "specs", slug + ".md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
