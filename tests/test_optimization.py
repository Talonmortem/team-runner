"""Offline optimization contracts; all checkout/state/transport fixtures isolated."""
from contextlib import nullcontext, contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from team_runner import verification as v, coordinator_events as events
from team_runner.catalog import profile
from team_runner.runner_store import atomic_json, read_json
from team_runner.heavy_resource import acquire
from team_runner.tracking import Tracking


class Verification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        (self.repo/'code').mkdir(); (self.repo/'code/a.py').write_text('pass\n')
        self.manifest=self.root/'manifest.json'
        self.sha=v.create(self.repo,['code'],self.manifest)['sha256']

    def test_manifest_digest_copy_add_delete_and_escape(self):
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha)['result'],'PASS')
        with self.assertRaises(ValueError):
            v.create(self.repo,['code','missing'],self.root/'missing.json')
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha[:-1])['result'],'FAIL')
        (self.repo/'code/b.py').write_text('new')
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha)['result'],'FAIL')
        (self.repo/'code/b.py').unlink(); (self.repo/'code/a.py').unlink()
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha)['result'],'FAIL')
        (self.repo/'code/a.py').symlink_to('/etc/passwd')
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha)['result'],'FAIL')
        value={'schema_version':1,'roots':['../escape'],'files':{}}
        atomic_json(self.manifest,value)
        sha=hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.assertEqual(v.verify(self.repo,self.manifest,sha)['result'],'FAIL')

    def test_copy_mismatch(self):
        import shutil
        copy=self.root/'copy';shutil.copytree(self.repo,copy)
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha,copy)['result'],'PASS')
        (copy/'code/a.py').write_text('changed')
        self.assertEqual(v.verify(self.repo,self.manifest,self.sha,copy)['result'],'FAIL')

    def test_evidence_execution_reuse_and_invalidation(self):
        output=self.root/'pass.json';cmd=[sys.executable,'-c','import os; assert os.getenv("OPT_TEST")=="yes"; assert "AMBIENT_FLAG" not in os.environ']
        with patch.dict(os.environ,{'AMBIENT_FLAG':'hidden'}):
            result=v.run_check(self.repo,self.manifest,self.sha,cmd,'test',{'OPT_TEST':'yes'},'one',output)
        self.assertEqual(result['result'],'PASS')
        reused=v.reuse(output,result['inputs'],'two',self.root/'reuse.json')
        self.assertEqual(reused['status'],'reused');self.assertNotEqual(reused['id'],result['id'])
        inputs=dict(result['inputs'],environment={'OPT_TEST':'no'})
        self.assertEqual(v.reuse(output,inputs,'two',self.root/'invalid.json')['result'],'REQUIRED')
        Path(result['logs'][0]['path']).write_text('tampered')
        self.assertEqual(v.reuse(output,result['inputs'],'two',self.root/'badlog.json')['result'],'REQUIRED')
        with self.assertRaises(ValueError):v.evidence_inputs({'result':'PASS'},cmd,'test',{},False)
        with self.assertRaises(ValueError):v.evidence_inputs({'result':'PASS'},cmd,'test',{'SECRET':'x'},True)

    def test_gate_acceptance_is_historical_but_initial_candidate_must_match(self):
        manifest=self.repo/'frozen.json';manifest.write_bytes(self.manifest.read_bytes())
        command=[sys.executable,'-c','print("gate")']
        result=v.run_check(self.repo,manifest,self.sha,command,'test',{},'one',self.repo/'result.json')
        item={'id':'E9-GATE','status':'completed','check_scope':'stage','gate_roots':['code'],
              'gate_command':command,'gate_evidence':{'path':'result.json','manifest':'frozen.json',
              'sha256':self.sha,'accepted':'accepted.json'}}
        self.assertFalse(v.gate_valid(self.repo,item));self.assertTrue(v.gate_valid(self.repo,item,current=True))
        v.accept_gate(self.repo,item);self.assertTrue(v.gate_valid(self.repo,item))
        (self.repo/'code/a.py').write_text('E10 edit')
        self.assertTrue(v.gate_valid(self.repo,item));self.assertFalse(v.gate_valid(self.repo,item,current=True))
        item['gate_command']=['true'];self.assertFalse(v.gate_valid(self.repo,item))

    def test_nested_resource_wait_is_reported_without_granting_pass(self):
        code='import json; print(json.dumps({"team_runner_resource":{"wait_seconds":0.01,"status":"clean"}})); print(json.dumps({"team_runner_resource":{"wait_seconds":-1}})); raise SystemExit(1)'
        result=v.run_check(self.repo,self.manifest,self.sha,[sys.executable,'-c',code],
                           'test',{},'one',self.root/'nested.json')
        self.assertEqual(result['result'],'FAIL')
        self.assertEqual(result['wait_seconds'],0.01)
        self.assertEqual(len(result['resources']),1)
        self.assertGreaterEqual(result['execution_seconds'],0)

    def test_scopes_legacy_and_required_stage(self):
        self.assertNotIn('scope',profile({'checks':[{'cwd':'.','argv':['true']}]})['checks'][0])
        with self.assertRaises(ValueError): profile({'checks':[{'cwd':'.','argv':['true'],'scope':'bad'}]})
        goal=self.repo/'feat/goal';goal.mkdir(parents=True)
        atomic_json(goal/'Tasks.json',{'queue':[{'id':'E9','path':'e9','status':'completed'},
            {'id':'E10','path':'e10','status':'not_started','depends_on':['E9']}]})
        atomic_json(goal/'e9/Tasks.json',{'queue':[{'id':'work','status':'completed'},
            {'id':'gate','status':'completed','check_scope':'stage','depends_on':['work']}]})
        atomic_json(goal/'e10/Tasks.json',{'queue':[{'id':'next','status':'not_started'}]})
        tracking=Tracking(goal);self.assertFalse(tracking.feature_done('E9'))
        self.assertEqual(tracking.next_item()[2]['id'],'gate')
        tracking.goal['queue'][0]['stage_gate']='gate'
        (goal/'e9/Tasks.json').unlink()
        self.assertFalse(tracking.feature_done('E9'))
        self.assertIsNone(tracking.next_item())
        del tracking.goal['queue'][0]['stage_gate']
        self.assertTrue(tracking.feature_done('E9'))

    def test_heavy_clean_and_unknown_crash_residue(self):
        path=self.root/'heavy.lock'
        with acquire(path) as (receipt,_,_): receipt['cleanup_inventory_complete']=True
        with acquire(path) as (receipt,_,_): receipt['cleanup_inventory_complete']=True
        atomic_json(str(path)+'.json',{'status':'running','cleanup_inventory_complete':False})
        with self.assertRaises(RuntimeError):
            with acquire(path):pass

    def test_cleanup_interruption_cannot_leave_reusable_pass(self):
        @contextmanager
        def broken_cleanup():
            yield {}, None, None
            raise RuntimeError('cleanup receipt lost')
        output=self.root/'interrupted.json'
        with patch('team_runner.heavy_resource.acquire', broken_cleanup):
            with self.assertRaisesRegex(RuntimeError, 'cleanup receipt lost'):
                v.run_check(self.repo,self.manifest,self.sha,[sys.executable,'-c','pass'],
                            'test',{},'one',output,heavy=True)
        self.assertEqual(read_json(output)['result'],'UNKNOWN')

    def test_heavy_detached_owned_child_fences_next_run(self):
        path=self.root/'heavy.lock'
        code='import os,time; pid=os.fork(); os._exit(0) if pid else None; os.setsid(); time.sleep(30)'
        with patch('team_runner.heavy_resource.default_path', return_value=path):
            result=v.run_check(self.repo,self.manifest,self.sha,[sys.executable,'-c',code],
                               'test',{},'one',self.root/'leak.json',heavy=True)
        self.assertEqual(result['result'],'FAIL')
        self.assertEqual(result['resource']['status'],'cleanup_required')
        self.assertIn('registered_processes',result['resource'])
        receipt=read_json(str(path)+'.json');self.assertEqual(receipt['status'],'cleanup_required')
        try:
            with self.assertRaises(RuntimeError):
                with acquire(path):pass
        finally:
            import signal
            for process in receipt['processes']:
                os.kill(process['pid'],signal.SIGTERM)
                os.waitpid(process['pid'],0)
        with acquire(path):pass

    def test_heavy_two_processes_exclude_each_other(self):
        path=self.root/'heavy.lock'; ready=self.root/'ready'
        code='''import sys,time
from pathlib import Path
from team_runner.heavy_resource import acquire
with acquire(sys.argv[1]) as (r,fd,m):
 Path(sys.argv[2]).write_text('ready')
 time.sleep(.3)
 r['cleanup_inventory_complete']=True
'''
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        process=subprocess.Popen([sys.executable,'-c',code,str(path),str(ready)],env=env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        import time
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(ready.exists())
        with acquire(path) as (receipt,_,_):
            self.assertGreater(receipt['wait_seconds'],.1);receipt['cleanup_inventory_complete']=True
        self.assertEqual(process.wait(),0)


class Events(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.attempt=self.tmp.name
        self.state={'run_id':'parent','current':{'attempt_dir':self.attempt,'dispatch_id':'coord','terminal':'term',
            'messages':[{'id':'m','run_id':'child','type':'worker_done'}]}}
        self.runner=Mock();self.runner.load_state.return_value=self.state
        self.runner.context.orca_id='workspace';self.runner.launch_permission.return_value=nullcontext()
        self.live={'dispatch':{'id':'coord','status':'dispatched','assigneeHandle':'term','processIncarnation':'pty:inc'},
            'terminal':{'handle':'term','ptyId':'pty','incarnationId':'inc','worktreeId':'workspace'},
            'observation':{'exactWorker':True,'status':'live'},'projection':{'stage':{'activity':'idle'}}}
        self.sends=[]
        def call(*args,**kwargs):
            if args[:2]==('orchestration','worker-show'):return self.live
            if args[:2]==('orchestration','request-show'):return {'state':'absent'}
            if args[:2]==('orchestration','send'):
                self.sends.append(args);return self.receipt(args)
            raise AssertionError(args)
        self.runner.orca.side_effect=call
        events.capture(self.runner,self.state,now=0)

    def receipt(self, args):
        return {'message': {'id': 'notification', 'run_id': args[args.index('--run')+1],
                            'to_handle': args[args.index('--to')+1]},
                'mutation': {'requestId': args[-1]}}

    def test_idle_notification_has_no_minute_delay(self):
        events.tick(self.runner, self.state, now=1)
        events.tick(self.runner, self.state, now=2)
        self.assertEqual(len(self.sends), 1)

    def test_duplicate_batch_restart_and_processing_ack(self):
        events.capture(self.runner,self.state,now=5)
        self.assertEqual(len(events.view(self.attempt)['events']),1)
        events.tick(self.runner,self.state,now=61);events.tick(self.runner,self.state,now=62)
        self.assertEqual(len(self.sends),1)
        journal=events.view(self.attempt);self.assertEqual(journal['notifications'][0]['message_id'],'notification')
        self.assertIsNone(journal['events'][0]['processed_at'])
        self.assertNotIn('turn_started',journal['notifications'][0])
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':'term'}):
            events.acknowledge(self.runner,self.attempt,'coord',[journal['events'][0]['id']],now=70)
        self.assertEqual(events.view(self.attempt)['events'][0]['coordinator_wait_seconds'],70)

    def test_restart_after_send_intent_only_reads_receipt(self):
        journal=events.view(self.attempt)
        journal['notifications']=[{'id':'request','events':[journal['events'][0]['id']],
                                  'dispatch':'coord','status':'sending'}]
        atomic_json(events.paths(self.attempt)[0],journal)
        self.runner.orca.side_effect=lambda *a,**k: {'state':'completed','receipt':{'ok':True,'result':{
            'send':{'accepted':True,'prompt':{'requestId':'request','stages':['input_accepted','turn_started']}}}}}
        events.tick(self.runner,self.state,now=80)
        self.assertTrue(events.view(self.attempt)['notifications'][0]['turn_started'])
        self.assertTrue(all(c.args[:2]==('orchestration','request-show') for c in self.runner.orca.call_args_list))

    def test_multiple_events_are_one_notification(self):
        self.state['current']['messages'].append({'id':'m2','run_id':'child','type':'question'})
        events.capture(self.runner,self.state,now=1)
        events.tick(self.runner,self.state,now=61)
        self.assertEqual(len(self.sends),1)
        self.assertEqual(len(events.view(self.attempt)['notifications'][0]['events']),2)

    def test_stale_pause_stop_and_warning(self):
        for flag in ['paused','stop_requested','control_uncertain']:
            self.state[flag]=True;events.tick(self.runner,self.state,now=65);self.state.pop(flag)
        self.live['dispatch']['id']='stale'
        events.tick(self.runner,self.state,now=301);events.tick(self.runner,self.state,now=302)
        self.assertEqual(self.sends,[]);self.assertEqual(self.runner.notify.call_count,1)

    def test_unknown_activity_uses_dispatch_mailbox_without_terminal_input(self):
        self.live.pop('projection')
        events.tick(self.runner,self.state,now=1)
        self.assertEqual(len(self.sends),1)
        args=self.sends[0]
        self.assertEqual(args[:2],('orchestration','send'))
        self.assertEqual(args[args.index('--to')+1],'dispatch:coord')
        self.assertEqual(args[args.index('--run')+1],'parent')
        self.assertFalse(any(c.args[:2]==('terminal','send') for c in self.runner.orca.call_args_list))

    def test_working_coordinator_gets_one_queued_notification(self):
        self.live['projection']['stage']['activity']='working'
        events.tick(self.runner,self.state,now=1);events.tick(self.runner,self.state,now=2)
        self.assertEqual(len(self.sends),1)
        self.assertIsNone(events.view(self.attempt)['events'][0]['processed_at'])

    def test_unknown_dispatch_send_reconciles_durable_message_after_restart(self):
        journal=events.view(self.attempt)
        record={'id':'request','events':[journal['events'][0]['id']], 'dispatch':'coord',
                'run_id':'parent','transport':'dispatch','status':'unknown'}
        journal['notifications']=[record];atomic_json(events.paths(self.attempt)[0],journal)
        self.runner.orca.side_effect=lambda *a,**k: {'state':'completed','receipt':{
            'message':{'id':'persisted','run_id':'parent','to_handle':'dispatch:coord'},
            'mutation':{'requestId':'request'}}}
        events.tick(self.runner,self.state,now=80)
        record=events.view(self.attempt)['notifications'][0]
        self.assertEqual(record['status'],'accepted');self.assertEqual(record['message_id'],'persisted')
        self.assertNotIn('input_accepted',record)
        self.assertTrue(all(c.args[:2]==('orchestration','request-show') for c in self.runner.orca.call_args_list))

    def test_wrong_or_failed_receipt_never_allows_another_send(self):
        events.tick(self.runner,self.state,now=1)
        journal=events.view(self.attempt);journal['notifications'][0]['status']='unknown'
        atomic_json(events.paths(self.attempt)[0],journal)
        original=self.receipt(self.sends[0])
        import copy
        for field,value in [('to_handle','dispatch:foreign'),('run_id','other-project'),('id',None)]:
            bad=copy.deepcopy(original);bad['message'][field]=value
            self.runner.orca.side_effect=lambda *a,**k: {'state':'completed','receipt':bad}
            events.tick(self.runner,self.state,now=80)
            self.assertEqual(events.view(self.attempt)['notifications'][0]['status'],'unknown')
            self.assertEqual(len(self.sends),1)
            self.runner.orca.assert_called_with('orchestration','request-show','--request',original['mutation']['requestId'])
            journal=events.view(self.attempt);journal['notifications'][0].pop('receipt_checked_at',None)
            atomic_json(events.paths(self.attempt)[0],journal)
        self.runner.orca.side_effect=lambda *a,**k: {'state':'completed','receipt':{'ok':False,'result':original}}
        events.tick(self.runner,self.state,now=100)
        self.assertEqual(events.view(self.attempt)['notifications'][0]['status'],'unknown')
        self.assertEqual(len(self.sends),1)

    def test_wrong_request_receipt_remains_unknown(self):
        events.tick(self.runner,self.state,now=1)
        journal=events.view(self.attempt);journal['notifications'][0]['status']='unknown'
        atomic_json(events.paths(self.attempt)[0],journal)
        receipt=self.receipt(self.sends[0]);receipt['mutation']['requestId']='foreign'
        self.runner.orca.side_effect=lambda *a,**k: {'state':'completed','receipt':receipt}
        events.tick(self.runner,self.state,now=80)
        self.assertEqual(events.view(self.attempt)['notifications'][0]['status'],'unknown')
        self.assertEqual(len(self.sends),1)

    def test_cancel_before_send_preserves_intent_without_input(self):
        self.runner.launch_permission.side_effect=RuntimeError('cancelled')
        events.tick(self.runner,self.state,now=1)
        self.assertEqual(self.sends,[])
        self.assertEqual(events.view(self.attempt)['notifications'][0]['status'],'unknown')

    def test_foreign_worker_identity_does_not_get_notified(self):
        import copy
        for section,field,value in [('dispatch','assigneeHandle','foreign'),
                                    ('dispatch','processIncarnation','pty:foreign'),
                                    ('terminal','handle','foreign'),('terminal','worktreeId','foreign'),
                                    ('observation','exactWorker',False),('observation','status','unverifiable')]:
            with self.subTest(section=section,field=field):
                original=copy.deepcopy(self.live)
                self.live[section][field]=value
                events.tick(self.runner,self.state,now=1)
                self.assertEqual(self.sends,[])
                self.live=original

    def test_notification_does_not_touch_another_project_journal(self):
        import copy
        other_attempt=str(Path(self.attempt)/'other-project')
        other=copy.deepcopy(self.state);other['run_id']='other-parent'
        other['current'].update(attempt_dir=other_attempt,dispatch_id='other-coord',terminal='other-term')
        other_runner=Mock()
        events.capture(other_runner,other,now=0)
        path=events.paths(other_attempt)[0];before=path.read_bytes()
        events.tick(self.runner,self.state,now=1)
        self.assertEqual(path.read_bytes(),before)
        other_runner.orca.assert_not_called()
        self.assertEqual(self.sends[0][self.sends[0].index('--to')+1],'dispatch:coord')

    def test_unknown_send_is_never_repeated_and_attempt_fenced(self):
        def unknown(*args,**kwargs):
            if args[:2]==('orchestration','worker-show'):return self.live
            if args[:2]==('orchestration','request-show'):return {'state':'absent'}
            self.sends.append(args);raise RuntimeError('response lost')
        self.runner.orca.side_effect=unknown
        events.tick(self.runner,self.state,now=61);events.tick(self.runner,self.state,now=90)
        self.assertEqual(len(self.sends),1)
        self.state['current']['dispatch_id']='replacement'
        events.tick(self.runner,self.state,now=100)
        self.assertEqual(len(self.sends),1)
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':'term'}):
            with self.assertRaises(ValueError):events.acknowledge(self.runner,self.attempt,'coord',[],now=110)

if __name__=='__main__':unittest.main()
