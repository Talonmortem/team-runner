# Team Runner

Python 3.11+, standard library, no runtime dependencies. Respect existing edits. Never commit or change branches without explicit request.

`src/team_runner`: explicit Context → Runner/Launcher, Tasks.json adapter, Orca adapter, XDG catalog, loopback HTTP/UI. Never introduce global current workspace, implicit Orca `current`, shared mailboxes, cross-attempt reuse or automatic replay of unknown effects. Project rules override common materials; immutable attempt snapshots precede dispatch.

Check: `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`. Standalone helper tests: `python3 tests/test_orca_msg.py`, `test_orca_recover.py`, `test_token_usage.py`. Browser fixtures are isolated; live smoke must be explicitly authorized. Never launch registered project goals as a test.
