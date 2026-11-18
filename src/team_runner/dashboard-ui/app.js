'use strict';

const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const svgNS = 'http://www.w3.org/2000/svg';
const number = new Intl.NumberFormat('ru', {maximumFractionDigits: 1});
const labels = {
  available: 'Доступно', unavailable: 'Недоступно', opened: 'Терминал открыт',
  working: 'Работает', waiting: 'Ожидает', unconfirmed: 'Не подтверждено',
  finished: 'Завершён', idle: 'Нет активности', running: 'Выполняется',
  paused: 'Пауза', pausing: 'Переход к паузе', stopped: 'Остановлено',
  stopping: 'Останавливается', starting: 'Запускается', recovering: 'Восстанавливается',
  needs_action: 'Требуется действие', completed: 'Завершено', failed: 'Ошибка', unknown: 'Неизвестно',
  active: 'Активно', cancelled: 'Отменено', pending: 'В очереди', succeeded: 'Выполнено',
  error: 'Ошибка', PASS: 'Пройдена', FAIL: 'Не пройдена', required: 'Требуется',
  not_applicable: 'Неприменима'
};
let projects = {}, base = '', selection = 0, state, agentSelection, nodeSelection, checkout;
let view = 'graph', inspector = 'agent', focusStatus = '';
let dialog = null, dialogId = null, dialogs = [], chatBusy = false, unsynced = null;
let usageRequest = 0, dialogRequest = 0, replyDraft = null;
const pendingState = new Set();
const orderKey = 'team-runner.sidebar-order';
let sidebarOrder = {projects: [], workspaces: {}};
let draggedRow = null;
try {
  const saved = JSON.parse(localStorage.getItem(orderKey));
  if (saved && Array.isArray(saved.projects) && saved.workspaces && typeof saved.workspaces === 'object') {
    sidebarOrder = saved;
  }
} catch (error) {
  // Storage may be unavailable in a private browser profile.
}

const collapsedProjects = new Set(Array.isArray(sidebarOrder.collapsed) ? sidebarOrder.collapsed : []);

function node(tag, value, parent, attrs = {}) {
  const element = document.createElement(tag);
  if (value != null) element.textContent = value;
  for (const [key, val] of Object.entries(attrs)) element.setAttribute(key, val);
  if (parent) parent.append(element);
  return element;
}

function svg(tag, parent, attrs, value) {
  const element = document.createElementNS(svgNS, tag);
  for (const [key, val] of Object.entries(attrs)) element.setAttribute(key, val);
  if (value != null) element.textContent = value;
  parent.append(element);
  return element;
}

function label(key) {
  return labels[key] || key || 'Неизвестно';
}

function hint(parent, value) {
  node('p', value, parent, {class: 'muted'});
}

function status(id, value) {
  const element = $(id);
  element.hidden = !value;
  element.textContent = value || '';
}

function cached(element, data, render) {
  const signature = JSON.stringify(data ?? null);
  if (element.dataset.content !== signature) {
    element.dataset.content = signature;
    render(element);
  }
}

async function api(path, data) {
  const headers = {Authorization: 'Bearer ' + token};
  const options = {headers, cache: 'no-store'};
  if (data !== undefined) {
    options.method = 'POST';
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch('/api' + path, options);
  } catch (error) {
    throw new Error('Связь с панелью прервана', {cause: error});
  }
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error?.message || value.error || 'HTTP ' + response.status);
    error.responseStatus = response.status;
    throw error;
  }
  return value;
}

function scope() {
  const {identity, generation, revision, current} = state.summary;
  return {identity, generation, revision, attempt: current.attempt};
}

function orderedIds(items, order) {
  const ids = Object.keys(items);
  const saved = Array.isArray(order) ? order.filter(id => Object.hasOwn(items, id)) : [];
  return [...new Set([...saved, ...ids])];
}

function saveSidebar() {
  try {
    localStorage.setItem(orderKey, JSON.stringify(sidebarOrder));
  } catch (error) {
    status('sidebar-status', 'Настройки списка сохранены до закрытия страницы');
  }
}

function setSidebarOrder(projectId, order, focus) {
  if (projectId) sidebarOrder.workspaces[projectId] = order;
  else sidebarOrder.projects = order;
  saveSidebar();
  renderProjects(focus);
}

