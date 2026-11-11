import subprocess
from unittest.mock import patch
from test_goal_runner import Fixture, runner, launch, store

class Cleanup(Fixture):
 def setup_agents(self):
  attempt,st=self.attempt()
  agents=[{'id':d,'role':role,'dispatch_id':d,'status':'active', 'keep_session':keep} for d,role,keep in [('coord','coordinator',False),('done','explorer',False),('failed-report','tester',False),('active','tester',False),('keep','tester',True)]]
  store.atomic_json(attempt/'agents.json',{'agents':agents,'runs':[],'fenced':False})
  return attempt,st
 def test_completed_children_close_before_coordinator_settles(self):
  attempt,st=self.setup_agents(); closed=[]
  st['current']['messages']=[{'type':'worker_done','payload':{'dispatchId':d,'outcome':'succeeded'}} for d in ('done','failed-report')]
  runner.save_state(st)
  def observe(a): return {'dispatch':{'status':'completed' if a['dispatch_id'] in ('done','failed-report') else 'dispatched'}}
  def release(a,*args): closed.append(a['dispatch_id']);a['status']='released'
  with patch.object(launch,'observe_agent',side_effect=observe),patch.object(launch,'release_agent',side_effect=release):
   runner.release_completed_children(attempt)
   runner.release_completed_children(attempt)
  self.assertEqual(closed,['done','failed-report'])
  self.assertFalse(launch.registry(attempt)['fenced'])
 def test_failed_child_requires_persisted_report_from_same_attempt(self):
  attempt,st=self.setup_agents()
  st['current']['messages']=[{'type':'worker_done','payload':{'dispatchId':'failed-report','outcome':'failed'}}]
  closed=[]
  def release(a,*args): closed.append(a['dispatch_id']);a['status']='released'
  with patch.object(launch,'observe_agent',return_value={'dispatch':{'status':'failed'}}),patch.object(launch,'release_agent',side_effect=release):
   runner.release_completed_children(attempt)
   self.assertEqual(closed,[])
   st['current']['attempt_dir']='other';runner.save_state(st)
   runner.release_completed_children(attempt)
   self.assertEqual(closed,[])
   st['current']['attempt_dir']=str(attempt);runner.save_state(st)
   runner.release_completed_children(attempt)
   runner.release_completed_children(attempt)
  self.assertEqual(closed,['failed-report'])
 def test_launcher_failed_release_requires_matching_failure_report(self):
  record={'dispatch_id':'failed-report'}
  with patch.object(launch,'observe_agent',return_value={'dispatch':{'status':'failed'},'terminalResource':{'ownershipState':'external'}}),patch.object(launch,'close_registered_terminal') as close:
   for report in (None,{'dispatchId':'other','outcome':'failed'},{'dispatchId':'failed-report','outcome':'succeeded'}):
    with self.assertRaisesRegex(RuntimeError,'accepted worker_done'):
     launch.release_agent(record,accepted_done=report)
   close.assert_not_called()
   launch.release_agent(record,accepted_done={'dispatchId':'failed-report','outcome':'failed'})
   close.assert_called_once()
   self.assertEqual(record['status'],'released')
 def test_failed_child_settles_with_persisted_report(self):
  attempt,st=self.attempt()
  st['current']['messages']=[{'type':'worker_done','payload':{'dispatchId':'failed-report','outcome':'failed'}}]
  runner.save_state(st)
  store.atomic_json(attempt/'agents.json',{'agents':[{'role':'tester','dispatch_id':'failed-report','status':'active'}],'runs':[],'fenced':False})
  with patch.object(launch,'observe_agent',return_value={'dispatch':{'status':'failed'},'terminalResource':{'ownershipState':'external'}}),patch.object(launch,'close_registered_terminal'):
   self.assertTrue(runner.settle_children(attempt))
  self.assertEqual(launch.registry(attempt)['agents'][0]['status'],'released')

 def test_uncertain_release_is_visible_and_blocks_ack(self):
  attempt,st=self.setup_agents()
  st['current']['messages']=[{'type':'worker_done','payload':{'dispatchId':'done','outcome':'succeeded'}}]
  runner.save_state(st)
  with patch.object(launch,'observe_agent',return_value={'dispatch':{'status':'completed'}}),patch.object(launch,'release_agent',side_effect=RuntimeError('release_unknown')):
   with self.assertRaisesRegex(RuntimeError,'release_unknown'): runner.release_completed_children(attempt)
  self.assertEqual(launch.registry(attempt)['agents'][1]['cleanup_error'],'release_unknown')
 def test_failed_outcome_releases_coordinator(self):
  attempt,st=self.attempt()
  store.atomic_json(attempt/'agents.json',{'agents':[{'role':'coordinator','dispatch_id':'owned','status':'active'}],'runs':[],'fenced':False})
  with patch.object(runner,'poll_children'),patch.object(runner,'settle_children',return_value=True),patch.object(launch,'release_agent') as release,patch.object(runner,'git',return_value='head'),patch.object(runner,'notify'):
   self.assertFalse(runner.finish_item(None,st,{'outcome':'failed','body':'failed'},lambda:[]))
  release.assert_called_once()
  self.assertEqual(release.call_args.args[2]["outcome"], "failed")
  self.assertTrue(st["current"]["failed"])
 def test_timeout_replays_read_without_relaunch(self):
  _,st=self.attempt()
  receipt={'messages':[{'id':'done','type':'worker_done','payload':{'dispatchId':'owned','outcome':'succeeded'},'body':'ok'}]}
  with patch.object(runner,'process_controls'),patch.object(runner,'poll_children'),patch.object(runner,'poll_outputs'),patch.object(runner,'orca',side_effect=[subprocess.TimeoutExpired('check',10),receipt]) as call,patch('team_runner.engine.time.sleep'),patch.object(launch,'launch') as spawn:
   self.assertEqual(runner.wait_coordinator(st)['outcome'],'succeeded')
  self.assertEqual(call.call_count,2);spawn.assert_not_called()

 def test_explicitly_cancelled_closed_child_does_not_block_cleanup(self):
  attempt,st=self.attempt()
  store.atomic_json(attempt/'agents.json',{'agents':[{'role':'tester','dispatch_id':'cancelled','status':'stopped','terminal_closed':True}],'runs':[],'fenced':False})
  self.assertTrue(runner.settle_children(attempt))
 def test_stopped_child_without_close_proof_still_blocks(self):
  attempt,st=self.attempt()
  store.atomic_json(attempt/'agents.json',{'agents':[{'role':'tester','dispatch_id':'cancelled','status':'stopped'}],'runs':[],'fenced':False})
  self.assertFalse(runner.settle_children(attempt))

 def test_next_coordinator_uses_main_mailbox(self):
  from types import SimpleNamespace
  attempt,st=self.attempt()
  st['mailbox']='term_main';st['current'].pop('dispatch_id');runner.save_state(st)
  with patch.object(launch,'launch',return_value={'dispatch_id':'new','terminal':'term_new'}) as call,patch.object(runner,'process_controls'),patch.object(runner,'notify'):
   runner.start_coordinator(SimpleNamespace(mode='auto'),st,'X','spec','title')
  self.assertEqual(call.call_args.kwargs['mailbox'],'term_main')
 def test_preflight_refusal_is_not_unknown_and_never_replayed(self):
  from team_runner import runner_operations as ops
  p=self.root/'refusal.json'
  store.atomic_json(p,{'start':{'args':['orchestration','worker-start'],'status':'unknown','failure_receipt':{'ok':False,'error':{'code':'consumer_fenced'}}}})
  with patch.object(runner,'orca') as call:
   self.assertEqual(ops.reconcile(p,call),[])
   call.assert_not_called()
  self.assertEqual(store.read_json(p)['start']['status'],'rejected')

 def test_technical_cleanup_releases_failures_and_keeps_retained_sessions(self):
  attempt,st=self.setup_agents()
  st['current']['messages']=[{'type':'worker_done','payload':{'dispatchId':d,'outcome':outcome}} for d,outcome in [('coord','failed'),('done','succeeded'),('failed-report','failed'),('keep','succeeded')]]
  runner.save_state(st)
  closed=[]
  def release(a,*args): closed.append(a['dispatch_id']);a['status']='released'
  with patch.object(launch,'release_agent',side_effect=release):
   runner.cleanup_accepted_reports(st)
   runner.cleanup_accepted_reports(st)
  self.assertEqual(closed,['coord','done','failed-report'])
