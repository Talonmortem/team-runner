"""Read-only OpenCode response throughput, scoped to an injected Dispatch preamble."""
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3

PREAMBLE = 'You are working inside Orca, a multi-agent IDE. You are a dispatched worker.'


def measurement(message):
    """Response time includes API latency; never call this pure decode speed."""
    timing, tokens = message.get('time') or {}, message.get('tokens') or {}
    values = [timing.get('created'), timing.get('completed'),
              tokens.get('output'), tokens.get('reasoning')]
    if (not message.get('finish') or message['finish'] == 'error'
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values)):
        return None
    start, end, output, reasoning = values
    if end <= start:
        return None
    seconds = (end - start) / 1000
    return {'response_seconds': seconds, 'output_tokens': output, 'reasoning_tokens': reasoning,
            'tokens_per_second': (output + reasoning) / seconds, 'finish': message['finish'],
            'completed_at_ms': end}


def dispatch_metrics(connection, repo, agent):
    dispatch, task, terminal = (agent.get(k, '') for k in ('dispatch_id', 'task_id', 'terminal'))
    if not (re.fullmatch(r'ctx_[0-9a-f]{12}', dispatch)
            and re.fullmatch(r'task_[0-9a-f]{12}', task) and terminal):
        return {'status': 'unknown', 'reason': 'dispatch_identity_unavailable'}
    # Match the injected task + terminal + dispatch, never a mention in an assistant report.
    matches = []
    for session, seq, raw in connection.execute(
            "SELECT m.session_id, m.seq, m.data FROM session_message m "
            "JOIN session_v2 s ON s.id=m.session_id WHERE s.directory=? "
            "AND m.type='user' AND instr(m.data,?)>0", (str(Path(repo).resolve()), dispatch)):
        message = json.loads(raw)
        text = message.get('text', '')
        if (text.startswith(PREAMBLE) and f'Your task ID is: {task}\n' in text
                and f'--from {terminal} --type worker_done' in text
                and f'--task-id {task} --dispatch-id {dispatch} --outcome' in text):
            matches.append((session, seq))
    if len(matches) != 1:
        return {'status': 'unknown', 'reason': 'session_binding_ambiguous' if matches else 'session_not_found'}
    session, start_seq = matches[0]
    samples, excluded = [], 0
    for kind, raw in connection.execute(
            'SELECT type,data FROM session_message WHERE session_id=? AND seq>? ORDER BY seq',
            (session, start_seq)):
        message = json.loads(raw)
        if kind == 'user' and message.get('text', '').startswith(PREAMBLE):
            break  # A reused session's next Dispatch must not be charged to this one.
        if kind != 'assistant':
            continue
        sample = measurement(message)
        if sample is None:
            excluded += 1
        else:
            model = message.get('model') or {}
            sample['model'] = {k: model.get(k) for k in ('id', 'providerID', 'variant')}
            samples.append(sample)
    if not samples:
        return {'status': 'unknown', 'reason': 'no_completed_measurements', 'session_id': session,
                'excluded_responses': excluded}
    seconds = sum(s['response_seconds'] for s in samples)
    output = sum(s['output_tokens'] for s in samples)
    reasoning = sum(s['reasoning_tokens'] for s in samples)
    groups = {}
    for sample in samples:
        key = tuple(sample['model'].get(k) for k in ('id', 'providerID', 'variant'))
        group = groups.setdefault(key, {'model': sample['model'], 'responses': 0,
                                      'tokens': 0, 'response_seconds': 0})
        group['responses'] += 1
        group['tokens'] += sample['output_tokens'] + sample['reasoning_tokens']
        group['response_seconds'] += sample['response_seconds']
    for group in groups.values():
        group['tokens_per_second'] = group['tokens'] / group['response_seconds']
    return {'status': 'measured', 'by_model': list(groups.values()), 'source': 'OpenCode local response timestamps and usage',
            'scope': 'dispatch', 'basis': '(output + reasoning) / response time including API latency',
            'session_id': session, 'responses': len(samples), 'excluded_responses': excluded,
            'length_responses': sum(s['finish'] == 'length' for s in samples),
            'response_seconds': seconds, 'output_tokens': output, 'reasoning_tokens': reasoning,
            'tokens_per_second': (output + reasoning) / seconds, 'last': samples[-1]}


def annotate_agents(repo, agents, database=None):
    """Attach derived metrics to response objects only; never mutate snapshots or the DB."""
    path = Path(database) if database is not None else Path(
        os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/opencode.db'
    cloud = []
    for agent in agents:
        agent['token_speed'] = {'status': 'unknown', 'reason': 'runtime_not_supported'}
        if (agent.get('requested') or {}).get('agent') == 'opencode2':
            cloud.append(agent)
    if not cloud:
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            for agent in cloud:
                try:
                    agent['token_speed'] = dispatch_metrics(connection, repo, agent)
                except (ValueError, TypeError, AttributeError, sqlite3.Error):
                    agent['token_speed'] = {'status': 'unknown', 'reason': 'measurement_unavailable'}
    except sqlite3.Error:
        for agent in cloud:
            agent['token_speed'] = {'status': 'unknown', 'reason': 'database_unavailable'}