function sortable(row, ids, index, projectId) {
  if (ids.length < 2) return;
  const id = ids[index];
  const grip = node('span', '⠿', null, {class: 'drag-grip', draggable: 'true',
    tabindex: '0', role: 'group', 'aria-label': 'Перемещение стрелками вверх и вниз',
    title: 'Перетащить или переместить стрелками ↑ ↓'});
  row.prepend(grip);
  grip.ondragstart = event => {
    draggedRow = {projectId, id};
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', id);
  };
  grip.ondragend = () => {
    draggedRow = null;
    clearDropMarker();
  };
  function acceptsDrop() {
    return draggedRow && draggedRow.projectId === projectId && draggedRow.id !== id;
  }
  function insertAfter(event) {
    const box = row.getBoundingClientRect();
    return event.clientY >= (box.top + box.bottom) / 2;
  }
  row.ondragover = event => {
    if (!acceptsDrop()) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    clearDropMarker();
    row.dataset.drop = insertAfter(event) ? 'after' : 'before';
  };
  row.ondragleave = event => {
    if (!row.contains(event.relatedTarget)) delete row.dataset.drop;
  };
  row.ondrop = event => {
    if (!acceptsDrop()) return;
    event.preventDefault();
    const moving = draggedRow.id;
    const order = ids.filter(item => item !== moving);
    order.splice(order.indexOf(id) + Number(insertAfter(event)), 0, moving);
    draggedRow = null;
    setSidebarOrder(projectId, order);
  };

  grip.onkeydown = event => {
    const step = {ArrowUp: -1, ArrowDown: 1}[event.key];
    if (!step) return;
    event.preventDefault();
    if (!ids[index + step]) return;
    const order = [...ids];
    [order[index], order[index + step]] = [order[index + step], order[index]];
    setSidebarOrder(projectId, order, id);
  };
}

function clearDropMarker() {
  for (const row of $('projects').querySelectorAll('[data-drop]')) delete row.dataset.drop;
}

function renderProjects(focus) {
  const list = $('projects');
  const projectIds = orderedIds(projects, sidebarOrder.projects);
  list.replaceChildren();
  for (const [index, projectId] of projectIds.entries()) {
    const project = projects[projectId];
    const section = node('section', null, list, {class: 'project', 'data-project': projectId});
    const heading = node('div', null, section, {class: 'sidebar-row', 'data-order-id': projectId});
    const name = node('h3', null, heading);
    const workspaceIds = orderedIds(project.workspaces, sidebarOrder.workspaces[projectId]);
    if (!workspaceIds.length) name.textContent = project.name;
    const group = node('div', null, section, {class: 'project-checkouts', id: 'checkouts-' + projectId});
    const selectedProject = checkout?.projectId === projectId;
    if (workspaceIds.length) {
      const toggle = node('button', null, name, {type: 'button', class: 'project-toggle',
        'aria-controls': group.id});
      node('span', '›', toggle, {class: 'project-chevron', 'aria-hidden': 'true'});
      node('span', project.name, toggle);
      const count = node('span', '', toggle, {class: 'project-count', 'aria-hidden': 'true'});
      function updateCollapse() {
        group.hidden = collapsedProjects.has(projectId);
        group.classList.toggle('has-selection', selectedProject);
        toggle.setAttribute('aria-expanded', !group.hidden);
        toggle.classList.toggle('contains-current', selectedProject && group.hidden);
        count.textContent = group.hidden ? workspaceIds.length : '';
      }
      toggle.onclick = () => {
        if (collapsedProjects.has(projectId)) collapsedProjects.delete(projectId);
        else collapsedProjects.add(projectId);
        sidebarOrder.collapsed = [...collapsedProjects];
        saveSidebar();
        updateCollapse();
      };
      updateCollapse();
    }
    sortable(heading, projectIds, index, null);
    for (const [index, workspaceId] of workspaceIds.entries()) {
      const workspace = project.workspaces[workspaceId];
      const row = node('div', null, group, {class: 'sidebar-row', 'data-order-id': workspaceId});
      const button = node('button', workspace.name + (workspace.disabled ? ' · отключён' : ''), row,
        {class: 'checkout', 'data-workspace': workspaceId, title: workspace.path,
          'aria-current': workspaceId === checkout?.workspaceId && projectId === checkout?.projectId});
      button.onclick = () => selectCheckout(projectId, workspaceId);
      sortable(row, workspaceIds, index, projectId);
    }
  }
  if (!projectIds.length) hint(list, 'Нет зарегистрированных проектов');
  if (focus) {
    const row = [...list.querySelectorAll('[data-order-id]')].find(row => row.dataset.orderId === focus);
    row?.querySelector('.drag-grip')?.focus({preventScroll: true});
  }
}

function selectCheckout(projectId, workspaceId) {
  checkout = {projectId, workspaceId};
  base = '/projects/' + encodeURIComponent(projectId) + '/workspaces/' + encodeURIComponent(workspaceId);
  ++selection;
  state = agentSelection = nodeSelection = undefined;
  dialog = dialogId = unsynced = null;
  dialogs = [];
  chatBusy = false;
  focusStatus = '';
  replyDraft = null;
  $('chat-text').value = '';
  status('state-error', '');
  status('chat-status', '');
  renderProjects();
  renderState();
  renderChat();
  void refresh();
  if (inspector === 'hoster') void refreshDialogs();
  if (view === 'usage') void refreshUsage();
}

function stateMark(parent, value) {
  const allowed = ['working', 'waiting', 'unconfirmed', 'finished', 'idle'];
  return node('span', label(value), parent, {class: 'state ' + (allowed.includes(value) ? value : 'unconfirmed')});
}

