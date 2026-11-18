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


class DispatchBindingError(ValueError):
    pass


def opencode_database():
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/opencode.db'


def dispatch_marker(text):
    if not isinstance(text, str) or not text.startswith(PREAMBLE):
        return None
    tasks = set(re.findall(r'^Your task ID is: (task_[0-9a-f]{12})$', text, re.M))
    commands = set(re.findall(r'--from (\S+) --type worker_done\b[^\n]*?'
                              r'--task-id (task_[0-9a-f]{12}) --dispatch-id (ctx_[0-9a-f]{12}) --outcome\b', text))
    if len(tasks) == len(commands) == 1:
        terminal, task, dispatch = commands.pop()
        if task in tasks:
            return dispatch, task, terminal
    # Even an unrecognized new Dispatch ends the preceding interval.
    return ()


def dispatch_index(connection, repo, dispatch=None):
    query = ("SELECT m.session_id, m.seq, m.data FROM session_message m "
             "JOIN session_v2 s ON s.id=m.session_id WHERE s.directory=? "
             "AND m.type='user' AND instr(m.data,?)>0")
    matches = {}
    for session, seq, raw in connection.execute(query, (str(Path(repo).resolve()), dispatch or PREAMBLE)):
        binding = dispatch_marker(json.loads(raw).get('text'))
        if binding:
            matches.setdefault(binding, set()).add((session, seq))
    return matches


def dispatch_responses(connection, repo, agent, bindings=None):
    """Resolve one exact injected preamble and stop at the next Dispatch."""
    dispatch, task, terminal = (agent.get(k, '') for k in ('dispatch_id', 'task_id', 'terminal'))
    if not (re.fullmatch(r'ctx_[0-9a-f]{12}', dispatch or '')
            and re.fullmatch(r'task_[0-9a-f]{12}', task or '') and terminal):
        raise DispatchBindingError('dispatch_identity_unavailable')
    if bindings is None:
        bindings = dispatch_index(connection, repo, dispatch)
    matches = bindings.get((dispatch, task, terminal), set())
    if len(matches) != 1:
        raise DispatchBindingError('session_binding_ambiguous' if matches else 'session_not_found')
    session, start_seq = next(iter(matches))
    responses = []
    incomplete = False
    for kind, seq, raw in connection.execute(
            'SELECT type,seq,data FROM session_message WHERE session_id=? AND seq>? ORDER BY seq',
            (session, start_seq)):
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError('expected OpenCode message object')
        except (TypeError, ValueError):
            # A damaged user record may hide the next Dispatch boundary.
            incomplete = True
            break
        if kind == 'user' and dispatch_marker(message.get('text')) is not None:
            break  # A reused session's next Dispatch must not be charged to this one.
        if kind == 'assistant':
            responses.append((seq, message))
    return session, responses, incomplete


def dispatch_metrics(connection, repo, agent):
    try:
        session, responses, incomplete = dispatch_responses(connection, repo, agent)
    except DispatchBindingError as exc:
        return {'status': 'unknown', 'reason': str(exc)}
    samples, excluded = [], int(incomplete)
    for _, message in responses:
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
    path = Path(database) if database is not None else opencode_database()
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
