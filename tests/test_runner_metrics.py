import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from team_runner.runner_metrics import PREAMBLE, annotate_agents, measurement


class TokenSpeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.db = Path(self.tmp.name) / 'usage.db'
        self.c = sqlite3.connect(self.db)
        self.addCleanup(self.c.close)
        self.c.executescript('CREATE TABLE session_v2(id TEXT, directory TEXT); '
                            'CREATE TABLE session_message(session_id TEXT,type TEXT,seq INTEGER,data TEXT);')
        self.c.execute('INSERT INTO session_v2 VALUES (?,?)', ('session', str(self.repo)))
        self.agent = {'requested': {'agent': 'opencode2'}, 'dispatch_id': 'ctx_123456789abc',
                      'task_id': 'task_123456789abc', 'terminal': 'term_fixture'}
        self.preamble = (PREAMBLE + '\nYour task ID is: task_123456789abc\n'
                         'orca orchestration send --from term_fixture --type worker_done '
                         '--task-id task_123456789abc --dispatch-id ctx_123456789abc --outcome succeeded')
        self.add('user', 1, {'text': self.preamble})

    def add(self, kind, seq, data, session='session'):
        self.c.execute('INSERT INTO session_message VALUES (?,?,?,?)', (session, kind, seq, json.dumps(data)))
        self.c.commit()

    def response(self, start=1000, end=3000, output=10, reasoning=30, finish='stop'):
        return {'time': {'created': start, 'completed': end},
                'tokens': {'output': output, 'reasoning': reasoning}, 'finish': finish}

    def metrics(self, repo=None):
        annotate_agents(repo or self.repo, [self.agent], self.db)
        return self.agent['token_speed']

    def test_weighted_speed_length_and_idle_gap(self):
        self.add('assistant', 2, self.response())  # 40 / 2 = 20
        self.add('assistant', 3, self.response(100000, 101000, 0, 80, 'length'))  # 80 / 1
        m = self.metrics()
        self.assertEqual(m['tokens_per_second'], 40)  # 120 / 3, not mean 50 or wall time
        self.assertEqual(m['last']['tokens_per_second'], 80)
        self.assertEqual((m['output_tokens'], m['reasoning_tokens'], m['length_responses']), (10, 110, 1))
        self.assertNotIn('text', json.dumps(m))

    def test_incomplete_missing_invalid_and_failed_not_zero(self):
        for i, change in enumerate(({'finish': None}, {'tokens': {}}, {'time': {}},
                                    {'finish': 'error'}, {'tokens': {'output': -1, 'reasoning': 0}})):
            self.add('assistant', i + 2, {**self.response(), **change})
        m = self.metrics()
        self.assertEqual(m['status'], 'unknown')
        self.assertEqual(m['excluded_responses'], 5)
        self.assertNotIn('tokens_per_second', m)
        self.assertIsNone(measurement(self.response(end=1000)))
        self.assertIsNone(measurement(self.response(end=999)))
        self.assertIsNone(measurement(self.response(output=float('nan'))))
        self.assertEqual(measurement(self.response(output=0, reasoning=0))['tokens_per_second'], 0)

    def test_model_effort_changes_are_separate(self):
        for seq, effort in ((2, 'high'), (3, 'medium')):
            self.add('assistant', seq, {**self.response(), 'model': {
                'id': 'claude-opus-4.8', 'providerID': 'cloudru', 'variant': effort}})
        groups = self.metrics()['by_model']
        self.assertEqual([g['model']['variant'] for g in groups], ['high', 'medium'])
        self.assertEqual([g['responses'] for g in groups], [1, 1])

    def test_dispatch_reuse_boundary(self):
        self.add('assistant', 2, self.response())
        self.add('user', 3, {'text': self.preamble.replace('ctx_123456789abc', 'ctx_aaaaaaaaaaaa')})
        self.add('assistant', 4, self.response(output=999))
        self.assertEqual(self.metrics()['responses'], 1)

    def test_foreign_directory_and_terminal_do_not_bind(self):
        self.add('assistant', 2, self.response())
        self.assertEqual(self.metrics(Path(str(self.repo) + '-other'))['status'], 'unknown')
        self.agent['terminal'] = 'term_other'
        self.assertEqual(self.metrics()['reason'], 'session_not_found')

    def test_ambiguous_session_and_quoted_preamble(self):
        self.c.execute('INSERT INTO session_v2 VALUES (?,?)', ('second', str(self.repo)))
        self.add('user', 1, {'text': 'quoted: ' + self.preamble}, 'second')
        self.add('assistant', 2, self.response())
        self.assertEqual(self.metrics()['status'], 'measured')
        self.add('user', 2, {'text': self.preamble}, 'second')
        self.assertEqual(self.metrics()['reason'], 'session_binding_ambiguous')

    def test_missing_database_not_created_and_unsupported_runtime(self):
        absent = self.db.with_name('absent.db')
        annotate_agents(self.repo, [self.agent], absent)
        self.assertFalse(absent.exists())
        self.assertEqual(self.agent['token_speed']['reason'], 'database_unavailable')
        self.agent['requested']['agent'] = 'codex'
        annotate_agents(self.repo, [self.agent], self.db)
        self.assertEqual(self.agent['token_speed']['reason'], 'runtime_not_supported')


if __name__ == '__main__':
    unittest.main()