function selectNode(id, agentId) {
  nodeSelection = id;
  agentSelection = agentId;
  focusStatus = '';
  setInspector('agent');
  renderState();
  for (const element of $('graph-view').querySelectorAll('[data-node]')) {
    if (element.dataset.node === id) element.focus({preventScroll: true});
  }
}

function graphEdgeStates(graph, agents, calls) {
  const edges = new Map(graph.edges.map(edge => [edge.id, edge]));
  const roster = new Map(agents.map(agent => [agent.id, agent]));
  const states = new Map();
  for (const call of calls) {
    if (call.status !== 'running' || !call.dispatch_id) continue;
    const edge = edges.get(call.edge_id);
    const parent = roster.get(call.parent_agent);
    const child = roster.get(call.child_agent);
    if (!edge || !parent || !child || parent.node_id !== edge.source || child.node_id !== edge.target ||
        child.dispatch_id !== call.dispatch_id) continue;
    if (child.state === 'working' || (child.state === 'waiting' && !states.has(edge.id))) {
      states.set(edge.id, child.state);
    }
  }
  return states;
}

function renderGraph(parent, graph, agents, future, edgeStates = new Map(), previousStates = new Map()) {
  const width = 198, height = 76;
  const points = new Map(graph.nodes.map((entry, i) => [
    entry.id, graph.layout?.[entry.id] || {x: 255 * (i % 3), y: 115 * Math.floor(i / 3)}
  ]));
  if (!points.size) return;
  const pos = [...points.values()];
  const left = Math.min(...pos.map(p => p.x)) - 38;
  const top = Math.min(...pos.map(p => p.y)) - 52;
  const right = Math.max(...pos.map(p => p.x)) + width + 44;
  const bottom = Math.max(...pos.map(p => p.y)) + height + 38;
  const root = svg('svg', parent, {class: 'graph', viewBox: [left, top, right - left, bottom - top].join(' '),
    role: 'group', 'aria-label': future ? 'Граф будущего профиля' : 'Граф снимка попытки'});
  const defs = svg('defs', root, {});
  const arrowId = future ? 'future-arrow' : 'attempt-arrow';
  const marker = svg('marker', defs, {id: arrowId, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: 'auto'});
  svg('path', marker, {d: 'M0 0 L8 4 L0 8 z'});
  for (const activity of new Set(edgeStates.values())) {
    const activeMarker = svg('marker', defs, {id: arrowId + '-' + activity,
      class: activity, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: 'auto'});
    svg('path', activeMarker, {d: 'M0 0 L8 4 L0 8 z'});
  }
  for (const group of graph.groups || []) {
    const members = graph.nodes.filter(entry => entry.group === group.id).map(entry => points.get(entry.id));
    if (!members.length) continue;
    const x = Math.min(...members.map(p => p.x)) - 12, y = Math.min(...members.map(p => p.y)) - 25;
    svg('rect', root, {class: 'group-bounds', x, y, rx: 12,
      width: Math.max(...members.map(p => p.x)) + width + 12 - x,
      height: Math.max(...members.map(p => p.y)) + height + 12 - y});
    svg('text', root, {x: x + 10, y: y + 16, class: 'group-label'}, group.name);
  }
  for (const edge of graph.edges || []) {
    const a = points.get(edge.source), b = points.get(edge.target);
    if (!a || !b) continue;
    const x = a.x + width, y = a.y + height / 2, ex = b.x, ey = b.y + height / 2;
    const bend = Math.max(35, Math.abs(ex - x) / 2);
    const curve = edge.source === edge.target
      ? `M${x} ${y} C${x+60} ${y-95} ${ex-60} ${ey-95} ${ex} ${ey}`
      : `M${x} ${y} C${x+bend} ${y} ${ex-bend} ${ey} ${ex} ${ey}`;
    const activity = edgeStates.get(edge.id);
    const markerId = activity ? arrowId + '-' + activity : arrowId;
    const path = svg('path', root, {class: ['edge', edge.type, activity].filter(Boolean).join(' '), d: curve,
      'marker-end': 'url(#' + markerId + ')', 'data-edge': edge.id});
    const title = edge.label || edge.type;
    svg('title', path, {}, activity ? title + ' · ' + label(activity) : title);
    if (edge.label) svg('text', root, {x: (x + ex) / 2, y: (y + ey) / 2 - 6,
      class: 'edge-label', 'text-anchor': 'middle'}, edge.label);
  }
  for (const entry of graph.nodes) {
    const matches = agents.filter(agent => agent.node_id === entry.id);
    const current = ['working', 'waiting', 'unconfirmed', 'finished']
      .map(kind => matches.find(agent => agent.state === kind)).find(Boolean);
    const {x, y} = points.get(entry.id);
    const activity = current?.state || 'idle';
    const group = svg('g', root, {class: 'graph-node ' + activity, transform: `translate(${x} ${y})`,
      'data-node': entry.id, 'data-agent-state': activity});
    if (!future) {
      group.setAttribute('role', 'button');
      group.setAttribute('tabindex', '0');
      group.setAttribute('aria-label', entry.name + ' · ' + label(activity));
      group.setAttribute('aria-pressed', nodeSelection === entry.id);
      group.onclick = () => selectNode(entry.id, current?.id);
      group.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          group.onclick();
        }
      };
    }
    svg('title', group, {}, entry.name + ' · ' + (current ? label(activity) : entry.role));
    svg('rect', group, {width, height, rx: 11});
    if (previousStates.has(entry.id) && previousStates.get(entry.id) !== activity) {
      svg('rect', group, {class: 'state-flash', width, height, rx: 11, 'aria-hidden': 'true'});
    }
    svg('text', group, {class: 'title', x: 12, y: 24}, fit(entry.name, 23));
    svg('text', group, {class: 'meta', x: 12, y: 45}, fit(entry.role, 25));
    svg('circle', group, {cx: 16, cy: 60, r: 4,
      class: activity === 'working' ? 'working-spinner' : 'state-dot'});
    const detail = future ? [entry.primary?.agent, entry.primary?.model].filter(Boolean).join(' · ') : label(activity);
    svg('text', group, {class: 'meta', x: 25, y: 64}, fit(detail, 24));
  }
}

