'use strict';
let teamLibrary = [], teamProfileId = '', graphSelection = null;
let graphView = {x:0,y:0,w:1200,h:800}, graphConnect = null;
const svgNS = 'http://www.w3.org/2000/svg';
function graphId(prefix) {return prefix + '_' + crypto.randomUUID();}
function graphConfig(cfg) {
  if (cfg.version === 3) return cfg;
  const nodes = Object.entries(cfg.roles).map(([role, primary]) => ({id:role,name:role,role,instructions:'',primary:structuredClone(primary),reserve:structuredClone(cfg.reserves?.[role] || null),group:null}));
  return {version:3,root:'coordinator',nodes,edges:nodes.filter(n => n.id !== 'coordinator').map(n => ({id:'delegate_' + n.id,source:'coordinator',target:n.id,type:'delegation',label:'',instructions:'',max_passes:3})),groups:[],
    layout:Object.fromEntries(nodes.map((n,i) => [n.id,{x:i ? 420 + Math.floor((i-1)/6)*310 : 80,y:i ? 50 + (i-1)%6*140 : 260}])),auto_continue_after_replan:cfg.auto_continue_after_replan === true};
}
function graphDirty() {$('saved').textContent = 'Не сохранено';}
function svgNode(tag, attrs, parent, value) {
  const n = document.createElementNS(svgNS,tag);
  for (const [k,v] of Object.entries(attrs || {})) n.setAttribute(k,v);
  if (value !== undefined) n.textContent = value;
  if (parent) parent.append(n);
  return n;
}
function graphPoint(event) {
  const p = new DOMPoint(event.clientX,event.clientY);
  return p.matrixTransform($('team-graph').getScreenCTM().inverse());
}
function viewGraph() {$('team-graph').setAttribute('viewBox', `${graphView.x} ${graphView.y} ${graphView.w} ${graphView.h}`);}
function graphSelect(kind,id) {if ($('config-panel').disabled) return;graphSelection = {kind,id}; renderGraph(); renderInspector();}
function renderGraph() {
  const svg = $('team-graph'); svg.replaceChildren(); viewGraph();
  const defs = svgNode('defs',{},svg);
  for (const type of ['delegation','rework']) {
    const marker = svgNode('marker',{id:'arrow-'+type,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:16,markerHeight:16,markerUnits:'userSpaceOnUse',orient:'auto-start-reverse'},defs);
    svgNode('path',{d:'M 0 0 L 10 5 L 0 10 z',class:type},marker);
  }
  const hidden = new Map(), anchors = new Map();
  for (const g of settings.groups) {
    const members = settings.nodes.filter(n => n.group === g.id);
    if (!members.length) continue;
    const points = members.map(n => settings.layout[n.id]);
    const x = Math.min(...points.map(p => p.x))-20, y = Math.min(...points.map(p => p.y))-45;
    const w = g.collapsed ? 265 : Math.max(...points.map(p => p.x))+280-x;
    const h = g.collapsed ? 65 : Math.max(...points.map(p => p.y))+125-y;
    const group = svgNode('g',{'data-group':g.id,tabindex:0,role:'button','aria-label':'Группа '+g.name},svg);
    svgNode('rect',{x,y,width:w,height:h,rx:18,class:'graph-group'+(graphSelection?.id === g.id ? ' selected' : '')},group);
    svgNode('text',{x:x+12,y:y+27,class:'graph-group-title'},group,(g.collapsed ? '▸ ' : '▾ ')+g.name+' ('+members.length+')');
    group.onclick = e => {e.stopPropagation();graphSelect('group',g.id);};
    group.ondblclick = e => {e.stopPropagation();g.collapsed = !g.collapsed;graphDirty();renderGraph();};
    group.onkeydown = e => {if (e.key === 'Enter') graphSelect('group',g.id);};
    if (g.collapsed) for (const n of members) {hidden.set(n.id,g.id); anchors.set(n.id,{x,y,w,h});}
  }
  function anchor(id, output) {
    const a = anchors.get(id) || {...settings.layout[id],w:255,h:104};
    return {x:a.x+(output ? a.w : 0),y:a.y+a.h/2};
  }
  const edgeLayer = svgNode('g',{},svg);
  for (const e of settings.edges) {
    if (hidden.has(e.source) && hidden.get(e.source) === hidden.get(e.target)) continue;
    const a = anchor(e.source,true), b = anchor(e.target,false), bend = Math.max(65,Math.abs(b.x-a.x)*.4);
    const d = e.source === e.target ? `M ${a.x} ${a.y} C ${a.x+150} ${a.y-160}, ${b.x-150} ${b.y-160}, ${b.x} ${b.y}` : `M ${a.x} ${a.y} C ${a.x+bend} ${a.y}, ${b.x-bend} ${b.y}, ${b.x} ${b.y}`;
    const path = svgNode('path',{d,class:'graph-edge '+e.type+(graphSelection?.id === e.id ? ' selected' : ''),'marker-end':'url(#arrow-'+e.type+')',tabindex:0,role:'button','aria-label':'Связь '+(e.label || e.id),'data-edge':e.id},edgeLayer);
    svgNode('title',{},path,(e.label || e.type)+' · '+e.max_passes+' прохода');
    path.onclick = ev => {ev.stopPropagation();graphSelect('edge',e.id);};
    path.onkeydown = ev => {if (ev.key === 'Enter') graphSelect('edge',e.id);};
    if (e.label) svgNode('text',{x:(a.x+b.x)/2,y:(a.y+b.y)/2-10,class:'graph-edge-label'},edgeLayer,e.label);
  }
  for (const n of settings.nodes) {
    if (hidden.has(n.id)) continue;
    const p = settings.layout[n.id];
    const group = svgNode('g',{'data-node':n.id,transform:`translate(${p.x} ${p.y})`,tabindex:0,role:'button','aria-label':n.name+' · '+n.role},svg);
    svgNode('rect',{width:255,height:104,rx:22,class:'graph-bubble'+(graphSelection?.id === n.id ? ' selected' : '')+(n.id === settings.root ? ' root' : '')},group);
    svgNode('text',{x:18,y:26,class:'graph-name'},group,(n.id === settings.root ? '◆ ' : '')+n.name.slice(0,29));
    svgNode('text',{x:18,y:50},group,n.role);
    svgNode('text',{x:18,y:72},group,n.primary.agent+' · '+n.primary.model.slice(0,26));
    svgNode('text',{x:18,y:92},group,n.primary.effort+(n.primary.fast ? ' · Fast' : ''));
    for (const [side,x] of [['in',0],['out',255]]) {
      const port = svgNode('circle',{cx:x,cy:52,r:8,class:'graph-port'+(graphConnect === n.id && side === 'out' ? ' connecting' : ''),'data-port':side,tabindex:0,role:'button','aria-label':n.name+' '+(side === 'out' ? 'выход' : 'вход')},group);
      port.onclick = ev => {ev.stopPropagation();if (side === 'out') {graphConnect = n.id;renderGraph();} else graphLink(n.id);};
      port.onkeydown = ev => {if (ev.key === 'Enter') port.onclick(ev);};
    }
    group.onclick = ev => {if (!ev.target.closest('[data-port]')) graphSelect('node',n.id);};
    group.onkeydown = ev => {if (ev.key === 'Enter' && !ev.target.closest('[data-port]')) graphSelect('node',n.id);};
  }
}
function graphLink(target) {
  if ($('config-panel').disabled) return;
  if (!graphConnect) return;
  const e = {id:graphId('edge'),source:graphConnect,target,type:'delegation',label:'',instructions:'',max_passes:3};
  settings.edges.push(e); graphConnect = null; graphDirty(); graphSelect('edge',e.id);
}
function inspectorField(parent, title, value, apply, options, multiline=false) {
  const label = node('label',title,parent);
  const input = node(options ? 'select' : multiline ? 'textarea' : 'input',undefined,label);
  input.setAttribute('aria-label',title);
  if (options) for (const [id,name] of options) option(input,id,name);
  if (multiline) input.rows = 5;
  input.value = value ?? '';
  input.onchange = () => {apply(input.value);graphDirty();renderGraph();};
  return input;
}
function inspectorButton(parent,title,action) {const b=node('button',title,parent);b.onclick=handle(action);return b;}
function renderInspector() {
  const box=$('graph-fields');box.replaceChildren();$('roles').replaceChildren();
  if (!graphSelection) {node('p','Выберите агента, связь или группу.',box);return;}
  const {kind,id}=graphSelection;
  const item = settings[kind === 'node' ? 'nodes' : kind === 'edge' ? 'edges' : 'groups'].find(n => n.id === id);
  if (!item) {graphSelection=null;return;}
  node('h3',kind === 'node' ? 'Агент' : kind === 'edge' ? 'Связь' : 'Группа',box);
  node('small','ID: '+id,box);
  if (kind === 'node') {
    inspectorField(box,'Имя агента',item.name,v=>item.name=v);
    inspectorField(box,'Роль агента',item.role,v=>{item.role=v;renderInspector();},Object.keys(defaults.roles).filter(r=>id===settings.root ? r==='coordinator' : r!=='coordinator').map(r=>[r,r]));
    inspectorField(box,'Инструкции агента',item.instructions,v=>item.instructions=v,null,true);
    inspectorField(box,'Группа агента',item.group || '',v=>item.group=v || null,[['','Без группы'],...settings.groups.map(g=>[g.id,g.name])]);
    inspectorButton(box,'Копировать агента',()=>{
      const n=structuredClone(item);n.id=graphId('agent');n.name+=' — копия';
      if (n.role==='coordinator') n.role='planner';
      settings.nodes.push(n);settings.layout[n.id]={x:settings.layout[id].x+60,y:settings.layout[id].y+140};
      graphDirty();graphSelect('node',n.id);
    });
    const remove=inspectorButton(box,'Удалить агента',()=>{
      settings.nodes=settings.nodes.filter(n=>n.id!==id);settings.edges=settings.edges.filter(e=>e.source!==id && e.target!==id);delete settings.layout[id];
      graphSelection=null;graphDirty();renderSettings();
    });remove.disabled=id===settings.root;
    renderRole(id,item.primary,false);renderRole(id,item.reserve,true);
  } else if (kind === 'edge') {
    inspectorField(box,'Тип связи',item.type,v=>item.type=v,[['delegation','Делегирование'],['rework','Возврат на доработку']]);
    for (const [key,title] of [['source','От агента'],['target','К агенту']]) inspectorField(box,title,item[key],v=>item[key]=v,settings.nodes.map(n=>[n.id,n.name]));
    inspectorField(box,'Подпись связи',item.label,v=>item.label=v);
    inspectorField(box,'Инструкции передачи',item.instructions,v=>item.instructions=v,null,true);
    const limit=inspectorField(box,'Лимит проходов',item.max_passes,v=>item.max_passes=Number(v));limit.type='number';limit.min='1';limit.step='1';
    inspectorButton(box,'Удалить связь',()=>{settings.edges=settings.edges.filter(e=>e.id!==id);graphSelection=null;graphDirty();renderSettings();});
  } else {
    inspectorField(box,'Имя группы',item.name,v=>item.name=v);
    inspectorButton(box,item.collapsed ? 'Развернуть группу' : 'Свернуть группу',()=>{item.collapsed=!item.collapsed;graphDirty();renderSettings();});
    inspectorButton(box,'Удалить группу',()=>{for (const n of settings.nodes) if (n.group===id) n.group=null;settings.groups=settings.groups.filter(g=>g.id!==id);graphSelection=null;graphDirty();renderSettings();});
  }
}
function fitGraph() {
  if (!settings?.nodes.length) return;
  const points=Object.values(settings.layout),x=Math.min(...points.map(p=>p.x))-75,y=Math.min(...points.map(p=>p.y))-85;
  const w=Math.max(...points.map(p=>p.x))+335-x,h=Math.max(...points.map(p=>p.y))+180-y;
  graphView={x,y,w:Math.max(500,w),h:Math.max(400,h)};viewGraph();
}
function layoutGraph() {
  const levels=new Map([[settings.root,0]]),queue=[settings.root];
  for (let i=0;i<queue.length;i++) for (const e of settings.edges) if (e.type==='delegation' && e.source===queue[i] && !levels.has(e.target)) {levels.set(e.target,levels.get(e.source)+1);queue.push(e.target);}
  const counts=new Map();
  for (const n of settings.nodes) {const l=levels.get(n.id) ?? Math.max(1,...levels.values())+1,c=counts.get(l)||0;settings.layout[n.id]={x:80+l*370,y:70+c*155};counts.set(l,c+1);}
  graphDirty();renderGraph();fitGraph();
}
function renderLibrary() {
  const select=$('team-profile');select.replaceChildren();option(select,'','Черновик каталога');
  for (const p of teamLibrary) option(select,p.id,p.name);
  select.value=teamProfileId;
  for (const id of ['team-profile-save','team-profile-rename','team-profile-delete']) $(id).disabled=!teamProfileId;
}
async function libraryAction(action) {
  const target=selected,version=selectionVersion,parts=target.split('/');
  const data={action,scope:{project_id:parts[1],workspace_id:parts[3]}};
  if (action!=='create') data.id=teamProfileId;
  if (['create','rename'].includes(action)) data.name=$('team-profile-name').value;
  if (['create','save'].includes(action)) data.config=structuredClone(settings);
  const result=await api('team-profiles',data);
  if (selected!==target || selectionVersion!==version) return;
  teamLibrary=result.profiles;
  if (action==='create') teamProfileId=teamLibrary.find(p=>p.name===data.name.trim())?.id || '';
  if (action==='delete') teamProfileId='';
  renderLibrary();$('saved').textContent='Библиотека сохранена. Для каталога нажмите «Сохранить».';
}
function renderGraphLimits(v,target) {
  const calls=(v.resources?.graph_calls || []).filter(c=>c.status==='limit'),box=$('graph-limits');
  const signature=JSON.stringify([target,v.state?.current?.attempt_dir,calls]);
  if (box.dataset.signature===signature) return;
  box.dataset.signature=signature;box.replaceChildren();
  for (const call of calls) {
    const line=node('div',undefined,box);node('p','Лимит связи '+call.edge_id+' исчерпан.',line);
    const attempt=v.state.current.attempt_dir;
    inspectorButton(line,'Разрешить один проход',async()=>{await api('graph-pass',{attempt,call_id:call.id,request_id:crypto.randomUUID()},target);await refresh();});
  }
}
// Setup after app.js has installed its helpers and selected the initial workspace.
document.addEventListener('DOMContentLoaded',()=>{
  $('graph-add').onclick=handle(()=>{
    const id=graphId('agent'),role='explorer';
    settings.nodes.push({id,name:'Новый агент',role,instructions:'',primary:structuredClone(defaults.roles[role]),reserve:null,group:null});
    settings.layout[id]={x:graphView.x+graphView.w/2,y:graphView.y+graphView.h/2};graphDirty();graphSelect('node',id);
  });
  $('graph-group-add').onclick=handle(()=>{const g={id:graphId('group'),name:'Новая группа',collapsed:false};settings.groups.push(g);graphDirty();graphSelect('group',g.id);});
  $('graph-fit').onclick=fitGraph;$('graph-layout').onclick=layoutGraph;
  function zoom(factor,anchor={x:graphView.x+graphView.w/2,y:graphView.y+graphView.h/2}) {
    if (graphView.w*factor<150 || graphView.w*factor>100000) return;
    graphView.x=anchor.x+(graphView.x-anchor.x)*factor;graphView.y=anchor.y+(graphView.y-anchor.y)*factor;graphView.w*=factor;graphView.h*=factor;viewGraph();
  }
  $('graph-zoom-in').onclick=()=>zoom(.8);$('graph-zoom-out').onclick=()=>zoom(1.25);
  $('team-profile').onchange=handle(()=>{
    const p=teamLibrary.find(p=>p.id===$('team-profile').value);if (!p) {teamProfileId='';renderLibrary();return;}
    teamProfileId=p.id;settings=structuredClone(p.config);$('team-profile-name').value=p.name;graphSelection=null;graphDirty();renderSettings();fitGraph();
  });
  $('team-profile-new').onclick=handle(()=>{
    const root={id:graphId('agent'),name:'Координатор',role:'coordinator',instructions:'',primary:structuredClone(defaults.roles.coordinator),reserve:null,group:null};
    settings={version:3,root:root.id,nodes:[root],edges:[],groups:[],layout:{[root.id]:{x:120,y:150}},auto_continue_after_replan:false};
    teamProfileId='';graphSelection={kind:'node',id:root.id};graphDirty();renderSettings();fitGraph();
  });
  $('team-profile-copy').onclick=handle(()=>libraryAction('create'));
  $('team-profile-save').onclick=handle(()=>libraryAction('save'));
  $('team-profile-rename').onclick=handle(()=>libraryAction('rename'));
  $('team-profile-delete').onclick=handle(()=>libraryAction('delete'));
  const svg=$('team-graph');let drag=null;
  svg.addEventListener('wheel',e=>{if ($('config-panel').disabled) return;e.preventDefault();zoom(e.deltaY>0?1.12:1/1.12,graphPoint(e));},{passive:false});
  svg.addEventListener('pointerdown',e=>{
    if (e.button!==0 || $('config-panel').disabled) return;
    const bubble=e.target.closest('[data-node]'),port=e.target.closest('[data-port]');
    if (port) {if (port.dataset.port==='out') {graphConnect=bubble.dataset.node;drag={kind:'link',start:graphPoint(e),matrix:svg.getScreenCTM().inverse(),moved:false};}return;}
    if (e.target.closest('[data-edge],[data-group]')) return;
    const start=graphPoint(e);
    drag=bubble ? {kind:'node',id:bubble.dataset.node,start,matrix:svg.getScreenCTM().inverse(),position:{...settings.layout[bubble.dataset.node]}} : {kind:'pan',start,matrix:svg.getScreenCTM().inverse(),view:{...graphView}};
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener('pointermove',e=>{
    if (!drag) return;const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(drag.matrix),dx=p.x-drag.start.x,dy=p.y-drag.start.y;
    if (Math.abs(dx)+Math.abs(dy)>3) drag.moved=true;
    if (drag.kind==='node' && drag.moved) {settings.layout[drag.id]={x:drag.position.x+dx,y:drag.position.y+dy};graphDirty();renderGraph();}
    if (drag.kind==='pan') {graphView.x=drag.view.x+drag.start.x-p.x;graphView.y=drag.view.y+drag.start.y-p.y;viewGraph();}
  });
  svg.addEventListener('pointerup',e=>{
    if (drag?.kind==='link') {const hit=document.elementFromPoint(e.clientX,e.clientY)?.closest('[data-node]');if (hit && drag.moved) graphLink(hit.dataset.node);}
    const clickedNode=drag?.kind==='node' && !drag.moved ? drag.id : null;
    if (svg.hasPointerCapture(e.pointerId)) svg.releasePointerCapture(e.pointerId);drag=null;
    if (clickedNode) graphSelect('node',clickedNode);
  });
  svg.addEventListener('pointercancel',()=>{drag=null;graphConnect=null;});
  svg.addEventListener('keydown',e=>{if (e.key==='Escape') {graphConnect=null;renderGraph();}});
});
