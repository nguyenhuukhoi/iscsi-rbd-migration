// DESIGN-UI 15, 17, 18, 19 and 20: the page logic itself (migui/static/app.js) against a minimal fake DOM and a fake fetch, in node.
// Handlers are fired directly (even on a disabled button, like a second Enter would), so what is tested is the
// guard in busy()/planning()/startRollback(), not the browser's own refusal to click a disabled button. GUARD-5/6
// render the plan content (17) on the confirm screen and in the history detail of a job. GUARD-7/8/9 drive the plan
// stream of 18 (POST /api/plan answered as server-sent events, fed chunk by chunk) and its panel; GUARD-10/11 the
// automatic Doctor of a new session and route('#confirm') without Doctor (19.1); GUARD-12/13 the live lines of the plan
// being made (20): stage, newest line and the "log plan live" details, for a VM and for a volume group. GUARD-14..17
// the multi-select source boxes (21): the arrays of POST /api/list, the HOSTS_MAX lock, Esc / click outside.
//
//   node tests/ui/guards.mjs <repo>      prints PASS/FAIL lines and 'TOTAL pass=N fail=M'
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const repo = process.argv[2] || path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');
const SRC = fs.readFileSync(path.join(repo, 'migui', 'static', 'app.js'), 'utf8');
let pass = 0, fail = 0;
const ok = (label, cond, detail) => {
  if (cond) { pass++; console.log('PASS ' + label); } else { fail++; console.log('FAIL ' + label + ' ' + JSON.stringify(detail || '')); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class FakeEl {
  constructor(tag, id) {
    Object.assign(this, { tagName: tag, id, value: '', textContent: '', disabled: false, checked: false, className: '',
      title: '', href: '', dataset: {}, style: {}, children: [], handlers: {}, scrollTop: 0, scrollHeight: 0 });
    this.childNodes = this.children;
    const set = new Set();
    this.classList = { toggle: (c, on) => { on = on === undefined ? !set.has(c) : on; if (on) set.add(c); else set.delete(c); return on; },
      contains: (c) => set.has(c), add: (c) => set.add(c), remove: (c) => set.delete(c) };
  }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  fire(t) { const ev = { preventDefault() {}, currentTarget: this, target: this }; return Promise.all((this.handlers[t] || []).map((f) => f(ev))); }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { for (const c of cs) this.appendChild(c); }
  replaceChildren() { this.children.length = 0; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); }
  get firstChild() { return this.children[0]; }
  setAttribute(k, v) { this[k] = v; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  scrollIntoView() {}
}
const find = (root, pred) => {
  if (root instanceof FakeEl && pred(root)) return root;
  for (const c of (root && root.children) || []) { const f = find(c, pred); if (f) return f; }
  return null;
};

const INV = { source: { hosts: [{ host: 'cmp1', zone: 'nova', state: 'up', status: 'enabled' }], projects: [{ id: 'p', name: 'admin' }] },
  destination: { hosts: [], projects: [], zones: { compute: ['AZ1'], volume: ['AZ1'] }, flavors: [], volume_types: [] }, conf: {} };
const ROWS = [
  { id: 'vm-1', ident: 'vm-1', name: 'app01', result: 'selected', status: 'ACTIVE', boot: 'bfv', volumes: [], networks: [], state: null },
  { id: 'vm-2', ident: 'vm-2', name: 'app02', result: 'selected', status: 'ACTIVE', boot: 'bfv', volumes: [], networks: [], state: null },
  { id: 'vm-3', ident: 'vm-3', name: 'old', result: 'selected', status: 'SHUTOFF', boot: 'bfv', volumes: [], networks: [],
    state: { dir: '/var/lib/mig-migrate/vm-3', live: true, started: '2026-11-20T00:00:00Z' } },
];

// A fetch Response: JSON, or a text/event-stream whose chunks the test pushes (stream()).
const enc = new TextEncoder();
const jsonRes = (status, data) => ({ status, headers: { get: () => 'application/json' }, json: async () => data });
function stream() {
  const chunks = [];
  let wake = null, ended = false, broken = false;
  const kick = () => { if (wake) { const w = wake; wake = null; w(); } };
  const s = {
    // one SSE event, cut in two chunks in the middle of its data line (the parser must buffer)
    push(name, data) {
      const txt = 'event: ' + name + '\ndata: ' + JSON.stringify(data) + '\n\n';
      const k = Math.floor(txt.length / 2);
      chunks.push(enc.encode(txt.slice(0, k)), enc.encode(txt.slice(k)));
      kick();
    },
    ping() { chunks.push(enc.encode(': ping\n\n')); kick(); },
    end() { ended = true; kick(); },
    fail() { broken = true; kick(); },
    res: { status: 200, headers: { get: (h) => (h.toLowerCase() === 'content-type' ? 'text/event-stream; charset=utf-8' : null) },
      json: async () => { throw new Error('not JSON'); },
      body: { getReader: () => ({ read: async () => {
        for (;;) {
          if (chunks.length) return { value: chunks.shift(), done: false };
          if (broken) throw new Error('network error');
          if (ended) return { value: undefined, done: true };
          await new Promise((r) => { wake = r; });
        }
      } }) } },
  };
  return s;
}
// rows + batch as the stream the web sends: planning, row (per row), batch, end
function planStream(rows, batch, ms) {
  const s = stream();
  (async () => {
    let i = 0;
    for (const r of rows) {
      i++;
      s.push('planning', { ident: r.ident, idents: [r.ident], name: r.name, i, n: rows.length, t: '2026-11-20T00:00:00Z' });
      await sleep(ms || 0);
      s.push('row', r);
    }
    s.push('batch', batch);
    s.push('end', { rc: batch ? 0 : 1 });
    s.end();
  })();
  return s.res;
}

function boot(loggedIn, over, search = "?test=1") {
  const els = {};
  const calls = [];
  const doc = {
    getElementById: (id) => (els[id] = els[id] || new FakeEl('div', id)),
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => new FakeEl(tag),
    createTextNode: (t) => ({ text: t }),
  };
  const reply = (status, data, ms) => new Promise((r) => setTimeout(() => r(jsonRes(status, data)), ms || 0));
  const fetch = (p, init) => {
    calls.push({ path: p, method: init.method, body: init.body, headers: init.headers });
    // over[p]: a JSON object (200), or a function (n-th call, init) -> a Response or a promise of one
    if (over && typeof over[p] === 'function') return Promise.resolve(over[p](calls.filter((c) => c.path === p).length, init));
    if (over && over[p]) return reply(200, over[p]);
    if (p === '/api/session') return reply(loggedIn ? 200 : 401, loggedIn ? { session: '0123456789abcdef', expires_in: 28800, doctor_ok: true } : {});
    if (p === '/api/login') return reply(401, { error: 'wrong username or password' }, 300);
    if (p === '/api/inventory') return reply(200, { data: INV, catalog: { help: [] } });
    if (p === '/api/jobs' || p.startsWith('/api/jobs?')) { const jobs = p.includes('limit=50') ? (over?.['/api/status']?.jobs || []) : []; return reply(200, { jobs, total: jobs.length, offset:0, limit:50, active:null }); }
    if (p === '/api/list') return reply(200, { data: { rows: ROWS }, argv: ['migrate', 'list', '--json', '--host', 'cmp1'] });
    if (p === '/api/plan') return new Promise((r) => setTimeout(() => r(planStream([], null, 0)), 300));
    if (p === '/api/rollback') return reply(409, { error: 'no live state' }, 300);
    return reply(404, {});
  };
  const winHandlers = {};
  const win = { addEventListener(t, f) { (winHandlers[t] = winHandlers[t] || []).push(f); }, scrollTo() {} };
  const location = { search, hash: '', host: '127.0.0.1:8787' };
  const ctx = { window: win, document: doc, location, fetch, EventSource: class { constructor(url){this.url=url;this.handlers={};} addEventListener(t,f){this.handlers[t]=f;} close(){this.closed=true;} fire(t,d){this.handlers[t]?.({data:JSON.stringify(d)});} },
    setInterval: () => 0, clearInterval: () => {}, setTimeout, clearTimeout, console, Date, Math, JSON, Set, Array, Object, String, Number,
    Promise, isNaN, Map, TextDecoder, URLSearchParams };
  vm.runInNewContext(SRC, ctx);
  // the browser fires hashchange after location.hash changes; here the test does it
  const hash = (h) => { location.hash = h; for (const f of winHandlers.hashchange || []) f(); };
  // a window event (21: Esc on keydown, a click elsewhere on the page)
  const winFire = (type, ev) => { for (const f of winHandlers[type] || []) f(ev); };
  return { $: doc.getElementById, calls, count: (p) => calls.filter((c) => c.path === p).length, location, hash, win, winFire };
}

// --- 21: the multi-select boxes of the source (a button + a panel of checkboxes) ------------------------------------
const hasCls = (e, c) => typeof e.className === 'string' && e.className.split(' ').includes(c);
const mselPart = (t, id, c) => find(t.$(id), (e) => hasCls(e, c));
const mselBox = (t, id, v) => find(t.$(id), (e) => e.tagName === 'input' && e.type === 'checkbox' && e.value === v);
const mselBtn = (t, id, text) => find(mselPart(t, id, 'msel-panel'), (e) => e.tagName === 'button' && e.textContent === text);
const mselOpen = (t, id) => !mselPart(t, id, 'msel-panel').classList.contains('hidden');
// tick (or untick) values in a box through its panel; a box whose entries are not there yet (no inventory while a
// Doctor runs) is given them first through window.miguiTest.msel, as the tests used to set a <select>'s value
const MSEL_KEY = { 'vm-src-host': 'vmHosts', 'vm-src-project': 'vmProjects', 'vol-src-project': 'volProjects' };
async function tick(t, id, values, on = true) {
  const missing = values.filter((v) => !mselBox(t, id, v));
  if (missing.length) {
    const ms = t.win.miguiTest.msel[MSEL_KEY[id]];
    const have = ms.items.slice();
    ms.setItems(have.concat(missing.map((v) => ({ value: v, text: v }))));
  }
  if (!mselOpen(t, id)) await mselPart(t, id, 'msel-btn').fire('click');
  for (const v of values) {
    const cb = mselBox(t, id, v);
    cb.checked = on;
    await cb.fire('change');
  }
}

// --- 15 (c): login, two submits while the first is in flight -------------------------------------------------------
{
  const t = boot(false);
  await sleep(20);
  t.$('login-user').value = ' test ';
  t.$('login-password').value = ' wrong pass phrase ';
  const a = t.$('login-form').fire('submit');
  const during = { btn: t.$('login-submit').disabled, user: t.$('login-user').disabled, pass: t.$('login-password').disabled,
    text: t.$('login-submit').textContent };
  const b = t.$('login-form').fire('submit');
  await Promise.all([a, b]);
  const sent = JSON.parse((t.calls.find((c) => c.path === '/api/login') || {}).body || '{}');
  ok('GUARD-1 two login submits while the first is in flight: one POST /api/login {username (trimmed), password (as '
     + 'typed)}; both fields and the button locked meanwhile ("Đang đăng nhập…"), unlocked after, password field emptied',
  t.count('/api/login') === 1 && during.btn && during.user && during.pass && during.text === 'Đang đăng nhập…'
     && sent.username === 'test' && sent.password === ' wrong pass phrase ' && Object.keys(sent).length === 2
     && !t.$('login-submit').disabled && !t.$('login-user').disabled && !t.$('login-password').disabled
     && t.$('login-password').value === '' && t.$('login-user').value === ' test ',
  { posts: t.count('/api/login'), during, sent });
}

// --- 15 (b): plan twice, and the other plan button, while a plan runs -------------------------------------------------
{
  const t = boot(true);
  await sleep(50);
  await tick(t, 'vm-src-host', ['cmp1']);
  await t.$('vm-load').fire('click');
  await t.$('vm-select-shown').fire('click');
  const sel = t.$('vm-selected').textContent;
  const a = t.$('vm-plan').fire('click');
  await sleep(10);
  const during = { vm: [t.$('vm-plan').disabled, t.$('vm-plan').textContent], vol: [t.$('vol-plan').disabled, t.$('vol-plan').textContent] };
  const b = t.$('vm-plan').fire('click');
  const c = t.$('vol-plan').fire('click');
  await Promise.all([a, b, c]);
  ok('GUARD-2 plan clicked twice (and vol-plan once) while a plan is in flight: one POST /api/plan; both plan buttons '
     + 'locked "Đang plan…" meanwhile; vm-plan back to "Plan 2 VM" after',
  sel === '2' && t.count('/api/plan') === 1 && during.vm[0] && during.vm[1] === 'Đang plan…' && during.vol[0]
     && during.vol[1] === 'Đang plan…' && !t.$('vm-plan').disabled && t.$('vm-plan').textContent === 'Plan 2 VM',
  { sel, posts: t.count('/api/plan'), during, after: t.$('vm-plan').textContent });

  // --- rollback button of a row with a live state, double click ----------------------------------------------------
  const rb = find(t.$('vm-rows'), (e) => e.tagName === 'button' && e.textContent === 'Rollback…');
  const r1 = rb.fire('click');
  const lockedText = rb.textContent;
  const r2 = rb.fire('click');
  await Promise.all([r1, r2]);
  ok('GUARD-3 a row\'s "Rollback…" clicked twice: one POST /api/rollback, "Đang chuẩn bị…" meanwhile, unlocked after',
    rb && t.count('/api/rollback') === 1 && lockedText === 'Đang chuẩn bị…' && !rb.disabled && rb.textContent === 'Rollback…',
    { posts: t.count('/api/rollback'), lockedText });

  // --- busy() restores the button after an error too ------------------------------------------------------------
  await t.$('hist-refresh').fire('click');
  ok('GUARD-4 a request that fails (404) still unlocks its button and restores its text',
    !t.$('hist-refresh').disabled && t.$('hist-refresh').textContent !== 'Đang tải…', t.$('hist-refresh').textContent);
}
// --- 17: the confirm screen shows the whole plan of every command ---------------------------------------------------
const texts = (root) => {
  const out = [];
  const walk = (n) => {
    if (n instanceof FakeEl) { if (n.textContent) out.push(n.textContent); n.children.forEach(walk); } else if (n && n.text) out.push(n.text);
  };
  walk(root);
  return out;
};
const all = (root, pred, acc = []) => {
  if (root instanceof FakeEl && pred(root)) acc.push(root);
  for (const c of (root && root.children) || []) all(c, pred, acc);
  return acc;
};
const pills = (root) => all(root, (e) => e.tagName === 'span' && /^pill /.test(e.className)).map((e) => e.className + '=' + e.textContent);
{
  const C3 = [{ level: 'OK', text: 'source VM ACTIVE' }, { level: 'WARN', text: 'flavor m1.large mapped by name' },
    { level: 'FAIL', text: 'quota RAM <b>short</b>' }];
  const C2 = [{ level: 'OK', text: 'boot from volume' }, { level: 'INFO', text: 'pool volumes-nvme' }];
  const argv = (id) => ['migrate', 'plan', id, '--az', 'AZ1', '--json'];
  const PLAN = { rows: [
    { ident: 'vm-1', name: 'app01', result: 'PLAN-OK', checks: C3, counts: { ok: 1, warn: 1, fail: 1, info: 0 },
      effective: { az: { value: 'AZ1', from: 'cli' } }, log: '/var/log/mig-batch/x/app01.plan.log', argv: argv('vm-1') },
    { ident: 'vm-2', name: 'app02', result: 'PLAN-OK', checks: C2, counts: { ok: 1, warn: 0, fail: 0, info: 1 },
      effective: { az: { value: 'AZ1', from: 'cli' } }, log: '/var/log/mig-batch/x/app02.plan.log', argv: argv('vm-2') }],
  batch: { id: '0123456789abcdef', token: 'f'.repeat(32), kind: 'vm', rule: { mode: 'count', expect: ['2'] },
    jobs: ['a'.repeat(16), 'b'.repeat(16)], names: ['app01', 'app02'], planned: '2026-11-20T00:00:00Z', expires_in: 900,
    commands: [['migrate', 'migrate', 'vm-1', '--az', 'AZ1', '--yes', '--progress-json'],
      ['migrate', 'migrate', 'vm-2', '--az', 'AZ1', '--yes', '--progress-json']], conf: {} } };
  const t = boot(true, { '/api/plan': () => planStream(PLAN.rows, PLAN.batch, 0) });
  await sleep(50);
  await tick(t, 'vm-src-host', ['cmp1']);
  await t.$('vm-load').fire('click');
  await t.$('vm-select-shown').fire('click');
  await t.$('vm-plan').fire('click');
  await t.$('vm-to-confirm').fire('click');
  const dets = all(t.$('cf-commands'), (e) => e.tagName === 'details');
  const sum = dets.map((d) => (find(d, (e) => e.tagName === 'summary') || {}).textContent);
  const rows0 = all(dets[0] || {}, (e) => e.tagName === 'tr' && !!find(e, (x) => x.tagName === 'span' && /^pill /.test(x.className)));
  const tx0 = texts(dets[0] || {}), tx1 = texts(dets[1] || {});
  const at = (a, s) => a.indexOf(s);
  ok('GUARD-5 (17) confirm: each command\'s details is "nội dung plan · N check (x OK · y WARN · z FAIL) · option hiệu lực"; '
     + 'inside, every check (OK too) with its level pill, then the options, "log plan: <log>" and the plan command; open '
     + 'when the plan has a WARN/FAIL, closed when all OK/INFO; a check text stays text',
  dets.length === 2 && sum[0] === 'nội dung plan · 3 check (1 OK · 1 WARN · 1 FAIL) · option hiệu lực'
     && sum[1] === 'nội dung plan · 2 check (1 OK · 0 WARN · 0 FAIL) · option hiệu lực'
     && dets[0].open === true && !dets[1].open && rows0.length === 3
     && JSON.stringify(pills(dets[0])) === JSON.stringify(['pill ok=OK', 'pill warn=WARN', 'pill fail=FAIL'])
     && JSON.stringify(pills(dets[1])) === JSON.stringify(['pill ok=OK', 'pill skip=INFO'])
     && tx0.includes('quota RAM <b>short</b>') && tx0.includes('log plan: /var/log/mig-batch/x/app01.plan.log')
     && tx0.includes('migrate plan vm-1 --az AZ1 --json') && tx1.includes('log plan: /var/log/mig-batch/x/app02.plan.log')
     && at(tx0, 'source VM ACTIVE') < at(tx0, 'AZ1') && at(tx0, 'AZ1') < at(tx0, 'log plan: /var/log/mig-batch/x/app01.plan.log')
     && at(tx0, 'log plan: /var/log/mig-batch/x/app01.plan.log') < at(tx0, 'migrate plan vm-1 --az AZ1 --json'),
  { n: dets.length, sum, open: dets.map((d) => d.open), rows0: rows0.length, p0: pills(dets[0] || {}), p1: pills(dets[1] || {}), tx0 });

  // --- history: "Plan lúc đó" under the options of a job; a job without checks: "không ghi nhận" -------------------
  const job = (id, name, checks) => ({ id, name, kind: 'vm', ident: 'vm-' + name, state: 'OK', rc: 0,
    argv: ['migrate', 'migrate', 'vm-' + name], effective: { az: { value: 'AZ1', from: 'cli' } }, conf: {},
    projects: { src: 'admin', dst: 'admin' }, t_start: '2026-11-20T00:00:00Z', checks });
  const t2 = boot(true, { '/api/status': { jobs: [job('c'.repeat(16), 'new', C3), job('d'.repeat(16), 'old', null)], active: null, cli: { live: [] } } });
  await sleep(50);
  await t2.$('hist-refresh').fire('click');
  for (let i = 0; i < 2; i++) {
    const tg = find(t2.$('hist-rows'), (e) => e.tagName === 'button' && e.textContent === 'chi tiết ▾');
    if (tg) await tg.fire('click');
  }
  const cards = all(t2.$('hist-rows'), (e) => e.tagName === 'div' && e.className === 'card grow3');
  const idx = (card) => card.children.findIndex((c) => c.tagName === 'h3' && c.textContent === 'Plan lúc đó');
  const p0 = cards[0] && idx(cards[0]) >= 0 ? cards[0].children[idx(cards[0]) + 1] : null;
  const p1 = cards[1] && idx(cards[1]) >= 0 ? cards[1].children[idx(cards[1]) + 1] : null;
  ok('GUARD-6 (17) history detail: "Plan lúc đó" right under the options table with the same check table (3 pills); a '
     + 'job record without checks: "không ghi nhận"',
  cards.length === 2 && p0 && idx(cards[0]) === 2
     && JSON.stringify(pills(p0)) === JSON.stringify(['pill ok=OK', 'pill warn=WARN', 'pill fail=FAIL'])
     && p1 && p1.textContent === 'không ghi nhận',
  { cards: cards.length, p0: p0 && pills(p0), p1: p1 && p1.textContent });
}
// --- 18: the "Đang plan" panel follows the stream event by event -------------------------------------------------
const BATCH1 = { id: '0123456789abcdef', token: 'f'.repeat(32), kind: 'vm', rule: { mode: 'name', expect: ['app01'] },
  jobs: ['a'.repeat(16)], names: ['app01'], planned: '2026-11-20T00:00:00Z', expires_in: 900,
  commands: [['migrate', 'migrate', 'vm-1', '--yes', '--progress-json']], conf: {} };
const planning = (id, name, i) => ({ ident: id, idents: [id], name, i, n: 2, t: '2026-11-20T00:00:00Z' });
async function loaded(t) {
  await sleep(50);
  await tick(t, 'vm-src-host', ['cmp1']);
  await t.$('vm-load').fire('click');
  await t.$('vm-select-shown').fire('click');
}
{
  const st = stream();
  const t = boot(true, { '/api/plan': () => st.res });
  await loaded(t);
  const snap = () => texts(t.$('vm-plan-panel'));
  const click = t.$('vm-plan').fire('click');
  await sleep(10);
  const s0 = snap();
  st.push('planning', planning('vm-1', 'app01', 1));
  await sleep(10);
  const s1 = snap();
  st.ping();
  st.push('row', { ident: 'vm-1', name: 'app01', result: 'PLAN-OK', counts: { ok: 1, info: 0, warn: 1, fail: 0 },
    checks: [{ level: 'OK', text: 'a' }, { level: 'WARN', text: 'b' }] });
  st.push('planning', planning('vm-2', 'app02', 2));
  await sleep(10);
  const s2 = snap();
  const col = texts(t.$('vm-rows'));
  const locked = [t.$('vm-plan').disabled, t.$('vm-plan').textContent, t.$('vol-plan').disabled, t.$('vol-plan').textContent];
  st.push('row', { ident: 'vm-2', name: 'app02', result: 'PLAN-FAIL', info: 'quota RAM short\nsecond line',
    counts: { ok: 0, info: 0, warn: 0, fail: 1 }, checks: [{ level: 'FAIL', text: 'quota RAM <b>short</b>' }] });
  st.push('batch', BATCH1);
  st.push('end', { rc: 0 });
  st.end();
  await click;
  const s3 = snap();
  const det = all(t.$('vm-plan-panel'), (e) => e.tagName === 'details');
  const sent = t.calls.find((c) => c.path === '/api/plan') || {};
  const body = JSON.parse(sent.body || '{}');
  ok('GUARD-7 (18) plan stream: POST /api/plan with X-Requested-With and the items; the panel goes "chờ" -> "đang plan '
     + '(từ hh:mm:ss · n s)" -> "PLAN-OK (1 OK · 1 WARN · 0 FAIL)" / "PLAN-FAIL <first info line>" with its checks in a '
     + '<details>; total "plan i/2 · đã mất mm:ss" then "plan 2 VM mất mm:ss · 1 OK · 1 FAIL"; the table column follows '
     + 'each row; both plan buttons locked for the whole stream; after it "Plan 2 VM" and "Sang xác nhận (1 PLAN-OK)"',
  sent.method === 'POST' && sent.headers['X-Requested-With'] === 'migui' && body.kind === 'vm'
     && JSON.stringify(body.items) === JSON.stringify([{ ident: 'vm-1' }, { ident: 'vm-2' }])
     && s0.includes('Đang plan') && s0.includes('plan 0/2 · đã mất 00:00') && s0.filter((x) => x === 'chờ').length === 2
     && s1.includes('plan 1/2 · đã mất 00:00') && s1.includes('đang plan') && s1.some((x) => /^ từ \d\d:\d\d:\d\d · 0 s$/.test(x))
     && s1.filter((x) => x === 'chờ').length === 1
     && s2.includes('PLAN-OK') && s2.includes(' (1 OK · 1 WARN · 0 FAIL) · 0 s') && s2.includes('plan 2/2 · đã mất 00:00')
     && col.includes('PLAN-OK') && locked[0] && locked[1] === 'Đang plan…' && locked[2] && locked[3] === 'Đang plan…'
     && s3.includes('Plan') && s3.includes('plan 2 VM mất 00:00 · 1 OK · 1 FAIL') && s3.includes('PLAN-FAIL')
     && s3.includes(' quota RAM short') && !s3.some((x) => x.includes('second line'))
     && det.length === 1 && texts(det[0]).includes('1 check') && texts(det[0]).includes('quota RAM <b>short</b>')
     && JSON.stringify(pills(det[0])) === JSON.stringify(['pill fail=FAIL'])
     && !t.$('vm-plan').disabled && t.$('vm-plan').textContent === 'Plan 2 VM'
     && !t.$('vm-to-confirm').disabled && t.$('vm-to-confirm').textContent === 'Sang xác nhận (1 PLAN-OK)',
  { s0, s1, s2, s3, locked, det: det.length, plan: t.$('vm-plan').textContent, to: t.$('vm-to-confirm').textContent });
  await t.$('vm-to-confirm').fire('click');
  ok('GUARD-7 (18) "Sang xác nhận" opens the confirm screen of the streamed batch as before',
    t.location.hash === '#confirm' && texts(t.$('cf-commands')).includes('migrate migrate vm-1 --yes --progress-json'),
    { hash: t.location.hash });
}
{
  const st1 = stream(), st2 = stream();
  const t = boot(true, { '/api/plan': (n) => (n === 1 ? st1.res : st2.res) });
  await loaded(t);
  const LOST = 'mất kết nối, plan vẫn chạy trên mig-host; bấm Plan lại sẽ chờ runner rảnh';
  let click = t.$('vm-plan').fire('click');
  await sleep(10);
  st1.push('planning', planning('vm-1', 'app01', 1));
  await sleep(10);
  st1.fail();
  await click;
  const a = { panel: texts(t.$('vm-plan-panel')), plan: [t.$('vm-plan').disabled, t.$('vm-plan').textContent],
    vol: t.$('vol-plan').textContent, to: t.$('vm-to-confirm').disabled };
  click = t.$('vm-plan').fire('click');
  await sleep(10);
  st2.push('planning', planning('vm-1', 'app01', 1));
  st2.end();  // the server side closed without 'end'
  await click;
  const b = { panel: texts(t.$('vm-plan-panel')), plan: [t.$('vm-plan').disabled, t.$('vm-plan').textContent] };
  ok('GUARD-8 (18) the stream breaks (network error), or closes without its end event: the total line says "' + LOST
     + '", the plan buttons unlock ("Plan 2 VM", vol-plan no longer "Đang plan…"), no batch to confirm',
  a.panel.some((x) => x.startsWith(LOST)) && !a.plan[0] && a.plan[1] === 'Plan 2 VM' && a.vol === 'Plan 0 volume' && a.to
     && b.panel.some((x) => x.startsWith(LOST)) && !b.plan[0] && b.plan[1] === 'Plan 2 VM' && t.count('/api/plan') === 2,
  { a, b });
}
{
  const t = boot(true, { '/api/plan': () => jsonRes(429, { error: 'a plan of this session is still running' }) });
  await loaded(t);
  await t.$('vm-plan').fire('click');
  const p = texts(t.$('vm-plan-panel'));
  ok('GUARD-9 (18) a 429 of the runner is shown in the panel; the buttons unlock',
    p.some((x) => x.includes('(429)') && x.includes('a plan of this session is still running'))
      && !t.$('vm-plan').disabled && t.$('vm-plan').textContent === 'Plan 2 VM', p);
}

// --- 20: the lines of the plan being made: stage, newest line, "log plan live" -------------------------------------
const isLog = (e) => e.tagName === 'details' && texts(e).some((x) => x.startsWith('log plan live'));
const preOf = (d) => find(d, (e) => e.tagName === 'pre');
const clsText = (root, tag, cls) => all(root, (e) => e.tagName === tag && e.className.split(' ').includes(cls)).map((e) => e.textContent);
{
  const st = stream();
  const t = boot(true, { '/api/plan': () => st.res });
  await loaded(t);
  const panel = t.$('vm-plan-panel');
  const snap = () => ({ stage: clsText(panel, 'span', 'planstage'), latest: clsText(panel, 'div', 'planline'),
    logs: all(panel, isLog).map((d) => ({ open: d.open, pre: (preOf(d) || {}).textContent })) });
  const line = (id, text, level) => st.push('line', { ident: id, idents: [id], text, level: level || null, t: '2026-11-20T00:00:00Z' });
  const click = t.$('vm-plan').fire('click');
  await sleep(10);
  st.push('planning', planning('vm-1', 'app01', 1));
  await sleep(10);
  const a = snap();
  line('vm-1', 'VM app01 status=ACTIVE host=cmp1');
  line('vm-1', '  port port-src-1 198.51.100.50');
  await sleep(10);
  const b = snap();
  line('vm-1', 'Destination preflight (mode=cold ip=same):');
  line('vm-1', '  OK    Ceph pool volumes: 12 GB needed, 900 GB left', 'OK');
  line('vm-1', '  WARN  flavor m1.small mapped by name', 'WARN');
  line('vm-1', '  OK    quota RAM', 'OK');
  await sleep(10);
  const c = snap();
  line('vm-1', 'Preflight passed.');
  await sleep(10);
  const d = snap();
  st.push('row', { ident: 'vm-1', name: 'app01', result: 'PLAN-OK', counts: { ok: 2, info: 0, warn: 1, fail: 0 } });
  st.push('planning', planning('vm-2', 'app02', 2));
  await sleep(10);
  const e = snap();
  st.push('row', { ident: 'vm-2', name: 'app02', result: 'PLAN-OK', counts: { ok: 1, info: 0, warn: 0, fail: 0 } });
  st.push('batch', BATCH1);
  st.push('end', { rc: 0 });
  st.end();
  await click;
  const f = snap();
  const ALL7 = ['VM app01 status=ACTIVE host=cmp1', '  port port-src-1 198.51.100.50', 'Destination preflight (mode=cold ip=same):',
    '  OK    Ceph pool volumes: 12 GB needed, 900 GB left', '  WARN  flavor m1.small mapped by name', '  OK    quota RAM',
    'Preflight passed.'].join('\n');
  ok('GUARD-12 (20) the stage of the VM being planned follows its lines: "đang export VM" (no line yet, and after the '
     + 'VM/port lines), "preflight đích · 3 check" after "Destination preflight" + 3 lines with a level, "xong" after '
     + '"Preflight passed."; the newest line in mono (trimmed); the next VM starts again at "đang export VM"',
  JSON.stringify(a.stage) === '["đang export VM"]' && a.latest.length === 0 && a.logs.length === 0
     && JSON.stringify(b.stage) === '["đang export VM"]' && JSON.stringify(b.latest) === '["port port-src-1 198.51.100.50"]'
     && JSON.stringify(c.stage) === '["preflight đích · 3 check"]' && JSON.stringify(c.latest) === '["OK    quota RAM"]'
     && JSON.stringify(d.stage) === '["xong"]' && JSON.stringify(d.latest) === '["Preflight passed."]'
     && JSON.stringify(e.stage) === '["đang export VM"]' && e.latest.length === 0,
  { a, b, c, d, e });
  ok('GUARD-12 (20) "log plan live" is an open <details> with every line received while the VM plans; once its row '
     + 'came it is closed and keeps all 7 lines (after the next VM and after the end of the stream too)',
  b.logs.length === 1 && b.logs[0].open === true && b.logs[0].pre === 'VM app01 status=ACTIVE host=cmp1\n  port port-src-1 198.51.100.50'
     && d.logs.length === 1 && d.logs[0].open === true && d.logs[0].pre === ALL7
     && e.logs.length === 1 && e.logs[0].open === false && e.logs[0].pre === ALL7
     && f.logs.length === 1 && f.logs[0].open === false && f.logs[0].pre === ALL7
     && texts(panel).includes('log plan live · 7 dòng'),
  { b: b.logs, d: d.logs, e: e.logs, f: f.logs });
}
{
  const VOLS = [{ id: 'v-1', name: 'data-1', status: 'available', size: 1, state: null },
    { id: 'v-2', name: 'data-2', status: 'available', size: 1, state: null }];
  const st = stream();
  const t = boot(true, { '/api/plan': () => st.res, '/api/list': () => jsonRes(200, { data: { rows: VOLS }, argv: [] }) });
  await sleep(50);
  await tick(t, 'vol-src-project', ['admin']);
  await t.$('vol-load').fire('click');
  await t.$('vol-select-shown').fire('click');
  const panel = t.$('vol-plan-panel');
  const click = t.$('vol-plan').fire('click');
  await sleep(10);
  st.push('planning', { ident: 'v-1', idents: ['v-1', 'v-2'], name: 'project admin', i: 1, n: 2, t: '2026-11-20T00:00:00Z' });
  for (const [text, level] of [['Destination preflight (migrate-volume type=x pool=y):'], ['  OK    pool y', 'OK']]) {
    st.push('line', { ident: 'v-1', idents: ['v-1', 'v-2'], text, level: level || null, t: '2026-11-20T00:00:00Z' });
  }
  await sleep(10);
  const a = { stage: clsText(panel, 'span', 'planstage'), latest: clsText(panel, 'div', 'planline'), logs: all(panel, isLog).length };
  st.push('row', { ident: 'v-1', name: 'data-1', result: 'READY' });
  st.push('row', { ident: 'v-2', name: 'data-2', result: 'READY' });
  st.push('batch', null);
  st.push('end', { rc: 1 });
  st.end();
  await click;
  const logs = all(panel, isLog);
  ok('GUARD-13 (20) volumes: both volumes of the project read "đang plan project admin" with the newest line; one '
     + '"log plan live" for the group, closed after the rows with both lines kept',
  JSON.stringify(a.stage) === '["đang plan project admin","đang plan project admin"]'
     && JSON.stringify(a.latest) === '["OK    pool y","OK    pool y"]' && a.logs === 1
     && logs.length === 1 && logs[0].open === false
     && (preOf(logs[0]) || {}).textContent === 'Destination preflight (migrate-volume type=x pool=y):\n  OK    pool y',
  { a, logs: logs.length });
}

// --- 19.1: Doctor runs by itself in a new session; route('#confirm') without Doctor keeps the confirmation --------
const RB = { id: '1123456789abcdef', token: 'e'.repeat(32), kind: 'rollback',
  rule: { mode: 'name', expect: ['mig-peer'], alt: ['vm-3'] }, jobs: ['c'.repeat(16)], names: ['mig-peer'],
  planned: '2026-11-20T00:00:00Z', expires_in: 900, commands: [['migrate', 'rollback', 'vm-3']], conf: {} };
const DOC_OK = { doctor_ok: true, rc: 0, data: { passed: true, checks: [{ level: 'OK', text: 'clouds reachable' }], counts: { ok: 1 } } };
const DOC_FAIL = { doctor_ok: false, rc: 1, data: { passed: false, checks: [{ level: 'FAIL', text: 'nova down' }], counts: { fail: 1 } } };
const shown = (t, s) => !t.$('screen-' + s).classList.contains('hidden');
async function openRollback(t) {
  await tick(t, 'vm-src-host', ['cmp1']);
  await t.$('vm-load').fire('click');
  const rb = find(t.$('vm-rows'), (e) => e.tagName === 'button' && e.textContent === 'Rollback…');
  await rb.fire('click');
  // Rollback keeps its origin screen under a dedicated modal.
}
{
  let release;
  const gate = new Promise((r) => { release = r; });
  const t = boot(false, {
    '/api/login': () => jsonRes(200, { session: '0123456789abcdef', user: 'test', expires_in: 28800, doctor_ok: false }),
    '/api/doctor': async () => { await gate; return jsonRes(200, DOC_OK); },
    '/api/rollback': () => jsonRes(200, { batch: RB, original: null }),
  });
  await sleep(20);
  t.$('login-user').value = 'test';
  t.$('login-password').value = 'right-password-x';
  await t.$('login-form').fire('submit');
  await sleep(10);
  const during = { posts: t.count('/api/doctor'), btn: t.$('doctor-run').disabled, text: t.$('doctor-run').textContent,
    screen: shown(t, 'overview') };
  await openRollback(t);
  await sleep(10);
  const mid = { posts: t.count('/api/doctor'), flash: t.$('flash').textContent, doctor: shown(t, 'doctor'), confirm: !!t.$('action-dialog').open };
  const extra = t.$('doctor-run').fire('click');  // a click on the locked button while the automatic run is in flight
  release();
  await extra;
  await sleep(50);
  const end = { posts: t.count('/api/doctor'), confirm: !!t.$('action-dialog').open, doctor: shown(t, 'doctor'),
    label: t.$('dialog-title').textContent, hint: texts(t.$('dialog-body')).join(' '), btn: t.$('doctor-run').disabled };
  ok('GUARD-10 (19.1) login with doctor_ok=false runs Doctor by itself: exactly one POST /api/doctor, the doctor-run '
     + 'button locked "Đang chạy…" meanwhile',
  during.posts === 1 && during.btn && during.text === 'Đang chạy…' && during.screen, during);
  ok('GUARD-10 (v1 D3) rollback remains reachable while the automatic Doctor runs; the run stays single-flight and the original name/UUID hint survives',
    mid.posts === 1 && mid.confirm && !mid.doctor && end.posts === 1 && end.confirm && !end.doctor
    && end.label === 'Rollback mig-peer' && end.hint.includes('vm-3') && !end.btn, {mid,end});
}
{
  const t = boot(true, {
    '/api/session': () => jsonRes(200, { session: '0123456789abcdef', user: 'test', expires_in: 28800, doctor_ok: false }),
    '/api/doctor': (n) => jsonRes(200, n <= 2 ? DOC_FAIL : DOC_OK),
    '/api/rollback': () => jsonRes(200, { batch: RB, original: null }),
  });
  await sleep(50);
  const auto = t.count('/api/doctor');
  await openRollback(t);
  await sleep(50);
  const red = { posts: t.count('/api/doctor'), doctor: shown(t, 'doctor'), confirm: !!t.$('action-dialog').open,
    flash: t.$('flash').textContent };
  await t.$('doctor-run').fire('click');
  await sleep(10);
  ok('GUARD-11 (v1 D3) a red Doctor does not block recovery or discard a prepared rollback; manually rerunning Doctor remains single-flight',
    auto === 1 && red.posts === 1 && red.confirm && !red.doctor && t.count('/api/doctor') === 2
    && t.$('action-dialog').open && t.$('dialog-title').textContent === 'Rollback mig-peer'
    && t.count('/api/rollback') === 1, {auto,red,posts:t.count('/api/doctor')});
}

// --- 21: several source hosts and projects ---------------------------------------------------------------------
const listBodies = (t) => t.calls.filter((c) => c.path === '/api/list').map((c) => JSON.parse(c.body || '{}'));
const invWith = (hosts, projects) => ({ data: { source: { hosts, projects }, destination: INV.destination, conf: {} }, catalog: { help: [] } });
{
  const HOSTS = [{ host: 'cmp1', zone: 'nova', state: 'up', status: 'enabled' }, { host: 'cmp2', zone: 'nova', state: 'up', status: 'enabled' },
    { host: 'cmp3', zone: 'nova', state: 'down', status: 'enabled' }, { host: 'cmp4', zone: 'AZ2', state: 'up', status: 'disabled' }];
  const PROJS = [{ id: 'p1', name: 'admin' }, { id: 'p2', name: 'other' }, { id: 'p3', name: 'third' }];
  const t = boot(true, { '/api/inventory': invWith(HOSTS, PROJS) });
  await sleep(50);
  const id = 'vm-src-host';
  const first = { btn: mselPart(t, id, 'msel-btn').textContent, open: mselOpen(t, id),
    search: !mselPart(t, id, 'msel-search').classList.contains('hidden'),
    note: (find(t.$(id), (e) => e.tagName === 'span' && e.textContent === 'nova, down/enabled') || {}).textContent };
  await tick(t, id, ['cmp1', 'cmp2']);
  await tick(t, 'vm-src-project', ['admin']);
  const opened = { host: mselOpen(t, id), project: mselOpen(t, 'vm-src-project') };  // opening one closes the other
  const sum = [mselPart(t, id, 'msel-btn').textContent, mselPart(t, 'vm-src-project', 'msel-btn').textContent];
  await t.$('vm-load').fire('click');
  const closedByLoad = !mselOpen(t, 'vm-src-project');
  await t.$('vm-load').fire('click');  // the ticks stay for the session: the same arrays again
  const b = listBodies(t);
  const title = [t.$('vm-table-title').textContent, t.$('vm-table-title').title];
  ok('GUARD-14 (21) tick 2 hosts + 1 project: POST /api/list {hosts: [cmp1, cmp2], projects: [admin]} exactly, again on '
     + 'a second load (ticks kept); summaries "2 host" / "admin" (was "mọi host"); each host shows zone, state/status; '
     + 'no search box at 4 entries; opening one box closes the other; Tải closes the open box; title "Instance trên 2 '
     + 'host / admin (3)" with the names in its tooltip',
  first.btn === 'mọi host' && !first.open && !first.search && first.note === 'nova, down/enabled'
     && b.length === 2 && JSON.stringify(b[0]) === '{"hosts":["cmp1","cmp2"],"projects":["admin"]}'
     && JSON.stringify(b[1]) === JSON.stringify(b[0]) && sum[0] === '2 host' && sum[1] === 'admin'
     && !opened.host && opened.project && closedByLoad
     && title[0] === 'Instance trên 2 host / admin (3)' && title[1] === 'host: cmp1, cmp2\nproject: admin',
  { first, b, sum, opened, closedByLoad, title });

  await mselPart(t, id, 'msel-btn').fire('click');
  await mselBtn(t, id, 'Bỏ hết').fire('click');
  await tick(t, 'vm-src-project', ['admin'], false);
  await t.$('vm-load').fire('click');
  const all3 = listBodies(t)[2];
  const want = t.win.miguiTest.srcHostsForAll({ source: { hosts: HOSTS } });
  ok('GUARD-15 (21) "Bỏ hết" on hosts and the project unticked: the load sends hosts = srcHostsForAll(S.inv) (the up '
     + 'hosts cmp1, cmp2, cmp4) and projects []; summaries back to "mọi host" / "mọi project"; title "Instance trên '
     + 'mọi host / mọi project (3)", tooltip lists the up hosts',
  JSON.stringify(want) === '["cmp1","cmp2","cmp4"]' && JSON.stringify(all3) === JSON.stringify({ hosts: want, projects: [] })
     && mselPart(t, id, 'msel-btn').textContent === 'mọi host' && mselPart(t, 'vm-src-project', 'msel-btn').textContent === 'mọi project'
     && t.$('vm-table-title').textContent === 'Instance trên mọi host / mọi project (3)'
     && t.$('vm-table-title').title === 'host: mọi host đang up (3): cmp1, cmp2, cmp4\nproject: mọi project',
  { all3, want, title: t.$('vm-table-title').textContent });

  // Esc closes the open box (and only when one is open); a click outside closes it, a click inside does not
  await mselPart(t, id, 'msel-btn').fire('click');
  const before = mselOpen(t, id);
  const panel = mselPart(t, id, 'msel-panel');
  t.winFire('click', { target: panel, composedPath: () => [panel, t.$(id)] });
  const afterInside = mselOpen(t, id);
  t.winFire('keydown', { key: 'Enter' });
  const afterEnter = mselOpen(t, id);
  t.winFire('keydown', { key: 'Escape' });
  const afterEsc = mselOpen(t, id);
  await mselPart(t, 'vm-src-project', 'msel-btn').fire('click');
  const reopened = mselOpen(t, 'vm-src-project');
  t.winFire('click', { target: t.$('vm-search'), composedPath: () => [t.$('vm-search')] });
  ok('GUARD-16 (21) Esc closes the open box (another key does not); a click inside the box keeps it open, a click '
     + 'elsewhere closes it',
  before && afterInside && afterEnter && !afterEsc && reopened && !mselOpen(t, 'vm-src-project')
     && mselPart(t, id, 'msel-btn')['aria-expanded'] === 'false',
  { before, afterInside, afterEnter, afterEsc, reopened });
}
{
  const n129 = Array.from({ length: 129 }, (_x, i) => String(i + 1).padStart(2, '0'));
  const t = boot(true, { '/api/inventory': invWith(n129.map((n) => ({ host: 'h' + n, zone: 'nova', state: 'up', status: 'enabled' })),
    n129.map((n) => ({ id: 'id' + n, name: 'proj' + n }))) });
  await sleep(50);
  const id = 'vm-src-host';
  const search = mselPart(t, id, 'msel-search');
  await mselPart(t, id, 'msel-btn').fire('click');
  search.value = 'h0';
  await search.fire('input');
  const shownH1 = all(t.$(id), (e) => e.tagName === 'input' && e.type === 'checkbox').length;
  await mselBtn(t, id, 'Tất cả').fire('click');
  const ticked10 = mselPart(t, id, 'msel-btn').textContent;
  search.value = '';
  await search.fire('input');
  await mselBtn(t, id, 'Tất cả').fire('click');
  const warn = mselPart(t, id, 'msel-warn');
  const locked = { dis: t.$('vm-load').disabled, text: t.$('vm-load').textContent, btn: mselPart(t, id, 'msel-btn').textContent,
    over: mselPart(t, id, 'msel-btn').classList.contains('over'), warn: !warn.classList.contains('hidden') && warn.textContent };
  await t.$('vm-load').fire('click');
  const sent129 = t.count('/api/list');
  await tick(t, id, ['h129'], false);
  const unlocked = { dis: t.$('vm-load').disabled, text: t.$('vm-load').textContent, warn: warn.classList.contains('hidden') };
  await mselPart(t, 'vm-src-project', 'msel-btn').fire('click');
  await mselBtn(t, 'vm-src-project', 'Tất cả').fire('click');
  const projLock = { dis: t.$('vm-load').disabled, text: t.$('vm-load').textContent };
  await t.$('vm-load').fire('click');
  const sentP = t.count('/api/list');
  await mselBtn(t, 'vm-src-project', 'Bỏ hết').fire('click');
  await t.$('vm-load').fire('click');
  const b = listBodies(t);
  await mselPart(t, 'vol-src-project', 'msel-btn').fire('click');
  await mselBtn(t, 'vol-src-project', 'Tất cả').fire('click');
  const volLock = { dis: t.$('vol-load').disabled, text: t.$('vol-load').textContent };
  await t.$('vol-load').fire('click');
  ok('GUARD-17 (21) 129 entries: a search box; search "h0" shows 9 hosts and "Tất cả" ticks those 9; "Tất cả" '
     + 'unfiltered ticks 129: warning in the box, summary "129 host" in red, Tải locked "Tối đa 128 host", a click sends '
     + 'nothing; untick one: unlocked "Tải danh sách"; 129 projects: "Tối đa 128 project", nothing sent; with 128 hosts '
     + 'the body has 128 hosts; the Volume box locks vol-load the same way',
  !search.classList.contains('hidden') && shownH1 === 9 && ticked10 === '9 host'
     && locked.dis && locked.text === 'Tối đa 128 host' && locked.btn === '129 host' && locked.over
     && locked.warn === 'Đã tick 129 host, tối đa 128 cho một lần tải: bỏ bớt.' && sent129 === 0
     && !unlocked.dis && unlocked.text === 'Tải danh sách' && unlocked.warn
     && projLock.dis && projLock.text === 'Tối đa 128 project' && sentP === 0
     && b.length === 1 && b[0].hosts.length === 128 && b[0].projects.length === 0 && !b[0].hosts.includes('h129')
     && volLock.dis && volLock.text === 'Tối đa 128 project' && t.count('/api/list') === 1,
  { shownH1, ticked10, locked, sent129, unlocked, projLock, sentP, b: b.map((x) => [x.hosts.length, x.projects.length]), volLock });
}
{
  const PROJS = [{ id: 'p1', name: 'admin' }, { id: 'p2', name: 'other' }];
  const VOLS = [{ id: 'v-1', name: 'data-1', status: 'available', size: 1, state: null },
    { id: 'v-2', name: 'data-2', status: 'available', size: 2, state: null }];
  const t = boot(true, { '/api/inventory': invWith(INV.source.hosts, PROJS),
    '/api/list': () => jsonRes(200, { data: { rows: VOLS }, argv: [] }) });
  await sleep(50);
  const none = { btn: mselPart(t, 'vol-src-project', 'msel-btn').textContent };
  await t.$('vol-load').fire('click');
  none.sent = t.count('/api/list');
  none.flash = t.$('flash').textContent;
  await tick(t, 'vol-src-project', ['admin', 'other']);
  await t.$('vol-load').fire('click');
  const b = listBodies(t);
  ok('GUARD-18 (21) Volume: nothing ticked = "chọn project" and no request ("Chọn project nguồn."); admin + other '
     + 'ticked: POST /api/list {projects: [admin, other], volumes: true, scope: available}; title "Volume của 2 project '
     + '(2)" with the names in its tooltip',
  none.btn === 'chọn project' && none.sent === 0 && none.flash === 'Chọn project nguồn.'
     && b.length === 1 && JSON.stringify(b[0]) === '{"projects":["admin","other"],"volumes":true,"scope":"available"}'
     && t.$('vol-table-title').textContent === 'Volume của 2 project (2)' && t.$('vol-table-title').title === 'project: admin, other',
  { none, b, title: t.$('vol-table-title').textContent });
}
// v1: keep the old request/stream/locking tests above, and exercise the new safety boundaries.
{
  const t = boot(true); await sleep(40);
  await t.$('vm-load').fire('click'); await t.$('vm-select-shown').fire('click');
  const h = t.win.miguiTest, k = h.state.vm;
  const stamp = () => { k.batch = {jobs:['a','b'],kind:'vm',uiFingerprint:h.selectionFingerprint('vm')}; };
  stamp(); const before = k.batch.uiFingerprint;
  k.sel = new Set([...k.sel].reverse()); h.vmCounts();
  ok('V1 fingerprint is order-independent for the selected set', k.batch?.uiFingerprint === before);
  k.sel.delete('vm-2'); h.vmCounts();
  ok('V1 D1 selection change clears batch and disables confirmation', !k.batch && t.$('vm-to-confirm').disabled && t.$('vm-to-confirm').textContent === 'Cần plan lại');
  stamp(); t.$('o-verify').value='none'; await t.$('o-verify').fire('change');
  ok('V1 D1 option change clears the batch immediately', !k.batch && t.$('vm-to-confirm').disabled);
  t.$('o-ip').value='row'; stamp(); k.ip['vm-1']='198.51.100.99'; h.vmCounts();
  ok('V1 D1 per-row IP participates in the fingerprint', !k.batch);
  h.state.vol.sel.add('vol-1'); h.state.vol.batch={jobs:['v'],uiFingerprint:h.selectionFingerprint('vol')};
  t.$('vol-az').value='AZ2'; await t.$('vol-az').fire('change');
  ok('V1 D1 volume options invalidate their own batch', !h.state.vol.batch && t.$('vol-to-confirm').disabled);
}
{
  const stream1 = stream(); const t=boot(true, {'/api/plan':()=>stream1.res}); await sleep(40);
  await t.$('vm-load').fire('click'); await t.$('vm-select-shown').fire('click');
  const request=t.$('vm-plan').fire('click'); await sleep(10);
  await t.$('vm-select-none').fire('click');
  stream1.push('batch',{id:'stale',jobs:['a'],commands:[],names:['app01']}); stream1.push('end',{}); stream1.end(); await request;
  ok('V1 D1 selection changed during SSE cannot resurrect a stale batch', !t.win.miguiTest.state.vm.batch && t.$('vm-to-confirm').disabled);
}
{
  const t=boot(true, {'/api/batches/test/run':()=>jsonRes(202,{jobs:[]})}); await sleep(40);
  const h=t.win.miguiTest;
  const b={id:'test',kind:'vm',jobs:['a','b'],names:['app01','app02'],commands:[['migrate','migrate','vm-1'],['migrate','migrate','vm-2']],
    rows:[{ident:'vm-1',name:'app01',result:'PLAN-OK',checks:[{level:'WARN',text:'Review quota'}]}],rule:{mode:'count',expect:['2']},token:'token-secret',at:Date.now(),expires_in:900};
  h.state.confirm=b; h.renderConfirm();
  await t.$('cf-run').fire('click');
  ok('V1 confirmation cannot POST without a matching typed value', t.$('cf-run').disabled && t.count('/api/batches/test/run')===0);
  t.$('cf-typed').value='2'; await t.$('cf-typed').fire('input');
  ok('V1 every WARN requires explicit acknowledgement even after count matches',t.$('cf-run').disabled);
  const ack=find(t.$('cf-warnings'),e=>e.tagName==='input'); ack.checked=true; await ack.fire('change');
  ok('V1 matching count plus all warning acknowledgements enables Run',!t.$('cf-run').disabled);
  await t.$('cf-run').fire('click');
  const body=JSON.parse(t.calls.find(c=>c.path==='/api/batches/test/run').body);
  ok('V1 Run preserves typed, confirm_token and continue_on_error request shape',body.typed==='2' && body.confirm_token==='token-secret' && Object.keys(body).length===3);
  ok('V1 names with spaces use whole lines; aliases work; duplicate names cannot bypass matching',
    h.confirmationMatches({rule:{mode:'name',expect:['VM one','VM two']}},'VM two\nVM one') &&
    !h.confirmationMatches({rule:{mode:'name',expect:['VM one','VM two']}},'VM one\nVM one') &&
    h.confirmationMatches({rule:{mode:'name',expect:['VM one'],alt:['uuid-1']}},'uuid-1'));
}
{
  const t=boot(true); await sleep(40); const h=t.win.miguiTest;
  const j={id:'j1',name:'app01',kind:'vm',ident:'vm-1',state:'RUNNING',volumes_total:7,cancel:{sigint_at:'2026-11-21T00:00:00Z'}};
  ok('V1 D6 SIGKILL countdown reconstructs from server timestamp, including reload and elapsed deadline',
    h.cancelRemaining(j,Date.parse('2026-11-21T00:01:30Z'))===30 && h.cancelRemaining(j,Date.parse('2026-11-21T00:03:00Z'))===0 && h.cancelRemaining({})===null);
  h.state.jobs=[j]; h.state.watch=j.id; h.renderRunning();
  const es=h.state.es; es.fire('open');
  ok('V1 D8 SSE open marks data connected', h.state.connection==='connected');
  es.fire('error'); const since=h.state.disconnectedAt;
  ok('V1 D8 interrupted SSE shows reconnect state and keeps the same native EventSource for Last-Event-ID replay',h.state.connection==='reconnecting' && !!since && h.state.es===es && !es.closed);
  es.fire('open');
  ok('V1 D8 reconnection clears the stale-data timestamp',h.state.connection==='connected' && !h.state.disconnectedAt);
  es.fire('progress',{event:{event:'copy',volume:'v1',pct:100}});
  ok('V1 D16 progress denominator uses planned total, not observed events',t.$('st-vol').textContent==='1/7');
  h.state.armedJobId='j1'; h.state.jobs=[{...j,id:'j2',name:'app02'}];h.state.watch='j2';h.runLocks();
  ok('V1 D6 cancellation cannot remain armed for a different job',h.state.armedJobId===null && t.$('run-kill').disabled);
  const failed={...j,state:'FAIL',t_stop:'2026-11-21T00:00:00Z',t_end:'2026-11-21T00:01:00Z',exit_class:4};
  h.state.jobs=[failed];h.state.watch='j1';h.renderRunning();h.runLocks();
  ok('V1 D7 failure after t-stop keeps downtime live, shows recovery banner and hides cancellation',h.sourceStillDown(failed) && !h.sourceStillDown({...failed,rolled_back:true}) && !t.$('run-failure').classList.contains('hidden') && t.$('run-cancel-panel').classList.contains('hidden'));
  ok('V1 D16 old runner without known totals reports unknown rather than observed count',h.volumeTotal({kind:'vm',ident:'missing'})===null);
}
{
  const path0='/api/jobs?offset=0&limit=50&q=&state=&kind=&from=&to=';
  const path1='/api/jobs?offset=50&limit=50&q=&state=&kind=&from=&to=';
  const t=boot(true,{'/api/status':{jobs:[],cli:{}},[path0]:{jobs:[],total:125,active:null},[path1]:{jobs:[],total:125,active:null}}); await sleep(40);
  await t.$('hist-refresh').fire('click');await t.$('hist-next').fire('click');
  ok('V1 pagination sends offset and limit, updates range and exports identical query',t.count(path0)===1 && t.count(path1)===1 && t.$('hist-page').textContent==='51–100 / 125 job' && t.$('hist-csv').href==='/api/jobs.csv?'+path1.split('?')[1]);
  t.$('hist-search').value='app & one';t.$('hist-state').value='FAIL';t.$('hist-kind').value='vm';t.$('hist-from').value='2026-11-01';t.$('hist-to').value='2026-11-21';
  await t.$('hist-filter-form').fire('submit');
  ok('V1 filters reset pagination and encode search/date parameters',t.calls.some(c=>c.path==='/api/jobs?offset=0&limit=50&q=app%20%26%20one&state=FAIL&kind=vm&from=2026-11-01&to=2026-11-21'));
  const h=t.win.miguiTest;
  ok('V1 jobs use the server envelope',h.jobsEnvelope({jobs:[{id:'a'}],total:120,active:null}).total===120);
  const impact=h.batchImpact({kind:'vm',impact:{vms:1,volumes:2,gb:60,stop_source:0,detach_ports:0},commands:[['migrate','migrate','vm-1']],rows:[{ident:'vm-1',impact:{creates:{server:true},source_after:'ACTIVE'},effective:{mode:{value:'live'},verify:{value:'full'}}}]});
  ok('V1 D4 impact uses server totals and verified row impact',impact.volumes===2 && impact.gb===60 && impact.detach_ports===0 && impact.servers===1 && impact.mode==='live' && impact.verify==='full' && impact.source_after==='ACTIVE');
}
{
  const t=boot(true,{'/api/doctor':()=>jsonRes(200,DOC_OK)});await sleep(40);
  const h=t.win.miguiTest;
  ok('V1 write-check action is hidden when server capability is absent',t.$('doctor-write').classList.contains('hidden'));
  h.state.session.features={write_check:true};
  await t.$('doctor-write').fire('click');
  ok('V1 write-check explains the canary in a dialog before POST',t.$('action-dialog').open && t.count('/api/doctor')===0 && texts(t.$('dialog-body')).join(' ').includes('4 MiB'));
  await Promise.all([t.$('dialog-accept').fire('click'),t.$('dialog-accept').fire('click')]);
  const requests=t.calls.filter(c=>c.path==='/api/doctor');
  ok('V1 write-check posts exactly {write_check:true} once under the existing busy lock',requests.length===1 && requests[0].body==='{"write_check":true}' && !t.$('action-dialog').open);
  h.state.jobs=[{id:'cancel-a',name:'app01',state:'RUNNING',kind:'vm'}];h.state.watch='cancel-a';h.runLocks();
  await t.$('run-cancel').fire('click');
  ok('V1 SIGINT requires a dialog for the current job',t.$('action-dialog').open && h.state.armedJobId==='cancel-a');
  h.state.jobs=[{id:'cancel-b',name:'app02',state:'RUNNING',kind:'vm'}];h.state.watch='cancel-b';
  await t.$('dialog-accept').fire('click');
  ok('V1 dialog cannot send SIGINT after the running job changes',!t.calls.some(c=>c.path.endsWith('/cancel')));
  h.openConfirm({...RB,at:Date.now(),original:{t_stop:'2026-11-21T00:00:00Z',delete:{server:'dst-1',ports:[],volumes:['v1','v2'],images:[]},age_seconds:3600}},'history');
  ok('V1 rollback dialog shows deletion scope and requires separate RPO acknowledgement',t.$('action-dialog').open && texts(t.$('dialog-body')).join(' ').includes('2 volume') && t.$('dialog-accept').disabled && all(t.$('dialog-body'),e=>e.tagName==='input' && e.type==='checkbox').length===1);
}
// Round 2: the 1.0.0 contract, exercised through controls and server-shaped records.
{
  for (const search of ['', '?test=0', '?other=test=1']) {
    const t=boot(true,{},search); await sleep(30);
    ok('R2-7 production has no miguiTest or state: '+(search || '(no query)'),!('miguiTest' in t.win));
  }
  const t=boot(true);await sleep(40);const h=t.win.miguiTest;
  ok('R2-7 test=1 preserves existing hooks',!!h.state && !!h.msel && typeof h.openConfirm==='function');
  h.state.vm.rows=ROWS;h.state.vm.sel=new Set(['vm-1','vm-2']);
  t.$('o-ip').value='row';await t.$('o-ip').fire('change');h.renderVm();
  const ips=all(t.$('vm-row-ips'),e=>e.tagName==='input');
  ok('R2-1 row IP table contains only selected VMs; selection table has no IP inputs',ips.length===2 && texts(t.$('vm-row-ips')).some(x=>x.includes('để trống = same')) && !find(t.$('vm-rows'),e=>e.tagName==='input' && e.type==='text'));
  for(const value of ['256.1.2.3','1.2.3','1.2.3.4.5','abc','01.2.3.4']) {
    ips[0].value=value;await ips[0].fire('input');
    ok('R2-1 rejects IPv4 '+value,t.$('vm-plan').disabled && ips[0]['aria-invalid']==='true');
  }
  ips[0].value='192.0.2.42';await ips[0].fire('input');
  const fingerprint=h.selectionFingerprint('vm');
  h.state.vm.batch={jobs:['j1'],uiFingerprint:fingerprint};
  ips[0].value='';await ips[0].fire('input');
  ok('R2-1 blank means same, valid IP persists in store and edits invalidate plan',!t.$('vm-plan').disabled && h.state.vm.ip['vm-1']==='' && !h.state.vm.batch && fingerprint!==h.selectionFingerprint('vm'));
  t.$('o-ip').value='same';await t.$('o-ip').fire('change');
  ok('R2-1 row IP editor hides outside row mode',t.$('vm-row-ips').classList.contains('hidden'));
  for(const name of ['overview','history','doctor']) {h.updateStepper(name);ok('R2-3 stepper hidden on '+name,t.$('stepper').classList.contains('hidden'));}
  h.updateStepper('options');
  ok('R2-3 normal campaign starts at Chon without Doctor',!t.$('stepper').classList.contains('hidden') && t.$('step-doctor').classList.contains('hidden') && texts(t.$('step-instance'))[0]==='1');
  await t.$('overview-new').fire('click');h.updateStepper('doctor');
  ok('R2-3 new campaign includes Doctor as step 1',!t.$('stepper').classList.contains('hidden') && !t.$('step-doctor').classList.contains('hidden') && texts(t.$('step-doctor'))[0]==='1');
  h.state.vm.rows=['preparing','cutover','migrated'].map((phase,i)=>({...ROWS[i],source_power:i?'SHUTOFF':'ACTIVE',state:{live:true,dir:'/state/'+i,phase,dst_server_id:'dst'}}));h.renderVm();
  const text=texts(t.$('vm-rows')).join(' ');
  ok('R2-15 source power and authoritative migration phase remain beside live rollback controls',text.includes('ACTIVE') && text.includes('SHUTOFF') && ['Cần xử lý · nguồn chưa dừng','Cần xử lý · nguồn đã tắt','Đã cutover'].every(x=>text.includes(x)) && all(t.$('vm-rows'),e=>e.tagName==='button' && e.textContent==='Rollback…').length===3);
  ok('R2-17 HOSTS_MAX agrees with runner LIST_MAX=128',h.HOSTS_MAX===128 && /LIST_MAX = 128/.test(fs.readFileSync(path.join(repo,'migui/runner.py'),'utf8')));
}
{
  const t=boot(true);await sleep(40);const h=t.win.miguiTest;
  const b={...BATCH1,at:Date.now(),impact:{vms:1,volumes:2,gb:60,stop_source:1,detach_ports:1},conf_digest:'conf-hash',fingerprint:'batch-hash',rows:[{ident:'vm-1',name:'app01',result:'PLAN-OK',impact:{source_after:'SHUTOFF',creates:{server:true}},checks:[{level:'WARN',text:'quota'},{level:'WARN',text:'quota'}]}]};
  h.state.jobs=[{kind:'vm',state:'OK',t_start:'2026-11-21T00:00:00Z',t_end:'2026-11-21T00:10:00Z',progress:{copy:{mbps:1024}}}];
  h.openConfirm(b,'options');
  ok('R2-11 estimate prefers progress.copy.mbps',texts(t.$('cf-impact')).some(x=>x.includes('ước lượng: 1 phút') && x.includes('MB/s')));
  h.state.jobs[0].progress=null;h.renderConfirm();
  ok('R2-11 estimate falls back to average duration',texts(t.$('cf-impact')).some(x=>x.includes('ước lượng: 10 phút') && x.includes('thời lượng trung bình')));
  ok('R2-14 conf snapshot excludes hashes and technical detail retains them',!t.$('cf-conf').textContent.includes('hash') && t.$('cf-technical').textContent.includes('conf-hash') && t.$('cf-technical').textContent.includes('batch-hash'));
  ok('R2-16 repeated WARN for one VM counts once with one acknowledgement',all(t.$('cf-warnings'),e=>e.tagName==='input').length===1 && texts(t.$('cf-warnings')).includes('1 VM (app01): quota'));
  b.commands=[...b.commands,['migrate','migrate','vm-2']];b.rows.push({...b.rows[0],ident:'vm-2',name:'app02'});h.openConfirm(b,'options');
  ok('R2-16 shared WARN counts distinct VMs with one group acknowledgement',all(t.$('cf-warnings'),e=>e.tagName==='input').length===1 && texts(t.$('cf-warnings')).includes('2 VM (app01, app02): quota'));
  const rule={mode:'name',expect:['shared','shared','VM with spaces'],ids:['id-1','id-2','id-3']};
  ok('R2-13 mixed IDs and whole names match once per job, IDs first',h.confirmationMatches({rule},'id-2\nVM with spaces\nshared') && !h.confirmationMatches({rule},'id-2\nid-2\nid-3') && !h.confirmationMatches({rule},'id-1\nid-2') && !h.confirmationMatches({rule},'id-1\nid-2\n VM with spaces'));
  h.openConfirm({...RB,at:Date.now(),original:{t_stop:null,dst_server_status:'GONE',age_seconds:null,source_power:'SHUTOFF',delete:{server:'dst-server',ports:['p1'],volumes:['v1','v2'],images:['i1']}}},'history');
  const f=h.state.rollbackFields;
  ok('R2-2/9 rollback action names the VM, red, with counts and IDs in details',t.$('dialog-title').textContent==='Rollback mig-peer' && t.$('dialog-accept').textContent==='Rollback mig-peer' && t.$('dialog-accept').className==='btn danger solid' && texts(t.$('dialog-body')).some(x=>x.includes('1 server · 1 port · 2 volume · 1 image')) && texts(find(t.$('dialog-body'),e=>e.tagName==='details')).includes('v1, v2'));
  ok('R2-9 rollback renders GONE, null time and source SHUTOFF without guessing',texts(t.$('dialog-body')).some(x=>x.includes('GONE')) && texts(t.$('dialog-body')).some(x=>x.includes('nguồn sẽ được giữ SHUTOFF')));
  f.typed.value='vm-3';await f.typed.fire('input');
  ok('R2-9 rollback ident alone is gated by RPO',t.$('dialog-accept').disabled);
  f.ack.checked=true;await f.ack.fire('change');
  ok('R2-9 RPO plus full ident unlocks rollback',!t.$('dialog-accept').disabled);
  f.typed.value='wrong';await f.typed.fire('input');
  ok('R2-9 wrong name locks rollback again',t.$('dialog-accept').disabled);
  const origin=t.location.hash;await t.$('dialog-close').fire('click');
  ok('R2-9 closing rollback preserves origin and discards token',t.location.hash===origin && !t.$('action-dialog').open && h.state.confirm===null && !find(t.$('dialog-body'),e=>e.id==='screen-confirm'));
  h.openConfirm({...RB,at:Date.now(),original:{source_power:'ACTIVE',delete:null}},'instance');
  ok('R2-9 ACTIVE source will restart and null deletion scope is safe',texts(t.$('dialog-body')).some(x=>x.includes('nguồn sẽ được bật lại')));
  ok('A20 rollback dialog: no source_status (not read) shows "Nguồn hiện tại: không đọc được"',texts(t.$('dialog-body')).includes('Nguồn hiện tại: không đọc được'));
  await t.$('dialog-close').fire('click');
  for (const [st,want] of [['SHUTOFF','Nguồn hiện tại: SHUTOFF'],['GONE','Nguồn hiện tại: không tìm thấy (đã xóa, hoặc credentials/scope/endpoint nguồn không thấy)'],[null,'Nguồn hiện tại: không đọc được']]) {
    h.openConfirm({...RB,at:Date.now(),original:{source_power:'ACTIVE',source_status:st,delete:null}},'instance');
    ok('A20 rollback dialog: source_status '+st+' shows "'+want+'", once',texts(t.$('dialog-body')).filter(x=>x.startsWith('Nguồn hiện tại: ')).length===1 && texts(t.$('dialog-body')).includes(want),texts(t.$('dialog-body')));
    await t.$('dialog-close').fire('click');
  }
  // 1.1.1 (A21): the destination server now, next to the source line
  for (const [orig,want] of [[{dst_server_status:'ACTIVE',delete:{server:'srv-dst-0001'}},'Đích hiện tại: ACTIVE'],
                             [{dst_server_status:'GONE',delete:{server:'srv-dst-0001'}},'Đích hiện tại: đã xóa'],
                             [{dst_server_status:null,dst_error:'public endpoint for compute service in RegionOne region not found',delete:{server:'srv-dst-0001'}},'Đích hiện tại: không đọc được'],
                             [{dst_server_status:null,delete:{server:'srv-dst-0001'}},'Đích hiện tại: không đọc được'],
                             [{dst_server_status:null,dst_error:null,delete:{server:null,volumes:['vol-dst-0001']}},'Đích hiện tại: không có server đích ghi nhận']]) {
    h.openConfirm({...RB,at:Date.now(),original:{source_power:'ACTIVE',source_status:'SHUTOFF',...orig}},'instance');
    const lines=texts(t.$('dialog-body'));
    ok('A21 rollback dialog: '+JSON.stringify(orig)+' shows "'+want+'", once, right after "Nguồn hiện tại"',lines.filter(x=>x.startsWith('Đích hiện tại: ')).length===1 && lines.includes(want) && lines.indexOf(want)===lines.indexOf('Nguồn hiện tại: SHUTOFF')+1,lines);
    await t.$('dialog-close').fire('click');
  }
  await t.$('dialog-close').fire('click');h.state.session.features={write_check:true};await t.$('doctor-write').fire('click');
  ok('R2-2 write-check button names the action',t.$('dialog-accept').textContent==='Kiểm tra ghi RBD');
  await t.$('dialog-close').fire('click');await t.$('vm-paste').fire('click');
  ok('R2-2 paste button retains matching-row action',t.$('dialog-accept').textContent==='Chọn các dòng khớp');
}
{
  let jobs=[],active=null;
  const activePath='/api/jobs?offset=0&limit=500&q=&state=RUNNING,QUEUED&kind=&from=&to=';
  const t=boot(true,{'/api/jobs':()=>jsonRes(200,{jobs,total:jobs.length,active}),[activePath]:()=>jsonRes(200,{jobs,total:jobs.length,active})});await sleep(50);const h=t.win.miguiTest;
  const first={id:'first',batch:'batch',name:'app01',kind:'vm',state:'RUNNING',t_start:'2026-11-21T00:00:00Z',phase_detail:null};
  const last={...first,id:'last',name:'app02',state:'QUEUED',t_start:null};
  jobs=[first,last];active={id:'batch',jobs:['first','last'],state:'RUNNING',expires_in:900};
  await h.refreshJobs();const n=t.count(activePath);active={...active,expires_in:897};await h.refreshJobs();await h.refreshJobs();
  ok('R2-12 active page fetched once per active transition, ignoring changing TTL',n===1 && t.count(activePath)===n);
  h.state.watch='first';h.renderRunning();
  ok('R2-5 empty running log shows first-line skeleton',!t.$('run-log-empty').classList.contains('hidden') && t.$('run-log-empty').textContent==='đang chờ dòng log đầu tiên…');
  h.state.es.fire('line',{t:new Date().toISOString(),text:'first log'});
  ok('R2-5 first log removes skeleton',t.$('run-log-empty').classList.contains('hidden'));
  jobs=[{...last,state:'OK',rc:0,t_start:'2026-11-21T00:01:00Z',t_end:'2026-11-21T00:02:00Z',phase:'done',phase_detail:{name:'done',volumes_total:3,volumes_done:3,done:['a','b','c']}},{...first,state:'OK',rc:0}];active=null;
  await h.refreshJobs();h.renderRunning();
  ok('R2-5 fast completed batch keeps its last job and record phase/volumes/result',h.state.watch==='last' && t.$('st-pass').textContent==='done' && t.$('st-vol').textContent==='3/3' && t.$('st-res').textContent==='OK' && t.count(activePath)===n+1);
  for (const [code,label] of [[3,'lỗi trước cutover — nguồn vẫn chạy'],[4,'lỗi sau cutover — nguồn đang tắt, cần rollback']]) {
    h.state.jobs=[{...jobs[0],state:'FAIL',rc:code,exit_class:code,phase_detail:{name:'copy',volumes_total:3,volumes_done:1}}];h.renderRunning();
    ok('R2-5 exit class '+code+' and partial volume count survive completion',t.$('st-res-note').textContent.includes(label) && t.$('st-vol').textContent==='1/3' && t.$('st-res').textContent==='FAIL');
  }
  const j={...first,cancel:{sigint_at:new Date(Date.now()-1000).toISOString()}};h.state.jobs=[j];h.state.watch=j.id;h.runLocks();
  ok('R2-6 SIGKILL locked via disabled and server countdown title only',t.$('run-kill').disabled && /^mở sau 11\d s$/.test(t.$('run-kill').title) && t.$('run-kill').textContent==='Buộc dừng (SIGKILL)');
}
{
  const sigint=new Date(Date.now()-10000).toISOString();
  const t=boot(true,{'/api/jobs/custom/cancel':{cancel:{sigint_at:sigint},force_in:230}});await sleep(40);const h=t.win.miguiTest;
  const j={id:'custom',name:'app01',state:'RUNNING'};await h.sendSigint(j);
  ok('R2-6 force_in overrides 120 fallback without invented cancel fields',h.cancelRemaining(j)>=229 && h.cancelRemaining(j)<=230);
}
{
  let release;const gate=new Promise(r=>{release=r;});
  const t=boot(true,{'/api/jobs':async()=>{await gate;return jsonRes(200,{jobs:[],total:120,active:null});}});
  await sleep(20);
  ok('R2-4 overview requests jobs immediately and shows loading skeletons',t.count('/api/jobs')===1 && !!find(t.$('overview-stats'),e=>hasCls(e,'skeleton')) && texts(t.$('overview-jobs')).includes('Đang tải hoạt động…'));
  release();await sleep(50);
  ok('R2-4 overview loads all per-state totals without loaded-history fallback',t.calls.filter(c=>c.path.includes('limit=1&')).length===6 && !texts(t.$('overview-stats')).join(' ').includes('theo lịch sử đã tải') && t.win.miguiTest.state.overviewCounts.length===4);
  t.hash('#history');t.hash('#overview');await sleep(40);
  ok('R2-4 reentering overview immediately reloads totals',t.calls.filter(c=>c.path.includes('limit=1&')).length===12);
}
// Astra round 3: the job followed from Tổng quan (S.watchPinned) has a lifecycle, and Esc closes one layer only.
{
  const t=boot(true); await sleep(40); const h=t.win.miguiTest;
  const old={id:'old',name:'data-archive-01',kind:'vm',ident:'vm-9',state:'OK',batch:'b0',t_start:'2026-11-22T01:00:00Z',t_end:'2026-11-22T01:10:00Z'};
  const run={id:'run',name:'app02',kind:'vm',ident:'vm-2',state:'RUNNING',batch:'b1',t_start:'2026-11-22T02:00:00Z'};
  const queued={id:'next',name:'app03',kind:'vm',ident:'vm-3',state:'QUEUED',batch:'b1'};
  h.state.jobs=[old,run,queued]; h.state.active={id:'b1',jobs:['run','next'],state:'RUNNING',continue_on_error:false};
  h.state.watch='old'; h.state.watchPinned=true; h.renderRunning(); h.runLocks();
  const queue=texts(t.$('run-queue')).join(' ');
  ok('PIN-1 a followed job that is not the running one: Chạy shows it, says which job runs, its own batch in the queue, '
     + 'stop-after (a control of the running batch) locked, no cancel panel for a finished job',
    t.$('run-title').textContent.startsWith('data-archive-01') && !t.$('run-pin-note').classList.contains('hidden')
    && t.$('run-pin-text').textContent.includes('app02') && queue.includes('data-archive-01') && !queue.includes('app03')
    && t.$('run-stop-after').disabled && t.$('run-cancel-panel').classList.contains('hidden'),
    {title:t.$('run-title').textContent, note:t.$('run-pin-text').textContent, queue, stop:t.$('run-stop-after').disabled});
  await t.$('run-pin-back').fire('click');
  ok('PIN-2 "Xem job đang chạy": the pin is released, the running job, its batch queue, its cancel panel and stop-after are back',
    !h.state.watchPinned && t.$('run-title').textContent.startsWith('app02') && t.$('run-pin-note').classList.contains('hidden')
    && texts(t.$('run-queue')).join(' ').includes('app03') && !t.$('run-stop-after').disabled
    && !t.$('run-cancel-panel').classList.contains('hidden'),
    {pinned:h.state.watchPinned, title:t.$('run-title').textContent, stop:t.$('run-stop-after').disabled});
  h.state.watch='run'; h.state.watchPinned=true; h.renderRunning();
  ok('PIN-3 a pin on the running job itself needs no note', t.$('run-pin-note').classList.contains('hidden'));
  h.state.watch='old'; h.state.watchPinned=true;
  await t.$('logout').fire('click'); await sleep(20);
  ok('PIN-4 logout forgets the followed job (watch and pin)', h.state.watch===null && h.state.watchPinned===false,
    {watch:h.state.watch, pinned:h.state.watchPinned});
}
{
  const t=boot(true); await sleep(40); const h=t.win.miguiTest;
  const old={id:'old',name:'data-archive-01',kind:'vm',ident:'vm-9',state:'OK',batch:'b0',t_start:'2026-11-22T01:00:00Z'};
  const run={id:'run',name:'app02',kind:'vm',ident:'vm-2',state:'RUNNING',batch:'b1',t_start:'2026-11-22T02:00:00Z'};
  t.hash('#running'); await sleep(40);  // the route refresh (fake /api/jobs: no active batch) settles first
  h.state.jobs=[old,run]; h.state.active={id:'b1',jobs:['run'],state:'RUNNING'};
  h.state.watch='old'; h.state.watchPinned=true; h.renderRunning();
  const before=t.$('run-title').textContent;
  await t.$('nav-running').fire('click');
  ok('PIN-5 the nav link Hàng đợi, clicked while Chạy is open (no hashchange): the pin is released and the running job drawn at once',
    before.startsWith('data-archive-01') && !h.state.watchPinned && t.$('run-title').textContent.startsWith('app02')
    && t.$('run-pin-note').classList.contains('hidden'), {before, after:t.$('run-title').textContent});
  h.state.watch='old'; h.state.watchPinned=true; h.renderRunning();
  let focused=null; t.$('run-title').focus=()=>{focused='run-title';};
  await t.$('run-pin-back').fire('click');
  ok('PIN-6 "Xem job đang chạy" hides itself and moves the focus to the job title', focused==='run-title' && t.$('run-title').tabindex==='-1');
  // stop-after acts on the batch drawn on the screen only
  h.state.watchPinned=false; h.renderRunning();
  const shownBatch=t.$('run-stop-after').dataset.batch;
  h.state.active={id:'b2',jobs:['run2'],state:'RUNNING'};  // a refresh changed the active batch, the screen not redrawn yet
  const n0=t.calls.filter(c=>c.path.startsWith('/api/batches/')).length;
  await t.$('run-stop-after').fire('click'); await sleep(20);
  const n1=t.calls.filter(c=>c.path.startsWith('/api/batches/')).length;
  ok('STOP-1 stop-after drawn for batch b1, active batch became b2 before the redraw: nothing is sent, the screen is redrawn',
    shownBatch==='b1' && n1===n0 && t.$('run-stop-after').dataset.batch==='', {shownBatch, n0, n1, now:t.$('run-stop-after').dataset.batch});
  h.state.jobs=[old,run]; h.state.active={id:'b1',jobs:['run'],state:'RUNNING'}; h.renderRunning();
  await t.$('run-stop-after').fire('click'); await sleep(20);
  const sent=t.calls.filter(c=>c.path.startsWith('/api/batches/')).map(c=>c.path);
  ok('STOP-2 stop-after on the batch drawn: POST /api/batches/b1/stop', sent.includes('/api/batches/b1/stop'), sent);
}
{
  const t=boot(true); await sleep(50); const h=t.win.miguiTest; const id='vm-src-host';
  await mselPart(t, id, 'msel-btn').fire('click');
  h.state.screen='history'; h.state.histOpen=new Set(['job-a']);
  const ev={key:'Escape'}; t.winFire('keydown', ev);
  ok('ESC-1 Esc with a multi-select open closes that box only: the History detail behind it stays open',
    !mselOpen(t, id) && ev.mselClosed===true && h.state.histOpen.size===1, {open:mselOpen(t, id), hist:h.state.histOpen.size});
  t.winFire('keydown', {key:'Escape'});
  ok('ESC-2 the next Esc closes the History detail', h.state.histOpen.size===0);
}
ok('WORDING-1 options not recorded: the job stopped before writing them, or ran an older version (not always "< 7.3")',
  SRC.includes("'không ghi nhận: job dừng trước khi ghi option, hoặc chạy bằng bản < 7.3'")
  && SRC.includes("'option gốc: không ghi nhận (job dừng trước khi ghi option, hoặc chạy bằng bản < 7.3)'")
  && !SRC.includes("'không ghi nhận (job chạy bằng bản < 7.3)'"));
ok('FOCUS-1 a disabled dialog opener is waited for only while the focus stays on the page body, on the same screen, '
   + 'with no new dialog; after 5 s the screen heading gets the focus, never the still-disabled opener',
  /const moved = document.activeElement && document.activeElement !== document.body;/.test(SRC)
  && SRC.includes("if (moved || S.screen !== screen || $('action-dialog').open) { clearInterval(t); return; }")
  && SRC.includes("if (target.isConnected === false) { clearInterval(t); heading?.setAttribute('tabindex', '-1'); heading?.focus?.(); return; }")
  && SRC.includes("if (++n > 50) { clearInterval(t); heading?.setAttribute('tabindex', '-1'); heading?.focus?.(); }"));
{
  let h=null;
  const t=boot(true,{"/api/batches/b1/stop":()=>{ h.state.active=null; return jsonRes(200,{}); }}); await sleep(40); h=t.win.miguiTest;
  t.hash("#running"); await sleep(40);
  h.state.jobs=[{id:"run",name:"app02",kind:"vm",ident:"vm-2",state:"RUNNING",batch:"b1"}]; h.state.active={id:"b1",jobs:["run"],state:"RUNNING"};
  h.renderRunning();
  let error=null; try { await t.$("run-stop-after").fire("click"); await sleep(20); } catch (e) { error=String(e); }
  ok("STOP-3 the batch ends while the stop request is in flight: no error, nothing marked", error===null && h.state.active===null, {error});
}
{
  let h=null; let posted=0; const batchB={id:"b2",jobs:["run2"],state:"RUNNING"};
  const t=boot(true,{"/api/batches/b1/stop":()=>{ posted++; h.state.active=batchB; return jsonRes(200,{}); }}); await sleep(40); h=t.win.miguiTest;
  t.hash("#running"); await sleep(40);
  h.state.jobs=[{id:"run",name:"app02",kind:"vm",ident:"vm-2",state:"RUNNING",batch:"b1"}]; h.state.active={id:"b1",jobs:["run"],state:"RUNNING"};
  h.renderRunning();
  let error=null; try { await t.$("run-stop-after").fire("click"); await sleep(20); } catch (e) { error=String(e); }
  ok("STOP-4 batch B became active while the stop request for A was in flight: B is not marked stopping", error===null && posted===1 && !batchB.stop, {error, posted, batchB});
}
// fix-1.0.1 (Review 23/11 #2): a cancel that reached a job after migrate had finished it: the job is OK (green), with a note
{
  const LATE = 'Hủy đến sau khi job đã xong';
  const late = { id: 'e'.repeat(16), name: 'app01', kind: 'vm', ident: 'vm-1', state: 'OK', rc: 0, batch: 'b9',
    argv: ['migrate', 'migrate', 'vm-1'], effective: {}, conf: {}, projects: { src: 'admin', dst: 'admin' },
    t_start: '2026-11-23T00:00:00Z', t_end: '2026-11-23T00:05:00Z', dst_server_id: 'srv-dst-fake',
    cancel: { sigint_at: '2026-11-23T00:04:59Z', late: true, exit: 130 } };
  const t = boot(true, { '/api/status': { jobs: [late], active: null, cli: { live: [] } } }); await sleep(50); const h = t.win.miguiTest;
  h.state.jobs = [late]; h.state.watch = late.id; h.renderRunning();
  ok('LATE-1 Chạy: a job cancelled after it finished stays OK (green) with the note "' + LATE + '"',
    t.$('st-res').textContent === 'OK' && t.$('st-res').className === 'v dot-ok' && t.$('st-res-note').textContent.includes(LATE)
    && t.$('st-res-note').textContent.includes('exit 0'), { res: t.$('st-res').textContent, note: t.$('st-res-note').textContent });
  h.state.jobs = [{ ...late, cancel: { sigint_at: '2026-11-23T00:04:59Z' }, state: 'CANCELLED', rc: 130 }]; h.renderRunning();
  ok('LATE-2 an ordinary cancel has no such note', !t.$('st-res-note').textContent.includes(LATE), t.$('st-res-note').textContent);
  await t.$('hist-refresh').fire('click');
  const tg = find(t.$('hist-rows'), (e) => e.tagName === 'button' && e.textContent === 'chi tiết ▾');
  if (tg) await tg.fire('click');
  const pre = find(t.$('hist-rows'), (e) => e.tagName === 'pre');
  ok('LATE-3 Lịch sử: the detail says the cancel came after the job had finished, with the exit migrate gave',
    !!pre && pre.textContent.includes('state OK · exit 0') && pre.textContent.includes(LATE + ' (migrate thoát 130)'),
    pre && pre.textContent);
}
// Static CSP, unique IDs, local assets and computed contrast form a release guard too.
{
  const html=fs.readFileSync(path.join(repo,'migui/static/index.html'),'utf8');
  const css=fs.readFileSync(path.join(repo,'migui/static/app.css'),'utf8');
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
  ok('R2-8 obsolete plain-list jobs and inferred impact branches removed',!SRC.includes('legacyHistory') && !SRC.includes('Array.isArray(data) ? {jobs:') && !SRC.includes('runner cũ') && !SRC.includes('Runner cũ'));
  ok('R2-10 static theme label and accessible action label are consistent',/id="theme-toggle"[^>]*aria-label="[^"]+">Giao diện<\/button>/.test(html) && SRC.includes("$('theme-toggle').setAttribute('aria-label',$('theme-toggle').textContent)"));
  ok('R2-1/3 copy points to the Network IP editor without fixed campaign step numbers',html.includes('ngay trong nhóm Mạng bên dưới') && !html.includes('ở bước Chọn (cột IP)') && !html.includes('ĐỢT MỚI · BƯỚC'));
  ok('V1 HTML has unique IDs and all literal JS element references exist',new Set(ids).size===ids.length && [...SRC.matchAll(/\$\('([^']+)'\)/g)].every(m=>ids.includes(m[1])));
  ok('V1 CSP: no HTML parsing, dynamic evaluation, inline handlers/styles or remote assets',
    !/\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval)\b|new\s+Function|\.style\./.test(SRC) &&
    !/\s(?:style|on\w+)\s*=|<style\b|<script\b(?![^>]*\bsrc=)/i.test(html) && !/url\s*\(|@import/.test(css));
  const palettes=[...css.matchAll(/:root(?:\[data-theme=dark\])?\{([^}]+)\}/g)].slice(0,2).map(m=>Object.fromEntries([...m[1].matchAll(/--([\w-]+):([^;}]+)/g)].map(x=>[x[1],x[2]])));
  const lum=hex=>{const c=hex.replace('#','').match(/../g).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);return c[0]*.2126+c[1]*.7152+c[2]*.0722;};
  const ratio=(a,b)=>{a=lum(a);b=lum(b);return(Math.max(a,b)+.05)/(Math.min(a,b)+.05);};
  const pairs=[['fg','card'],['fg','bg'],['muted','card'],['muted','bg'],['subtle','card'],['subtle','bg'],['okfg','okbg'],['warnfg','warnbg'],['failfg','failbg'],['blue','card'],['blue2','sel'],['muted','skipbg'],['headfg','head'],['navfg','head'],['dim','head']];
  const results=palettes.flatMap((p,i)=>pairs.map(([a,b])=>({theme:i,pair:a+'/'+b,ratio:ratio(p[a] || palettes[0][a],p[b] || palettes[0][b])})));
  results.push({pair:'primary',ratio:ratio('#FFFFFF','#1F5FBF')},{pair:'danger',ratio:ratio('#FFFFFF','#B42318')},{pair:'log',ratio:ratio('#D5DCE6','#0F141B')});
  const min=Math.min(...results.map(x=>x.ratio));
  ok('V1 contrast: all light/dark text token pairs >= 4.5:1; minimum '+min.toFixed(2)+':1',palettes.length===2 && results.every(x=>x.ratio>=4.5),results.filter(x=>x.ratio<4.5));
}
console.log(`TOTAL pass=${pass} fail=${fail}`);
process.exit(fail ? 1 : 0);