function fit(value, length) {
  return value.length > length ? value.slice(0, length - 1) + '…' : value;
}

function renderAttempt() {
  const graph = state?.graph, agents = state?.agents || [], legacy = state?.legacy_roles || [];
  const calls = state?.diagnostics?.waits?.graph_calls || [];
  const edgeStates = graph ? graphEdgeStates(graph, agents, calls) : new Map();
  const attempt = state?.summary?.current?.attempt || '';
  cached($('graph-view'), [attempt, graph, agents, legacy, [...edgeStates], nodeSelection, agentSelection], parent => {
    const previousStates = new Map();
    if (attempt && parent.dataset.attempt === attempt) {
      for (const node of parent.querySelectorAll('[data-node]')) {
        previousStates.set(node.dataset.node, node.dataset.agentState);
      }
    }
    parent.dataset.attempt = attempt;
    parent.replaceChildren();
    if (graph) {
      const legend = node('div', null, parent, {class: 'legend'});
      for (const group of ['working', 'waiting', 'unconfirmed', 'finished']) stateMark(legend, group);
      renderGraph(parent, graph, agents, false, edgeStates, previousStates);
      const orphans = agents.filter(agent => !graph.nodes.some(entry => entry.id === agent.node_id));
      if (orphans.length) {
        node('h3', 'Dispatch без узла в снимке', parent);
        renderList(parent, orphans.map(agent => ({role: agent.role, agent})));
      }
    } else if (legacy.length || agents.length) {
      node('h3', 'Роли попытки', parent);
      const items = agents.map(agent => ({role: agent.role, agent}));
      for (const role of legacy) if (!items.some(entry => entry.role === role)) items.push({role});
      renderList(parent, items);
    } else {
      hint(parent, 'Нет текущей попытки');
    }
  });
}

function renderList(parent, items) {
  const list = node('div', null, parent, {class: 'stack'});
  for (const {role, agent} of items) {
    const button = node('button', role, list, {'data-agent': agent?.id || '', 'aria-pressed': agent ? agent.id === agentSelection : role === nodeSelection});
    if (agent) {
      button.append(' · ');
      stateMark(button, agent.state);
    }
    button.onclick = () => selectNode(agent?.node_id || role, agent?.id);
  }
}

function fields(parent, entries) {
  const list = node('dl', null, parent);
  for (const [key, value] of entries) {
    if (value == null || value === '') continue;
    node('dt', key, list);
    node('dd', value, list);
  }
}

function renderAgent() {
  const agents = state?.agents || [];
  const entry = state?.graph?.nodes.find(item => item.id === nodeSelection);
  const related = entry ? agents.filter(agent => agent.node_id === entry.id) : agents.filter(agent => agent.id === agentSelection);
  const selected = related.find(agent => agent.id === agentSelection);
  cached($('agent'), [entry, related, selected, state?.enable_actions, nodeSelection, focusStatus], parent => {
    parent.replaceChildren();
    if (!nodeSelection && !selected) {
      hint(parent, 'Выберите узел или роль');
      return;
    }
    node('h2', entry?.name || selected?.role || nodeSelection, parent);
    if (entry) {
      fields(parent, [['Узел', entry.id], ['Роль', entry.role],
        ['Модель', [entry.primary?.agent, entry.primary?.model].filter(Boolean).join(' · ')]]);
    }
    if (!related.length) hint(parent, 'Dispatch ещё не зарегистрирован');
    for (const agent of [...related].reverse()) {
      const card = node('article', null, parent, {class: 'agent-card', 'data-dispatch': agent.dispatch_id || ''});
      stateMark(card, agent.state);
      fields(card, [['Активность', agent.activity], ['Статус', agent.status], ['Dispatch', agent.dispatch_id],
        ['Run', agent.run_id], ['Task', agent.task_id], ['Терминал', agent.terminal],
        ['Incarnation', agent.incarnation], ['Runtime', agent.observed?.agent || agent.requested?.agent],
        ['Модель', agent.observed?.model || agent.requested?.model]]);
      if (state?.enable_actions && agent.can_focus) {
        const button = node('button', 'Открыть терминал Orca', card, {type: 'button', 'data-focus': agent.id});
        button.onclick = () => openTerminal(button, agent);
      }
    }
    if (focusStatus) node('p', focusStatus, parent, {role: 'status'});
  });
}

