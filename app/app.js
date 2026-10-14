/* Loop Deck — vanilla-JS SPA over status.json (schema 3) + history.jsonl.
   Loopback-only surface. A0: copy-paste affordances (never executed by the page). A2 (the action
   tier): the SAME origin serves a gated POST /act (deck-server.py) — static verb
   whitelist, loop-gate.sh checked, append-only audit; on a plain static server the live
   buttons degrade to a clear error toast.
   All dynamic text goes through textContent (crontab strings are untrusted for DOM purposes). */
'use strict';

let S = { st: null, hist: [], act: null };   // act: GET __act result (true only when enabled)
const F = { g: '', s: '', q: '' };            // Loops-tab filters (persist across renders)
const SORT = { k: 'sev', dir: -1 };
const SEV = { RED: 5, STALE: 4, UNKNOWN: 4, GREEN: 3, LIVE: 3, SCHED: 2, NA: 1, 'NO-DATA': 1, DISABLED: 0 };
const STATUS_SYM = { GREEN: '●', LIVE: '●', STALE: '◐', RED: '✕', SCHED: '◔', NA: '—', UNKNOWN: '?',
                     'NO-DATA': '○', DISABLED: '⊘' };
const LETTER_NAME = { G: 'GREEN', L: 'LIVE', S: 'STALE', R: 'RED', C: 'SCHED', N: 'NA', U: 'UNKNOWN',
                      E: 'NO-DATA', D: 'DISABLED' };
const HARNESS = () => S.st && S.st.mode === 'harness';
const MAXATT = () => (S.st && S.st.max_attempts) || 3;      // ledger threshold (status.json), fleet default 3
const NORUNS = () => (S.st && S.st.runs_total ? 'no row has runs yet' : 'no runs recorded yet');

/* ---------- tiny DOM helpers (safe by construction) ---------- */
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === 'cls') n.className = v;
    else if (k === 'onclick') n.addEventListener('click', v);
    else if (k === 'onkeydown') n.addEventListener('keydown', v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat(9)) {
    if (kid == null) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}
