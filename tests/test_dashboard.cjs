// Exercise the production refresh logic with a small DOM adapter; no browser dependencies.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function setup(fetcher = async () => { throw Error('unexpected request'); }) {
  let document;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.listeners = {}; this.hidden = false; this.disabled = false; this._text = ''; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
    replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
    remove() { this.parent.children = this.parent.children.filter(n => n !== this); }
    contains(el) { return this === el || this.children.some(c => c.contains(el)); }
    focus() { document.activeElement = this; }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(event, handler) { this.listeners[event] = handler; }
    querySelectorAll(selector) { return this.children.flatMap(c => [...(selector.split(',').includes(c.tagName) ? [c] : []), ...c.querySelectorAll(selector)]); }
    get value() { return this._value ?? this.children[0]?.value ?? ''; }
    set value(value) { this._value = value; }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  const ids = new Map();
  document = {activeElement: null, getElementById(id) { if (!ids.has(id)) ids.set(id, new Element('div')); return ids.get(id); }, createElement(tag) { return new Element(tag); }};
  const context = vm.createContext({document, fetch: fetcher, AbortSignal, setInterval() {}, confirm: () => true, console});
  const code = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/init\(\);\s*$/, '');
  vm.runInContext(code, context);
  return {run: text => vm.runInContext(text, context), document, ids};
}
const container = {name:'demo',status:'RUNNING',ipv4:'10.0.0.2'};
test('polling preserves row identity, focus, expanded details, logs and form input', () => {
  const app = setup();
  app.run(`reconcile(${JSON.stringify([container])});`);
  const before = app.run('views.get("demo")');
  before.detail.hidden = false; before.snapshots.open = true; before.logBody.textContent = 'Keep these logs'; before.expand.focus();
  app.document.getElementById('name').value = 'unfinished-name';
  app.run(`reconcile(${JSON.stringify([{...container,ipv4:'10.0.0.3'}])});`);
  const after = app.run('views.get("demo")');
  assert.equal(after.el, before.el); assert.equal(app.document.activeElement, before.expand);
  assert.equal(after.detail.hidden, false); assert.equal(after.snapshots.open, true);
  assert.equal(after.logBody.textContent, 'Keep these logs'); assert.equal(after.ip.textContent, '10.0.0.3');
  assert.equal(app.document.getElementById('name').value, 'unfinished-name');
});
test('failed metrics retain previous values and indicate stale data without fetching snapshots', async () => {
  const requests = [];
  const app = setup(async url => { requests.push(url); throw Error('offline'); });
  app.run(`reconcile(${JSON.stringify([container])}); const v=views.get('demo'); v.metrics={cpu_usage_percent:12.5,memory_usage_bytes:1024}; renderMetrics(v);`);
  await app.run("loadMetrics(views.get('demo'))");
  const view = app.run("views.get('demo')");
  assert.equal(view.cpu.value.textContent, '12.5%'); assert.equal(view.memory.value.textContent, '1.0 KiB');
  assert.match(view.cpu.note.textContent, /Stale/);
  assert.deepEqual(requests, ['/containers/demo/metrics?include_snapshots=false']);
});
test('late metrics cannot overwrite a stopped container', async () => {
  let resolve;
  const app = setup(() => new Promise(r => { resolve = r; }));
  app.run(`reconcile(${JSON.stringify([container])});`);
  const pending = app.run("loadMetrics(views.get('demo'))");
  app.run(`reconcile(${JSON.stringify([{...container,status:'STOPPED'}])});`);
  resolve({status:200,ok:true,json:async () => ({cpu_usage_percent:90})}); await pending;
  assert.equal(app.run("views.get('demo').cpu.value.textContent"), '—');
  assert.equal(app.run("views.get('demo').primary.textContent"), 'Start');
});
test('removing a focused container restores focus to Refresh', () => {
  const app = setup(); app.run(`reconcile(${JSON.stringify([container])}); views.get('demo').expand.focus(); reconcile([]);`);
  assert.equal(app.run('views.size'), 0);
  assert.equal(app.document.activeElement, app.document.getElementById('refresh'));
});
test('older release response cannot replace the newest distribution selection', async () => {
  const pending = [];
  const app = setup(() => new Promise(resolve => pending.push(resolve)));
  app.document.getElementById('distro').value = 'alpine'; app.document.getElementById('arch').value = 'amd64';
  const first = app.run('loadReleases()');
  app.document.getElementById('distro').value = 'ubuntu'; const second = app.run('loadReleases()');
  pending[1]({status:200,ok:true,json:async () => ({releases:['noble'],default_release:'noble'})}); await second;
  pending[0]({status:200,ok:true,json:async () => ({releases:['edge'],default_release:'edge'})}); await first;
  assert.equal(app.document.getElementById('release').value, 'noble');
});