async function openTerminal(button, agent) {
  const target = selection;
  const request = {scope: scope(), agent_id: agent.id, dispatch_id: agent.dispatch_id, terminal: agent.terminal};
  button.disabled = true;
  try {
    const result = await api(base + '/focus', request);
    if (target === selection) focusStatus = label(result.status);
  } catch (error) {
    if (target === selection) focusStatus = error.message;
  } finally {
    button.disabled = false;
    if (target === selection) renderAgent();
  }
}

function renderState() {
  const summary = state?.summary;
  $('workspace-name').textContent = checkout ? projects[checkout.projectId].workspaces[checkout.workspaceId].name : '';
  status('run-status', summary ? label(summary.observed_state) : '');
  $('goal').textContent = summary?.goal || (checkout ? 'Нет активной цели' : 'Выберите checkout');
  status('item', summary?.current?.item);
  $('progress-row').hidden = !summary;
  if (summary) {
    $('progress').value = summary.progress.completed;
    $('progress').max = Math.max(1, summary.progress.total);
    $('progress-count').textContent = summary.progress.completed + ' / ' + summary.progress.total;
  }
  const questions = pendingQuestions();
  status('blocker', questions.length && summary?.blocker?.code === 'answer_required'
    ? '' : summary?.blocker?.message);
  cached($('pending-questions'), questions, parent => {
    parent.replaceChildren();
    parent.hidden = !questions.length;
    for (const question of questions) {
      const card = node('article', null, parent, {class: 'notice'});
      node('h2', 'Нужно ваше решение · ' + label(question.role), card);
      node('p', question.text || 'Текст вопроса недоступен', card, {class: 'question-text'});
      if (question.key) {
        const open = node('button', 'Варианты ответа', card, {type: 'button'});
        open.onclick = () => {
          $('reply-target').value = question.key;
          setInspector('hoster');
          selectReplyTarget();
          $('reply-target').focus({preventScroll: true});
        };
      }
    }
  });
  renderAttempt();
  renderAgent();
  renderFuture();
  renderDiagnostics();
  renderQuestions();
}

function renderFuture() {
  cached($('future-view'), state?.future, parent => {
    parent.replaceChildren();
    const future = state?.future;
    if (future?.graph) renderGraph(parent, future.graph, [], true);
    if (future?.profile) {
      const profile = node('details', null, parent, {class: 'diagnostic'});
      node('summary', 'Настройки checkout', profile);
      node('pre', JSON.stringify(future.profile, null, 2), profile);
    }
    if (future?.error || (!future?.graph && !future?.profile)) hint(parent, future?.error || 'Профиль недоступен');
  });
}

function compactStatus(value) {
  if (typeof value === 'boolean') return value ? 'Доступно' : 'Недоступно';
  if (typeof value === 'string') return label(value);
  if (!value) return 'Неизвестно';
  if (value.available === false) return 'Недоступно';
  if (value.available === true) return 'Доступно';
  if (value.state === 'unavailable') return label('unavailable');
  if (value.configured === false) return 'Не настроен';
  if (value.paused) return label('paused');
  if (value.confirmed === false || value.process?.confirmed === false) return label('unconfirmed');
  return label(value.status || value.state || value.phase || value.process?.state);
}

function renderDiagnostics() {
  const facts = state?.diagnostics;
  const messages = state?.messages || [];
  cached($('diagnostics-view'), [facts, messages], parent => {
    const opened = new Set([...parent.querySelectorAll('details[open]')].map(e => e.id));
    parent.replaceChildren();
    if (!facts) return hint(parent, 'Диагностика недоступна');
    for (const [key, title, hasStatus] of [['orca', 'Orca', true], ['service', 'Сервис проекта', true],
      ['observer', 'Инфраструктурный observer', true], ['waits', 'Ожидания'], ['verification', 'Результаты проверок'],
      ['commands', 'История команд'], ['operations', 'Операции']]) {
      const value = facts[key];
      const detail = node('details', null, parent, {class: 'diagnostic', id: 'diagnostic-' + key});
      detail.open = opened.has(detail.id);
      const suffix = hasStatus ? ' · ' + compactStatus(value) : Array.isArray(value) ? ' · ' + value.length : '';
      node('summary', title + suffix, detail);
      if (key === 'waits' && messages.length) {
        for (const message of messages) {
          const notice = node('div', null, detail, {class: 'agent-card'});
          node('h3', message.type === 'question' ? 'Вопрос агента' : 'Эскалация', notice);
          node('p', message.text, notice);
          fields(notice, [['Dispatch', message.dispatch]]);
        }
      }
      if (key === 'verification') {
        for (const check of value?.checks || []) {
          fields(detail, [[label(check.status), check.command.join(' ')]]);
        }
      }
      if (value == null) hint(detail, 'Нет данных');
      else node('pre', JSON.stringify(value, null, 2), detail);
    }
  });
}

