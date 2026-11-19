"""Graph profiles and call chains, using isolated fake Orca only."""
import copy
import json
import os
from pathlib import Path
import threading
import unittest
import uuid
from unittest.mock import patch

from team_runner import runner_config as config, team_graph, graph_dispatch as graph
from team_runner.runner_store import atomic_json, read_json, lock
from team_runner.team_profiles import TeamProfiles
import test_platform as fixtures


def edge(ident, source, target, kind='delegation', limit=3):
    return dict(id=ident, source=source, target=target, type=kind, label='', instructions='', max_passes=limit)


def small_graph():
    cfg = team_graph.migrate(config.defaults())
    cfg['nodes'] = [n for n in cfg['nodes'] if n['id'] in ('coordinator', 'implementer_standard', 'tester')]
    cfg['layout'] = {n['id']: cfg['layout'][n['id']] for n in cfg['nodes']}
    cfg['edges'] = [edge('implement', 'coordinator', 'implementer_standard'),
                    edge('check', 'implementer_standard', 'tester'),
                    edge('revise', 'tester', 'implementer_standard', 'rework')]
    return cfg


class GraphConfig(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt

    def validate(self, cfg):
        return config.validate(cfg, repo=self.a.repo, config_root=self.catalog.config_root.parent)

    def test_migrate_load_save_library_snapshot(self):
        cfg = self.validate(small_graph())
        atomic_json(self.a.context.settings_dir / 'config.json', cfg)
        self.assertEqual(self.a.load_config(), cfg)
        attempt = self.attempt(self.a)
        self.assertEqual(config.snapshot_role(attempt, 'coordinator'), cfg['nodes'][0]['primary'])
        cfg['nodes'][0]['instructions'] = 'next attempt'
        library = TeamProfiles(self.catalog.config_root)
        saved = library.update({'action':'create', 'name':'Team', 'config':cfg}, self.validate)
        ident = saved['profiles'][0]['id']
        library.update({'action':'rename', 'id':ident, 'name':'Renamed'}, self.validate)
        cfg['layout']['tester']['x'] = 123
        library.update({'action':'save', 'id':ident, 'config':cfg}, self.validate)
        self.assertEqual(TeamProfiles(self.catalog.config_root).read()['profiles'][0]['config'], cfg)
        self.assertEqual(read_json(attempt / 'config.json')['nodes'][0]['instructions'], '')
        library.update({'action':'delete', 'id':ident}, self.validate)
        self.assertEqual(library.read()['profiles'], [])
        self.assertEqual(self.a.load_config()['nodes'][0]['instructions'], '')
        from team_runner.application import configuration, write_configuration
        self.assertEqual(configuration(self.a)['graph']['version'], 3)
        write_configuration(self.a, cfg)
        self.assertEqual(read_json(attempt / 'config.json')['nodes'][0]['instructions'], '')

    def test_arbitrary_composition_duplicate_roles_multi_parent_and_cycles(self):
        cfg = small_graph()
        clone = copy.deepcopy(cfg['nodes'][-1]);clone.update(id='other_tester', name='Second tester')
        cfg['nodes'].append(clone);cfg['layout']['other_tester'] = {'x':0,'y':0}
        cfg['edges'] += [edge('other_check', 'coordinator', 'other_tester'),
                         edge('multi_parent', 'other_tester', 'tester'),
                         edge('cycle', 'tester', 'implementer_standard')]
        self.validate(cfg)
        cfg['nodes'] = cfg['nodes'][:1];cfg['edges'] = [];cfg['layout'] = {'coordinator':{'x':1,'y':2}}
        self.validate(cfg)

    def test_invalid_graphs(self):
        changes = [lambda c:c['nodes'].append(copy.deepcopy(c['nodes'][0])),
                   lambda c:c['edges'][0].update(target='missing'),
                   lambda c:c['nodes'][1].update(role='coordinator'),
                   lambda c:c['edges'].clear(),
                   lambda c:c['nodes'][1].update(group='missing'),
                   lambda c:c['layout']['tester'].update(x=float('nan')),
                   lambda c:c['edges'][0].update(max_passes=0),
                   lambda c:c['edges'][0].update(max_passes=True),
                   lambda c:c['nodes'][1]['primary'].update(effort='bogus')]
        for change in changes:
            cfg = small_graph();change(cfg)
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):self.validate(cfg)

    def test_legacy_config_stays_legacy_until_graph_save(self):
        from team_runner.application import configuration, write_configuration
        value = configuration(self.a)
        self.assertEqual(value['config']['version'],2)
        self.assertEqual(value['graph']['version'],3)
        write_configuration(self.a,value['graph'])
        self.assertEqual(self.a.load_config()['version'],3)
        with self.assertRaises(ValueError):config.settings_update({'version':1,'roles':{}}, self.a.load_config())


