// spa_render.js — render every Loop Deck view of app/app.js against one status.json in a fake DOM.
// Usage: node tests/spa_render.js <app.js> <status.json> <history.jsonl|-> <act-enabled true|false>
// Prints JSON {chrome, views: {route: {text, liveButtons}}, posts}. Used by tests/test_loop_deck_spa.py.
'use strict';
const fs = require('fs');
const vm = require('vm');
const [appPath, statusPath, histPath, actEnabled] = process.argv.slice(2);

class Text_ {
  constructor(t) { this.t = String(t); }
  get textContent() { return this.t; }
  get nodeType() { return 3; }
}
class Node_ {
  constructor(tag) {
    this.tag = tag; this.kids = []; this.attrs = {}; this.className = ''; this.style = {}; this.listeners = {};
    this.classList = { add: c => { this.className += ' ' + c; }, remove: () => {} };
  }
  get nodeType() { return 1; }
  // like the real DOM: a non-node argument (null included) becomes a Text node, so "null" shows up
  append(...k) { this.kids.push(...k.map(asNode)); }
  replaceChildren(...k) { this.kids = k.map(asNode); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  focus() {}
  remove() {}
  select() {}
  insertAdjacentText(_, t) { this.kids.unshift(new Text_(t)); }
  getBoundingClientRect() { return { width: 0, height: 0, left: 0, top: 0 }; }
  set textContent(t) { this.kids = [new Text_(t)]; }
  get textContent() { return this.kids.map(k => k.textContent).join(''); }
  all() { return [this, ...this.kids.flatMap(k => (k instanceof Node_ ? k.all() : []))]; }
}
const asNode = x => (x instanceof Node_ || x instanceof Text_ ? x : new Text_(String(x)));
const ids = {};
for (const id of ['topbar', 'driftbar', 'tabs', 'view', 'tooltip']) ids['#' + id] = new Node_('div');
const document = {
  createElement: t => new Node_(t), createElementNS: (_, t) => new Node_(t), createTextNode: t => new Text_(t),
  querySelector: s => ids[s] || null, querySelectorAll: () => [], body: new Node_('body'), execCommand: () => true,
};
const files = { 'status.json': fs.readFileSync(statusPath, 'utf8'),
                'history.jsonl': histPath !== '-' && fs.existsSync(histPath) ? fs.readFileSync(histPath, 'utf8') : null };
const posts = [], gets = [];
async function fetch(url, opts) {
  if (opts && opts.method === 'POST') { posts.push(url); return { ok: false, status: 599, json: async () => ({}) }; }
  gets.push(url);
  if (url === '__act') return { ok: true, status: 200, json: async () => ({ enabled: actEnabled === 'true' }) };
  if (files[url] == null) return { ok: false, status: 404, json: async () => { throw new Error('404'); }, text: async () => '' };
  return { ok: true, status: 200, json: async () => JSON.parse(files[url]), text: async () => files[url] };
}
const store = {};
const sandbox = {
  document, fetch, location: { hash: '' }, navigator: {}, console, Date, JSON, Math, Promise,
  sessionStorage: { getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; } },
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {}, addEventListener: () => {},
  queueMicrotask: () => {}, innerWidth: 1000, innerHeight: 800,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, 'utf8'), sandbox, { filename: 'app.js' });

(async () => {
  for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));   // let load() + loadAct() settle
  const st = JSON.parse(files['status.json']);
  const routes = ['', 'loops', 'topology', 'breakers', 'history', 'runbook', ...st.loops.map(l => 'loop/' + l.slug)];
  const out = { views: {}, posts, gets };
  for (const r of routes) {
    sandbox.location.hash = '#/' + r;
    sandbox.render();
    const nodes = ids['#view'].all();
    out.views[r] = {
      text: ids['#view'].textContent,
      liveButtons: nodes.filter(n => n.tag === 'button' && /\blive\b/.test(n.className)).length,
      links: nodes.filter(n => n.tag === 'a' && n.attrs.href && !n.attrs.href.startsWith('#')).map(n => n.attrs.href),
      chrome: ids['#topbar'].textContent + ' | ' + ids['#driftbar'].textContent,
    };
  }
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
