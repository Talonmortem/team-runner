"""Explicit fast choice survives CLI/application without touching old snapshots."""
import contextlib
import io
import json
import unittest
import uuid
import urllib.error
from unittest.mock import patch

import test_platform as fixtures
import test_template_ui_cli as template_fixtures
from team_runner import cli, application, goal_intent


class FastInterface(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_cli_omission_and_explicit_on_off(self):
        self.assertIsNone(cli.parser().parse_args(['run']).fast)
        self.assertTrue(cli.parser().parse_args(['run', '--fast']).fast)
        self.assertFalse(cli.parser().parse_args(['run', '--no-fast']).fast)
        for flag, expected in (('--fast', True), ('--no-fast', False)):
            with patch.object(cli, 'Catalog', return_value=self.catalog), \
                    patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(['run', flag, '--goal', application.goals(self.a)[0],
                                   '--project', self.a.context.project_id, '--workspace', self.a.context.workspace_id])
            self.assertEqual(result, 0)
            self.assertIs(start.call_args.args[1]['fast'], expected)

    def test_fast_type_and_intent_saved_policy(self):
        goal = application.goals(self.a)[0]
        with self.assertRaisesRegex(ValueError, 'fast must be boolean'):
            application.start_options(self.a, {'goal': goal, 'fast': 'yes'})
        with patch.object(self.a, 'load_config', return_value={}):
            receipt = goal_intent.authorize(self.a, {'goal': goal, 'fast': False}, str(uuid.uuid4()))
        value = goal_intent.read(self.a)
        self.assertIs(value['options']['fast'], False)
        paused = goal_intent.change(self.a, desired_state='paused')
        goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                              generation=receipt['generation'], epoch=paused['epoch'])
        self.assertIs(goal_intent.read(self.a)['options']['fast'], False)

    def test_global_capability_read_never_forwards_or_starts_service(self):
        import threading
        import urllib.request
        from team_runner import runner_ui
        from unittest.mock import Mock
        services = Mock()
        server, token = runner_ui.make_server(self.catalog, services=services)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        path = (f'/api/projects/{self.a.context.project_id}/workspaces/'
                f'{self.a.context.workspace_id}/config/capabilities')
        cfg = dict(agent='claude', provider='current', profile='', model='unsupported', effort='high')
        try:
            request = urllib.request.Request(base + path, data=json.dumps(cfg).encode(), headers={
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'Origin': base})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response), {'fast': False})
            self.assertEqual(services.mock_calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class FastCapabilitiesHTTP(template_fixtures.TemplateHTTP):
    def test_capability_resolves_profile_and_does_not_write(self):
        from test_goal_runner import runner
        from team_runner import runner_config
        profile = runner.repo / 'unsupported-settings.json'
        profile.write_text(json.dumps({'env': {'CLAUDE_CODE_USE_BEDROCK': '1', 'PRIVATE': 'secret'}}))
        cfg = dict(agent='claude', provider='current', profile=str(profile),
                   model='claude-opus-5-5', effort='high', fast=True)
        before = {str(p): p.read_bytes() for p in runner.state_dir.rglob('*') if p.is_file()}
        self.assertEqual(self.request('/api/config/capabilities', cfg), {'fast': False})
        cfg['profile'] = ''
        with patch.object(runner_config, 'connections', return_value={
                'fast_models': {'claude': ['claude-opus-5-5']}}):
            self.assertEqual(self.request('/api/config/capabilities', cfg), {'fast': True})
        after = {str(p): p.read_bytes() for p in runner.state_dir.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_saved_fast_can_be_removed_after_capability_disappears(self):
        from test_goal_runner import runner
        from team_runner import runner_config, runner_store
        cfg = runner.load_config()
        cfg['roles']['coordinator'] = dict(agent='claude', provider='current', profile='',
                                           model='claude-opus-5-5', effort='high', fast=True)
        target = runner.context.settings_dir / 'config.json'
        runner_store.atomic_json(target, cfg)
        before = target.read_bytes()
        catalog = runner_config.connections(repo=runner.repo,
                                             config_root=runner.context.config_root.parent)
        catalog['fast_models'] = {'claude': []}
        with patch.object(runner_config, 'connections', return_value=catalog):
            data = self.request('/api/config')
            self.assertIs(data['config']['roles']['coordinator']['fast'], True)
            self.assertIn('unsupported', data['projections']['coordinator']['error'])
            self.assertEqual(target.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                runner.load_config()
            with self.assertRaises(urllib.error.HTTPError):
                self.request('/api/config', data['config'])
            data['config']['roles']['coordinator'].pop('fast')
            saved = self.request('/api/config', data['config'])
            self.assertNotIn('fast', saved['roles']['coordinator'])
            self.assertNotIn('fast', runner.load_config()['roles']['coordinator'])
            runner_store.atomic_json(target, cfg)
            legacy = {'version': 1, 'roles': data['config']['roles']}
            saved = self.request('/api/config', legacy)
            self.assertNotIn('fast', saved['roles']['coordinator'])

    def test_capability_endpoint_requires_auth_and_origin(self):
        cfg = dict(agent='claude', provider='current', profile='',
                   model='claude-opus-5-5', effort='high')
        for token, origin in ((False, True), (True, False)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/config/capabilities', cfg, token, origin)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/config/capabilities', {'profile': '/etc/passwd'})
        self.assertEqual(error.exception.code, 400)


class FastDraftUI(unittest.TestCase):
    def test_unavailable_saved_choice_removable_and_stale_reply_ignored(self):
        import shutil
        import subprocess
        from pathlib import Path
        if not shutil.which('node'):
            self.skipTest('node unavailable')
        source = (Path(__file__).parents[1] / 'src/team_runner/runner-ui/app.js').read_text()
        functions = source[source.index('function capabilityKey('):source.index('function renderComparison(')]
        script = r'''
const assert = require('node:assert/strict');
let selected = 'workspace-a', selectionVersion = 1, pending = [], requests = 0;
const fastCapabilities = new Map(), saved = {};
function $(id) {return saved;}
function renderComparison() {}
function node(tag, text, parent) {
 const n = {tag, textContent:text, children:[], isConnected:true,
   append(child) {this.children.push(child);this.lastChild=child;},
   replaceChildren() {this.children=[];}, setAttribute() {}};
 if(parent) parent.append(n); return n;
}
function option(parent,value,label) {const n=node('option',label,parent);n.value=value;}
function api() {requests++;return new Promise(resolve=>pending.push(resolve));}
''' + functions + r'''
(async()=>{
 const cfg={agent:'claude',profile:'/unsupported',provider:'current',model:'claude-opus-5-5',effort:'high',fast:true};
 const cell=node('td');renderFastControl(cell,'tester',cfg,false);
 assert.equal(cfg.fast,true);assert.equal(cell.children[0].tag,'select');
 pending.shift()({fast:false});await new Promise(setImmediate);
 const select=cell.children[0];assert.equal(select.children.length,2);
 assert.equal(select.children[1].disabled,true);
 select.value='';select.onchange();assert.equal(Object.hasOwn(cfg,'fast'),false);
 assert.equal(cell.children[0].textContent,'Недоступно');
 renderFastControl(node('td'),'tester',cfg,false);assert.equal(requests,1);
 const other={...cfg,profile:''}, stale=node('td');renderFastControl(stale,'tester',other,false);
 selected='workspace-b';selectionVersion++;pending.shift()({fast:true});await new Promise(setImmediate);
 assert.equal(stale.children[0].textContent,'Проверка…');
 selected='workspace-a';selectionVersion=3;
 const changed=node('td');renderFastControl(changed,'tester',other,false);
 other.profile='/new';pending.shift()({fast:true});await new Promise(setImmediate);
 assert.equal(changed.children[0].textContent,'Проверка…');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
