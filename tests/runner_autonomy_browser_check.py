#!/usr/bin/env python3
"""Isolated autonomy UI states using synthetic Orca and a disposable HTTP server."""
import argparse
import sys, threading, json, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_runner_autonomy import Autonomy
from runner_browser_check import BrowserCheck
from team_runner import runner_ui, application, state_time, network_recovery, autonomy
from team_runner.runner_store import atomic_json, read_json, lock
from team_runner.project_services import ServiceUnavailable

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    case = Autonomy(); case.setUp()
    evidence = Path(args.evidence).resolve()
    browser = BrowserCheck(case.root, evidence, args.worktree)
    def screenshot(name):
        top = browser.evaluate("document.querySelector('#autonomy-status').closest('section').getBoundingClientRect().top")
        browser.browser('scroll', '--page', browser.page, '--direction', 'down' if top > 0 else 'up', '--amount', str(abs(int(top))))
        browser.screenshot(name)
    class Services:
        def all(self): return []
        def forward(self, *a): raise ServiceUnavailable('isolated fixture')
        def status(self, *a): return {'state':'fixture','confirmed':True}
    server = None
    try:
        attempt, root, child = case.network_fixture()
        # The fixture's immutable task config and fake Orca remain isolated.
        case.now = time.time(); case.network_tick()
        state = case.a.load_state()
        state['current']['messages'] = [
            {'id':'old','run_id':'old-run','type':'escalation','payload':{},'body':'Historical diagnostic fixture'},
            {'id':'unknown','run_id':root['run_id'],'type':'escalation','payload':{},'body':'Unattributed diagnostic fixture'}]
        case.a.save_state(state)
        with lock(case.a.state_dir/'runner.lock'), patch.object(runner_ui, 'Runner', side_effect=lambda ctx: next(r for r in case.runners if r.context.workspace_id == ctx.workspace_id)):
            server, token = runner_ui.make_server(case.catalog, services=Services())
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            url = f'http://127.0.0.1:{server.server_port}/#token={token}'
            browser.page = browser.browser('tab','create','--url',url)['browserPageId']
            browser.dom("document.querySelector('#autonomy-status').textContent === 'Автономность: включена'", 'autonomy policy visible')
            browser.dom("document.querySelector('#next-operation').textContent.includes('продолжение сессии')", 'network continuation visible')
            duplicates = browser.evaluate("(() => {const ids=[...document.querySelectorAll('[id]')].map(e=>e.id);return ids.filter((id,i)=>ids.indexOf(id)!==i)})()")
            assert duplicates == [], duplicates
            screenshot('network-wait.png')
            # Model exhausted budget on a genuine blocked delegation after its first completion.
            network_recovery.cancel(case.a, 'fixture network settled')
            case.finish(attempt, child)
            reg = case.a.launcher.registry(attempt)
            reg['edge_passes']['implement'] = 3
            case.a.launcher.save_registry(attempt,reg)
            blocked, _ = case.delegate(attempt,root)
            assert blocked['status'] == 'limit'
            ledger = {autonomy.budget_key(case.a,case.a.load_state(),'implement'):{str(i):{'fixture':True} for i in range(3)}}
            atomic_json(case.a.state_dir/'autonomy-budget.json',ledger)
            autonomy.tick(case.a,case.a.load_state())
            case.a.save_state(case.a.load_state())
            browser.dom("document.querySelector('#autonomy-budget').textContent.includes('осталось 0')", 'exhausted budget visible')
            browser.dom("document.querySelector('#wait-reason').textContent.includes('лимита')", 'limit wait visible')
            screenshot('limit-exhausted.png')
            # A real unknown journal entry must show the blocking reason and keep policy unchanged.
            atomic_json(case.a.launcher.journal(attempt), {'unconfirmed':{'status':'unknown','args':['orchestration','send'], 'request_id':'00000000-0000-0000-0000-000000000001'}})
            browser.dom("document.querySelector('#blocker').textContent.includes('unknown_effect')", 'unknown effect blocks actions')
            screenshot('unknown-effect.png')
            errors = browser.browser('console','--page',browser.page,'--limit','30')
            atomic_json(evidence/'browser-autonomy-result.json',{'status':'PASS','steps':browser.steps,'duplicates':duplicates,'console':errors})
            print('PASS isolated browser states:', ', '.join(browser.steps))
    finally:
        if browser.page: browser.browser('tab','close','--page',browser.page)
        if server: server.shutdown(); server.server_close()
        case.doCleanups()


if __name__ == "__main__":
    main()
