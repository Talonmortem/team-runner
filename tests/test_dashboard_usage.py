import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from team_runner import dashboard_usage
from team_runner.catalog import Catalog
from team_runner.dashboard_usage import UsageCollector
from team_runner.runner_metrics import PREAMBLE
from team_runner.runner_store import atomic_json


NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
PROJECT = '9b1cafd4-3ba2-4a39-8f17-319b17b5bd45'
MAIN = 'cde0edd1-2e1f-4a7e-affb-61dd4f62a192'
OTHER = '0d69005d-65b5-491d-a1aa-24f3e72c3e0f'


def stamp(days=0, seconds=0):
    return (NOW + timedelta(days=days, seconds=seconds)).isoformat()


def agent(number, runtime, role):
    return {'dispatch_id': f'ctx_{number:012x}', 'task_id': f'task_{number:012x}',
            'terminal': f'term_{number}', 'requested': {'agent': runtime}, 'role': role}


def prompt(value):
    return (f"{PREAMBLE}\nYour task ID is: {value['task_id']}\n"
            f"orca orchestration send --from {value['terminal']} --type worker_done "
            f"--task-id {value['task_id']} --dispatch-id {value['dispatch_id']} --outcome succeeded")


def codex_marker(value, at, duplicate_representation=False):
    if duplicate_representation:
        return {'timestamp': at, 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': prompt(value)}}
    return {'timestamp': at, 'type': 'response_item', 'payload': {'role': 'user', 'type': 'message',
                                                               'content': [{'type': 'input_text', 'text': prompt(value)}]}}


def codex_count(at, incoming, outgoing, read=0, write=0, last=None):
    return {'timestamp': at, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': dict(input_tokens=incoming, output_tokens=outgoing,
                                  cached_input_tokens=read, cache_write_input_tokens=write),
        'last_token_usage': last}}}


def claude_marker(value, checkout, at, session='claude-session'):
    return {'timestamp': at, 'type': 'user', 'cwd': str(checkout), 'sessionId': session,
            'message': {'role': 'user', 'content': prompt(value)}}


def claude_message(message_id, at, incoming=30, outgoing=20, read=5, write=4):
    return {'timestamp': at, 'type': 'assistant', 'sessionId': 'claude-session', 'message': {
        'id': message_id, 'usage': {'input_tokens': incoming, 'output_tokens': outgoing,
                                    'cache_read_input_tokens': read, 'cache_creation_input_tokens': write}}}


def rows(report):
    return {row['role']: row for row in report['rows']}


