import subprocess
import unittest
from unittest.mock import patch
from team_runner import orca
class MailboxRouting(unittest.TestCase):
 def test_explicit_mailbox(self):
  for command,flag in [('run-create','--from'),('run-current','--from'),('run-use','--from'),('worker-start','--from'),('check','--terminal')]:
   with self.subTest(command=command),patch.object(orca.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"ok":true,"result":{}}','')) as run:
    orca.call('orchestration',command,mailbox='term_mailbox')
    self.assertIn(flag,run.call_args.args[0])
    argv=run.call_args.args[0]
    self.assertEqual(argv[argv.index(flag)+1],'term_mailbox')
 def test_context_path_cannot_replace_selected_orca(self):
  from types import SimpleNamespace
  context=SimpleNamespace(repo='/tmp',environment=lambda:{'PATH':'/usr/bin'})
  result=subprocess.CompletedProcess([],0,'{"ok":true,"result":{}}','')
  with patch.object(orca,'executable',return_value='orca'),patch.object(orca.shutil,'which',return_value='/shim/orca'),patch.object(orca.subprocess,'run',return_value=result) as run:
   orca.call('terminal','list',context=context)
   self.assertEqual(run.call_args.args[0][0],'/shim/orca')
   self.assertEqual(run.call_args.kwargs['env']['PATH'],'/usr/bin')
 def test_target_terminal_preserved(self):
  with patch.object(orca.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"ok":true,"result":{}}','')) as run:
   orca.call('terminal','show','--terminal','term_worker',mailbox='term_mailbox')
   self.assertEqual(run.call_args.args[0][-3:],['--terminal','term_worker','--json'])
 def test_full_records_survive_cli_concise_projection(self):
  import json
  records=[
   ('orchestration','run-show',{'run':{'id':'run_parent','coordinator_handle':'term_mailbox','legacy':0}}, {'runId':'run_parent'}),
   ('orchestration','run-current',{'run':{'id':'run_parent','coordinator_handle':'term_mailbox'}}, {'runId':'run_parent'}),
   ('orchestration','run-create',{'run':{'id':'run_child','coordinator_handle':'term_child'}}, {'runId':'run_child'}),
   ('orchestration','request-show',{'state':'completed','receipt':{'ok':True,'result':{'run':{'id':'run_child'}}}}, {'state':'completed'}),
  ]
  for group,command,full,concise in records:
   def transport(argv,**kwargs):
    return subprocess.CompletedProcess(argv,0,json.dumps({'ok':True,'result':full if '--full' in argv else concise}),'')
   with self.subTest(command=command),patch.object(orca.subprocess,'run',side_effect=transport):
    self.assertEqual(orca.call(group,command,mailbox='term_mailbox'),full)
 def test_full_flag_is_not_sent_to_other_command_groups(self):
  for group,command in [('worktree','show'),('terminal','show'),('terminal','create')]:
   with self.subTest(group=group,command=command),patch.object(orca.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"ok":true,"result":{}}','')) as run:
    orca.call(group,command)
    self.assertNotIn('--full',run.call_args.args[0])
class JournalMailbox(unittest.TestCase):
 def test_effect_uses_persisted_mailbox_before_process_environment(self):
  from pathlib import Path
  from types import SimpleNamespace
  from team_runner.engine import Runner
  runner=Runner(SimpleNamespace(repo=Path('/tmp'),state_dir=Path('/tmp')))
  with patch('team_runner.engine.operations.mutation') as mutation:
   runner.effect({'mailbox':'term_main'},'test','orchestration','run-create')
   self.assertEqual(mutation.call_args.kwargs['mailbox'],'term_main')
   runner.effect({'mailbox':'term_main'},'child','orchestration','check',mailbox='term_child')
   self.assertEqual(mutation.call_args.kwargs['mailbox'],'term_child')
if __name__=='__main__': unittest.main()
