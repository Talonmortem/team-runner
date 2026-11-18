"""One ownership classification for controls, notifications and UI diagnostics."""
import json


def payload(message):
    value = message.get('payload') or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def classify(message, current, registry):
    dispatch = payload(message).get('dispatchId')
    agents = [a for a in registry.get('agents', []) if dispatch and a.get('dispatch_id') == dispatch
              and a.get('run_id') == message.get('run_id')]
    if len(agents) != 1:
        return {'kind': 'unattributed', 'reason': 'no unique registered run/Dispatch'}
    agent = agents[0]
    base = {'agent': agent.get('id'), 'dispatch': dispatch}
    if agent.get('superseded_by') or agent.get('status') == 'superseded':
        return dict(base, kind='historical', reason='registered session superseded')
    if agent.get('status') in ('released', 'stopped'):
        return dict(base, kind='historical', reason='registered session completed')
    if agent.get('role') == 'coordinator':
        if dispatch != current.get('dispatch_id'):
            return dict(base, kind='historical', reason='previous coordinator Dispatch')
    elif not registry.get('graph_calls') and not agent.get('call_id'):
        if not any(a.get('role') == 'coordinator' and a.get('dispatch_id') == current.get('dispatch_id')
                   for a in registry.get('agents', [])):
            return dict(base, kind='unattributed', reason='current coordinator is not registered')
    else:
        # Graph ownership follows every registered call, never prose in a message.
        seen = set()
        child = agent
        while child.get('call_id'):
            if child['id'] in seen:
                return dict(base, kind='unattributed', reason='cyclic call chain')
            seen.add(child['id'])
            call = next((c for c in registry.get('graph_calls', []) if c['id'] == child['call_id']), None)
            if not call or call.get('child_agent') != child['id'] or call.get('dispatch_id') != child.get('dispatch_id'):
                return dict(base, kind='unattributed', reason='call/Dispatch chain mismatch')
            if call.get('status') in ('completed', 'stopped'):
                return dict(base, kind='historical', reason='call already closed')
            child = next((a for a in registry['agents'] if a['id'] == call.get('parent_agent')), {})
            if not child:
                return dict(base, kind='unattributed', reason='missing registered parent')
        if child.get('role') != 'coordinator' or child.get('dispatch_id') != current.get('dispatch_id'):
            return dict(base, kind='unattributed', reason='chain does not reach current coordinator')
    if message.get('type') != 'worker_done' and any(
            m.get('type') == 'worker_done' and m.get('run_id') == message.get('run_id')
            and payload(m).get('dispatchId') == dispatch for m in current.get('messages', [])):
        return dict(base, kind='historical', reason='registered session has a persisted completion report')
    technical_end = (message.get('type') == 'escalation'
                     and payload(message).get('stopReason') == 'finished'
                     and payload(message).get('taskId') == agent.get('task_id')
                     and payload(message).get('handle') == agent.get('terminal'))
    if technical_end:
        if any(w.get('dispatch') == dispatch and w.get('status') in ('waiting', 'notified')
               and message.get('id') in w.get('technical_events', [])
               for w in current.get('resource_waits', {}).values()):
            return dict(base, kind='waiting_resource', reason='registered native resource wait; processing unconfirmed')
        calls = {c['id']: c for c in registry.get('graph_calls', []) if c.get('parent_agent') == agent.get('id')}
        previous = message.get('classification') or {}
        remembered = previous.get('waiting_calls', [])
        if (remembered and previous.get('agent') == agent.get('id') and previous.get('dispatch') == dispatch
                and all(c in calls and calls[c].get('status') in ('completed', 'stopped') for c in remembered)):
            return dict(base, kind='historical', reason='registered child wait already settled', waiting_calls=remembered)
        opened = [c for c, value in calls.items() if value.get('status') not in ('completed', 'stopped')]
        if agent.get('status') == 'waiting_child' and opened:
            return dict(base, kind='waiting_child', reason='registered child call remains open', waiting_calls=opened)
    if (message.get('type') == 'escalation' and payload(message).get('stopReason') in ('finished', 'errored')
            and payload(message).get('taskId') == agent.get('task_id')
            and payload(message).get('handle') == agent.get('terminal')):
        from datetime import datetime
        try:
            ended = datetime.fromisoformat(message['created_at'].replace('Z', '+00:00'))
            ended_at = ended.timestamp() if ended.tzinfo else None
        except (KeyError, TypeError, ValueError, AttributeError):
            ended_at = None
        proof = agent.get('working_evidence') or {}
        # Event timestamps have second precision. A later exact activity event
        # proves another turn; queue acceptance alone cannot close an escalation.
        if (ended_at is not None and proof.get('dispatch') == dispatch
                and proof.get('source') == 'exact fresh agent_status'
                and isinstance(proof.get('at'), (int, float)) and proof['at'] >= ended_at + 1):
            return dict(base, kind='historical', reason='exact registered session resumed after technical turn end')
    return dict(base, kind='active', reason='current registered session and call chain')


def messages(current, registry):
    return [dict(m, classification=classify(m, current, registry)) for m in current.get('messages', [])]


def pending(current, registry, types=('question', 'escalation')):
    return [m for m in messages(current, registry) if m.get('type') in types
            and not m.get('closed') and not m.get('answered') and m['classification']['kind'] == 'active']