const $ = s => document.querySelector(s);
function chip(st) { return el('span', { cls: 'chip ' + st }, `${STATUS_SYM[st] || ''} ${st}`); }
function fmtAge(m) {
  if (m == null) return '—';
  if (m < 90) return `${Math.round(m)}m`;
  if (m < 2880) return `${(m / 60).toFixed(1)}h`;
  return `${(m / 1440).toFixed(1)}d`;
}
const rel = e => fmtAge((Date.now() / 1000 - e) / 60) + ' ago';
const abs = e => new Date(e * 1000).toISOString().slice(0, 16) + 'Z';
const shq = s => `'${String(s).replace(/'/g, `'\\''`)}'`;   // single-quote for shell paste

/* ---------- tooltip ---------- */
const tip = () => $('#tooltip');
function showTip(x, y, text) {
  const t = tip(); t.textContent = text; t.hidden = false;
  const r = t.getBoundingClientRect();
  t.style.left = Math.min(x + 12, innerWidth - r.width - 8) + 'px';
  t.style.top = Math.min(y + 12, innerHeight - r.height - 8) + 'px';
}
function hideTip() { tip().hidden = true; }

/* ---------- clipboard action buttons (the page itself never executes anything) ---------- */
function copyBtn(label, text, danger) {
  const b = el('button', { cls: 'act' + (danger ? ' danger' : ''), title: text }, label);
  b.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); }
    catch (_) {
      const ta = el('textarea', {}, text); document.body.append(ta);
      ta.select(); document.execCommand('copy'); ta.remove();
    }
    const old = b.textContent; b.textContent = '✓ copied'; b.classList.add('copied');
    setTimeout(() => { b.textContent = old; b.classList.remove('copied'); }, 1200);
  });
  return b;
}

/* ---------- A2 actions (same-origin /act; deck-server.py enforces token + gate + audit; OFF unless LOOP_DECK_ACT_TOKEN) ---------- */
let toastEl = null;
function toast(msg, ok) {
  if (!toastEl) { toastEl = el('div', { id: 'toast' }); document.body.append(toastEl); }
  toastEl.textContent = msg; toastEl.className = ok ? 'ok' : 'bad'; toastEl.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { toastEl.hidden = true; }, 3500);
}
/* Act token: the server refuses /act unless LOOP_DECK_ACT_TOKEN is set; the operator types the token
   once per tab (sessionStorage, never in a URL or a GET). URLs stay RELATIVE for path-prefix proxies. */
const TOKEN_KEY = 'loopDeckActToken';
function actToken() {
  try {
    let t = sessionStorage.getItem(TOKEN_KEY);
    if (!t) {
      t = window.prompt('Loop Deck action token (the server\'s LOOP_DECK_ACT_TOKEN):') || '';
      if (t) sessionStorage.setItem(TOKEN_KEY, t);
    }
    return t;
  } catch (_) { return ''; }
}
async function act(verb, slug, reason) {
  const tok = actToken();
  if (!tok) { toast(`${verb}: no action token entered — nothing was sent`, false); return; }
  try {
    const r = await fetch('act', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Loop-Deck-Token': tok },
      body: JSON.stringify({ verb, slug, reason }) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 403) { try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* no storage */ } }
    toast(`${verb}: ${j.msg || 'HTTP ' + r.status}`, r.ok);
    if (r.ok) setTimeout(() => load().then(render), 2000);   // pick up regenerated data / ack
  } catch (_) {
    toast(`${verb}: act unreachable — server is running in static (A0-only) mode`, false);
  }
}
async function loadAct() {      // learn whether actions exist from GET __act; never by POSTing
  try {
    const r = await fetch('__act', { cache: 'no-store' });
    const j = await r.json();
    S.act = r.ok && j.enabled === true;
  } catch (_) { S.act = false; }
}
const actNote = () => el('div', { cls: 'mut actnote' },
  'actions disabled in this deployment — this Deck is read-only (the server reports __act enabled:false)');
function actBtn(label, verb, opts) {
  if (S.act !== true) return null;            // hidden unless the server reports enabled
  const b = el('button', { cls: 'act live' + (opts && opts.danger ? ' danger' : '') }, label);
  b.addEventListener('click', () => {
    if (opts && opts.confirm && !window.confirm(opts.confirm)) return;
    act(verb, opts && opts.slug, opts && opts.reason);
  });
  return b;
}

/* ---------- data ---------- */
async function load() {
  const r = await fetch('status.json', { cache: 'no-store' });
  S.st = await r.json();
  try {
    const h = await fetch('history.jsonl', { cache: 'no-store' });
    S.hist = (await h.text()).split('\n').filter(Boolean)
      .map(l => { try { return JSON.parse(l); } catch (_) { return null; } })  // one bad line
      .filter(Boolean);                                    // must not drop the whole history
  } catch (_) { S.hist = []; }
}
async function poll() {
  try {
    const r = await fetch('status.json', { cache: 'no-store' });
    const st = await r.json();
    if (st.generated_epoch !== S.st.generated_epoch) { await load(); render(); }
    else renderChrome();                       // freshness pill still ages
  } catch (_) { /* server briefly away; next poll retries */ }
}

/* ---------- chrome: topbar + drift banner + tabs ---------- */
const TABS = [['', 'Overview'], ['loops', 'Loops'], ['topology', 'Topology'],
              ['breakers', 'Breakers'], ['history', 'History'], ['runbook', 'Runbook']];
function renderChrome() {
  const st = S.st, ageMin = (Date.now() / 1000 - st.generated_epoch) / 60;
  const pill = el('span', { cls: 'pill' + (ageMin > 90 ? ' bad' : ageMin > 40 ? ' warn' : '') },
    `data ${fmtAge(ageMin)} old · window ${(st.window_min / 60).toFixed(0)}h · ` +
    (HARNESS() ? `${st.runs_total} run records` : `${st.cron_entries} crons`));
  const bar = $('#topbar'); bar.replaceChildren(
    el('span', { cls: 'wordmark' }, 'LOOP', el('span', { cls: 'dim' }, ' DECK')),
    el('span', { cls: 'hero' },
      el('span', { cls: 'pct' }, st.health_pct == null ? '—' : `${st.health_pct}%`),
      el('span', { cls: 'sub' }, st.denom ? `${st.healthy}/${st.denom} loops healthy` : NORUNS())),
    pill);
  const banners = [];
  if (st.paused) banners.push(el('div', { cls: 'banner bad pausedbar' },   // every view (chrome)
    '⏸ PAUSED' + (st.pause_reason ? ` — ${st.pause_reason}` : '')));
  if (st.drift) {
    banners.push(el('div', { cls: 'banner bad' },
      '⚠ REGISTRY-DRIFT detected (loop-sync)',
      el('ul', {},
        st.drift_missing.map(x => el('li', {}, `registry loop with no crontab entry: ${x}`)),
        st.drift_unregistered.map(x => el('li', {}, 'unregistered cron: ', el('code', {}, x.slice(0, 160)))),
        (st.unclaimed_tags || []).map(x => el('li', {}, 'runs.jsonl tag claimed by no registry row: ',
          el('code', {}, x.slice(0, 160)))))));
  }
  $('#driftbar').replaceChildren(...banners);
  const cur = route()[0];
  $('#tabs').replaceChildren(...TABS.map(([r, label]) =>
    el('a', { href: '#/' + r, cls: cur === r ? 'on' : '' }, label)));
}
const route = () => (location.hash.replace(/^#\/?/, '')).split('/');

/* ---------- widgets ---------- */
function tile(n, label, color) {
  return el('div', { cls: 'tile' },
    color ? el('div', { cls: 'bar', style: `background:${color}` }) : null,
    el('div', { cls: 'n' }, String(n)), el('div', { cls: 'l' }, label));
}
function stripFor(slug, hours, maxCells) {
  const cut = Date.now() / 1000 - hours * 3600;
  const cells = S.hist.filter(l => l.e >= cut && l.s && l.s[slug]).slice(-(maxCells || 96));
  if (!cells.length) return el('span', { cls: 'mut' }, S.hist.length ? 'collecting…' : 'no history yet');
  const d = el('div', { cls: 'strip', role: 'img',
                        'aria-label': `uptime strip, last ${hours}h, ${cells.length} samples` });
  for (const l of cells) {
    const c = el('i', { cls: 'cell ' + l.s[slug] });
    c.addEventListener('mousemove', ev => showTip(ev.clientX, ev.clientY,
      `${l.ts} — ${LETTER_NAME[l.s[slug]] || l.s[slug]}`));
    c.addEventListener('mouseleave', hideTip);
    d.append(c);
  }
  return d;
}
function svgEl(tag, attrs) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
}
/* fire timeline: expected-fire grid (faint) + actual fires (accent ticks) over the window */
function fireTimeline(cron, windowMin) {
  const W = 720, H = 44, L = 8, R = 712, now = S.st.generated_epoch, t0 = now - windowMin * 60;
  const x = t => L + (t - t0) / (windowMin * 60) * (R - L);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, 'aria-label': 'fire timeline' });
  svg.append(svgEl('line', { x1: L, y1: 32, x2: R, y2: 32, class: 'axis' }));
  if (cron.period_min && cron.period_min <= windowMin && cron.period_min >= windowMin / 200) {
    for (let t = now; t >= t0; t -= cron.period_min * 60)
      svg.append(svgEl('line', { x1: x(t), y1: 27, x2: x(t), y2: 32, class: 'expected' }));
  }
  for (const e of cron.fire_epochs || []) {
    const fx = x(Math.max(e, t0));
    const tick = svgEl('line', { x1: fx, y1: 14, x2: fx, y2: 32, class: 'fire' });
    const hit = svgEl('rect', { x: fx - 4, y: 8, width: 8, height: 28, fill: 'transparent' });
    hit.addEventListener('mousemove', ev => showTip(ev.clientX, ev.clientY, `fired ${abs(e)} (${rel(e)})`));
    hit.addEventListener('mouseleave', hideTip);
    svg.append(tick, hit);
  }
  const labL = svgEl('text', { x: L, y: 42, class: 'lab' }); labL.textContent = `−${(windowMin / 60).toFixed(0)}h`;
  const labR = svgEl('text', { x: R, y: 42, class: 'lab', 'text-anchor': 'end' }); labR.textContent = 'now';
  svg.append(labL, labR);
  return el('div', { cls: 'chart' },
    el('div', { cls: 'mut' },
      `fires (launch evidence, syslog) — ${cron.fires_window} actual` +
      (cron.expected != null ? ` / ${cron.expected} expected · ${cron.missed} missed · punctuality ${cron.punctuality_pct}%` : ' · no period-based expectation')),
    svg);
}
/* fleet health trend from history */
function healthChart() {
  const hs = S.hist.filter(h => h.health != null);     // harness: a sample without rows-with-runs has none
  if (!S.hist.length) return el('div', { cls: 'quiet' }, 'no history yet');
  if (hs.length < 2) return el('div', { cls: 'quiet' },
    `health trend appears after ≥2 samples — collecting since ${hs.length ? hs[0].ts : 'first cron run'}`);
  const W = 720, H = 170, L = 34, R = 712, T = 10, B = 148;
  const stride = Math.max(1, Math.floor(hs.length / 600));
  const pts = hs.filter((_, i) => i % stride === 0 || i === hs.length - 1);
  const t0 = pts[0].e, t1 = pts[pts.length - 1].e || t0 + 1;
  const x = e => L + (e - t0) / Math.max(1, t1 - t0) * (R - L);
  const y = h => B - (h / 100) * (B - T);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, 'aria-label': 'fleet health trend' });
  for (const g of [0, 50, 100]) {
    svg.append(svgEl('line', { x1: L, y1: y(g), x2: R, y2: y(g), class: 'gridline' }));
    const lab = svgEl('text', { x: L - 6, y: y(g) + 3, class: 'lab', 'text-anchor': 'end' });
    lab.textContent = g; svg.append(lab);
  }
  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.e).toFixed(1)},${y(p.health).toFixed(1)}`).join('');
  svg.append(svgEl('path', { d: `${path}L${x(t1)},${B}L${x(t0)},${B}Z`, class: 'harea' }));
  svg.append(svgEl('path', { d: path, class: 'hline' }));
  const cross = svgEl('line', { y1: T, y2: B, class: 'crosshair', visibility: 'hidden' });
  svg.append(cross);
  svg.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * W;
    const ex = Math.min(t1, Math.max(t0, t0 + (px - L) / (R - L) * (t1 - t0)));
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.e - ex) < Math.abs(best.e - ex)) best = p;
    cross.setAttribute('x1', x(best.e)); cross.setAttribute('x2', x(best.e));
    cross.setAttribute('visibility', 'visible');
    showTip(ev.clientX, ev.clientY,
      `${best.ts} — health ${best.health}%${best.drift ? ' · DRIFT' : ''} · ` +
      Object.entries(best.c || {}).map(([k, v]) => `${k}:${v}`).join(' '));
  });
  svg.addEventListener('mouseleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
  const labL = svgEl('text', { x: L, y: H - 6, class: 'lab' }); labL.textContent = pts[0].ts;
  const labR = svgEl('text', { x: R, y: H - 6, class: 'lab', 'text-anchor': 'end' });
  labR.textContent = pts[pts.length - 1].ts;
  svg.append(labL, labR);
  return el('div', { cls: 'chart' }, el('div', { cls: 'mut' }, 'fleet health % (single series)'), svg);
}
/* RED episodes + MTTR from history */
function episodes() {
  const eps = [], open = {};
  for (const l of S.hist) {
    if (!l.s) continue;
    for (const [slug, letter] of Object.entries(l.s)) {
      if (letter === 'R' && !open[slug]) open[slug] = { slug, start: l.e, startTs: l.ts };
      else if (letter !== 'R' && open[slug]) {
        eps.push({ ...open[slug], end: l.e, endTs: l.ts }); delete open[slug];
      }
    }
  }
  for (const o of Object.values(open)) eps.push({ ...o, end: null });
  return eps.sort((a, b) => b.start - a.start);
}

