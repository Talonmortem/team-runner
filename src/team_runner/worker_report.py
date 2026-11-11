"""Generate a compact report and durably send worker_done without shell interpolation.

Input JSON: {"outcome":"succeeded|failed", "summary":"...",
"files_modified":[], "checks":[], "issues":"none", "next_step":"none"}.
checks contains paths relative to TEAM_RUNNER_ATTEMPT, pointing to existing evidence.
Detailed requirement matrices remain linked evidence, not repeated report prose.
Unknown send outcomes require reconciliation; invocation never blindly resends.
"""
import argparse
import json
import os
from pathlib import Path

from .catalog import Catalog
from .orca import call
from .runner_store import atomic_json, lock, read_json
from . import runner_operations as operations


def complete(payload, sender, task, dispatch, capability=None):
    context = Catalog().context(os.environ['TEAM_RUNNER_PROJECT'], os.environ['TEAM_RUNNER_WORKSPACE'])
    attempt = Path(os.environ['TEAM_RUNNER_ATTEMPT']).resolve()
    context.check_attempt(attempt)
    if sender != os.environ.get('ORCA_TERMINAL_HANDLE'):
        raise ValueError('completion must originate from the assigned terminal')
    agents = read_json(attempt / 'agents.json', {}).get('agents', [])
    agent = next((a for a in agents if a.get('terminal') == sender
                  and a.get('task_id') == task and a.get('dispatch_id') == dispatch), None)
    if not agent or agent.get('role') == 'coordinator':
        raise ValueError('worker lifecycle identity mismatch')
    if not isinstance(payload, dict) or payload.get('outcome') not in ('succeeded', 'failed'):
        raise ValueError('outcome must be succeeded or failed')
    if set(payload) - {'outcome', 'summary', 'files_modified', 'checks', 'issues', 'next_step'}:
        raise ValueError('unknown report fields')
    if 'summary' not in payload:
        raise ValueError('summary is required')
    for key in ('summary', 'issues', 'next_step'):
        value = payload.get(key, 'none')
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError(key + ' must be concise text (1..2000 characters)')
    for key in ('files_modified', 'checks'):
        values = payload.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v or '\x00' in v for v in values):
            raise ValueError(key + ' must contain paths')
        for value in values:
            path = Path(value)
            if path.is_absolute() or '..' in path.parts or (key == 'files_modified' and ',' in value):
                raise ValueError('expected relative path: ' + value)
            if key == 'checks':
                resolved = (attempt / path).resolve()
                if not resolved.is_relative_to(attempt) or not resolved.is_file():
                    raise ValueError('missing or external evidence: ' + value)
    # Use registry identity, never a caller-supplied path component.
    stem = str(agent['id'])
    if Path(stem).name != stem or stem in ('.', '..'):
        raise ValueError('invalid registry id')
    report = attempt / 'reports' / (stem + '-summary.md')
    receipt = report.with_suffix('.json')
    journal = report.with_suffix('.completion.json')
    with lock(report.with_suffix('.lock')):
        old = read_json(receipt)
        if old is not None and old != payload:
            raise ValueError('completion input changed; inspect existing receipt')
        if old is None:
            text = '\n'.join([
                '# Worker result',
                f"Role: {agent['role']}; task: {task}; dispatch: {dispatch}",
                'Launch: ' + json.dumps(agent.get('requested', {}), ensure_ascii=False),
                'Outcome: ' + payload['outcome'], payload.get('summary', 'none'),
                'Files/evidence: ' + str(receipt),
                'Checks: ' + ('; '.join(payload.get('checks', [])) or 'not run'),
                'Issues: ' + payload.get('issues', 'none'),
                'Next: ' + payload.get('next_step', 'none'), ''])
            report.parent.mkdir(parents=True, exist_ok=True)
            temporary = report.with_suffix('.tmp')
            temporary.write_text(text)
            temporary.replace(report)
            atomic_json(receipt, payload)
        elif not report.is_file():
            raise ValueError('completion report missing; inspect receipt')
        args = ['orchestration', 'send', '--from', sender, '--type', 'worker_done',
                '--task-id', task, '--dispatch-id', dispatch, '--outcome', payload['outcome'],
                '--subject', 'Worker result', '--body', payload.get('summary', 'none'),
                '--report-path', str(report)]
        if payload.get('files_modified'):
            args += ['--files-modified', ','.join(payload['files_modified'])]
        if capability:
            args += ['--dispatch-capability', capability]
        return operations.mutation(journal, 'worker_done',
                                   lambda *a, **kw: call(*a, context=context, **kw),
                                   *args, mailbox=sender)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--from', dest='sender', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--dispatch-id', required=True)
    parser.add_argument('--dispatch-capability')
    args = parser.parse_args()
    result = complete(json.loads(Path(args.input).read_text()), args.sender,
                      args.task_id, args.dispatch_id, args.dispatch_capability)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