class GraphExecution(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt

    def prepare(self, cfg=None):
        attempt = self.attempt(self.a)
        atomic_json(attempt / 'config.json', cfg or small_graph())
        task = self.a.launcher.orca('orchestration','task-create','--run','root-run','--task-title','root','--spec','root')['task']['id']
        root = self.a.launcher.launch(attempt,'coordinator',task,'root-run','root',self.a.state_dir)
        state = self.a.load_state()
        state['current']['dispatch_id'] = root['dispatch_id']
        self.a.save_state(state)
        return attempt, root

    def delegate(self, attempt, parent, ident='implement', request=None, **kwargs):
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE':parent['terminal']}):
            call = graph.delegate(self.a.launcher, attempt, ident, 'spec', ident, self.a.state_dir,
                                  request or str(uuid.uuid4()), **kwargs)
        return call, next(a for a in self.a.launcher.registry(attempt)['agents'] if a.get('call_id') == call['id']) if call.get('child_agent') else None

    def finish(self, attempt, child, outcome='succeeded', return_edge=None):
        path = self.a.launcher.orca.root / 'transport.json'
        transport = read_json(path);transport['workers'][child['dispatch_id']]['done'] = True;atomic_json(path,transport)
        payload = dict(outcome=outcome, summary='result', checks=[], files_modified=[])
        if return_edge:
            payload['return_edge'] = return_edge
            reg = self.a.launcher.registry(attempt)
            graph.return_edge(self.a.launcher, attempt, reg, child, payload)
        atomic_json(attempt / 'reports' / (child['id'] + '-summary.json'), payload)
        st = self.a.load_state()
        self.a.persist_messages(st, {'messages':[{'id':str(uuid.uuid4()),'type':'worker_done',
            'payload':{'dispatchId':child['dispatch_id'], 'outcome':'failed' if return_edge else outcome}}]},child['run_id'])
        self.a.release_completed_children(attempt)

    def test_three_levels_rework_recheck_and_fresh_sessions(self):
        attempt,root = self.prepare()
        outer,impl = self.delegate(attempt,root)
        check,tester = self.delegate(attempt,impl,'check')
        reg = self.a.launcher.registry(attempt)
        self.assertEqual([a['status'] for a in reg['agents']],['waiting_child','waiting_child','active'])
        with self.assertRaisesRegex(ValueError,'unresolved child'):
            graph.return_edge(self.a.launcher, attempt, reg, impl, {'outcome':'succeeded'})
        self.finish(attempt,tester,'changes_requested','revise')
        reg = self.a.launcher.registry(attempt)
        self.assertEqual(reg['graph_calls'][1]['result']['outcome'],'changes_requested')
        self.assertEqual(next(a for a in reg['agents'] if a['id']==impl['id'])['status'],'active')
        with self.assertRaisesRegex(ValueError,'new successful'):
            graph.return_edge(self.a.launcher,attempt,reg,impl,{'outcome':'succeeded'})
        recheck,fresh = self.delegate(attempt,impl,'check')
        self.assertNotEqual(fresh['terminal'],tester['terminal'])
        self.assertNotEqual(fresh['dispatch_id'],tester['dispatch_id'])
        self.finish(attempt,fresh)
        self.finish(attempt,impl)
        reg = self.a.launcher.registry(attempt)
        self.assertFalse(graph.rework_problems(reg))
        self.assertEqual(reg['graph_calls'][0]['status'],'completed')
        self.assertEqual(reg['graph_calls'][0]['result']['outcome'],'succeeded')
        self.assertEqual(next(a for a in reg['agents'] if a['id']==root['id'])['status'],'active')

    def test_normal_reconcile_settles_accepted_failed_child_without_acceptance(self):
        attempt, root = self.prepare()
        call, child = self.delegate(attempt, root)
        transport_path = self.a.launcher.orca.root / 'transport.json'
        transport = read_json(transport_path)
        transport['workers'][child['dispatch_id']]['done'] = True
        atomic_json(transport_path, transport)
        st = self.a.load_state()
        self.a.persist_messages(st, {'messages': [{'id': 'failed-result', 'type': 'worker_done',
            'payload': {'dispatchId': child['dispatch_id'], 'outcome': 'failed'}}]}, child['run_id'])
        reg = self.a.launcher.registry(attempt)
        next(a for a in reg['agents'] if a['id'] == child['id']).update(status='unknown', error='settlement unconfirmed')
        reg['fenced'] = True
        self.a.launcher.save_registry(attempt, reg)
        observe = self.a.launcher.observe_agent
        def failed_child(agent):
            data = observe(agent)
            if agent['id'] == child['id']:
                data['dispatch']['status'] = 'failed'
                data.setdefault('worker', {})['state'] = 'failed'
            return data
        with patch.object(self.a.launcher, 'observe_agent', side_effect=failed_child):
            self.a.recover(self.a.load_state())
        reg = self.a.launcher.registry(attempt)
        self.assertFalse(reg['fenced'])
        self.assertEqual(next(a for a in reg['agents'] if a['id'] == child['id'])['status'], 'released')
        self.assertEqual(next(a for a in reg['agents'] if a['id'] == root['id'])['status'], 'active')
        result = next(c for c in reg['graph_calls'] if c['id'] == call['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['outcome'], 'failed')
        self.assertEqual(self.a.load_state()['history'], [])
        self.assertEqual(self.a.load_state()['recovery_issues'], [])
        self.assertEqual(len(self.a.launcher.orca.workers), 2)

    def test_request_dedup_limit_grant_and_snapshot(self):
        cfg = small_graph();cfg['edges'][0]['max_passes'] = 1
        attempt,root = self.prepare(cfg)
        identifier = str(uuid.uuid4())
        first,child = self.delegate(attempt,root,request=identifier)
        duplicate,same_child = self.delegate(attempt,root,request=identifier)
        self.assertEqual(child,same_child)
        self.assertEqual(first,duplicate)
        self.assertEqual(self.a.launcher.registry(attempt)['edge_passes'],{'implement':1})
        self.finish(attempt,child)
        blocked,_ = self.delegate(attempt,root)
        self.assertEqual(blocked['status'],'limit')
        from team_runner.protocol import summary
        self.assertEqual(summary(self.a)['blocker']['code'],'graph_edge_limit')
        token = str(uuid.uuid4())
        graph.grant(self.a.launcher,attempt,blocked['id'],token)
        graph.grant(self.a.launcher,attempt,blocked['id'],token)
        resumed,new_child = self.delegate(attempt,root,request=blocked['id'])
        self.assertEqual(resumed['status'],'running')
        self.assertNotEqual(child['terminal'],new_child['terminal'])
        self.assertEqual(self.a.launcher.registry(attempt)['edge_passes'],{'implement':2})
        self.assertEqual(read_json(attempt/'config.json'),cfg)
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE':root['terminal']}), self.assertRaises(ValueError):
            graph.delegate(self.a.launcher,attempt,'implement','different','implement',self.a.state_dir,identifier)

    def test_authorization_ancestor_and_direct_launch_bypass(self):
        cfg = small_graph();cfg['edges'].append(edge('cycle','tester','coordinator'))
        attempt,root = self.prepare(cfg)
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':'foreign'}),self.assertRaises(ValueError):
            graph.delegate(self.a.launcher,attempt,'implement','s','t',self.a.state_dir,str(uuid.uuid4()))
        outer,impl = self.delegate(attempt,root)
        with self.assertRaisesRegex(ValueError,'not permitted'):self.delegate(attempt,impl,'implement')
        check,tester = self.delegate(attempt,impl,'check')
        with self.assertRaisesRegex(ValueError,'ancestor'):self.delegate(attempt,tester,'cycle')
        with self.assertRaisesRegex(ValueError,'allowed edge'):
            self.a.launcher.launch(attempt,'tester','x','x','x',self.a.state_dir)
        with self.assertRaisesRegex(ValueError,'delegate'):
            self.a.launcher.dispatch_role(attempt,'tester','s','t',self.a.state_dir)
        reg = self.a.launcher.registry(attempt)
        with self.assertRaisesRegex(ValueError,'immediate caller'):
            graph.return_edge(self.a.launcher,attempt,reg,tester,{'outcome':'changes_requested','return_edge':'implement'})

    def test_unknown_dispatch_reconcile_no_duplicate_effect_and_nested_stop(self):
        attempt,root = self.prepare()
        identifier = str(uuid.uuid4())
        (self.a.launcher.orca.root / 'lose-dispatch').touch()
        with self.assertRaisesRegex(RuntimeError,'lost Dispatch'):
            self.delegate(attempt,root,request=identifier)
        before = read_json(self.a.launcher.orca.root/'transport.json')
        with self.assertRaisesRegex(RuntimeError,'fenced'):
            self.delegate(attempt,root,request=identifier)
        self.assertEqual(read_json(self.a.launcher.orca.root/'transport.json'),before)
        self.assertEqual(self.a.launcher.reconcile_attempt(attempt),[])
        first,impl = self.delegate(attempt,root,request=identifier)
        self.assertEqual(len(self.a.launcher.orca.workers),2)
        nested,tester = self.delegate(attempt,impl,'check')
        self.assertTrue(self.a.launcher.stop_attempt(attempt))
        self.assertFalse(self.a.launcher.orca.terminals)
        self.assertTrue(all(c['status']=='stopped' for c in self.a.launcher.registry(attempt)['graph_calls']))

    def test_recovery_uses_exact_replacement_not_superseded_record(self):
        attempt,root = self.prepare()
        ident = str(uuid.uuid4())
        call,child = self.delegate(attempt,root,request=ident)
        reg = self.a.launcher.registry(attempt)
        previous = dict(child, id=str(uuid.uuid4()), status='superseded', superseded_by=child['id'])
        previous.pop('dispatch_id')
        reg['agents'].insert(1,previous)
        self.a.launcher.save_registry(attempt,reg)
        repeated,again = self.delegate(attempt,root,request=ident)
        self.assertEqual(repeated['child_agent'],child['id'])
        self.assertFalse(self.a.launcher.registry(attempt)['fenced'])
        self.assertEqual(len(self.a.launcher.orca.workers),2)

    def test_resume_settles_persisted_reports_child_first(self):
        attempt, root = self.prepare()
        _, parent = self.delegate(attempt, root)
        _, child = self.delegate(attempt, parent, 'check')
        path = self.a.launcher.orca.root / 'transport.json'
        transport = read_json(path)
        for worker in (parent, child):
            transport['workers'][worker['dispatch_id']]['done'] = True
        atomic_json(path, transport)
        state = self.a.load_state()
        self.assertEqual(parent['run_id'], child['run_id'])
        receipt = {'messages': [{'id': str(uuid.uuid4()), 'type': 'worker_done',
            'payload': {'dispatchId': worker['dispatch_id'], 'outcome': 'failed'}}
            for worker in (parent, child)]}
        def checkpoint(stage):
            if stage == 'message:after_save':
                raise SystemExit(86)

        with patch('team_runner.runner_operations.checkpoint', side_effect=checkpoint), self.assertRaises(SystemExit):
            self.a.persist_messages(state, receipt, child['run_id'])
        self.assertTrue(all(c['status'] == 'running' for c in self.a.launcher.registry(attempt)['graph_calls']))
        with patch.object(self.a.launcher, 'release_agent', wraps=self.a.launcher.release_agent) as release:
            self.a.recover(self.a.load_state())
            self.assertEqual([entry.args[0]['id'] for entry in release.call_args_list], [child['id'], parent['id']])
        self.assertEqual([c['result']['outcome'] for c in self.a.launcher.registry(attempt)['graph_calls']],
                         ['failed', 'failed'])

    def test_foreign_or_conflicting_report_cannot_settle_call(self):
        attempt, root = self.prepare()
        _, child = self.delegate(attempt, root)
        state = self.a.load_state()
        report = {'id': 'foreign', 'type': 'worker_done',
                  'payload': {'dispatchId': child['dispatch_id'], 'outcome': 'succeeded'}}
        with patch.object(self.a.launcher, 'release_agent') as release:
            self.a.persist_messages(state, {'messages': [report]}, 'foreign-run')
            self.assertTrue(self.a.launcher.registry(attempt)['fenced'])
            state['current']['messages'] = []
            self.a.persist_messages(state, {'messages': [
                dict(report, id='success'),
                dict(report, id='failure', payload=dict(report['payload'], outcome='failed'))]}, child['run_id'])
            self.a.recover(self.a.load_state())
            release.assert_not_called()
        registry = self.a.launcher.registry(attempt)
        self.assertTrue(registry['fenced'])
        self.assertEqual(registry['graph_calls'][0]['status'], 'running')

    def test_crash_after_child_close_settles_after_restart_without_replay(self):
        attempt, root = self.prepare()
        _, child = self.delegate(attempt, root)

        def checkpoint(stage):
            if stage == 'close:after_receipt':
                raise SystemExit(86)

        with patch('team_runner.runner_operations.checkpoint', side_effect=checkpoint), self.assertRaises(SystemExit):
            self.finish(attempt, child)
        journal = self.a.launcher.journal(attempt)
        close_key = child['id'] + '-close'
        self.assertEqual(read_json(journal)[close_key]['stage'], 'receipt')
        self.assertEqual(self.a.launcher.registry(attempt)['graph_calls'][0]['status'], 'settling')
        from team_runner.engine import Runner
        old = self.a
        self.a = Runner(old.context)
        self.a.launcher.orca = old.launcher.orca
        with patch.object(self.a.launcher, 'release_agent') as release:
            self.assertEqual(self.a.settle_graph_reports(attempt), [])
            self.assertEqual(self.a.settle_graph_reports(attempt), [])
            release.assert_not_called()
        self.assertEqual(read_json(journal)[close_key]['stage'], 'close_verified')
        reg = self.a.launcher.registry(attempt)
        self.assertEqual(reg['graph_calls'][0]['status'], 'completed')
        self.assertEqual(next(a for a in reg['agents'] if a['id'] == root['id'])['status'], 'active')

    def test_interrupted_close_without_receipt_keeps_call_blocked(self):
        attempt, root = self.prepare()
        _, child = self.delegate(attempt, root)

        def checkpoint(stage):
            if stage == 'close:after_effect':
                raise SystemExit(86)

        with patch('team_runner.runner_operations.checkpoint', side_effect=checkpoint), self.assertRaises(SystemExit):
            self.finish(attempt, child)
        with patch.object(self.a.launcher, 'release_agent') as release:
            self.assertTrue(self.a.settle_graph_reports(attempt))
            release.assert_not_called()
        reg = self.a.launcher.registry(attempt)
        self.assertTrue(reg['fenced'])
        self.assertEqual(reg['graph_calls'][0]['status'], 'settling')

    def test_stop_during_startup_prevents_dispatch_and_epoch_resume(self):
        from team_runner import goal_intent
        attempt,root = self.prepare()
        goal_intent.authorize(self.a, {'goal':'feat/goal-fixture'},str(uuid.uuid4()))
        original = self.a.launcher.orca
        def cancel(*args,**kwargs):
            result = original(*args,**kwargs)
            if args[:2] == ('terminal','wait'):
                goal_intent.change(self.a, desired_state='stopped')
            return result
        with patch.object(self.a.launcher,'orca',side_effect=cancel):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                self.delegate(attempt,root)
        self.assertEqual(len(original.workers),1)
        self.assertTrue(self.a.launcher.registry(attempt)['fenced'])

    def test_json_message_cursor_cannot_cross_caller_dispatch(self):
        from team_runner.agent_messages import messages
        attempt,root = self.prepare()
        _,impl = self.delegate(attempt,root)
        _,tester = self.delegate(attempt,impl,'check')
        st = self.a.load_state()
        st['current']['messages'] = [{'id':a['id'],'run_id':'fixture-run','type':'question','payload':{'dispatchId':a['dispatch_id']}} for a in (root,impl,tester)]
        self.a.save_state(st)
        with patch.dict(os.environ,{'TEAM_RUNNER_ATTEMPT':str(attempt),'ORCA_TERMINAL_HANDLE':root['terminal']}):
            result = messages(self.a,limit=1)
            self.assertEqual(result['items'][0]['id'],root['id'])
            following = messages(self.a,after=result['next_cursor'])
            self.assertEqual([m['id'] for m in following['items']],[impl['id']])
        with patch.dict(os.environ,{'TEAM_RUNNER_ATTEMPT':str(attempt),'ORCA_TERMINAL_HANDLE':impl['terminal']}):
            with self.assertRaisesRegex(ValueError,'another caller Dispatch'):
                messages(self.a,after=result['next_cursor'])

    def test_return_intent_rejects_changed_payload_before_receipt(self):
        cfg=small_graph();cfg['edges'].append(edge('revise_again','tester','implementer_standard','rework'))
        attempt,root = self.prepare(cfg)
        _,impl = self.delegate(attempt,root)
        _,tester = self.delegate(attempt,impl,'check')
        reg=self.a.launcher.registry(attempt)
        graph.return_edge(self.a.launcher,attempt,reg,tester,{'outcome':'changes_requested','summary':'first','return_edge':'revise'})
        for payload in ({'outcome':'changes_requested','summary':'different','return_edge':'revise'},
                        {'outcome':'changes_requested','summary':'first','return_edge':'revise_again'}):
            with self.assertRaisesRegex(ValueError,'input changed'):
                graph.return_edge(self.a.launcher,attempt,reg,tester,payload)
        self.assertEqual(reg['edge_passes']['revise'],1)
        self.assertNotIn('revise_again',reg['edge_passes'])


    def test_wait_serializes_with_child_close_in_flight(self):
        attempt,root = self.prepare()
        call,child = self.delegate(attempt,root)
        attempted = threading.Event()
        results, errors = [], []
        original_check = self.a.launcher.check_operations

        def waiting_lock(path, *args, **kwargs):
            attempted.set()
            return lock(path, *args, **kwargs)

        def check_operations(path):
            try:
                return original_check(path)
            finally:
                # On the old implementation, the unsynchronized check sees
                # the still-pending close and fails before signalling us.
                attempted.set()

        def wait():
            try:
                results.append(graph.wait(self.a.launcher,attempt,call['id']))
            except Exception as exc:
                errors.append(exc)

        journal = self.a.launcher.journal(attempt)
        operations = read_json(journal,{})
        with patch.object(graph,'lock',side_effect=waiting_lock), \
                patch.object(self.a.launcher,'check_operations',side_effect=check_operations):
            with lock(attempt / 'agents.lock'):
                reg = self.a.launcher.registry(attempt)
                reg['graph_calls'][0]['status'] = 'settling'
                self.a.launcher.save_registry(attempt,reg)
                pending = copy.deepcopy(operations)
                pending['child-close'] = {'status':'pending','stage':'calling'}
                atomic_json(journal,pending)
                thread = threading.Thread(target=wait)
                thread.start()
                observed = attempted.wait(5)
                # Complete the close while still owning the settlement lock.
                atomic_json(journal,operations)
                reg['graph_calls'][0].update(status='completed',result={'outcome':'succeeded'})
                self.a.launcher.save_registry(attempt,reg)
            thread.join(5)
        self.assertTrue(observed)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(results[0]['status'],'completed')

    def test_wait_unknown_operation_still_requires_reconciliation(self):
        attempt,root = self.prepare()
        call,child = self.delegate(attempt,root)
        journal = self.a.launcher.journal(attempt)
        operations = read_json(journal,{})
        operations['child-close'] = {'status':'unknown'}
        atomic_json(journal,operations)
        with self.assertRaisesRegex(RuntimeError,'operation outcome unknown'):
            graph.wait(self.a.launcher,attempt,call['id'])

    def test_unknown_child_without_report_fences_chain(self):
        attempt,root = self.prepare()
        call,child = self.delegate(attempt,root)
        original=self.a.launcher.observe_agent
        def unknown(record):
            data=original(record)
            if record['id']==child['id']:
                data.update(dispatch={'status':'failed'},worker={'state':'outcome_unknown'})
            return data
        with patch.object(self.a.launcher,'observe_agent',side_effect=unknown):
            self.a.release_completed_children(attempt)
        reg=self.a.launcher.registry(attempt)
        self.assertTrue(reg['fenced'])
        self.assertEqual(next(a for a in reg['agents'] if a['id']==child['id'])['status'],'unknown')
        with self.assertRaisesRegex(RuntimeError,'blocked'):
            graph.wait(self.a.launcher,attempt,call['id'],sleep=lambda _:None)
        from team_runner.protocol import summary
        self.assertEqual(summary(self.a)['blocker']['code'],'graph_outcome_unknown')


    def test_unknown_runtime_despite_accepted_report_fences_chain(self):
        attempt,root = self.prepare()
        call,child = self.delegate(attempt,root)
        st=self.a.load_state();st['current']['messages']=[{'id':'accepted','type':'worker_done','payload':{'dispatchId':child['dispatch_id'],'outcome':'succeeded'}}];self.a.save_state(st)
        with patch.object(self.a.launcher,'observe_agent',return_value={'dispatch':{'status':'failed'},'worker':{'state':'outcome_unknown'}}):
            self.a.release_completed_children(attempt)
        self.assertTrue(self.a.launcher.registry(attempt)['fenced'])
        with self.assertRaisesRegex(RuntimeError,'blocked'):
            graph.wait(self.a.launcher,attempt,call['id'],sleep=lambda _:None)


    def test_reserve_and_message_routing(self):
        cfg = small_graph();cfg['nodes'][1]['reserve'] = dict(cfg['nodes'][1]['primary'],model='reserve-model')
        attempt,root = self.prepare(cfg)
        _,impl = self.delegate(attempt,root,variant='reserve',reason='user choice')
        self.assertEqual(impl['requested']['model'],'reserve-model')
        _,tester = self.delegate(attempt,impl,'check')
        messages = [{'type':'question','payload':{'dispatchId':a['dispatch_id']}} for a in (root,impl,tester)]
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':root['terminal']}):
            routed = graph.messages(self.a.launcher,attempt,messages)
        self.assertEqual([m['payload']['dispatchId'] for m in routed],[root['dispatch_id'],impl['dispatch_id']])



class GraphReportAndHTTP(unittest.TestCase):
    setUp = fixtures.Platform.setUp
    attempt = fixtures.Platform.attempt
    prepare = GraphExecution.prepare
    delegate = GraphExecution.delegate
    def test_worker_report_rework_and_return_limit(self):
        from team_runner import worker_report
        attempt,root = self.prepare()
        _,impl = self.delegate(attempt,root)
        _,tester = self.delegate(attempt,impl,'check')
        # Exhaust the return edge, independently of the delegation edge.
        reg = self.a.launcher.registry(attempt);reg['edge_passes']['revise'] = 3;self.a.launcher.save_registry(attempt,reg)
        payload = {'outcome':'changes_requested','summary':'Fix it','return_edge':'revise'}
        env = {**self.a.context.environment(attempt), 'ORCA_TERMINAL_HANDLE':tester['terminal']}
        with patch.dict(os.environ,env), patch.object(worker_report,'Catalog',return_value=self.catalog), patch.object(worker_report,'call',return_value={}) as send:
            with self.assertRaisesRegex(ValueError,'limit exhausted'):
                worker_report.complete(payload,tester['terminal'],tester['task_id'],tester['dispatch_id'])
            send.assert_not_called()
            blocked = next(c for c in self.a.launcher.registry(attempt)['graph_calls'] if c['status']=='limit')
            graph.grant(self.a.launcher,attempt,blocked['id'],str(uuid.uuid4()))
            worker_report.complete(payload,tester['terminal'],tester['task_id'],tester['dispatch_id'])
            argv = send.call_args.args
            self.assertEqual(argv[argv.index('--outcome')+1],'failed')
            reg = self.a.launcher.registry(attempt)
            self.assertEqual(reg['edge_passes']['revise'],4)
            worker_report.complete(payload,tester['terminal'],tester['task_id'],tester['dispatch_id'])
            self.assertEqual(self.a.launcher.registry(attempt)['edge_passes']['revise'],4)
        before = read_json(attempt/'config.json')
        self.assertEqual(before['edges'][2]['max_passes'],3)

    def test_profile_http_and_workspace_config(self):
        import threading
        import urllib.request
        from team_runner.runner_ui import make_server
        server,token = make_server(self.catalog)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        def request(path,payload=None):
            headers = {'Authorization':'Bearer '+token,'Origin':origin,'Content-Type':'application/json'}
            req = urllib.request.Request(origin+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
            with urllib.request.urlopen(req) as response:return json.load(response)
        try:
            cfg = small_graph()
            scope = {k:self.a.context.identity()[k] for k in ('project_id','workspace_id')}
            prefix = '/api/projects/'+scope['project_id']+'/workspaces/'+scope['workspace_id']
            self.assertEqual(request('/api/team-profiles')['profiles'],[])
            library = request('/api/team-profiles',dict(action='create',name='HTTP team',config=cfg,scope=scope))
            self.assertEqual(library['profiles'][0]['config'],cfg)
            request(prefix+'/config',cfg)
            self.assertEqual(request(prefix+'/config')['graph'],cfg)
            request('/api/team-profiles',dict(action='delete',id=library['profiles'][0]['id'],scope=scope))
            self.assertEqual(request('/api/team-profiles')['profiles'],[])
        finally:server.shutdown();server.server_close();thread.join()

if __name__ == '__main__':unittest.main()
