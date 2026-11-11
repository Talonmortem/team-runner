"""UI contract decisions and request persistence without browser/provider effects."""
from pathlib import Path
import shutil
import subprocess
import unittest


class GoalUI(unittest.TestCase):
    def test_guarded_requests_and_malformed_observations(self):
        if not shutil.which('node'):
            self.skipTest('node unavailable')
        source = (Path(__file__).parents[1] / 'src/team_runner/runner-ui/app.js').read_text()
        source = source[:source.rindex('handle(async () => {')]
        harness = r'''
const assert = require('node:assert/strict');
const elements = new Map(), storage = new Map();
class Element {
  constructor() {this.textContent='';this.value='';this.checked=false;this.dataset={};this.hidden=false;this.disabled=false;this.isConnected=true;}
  querySelectorAll() {return [];}
  querySelector() {return new Element();}
  contains() {return false;}
  replaceChildren() {}
}
const document = {getElementById(id) {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},querySelectorAll(){return [];},activeElement:null};
const location={hash:''}, history={replaceState(){}}, sessionStorage={getItem(k){return storage.get(k)||null;},setItem(k,v){storage.set(k,v);}};
const crypto=require('node:crypto').webcrypto;
'''
        checks = r'''
(async()=>{
const target='projects/p/workspaces/w';
const view={schema_version:1,identity:{project_id:'p',workspace_id:'w',path:'/fixture',orca_id:'fixture',orca_instance:'instance'},revision:'a'.repeat(64),generation:null,epoch:null,goal:null,confirmed:true,observed_state:'paused',desired_state:'paused',current:{item:null,attempt:null},progress:{total:0,completed:0},available_actions:[{action:'start'},{action:'pause'},{action:'reply'}],final_acceptance:{status:'not_verified'},blocker:null};
assert.equal(validSummary(view,target),view);
for(const patch of [{schema_version:2},{confirmed:'true'},{identity:{...view.identity,workspace_id:'foreign'}},{progress:{total:1,completed:2}},{available_actions:[{action:'delete'}]},{final_acceptance:null},{epoch:0}]) assert.throws(()=>validSummary({...view,...patch},target));
selected=target;summaries.set(target,view);
currentDetail={confirmed:false};assert.equal(allowed('start'),true,'persisted summary allows cold execute while service offline');
renderActions=()=>{};renderRequests=()=>{};refresh=async()=>{};
let calls=[],resolve;
api=async(path,body,t)=>{calls.push({path,body:structuredClone(body),target:t});return new Promise(r=>resolve=r);};
const first=action('pause');
await action('pause');
assert.equal(calls.length,1,'double click sends one POST');
let stored=requestRecords(target)[0];
assert.equal(stored.status,'sending');assert.deepEqual(stored.body.scope,scope(target));
const original=structuredClone(stored.body);
summaries.set(target,{...view,revision:'b'.repeat(64),current:{item:'new',attempt:'new-attempt'}});
assert.deepEqual(requestRecords(target)[0].body,original,'polling cannot rewrite saved scope');
resolve({schema_version:1,request_id:stored.request_id,action:stored.action,status:'pending',scope:{identity:view.identity}});await first;
assert.equal(requestRecords(target)[0].status,'pending');
await assert.rejects(action('pause'),/проверьте результат/);
api=async(path,body)=>{assert.equal(body,undefined);assert.ok(path.startsWith('operations/'));return {schema_version:1,request_id:stored.request_id,action:stored.action,status:'succeeded',scope:{identity:view.identity}};};
await observeRequests(target);assert.equal(requestRecords(target)[0].status,'succeeded');
api=async()=>{throw new Error('connection lost');};
await assert.rejects(action('reply',{message_id:'q',text:'approved'}),/connection lost/);
const lost=requestRecords(target).find(r=>r.action==='reply');assert.equal(lost.status,'unknown');assert.equal(lost.body.payload.text,'approved');
await assert.rejects(action('reply',{message_id:'q',text:'changed'}),/проверьте результат/);
assert.equal(requestRecords(target).find(r=>r.action==='reply').request_id,lost.request_id);
const other='projects/p/workspaces/other';selected=other;summaries.set(other,{...view,identity:{...view.identity,workspace_id:'other'}});
assert.equal(requestRecords(other).length,0,'workspace journals isolated');
selected=target;summaries.set(target,view);$('goal').value='feat/example';$('mode').value='auto';$('final').checked=true;$('execute-recovery').checked=true;
let sent;
mutation=async(name,path,body,t)=>{sent={name,path,body,target:t};};
await execute(false);assert.deepEqual(sent.body.options,{goal:'feat/example',mode:'auto',final:true,recovery:true});
assert.equal(sent.body.generation,null);assert.equal(sent.body.epoch,null);
summaries.set(target,{...view,goal:'feat/example',generation:'12345678-1234-1234-1234-123456789abc',epoch:3});
await execute(true);assert.deepEqual(sent.body.options,{resume:true});assert.equal(sent.body.epoch,3);
summaries.set(target,{...view,confirmed:false});await assert.rejects(execute(),/недоступен/);
// Force restart uses the retry receipt's exact prepared scope and saved policy.
storage.clear();
const prepared={generation:'12345678-1234-1234-1234-123456789abc',epoch:5,attempt:'one',goal:'feat/example'};
const restartView={...view,goal:prepared.goal,generation:prepared.generation,epoch:4,current:{item:'one',attempt:'/old'},available_actions:[{action:'retry'}]};
const preparedView={...restartView,epoch:5,current:{item:'one',attempt:'one'},available_actions:[{action:'start'}]};
let restartCalls=[], retryStatus='succeeded', afterRetry=()=>{};
mutation=async(name,path,body,t)=>{restartCalls.push({name,path,body,t});afterRetry();return {status:retryStatus,result:{prepared}};};
api=async()=>preparedView;
summaries.set(target,restartView);
await forceRestart();
assert.deepEqual(restartCalls.map(c=>c.name),['retry','execute']);
assert.deepEqual(restartCalls[0].body.payload,{force:true,allow_extra_attempt:true});
assert.deepEqual(restartCalls[1].body.options,{resume:true});assert.equal(restartCalls[1].body.epoch,5);
for(const status of ['pending','unknown','failed']) {
  restartCalls=[];retryStatus=status;summaries.set(target,restartView);await forceRestart();
  assert.equal(restartCalls.length,1,'no resume after '+status);
}
retryStatus='succeeded';restartCalls=[];summaries.set(target,restartView);
api=async()=>({...preparedView,epoch:6});
await assert.rejects(forceRestart(),/Состояние изменилось/);assert.equal(restartCalls.length,1);
restartCalls=[];summaries.set(target,restartView);api=async()=>preparedView;
afterRetry=()=>{selectionVersion++;};await forceRestart();assert.equal(restartCalls.length,1,'selection change cancels continuation');
afterRetry=()=>{};
const broken='projects/p/workspaces/broken';storage.set(requestKey(broken),'bad-json');summaries.set(broken,view);
assert.equal(requestRecords(broken)[0].status,'unknown');assert.throws(()=>scope(broken),/Повреждён/);
assert.equal(storage.get(requestKey(broken)),'bad-json','corrupt journal is retained');
console.log('UI guards, cold defaults, scoped persistence, duplicate fencing, timeout-safe observation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', harness + source + checks], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    def test_question_and_escalation_controls_follow_current_permissions(self):
        if not shutil.which('node'):
            self.skipTest('node unavailable')
        source = (Path(__file__).parents[1] / 'src/team_runner/runner-ui/app.js').read_text()
        functions = source[source.index('function keepFocus('):source.index('function renderDetails(')]
        harness = r"""
const assert=require('node:assert/strict');
const elements=new Map(), summaries=new Map(),drafts=new Map();
let selected='projects/p/workspaces/w', enabled=true, effects=[];
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.attributes={};this.textContent='';this.value='';this.selectionStart=0;this.selectionEnd=0;}
 append(child){this.children.push(child);child.parent=this;}
 replaceChildren(){this.children=[];}
 contains(child){return this===child || this.children.some(n=>n.contains(child));}
 setAttribute(k,v){this.attributes[k]=v;}
 getAttribute(k){return k==='data-focus' ? this.dataset.focus : this.attributes[k];}
 querySelectorAll(selector){const all=this.children.flatMap(c=>[c,...c.querySelectorAll('*')]);return all.filter(n=>selector==='*'||selector==='[data-focus]'&&n.dataset.focus||n.tag===selector);}
 focus(){document.activeElement=this;}
 setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
}
const document={activeElement:null};
const $=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
function node(tag,text,parent){const n=new Element(tag);if(text!==undefined)n.textContent=text;parent?.append(n);return n;}
const same=target=>target===selected;
const allowed=(name,target)=>name==='reply'&&enabled&&same(target);
const scope=target=>{assert.equal(enabled,true,'unavailable replies must not build command scope');return {generation:'g',attempt:'a'};};
const handle=fn=>fn;
const action=(name,payload,target)=>{effects.push({name,payload,target});};
summaries.set(selected,{generation:'g',current:{attempt:'a'}});
"""
        checks = r"""
(async()=>{
const target=selected, escalation={id:'e',run_id:'child',type:'escalation',body:'Escalation body'},question={id:'q',run_id:'child',type:'question',body:'Question body'};
renderQuestions({messages:[escalation]},target);
assert.equal($('questions').querySelectorAll('input').length,0);
assert.equal($('questions').querySelectorAll('button').length,0);
assert.ok($('questions').querySelectorAll('p').some(n=>n.textContent==='Escalation body'));
renderQuestions({messages:[question,escalation]},target);
assert.equal($('questions').querySelectorAll('input').length,1);
assert.equal($('questions').querySelectorAll('button').length,1);
let input=$('questions').querySelectorAll('input')[0],button=$('questions').querySelectorAll('button')[0];
input.value='approved';input.oninput();input.focus();input.setSelectionRange(2,5);
renderQuestions({messages:[{...question,body:'Updated question body'},escalation]},target);
input=$('questions').querySelectorAll('input')[0];button=$('questions').querySelectorAll('button')[0];
assert.equal(input.value,'approved');assert.equal(document.activeElement,input);assert.deepEqual([input.selectionStart,input.selectionEnd],[2,5]);
await button.onclick();assert.deepEqual(effects[0],{name:'reply',payload:{message_id:'q',run_id:'child',text:'approved'},target});
// Same messages, changed backend actions: a stop or unknown effect removes reply controls.
enabled=false;
renderQuestions({messages:[{...question,body:'Updated question body'},escalation]},target);
assert.equal($('questions').querySelectorAll('input').length,0);
assert.equal($('questions').querySelectorAll('button').length,0);
assert.equal(document.activeElement,$('decision-heading'));
assert.ok($('questions').querySelectorAll('p').some(n=>n.textContent==='Escalation body'));
assert.throws(()=>button.onclick(),/Ответ больше не доступен/);assert.equal(effects.length,1);
enabled=true;
renderQuestions({messages:[{...question,body:'Updated question body'},escalation]},target);
assert.equal($('questions').querySelectorAll('input')[0].value,'approved','permission changes preserve the draft');
renderQuestions({messages:[{...question,answered:true},{...escalation,closed:true}]},target);
assert.equal($('questions').querySelectorAll('input').length,0);
console.log('Escalation is readable without reply; question permissions, focus, selection and draft follow polling');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', harness + functions + checks], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
