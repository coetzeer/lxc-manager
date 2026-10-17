'use strict';
const $ = id => document.getElementById(id);
const views = new Map();
const busy = new Map();
let refreshTask = null, refreshAgain = false, releaseRequest = 0, creating = false, doctorRequest = 0, doctorName;
let webName = null;
let shellSessions = new Map(), activeShellId = null, pendingShellName = null, shellConfigRequest = 0;
let shellDockMinimized = false, shellDockHeight = 380;
const SHELL_STATE_KEY = 'lxc-manager-shell-workspace';
let mutationVersion = 0;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function setText(el, value) { const text = String(value); if (el.textContent !== text) el.textContent = text; }
function button(text, action, cls) {
  const el = node('button', text, cls); el.type = 'button'; el.addEventListener('click', action); return el;
}
function notify(text, error = false) {
  $('notice').hidden = false; $('notice').className = error ? 'toast error' : 'toast';
  setText($('notice-text'), text);
}
async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = {...(options.headers || {})};
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    const csrf = (document.cookie || '').split('; ').find(item => item.startsWith('lxc_manager_csrf='))?.split('=').slice(1).join('=');
    if (csrf) headers['X-CSRF-Token'] = decodeURIComponent(csrf);
  }
  const response = await fetch(path, {...options, method, headers, credentials: 'same-origin', signal: AbortSignal.timeout(options.method ? 240000 : 45000)});
  if (response.status === 401 && location.pathname !== '/login') {
    const next = location.pathname + location.search;
    location.assign('/login?next=' + encodeURIComponent(next));
  }
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
  return data;
}
function bytes(value) {
  if (value == null) return '—';
  let unit = 0; const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return value.toFixed(unit ? 1 : 0) + ' ' + units[unit];
}
function path(name) { return '/containers/' + encodeURIComponent(name); }
function openDrawer(id) { if (!$(id).open) $(id).showModal(); }
function ensureLocalAction(action) {
  action();
}
function newShellId() { return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`; }
function shellLabel(name) {
  const count = [...shellSessions.values()].filter(session => session.name === name).length + 1;
  return `${name}#${count}`;
}
function persistShellState() {
  try {
    localStorage.setItem(SHELL_STATE_KEY, JSON.stringify({
      sessions: [...shellSessions.values()].map(session => ({id: session.id, name: session.name, shell: session.shell, label: session.label})),
      dockHeight: shellDockHeight,
      activeSession: activeShellId,
    }));
  } catch (_) {}
}
function setDockHeight(height) {
  shellDockHeight = Math.max(240, Math.min(Math.round(height), Math.max(300, window.innerHeight - 90)));
  $('shell-dock').style.setProperty('--shell-dock-height', `${shellDockHeight}px`); persistShellState();
}
function renderShellTabs() {
  const tabs = $('shell-tabs'); tabs.replaceChildren();
  for (const session of shellSessions.values()) {
    const tab = node('div', undefined, 'shell-tab' + (session.id === activeShellId ? ' active' : ''));
    const select = button(session.label, () => activateShellSession(session.id));
    select.setAttribute('role', 'tab'); select.setAttribute('aria-selected', String(session.id === activeShellId));
    if (session.unread) select.append(node('span', ' •', 'shell-unread'));
    const close = button('×', () => closeShellSession(session.id), 'shell-tab-close'); close.setAttribute('aria-label', 'Close ' + session.label);
    tab.append(select, close); tabs.append(tab);
  }
  $('shell-dock').hidden = shellSessions.size === 0;
  $('shell-dock').classList.toggle('minimized', shellDockMinimized);
  document.body.classList.toggle('shell-dock-open', shellSessions.size > 0);
  document.body.style.setProperty('--shell-dock-space', shellSessions.size ? `${shellDockMinimized ? 45 : shellDockHeight}px` : '0px');
}
function updateShellSession(session, state) {
  session.state = state; renderShellTabs();
}
function activateShellSession(id) {
  const session = shellSessions.get(id); if (!session) return;
  activeShellId = id;
  for (const other of shellSessions.values()) other.panel.hidden = other.id !== id;
  session.unread = false; renderShellTabs(); persistShellState();
  requestAnimationFrame(() => { try { session.fit.fit(); } catch (_) {} session.terminal.focus(); });
}
function resizeShellSession(session) {
  if (!session || session.id !== activeShellId) return;
  try { session.fit.fit(); } catch (_) { return; }
  if (session.socket?.readyState === WebSocket.OPEN) session.socket.send(JSON.stringify({type:'resize', cols:session.terminal.cols, rows:session.terminal.rows}));
}
function connectShellSession(session) {
  if (session.manual) return;
  updateShellSession(session, 'connecting');
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams({session_id: session.id, shell: session.shell});
  const socket = new WebSocket(`${protocol}//${location.host}/containers/${encodeURIComponent(session.name)}/shell?${params}`);
  session.socket = socket; socket.binaryType = 'arraybuffer';
  socket.onopen = () => { if (session.socket !== socket) return; updateShellSession(session, 'connected'); resizeShellSession(session); session.terminal.focus(); };
  socket.onmessage = async event => {
    if (session.socket !== socket) return;
    let output;
    if (typeof event.data === 'string') output = event.data;
    else { const data = event.data instanceof Blob ? new Uint8Array(await event.data.arrayBuffer()) : new Uint8Array(event.data); output = session.decoder.decode(data, {stream:true}); }
    session.bytes += output.length; session.terminal.write(output);
    if (session.id !== activeShellId) { session.unread = true; renderShellTabs(); }
  };
  socket.onerror = () => { if (session.socket === socket) updateShellSession(session, 'reconnecting'); };
  socket.onclose = event => {
    if (session.socket !== socket || session.manual) return;
    session.socket = null;
    if (event.code === 1008) { updateShellSession(session, 'disconnected'); session.terminal.write(`\r\n\x1b[90m[session unavailable: ${event.reason}]\x1b[0m\r\n`); return; }
    updateShellSession(session, 'reconnecting');
    clearTimeout(session.reconnectTimer); session.reconnectTimer = setTimeout(() => connectShellSession(session), 1000);
  };
}
function createShellSession(name, shell, id = newShellId(), restored = false) {
  if (typeof Terminal !== 'function' || !globalThis.FitAddon?.FitAddon) { notify('Terminal assets did not load. Refresh the page and try again.', true); return; }
  const session = {id, name, shell, label: restored ? `${name} · ${shell.split('/').pop()}` : shellLabel(name), state:'connecting', socket:null, terminal:null, fit:null, panel:null, decoder:new TextDecoder(), bytes:0, unread:false, manual:false, reconnectTimer:null};
  session.panel = node('div', undefined, 'shell-session-panel'); session.panel.hidden = true; $('shell-terminal').append(session.panel);
  session.terminal = new Terminal({cursorBlink:true,cursorStyle:'block',fontFamily:'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',fontSize:13,lineHeight:1.18,scrollback:5000,theme:{background:'#0b1114',foreground:'#dfffee',cursor:'#88e4c2',cursorAccent:'#0b1114',selectionBackground:'#88e4c244',black:'#101419',brightBlack:'#61717f',red:'#ffa4a4',brightRed:'#ffc1c1',green:'#88e4c2',brightGreen:'#a5eed5',yellow:'#edcb83',brightYellow:'#f7dea8',blue:'#8ec5ff',brightBlue:'#b9dcff',magenta:'#d7a9ff',brightMagenta:'#e7caff',cyan:'#8adce8',brightCyan:'#b9eef5',white:'#d7e0e8',brightWhite:'#ffffff'}});
  session.fit = new FitAddon.FitAddon(); session.terminal.loadAddon(session.fit); session.terminal.open(session.panel);
  const focus = () => session.terminal.focus(); session.panel.tabIndex = 0; session.panel.addEventListener('pointerdown', focus); session.panel.addEventListener('click', focus);
  session.terminal.attachCustomKeyEventHandler(event => { if (event.type !== 'keydown' || !event.ctrlKey || !event.shiftKey) return true; if (event.code === 'KeyC' && session.terminal.hasSelection()) { navigator.clipboard?.writeText(session.terminal.getSelection()); return false; } if (event.code === 'KeyV' && navigator.clipboard?.readText) { navigator.clipboard.readText().then(text => session.terminal?.paste(text)); return false; } return true; });
  session.terminal.onData(data => { if (session.socket?.readyState === WebSocket.OPEN) session.socket.send(JSON.stringify({type:'input',data})); });
  session.resizeObserver = new ResizeObserver(() => requestAnimationFrame(() => resizeShellSession(session))); session.resizeObserver.observe($('shell-terminal'));
  if (!restored) session.terminal.write('\x1b[90mOpening PTY session…\x1b[0m\r\n'); else session.terminal.write('\x1b[90mReconnecting PTY session…\x1b[0m\x1b[0m\r\n');
  shellSessions.set(id, session); activeShellId = id; shellDockMinimized = false; renderShellTabs(); activateShellSession(id); persistShellState(); connectShellSession(session);
}
function closeShellSession(id) {
  const session = shellSessions.get(id); if (!session) return;
  session.manual = true; clearTimeout(session.reconnectTimer);
  if (session.socket?.readyState === WebSocket.OPEN) { session.socket.send(JSON.stringify({type:'close'})); session.socket.close(); }
  session.resizeObserver?.disconnect(); session.terminal.dispose(); session.panel.remove(); shellSessions.delete(id);
  if (activeShellId === id) activeShellId = shellSessions.keys().next().value || null;
  renderShellTabs(); if (activeShellId) activateShellSession(activeShellId); persistShellState();
}
async function loadShellChoices(name) {
  const request = ++shellConfigRequest; const select = $('shell-choice'); select.disabled = true; select.replaceChildren(node('option', 'Loading shells…')); $('shell-custom').value = ''; setText($('shell-config-status'), '');
  try {
    const data = await api(path(name) + '/shells'); if (request !== shellConfigRequest) return;
    select.replaceChildren(...data.shells.map(shell => { const option = node('option', shell); option.value = shell; return option; }));
    if (!data.shells.length) { select.replaceChildren(node('option', 'No standard shells found')); select.value = ''; }
    else { select.value = data.default && data.shells.includes(data.default) ? data.default : data.shells[0]; select.disabled = false; }
    setText($('shell-config-status'), data.shells.length ? `${data.shells.length} shells available.` : 'Enter a custom executable path.');
  } catch (error) { if (request === shellConfigRequest) { select.replaceChildren(node('option', 'Shell discovery failed')); select.value = ''; setText($('shell-config-status'), error.message); } }
}
function openShell(name) {
  ensureLocalAction(() => { const view = views.get(name); if (!view || view.container.status !== 'RUNNING') { notify('Start the container before opening a shell.', true); return; } pendingShellName = name; setText($('shell-config-title'), 'Open shell · ' + name); openDrawer('shell-config-drawer'); loadShellChoices(name); });
}
function restoreShellSessions() {
  try {
    const state = JSON.parse(localStorage.getItem(SHELL_STATE_KEY) || '{}'); shellDockHeight = Number(state.dockHeight) || shellDockHeight; setDockHeight(shellDockHeight);
    if (!Array.isArray(state.sessions)) return;
    for (const item of state.sessions.slice(0, 12)) if (item?.id && item.name && item.shell && views.get(item.name)?.container.status === 'RUNNING') createShellSession(item.name, item.shell, item.id, true); if (state.activeSession && shellSessions.has(state.activeSession)) activateShellSession(state.activeSession);
  } catch (_) {}
}
function openWeb(name) {
  ensureLocalAction(() => { webName = name; setText($('web-title'), name + ' web app'); setText($('web-status'), ''); $('web-port').value = ''; openDrawer('web-drawer'); $('web-port').focus(); });
}
function metric(label) {
  const el = node('div', undefined, 'metric'), value = node('strong', '—'), note = node('small', 'Not running'), bar = node('progress', undefined, 'inactive');
  bar.max = 100; bar.value = 0; bar.setAttribute('aria-label', label);
  el.append(node('span', label, 'mobile-label'), value, note, bar); return {el, value, note, bar};
}
function createView(container) {
  const name = container.name;
  const el = node('article', undefined, 'container'); el.dataset.container = name;
  const row = node('div', undefined, 'container-row'), identity = node('div', undefined, 'identity');
  const badge = node('span', '', 'badge');
  identity.append(node('h3', name), badge);
  const ip = node('div', undefined, 'ip'), ipValue = node('span', '—');
  ip.append(node('span', 'IP ADDRESS', 'mobile-label'), ipValue);
  const cpu = metric('CPU'), memory = metric('MEMORY'), actions = node('div', undefined, 'actions');
  const view = {el, container, badge, ip: ipValue, cpu, memory, epoch: 0, metrics: null, metricsPending: false, snapshotsLoaded: false, logsLoaded: false};
  const primary = button('Start', () => mutate(name, view.container.status === 'RUNNING' ? 'stop' : 'start'));
  const detail = node('div', undefined, 'details-panel'); detail.hidden = true;
  detail.id = 'details-' + name;
  const expand = button('Details', () => { detail.hidden = !detail.hidden; expand.setAttribute('aria-expanded', String(!detail.hidden)); });
  expand.setAttribute('aria-expanded', 'false'); expand.setAttribute('aria-controls', detail.id);
  const shellOpen = button('Open shell', () => openShell(name), 'shell-action');
  const more = node('details', undefined, 'more'), summary = node('summary', '•••'); summary.setAttribute('aria-label', 'More actions for ' + name);
  const menu = node('div', undefined, 'menu');
  for (const [label, action] of [['Open web app', () => openWeb(name)], ['Restart', () => mutate(name, 'restart')], ['Create snapshot', () => mutate(name, 'snapshot')], ['Diagnose', () => runDoctor(name)], ['Delete container', () => mutate(name, 'delete')]]) {
    menu.append(button(label, () => { more.open = false; action(); }, label === 'Delete container' ? 'danger' : ''));
  }
  more.append(summary, menu); actions.append(primary, shellOpen, expand, more);
  row.append(identity, ip, cpu.el, memory.el, actions);
  const io = node('div', undefined, 'details-grid'); view.io = {};
  for (const [key, label] of [['disk_read_bytes','Disk read'],['disk_write_bytes','Disk written'],['network_rx_bytes','Network received'],['network_tx_bytes','Network sent']]) {
    const cell = node('div'), value = node('strong', '—'); cell.append(node('span', label), value); io.append(cell); view.io[key] = value;
  }
  const snapshots = node('details'), snapshotTitle = node('summary', 'Snapshots'), snapshotBody = node('div');
  const snapshotRefresh = button('Refresh snapshots', () => loadSnapshots(view));
  snapshots.append(snapshotTitle, snapshotRefresh, snapshotBody);
  snapshots.addEventListener('toggle', () => { if (snapshots.open && !view.snapshotsLoaded) loadSnapshots(view); });
  const logs = node('details'), logBody = node('pre', 'Logs have not been loaded.');
  const logRefresh = button('Refresh logs', () => loadLogs(view));
  logs.append(node('summary', 'Logs'), logRefresh, logBody);
  logs.addEventListener('toggle', () => { if (logs.open && !view.logsLoaded) loadLogs(view); });
  detail.append(io, snapshots, logs); el.append(row, detail);
  Object.assign(view, {primary, shellOpen, expand, menu, detail, snapshots, snapshotTitle, snapshotBody, snapshotRefresh, logBody, logRefresh});
  updateView(view, container); return view;
}
function renderMetrics(view, stale = false) {
  const m = view.metrics;
  const running = view.container.status === 'RUNNING';
  setText(view.cpu.value, running && m?.cpu_usage_percent != null ? m.cpu_usage_percent.toFixed(1) + '%' : '—');
  setText(view.memory.value, running ? bytes(m?.memory_usage_bytes) : '—');
  const note = !running ? 'Not running' : stale ? 'Stale · retrying' : !m ? 'Awaiting metrics' : '100% = one core';
  setText(view.cpu.note, note);
  setText(view.memory.note, !running || stale || !m ? note : m.memory_limit_bytes > 0 ? 'of ' + bytes(m.memory_limit_bytes) : 'Limit not available');
  view.cpu.note.className = view.memory.note.className = stale ? 'stale' : '';
  const cpuKnown = running && m?.cpu_usage_percent != null;
  view.cpu.bar.className = cpuKnown ? '' : 'inactive';
  view.cpu.bar.value = cpuKnown ? Math.min(100, m.cpu_usage_percent) : 0;
  const memoryKnown = running && m?.memory_usage_bytes != null && m?.memory_limit_bytes > 0;
  view.memory.bar.className = memoryKnown ? '' : 'inactive';
  view.memory.bar.value = memoryKnown ? Math.min(100, 100 * m.memory_usage_bytes / m.memory_limit_bytes) : 0;
  for (const [key, el] of Object.entries(view.io)) setText(el, running ? bytes(m?.[key]) : '—');
}
function updateView(view, container) {
  if (view.container.status !== container.status) { view.epoch++; view.metrics = null; }
  view.container = container;
  const pending = busy.get(container.name);
  setText(view.badge, pending || container.status);
  view.badge.className = 'badge' + (pending ? ' pending' : container.status === 'RUNNING' ? ' running' : '');
  setText(view.ip, container.ipv4 || container.ipv6 || '—');
  setText(view.primary, pending ? 'Wait…' : (container.status === 'RUNNING' ? 'Stop' : 'Start'));
  view.primary.disabled = Boolean(pending);
  view.menu.querySelectorAll('button').forEach(b => b.disabled = Boolean(pending));
  renderMetrics(view, view.stale);
}
async function loadMetrics(view) {
  if (view.metricsPending || busy.has(view.container.name) || view.container.status !== 'RUNNING') return;
  view.metricsPending = true; const epoch = view.epoch;
  try {
    const m = await api(path(view.container.name) + '/metrics?include_snapshots=false');
    if (epoch !== view.epoch || views.get(view.container.name) !== view) return;
    view.metrics = m; view.stale = false; renderMetrics(view);
  } catch (error) {
    if (epoch !== view.epoch) return;
    view.stale = true; renderMetrics(view, true); view.cpu.note.title = error.message;
  } finally { view.metricsPending = false; }
}
async function loadSnapshots(view) {
  if (view.snapshotRefresh.disabled) return;
  view.snapshotRefresh.disabled = true; const epoch = view.epoch;
  if (!view.snapshotsLoaded) setText(view.snapshotBody, 'Loading snapshots…');
  try {
    const {snapshots} = await api(path(view.container.name) + '/snapshots');
    if (epoch !== view.epoch) return;
    view.snapshotBody.replaceChildren(); setText(view.snapshotTitle, 'Snapshots (' + snapshots.length + ')');
    if (!snapshots.length) view.snapshotBody.append(node('p', 'No snapshots yet.', 'hint'));
    for (const name of snapshots) {
      const row = node('div', undefined, 'snapshot');
      row.append(node('span', name), button('Delete', () => mutate(view.container.name, 'delete-snapshot', name), 'danger')); view.snapshotBody.append(row);
    }
    view.snapshotsLoaded = true;
  } catch (error) { notify('Snapshots: ' + error.message, true); if (!view.snapshotsLoaded) setText(view.snapshotBody, 'Unable to load snapshots. Try again.'); }
  finally { view.snapshotRefresh.disabled = false; }
}
async function loadLogs(view) {
  if (view.logRefresh.disabled) return;
  view.logRefresh.disabled = true;
  try { const data = await api(path(view.container.name) + '/console'); setText(view.logBody, data.console || 'No log output.'); view.logsLoaded = true; }
  catch (error) { notify('Logs: ' + error.message, true); }
  finally { view.logRefresh.disabled = false; }
}
function reconcile(containers) {
  const names = new Set(containers.map(c => c.name));
  for (const [name, view] of views) if (!names.has(name) && !busy.has(name)) {
    if (view.el.contains(document.activeElement)) $('refresh').focus();
    view.epoch++; view.el.remove(); views.delete(name);
  }
  for (const container of containers) {
    let view = views.get(container.name);
    if (!view) { view = createView(container); views.set(container.name, view); $('containers').append(view.el); }
    else if (!busy.has(container.name)) updateView(view, container);
  }
  $('empty').hidden = views.size > 0;
  setText($('empty'), 'No containers yet. Create your first container to get started.');
  setText($('total'), containers.length);
  setText($('running'), containers.filter(c => c.status === 'RUNNING').length);
  setText($('stopped'), containers.filter(c => c.status === 'STOPPED').length);
}
async function refresh() {
  if (refreshTask) { refreshAgain = true; return refreshTask; }
  refreshTask = (async () => {
    do {
      refreshAgain = false; $('refresh').disabled = true; const version = mutationVersion;
      const healthJob = api('/health').then(health => {
        setText($('health'), health.lxc_accessible ? '● Connected' : '● LXC unavailable');
        $('health').className = health.lxc_accessible ? 'connection good' : 'connection';
        $('health').title = health.error || '';
      }).catch(error => { setText($('health'), '● Offline'); $('health').className = 'connection'; $('health').title = error.message; });
      try {
        const containers = await api('/containers');
        if (version === mutationVersion) {
          reconcile(containers);
          setText($('updated'), 'Updated ' + new Date().toLocaleTimeString()); $('updated').className = 'hint';
          const pending = [...views.values()]; let index = 0;
          await Promise.all(Array.from({length: Math.min(3, pending.length)}, async () => { while (index < pending.length) await loadMetrics(pending[index++]); }));
        } else refreshAgain = true;
      } catch (error) {
        setText($('updated'), 'Connection lost · showing last data'); $('updated').className = 'hint stale'; $('updated').title = error.message;
        if (!views.size) setText($('empty'), 'Unable to load containers. Use Refresh to try again.');
        for (const view of views.values()) { view.stale = true; renderMetrics(view, true); }
      }
      await healthJob;
    } while (refreshAgain);
  })();
  try { await refreshTask; } finally { refreshTask = null; $('refresh').disabled = false; }
}
async function mutate(name, action, snapshot) {
  if (busy.has(name)) return;
  const deleting = action === 'delete' || action === 'delete-snapshot';
  if (deleting && !confirm(action === 'delete' ? 'Permanently delete ' + name + ' and its filesystem? A running container will be stopped.' : 'Permanently delete snapshot ' + snapshot + ' for ' + name + '?')) return;
  const labels = {start:'Starting…',stop:'Stopping…',restart:'Restarting…',snapshot:'Snapshotting…',delete:'Deleting…','delete-snapshot':'Deleting…'};
  const view = views.get(name);
  busy.set(name, labels[action]); mutationVersion++;
  if (view) { view.epoch++; updateView(view, view.container); }
  let url = path(name);
  if (action === 'delete-snapshot') url += '/snapshots/' + encodeURIComponent(snapshot);
  else if (action !== 'delete') url += '/' + action;
  try {
    await api(url, {method: deleting ? 'DELETE' : 'POST'});
    notify(name + ': ' + action.replaceAll('-', ' ') + ' completed.');
    if (view && (action === 'snapshot' || action === 'delete-snapshot')) {
      view.snapshotsLoaded = false;
      if (view.snapshots.open) await loadSnapshots(view);
    }
  } catch (error) { notify(error.message, true); }
  finally {
    busy.delete(name); mutationVersion++;
    if (view) { view.epoch++; updateView(view, view.container); }
    await refresh();
  }
}
function updateCreateButton() { $('create').disabled = creating || $('release').disabled || !$('release').value; setText($('create'), creating ? 'Creating…' : 'Create container'); }
async function loadReleases() {
  const request = ++releaseRequest;
  const previous = $('release').value;
  $('release').disabled = true; $('retry-releases').hidden = true; setText($('release-status'), 'Loading available releases…'); updateCreateButton();
  try {
    const data = await api('/images/' + encodeURIComponent($('distro').value) + '/releases?arch=' + encodeURIComponent($('arch').value));
    if (request !== releaseRequest) return;
    $('release').replaceChildren(...data.releases.map(release => { const option = node('option', release); option.value = release; return option; }));
    if (!data.releases.length) { const option = node('option', 'No releases available'); option.value = ''; $('release').append(option); }
    else { $('release').value = data.releases.includes(previous) ? previous : data.releases.includes(data.default_release) ? data.default_release : data.releases[0]; $('release').disabled = false; }
    setText($('release-status'), data.releases.length ? data.releases.length + ' releases available.' : 'No images available for this distribution and architecture.');
  } catch (error) { if (request !== releaseRequest) return; setText($('release-status'), error.message); $('retry-releases').hidden = false; }
  finally { if (request === releaseRequest) updateCreateButton(); }
}
async function loadImages() {
  $('retry-images').hidden = true;
  try {
    const {images} = await api('/images');
    $('distro').replaceChildren(...images.map(image => { const option = node('option', image.distro); option.value = image.distro; return option; }));
    if (images.some(i => i.distro === 'alpine')) $('distro').value = 'alpine';
    $('distro').disabled = images.length === 0;
    if (images.length) await loadReleases();
    else { setText($('release-status'), 'No distributions available.'); $('retry-images').hidden = false; }
  } catch (error) { setText($('release-status'), 'Image catalog: ' + error.message); $('retry-images').hidden = false; }
}
async function runDoctor(name) {
  doctorName = name; const request = ++doctorRequest; openDrawer('doctor-drawer');
  setText($('doctor-title'), name ? 'Diagnose ' + name : 'Host diagnostics'); $('rerun-doctor').disabled = true;
  const results = $('doctor-results'); results.replaceChildren(node('p', 'Checking setup…', 'hint'));
  try {
    const report = await api('/doctor' + (name ? '?name=' + encodeURIComponent(name) : ''));
    if (request !== doctorRequest) return;
    results.replaceChildren();
    for (const check of report.checks) {
      const item = node('div', undefined, 'check');
      item.append(node('span', check.status.toUpperCase(), 'badge'), node('p', check.message, check.status === 'error' ? 'danger' : ''));
      if (check.path) item.append(node('code', check.path));
      if (check.remediation) item.append(node('pre', check.remediation), node('p', check.remediation_note, 'hint'));
      results.append(item);
    }
    results.append(node('p', report.scope, 'hint'));
  } catch (error) { if (request === doctorRequest) results.replaceChildren(node('p', error.message, 'danger')); }
  finally { if (request === doctorRequest) $('rerun-doctor').disabled = false; }
}
function init() {
  $('logout').addEventListener('click', async () => { try { await api('/auth/logout', {method: 'POST'}); location.assign('/login'); } catch (error) { notify(error.message, true); } });
  $('open-create').addEventListener('click', () => openDrawer('create-drawer'));
  document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => $(b.dataset.close).close()));
  $('dismiss-notice').addEventListener('click', () => { $('notice').hidden = true; });
  $('shell-config-form').addEventListener('submit', event => {
    event.preventDefault();
    const custom = $('shell-custom').value.trim(); const shell = custom || $('shell-choice').value;
    if (!pendingShellName || !/^\/[A-Za-z0-9._+@%/-]+$/.test(shell)) { setText($('shell-config-status'), 'Use an absolute executable path without arguments.'); return; }
    $('shell-config-drawer').close(); createShellSession(pendingShellName, shell); pendingShellName = null;
  });
  $('shell-minimize').addEventListener('click', () => { shellDockMinimized = !shellDockMinimized; renderShellTabs(); persistShellState(); });
  $('shell-close-all').addEventListener('click', () => { if (shellSessions.size && confirm('Close all terminal sessions?')) [...shellSessions.keys()].forEach(closeShellSession); });
  $('shell-resize').addEventListener('pointerdown', event => {
    event.preventDefault(); const startY = event.clientY, startHeight = shellDockHeight;
    const move = moveEvent => setDockHeight(startHeight + startY - moveEvent.clientY);
    const stop = () => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', stop); };
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', stop);
  });
  $('web-form').addEventListener('submit', event => {
    event.preventDefault(); const port = Number($('web-port').value);
    if (!Number.isInteger(port) || port < 1 || port > 65535 || !webName) { setText($('web-status'), 'Enter a port from 1 to 65535.'); return; }
    window.open(`/containers/${encodeURIComponent(webName)}/web/${port}/`, '_blank', 'noopener'); $('web-drawer').close();
  });
  $('doctor').addEventListener('click', () => runDoctor());
  $('rerun-doctor').addEventListener('click', () => runDoctor(doctorName));
  $('refresh').addEventListener('click', refresh);
  $('distro').addEventListener('change', loadReleases); $('arch').addEventListener('change', loadReleases);
  $('retry-releases').addEventListener('click', loadReleases); $('retry-images').addEventListener('click', loadImages);
  $('create-form').addEventListener('submit', async event => {
    event.preventDefault(); if (creating || $('release').disabled || !$('release').value) return;
    const data = Object.fromEntries(new FormData(event.target)); creating = true; updateCreateButton();
    event.target.querySelectorAll('input,select').forEach(el => el.disabled = true);
    try {
      await api('/containers', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      $('name').value = ''; $('create-drawer').close(); notify(data.name + ' created.'); mutationVersion++; await refresh();
    } catch (error) { setText($('release-status'), error.message); notify(error.message, true); }
    finally { creating = false; event.target.querySelectorAll('input,select').forEach(el => el.disabled = false); updateCreateButton(); }
  });
  refresh().then(restoreShellSessions); loadImages();
  setInterval(() => { if (!document.hidden && $('auto-refresh').checked && !refreshTask) refresh(); }, 10000);
}
init();
