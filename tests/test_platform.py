"""Isolation contracts: independent projects, checkout ownership and immutable attempts."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid
from unittest.mock import patch
from team_runner.catalog import Catalog, Context, unsettled
from team_runner.engine import Runner
from team_runner.runner_store import atomic_json, read_json, lock
from team_runner import runner_ui, runner_config
from team_runner.cli import main
from team_runner.service import start_runner
from runner_browser_fixture import OrcaFixture, prepare_checkout


class Platform(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root/'config', self.root/'state')
        self.p1 = self.catalog.project('Alpha')['id']
        self.p2 = self.catalog.project('Sber fixture')['id']
        self.runners = []
        for n,p in enumerate((self.p1,self.p2)):
            repo = self.root/str(n); prepare_checkout(repo)
            w = self.catalog.workspace(p, {'path':str(repo),'orca_id':'fixture','orca_instance':'instance'})
            runner = Runner(self.catalog.context(p,w['id']))
            runner.launcher.orca = OrcaFixture(repo/'.transport',repo)
            self.runners.append(runner)
        self.a,self.b = self.runners

    def attempt(self, runner):
        attempt = runner.state_dir/'attempts'/str(uuid.uuid4())
        runner.context.snapshot(attempt)
        atomic_json(attempt/'config.json',runner.load_config())
        runner.save_state({'history':[], 'run_id':'local-run', 'current':{'item':'T1','attempt_dir':str(attempt),'attempt':1}})
        return attempt

    def test_independent_controls_reuse_and_colliding_message_ids(self):
        records=[]
        for r in self.runners:
            attempt=self.attempt(r)
            one=r.launcher.dispatch_role(attempt,'tester','fixture','T1',r.state_dir,function='f')
            records.append(one)
            st=r.load_state(); r.persist_messages(st,{'messages':[{'id':'q','type':'question','body':'Question'}]},'same-run')
        self.assertEqual(records[0]['dispatch_id'],records[1]['dispatch_id'])
        b_before=read_json(self.b.launcher.orca.root/'transport.json')
        st=self.a.load_state()
        r=self.a.submit_control('reply',{'message_id':'q','text':'hold'},str(uuid.uuid4()))
        self.assertEqual(r['status'],'done')
        self.assertEqual(self.a.submit_control('stop')['status'],'done')
        self.assertFalse(self.a.launcher.orca.terminals)
        self.assertEqual(read_json(self.b.launcher.orca.root/'transport.json'),b_before)
        # Same request UUID is independently applied in another workspace.
        request=str(uuid.uuid4())
        self.a.submit_control('pause',request_id=request)
        self.b.submit_control('pause',request_id=request)
        self.assertTrue(self.a.load_state()['paused']); self.assertTrue(self.b.load_state()['paused'])
        with self.assertRaises(ValueError):
            self.a.launcher.dispatch_role(Path(self.b.load_state()['current']['attempt_dir']), 'tester','x','x',self.a.state_dir)
        self.a.submit_control('reconcile')
        self.assertEqual(read_json(self.b.launcher.orca.root/'transport.json'),b_before)

    def test_canonical_alias_unknown_lease_disable_and_path_change(self):
        ctx=self.a.context
        link=self.root/'alias';link.symlink_to(ctx.repo, target_is_directory=True)
        w=self.catalog.workspace(self.p2, {'path':str(link),'orca_id':'fixture','orca_instance':'instance'})
        alias=self.catalog.context(self.p2,w['id'])
        self.assertEqual(ctx.checkout_key,alias.checkout_key)
        with lock(ctx.checkout_lock):
            self.catalog.claim(ctx)
            with self.assertRaises(RuntimeError):
                with lock(alias.checkout_lock,blocking=False): pass
        atomic_json(ctx.state_dir/'startup.json',{'status':'unknown'})
        with lock(alias.checkout_lock), self.assertRaises(ValueError): self.catalog.claim(alias)
        with self.assertRaises(ValueError): self.catalog.disable(ctx.project_id,ctx.workspace_id)
        with self.assertRaises(ValueError): self.catalog.workspace(ctx.project_id, {'path':str(self.b.repo)}, ctx.workspace_id)
        atomic_json(ctx.state_dir/'startup.json',{'status':'idle'})
        self.catalog.disable(ctx.project_id,ctx.workspace_id)
        self.assertTrue(ctx.state_dir.exists())
        with self.assertRaises(ValueError): self.catalog.context(ctx.project_id,ctx.workspace_id)

    def test_process_locks_allow_distinct_checkouts(self):
        pipes=[os.pipe(),os.pipe()]; pids=[]
        for i,r in enumerate(self.runners):
            pid=os.fork()
            if pid==0:
                try:
                    with lock(r.context.checkout_lock,blocking=False):
                        os.write(pipes[i][1],b'1'); os.read(pipes[1-i][0],1)
                finally: os._exit(0)
            pids.append(pid)
        for pid in pids:
            _,status=os.waitpid(pid,0);self.assertEqual(os.waitstatus_to_exitcode(status),0)
        for pair in pipes:
            for fd in pair:os.close(fd)

    def test_profiles_precedence_snapshot_and_inherited_conflict(self):
        ctx=self.a.context
        instruction=self.a.repo/'AGENTS.md';instruction.write_text('original rules')
        self.catalog.project(project_id=ctx.project_id, values={'roles':{'tester':{'model':'project'}}})
        self.catalog.workspace(ctx.project_id, {'roles':{'tester':{'model':'workspace'}},'profile':{'instructions':['AGENTS.md']}},ctx.workspace_id)
        r=Runner(self.catalog.context(ctx.project_id,ctx.workspace_id));attempt=self.attempt(r)
        instruction.write_text('new rules')
        self.catalog.workspace(ctx.project_id, {'roles':{'tester':{'model':'next'}},'profile':{'constraints':['new']}},ctx.workspace_id)
        self.assertEqual(read_json(attempt/'context.json')['instructions'][0]['text'],'original rules')
        self.assertEqual(read_json(attempt/'config.json')['roles']['tester']['model'],'workspace')
        nxt=Runner(self.catalog.context(ctx.project_id,ctx.workspace_id))
        self.assertEqual(nxt.load_config()['roles']['tester']['model'],'next')
        self.catalog.project(project_id=ctx.project_id, values={'profile':{'acceptance':{'require_commit':True},'workflow':{'allow_commits':True}}})
        self.catalog.workspace(ctx.project_id, {'profile':{'acceptance':{'require_clean':True}}},ctx.workspace_id)
        merged=self.catalog.context(ctx.project_id,ctx.workspace_id).profile
        self.assertTrue(merged['acceptance']['require_commit']); self.assertTrue(merged['acceptance']['require_clean'])
        self.assertEqual(nxt.load_config(overrides={'agent':'codex','model':'cli'})['roles']['coordinator']['model'],'cli')
        env={**ctx.environment(attempt)}
        with patch.dict(os.environ,env):
            self.assertEqual(main(['status','--project',self.b.context.project_id,'--workspace',self.b.context.workspace_id]),1)
        with self.assertRaises(ValueError): self.b.context.check_attempt(attempt)

    def test_bad_orca_incarnation_missing_checkout_and_no_auto_start(self):
        ctx=self.a.context
        ctx.verify(self.a.orca)
        for changes in ({'path':str(self.b.repo)}, {'instanceId':'foreign'}, {'id':'foreign'}, {'hostId':'remote'}):
            wt={'id':ctx.orca_id,'instanceId':ctx.orca_instance,'path':str(ctx.repo),'hostId':'local',**changes}
            with self.assertRaises(ValueError):ctx.verify(lambda *a:{'worktree':wt})
        with patch.object(self.a, 'orca',side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):start_runner(self.a,{'goal':'feat/goal-fixture'})
        self.assertFalse((ctx.state_dir/'startup.json').exists())
        self.assertEqual(self.catalog.read(),Catalog(self.catalog.config_root,self.catalog.state_root).read())
        moved=self.root/'moved';ctx.repo.rename(moved)
        with self.assertRaises(OSError):ctx.verify(lambda *a:{})

    def test_start_after_idle_stop_preserves_history_and_options(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                history = [{'item': 'GOAL-FINAL-REVIEW', 'problems': []}]
                st = {'history': history, 'current': None, 'goal': 'feat/goal-fixture',
                      'options': {'goal': 'feat/goal-fixture', 'mode': 'confirm'},
                      'stop_requested': True, 'stop_status': 'stopped'}
                self.a.save_state(st)
                atomic_json(self.a.state_dir / 'startup.json', {'status': 'idle'})
                with patch.object(self.a.launcher, 'create_terminal', return_value={'handle': 'fixture'}):
                    result = start_runner(self.a, {'resume': True} if resume else {'goal': 'feat/goal-fixture'})
                self.assertEqual(result['status'], 'starting')
                saved = self.a.load_state()
                self.assertEqual(saved['history'], history)
                self.assertEqual(saved['options'], st['options'])
                self.assertNotIn('stop_requested', saved)
                self.assertNotIn('stop_status', saved)

    def test_idle_restart_does_not_bypass_uncertainty_or_validation(self):
        for changes, data in (({'current': {'item': 'T1'}}, {'resume': True}),
                              ({'stop_status': 'unknown'}, {'resume': True}),
                              ({'control_uncertain': 'command'}, {'resume': True}),
                              ({'recovery_issues': ['unconfirmed']}, {'resume': True}),
                              ({}, {'goal': 'missing'})):
            with self.subTest(changes=changes, data=data):
                st = {'history': [], 'current': None, 'stop_requested': True,
                      'stop_status': 'stopped', **changes}
                self.a.save_state(st)
                with patch.object(self.a.launcher, 'create_terminal') as create:
                    with self.assertRaises(ValueError):start_runner(self.a, data)
                    create.assert_not_called()
                self.assertEqual(self.a.load_state(), st)

    def test_cli_catalog_lifecycle_without_start(self):
        import contextlib, io
        env={'XDG_CONFIG_HOME':str(self.root/'cli-config'),'XDG_STATE_HOME':str(self.root/'cli-state')}
        def invoke(argv):
            output=io.StringIO()
            with contextlib.redirect_stdout(output), patch.dict(os.environ,env):
                self.assertEqual(main(argv),0)
            return json.loads(output.getvalue())
        p=invoke(['project','add','--name','CLI'])
        w=invoke(['workspace','add','--project',p['id'],'--path',str(self.a.repo),'--orca-id','fixture','--orca-instance','instance'])
        invoke(['workspace','update','--project',p['id'],'--workspace',w['id'],'--name','Renamed'])
        self.assertEqual(invoke(['workspace','list','--project',p['id']])[w['id']]['name'],'Renamed')
        self.assertFalse(invoke(['status','--project',p['id'],'--workspace',w['id']])['runner_running'])
        self.assertFalse((self.root/'cli-state').exists())
        invoke(['workspace','disable','--project',p['id'],'--workspace',w['id']])
        self.assertTrue(invoke(['workspace','list','--project',p['id']])[w['id']]['disabled'])

    def test_archive_never_imports_or_executes_queue_and_refuses_unknown(self):
        from team_runner.migration import archive, assert_quiescent
        legacy=self.root/'legacy'
        atomic_json(legacy/'state.json',{'history':[{'dispatch_id':'original'}],'current':None})
        atomic_json(legacy/'commands/old.json',{'action':'run','status':'pending'})
        atomic_json(legacy/'startup.json',{'status':'unknown'})
        with self.assertRaises(ValueError):assert_quiescent(legacy,self.catalog)
        with self.assertRaises(ValueError):archive(legacy,[],self.root/'blocked',self.catalog)
        atomic_json(legacy/'startup.json',{'status':'idle'})
        assert_quiescent(legacy,self.catalog)
        target=self.root/'archive'
        result=archive(legacy,[],target,self.catalog)
        self.assertFalse(result['executable_import'])
        self.assertEqual(read_json(legacy/'commands/old.json')['status'],'pending')
        self.assertEqual(read_json(legacy/'state.json')['history'][0]['dispatch_id'],'original')
        self.assertEqual((target/'legacy.tar.gz').stat().st_mode & 0o222,0)
        # Allow TemporaryDirectory's owner to remove the intentionally read-only archive.
        target.chmod(0o700)
        for file in target.iterdir():file.chmod(0o600)

    def test_http_scopes_security_and_catalog(self):
        from team_runner.project_services import ProjectServices
        services = ProjectServices(self.catalog)
        for project in (self.p1, self.p2):
            self.assertEqual(services.action(project, 'start', str(uuid.uuid4()))['status'], 'done')
            self.addCleanup(services.action, project, 'stop', str(uuid.uuid4()))
        server,token=runner_ui.make_server(self.catalog)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,data=None,headers=None):
            req=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token,'Origin':base,
                'Content-Type':'application/json',**(headers or {})},data=json.dumps(data).encode() if data is not None else None)
            with urllib.request.urlopen(req) as response:return json.load(response)
        try:
            project=request('/api/projects',{'name':'Third'})
            self.assertIn(project['id'],request('/api/projects')['projects'])
            prefixes=['/api/projects/'+r.context.project_id+'/workspaces/'+r.context.workspace_id for r in self.runners]
            req={'request_id':str(uuid.uuid4())}
            request(prefixes[0]+'/pause',req)
            self.assertTrue(request(prefixes[0]+'/state')['state']['paused'])
            self.assertNotIn('paused',request(prefixes[1]+'/state')['state'])
            for path,headers in [(prefixes[0]+'/pause',{'Origin':'http://foreign'}),(prefixes[0]+'/pause',{'Host':'foreign'}),('/api/projects',{'Authorization':''})]:
                with self.assertRaises(urllib.error.HTTPError) as cm:request(path,req,headers)
                self.assertEqual(cm.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):request('/api/pause',req)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