async function refresh() {
  if (!base || pendingState.has(selection)) return;
  const target = selection, path = base;
  pendingState.add(target);
  try {
    const result = await api(path + '/state');
    if (target !== selection) return;
    if (state && (state.summary.current.attempt !== result.summary.current.attempt ||
                  state.summary.generation !== result.summary.generation)) {
      agentSelection = nodeSelection = undefined;
      focusStatus = '';
      $('reply-target').value = '';
    }
    state = result;
    status('state-error', '');
    renderState();
    renderChat();
  } catch (error) {
    if (target === selection) {
      status('state-error', error.message);
    }
  } finally {
    pendingState.delete(target);
  }
}

function setView(name) {
  view = name;
  for (const button of document.querySelectorAll('[data-view]')) {
    const selected = button.dataset.view === name;
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
    $(button.dataset.view + '-view').hidden = !selected;
  }
  if (name === 'usage') void refreshUsage();
}

function setInspector(name) {
  inspector = name;
  for (const button of document.querySelectorAll('[data-inspector]')) {
    const selected = button.dataset.inspector === name;
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
    $(button.dataset.inspector).hidden = !selected;
  }
  if (name === 'hoster') void refreshDialogs();
}

function coverageText(value) {
  return `Измерено: ${number.format(value.measured || 0)} · Частично: ${number.format(value.partial || 0)} · Неизвестно: ${number.format(value.unknown || 0)}`;
}