class DashboardUsage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.catalog = Catalog(self.root / 'config', self.root / 'state')
        self.repo = self.root / 'main'
        self.other_repo = self.root / 'main-copy'
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        self.db = self.root / 'opencode.db'
        for directory in (self.repo, self.other_repo, self.codex, self.claude):
            directory.mkdir()
        workspaces = {}
        for wid, checkout in ((MAIN, self.repo), (OTHER, self.other_repo)):
            workspaces[wid] = dict(path=str(checkout), name=wid, disabled=False, profile={}, roles={},
                                   orca_id='repo::' + str(checkout), orca_instance='instance-' + wid)
        atomic_json(self.catalog.path, {'projects': {PROJECT: dict(workspaces=workspaces, roles={}, profile={})}})
        self.collector = UsageCollector(codex_root=self.codex, claude_root=self.claude, opencode_db=self.db)

    def setup_attempt(self, name, agents, *, current=True, wid=MAIN, finished=None):
        context = self.catalog.context(PROJECT, wid)
        attempt = context.state_dir / 'attempts' / name
        atomic_json(attempt / 'context.json', {'identity': context.identity()})
        atomic_json(attempt / 'agents.json', {'agents': [{**value, 'attempt': str(attempt)} for value in agents]})
        state_path = context.state_dir / 'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if current:
            state['current'] = {'attempt_dir': str(attempt)}
        else:
            state.setdefault('history', []).append({'attempt_dir': str(attempt), 'finished_at': finished})
        atomic_json(state_path, state)

    def open_database(self, directory=None):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        conn.executescript('CREATE TABLE session_v2(id TEXT, directory TEXT); '
                           'CREATE TABLE session_message(session_id TEXT, type TEXT, seq INTEGER, data TEXT);')
        conn.execute('INSERT INTO session_v2 VALUES (?,?)', ('open-session', str(directory or self.repo)))
        return conn

    @staticmethod
    def write_log(path, *records, mode='w'):
        with path.open(mode) as handle:
            for record in records:
                handle.write(json.dumps(record) + '\n')

    def codex_log(self, filename, *records, checkout=None, session='codex-session'):
        path = self.codex / filename
        self.write_log(path, {'type': 'session_meta', 'payload': {'id': session, 'cwd': str(checkout or self.repo)}},
                       *records)
        return path

    @staticmethod
    def oc_record(conn, kind, seq, record):
        conn.execute('INSERT INTO session_message VALUES (?,?,?,?)',
                     ('open-session', kind, seq, json.dumps(record)))
        conn.commit()

    def oc_response(self, conn, seq, at=stamp(-1), **tokens):
        end = int(datetime.fromisoformat(at).timestamp() * 1000)
        self.oc_record(conn, 'assistant', seq,
                       {'finish': 'stop', 'time': {'created': end - 2000, 'completed': end},
                        'tokens': dict(input=15, output=35, reasoning=5, cache={'read': 8, 'write': 2}) | tokens})

    def collect(self):
        return self.collector.collect(self.catalog, now=NOW.timestamp())

    def test_three_runtimes_reuse_duplicates_and_period_boundaries(self):
        c1, c2 = agent(1, 'codex', 'planner'), agent(2, 'codex', 'implementer')
        h1, h2 = agent(3, 'claude', 'coordinator'), agent(4, 'claude', 'tester')
        o1 = agent(5, 'opencode2', 'reviewer')
        self.setup_attempt('old-attempt', [c1], current=False, finished=stamp(-1))
        self.setup_attempt('active-attempt', [c2, h1, h2, o1])
        old_log = self.codex_log('rollout-old.jsonl', codex_count(stamp(-40), 500, 100, 100, 8),
                                 codex_marker(c1, stamp(-35)), codex_count(stamp(-30, -1), 510, 102, 102, 8),
                                 codex_count(stamp(-30), 520, 104, 104, 9))
        self.codex_log('rollout-current.jsonl', codex_count(stamp(-40), 500, 100, 100, 8),
                       codex_marker(c1, stamp(-35)), codex_count(stamp(-30, -1), 510, 102, 102, 8),
                       codex_count(stamp(-30), 520, 104, 104, 9),
                       codex_count(stamp(-7), 531, 107, 107, 10),
                       codex_marker(c2, stamp(-2)), codex_marker(c2, stamp(-2, 1), True),
                       codex_count(stamp(-1), 550, 110, 110, 11),
                       codex_count(stamp(-1, 1), 550, 110, 110, 11),
                       codex_count(stamp(), 999, 999, 114, 11))
        (self.codex / 'rollout-copy.jsonl').write_bytes(old_log.read_bytes())
        self.write_log(self.claude / 'claude-session.jsonl',
                       claude_marker(h1, self.repo, stamp(-2)),
                       claude_message('m1', stamp(-1), outgoing=3), claude_message('m1', stamp(-1, 2), outgoing=20),
                       claude_marker(h2, self.repo, stamp(-1, 3)),
                       claude_message('m2', stamp(-1, 4), outgoing=50))
        conn = self.open_database()
        self.oc_record(conn, 'user', 1, {'text': prompt(o1)})
        self.oc_response(conn, 2)
        self.oc_response(conn, 2)  # repeated export of the same sequence
        self.oc_record(conn, 'user', 3, {'text': prompt(agent(6, 'opencode2', 'unregistered'))})
        self.oc_response(conn, 4, input=9999)

        document = self.collect()
        self.assertEqual(document, self.collect())
        current = rows(document['current'][PROJECT][MAIN])
        week, month = (rows(document['history'][period]) for period in ('7', '30'))
        self.assertEqual(set(current), {'implementer', 'coordinator', 'tester', 'reviewer'})
        self.assertEqual(document['history']['30']['coverage'], {'measured': 5, 'partial': 0, 'unknown': 0})
        self.assertEqual([week['planner'][key] for key in ('input_tokens', 'output_tokens', 'cache_read_tokens',
                                                          'cache_write_tokens')], [11, 3, 3, 1])
        self.assertEqual(month['planner']['input_tokens'], 21)
        self.assertEqual(current['implementer']['input_tokens'], 19)
        self.assertEqual(current['coordinator']['input_tokens'], 39)
        self.assertEqual(current['coordinator']['output_tokens'], 20)
        self.assertEqual(current['tester']['output_tokens'], 50)
        self.assertEqual(current['reviewer']['input_tokens'], 25)
        self.assertEqual(current['reviewer']['output_tokens'], 40)
        self.assertEqual(current['reviewer']['tokens_per_second'], 20)

    def test_checkout_exact_and_missing_data_are_unknown(self):
        c, h, o = agent(1, 'codex', 'planner'), agent(2, 'claude', 'coordinator'), agent(3, 'opencode2', 'reviewer')
        self.setup_attempt('active-attempt', [c, h, o])
        self.codex_log('rollout-other.jsonl', codex_marker(c, stamp(-2)), codex_count(stamp(-1), 100, 20),
                       checkout=self.other_repo)
        self.write_log(self.claude / 'foreign.jsonl', claude_marker(h, self.other_repo, stamp(-2)),
                       claude_message('m', stamp(-1)))
        conn = self.open_database(self.other_repo)
        self.oc_record(conn, 'user', 1, {'text': prompt(o)})
        self.oc_response(conn, 2)
        current = self.collect()['current'][PROJECT][MAIN]
        self.assertEqual(current['coverage'], {'measured': 0, 'partial': 0, 'unknown': 3})
        for value in current['rows']:
            self.assertEqual(value['unknown_reasons'], {'session_not_found': 1})
            self.assertIsNone(value['input_tokens'])
            self.assertIsNone(value['output_tokens'])

    def test_conflicting_bindings_and_incomplete_sources(self):
        c, h, o = agent(1, 'codex', 'planner'), agent(2, 'claude', 'coordinator'), agent(3, 'opencode2', 'reviewer')
        self.setup_attempt('active-attempt', [c, h, o])
        for session in ('first', 'second'):
            self.codex_log(f'rollout-{session}.jsonl', codex_marker(c, stamp(-2)),
                           codex_count(stamp(-1), 100, 20), session=session)
        claude = self.claude / 'claude-session.jsonl'
        self.write_log(claude, claude_marker(h, self.repo, stamp(-2)),
                       claude_message('m', stamp(-1), write=None))
        with claude.open('a') as handle:
            handle.write('{"type":"user", "message":')
        current = rows(self.collect()['current'][PROJECT][MAIN])
        self.assertEqual(current['planner']['unknown_reasons'], {'session_binding_ambiguous': 1})
        self.assertEqual(current['coordinator']['coverage']['partial'], 1)
        self.assertEqual(current['coordinator']['unknown_reasons'], {'incomplete_log': 1, 'input_cache_incomplete': 1})
        self.assertEqual(current['coordinator']['input_tokens'], 35)
        self.assertIsNone(current['coordinator']['cache_write_tokens'])
        self.assertEqual(current['reviewer']['unknown_reasons'], {'database_unavailable': 1})
        self.assertFalse(self.db.exists())

    def test_codex_truncated_baseline_and_refresh(self):
        c = agent(1, 'codex', 'planner')
        self.setup_attempt('active-attempt', [c])
        log = self.codex_log('rollout-incomplete.jsonl', codex_marker(c, stamp(-2)),
                             codex_count(stamp(-1), 100, 20, last={'input_tokens': 5, 'output_tokens': 2}))
        before = rows(self.collect()['current'][PROJECT][MAIN])['planner']
        self.assertEqual(before['unknown_reasons'], {'counter_baseline_unavailable': 1})
        self.assertIsNone(before['input_tokens'])
        self.write_log(log, codex_count(stamp(-1, 1), 112, 23), mode='a')
        after = rows(self.collect()['current'][PROJECT][MAIN])['planner']
        self.assertEqual(after['input_tokens'], 12)
        self.assertEqual(after['output_tokens'], 3)
        self.assertEqual(after['coverage']['partial'], 1)

    def test_log_cache_keeps_newest_without_changing_report(self):
        old, new = agent(1, 'codex', 'planner'), agent(2, 'codex', 'tester')
        self.setup_attempt('active-attempt', [old, new])
        older = self.codex_log('rollout-old.jsonl', codex_marker(old, stamp(-2)),
                               codex_count(stamp(-1), 10, 2), session='old')
        newer = self.codex_log('rollout-new.jsonl', codex_marker(new, stamp(-2)),
                               codex_count(stamp(-1), 20, 4), session='new')
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        with patch.object(dashboard_usage, 'LOG_CACHE_MAX_FILES', 1), \
                patch.object(dashboard_usage, 'codex_log', wraps=dashboard_usage.codex_log) as parse:
            expected = self.collect()
            self.assertEqual(set(self.collector.cache['codex'][1]), {newer})
            parse.reset_mock()
            self.assertEqual(self.collect(), expected)
            self.assertEqual([call.args[0] for call in parse.call_args_list], [older])
            newer.unlink()
            report = rows(self.collect()['current'][PROJECT][MAIN])
            self.assertEqual(report['planner']['input_tokens'], 10)
            self.assertEqual(report['tester']['unknown_reasons'], {'session_not_found': 1})
            self.assertEqual(set(self.collector.cache['codex'][1]), {older})

    def test_log_cache_evicts_by_size_and_still_reads_oversized_log(self):
        old, new = agent(1, 'claude', 'planner'), agent(2, 'claude', 'tester')
        self.setup_attempt('active-attempt', [old, new])
        older = self.claude / 'old.jsonl'
        newer = self.claude / 'new.jsonl'
        self.write_log(older, claude_marker(old, self.repo, stamp(-2)),
                       claude_message('m1', stamp(-1)))
        self.write_log(newer, claude_marker(new, self.repo, stamp(-2)),
                       claude_message('m2' * 1000, stamp(-1)))
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        expected = self.collect()
        cache = self.collector.cache['claude'][1]
        old_size, new_size = cache[older][2], cache[newer][2]
        with patch.object(dashboard_usage, 'LOG_CACHE_MAX_BYTES', new_size):
            self.assertEqual(self.collect(), expected)
            self.assertEqual(set(self.collector.cache['claude'][1]), {newer})
        with patch.object(dashboard_usage, 'LOG_CACHE_MAX_BYTES', old_size):
            self.assertEqual(self.collect(), expected)
            self.assertEqual(set(self.collector.cache['claude'][1]), {older})

    def test_opencode_incomplete_segment_keeps_only_proven_prefix(self):
        o = agent(1, 'opencode2', 'reviewer')
        self.setup_attempt('active-attempt', [o])
        conn = self.open_database()
        self.oc_record(conn, 'user', 1, {'text': prompt(o)})
        self.oc_response(conn, 2)
        conn.execute('INSERT INTO session_message VALUES (?,?,?,?)',
                     ('open-session', 'user', 3, '{"text":'))
        self.oc_response(conn, 4, input=9999)
        current = rows(self.collect()['current'][PROJECT][MAIN])['reviewer']
        self.assertEqual(current['input_tokens'], 25)
        self.assertEqual(current['coverage']['partial'], 1)
        self.assertEqual(current['unknown_reasons'], {'incomplete_log': 1})

    def test_codex_stops_at_unrecognized_dispatch_and_finished_attempt(self):
        c = agent(1, 'codex', 'planner')
        self.setup_attempt('old-attempt', [c], current=False, finished=stamp(-1))
        malformed = codex_marker(c, stamp(-1))
        malformed['payload']['content'][0]['text'] = PREAMBLE
        self.codex_log('rollout-one.jsonl', codex_marker(c, stamp(-2)),
                       codex_count(stamp(-1), 10, 2),
                       codex_count(stamp(-1, 2), 20, 5), malformed,
                       codex_count(stamp(-1, 3), 200, 20))
        entry = rows(self.collect()['history']['7'])['planner']
        self.assertEqual(entry['input_tokens'], 10)
        self.setup_attempt('old-attempt', [c])
        entry = rows(self.collect()['current'][PROJECT][MAIN])['planner']
        self.assertEqual(entry['input_tokens'], 20)

    def test_dispatch_repeated_in_two_registries_and_unowned_attempt(self):
        c = agent(1, 'codex', 'planner')
        self.setup_attempt('active-attempt', [c])
        self.setup_attempt('foreign-attempt', [c], wid=OTHER)
        self.codex_log('rollout-one.jsonl', codex_marker(c, stamp(-2)), codex_count(stamp(-1), 10, 2))
        documents = self.collect()['current'][PROJECT]
        for wid in (MAIN, OTHER):
            self.assertEqual(rows(documents[wid])['planner']['unknown_reasons'], {'dispatch_registration_ambiguous': 1})
        other_context = self.catalog.context(PROJECT, OTHER)
        atomic_json(other_context.state_dir / 'attempts/foreign-attempt/context.json',
                    {'identity': self.catalog.context(PROJECT, MAIN).identity()})
        documents = self.collect()['current'][PROJECT]
        self.assertEqual(rows(documents[MAIN])['planner']['input_tokens'], 10)
        self.assertEqual(rows(documents[OTHER])['coordinator']['unknown_reasons'], {'registry_unavailable': 1})


if __name__ == '__main__':
    unittest.main()