/* ---------- views ---------- */
function loopAgg(l) {
  // actExp = fires from crons that HAVE an expectation (drives punct and the n/exp ratio);
  // act = all fires (shown alone when no expectation exists) — must never mix (checker finding)
  const ages = l.crons.map(c => c.age_min).filter(a => a != null);
  if (!l.crons.length && l.last_run) ages.push((S.st.generated_epoch - Date.parse(l.last_run.ts) / 1000) / 60);
  const exp = l.crons.reduce((s, c) => s + (c.expected || 0), 0);
  const actExp = l.crons.reduce((s, c) => s + (c.expected != null ? c.fires_window : 0), 0);
  return { age: ages.length ? Math.min(...ages) : null, exp, actExp,
           act: l.crons.reduce((s, c) => s + c.fires_window, 0),
           punct: exp ? Math.min(100, Math.round(100 * actExp / exp)) : null };
}
function vOverview() {
  const st = S.st, hot = st.breakers.filter(b => b.state === 'CIRCUIT-BROKEN').length;
  const red = st.counts.RED || 0, stale = st.counts.STALE || 0;
  const v = el('div', {},
    el('div', { cls: 'tiles' },
      st.health_pct == null ? tile('—', `health — ${NORUNS()}`) :
      tile(`${st.health_pct}%`, HARNESS() ? 'health (GREEN / rows with runs)' : 'fleet health (GREEN+LIVE / registered−NA)',
        st.health_pct >= 95 ? 'var(--green)' : st.health_pct >= 80 ? 'var(--amber)' : 'var(--red)'),
      tile(red, 'RED now', red ? 'var(--red)' : null),
      tile(stale, 'STALE now', stale ? 'var(--amber)' : null),
      st.missed_fires_total == null ? tile('—', 'missed — no runs recorded yet') :
      tile(st.missed_fires_total, HARNESS() ? 'rows missed (no run within 6× cadence)' :
        `missed fires (${(st.window_min / 60).toFixed(0)}h window)`, st.missed_fires_total ? 'var(--amber)' : null),
      tile(hot, 'circuit-broken items', hot ? 'var(--red)' : null),
      HARNESS() ? tile(st.drift ? st.unclaimed_tags.length : st.runs_total ? 'in sync' : '—',
        st.runs_total || st.drift ? 'registry ↔ runs.jsonl tags' : 'registry ↔ runs.jsonl — no runs recorded yet',
        st.drift ? 'var(--red)' : st.runs_total ? 'var(--green)' : null) :
      tile(st.drift ? st.drift_missing.length + st.drift_unregistered.length : 'in sync',
        'registry ↔ crontab (loop-sync)', st.drift ? 'var(--red)' : 'var(--green)'),
      st.escalations ? tile(st.escalations.last_7d, 'escalations logged (7d)',
        st.escalations.last_7d ? 'var(--amber)' : null) : null),
    el('h2', {}, 'Needs attention'));
  if (!st.attention.length) v.append(el('div', { cls: 'quiet' }, st.denom === 0 ?
    `${st.loops.length} rows registered — ${NORUNS()}` : '✔ all clear — no RED or STALE loops'));
  else {
    const box = el('div', { cls: 'attn' });
    for (const slug of st.attention) {
      const l = st.loops.find(x => x.slug === slug); if (!l) continue;
      const log = l.crons.find(c => c.log)?.log;
      box.append(el('div', { cls: 'row' }, chip(l.status),
        el('a', { href: '#/loop/' + l.slug }, l.name),
        el('span', { cls: 'why' }, (l.detail[0] || '').slice(0, 140)),
        log ? copyBtn('copy: tail log', `tail -n 100 -- ${shq(log)}`) : null));
    }
    v.append(box);
  }
  v.append(el('h2', {}, 'Groups'), el('div', { cls: 'gcards' }, S.st.groups.map(g => {
    const ls = g.slugs.map(s => st.loops.find(l => l.slug === s)).filter(Boolean);
    const byStatus = {};
    for (const l of ls) byStatus[l.status] = (byStatus[l.status] || 0) + 1;
    return el('div', { cls: 'gcard' },
      el('h4', {}, `${g.key}. ${g.title}`),
      el('div', { cls: 'mini' }, Object.entries(byStatus).sort((a, b) => SEV[b[0]] - SEV[a[0]])
        .map(([s, n]) => el('span', { cls: 'chip ' + s }, `${STATUS_SYM[s]} ${s} ${n}`))),
      el('div', { style: 'margin-top:8px' }, ls.filter(l => !['GREEN', 'LIVE'].includes(l.status))
        .map(l => el('div', { cls: 'mut' }, '· ', el('a', { href: '#/loop/' + l.slug }, l.name),
          ` — ${l.status}`))));
  })));
  return v;
}
function vLoops() {
  const st = S.st;
  const sel = (opts, val, set) => {
    const s = el('select', {}, opts.map(([v, t]) => {
      const o = el('option', { value: v }, t); if (v === val) o.selected = true; return o;
    }));
    s.addEventListener('change', () => { set(s.value); render(); });
    return s;
  };
  const inp = el('input', { placeholder: 'search name / driver / cadence…', value: F.q });
  inp.addEventListener('input', () => { F.q = inp.value; renderRows(); });
  let rows = st.loops.filter(l =>
    (!F.g || l.group === F.g) && (!F.s || l.status === F.s));
  const q = () => F.q.trim().toLowerCase();
  const match = l => !q() || `${l.name} ${l.driver} ${l.cadence} ${l.pattern}`.toLowerCase().includes(q());
  const cols = [['sev', 'Status'], ['name', 'Loop'], ['group', 'Grp'], ['cadence', 'Cadence'],
                ['age', 'Last fire'], ['fires', `Fires ${(st.window_min / 60).toFixed(0)}h`],
                ['punct', 'Punct'], ['strip', 'Uptime (24h)']];
  const keyf = { sev: l => SEV[l.status], name: l => l.name.toLowerCase(), group: l => l.group,
                 cadence: l => l.cadence, age: l => loopAgg(l).age ?? 9e9,
                 fires: l => loopAgg(l).act, punct: l => loopAgg(l).punct ?? -1, strip: l => 0 };
  const tbody = el('tbody');
  function renderRows() {
    const sorted = rows.filter(match).sort((a, b) => {
      const ka = keyf[SORT.k](a), kb = keyf[SORT.k](b);
      return (ka < kb ? -1 : ka > kb ? 1 : 0) * SORT.dir;
    });
    tbody.replaceChildren(...sorted.map(l => {
      const a = loopAgg(l);
      const tr = el('tr', { cls: 'rowlink', tabindex: '0', role: 'link',
        onclick: () => { location.hash = '#/loop/' + l.slug; },
        onkeydown: ev => { if (ev.key === 'Enter') location.hash = '#/loop/' + l.slug; } },
        el('td', {}, chip(l.status)),
        el('td', {}, el('b', {}, l.name), el('div', { cls: 'mut' }, l.driver.slice(0, 90))),
        el('td', {}, l.group),
        el('td', { cls: 'mono' }, l.cadence.slice(0, 24)),
        el('td', { cls: 'num' }, fmtAge(a.age)),
        el('td', { cls: 'num' }, l.crons.length ? (a.exp ? `${a.actExp}/${a.exp}` : String(a.act)) :
          l.run_success ? String(l.run_success.runs) : '—'),
        el('td', { cls: 'num' }, a.punct != null ? a.punct + '%' : '—'),
        el('td', {}, stripFor(l.slug, 24)));
      return tr;
    }));
  }
  renderRows();
  return el('div', {},
    el('div', { cls: 'filters' },
      sel([['', 'All groups'], ...st.groups.map(g => [g.key, `${g.key}. ${g.title.slice(0, 30)}`])],
        F.g, v => { F.g = v; }),
      sel([['', 'All statuses'], ...Object.keys(SEV).map(s => [s, s])], F.s, v => { F.s = v; }),
      inp),
    el('div', { cls: 'tablewrap' }, el('table', {},
      el('thead', {}, el('tr', {}, cols.map(([k, t]) =>
        el('th', { cls: 'sort', onclick: () => {
          SORT.dir = SORT.k === k ? -SORT.dir : -1; SORT.k = k; renderRows();
        } }, t + (SORT.k === k ? (SORT.dir < 0 ? ' ↓' : ' ↑') : ''))))),
      tbody)),
    el('div', { cls: 'mut' }, 'click a row (or Enter) to drill down · uptime strip: full=GREEN · half=STALE · dashed=RED · short gray=no signal — one cell per viz sample (~15m)'));
}
function vDetail(slug) {
  const l = S.st.loops.find(x => x.slug === slug);
  if (!l) return el('div', { cls: 'quiet' }, `no loop "${slug}" in the registry`);
  const v = el('div', {},
    el('div', {}, el('a', { href: '#/loops' }, '← all loops')),
    el('h2', {}, chip(l.status), ' ', l.name, ' ', el('span', { cls: 'aut' }, l.aut)),
    el('div', { cls: 'idgrid' },
      el('span', { cls: 'k' }, 'driver'), el('code', {}, l.driver),
      el('span', { cls: 'k' }, 'group'), el('span', {}, `${l.group}. ${l.group_title}`),
      el('span', { cls: 'k' }, 'cadence'), el('span', { cls: 'mono' }, l.cadence),
      el('span', { cls: 'k' }, 'pattern'), el('span', {}, l.pattern),
      el('span', { cls: 'k' }, 'verify chain'), el('span', {}, l.verify),
      el('span', { cls: 'k' }, 'handoff / gate'), el('span', {}, l.handoff),
      el('span', { cls: 'k' }, 'evidence'), el('span', { cls: 'mono' }, l.detail.join(' · ')),
      l.run_success ? [el('span', { cls: 'k' }, 'runs (24h)'), el('span', { cls: 'mono' },
        `${l.run_success.ok}/${l.run_success.runs} ok (${l.run_success.success_pct == null ? '—' : l.run_success.success_pct + '%'}) · last rc=${l.run_success.last_rc ?? '—'}` +
        (HARNESS() ? ' — runs.jsonl exit codes' : ' — exit-code evidence (run-logged.sh)'))] : null,
      l.last_run ? [el('span', { cls: 'k' }, 'last run'), el('span', { cls: 'mono' },
        `${l.last_run.ts} · rc=${l.last_run.rc} · dur ${l.last_run.dur_s == null ? '—' : l.last_run.dur_s + 's'}` +
        (l.last_run.note ? ` · ${l.last_run.note}` : ''))] : null,
      l.tags ? [el('span', { cls: 'k' }, 'run tags'), el('code', {}, l.tags.join(', '))] : null,
      l.depends_on && l.depends_on.length ? [el('span', { cls: 'k' }, 'depends on'),
        el('span', {}, l.depends_on.map((d, i) => [i ? ', ' : '', el('a', { href: '#/loop/' + d }, d)]))] : null,
      l.runbook_md !== undefined ? [el('span', { cls: 'k' }, 'runbook'),
        el('span', { cls: 'mono' }, l.runbook_md || 'no runbook')] : null,
      l.ack_ts ? [el('span', { cls: 'k' }, 'acknowledged'), el('span', { cls: 'mono' },
        `${l.ack_ts} (via deck — clears after 24h)`)] : null));
  if (l.ledger) v.append(el('div', { cls: 'banner ' + (l.ledger.attempts >= (l.ledger.max || 3) && !l.ledger.resolved ? 'bad' : 'ok') },
    `circuit breaker: ${l.ledger.item} — attempts ${l.ledger.attempts}/${l.ledger.max}` +
    (l.ledger.resolved ? ' (resolved)' : '')));
  v.append(el('h3', {}, 'Uptime'),
    el('div', {}, stripFor(slug, 24), el('span', { cls: 'mut' }, '  24h   ')),
    el('div', { style: 'margin-top:6px' }, stripFor(slug, 168, 336), el('span', { cls: 'mut' }, '  7d')),
    el('div', { cls: 'mut' }, 'full=GREEN · half=STALE · dashed=RED · short gray=no signal (shape + color, never color alone)'));
  if (l.crons.length) {
    v.append(el('h3', {}, `Schedule & fires (${l.crons.length} cron${l.crons.length > 1 ? 's' : ''})`));
    for (const c of l.crons) {
      v.append(el('div', { cls: 'idgrid', style: 'margin:8px 0' },
        el('span', { cls: 'k' }, 'cron'), el('code', {}, `${c.sched}  ${c.cmd.slice(0, 200)}`),
        el('span', { cls: 'k' }, 'last fire'), el('span', { cls: 'mono' },
          c.last_fire_epoch ? `${abs(c.last_fire_epoch)} (${rel(c.last_fire_epoch)})` : 'no syslog evidence'),
        el('span', { cls: 'k' }, 'log'), c.log ? el('code', {}, c.log) : el('span', { cls: 'mut' }, '—')),
        fireTimeline(c, S.st.window_min),
        el('div', { cls: 'actions' },
          c.log ? copyBtn('copy: tail log', `tail -n 100 -- ${shq(c.log)}`) : null,
          copyBtn('copy: run now (manual)', c.cmd)));
    }
    const fires = l.crons.flatMap(c => c.fire_epochs || []).sort((a, b) => b - a).slice(0, 15);
    if (fires.length) v.append(el('h3', {}, 'Recent fires (syslog CRON CMD lines)'),
      el('div', { cls: 'chart mono' }, fires.map(e => el('div', {}, `${abs(e)}  (${rel(e)})`))));
  }
  v.append(...[el('h3', {}, HARNESS() ? 'Actions' : (S.act === true ? 'Actions — A2 enabled (token + gate + audit) · A0 copy-paste' : 'Actions — A2 disabled on this server (no action token) · A0 copy-paste')),
    S.act === true ? el('div', { cls: 'actions' },
      actBtn('↻ rerun data now', 'rerun-viz'),
      actBtn(`✓ ack ${l.slug}`, 'ack', { slug: l.slug })) : actNote(),
    HARNESS() ? null : el('div', { cls: 'actions' },
      copyBtn('copy: card title for Plane', `[loop] ${l.name} — ${l.status}: ${(l.detail[0] || '').slice(0, 100)}`),
      copyBtn('copy: PAUSE whole fleet', 'mkdir -p ~/.loop-deck && touch ~/.loop-deck/LOOP_PAUSE', true)),
    HARNESS() ? null : el('div', { cls: 'mut' },
      'live verbs POST same-origin /act: static whitelist → loop-gate.sh check-action → append-only audit (actions.jsonl, Runbook tab) · persistent REDs auto-card to the kanban board every viz run (A1) · fleet pause/resume: Runbook')]
    .filter(Boolean));   // Node.append(null) would render the text "null"
  return v;
}
function vTopology() {
  const pre = el('pre', { cls: 'mermaid' }, S.st.topology_mmd);
  queueMicrotask(() => runMermaid());
  return el('div', {}, el('h2', {}, HARNESS() ? 'Topology — registry rows and depends_on edges (colored by status)' :
    'Topology — hub → groups → loops (colored by status)'), pre,
    el('div', { cls: 'mut' }, 'labels carry status via table + chips; diagram color is reinforcement only'));
}
function vBreakers() {
  const st = S.st, hot = st.breakers.filter(b => b.state === 'CIRCUIT-BROKEN'), mx = MAXATT();
  return el('div', {},
    el('div', { cls: 'tiles' },
      tile(hot.length, HARNESS() && st.max_attempts == null ? 'circuit-broken (no ledger)' :
        `circuit-broken (${mx}/${mx}, unresolved)`, hot.length ? 'var(--red)' : null),
      tile(st.breakers.filter(b => b.state === 'open').length, 'open (burning attempts)'),
      tile(st.breakers.filter(b => b.state === 'resolved').length, 'resolved', 'var(--green)')),
    st.budget ? el('div', { cls: 'mut' },
      `budget (loop-budget.json): report-only at ${st.budget.policy.report_only_at_pct}% · ` +
      `attempt cap ${st.budget.policy.max_attempts_per_item}/item · token meter: ` +
      (st.budget.meters.token_spend.available ? 'live' : `not available — ${st.budget.meters.token_spend.reason}`)) : null,
    st.breakers.length ? el('div', { cls: 'tablewrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Item', 'Burn', 'Attempts', 'State', 'Note'].map(h => el('th', {}, h)))),
      el('tbody', {}, st.breakers.map(b => el('tr', {},
        el('td', {}, el('code', {}, b.item)),
        el('td', {}, el('span', { cls: 'burn' }, [...Array(mx).keys()].map(i =>
          el('i', { cls: i < b.attempts ? (b.attempts >= mx ? 'hot' : 'warm') : '' })))),
        el('td', { cls: 'num' }, `${b.attempts}/${mx}`),
        el('td', {}, b.state === 'CIRCUIT-BROKEN' ? chip('RED') : b.state === 'resolved' ? chip('GREEN') : chip('STALE'),
          ' ', b.state),
        el('td', { cls: 'mut' }, b.note)))))) : el('div', { cls: 'quiet' }, 'no breakers'),
    el('div', { cls: 'mut' }, HARNESS() ?
      `source: loop-ledger.json — max_attempts ${st.max_attempts ?? '— (no ledger)'} (from the file); an item at the cap is CIRCUIT-BROKEN` :
      'source: loop-ledger.json — maxAttemptsPerItem 3 (gate.yaml); 3/3 unresolved ⇒ escalate, never thrash'));
}
function vHistory() {
  const eps = episodes(), closed = eps.filter(e => e.end);
  const mttr = closed.length ?
    fmtAge(closed.reduce((s, e) => s + (e.end - e.start), 0) / closed.length / 60) : null;
  const driftCells = S.hist.slice(-200);
  return el('div', {},
    el('h2', {}, 'Fleet health trend'), healthChart(),
    el('div', { cls: 'tiles' },
      tile(S.hist.length, 'samples collected (1 per viz run, ~15m)'),
      tile(eps.filter(e => !e.end).length, 'RED episodes ongoing', eps.some(e => !e.end) ? 'var(--red)' : null),
      tile(closed.length, 'RED episodes healed'),
      tile(mttr ?? '—', 'MTTR (mean time to green)', mttr ? 'var(--accent)' : null),
      S.st.state_prunes ? tile(S.st.state_prunes.last_7d, 'STATE lines pruned (7d — ledger keeps them)') : null),
    el('h3', {}, 'Drift timeline (recent samples)'),
    S.hist.length ? el('div', { cls: 'strip' }, driftCells.map(l => {
      const c = el('i', { cls: 'cell ' + (l.drift ? 'R' : 'G') });
      c.addEventListener('mousemove', ev => showTip(ev.clientX, ev.clientY,
        `${l.ts} — ${l.drift ? 'DRIFT' : 'in sync'}`));
      c.addEventListener('mouseleave', hideTip);
      return c;
    })) : el('div', { cls: 'quiet' }, 'no history yet'),
    el('h3', {}, 'RED episodes'),
    eps.length ? el('div', { cls: 'tablewrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Loop', 'Started', 'Duration', 'State'].map(h => el('th', {}, h)))),
      el('tbody', {}, eps.slice(0, 40).map(e => el('tr', {},
        el('td', {}, el('a', { href: '#/loop/' + e.slug }, e.slug)),
        el('td', { cls: 'mono' }, e.startTs),
        el('td', { cls: 'num' }, e.end ? fmtAge((e.end - e.start) / 60) : fmtAge((Date.now() / 1000 - e.start) / 60) + ' +'),
        el('td', {}, e.end ? chip('GREEN') : chip('RED'), ' ', e.end ? 'healed' : 'ongoing'))))))
      : el('div', { cls: 'quiet' }, S.hist.length ?
        `no RED episodes recorded since collection began (${S.hist[0].ts}) — that is the goal` : 'no history yet'));
}
function vRunbookHarness() {
  const st = S.st;
  const rule = (s, t) => el('tr', {}, el('td', {}, chip(s)), el('td', {}, t));
  return el('div', {},
    el('h2', {}, 'Runbook — per loop (registry.json runbook_md)'),
    el('div', { cls: 'tablewrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Loop', 'Owner', 'Runbook'].map(h => el('th', {}, h)))),
      el('tbody', {}, st.loops.map(l => el('tr', {},
        el('td', {}, el('a', { href: '#/loop/' + l.slug }, l.name)),
        el('td', { cls: 'mono' }, l.driver),
        el('td', {}, l.runbook_md ? el('span', { cls: 'mono' }, l.runbook_md) : el('span', { cls: 'mut' }, 'no runbook'))))))),
    el('h3', {}, 'Status rules (harness mode — runs.jsonl against cadence_s)'),
    el('div', { cls: 'tablewrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Status', 'Meaning'].map(h => el('th', {}, h)))),
      el('tbody', {},
        rule('GREEN', 'latest record (by ts) rc 0 within 2× cadence_s · seed: rc 0 at any age'),
        rule('STALE', 'latest rc 0, older than 2× cadence_s (up to 6×)'),
        rule('RED', 'latest rc ≠ 0, or missed: no run for more than 6× cadence_s'),
        rule('NO-DATA', 'no run records for the row yet (excluded from health)'),
        rule('DISABLED', 'enabled: false in registry.json (excluded from health)')))),
    el('h3', {}, 'Controls'),
    S.act === true ? el('div', { cls: 'actions' }, actBtn('↻ rerun data now', 'rerun-viz')) : actNote(),
    el('div', { cls: 'mut' }, 'pause state comes from state.json, written by the adapter; this Deck never writes it'),
    el('h3', {}, 'Raw data'),
    rawLinks(st));
}
const rawLinks = st => el('div', {},           // only files deck-server 1.7.0 serves (STATIC_ALLOW)
  ['status.json', 'history.jsonl', 'acks.jsonl'].map((f, i) => [i ? ' · ' : '', el('a', { href: f }, f)]),
  ' · generated ', st.generated);