function usageTable(parent, title, report, history) {
  node('h2', title, parent);
  if (!report) return hint(parent, 'Нет данных');
  if (report.coverage) hint(parent, coverageText(report.coverage));
  if (!report.rows?.length) return;
  const table = node('table', null, node('div', null, parent, {class: 'table-wrap'}));
  const head = node('tr', null, node('thead', null, table));
  const headings = history ? ['Проект', 'Checkout'] : [];
  for (const name of [...headings, 'Роль', 'Runtime', 'Вход', 'Выход', 'Кэш чтение', 'Кэш запись', 'Токенов/с', 'Покрытие']) {
    node('th', name, head, {scope: 'col'});
  }
  const known = node('tbody', null, table);
  for (const row of report.rows) {
    const tr = node('tr', null, known);
    if (history) {
      node('td', projects[row.project_id]?.name || row.project_id, tr);
      node('td', row.checkout, tr);
    }
    node('td', row.role, tr);
    node('td', row.runtime, tr);
    for (const field of ['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'tokens_per_second']) {
      node('td', row[field] == null ? '—' : number.format(row[field]), tr);
    }
    const cell = node('td', null, tr);
    const counts = coverageText(row.coverage || {});
    const reasons = Object.entries(row.unknown_reasons || {});
    if (reasons.length) {
      const details = node('details', null, cell);
      node('summary', counts, details);
      for (const [reason, count] of reasons) node('p', reason + ' · ' + count, details);
    } else cell.textContent = counts;
  }
}

async function refreshUsage() {
  const request = ++usageRequest;
  try {
    const result = await api('/usage');
    if (request !== usageRequest || view !== 'usage') return;
    const current = result.current?.[checkout?.projectId]?.[checkout?.workspaceId];
    const attempt = state?.summary.current.attempt;
    const period = $('period').value;
    cached($('usage-report'), [result, current, attempt, period], parent => {
      parent.replaceChildren();
      if (!result.collected_at) return hint(parent, result.status === 'unavailable' ? 'Статистика недоступна' : 'Сбор статистики');
      if (result.status === 'unavailable') hint(parent, 'Обновление статистики недоступно');
      hint(parent, 'Собрано ' + dateLabel(result.collected_at));
      const hasCurrent = attempt && current?.attempt === attempt;
      usageTable(parent, 'Текущая попытка', hasCurrent ? current : null, false);
      usageTable(parent, 'Последние ' + period + ' дней', result.history?.[period], true);
    });
  } catch (error) {
    if (request === usageRequest) {
      cached($('usage-report'), [error.message], parent => {
        parent.replaceChildren();
        hint(parent, error.message);
      });
    }
  }
}

function pendingQuestions() {
  const messages = (state?.messages || []).filter(message => message.type === 'question');
  return messages.map(message => ({
    ...message,
    key: message.id && messages.filter(row => row.id === message.id).length === 1 ? message.id : '',
    role: state.agents?.find(agent => agent.id === message.agent)?.role || 'Агент'
  }));
}

function suggestedReplies(text) {
  const flat = text.replace(/\s+/g, ' ').trim();
  const alternatives = flat.match(/(?:предпочтительный|первый) вариант:\s*(.+?)[;.]\s*(?:альтернативно|второй вариант:)\s*(.+?)(?=\.\s|\?$|$)/iu);
  const options = alternatives ? alternatives.slice(1).map((option, index) => ({
    label: (index + 1) + '. ' + option,
    text: 'Выбираю вариант: ' + option + '. Сначала перечисли точные изменения для моего согласования.'
  })) : [];
  options.push({label: 'Уточнить, что нужно согласовать',
    text: 'Уточни, какое решение от меня нужно. Дай варианты и последствия каждого, затем дождись моего выбора.'});
  return options;
}

function renderQuestions() {
  const questions = pendingQuestions().filter(question => question.key);
  const select = $('reply-target');
  cached(select, questions, parent => {
    const previous = parent.value;
    parent.replaceChildren();
    node('option', 'Чат с хостером', parent, {value: ''});
    for (const question of questions) {
      const value = question.text || question.dispatch || question.id;
      node('option', label(question.role) + ' · ' + fit(value, 70), parent, {value: question.key, title: value});
    }
    parent.value = questions.some(question => question.key === previous) ? previous : '';
  });
  if (replyDraft && !select.value && !$('chat-text').value.trim()) replyDraft = null;
  renderQuestionDetails();
}

function selectReplyTarget() {
  const key = $('reply-target').value;
  replyDraft = key && state ? {key, attempt: state.summary.current.attempt,
    generation: state.summary.generation} : null;
  renderQuestionDetails();
  renderChat();
}

function renderQuestionDetails() {
  const question = pendingQuestions().find(q => q.key && q.key === $('reply-target').value);
  cached($('reply-options'), [question, state?.enable_actions], parent => {
    parent.replaceChildren();
    parent.hidden = !question;
    if (!question) return;
    node('p', question.text, parent, {class: 'question-text'});
    hint(parent, state?.enable_actions ? 'Сообщение уйдёт агенту как ответ на этот вопрос.' :
      'Режим чтения. Черновик можно скопировать.');
    const options = suggestedReplies(question.text);
    options.push({label: 'Написать свой ответ', text: ''});
    for (const option of options) {
      const button = node('button', option.label, parent, {type: 'button'});
      button.onclick = () => {
        $('chat-text').value = option.text;
        selectReplyTarget();
        $('chat-text').focus();
      };
    }
  });
}

function dateLabel(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('ru');
}

function renderChat() {
  const select = $('dialogs');
  cached(select, [dialogs.map(({id, title}) => ({id, title})), dialogId], parent => {
    parent.replaceChildren();
    node('option', 'Новый диалог', parent, {value: ''});
    for (const row of dialogs) node('option', row.title || row.id, parent, {value: row.id});
    if (dialogId && !dialogs.some(row => row.id === dialogId)) {
      node('option', dialog?.title || 'Диалог', parent, {value: dialogId});
    }
    parent.value = dialogId || '';
  });
  const watch = dialog?.watch;
  status('watch', watch ? 'Наблюдение · ' + label(watch.status) : '');
  $('cancel-watch').hidden = watch?.status !== 'active';
  $('cancel-watch').disabled = chatBusy;
  $('send').disabled = !base || chatBusy || Boolean(replyDraft && !state?.enable_actions);
  $('send').textContent = replyDraft ? 'Ответить агенту' : 'Отправить';
  $('chat-text').placeholder = replyDraft ? 'Ответ на выбранный вопрос' : state?.enable_actions ? 'Отчёт, понаблюдай, пауза, продолжи или Ответь: …' : 'Покажи отчёт или понаблюдай';
  $('new-dialog').disabled = !base || chatBusy;
  select.disabled = !base || chatBusy;
  $('reply-target').disabled = !base || chatBusy;
  cached($('messages'), dialog?.messages, parent => {
    const atEnd = parent.scrollTop + parent.clientHeight >= parent.scrollHeight - 24;
    parent.replaceChildren();
    for (const message of dialog?.messages || []) {
      const card = node('article', null, parent, {class: 'message ' + (message.role === 'user' ? 'user' : 'assistant')});
      const sender = message.role === 'user' ? 'Вы' : message.kind === 'watch' ? 'Наблюдение' : 'Хостер';
      node('small', sender + (message.at ? ' · ' + dateLabel(message.at) : ''), card, {class: 'muted'});
      node('p', message.text, card);
      if (message.operation) {
        const op = message.operation;
        fields(card, [['Запрос', op.request_id], ['Действие', op.action], ['Статус', label(op.status)],
          ['Причина', op.error?.message || (typeof op.error === 'string' ? op.error : '')]]);
      }
    }
    if (atEnd) parent.scrollTop = parent.scrollHeight;
  });
}

async function refreshDialogs() {
  if (!base || chatBusy) return;
  const request = ++dialogRequest, target = selection, path = base + '/dialogs', id = dialogId;
  try {
    const data = await api(path);
    if (target !== selection || request !== dialogRequest || id !== dialogId || chatBusy) return;
    dialogs = Array.isArray(data) ? data : data.dialogs;
    if (id) {
      const value = await api(path + '/' + id);
      if (target !== selection || request !== dialogRequest || id !== dialogId || chatBusy) return;
      dialog = value;
    }
    if (unsynced && dialog?.messages?.some(message =>
        message.operation?.request_id === unsynced || message.request_id === unsynced)) {
      unsynced = null;
      status('chat-status', '');
    }
    renderChat();
  } catch (error) {
    if (target === selection && request === dialogRequest && !unsynced) status('chat-status', error.message);
  }
}

function chooseDialog(id) {
  ++dialogRequest;
  dialogId = id || null;
  dialog = unsynced = replyDraft = null;
  $('reply-target').value = '';
  renderQuestionDetails();
  $('chat-text').value = '';
  status('chat-status', '');
  renderChat();
  void refreshDialogs();
}

async function sendChat(event) {
  event.preventDefault();
  const draft = $('chat-text').value.trim();
  if (!draft || !base || chatBusy) return;
  if (replyDraft && (!state?.enable_actions || !state.summary.confirmed ||
      replyDraft.attempt !== state.summary.current.attempt ||
      replyDraft.generation !== state.summary.generation ||
      replyDraft.key !== $('reply-target').value ||
      !pendingQuestions().some(q => q.key === replyDraft.key))) {
    status('chat-status', 'Ответ привязан к прежнему вопросу. Выберите актуальный вопрос и подготовьте новый ответ.');
    return;
  }
  const text = replyDraft && !/^(?:ответь(?: агенту)?\s*:|\/reply\s+)/iu.test(draft)
    ? '/reply ' + draft : draft;
  if (text.length > 32000) {
    status('chat-status', 'Сократите ответ до 32 000 символов.');
    return;
  }
  const target = selection;
  const id = dialogId || crypto.randomUUID();
  const requestId = crypto.randomUUID();
  const request = {text, request_id: requestId};
  if (state) request.scope = scope();
  if ($('reply-target').value) request.message_id = $('reply-target').value;
  const path = base + '/dialogs/' + id;
  dialogId = id;
  chatBusy = true;
  $('chat-text').value = '';
  status('chat-status', '');
  renderChat();
  try {
    const value = await api(path, request);
    if (target === selection) {
      dialog = value;
      unsynced = null;
    }
  } catch (error) {
    if (target === selection) {
      unsynced = !error.responseStatus || error.responseStatus >= 500 ? requestId : null;
      status('chat-status', unsynced ? 'Исход запроса неизвестен · ' + unsynced : error.message);
      if (!unsynced && !dialog) dialogId = null;
    }
  } finally {
    if (target === selection) {
      chatBusy = false;
      renderChat();
      void refreshDialogs();
      void refresh();
    }
  }
}

async function cancelWatch() {
  if (!dialogId || chatBusy) return;
  const target = selection;
  chatBusy = true;
  renderChat();
  try {
    const result = await api(base + '/dialogs/' + dialogId + '/cancel', {});
    if (target === selection) dialog = result;
  } catch (error) {
    if (target === selection) status('chat-status', error.message);
  } finally {
    if (target === selection) {
      chatBusy = false;
      renderChat();
      void refreshDialogs();
    }
  }
}

async function init() {
  renderState();
  renderChat();
  try {
    const result = await api('/projects');
    projects = result.projects;
    status('mode', result.enable_actions ? 'Действия включены' : 'Чтение');
    const firstProject = orderedIds(projects, sidebarOrder.projects)
      .find(id => Object.keys(projects[id].workspaces).length);
    if (firstProject) {
      const [firstWorkspace] = orderedIds(projects[firstProject].workspaces, sidebarOrder.workspaces[firstProject]);
      selectCheckout(firstProject, firstWorkspace);
    }
    else renderProjects();
  } catch (error) {
    status('state-error', error.message);
  }
}

for (const button of document.querySelectorAll('[data-view]')) button.onclick = () => setView(button.dataset.view);
for (const button of document.querySelectorAll('[data-inspector]')) button.onclick = () => setInspector(button.dataset.inspector);
$('period').onchange = refreshUsage;
$('dialogs').onchange = event => chooseDialog(event.target.value);
$('new-dialog').onclick = () => chooseDialog('');
$('reply-target').onchange = selectReplyTarget;
$('chat').onsubmit = sendChat;
$('cancel-watch').onclick = cancelWatch;
void init();
setInterval(refresh, 2600);
setInterval(() => {if (inspector === 'hoster') void refreshDialogs();}, 3500);
setInterval(() => {if (view === 'usage') void refreshUsage();}, 10000);
