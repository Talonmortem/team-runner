#!/usr/bin/env python3
"""Opt-in harmless provider smoke; isolated catalog/state, no working goal queue."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from team_runner import runner_config as config
from team_runner.runner_launch import Launcher
from team_runner.catalog import Catalog
from team_runner.orca import call
from team_runner.runner_store import atomic_json


def settled(launch, attempt, record, seconds):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        receipt=launch.orca('orchestration','check','--run',record['run_id'],'--terminal',record['mailbox'],mailbox=record['mailbox'])
        atomic_json(attempt/'smoke-mail.json',receipt)
        matched=False
        for message in receipt.get('messages',[]):
            payload=message.get('payload') or {}
            if isinstance(payload,str):payload=json.loads(payload)
            if message.get('type')=='worker_done' and payload.get('dispatchId')==record['dispatch_id']:
                matched=payload.get('outcome')=='succeeded'
        if receipt.get('deliveryId'):
            launch.effect(attempt,'ack-'+receipt['deliveryId'],'orchestration','check','--ack',receipt['deliveryId'],'--run',record['run_id'],mailbox=record['mailbox'])
        if matched:return True
        time.sleep(1)
    return False


def context_reading(launch, record, runtime, state):
    launch.orca('terminal','wait','--terminal',record['terminal'],'--for','tui-idle','--timeout-ms','60000',timeout=70)
    output=launch.orca('terminal','read','--terminal',record['terminal'],'--limit','100')
    atomic_json(state/'context-before.json',output)
    if runtime=='claude':
        # /context is a local diagnostic, not a task or a guessed transcript token count.
        launch.orca('terminal','send','--terminal',record['terminal'],'--text','/context','--enter')
    until=time.monotonic()+20
    while time.monotonic()<until:
        output=launch.orca('terminal','read','--terminal',record['terminal'],'--limit','150')
        text='\n'.join((output.get('terminal') or {}).get('tail',[]))
        left=re.search(r'Context\s+(\d+)%\s+left|(\d+)%\s+context\s+left',text,re.I)
        used=re.search(r'Context\s+(\d+)%\s+used|(\d+)%\s+context\s+used',text,re.I)
        # Claude's /context summary: "Tokens: 27.1k / 200k (14%)".
        summary=re.search(r'(?:Tokens|Context usage)[^\n]*?\((\d+(?:\.\d+)?)%\)',text,re.I)
        percent=100-int(left[1] or left[2]) if left else int(used[1] or used[2]) if used else float(summary[1]) if summary else None
        atomic_json(state/'context-output.json',output)
        if percent is not None:return percent
        time.sleep(1)
    return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',choices=('codex','claude'),required=True)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--checkout',type=Path,required=True)
    parser.add_argument('--orca-id',required=True)
    parser.add_argument('--timeout',type=int,default=90)
    args=parser.parse_args()
    if args.state.exists():raise ValueError('fresh smoke directory required')
    cat=Catalog(args.state/'config',args.state/'state')
    wt=call('worktree','show','--worktree','id:'+args.orca_id)['worktree']
    project=cat.project('Live smoke')
    ws=cat.workspace(project['id'],{'path':str(args.checkout),'orca_id':wt['id'],'orca_instance':wt['instanceId']})
    ctx=cat.context(project['id'],ws['id']);launch=Launcher(ctx)
    attempt=ctx.state_dir/'attempts'/'smoke';ctx.snapshot(attempt)
    atomic_json(ctx.state_dir/'state.json',{'current':{'attempt_dir':str(attempt)}})
    cfg=config.defaults()
    if args.runtime=='claude':cfg['roles']['tester']=cfg['roles']['coordinator'].copy()
    atomic_json(attempt/'config.json',cfg)
    report={'runtime':args.runtime,'status':'INCOMPLETE','attempt':str(attempt)}
    spec=('Harmless launcher smoke only. Do not inspect/read/write any project files; do not start agents or queues. '
          'Run only the lifecycle commands required by the injected Orca preamble. '
          'Reply RUNNER_SMOKE_OK, then send worker_done succeeded using the exact preamble command. '
          'Report no files changed. Idle after completion.')
    try:
        one=launch.dispatch_role(attempt,'tester',spec,'runner smoke first',ctx.state_dir,function='smoke')
        report['first']=one['dispatch_id'];print(json.dumps(report),flush=True)
        if not settled(launch,attempt,one,args.timeout):
            report['reason']='first Dispatch did not complete (inspect output for provider limits)'
            atomic_json(args.state/'output.json',launch.orca('terminal','read','--terminal',one['terminal'],'--limit','80'))
            return 1
        percent=context_reading(launch,one,args.runtime,args.state);report['context_percent']=percent
        if percent is None:
            report['reason']='CLI context measurement unavailable; reuse refused';return 1
        two=launch.dispatch_role(attempt,'tester',spec,'runner smoke continuation',ctx.state_dir,
                                 function='smoke',reuse=one['dispatch_id'],context_percent=percent)
        report.update(continuation=two['dispatch_id'],same_terminal=one['terminal']==two['terminal'])
        if not settled(launch,attempt,two,args.timeout):report['reason']='continuation did not complete';return 1
        report['status']='PASS';return 0
    except Exception as exc:
        report['reason']=str(exc);return 1
    finally:
        report['cleanup_confirmed']=launch.stop_attempt(attempt)
        if not report['cleanup_confirmed']:report['status']='INCOMPLETE'
        atomic_json(args.state/'result.json',report);print(json.dumps(report),flush=True)


if __name__=='__main__':raise SystemExit(main())
