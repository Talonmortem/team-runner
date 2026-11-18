"""Read local usage only when checkout and registered Dispatch can be proven."""
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import heapq
import json
import math
import os
from pathlib import Path
import sqlite3
from sys import getsizeof

from .runner_metrics import (DispatchBindingError, PREAMBLE, dispatch_index, dispatch_marker,
                             dispatch_responses, measurement, opencode_database)
from .runner_store import read_json


PREAMBLE_BYTES = PREAMBLE.encode()
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
CODEX_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens')
CLAUDE_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
COVERAGE = ('measured', 'partial', 'unknown')
LOG_CACHE_MAX_FILES = 8192  # Per runtime, including files without matching Dispatches.
LOG_CACHE_MAX_BYTES = 16 * 1024 * 1024  # Estimated parsed size per runtime.


def timestamp(value):
    if type(value) in (int, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except (OSError, OverflowError, ValueError):
            pass
    return None


def counters(usage, fields):
    if not isinstance(usage, dict):
        usage = {}
    return tuple(n if type(n) is int and n >= 0 else None for n in map(usage.get, fields))


def canonical_checkout(value):
    if isinstance(value, str) and Path(value).is_absolute():
        return str(Path(value).resolve())
    return ''


def message_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(p.get('text', '') for p in value
                       if isinstance(p, dict) and p.get('type') in ('input_text', 'text')
                       and isinstance(p.get('text'), str))
    return ''


def load_attempts(catalog):
    """Read every retained attempt and include dated history whose registry is missing."""
    current, uses = {}, []
    for pid, project in catalog.read()['projects'].items():
        for wid in project['workspaces']:
            context = catalog.context(pid, wid, allow_disabled=True)
            checkout = canonical_checkout(str(context.repo))
            state = read_json(context.state_dir / 'state.json', {})
            active = state.get('current') or {}
            active_attempt = active.get('attempt_dir') or active.get('dispatch_id')
            current.setdefault(pid, {})[wid] = active_attempt
            attempts = context.state_dir / 'attempts'
            history = {row.get('attempt_dir') or row.get('dispatch_id'): row
                       for row in state.get('history', []) if row.get('attempt_dir') or row.get('dispatch_id')}
            attempts_to_read = set(history) | {str(path.parent) for path in attempts.glob('*/context.json')}
            if active_attempt:
                attempts_to_read.add(active_attempt)
            for key in sorted(attempts_to_read):
                row = active if key == active_attempt else history.get(key, {})
                finished = timestamp(row.get('finished_at'))
                started = timestamp(row.get('started_at'))
                path = Path(key)
                owned = (path.is_absolute() and path.parent == attempts and path.resolve() == path
                         and read_json(path / 'context.json', {}).get('identity') == context.identity())
                agents = read_json(path / 'agents.json', {}).get('agents', []) if owned else []
                if not agents and (row or key == active_attempt or owned):
                    agents = [{**row, 'role': 'coordinator'}]
                    reason = 'registry_unavailable'
                else:
                    reason = None
                for agent in agents:
                    requested = agent.get('requested') or {}
                    observed = agent.get('observed') or {}
                    runtime = observed.get('agent') or requested.get('agent') or 'unknown'
                    if runtime == 'opencode2':
                        runtime = 'opencode'
                    agent_reason = reason
                    if not all(agent.get(k) for k in ('dispatch_id', 'task_id', 'terminal')):
                        agent_reason = reason or 'dispatch_identity_unavailable'
                    if owned and agent.get('attempt', key) != key:
                        agent_reason = 'attempt_binding_mismatch'
                    uses.append(DispatchUse(
                        (pid, wid, checkout, agent.get('role') or 'unknown', runtime), key, agent,
                        started, finished, agent_reason))
    return current, uses


@dataclass
class DispatchUse:
    group: tuple
    attempt: str
    agent: dict
    started: float | None
    finished: float | None
    reason: str | None
    samples: list = field(default_factory=list)
    issues: set = field(default_factory=set)

    @property
    def binding(self):
        return (self.group[2], self.agent.get('dispatch_id'),
                self.agent.get('task_id'), self.agent.get('terminal'))


@dataclass(frozen=True)
class Sample:
    at: float | None
    tokens: tuple
    response_seconds: float = 0
    speed_tokens: int = 0


@dataclass
class Segment:
    binding: tuple
    started: float | None
    samples: list = field(default_factory=list)
    issues: set = field(default_factory=set)


def log_rows(stream, runtime):
    """Parse only useful records. An invalid line can obscure a Dispatch boundary."""
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        if not raw.startswith(b'{') or not raw.endswith(b'}'):
            yield None
            continue
        if PREAMBLE_BYTES not in raw:
            if runtime == 'codex':
                if not any(k in raw for k in (b'"token_count"', b'"turn_context"', b'"session_meta"')):
                    continue
            elif b'"assistant"' not in raw:
                continue
        try:
            event = json.loads(raw)
            yield event if isinstance(event, dict) else None
        except (ValueError, UnicodeError):
            yield None


def codex_log(path, checkouts):
    segments = []
    with path.open('rb') as stream:
        # A complete Codex rollout identifies the session before any usage counters.
        first = json.loads(stream.readline())
        meta = first.get('payload') or {}
        if first.get('type') != 'session_meta':
            return '', []
        session = meta.get('id')
        directory = canonical_checkout(meta.get('cwd'))
        if not session or directory not in checkouts:
            return '', []
        active = None
        previous = None
        clean_head = True
        last_marker_kind = None
        observed_dir = directory
        for event in log_rows(stream, 'codex'):
            if event is None:
                if active:
                    active.issues.add('incomplete_log')
                active, previous, clean_head, last_marker_kind = None, None, False, None
                continue
            payload = event.get('payload') or {}
            kind = event.get('type')
            if kind == 'turn_context' and payload.get('cwd'):
                observed_dir = canonical_checkout(payload['cwd'])
                if active and observed_dir != directory:
                    active.issues.add('checkout_changed')
                    active = None
            new_marker = None
            representation = None
            if kind == 'response_item' and payload.get('role') == 'user':
                new_marker = dispatch_marker(message_text(payload.get('content')))
                representation = 'response_item'
            elif payload.get('type') == 'user_message':
                new_marker = dispatch_marker(message_text(payload.get('message')))
                representation = 'user_message'
            if new_marker is not None:
                binding = (directory, *new_marker)
                # Codex may record the same user turn in both the event and replay streams.
                if (active and active.binding == binding and last_marker_kind is not None
                        and last_marker_kind != representation):
                    last_marker_kind = None
                    continue
                last_marker_kind = representation
                at = timestamp(event.get('timestamp'))
                active = Segment(binding, at) if new_marker and observed_dir == directory else None
                if active:
                    segments.append(active)
                continue
            if payload.get('type') != 'token_count' or not payload.get('info'):
                continue
            last_marker_kind = None
            info = payload['info']
            total = counters(info.get('total_token_usage'), CODEX_FIELDS)
            last = counters(info.get('last_token_usage'), CODEX_FIELDS)
            if previous is None and clean_head:
                # At the real session head Codex starts at zero. A nonzero inherited
                # balance proven by last_token_usage leaves the delta unconfirmed.
                previous = tuple(0 if delta is None or value == delta else None
                                 for value, delta in zip(total, last))
            delta = tuple(new - old if old is not None and new is not None and new >= old else None
                          for new, old in zip(total, previous or (None,) * len(TOKEN_FIELDS)))
            previous, clean_head = total, False
            if not active:
                continue
            if None in delta[:2]:
                active.issues.add('counter_baseline_unavailable')
            if not any(n is not None and n > 0 for n in delta):
                continue
            at = timestamp(event.get('timestamp'))
            if at is None:
                active.issues.add('timestamp_unavailable')
            else:
                active.samples.append(Sample(at, delta))
        return session, segments


def claude_log(path, checkouts):
    segments, active = [], None
    session = None
    with path.open('rb') as stream:
        for event in log_rows(stream, 'claude'):
            if event is None:
                if active:
                    active.issues.add('incomplete_log')
                active = None
                continue
            message = event.get('message') or {}
            if not isinstance(message, dict) or event.get('isSidechain'):
                continue
            if event.get('type') == 'user':
                new_marker = dispatch_marker(message_text(message.get('content')))
                if new_marker is not None:
                    directory = canonical_checkout(event.get('cwd'))
                    session = event.get('sessionId') or session
                    active = None
                    if new_marker and directory in checkouts and session:
                        active = Segment((directory, *new_marker), timestamp(event.get('timestamp')))
                        segments.append(active)
                continue
            if not active or event.get('type') != 'assistant':
                continue
            if ((event.get('cwd') and canonical_checkout(event['cwd']) != active.binding[0])
                    or (event.get('sessionId') and event['sessionId'] != session)):
                active.issues.add('session_identity_changed')
                active = None
                continue
            usage = counters(message.get('usage'), CLAUDE_FIELDS)
            at, msg_id = timestamp(event.get('timestamp')), message.get('id')
            if message.get('model') == '<synthetic>':
                continue
            if not isinstance(msg_id, str) or not msg_id or at is None:
                active.issues.add('incomplete_message')
                continue
            active.samples.append((msg_id, Sample(at, usage)))
    return session, segments


def apply_segments(entries, logs, runtime):
    """Treat a repeated marker in conflicting rollouts as an ambiguous binding."""
    bindings = defaultdict(list)
    message_bindings = defaultdict(set)
    for session, variants in logs.items():
        for segments in variants:
            for segment in segments:
                bindings[segment.binding].append((session, segment))
                if runtime == 'claude':
                    for message_id, _ in segment.samples:
                        message_bindings[message_id].add(segment.binding)
    for use in entries:
        matches = bindings[use.binding]
        if not matches:
            use.reason = 'session_not_found'
        elif len(matches) != 1:
            use.reason = 'session_binding_ambiguous'
        else:
            segment = matches[0][1]
            if segment.started is not None and use.started is None:
                use.started = segment.started
            use.issues.update(segment.issues)
            if runtime == 'codex':
                use.samples = segment.samples
            else:
                messages = {}
                for message_id, sample in segment.samples:
                    if len(message_bindings[message_id]) > 1:
                        use.issues.add('message_binding_ambiguous')
                        continue
                    previous = messages.get(message_id)
                    if previous:
                        if previous.at is None:
                            continue
                        # Claude repeats the assistant ID across streamed chunks.
                        # Input/cache should agree; output is cumulative across chunks.
                        if any(old != new and old is not None and new is not None
                               for index, (old, new) in enumerate(zip(previous.tokens, sample.tokens))
                               if index != 1):
                            use.issues.add('message_usage_conflict')
                            messages[message_id] = Sample(None, ())
                            continue
                        values = tuple(max((n for n in pair if n is not None), default=None)
                                       for pair in zip(previous.tokens, sample.tokens))
                        sample = Sample(min(previous.at, sample.at), values)
                    messages[message_id] = sample
                for sample in messages.values():
                    if sample.at is None:
                        continue
                    incoming, outgoing, read, write = sample.tokens
                    if incoming is None or outgoing is None:
                        use.issues.add('incomplete_message')
                    if incoming is not None and (read is None or write is None):
                        use.issues.add('input_cache_incomplete')
                    if any(value is not None for value in sample.tokens):
                        total_in = incoming + (read or 0) + (write or 0) if incoming is not None else None
                        use.samples.append(Sample(sample.at, (total_in, outgoing, read, write)))


def opencode_usage(connection, use, bindings):
    try:
        _, responses, incomplete = dispatch_responses(connection, use.group[2], use.agent, bindings)
    except DispatchBindingError as exc:
        use.reason = str(exc)
        return
    except (TypeError, ValueError, AttributeError, sqlite3.Error):
        use.reason = 'database_read_incomplete'
        return
    if incomplete:
        use.issues.add('incomplete_log')
    by_sequence = defaultdict(list)
    for seq, response in responses:
        if response not in by_sequence[seq]:
            by_sequence[seq].append(response)
    for group in by_sequence.values():
        if len(group) != 1:
            use.issues.add('message_usage_conflict')
            continue
        response = group[0]
        tokens = response.get('tokens') or {}
        cache = tokens.get('cache') or {}
        at_ms = (response.get('time') or {}).get('completed')
        at = timestamp(at_ms / 1000) if type(at_ms) in (int, float) else None
        incoming, outgoing, reasoning = counters(tokens, ('input', 'output', 'reasoning'))
        read, write = counters(cache, ('read', 'write'))
        if at is None or not response.get('finish'):
            use.issues.add('incomplete_response')
            continue
        if incoming is None or outgoing is None or reasoning is None:
            use.issues.add('incomplete_response')
        speed = measurement(response)
        total_in = incoming + (read or 0) + (write or 0) if incoming is not None else None
        total_out = outgoing + (reasoning or 0) if outgoing is not None else None
        if any(value is not None for value in (total_in, total_out, read, write)):
            use.samples.append(Sample(at, (total_in, total_out, read, write),
                                      speed['response_seconds'] if speed else 0,
                                      speed['output_tokens'] + speed['reasoning_tokens'] if speed else 0))
        if read is None or write is None:
            use.issues.add('input_cache_incomplete')


def report(uses, now, lower=None):
    by_group = defaultdict(list)
    for use in uses:
        if lower is not None and use.finished is not None and use.finished < lower:
            continue
        if use.started is not None and use.started >= now:
            continue
        # Persisted history's finished_at is recorded at whole-second precision.
        end = min(now, use.finished + 1) if use.finished is not None else now
        observed = [s for s in use.samples if s.at < end and (lower is None or s.at >= lower)]
        if lower is not None and not observed and use.samples and not use.issues and not use.reason:
            continue
        by_group[use.group].append((use, observed))
    total_coverage = dict.fromkeys(COVERAGE, 0)
    rows = []
    for (pid, wid, checkout, role, runtime), group in sorted(by_group.items()):
        coverage = dict.fromkeys(COVERAGE, 0)
        reasons = Counter()
        all_samples = []
        for use, observed in group:
            all_samples.extend(observed)
            issues = use.issues | ({use.reason} if use.reason else set())
            level = 'partial' if observed and issues else 'measured' if observed else 'unknown'
            coverage[level] += 1
            reasons.update(issues or ({'no_token_measurements'} if not observed else set()))
        for level, count in coverage.items():
            total_coverage[level] += count
        row = dict(project_id=pid, workspace_id=wid, checkout=checkout, role=role, runtime=runtime,
                   coverage=coverage, unknown_reasons=dict(reasons))
        for key, values in zip(TOKEN_FIELDS, zip(*(s.tokens for s in all_samples))):
            known = [n for n in values if n is not None]
            row[key] = sum(known) if known else None
        for key in TOKEN_FIELDS:
            row.setdefault(key, None)
        seconds = sum(s.response_seconds for s in all_samples)
        row['tokens_per_second'] = sum(s.speed_tokens for s in all_samples) / seconds if seconds else None
        rows.append(row)
    return {'coverage': total_coverage, 'rows': rows}


def segment_prefix(older, newer):
    """A copied rollout can end earlier, but conflicting copies prove no unique binding."""
    if len(older) > len(newer):
        return False
    for index, (left, right) in enumerate(zip(older, newer)):
        if left.binding != right.binding or left.started != right.started:
            return False
        if left == right:
            continue
        if index != len(older) - 1 or left.samples != right.samples[:len(left.samples)]:
            return False
        if not (left.issues <= right.issues or left.issues - right.issues == {'incomplete_log'}
                and len(left.samples) < len(right.samples)):
            return False
    return True


def parsed_size(value):
    """Estimate retained size, counting shared values each time they occur."""
    size = getsizeof(value)
    if isinstance(value, (Segment, Sample)):
        return size + getsizeof(vars(value)) + sum(map(parsed_size, vars(value).values()))
    if isinstance(value, (list, tuple, set)):
        return size + sum(map(parsed_size, value))
    return size


class UsageCollector:
    def __init__(self, *, codex_root=None, claude_root=None, opencode_db=None):
        codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        claude_home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        self.codex_roots = [Path(codex_root)] if codex_root is not None else [
            codex_home / 'sessions', codex_home / 'archived_sessions']
        self.claude_root = Path(claude_root) if claude_root is not None else claude_home / 'projects'
        self.opencode_db = Path(opencode_db) if opencode_db is not None else opencode_database()
        self.cache = {}

    def _logs(self, roots, runtime, pattern, checkouts, parse):
        logs = defaultdict(list)
        previous_checkouts, previous = self.cache.pop(runtime, (None, {}))
        if previous_checkouts != checkouts:
            previous = {}
        retained, oldest = {}, []
        retained_bytes = 0
        for root in roots:
            for path in root.rglob(pattern):
                if path.is_symlink():
                    continue
                try:
                    stat = path.stat()
                    stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    entry = previous.get(path)
                    if entry is None or stamp != entry[0]:
                        parsed = parse(path, checkouts)
                        entry = stamp, parsed, parsed_size(parsed)
                    _, (session, segments), size = entry
                except (OSError, ValueError, TypeError, AttributeError):
                    continue  # A broken transcript cannot establish a session binding.
                # Keep the newest entries across scans. Scan order alone would
                # make a full LRU reparse even unchanged files on every poll.
                if size <= LOG_CACHE_MAX_BYTES and path not in retained:
                    heapq.heappush(oldest, (stat.st_mtime_ns, path))
                    retained[path] = entry
                    retained_bytes += size
                    while len(retained) > LOG_CACHE_MAX_FILES or retained_bytes > LOG_CACHE_MAX_BYTES:
                        _, expired = heapq.heappop(oldest)
                        retained_bytes -= retained.pop(expired)[2]
                if session and segments:
                    variants = logs[session]
                    if not any(segment_prefix(segments, variant) for variant in variants):
                        logs[session] = [variant for variant in variants
                                         if not segment_prefix(variant, segments)] + [segments]
        self.cache[runtime] = checkouts, retained
        return logs

    def collect(self, catalog, *, now=None):
        now = timestamp(now) if now is not None else datetime.now(timezone.utc).timestamp()
        if now is None:
            raise ValueError('invalid token report timestamp')
        current, uses = load_attempts(catalog)
        by_dispatch = Counter((u.group[-1], u.agent.get('dispatch_id')) for u in uses if not u.reason)
        runtimes = defaultdict(list)
        for use in uses:
            runtime = use.group[-1]
            if use.reason:
                continue
            if by_dispatch[(runtime, use.agent['dispatch_id'])] != 1:
                use.reason = 'dispatch_registration_ambiguous'
            else:
                runtimes[runtime].append(use)
        for runtime, roots, pattern, parser in (
                ('codex', self.codex_roots, 'rollout-*.jsonl', codex_log),
                ('claude', [self.claude_root], '*.jsonl', claude_log)):
            entries = runtimes.pop(runtime, [])
            if entries:
                if not any(root.is_dir() for root in roots):
                    for use in entries:
                        use.reason = 'source_unavailable'
                    self.cache.pop(runtime, None)
                    continue
                checkouts = frozenset(u.group[2] for u in entries)
                apply_segments(entries, self._logs(roots, runtime, pattern, checkouts, parser), runtime)
            else:
                self.cache.pop(runtime, None)
        opencode = runtimes.pop('opencode', [])
        if opencode:
            try:
                with closing(sqlite3.connect(self.opencode_db.resolve().as_uri() + '?mode=ro',
                                             uri=True, timeout=.1)) as connection:
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('BEGIN')
                    indexes = {}
                    for checkout in {u.group[2] for u in opencode}:
                        try:
                            indexes[checkout] = dispatch_index(connection, checkout)
                        except (ValueError, TypeError, AttributeError, sqlite3.Error):
                            indexes[checkout] = None
                    for use in opencode:
                        index = indexes[use.group[2]]
                        if index is None:
                            use.reason = 'database_read_incomplete'
                        else:
                            opencode_usage(connection, use, index)
            except sqlite3.Error:
                for use in opencode:
                    use.reason = 'database_unavailable'
        for entries in runtimes.values():
            for use in entries:
                use.reason = 'runtime_not_supported'
        current_uses = defaultdict(list)
        for use in uses:
            current_uses[(*use.group[:2], use.attempt)].append(use)
        return {'collected_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                'current': {pid: {wid: {'attempt': attempt, **report(current_uses[(pid, wid, attempt)], now)}
                                  for wid, attempt in workspaces.items()}
                            for pid, workspaces in current.items()},
                'history': {str(days): report(uses, now, now - days * 86400) for days in (7, 30)}}


def collect(catalog, *, now=None, codex_root=None, claude_root=None, opencode_db=None):
    return UsageCollector(codex_root=codex_root, claude_root=claude_root,
                          opencode_db=opencode_db).collect(catalog, now=now)
