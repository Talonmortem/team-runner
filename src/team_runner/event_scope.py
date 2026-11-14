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
    base = {'agent': agent['id'], 'dispatch': dispatch}
    if agent.get('superseded_by') or agent.get('status') in ('released', 'stopped', 'superseded'):
        return dict(base, kind='historical', reason='registered session completed or superseded')
    if agent.get('role') == 'coordinator':
        if dispatch != current.get('dispatch_id'):
            return dict(base, kind='historical', reason='previous coordinator Dispatch')
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
    if agent.get('status') == 'waiting_child' and any(
            c.get('parent_agent') == agent['id'] and c.get('status') not in ('completed', 'stopped')
            for c in registry.get('graph_calls', [])):
        return dict(base, kind='waiting_child', reason='registered child call remains open')
    return dict(base, kind='active', reason='current registered session and call chain')


def messages(current, registry):
    return [dict(m, classification=classify(m, current, registry)) for m in current.get('messages', [])]


def pending(current, registry, types=('question', 'escalation')):
    return [m for m in messages(current, registry) if m.get('type') in types
            and not m.get('closed') and not m.get('answered') and m['classification']['kind'] == 'active']
