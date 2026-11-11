'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.hash.slice(1));
const token = params.get('token') || sessionStorage.getItem('runner-token') || '';
if (token) sessionStorage.setItem('runner-token', token);
history.replaceState(null, '', '/');
let settings, defaults, catalog, templates = [], comparison = {}, selected = '', selectionVersion = 0, projects = {}, loadedTarget = '';
function same(target) {return target === selected;}
const drafts = new Map();
const fastCapabilities = new Map();
async function api(path, data, target = selected) {
  const global = path.startsWith('projects') || path === 'services';
  const address = global ? '/api/' + path : '/api/' + target + '/' + path;
  if (!target && !global) throw new Error('Выберите каталог');
  const response = await fetch(address, {method: data === undefined ? 'GET' : 'POST',
    headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok || result.error && !result.status) {
    const error = new Error(result.error?.message || result.error || response.statusText);
    error.confirmedRejection = response.status >= 400 && response.status < 500 && result.schema_version === 1;
    throw error;
  }
  return result;
}
function node(tag, text, parent) {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (parent) parent.append(n); return n;}
function option(select, value, label = value) {const o = node('option', label, select); o.value = value;}
function datalist(id) {
  let list = $(id);
  if (!list) {list = node('datalist', undefined, document.body); list.id = id;}
  return list;
}
function renderSettings() {
  $('auto-continue-after-replan').checked = settings.auto_continue_after_replan === true;
  $('template').value = settings.template || 'current';
  $('roles').replaceChildren();
  for (const role of Object.keys(settings.roles)) {
    renderRole(role, settings.roles[role], false);
    renderRole(role, settings.reserves?.[role], true);
  }
  renderComparison();
}
function renderRole(role, cfg, reserve) {
  const tr = node('tr', undefined, $('roles')), label = node('td', role + (reserve ? ' · резерв' : ''), tr);
  if (reserve) {
    const enabled = node('input', undefined, label); enabled.type = 'checkbox'; enabled.checked = !!cfg;
    enabled.setAttribute('aria-label', role + ' reserve enabled');
    enabled.onchange = () => {
      settings.reserves ||= {};
      settings.reserves[role] = enabled.checked ? structuredClone(settings.roles[role]) : null;
      $('saved').textContent = 'Не сохранено'; renderSettings();
    };
  }
  if (!cfg) {const td = node('td', 'Не задан', tr); td.colSpan = 6; return;}
  for (const key of ['agent', 'profile', 'provider', 'model', 'effort']) {
    const td = node('td', undefined, tr);
    const input = node(key === 'agent' || key === 'effort' ? 'select' : 'input', undefined, td);
    input.setAttribute('aria-label', role + (reserve ? ' reserve' : '') + ' ' + key);
    if (key === 'agent') (catalog.runtimes || ['claude', 'codex']).forEach(v => option(input, v));
    if (key === 'effort') (catalog.efforts[cfg.agent] || []).forEach(v => option(input, v, v === 'default' ? 'по умолчанию' : v));
    if (key === 'profile' && cfg.agent === 'codex') input.setAttribute('list', 'profiles');
    if ((key === 'profile' || key === 'provider') && cfg.agent === 'opencode2') input.readOnly = true;
    if (key === 'provider' && cfg.agent !== 'opencode2') input.setAttribute('list', 'providers');
    if (key === 'model') input.setAttribute('list', cfg.agent === 'opencode2' ? 'opencode2-models' : 'models');
    input.value = cfg[key];
    input.onchange = () => {
      cfg[key] = input.value;
      if (key === 'agent') {
        delete cfg.fast;
        Object.assign(cfg, cfg.agent === 'opencode2' ? {profile: '', provider: 'cloudru',
          model: catalog.opencode2?.default_model || catalog.opencode2?.models?.[0] || '', effort: 'high'} :
          cfg.agent === 'claude' ? {profile: '', provider: 'current', model: 'claude-opus-5-5', effort: 'high'} :
          {profile: '', provider: catalog.current_provider || '', model: defaults.roles[role].agent === 'codex' ? defaults.roles[role].model : 'gpt-6.1-sol',
            effort: defaults.roles[role].agent === 'codex' ? defaults.roles[role].effort : 'high'});
        renderSettings();
      }
      if (key === 'profile' && cfg.agent === 'codex') {cfg.provider = catalog.profiles[cfg.profile]?.provider || ''; renderSettings();}
      if (['model', 'provider', 'profile'].includes(key)) {
        renderSettings();
      }
      $('saved').textContent = 'Не сохранено'; renderComparison();
    };
  }
  const speedCell = node('td', undefined, tr);
  renderFastControl(speedCell, role, cfg, reserve);
}
function capabilityKey(cfg) {
  return JSON.stringify(['agent', 'profile', 'provider', 'model', 'effort'].map(key => cfg[key]));
}
function renderFastControl(cell, role, cfg, reserve) {
  const target = selected, version = selectionVersion, key = capabilityKey(cfg);
  const cacheKey = JSON.stringify([target, version, key]);
  let entry = fastCapabilities.get(cacheKey);
  if (!entry) {
    entry = {supported: null}; fastCapabilities.set(cacheKey, entry);
    entry.promise = api('config/capabilities', structuredClone(cfg), target).then(value => {
      entry.supported = value.fast === true;
    }).catch(() => {entry.supported = false;});
  }
  const draw = () => {
    cell.replaceChildren();
    const supported = entry.supported === true;
    if (supported || Object.hasOwn(cfg, 'fast')) {
      const speed = node('select', undefined, cell);
      speed.setAttribute('aria-label', role + (reserve ? ' reserve' : '') + ' fast');
      option(speed, '', 'Наследовать');
      if (supported) {option(speed, 'on', 'Вкл.'); option(speed, 'off', 'Выкл.');}
      else {
        option(speed, cfg.fast ? 'on' : 'off', (cfg.fast ? 'Вкл.' : 'Выкл.') + ' · недоступно');
        speed.lastChild.disabled = true;
      }
      speed.value = cfg.fast === true ? 'on' : cfg.fast === false ? 'off' : '';
      speed.onchange = () => {
        if (speed.value === '') delete cfg.fast; else cfg.fast = speed.value === 'on';
        $('saved').textContent = 'Не сохранено'; renderComparison(); draw();
      };
    } else node('span', entry.supported === null ? 'Проверка…' : 'Недоступно', cell);
  };
  draw();
  if (entry.supported === null) entry.promise.then(() => {
    if (selected === target && selectionVersion === version && capabilityKey(cfg) === key && cell.isConnected) draw();
  });
}
function renderComparison() {
  if ($('comparison').contains(document.activeElement)) return;
  const signature = JSON.stringify([comparison,settings]);
  if ($('comparison').dataset.signature === signature) return;
  $('comparison').dataset.signature = signature;
  const expanded = new Set([...$('comparison').querySelectorAll('details[open] summary')].map(n => n.textContent));
  $('comparison').replaceChildren();
  for (const [role, p] of Object.entries(comparison)) {
    const box = node('details', undefined, $('comparison')); node('summary', role, box); box.open = expanded.has(role);
    if (p.error) node('p', 'Следующий запуск недоступен: ' + p.error, box);
    const table = node('table', undefined, box), head = node('tr', undefined, table);
    ['Параметр', 'Черновик', 'Сохранено', 'Следующий пункт', 'Источник', 'Снимок', 'Факт'].forEach(t => node('th', t, head));
    for (const reserve of [false, true]) for (const key of ['agent','profile','provider','model','effort','fast']) {
      const draft = (reserve ? settings?.reserves?.[role] : settings?.roles?.[role])?.[key];
      const saved = (reserve ? p.reserve_saved : p.saved)?.[key];
      const next = (reserve ? p.reserve_next : p.next)?.[key];
      const snapshot = (reserve ? p.reserve_snapshot : p.snapshot)?.[key];
      const fact = p.facts?.filter(f => (f.variant || 'primary') === (reserve ? 'reserve' : 'primary')).at(-1)?.values[key];
      const row = node('tr', undefined, table);
      [key + (reserve ? ' · резерв' : ''), draft ?? 'не задан', saved ?? 'не задан', next ?? 'не задан',
        reserve ? 'сохранённый резерв' : p.sources[key], snapshot ?? 'неизвестно', fact ?? 'неизвестно'].forEach(t => node('td', t, row));
      if (draft !== saved || saved !== next || snapshot !== next || (fact != null && fact !== snapshot)) row.className = 'different';
    }
  }
}
function text(id, value) {if ($(id).textContent !== value) $(id).textContent = value;}
const storageFaults = new Set();
const summaries = new Map(), workspaceDrafts = new Map(), refreshing = new Set(), sending = new Set();
const actionNames = new Set(['start', 'pause', 'stop', 'retry', 'reply', 'resolve', 'reconcile', 'continue-operation', 'service-reconcile']);
const states = {idle:'Не запущена', starting:'Запускается', running:'Выполняется', pausing:'Пауза после пункта', paused:'Приостановлена', stopping:'Останавливается', stopped:'Остановлена', needs_action:'Нужно решение', unconfirmed:'Не подтверждено', recovering:'Восстанавливается', waiting:'Ожидает проверки', failed:'Ошибка', unknown:'Исход неизвестен', completed:'Финальная приёмка пройдена', queue_completed:'Очередь выполнена'};
function validSummary(value, target) {
  const parts = target.split('/'), p = value?.progress, c = value?.current, identity = value?.identity;
  const expected = projects[parts[1]]?.workspaces?.[parts[3]];
  const valid = (!expected || ['path','orca_id','orca_instance'].every(k => identity?.[k] === expected[k])) && value?.schema_version === 1 && identity && identity.project_id === parts[1] && identity.workspace_id === parts[3]
    && ['path', 'orca_id', 'orca_instance'].every(k => typeof identity[k] === 'string' && identity[k])
    && typeof value.revision === 'string' && /^[a-f0-9]{64}$/.test(value.revision)
    && (value.generation === null || typeof value.generation === 'string' && /^[a-f0-9-]{36}$/.test(value.generation))
    && (value.epoch === null || Number.isInteger(value.epoch) && value.epoch >= 1)
    && typeof value.confirmed === 'boolean' && Object.hasOwn(states, value.observed_state)
    && ['running','paused','stopped'].includes(value.desired_state)
    && (value.goal === null || typeof value.goal === 'string') && c && (c.attempt === null || typeof c.attempt === 'string')
    && (c.item === null || typeof c.item === 'string') && p && Number.isInteger(p.total) && p.total >= 0
    && Number.isInteger(p.completed) && p.completed >= 0 && p.completed <= p.total
    && Array.isArray(value.available_actions) && value.available_actions.every(a => a && actionNames.has(a.action))
    && value.final_acceptance && ['not_verified','legacy_unverified','failed','accepted','not_required'].includes(value.final_acceptance.status)
    && (value.blocker === null || value.blocker && typeof value.blocker.code === 'string' && typeof value.blocker.message === 'string');
  if (!valid) throw new Error('Некорректный ответ состояния. Управление недоступно; откройте диагностику.');
  return value;
}
function requestKey(target) {return 'team-runner-requests:' + target;}
function requestRecords(target) {
  try {
    const records = JSON.parse(sessionStorage.getItem(requestKey(target)) || '[]');
    if (!Array.isArray(records) || records.some(r => !r || r.target !== target || typeof r.request_id !== 'string' || !r.body)) throw new Error();
    return records;
  } catch (_) {
    storageFaults.add(target);
    return [{target,action:'invalid-journal',request_id:'неизвестен',body:{},status:'unknown',error:'Повреждён сохранённый журнал запросов. Автоматический повтор запрещён.'}];
  }
}
function saveRequest(record) {
  const records = requestRecords(record.target).filter(r => r.request_id !== record.request_id);
  if (storageFaults.has(record.target)) throw new Error('Повреждён журнал запросов; отправка запрещена.');
  // Retain unresolved requests even when many later commands have completed.
  records.push(record);
  const terminal = records.filter(r => ['succeeded','failed','rejected'].includes(r.status)).slice(-20);
  sessionStorage.setItem(requestKey(record.target), JSON.stringify([...records.filter(r => !['succeeded','failed','rejected'].includes(r.status)), ...terminal]));
}
function unresolved(target, name) {
  return requestRecords(target).find(r => r.action === name && !['succeeded','failed','rejected'].includes(r.status));
}
function allowed(name, target = selected) {
  const summary = summaries.get(target);
  return !!(summary?.confirmed && summary.available_actions.some(a => a.action === name));
}
function scope(target) {
  const v = summaries.get(target);
  requestRecords(target);
  if (storageFaults.has(target)) throw new Error('Повреждён журнал запросов; отправка запрещена.');
  if (!v?.confirmed) throw new Error('Сначала получите подтверждённое состояние.');
  return {identity: structuredClone(v.identity), generation: v.generation, attempt: v.current.attempt, revision: v.revision};
}
function receipt(record, result) {
  if (!result || result.schema_version !== 1 || result.request_id !== record.request_id || result.action !== record.action || !['pending','running','succeeded','failed','unknown'].includes(result.status)) throw new Error('Некорректная квитанция операции');
  const parts = record.target.split('/'), identity = result.scope?.identity;
  if (!identity || identity.project_id !== parts[1] || identity.workspace_id !== parts[3]) throw new Error('Квитанция другого каталога');
  record.status = result.status; record.receipt = result;
  if (result.error) record.error = result.error.message || String(result.error);
  else delete record.error;
  saveRequest(record);
}
function renderRequests(target = selected) {
  if (!same(target)) return;
  const records = requestRecords(target), pending = records.filter(r => !['succeeded','failed','rejected'].includes(r.status));
  const latest = pending.at(-1) || records.at(-1);
  $('pending').hidden = !latest;
  text('pending', latest ? `${latest.action}: ${{sending:'Отправляется',pending:'Принят, ожидает выполнения',running:'Выполняется',succeeded:'Операция подтверждена',failed:'Ошибка операции',rejected:'Запрос отклонён',unknown:'Исход не подтверждён — повтор не отправлен'}[latest.status] || latest.status}${latest.error ? ' · ' + latest.error : ''}` : '');
  $('request-log').textContent = JSON.stringify(records.map(r => ({request_id:r.request_id,action:r.action,status:r.status,scope:r.body.scope || {generation:r.body.generation,epoch:r.body.epoch},error:r.error})),null,2);
  $('operation-check').hidden = !pending.length;
  $('operation-check').disabled = [...sending].some(k => k.startsWith(target + ':'));
}
async function observeRequests(target = selected) {
  requestRecords(target);
  if (storageFaults.has(target)) return;
  for (const record of requestRecords(target)) {
    if (['succeeded','failed','rejected'].includes(record.status) || sending.has(target + ':' + record.action)) continue;
    try {
      const result = await api('operations/' + record.request_id, undefined, target);
      receipt(record,result);
      if (same(target) && record.action === 'configure' && result.status === 'succeeded' && result.result?.roles) {
        settings = result.result; renderSettings(); $('saved').textContent = 'Сохранено';
      }
    }
    catch (e) {record.status = 'unknown'; record.error = e.message; saveRequest(record);}
  }
  if (same(target)) {renderRequests(target); renderActions(target);}
}
async function mutation(name, path, body, target) {
  const key = target + ':' + name;
  if (sending.has(key)) return;
  if (unresolved(target, name)) throw new Error('Сначала проверьте результат сохранённого запроса. Новый запрос не отправлен.');
  const record = {target, action:name, path, request_id:body.request_id, body:structuredClone(body), status:'sending'};
  saveRequest(record); sending.add(key);
  if (same(target)) {renderRequests(target); renderActions(target);}
  try {
    const result = await api(path, record.body, target);
    receipt(record, result);
    if (result.status === 'failed' || result.status === 'unknown') throw Object.assign(new Error(result.error?.message || result.error || result.status), {receipt: true});
    return result;
  } catch (e) {
    if (!e.receipt) {record.status = e.confirmedRejection ? 'rejected' : 'unknown'; record.error = e.message; saveRequest(record);}
    throw e;
  } finally {
    sending.delete(key);
    if (same(target)) {renderRequests(target); renderActions(target); await refresh();}
  }
}
async function action(name, payload = {}, target = selected) {
  $('error').textContent = '';
  if (!['configure','recovery'].includes(name) && !allowed(name, target)) throw new Error('Действие больше не доступно. Проверьте состояние.');
  const body = {schema_version:1, request_id:crypto.randomUUID(), action:name, scope:scope(target), payload};
  return mutation(name, 'commands', body, target);
}
async function forceRestart() {
  const target = selected, version = selectionVersion;
  if (!allowed('retry', target)) throw new Error('Перезапуск сейчас недоступен');
  if (requestRecords(target).some(r => !['succeeded','failed','rejected'].includes(r.status))) throw new Error('Сначала проверьте результат сохранённого запроса.');
  const result = await action('retry', {force:true, allow_extra_attempt:true}, target);
  if (result?.status !== 'succeeded') return;
  const prepared = result.result?.prepared;
  if (!same(target) || version !== selectionVersion || !prepared) return;
  const view = validSummary(await api('summary', undefined, target), target);
  if (!same(target) || version !== selectionVersion) return;
  summaries.set(target, view);
  if (!view.confirmed || view.generation !== prepared.generation || view.epoch !== prepared.epoch
      || view.current.attempt !== prepared.attempt || view.goal !== prepared.goal
      || view.desired_state !== 'paused' || !allowed('start', target)
      || requestRecords(target).some(r => !['succeeded','failed','rejected'].includes(r.status))) {
    throw new Error('Состояние изменилось после подготовки повтора. Проверьте его перед продолжением.');
  }
  return execute(true);
}
async function execute(resume = false) {
  const target = selected, view = summaries.get(target);
  if (!allowed('start', target)) throw new Error('Запуск сейчас недоступен');
  const options = resume ? {resume:true} : {goal:$('goal').value, mode:$('mode').value, final:$('final').checked, recovery:$('execute-recovery').checked};
  if (!resume && $('items').value.trim()) options.items = $('items').value.trim();
  const body = {request_id:crypto.randomUUID(), options, generation:resume ? view.generation : null, epoch:resume ? view.epoch : null};
  return mutation('execute', 'execute', body, target);
}
function handle(fn) {return async event => {
  const version = selectionVersion, button = event?.currentTarget;
  if (button?.dataset.busy === 'true') return;
  if (button) {button.dataset.busy = 'true'; button.disabled = true;}
  try {await fn();} catch (e) {if (version === selectionVersion) $('error').textContent = e.message;}
  finally {if (button) {delete button.dataset.busy; if (button.isConnected) button.disabled = false;} if (version === selectionVersion) renderActions();}
};}
function renderActions(target = selected) {
  if (!same(target)) return;
  requestRecords(target);
  const view = summaries.get(target), ready = !!view?.confirmed && !storageFaults.has(target), continuing = !!view?.goal;
  const show = (id, name, extra = true) => {
    const button = $(id); button.hidden = !ready || !allowed(name, target) || !extra;
    button.disabled = button.hidden || sending.has(target + ':' + name) || !!unresolved(target, name);
  };
  show('start', 'start', !continuing); show('resume', 'start', continuing);
  for (const id of ['start','resume']) $(id).disabled ||= sending.has(target + ':execute') || !!unresolved(target, 'execute') || (!$('goal').value && !continuing);
  ['pause','stop','reconcile'].forEach(id => show(id, id));
  show('force-restart', 'retry');
  $('force-restart-help').hidden = $('force-restart').hidden;
  $('force-restart').disabled ||= requestRecords(target).some(r => !['succeeded','failed','rejected'].includes(r.status));
  $('retry-controls').hidden = !ready || !allowed('retry', target) || view.observed_state !== 'stopped';
  const attempt = currentDetail?.state?.current?.attempt;
  show('retry', 'retry', !(attempt >= 3)); show('retry-extra', 'retry', attempt >= 3);
  $('gate').hidden = !ready || !allowed('resolve', target);
  for (const button of $('gate').querySelectorAll('button')) button.disabled = !allowed('resolve', target) || sending.has(target + ':resolve') || !!unresolved(target, 'resolve');
  for (const button of $('questions').querySelectorAll('button')) button.disabled = !allowed('reply', target) || sending.has(target + ':reply') || !!unresolved(target, 'reply');
  $('recovery-enabled').disabled = !ready || sending.has(target + ':recovery') || !!unresolved(target, 'recovery');
  $('config-panel').disabled = !ready || loadedTarget !== target || sending.has(target + ':configure') || !!unresolved(target, 'configure');
}
let currentDetail = null;
function renderSummary(view, target) {
  text('goal-state',!view.goal && view.observed_state === 'paused' ? 'Не запущена' : states[view.observed_state]);
  const noGoals = loadedTarget === target && !$('goal').options.length;
  const attempt = currentDetail?.state?.current?.attempt;
  $('status').textContent = `${view.goal || (noGoals ? 'Нет целей в tracking root. Проверьте профиль каталога в настройках.' : 'Выберите цель для запуска')} · ${view.current.item || 'Нет текущего пункта'}${view.current.attempt ? ' · попытка ' + (attempt || 'подготовлена') : ''}`;
  if (view.goal && [...$('goal').options].some(o => o.value === view.goal)) $('goal').value = view.goal;
  $('goal').disabled = !!view.goal || !view.confirmed;
  const p = view.progress; $('goal-progress').max = Math.max(1,p.total); $('goal-progress').value = p.completed;
  $('progress-text').textContent = `Выполнено ${p.completed} из ${p.total}`;
  $('final-status').textContent = ({accepted:'Финальная приёмка подтверждена',not_required:'Очередь выполнена. Финальная проверка не запрашивалась.',failed:'Финальная проверка не пройдена',legacy_unverified:'Историческая проверка не подтверждает приёмку текущей цели'})[view.final_acceptance.status] || 'Завершение пунктов ещё не означает финальную приёмку.';
  const pendingUnknown = requestRecords(target).some(r => r.status === 'unknown');
  $('decision').hidden = !view.blocker && !pendingUnknown && !allowed('retry',target);
  $('decision-diagnostics').hidden = !view.blocker || ['answer_required','confirmation_required','attempt_failed'].includes(view.blocker.code);
  $('blocker').textContent = view.blocker ? `${view.blocker.message} (${view.blocker.code})` : pendingUnknown ? 'Исход запроса не подтверждён. Проверьте его результат; автоматический повтор запрещён.' : allowed('retry',target) ? 'Попытка остановлена. Подготовьте повтор, если хотите продолжить.' : '';
  $('last-action').textContent = view.recovery ? `Восстановление: ${{observing:'наблюдение',waiting:'ожидание проверки',recovering:'в работе',needs_action:'нужно решение',disabled:'выключено'}[view.recovery.status] || view.recovery.status || 'нет данных'}${currentDetail?.recovery?.action ? ' · последнее действие: ' + currentDetail.recovery.action : ''}${view.recovery.next_check ? ' · следующая проверка ' + new Date(view.recovery.next_check * 1000).toLocaleString() : ''}` : '';
  renderActions(target); renderRequests(target);
}
function keepFocus(container, render) {
  const active = document.activeElement, key = container.contains(active) ? active.getAttribute('data-focus') : null;
  const selection = key && typeof active.selectionStart === 'number' ? [active.selectionStart,active.selectionEnd] : null;
  const open = new Set([...container.querySelectorAll('details[open]')].map(d => d.dataset.key));
  render();
  for (const detail of container.querySelectorAll('details')) detail.open = open.has(detail.dataset.key);
  if (key) {
    const next = [...container.querySelectorAll('[data-focus]')].find(n => n.getAttribute('data-focus') === key);
    next?.focus({preventScroll:true});
    if (next && selection) next.setSelectionRange(...selection);
  }
}
function renderQuestions(cur, target) {
  const questions = (cur.messages || []).filter(m => ['question','escalation'].includes(m.type) && !m.answered && !m.closed);
  const canReply = allowed('reply',target);
  const signature = JSON.stringify([canReply, questions.map(m => [target, summaries.get(target)?.generation, summaries.get(target)?.current.attempt, m.type, m.run_id, m.id, m.body])]);
  if ($('questions').dataset.signature === signature) return;
  const hadFocus = $('questions').contains(document.activeElement);
  $('questions').dataset.signature = signature;
  keepFocus($('questions'), () => {
    $('questions').replaceChildren();
    for (const q of questions) {
      const key = JSON.stringify([target, summaries.get(target)?.generation, summaries.get(target)?.current.attempt,q.run_id,q.id]);
      const wrap = node('div', undefined, $('questions')); wrap.className = 'question';
      node('strong',q.type === 'escalation' ? 'Эскалация' : 'Вопрос',wrap);
      node('p', q.body || 'Сообщение без текста', wrap);
      if (q.type === 'escalation') {
        node('p','Изучите сообщение и диагностику агента. Если нужно вмешательство, остановите выполнение.',wrap);
        continue;
      }
      if (!canReply) {node('p','Ответ сейчас недоступен. Проверьте текущее состояние и допустимые действия.',wrap);continue;}
      const label = node('label','Ваш ответ', wrap), input = node('input', undefined, label); input.value = drafts.get(key) || '';
      input.dataset.focus = 'answer:' + key;
      input.setAttribute('aria-label', 'Ответ ' + q.id); input.oninput = () => drafts.set(key,input.value);
      const observedScope = scope(target);
      const button = node('button', 'Ответить', wrap); button.dataset.focus = 'reply:' + key;
      button.onclick = handle(() => {
        if (!allowed('reply',target)) throw new Error('Ответ больше не доступен. Проверьте состояние.');
        if (!input.value.trim()) throw new Error('Введите ответ');
        // Bind this question to its rendered generation/attempt; polling must not retarget a draft.
        const currentScope = scope(target);
        if (currentScope.generation !== observedScope.generation || currentScope.attempt !== observedScope.attempt) throw new Error('Попытка изменилась. Проверьте новый вопрос.');
        return action('reply', {message_id:q.id, text:input.value, ...(q.run_id ? {run_id:q.run_id} : {})},target);
      });
    }
  });
  if (hadFocus && !canReply && same(target)) {
    $('decision-heading').tabIndex = -1; $('decision-heading').focus({preventScroll:true});
  }
}
function renderDetails(v, target) {
  const events = v.events || {events:[],overdue:0};
  $('event-warning').textContent = events.overdue ? `Не обработано более 5 минут: ${events.overdue}` : '';
  const checks = v.verification || {checks:[],records:[]};
  $('verification').replaceChildren();
  const labels = {executed:'Выполнено сейчас',reused:'Подтверждено по прежнему результату',required:'Требуется проверка',not_applicable:'Не применимо'};
  for (const check of checks.checks) node('p',`${labels[check.status] || check.status}: ${check.command.join(' ')}`,$('verification'));
  for (const record of checks.records) node('p',`${labels[record.status] || record.status}: ${record.result}`,$('verification'));
  const coordinatorWait = events.events.reduce((sum,e) => sum + (e.coordinator_wait_seconds || (e.processed_at ? 0 : Date.now()/1000-e.created_at)),0);
  node('p',`Ожидание координатора: ${Math.round(coordinatorWait)} с; очередь ресурса: ${Math.round(checks.resource_wait_seconds || 0)} с; выполнение: ${Math.round(checks.execution_seconds || 0)} с; повторы: ${checks.repeats || 0}`,$('verification'));

  const st = v.state, cur = st.current || {};
  const detailAttempt = cur.attempt_dir || cur.dispatch_id || cur.item || null;
  if (detailAttempt === summaries.get(target)?.current.attempt) renderQuestions(cur,target);
  else {$('questions').replaceChildren();delete $('questions').dataset.signature;}
  comparison = v.settings || {}; renderComparison();
  $('recovery').textContent = (st.recovery_issues || []).join('; ');
  const recovery = v.recovery || {};
  $('recovery-enabled').checked = summaries.get(target)?.recovery?.enabled === true;
  $('recovery-status').textContent = ({observing:'Наблюдение',recovering:'Восстановление',waiting:'Ожидание проверки',needs_action:'Нужно действие',disabled:'Выключено'})[recovery.status] || recovery.status || 'Нет данных';
  $('recovery-reason').textContent = recovery.reason ? 'Причина: ' + recovery.reason : '';
  $('recovery-action').textContent = recovery.action ? 'Действие: ' + recovery.action : '';
  $('recovery-next').textContent = recovery.next_check_at ? 'Следующая проверка: ' + new Date(recovery.next_check_at * 1000).toLocaleString() : '';
  $('operations').replaceChildren(); $('operation-history').replaceChildren();
  for (const op of v.operation_history || []) node('p', `${op.key}: ${op.status} · ${op.error || ''}`, $('operation-history'));
  for (const op of v.operations || []) {
    const div = node('div', `${op.key}: ${op.status} · ${op.error || ''}`, $('operations'));
    if (op.args?.[0] === 'orchestration' && op.status !== 'rejected' && allowed('continue-operation',target)) {
      const button = node('button', 'Продолжить операцию',div);
      button.onclick = handle(() => action('continue-operation', {operation_id:op.request_id},target));
    }
  }
  const agentSignature = JSON.stringify([v.resources?.agents,cur.outputs]);
  if ($('agents').dataset.signature !== agentSignature) {
    $('agents').dataset.signature = agentSignature;
    keepFocus($('agents'), () => {
      $('agents').replaceChildren();
      if (!v.resources?.agents?.length) node('p','Нет активных агентов', $('agents'));
      for (const a of v.resources?.agents || []) {
        const div = node('article', undefined, $('agents')); div.className = 'agent';
        node('strong', `${a.role}: ${a.runtime_state || a.status || 'Состояние неизвестно'}`,div);
        node('p', 'Подтверждённая модель: ' + (a.observed?.model || 'неизвестна'),div);
        const speed = a.token_speed;
        if (speed?.status === 'measured') node('p', `${speed.tokens_per_second.toFixed(1)} ток/с · ответов ${speed.responses}`,div);
        if (a.error) node('p',a.error,div);
        const detail = node('details', undefined, div); detail.dataset.key = a.dispatch_id || a.role;
        node('summary','Терминал и параметры',detail);
        node('p','Dispatch: ' + (a.dispatch_id || 'неизвестен'),detail);
        node('p','Запрошено: ' + JSON.stringify(a.requested || {}),detail);
        node('p','Подтверждено: ' + JSON.stringify(a.observed || {}),detail);
        if (speed?.status === 'measured') node('pre',JSON.stringify(speed,null,2),detail);
        if (a.terminal) {
          const button = node('button','Открыть терминал',detail); button.dataset.focus = 'terminal:' + a.dispatch_id;
          button.disabled = !v.confirmed;
          button.onclick = handle(async () => {await api('focus',{terminal:a.terminal,request_id:crypto.randomUUID()},target);});
        }
        const output = (cur.outputs || {})[a.dispatch_id]; if (output) node('pre',output,detail);
      }
    });
  }
  const queueSignature = JSON.stringify(v.queue);
  if ($('queue').dataset.signature !== queueSignature) {
    $('queue').dataset.signature = queueSignature; $('queue').replaceChildren();
    if (!v.queue?.length) node('p','Пункты появятся после выбора и запуска цели.', $('queue'));
    for (const item of v.queue || []) {
      const row = node('div',undefined,$('queue')); row.className = 'queue-row';
      node('strong',item.id,row); node('span',({completed:'Выполнено',in_progress:'В работе',not_started:'Ожидает',verification_required:'Требуется проверка',skipped:'Пропущено',failed:'Ошибка'})[item.status] || item.status,row); node('span',item.task || '',row);
    }
  }
  $('attempts').textContent = JSON.stringify([...(st.attempts || []),...(st.history || [])].map(a => ({item:a.item,attempt:a.attempt,outcome:a.outcome,problems:a.problems,snapshot:a.attempt_dir})),null,2);
  const health = v.health || {state:st.last_error ? 'error':'ok',error:st.last_error};
  $('runner-error').hidden = true; $('runner-error').textContent = `Состояние: ${health.state}${health.error ? ' · ' + health.error : ''}`;
  $('commands').textContent = JSON.stringify((v.commands || []).map(c => ({action:c.action,status:c.status,error:c.error})),null,2);
  $('log').textContent = v.log || '';
}
async function refresh() {
  const target = selected, version = selectionVersion;
  if (!target || refreshing.has(version)) return;
  refreshing.add(version);
  try {
    const view = validSummary(await api('summary',undefined,target),target);
    if (version !== selectionVersion) return;
    summaries.set(target,view); renderSummary(view,target);
    if (loadedTarget !== target) {
      await loadWorkspaceConfig(target,version);
      if (version !== selectionVersion) return;
    }
    try {
      const v = await api('state',undefined,target);
      if (version !== selectionVersion) return;
      if (!v || !v.state || !Array.isArray(v.queue) || !Array.isArray(v.resources?.agents)) throw new Error('Некорректные подробности состояния');
      currentDetail = v; renderDetails(v,target);
      text('connection',v.confirmed ? 'Соединение с сервисом подтверждено' : 'Сервис выключен · сохранённое состояние');
    } catch (e) {
      if (version !== selectionVersion) return;
      currentDetail = null; text('connection','Сервис недоступен · подробности не подтверждены');
      $('questions').replaceChildren(); delete $('questions').dataset.signature;
    }
    if (version !== selectionVersion) return;
    $('profile-panel').disabled = loadedTarget !== target;
    await observeRequests(target);
    if (version === selectionVersion) {renderSummary(view,target);renderActions(target);}
  } catch (e) {if (version === selectionVersion) {
    summaries.delete(target); currentDetail = null;
    $('connection').textContent = 'Состояние не подтверждено'; $('goal-state').textContent = 'Нет подтверждённого состояния'; $('error').textContent = e.message;
    $('decision').hidden = false; $('blocker').textContent = 'Не удалось проверить состояние. Управление заблокировано; запросы не повторялись.';
    $('profile-panel').disabled = true; renderActions(target);
  }} finally {refreshing.delete(version);}
}
function selectTemplate() {
  const template = templates.find(t => t.id === $('template').value);
  if (!template?.available) throw new Error('Cloud.ru доступен только внутри ~/repo/sber');
  settings = structuredClone(template.config); renderSettings(); $('saved').textContent = 'Не сохранено';
}
$('template').onchange = handle(selectTemplate);
$('recovery-enabled').onchange = handle(async () => {
  try {await action('recovery',{enabled:$('recovery-enabled').checked});} finally {await refresh();}
});
$('template-restore').onclick = handle(selectTemplate);
$('save').onclick = handle(async () => {
  const target = selected, result = await action('configure',structuredClone(settings),target);
  if (!same(target)) return;
  if (result?.status === 'succeeded') {settings = result.result; renderSettings(); $('saved').textContent = 'Сохранено';}
  else $('saved').textContent = 'Сохранение ожидает выполнения';
});
$('reset').onclick = handle(async () => {
  const target = selected, result = await action('configure',{reset:true},target);
  if (same(target) && result?.status === 'succeeded') {settings = result.result; renderSettings(); $('saved').textContent = 'Defaults сохранены';}
});
$('start').onclick = handle(() => execute(false));
$('resume').onclick = handle(() => execute(true));
$('pause').onclick = handle(() => action('pause'));
$('reconcile').onclick = handle(() => action('reconcile'));
$('stop').onclick = handle(() => action('stop'));
$('force-restart').onclick = handle(forceRestart);
$('retry-extra').onclick = handle(() => action('retry',{allow_extra_attempt:true,refresh:$('refresh').checked,saved_settings:$('refresh').checked}));
$('retry').onclick = handle(() => action('retry',{refresh:$('refresh').checked,saved_settings:$('refresh').checked}));
$('operation-check').onclick = handle(() => observeRequests());
$('decision-diagnostics').onclick = () => {$('diagnostics').open = true; $('diagnostics').querySelector('summary').focus();};
$('goal').onchange = () => renderActions();
for (const button of document.querySelectorAll('[data-choice]')) button.onclick = handle(() => action('resolve',{choice:button.dataset.choice}));
async function loadCatalog(projectId, workspaceId) {
  const value = await api('projects'); projects = value.projects;
  $('project').replaceChildren();
  for (const p of Object.values(projects)) option($('project'), p.id, p.name);
  if (projectId && projects[projectId]) $('project').value = projectId;
  await selectProject(workspaceId);
}
async function selectProject(workspaceId) {
  const p = projects[$('project').value];
  $('workspace').replaceChildren();
  if (p) for (const w of Object.values(p.workspaces)) if (!w.disabled) option($('workspace'), w.id, w.name);
  if (workspaceId) $('workspace').value = workspaceId;
  await selectWorkspace();
}
function rememberWorkspace() {
  if (!selected || loadedTarget !== selected) return;
  workspaceDrafts.set(selected,{settings:structuredClone(settings),profile:$('workspace-profile').value,
    goal:$('goal').value,items:$('items').value,mode:$('mode').value,final:$('final').checked,recovery:$('execute-recovery').checked,saved:$('saved').textContent});
}
async function selectWorkspace() {
  rememberWorkspace();
  const version = ++selectionVersion;
  fastCapabilities.clear();
  const p = projects[$('project').value], w = p?.workspaces[$('workspace').value];
  selected = w ? `projects/${p.id}/workspaces/${w.id}` : '';
  currentDetail = null; settings = undefined; comparison = {};
  $('workspace-panel').hidden = !w;
  $('empty-state').hidden = !!w;
  $('empty-state').textContent = p ? 'Подключите существующий Git checkout в настройках ниже.' : 'Создайте проект и подключите Git checkout в настройках ниже.';
  if (!w) $('settings-details').open = true;
  $('workspace-panel').disabled = false; $('config-panel').disabled = true; $('profile-panel').disabled = true;
  loadedTarget = ''; $('roles').replaceChildren(); $('goal').replaceChildren();
  $('workspace-path').textContent = w?.path || 'Каталог не подключён';
  for (const id of ['questions','agents','queue','operations','operation-history','comparison']) {$(id).replaceChildren();delete $(id).dataset.signature;}
  for (const id of ['status','attempts','commands','log','blocker','last-action']) $(id).textContent = '';
  $('decision').hidden = true; $('error').textContent = ''; $('saved').textContent = '';
  $('runner-error').textContent = ''; $('runner-error').hidden = true;
  if (!w) {$('connection').textContent = 'Выберите каталог'; return;}
  renderActions(); renderRequests();
  if (version === selectionVersion) await refresh();
}
async function loadWorkspaceConfig(target, version) {
  const [data, profile] = await Promise.all([api('config', undefined, target), api('profile', undefined, target)]);
  if (version !== selectionVersion) return;
  $('config-source').textContent = 'Файл: ' + data.source;
  const draft = workspaceDrafts.get(target);
  settings = draft ? structuredClone(draft.settings) : data.config; defaults = data.defaults; catalog = data.connections; templates = data.templates || [];
  $('template').replaceChildren();
  for (const t of templates) {option($('template'), t.id, t.name); $('template').lastChild.disabled = !t.available;}
  for (const id of ['profiles','providers','models','goal']) $(id).replaceChildren();
  datalist('opencode2-models').replaceChildren();
  Object.keys(catalog.profiles).forEach(v => option($('profiles'), v));
  [...catalog.providers, 'current'].forEach(v => option($('providers'), v));
  [...new Set(Object.values(defaults.roles).map(r => r.model))].forEach(v => option($('models'), v));
  (catalog.opencode2?.models || []).forEach(v => option($('opencode2-models'), v));
  data.goals.forEach(v => option($('goal'), v));
  $('workspace-profile').value = draft ? draft.profile : JSON.stringify(profile.profile, null, 2);
  $('items').value = draft?.items || ''; $('mode').value = draft?.mode || 'auto'; $('final').checked = draft?.final ?? true; $('execute-recovery').checked = draft?.recovery ?? true;
  if (draft?.goal && data.goals.includes(draft.goal)) $('goal').value = draft.goal;
  $('saved').textContent = draft?.saved || '';
  if (!data.goals.length) {$('status').textContent = 'Нет целей в tracking root. Проверьте профиль каталога.';}
  loadedTarget = target;
  renderSettings();
}
$('project').onchange = handle(() => selectProject());
$('workspace').onchange = handle(selectWorkspace);
$('project-add').onclick = handle(async () => {
  const p = await api('projects', {name:$('project-name').value}); await loadCatalog(p.id);
});
$('project-rename').onclick = handle(async () => {
  const id = $('project').value;
  await api('projects/' + id, {name:$('project-name').value}); await loadCatalog(id);
});
$('workspace-add').onclick = handle(async () => {
  const id = $('project').value, data = {path:$('workspace-path-input').value};
  if ($('orca-id').value) data.orca_id = $('orca-id').value;
  if ($('orca-instance').value) data.orca_instance = $('orca-instance').value;
  const w = await api('projects/' + id + '/workspaces', data); await loadCatalog(id, w.id);
});
$('workspace-disable').onclick = handle(async () => {
  const id = $('project').value; await api('disable', {}); await loadCatalog(id);
});
$('profile-save').onclick = handle(async () => {
  await api('profile', JSON.parse($('workspace-profile').value)); $('saved').textContent = 'Профиль сохранён';
});
async function serviceAction(project, name) {
  const key = 'team-runner-service-request:' + project + ':' + name;
  const previous = JSON.parse(sessionStorage.getItem(key) || 'null');
  if (previous && !['done','error'].includes(previous.status)) throw new Error('Исход запроса сервиса ' + previous.request_id + ' не подтверждён. Проверьте состояние; повтор не отправлен.');
  const body = {request_id:crypto.randomUUID()}, record = {body,request_id:body.request_id,project,action:name,status:'sending'};
  sessionStorage.setItem(key,JSON.stringify(record));
  try {
    const result = await api('projects/' + project + '/service/' + name,body);
    record.status = result.status; record.error = result.error; sessionStorage.setItem(key,JSON.stringify(record));
    if (result.status !== 'done') throw new Error(result.error || result.result?.last_error || result.status);
    return result;
  } catch (e) {
    if (record.status === 'sending') {record.status = 'unknown';record.error = e.message;sessionStorage.setItem(key,JSON.stringify(record));}
    throw e;
  }
}
let servicesBusy = false;
async function refreshServices() {
  if (servicesBusy) return;
  servicesBusy = true;
  try {
    const [rows, value] = await Promise.all([api('services'), api('projects')]);
    for (const row of rows) {
      const p = value.projects[row.project_id];
      if (!p) continue;
      let card = document.getElementById('service-' + p.id);
      if (!card) {
        card = node('article', undefined, $('services')); card.id = 'service-' + p.id;
        card.className = 'service-card'; card.dataset.project = p.id;
        const select = node('button', p.name, card); select.className = 'service-select';
        select.onclick = handle(async () => {await loadCatalog(p.id);});
        node('p', '', card).className = 'service-state';
        const label = node('label', 'Порт следующего запуска (0 — авто) ', card);
        const port = node('input', undefined, label); port.type = 'number'; port.min = 0; port.max = 65535;
        port.value = p.service_port; port.setAttribute('aria-label', 'Порт ' + p.name);
        const save = node('button', 'Сохранить порт', card);
        save.onclick = handle(async () => {await api('projects/' + p.id, {service_port: Number(port.value)}); await refreshServices();});
        const controls = node('div', undefined, card);
        for (const [action, text] of [['start','Запустить сервис'],['stop','Остановить сервис'],['restart','Перезапустить'],['reconcile','Проверить состояние']]) {
          const button = node('button', text, controls); button.dataset.serviceAction = action;
          button.onclick = handle(async () => {
            button.disabled = true;
            try {
              await serviceAction(p.id,action);
            } finally {button.disabled = false; await refreshServices(); await refresh();}
          });
        }
        node('div', '', card).className = 'workspace-summary';
      }
      card.querySelector('.service-select').textContent = p.name;
      card.querySelector('.service-state').textContent = `${row.state} · порт ${row.port || '—'}${row.confirmed ? '' : ' · не подтверждено'}${row.last_error ? ' · ' + row.last_error : ''}`;
      const summary = card.querySelector('.workspace-summary'); summary.replaceChildren();
      for (const w of row.workspaces) node('p', `${w.name}: ${w.goal || 'Цель не выбрана'} · ${w.item || '—'} · ${w.completed ?? '—'}/${w.total ?? '—'} · ${w.state || (w.error ? 'error' : 'ok')}${w.waiting ? ' · Ожидает ответа' : ''}${w.error ? ' · ' + w.error : ''}${w.confirmed ? '' : ' · сохранено, не подтверждено сервисом'}`, summary);
    }
  } catch (e) {$('error').textContent = e.message;} finally {servicesBusy = false;}
}
handle(async () => {
  await loadCatalog(); await refreshServices();
  setInterval(() => {refresh(); refreshServices();}, 2000);
})();

$('auto-continue-after-replan').onchange = () => {
  settings.auto_continue_after_replan = $('auto-continue-after-replan').checked;
  $('saved').textContent = 'Не сохранено';
};