function vRunbook() {
  const st = S.st;
  if (HARNESS()) return vRunbookHarness();
  const anat = el('pre', { cls: 'mermaid' }, st.anatomy_mmd);
  queueMicrotask(() => runMermaid());
  return el('div', {},
    el('h2', {}, 'What this surface is for'),
    el('div', { cls: 'chart' },
      '"Can I trust the fleet right now? If not, what exactly rotted, and what is the one action to take?" — the Loop Deck PRD'),
    el('h3', {}, 'Evidence precedence'),
    el('ol', {},
      el('li', {}, 'syslog CRON ', el('code', {}, 'CMD (...)'), ' lines — verbatim command match (LAUNCH evidence)'),
      el('li', {}, 'log-file mtime — fallback only (quiet scripts append nothing; old mtime ≠ dead loop)'),
      el('li', {}, el('code', {}, 'pgrep'), ' for @reboot daemons · ', el('code', {}, 'tmux ls'), ' for session loops · origin/master STATE.md header for the remote fixer (fetched — the local checkout can lag)')),
    el('div', { cls: 'mut' }, st.evidence_note),
    el('h3', {}, 'Status thresholds'),
    el('div', { cls: 'tablewrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Status', 'Meaning'].map(h => el('th', {}, h)))),
      el('tbody', {},
        el('tr', {}, el('td', {}, chip('GREEN')), el('td', {}, 'fired within 2× period (min 10m) · or daemon/tmux alive')),
        el('tr', {}, el('td', {}, chip('STALE')), el('td', {}, 'last fire within 6× period (min 30m)')),
        el('tr', {}, el('td', {}, chip('RED')), el('td', {}, 'beyond 6× period, daemon dead, or registry drift')),
        el('tr', {}, el('td', {}, chip('SCHED')), el('td', {}, 'scheduled; no fire evidence readable yet')),
        el('tr', {}, el('td', {}, chip('LIVE')), el('td', {}, 'session loop with tmux session up')),
        el('tr', {}, el('td', {}, chip('NA')), el('td', {}, 'no mechanical signal defined (hub/session-driven)'))))),
    el('h3', {}, 'Action tiers'),
    el('ul', {},
      el('li', {}, el('b', {}, 'A0 (live): '), 'copy-paste affordances on every loop page — those never execute anything'),
      el('li', {}, el('b', {}, 'A1 (live): '), 'every viz run auto-cards persistent REDs (2+ consecutive samples) to the kanban board — tools/card_red_loops.py, dedup by open-card name'),
      el('li', {}, el('b', {}, S.act === true ? 'A2 (enabled on this server): ' : 'A2 (DISABLED on this server — off since 1.7.0 unless LOOP_DECK_ACT_TOKEN is set): '), 'same-origin POST /act with a static verb whitelist (rerun-viz · pause · resume · ack), every verb through loop-gate.sh check-action, append-only audit')),
    el('h3', {}, 'Fleet controls (A2 — gated, audited, loopback-only)'),
    S.act === true ? el('div', { cls: 'actions' },
      actBtn('⏸ PAUSE fleet', 'pause', { danger: true, reason: 'deck runbook button',
        confirm: 'Pause the ENTIRE fleet? Every loop exits at its next cycle until resumed.' }),
      actBtn('▶ resume fleet', 'resume',
        { confirm: 'Resume the fleet? The pause file is archived with a timestamp (never deleted).' })) : actNote(),
    (st.recent_actions || []).length ? el('div', {},
      el('h3', {}, 'Recent /act audit (last 20 — actions.jsonl is append-only)'),
      el('div', { cls: 'tablewrap' }, el('table', {},
        el('thead', {}, el('tr', {}, ['When', 'Verb', 'Slug', 'Result', 'HTTP'].map(h => el('th', {}, h)))),
        el('tbody', {}, st.recent_actions.slice().reverse().map(a => el('tr', {},
          el('td', { cls: 'mono' }, a.ts),
          el('td', {}, el('code', {}, a.verb)),
          el('td', { cls: 'mono' }, a.slug || '—'),
          el('td', { cls: 'mono' }, a.result),
          el('td', { cls: 'num' }, String(a.http))))))))
      : el('div', { cls: 'mut' }, 'no /act invocations recorded yet — the audit table appears after the first one'),
    el('h3', {}, 'Access & security'),
    el('ul', {},
      el('li', {}, 'LOOPBACK ONLY: ', el('code', {}, 'ssh -L 8091:localhost:8091 <host>'), ' → http://localhost:8091'),
      el('li', {}, 'bind verified 127.0.0.1 every refresh (server killed if wider); CSP self-only; mermaid vendored; zero external runtime fetches'),
      el('li', {}, 'kill switch: ', el('code', {}, 'mkdir -p ~/.loop-deck && touch ~/.loop-deck/LOOP_PAUSE'))),
    el('h3', {}, 'The canonical loop anatomy'), anat,
    el('h3', {}, 'Raw data'),
    rawLinks(st));
}
function runMermaid() {
  if (!window.mermaid) {
    document.querySelectorAll('pre.mermaid').forEach(p => p.insertAdjacentText('afterbegin',
      '[mermaid.min.js not vendored — showing diagram source]\n'));
    return;
  }
  try {
    window.mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
    window.mermaid.run({ querySelector: 'pre.mermaid' });
  } catch (_) { /* diagram source stays visible — tables are authoritative */ }
}

/* ---------- router ---------- */
function render() {
  if (!S.st) return;
  renderChrome();
  const [r, arg] = route();
  const view = { '': vOverview, loops: vLoops, loop: () => vDetail(arg), topology: vTopology,
                 breakers: vBreakers, history: vHistory, runbook: vRunbook }[r] || vOverview;
  $('#view').replaceChildren(view());
  $('#view').focus({ preventScroll: true });
}
Promise.all([load(), loadAct()]).then(render).catch(err => {
  $('#view').replaceChildren(el('div', { cls: 'banner bad' },
    'failed to load status.json — is the generator cron running? ', String(err)));
});
setInterval(poll, 60000);
addEventListener('hashchange', render);
