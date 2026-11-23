// migrate operator UI (DESIGN-UI 5, 13.3, 14). No framework, no CDN. Every string from the server is rendered with
// textContent / createElement (T6): this file never parses a string as HTML or builds markup from data.
'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
  const SRC_LABEL = { cli: 'gõ tay', default: 'mặc định', MIG_DEFAULT_OPTS: 'MIG_DEFAULT_OPTS', conf: 'conf map',
    source: 'lấy từ nguồn', az: '= az' };
  const STATE_LABEL = { PLANNED: 'đã plan', QUEUED: 'chờ', RUNNING: 'đang chạy', OK: 'OK', FAIL: 'FAIL',
    CANCELLED: 'đã hủy', SKIPPED: 'bỏ qua (đợt dừng)', LOST: 'mất dấu (runner khởi động lại)',
    DETACHED: 'đang chạy ngoài UI (runner khởi động lại)' };
  // Review 23/11 #2: a cancel that reached a job after migrate had finished it (result rc 0): the job stays OK
  const LATE_CANCEL = 'Hủy đến sau khi job đã xong';
  const S = {
    session: null, doctorOk: false, inv: null, help: {},
    // src (21): the source hosts / projects ticked in the multi-selects, kept for the whole session
    vm: { rows: [], sel: new Set(), ip: {}, plan: {}, batch: null, src: { hosts: new Set(), projects: new Set() } },
    vol: { rows: [], sel: new Set(), plan: {}, batch: null, src: { projects: new Set() } },
    confirm: null, jobs: [], active: null, activeKey: 'null', cli: null,
    watch: null, es: null, esJob: null, clock: null, tick: null, poll: null, killTimer: null, armed: false,
    histOpen: new Set(), cmpA: null, cmpB: null,
  };

  // --- DOM helpers (textContent only) ---------------------------------------------------------------------------
  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') e.className = v;
        else if (k === 'text') e.textContent = String(v);
        else if (k === 'on') for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
        else if (k === 'checked' || k === 'disabled' || k === 'value' || k === 'colSpan' || k === 'selected') e[k] = v;
        else e.setAttribute(k, String(v));
      }
    }
    for (const c of kids.flat()) {
      if (c === null || c === undefined || c === false) continue;
      e.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    }
    return e;
  }
  const clear = (n) => { n.replaceChildren(); return n; };
  const show = (n, on) => n.classList.toggle('hidden', !on);
  function flash(text, kind) {
    const f = $('flash');
    f.textContent = text;
    f.className = 'msg ' + (kind || 'err');
    show(f, !!text);
    clearTimeout(S.toastTimer);
    if (text && kind && kind !== 'err') S.toastTimer = setTimeout(() => show(f, false), 8000);
  }
  const fmtTime = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    const p = (x) => String(x).padStart(2, '0');
    return p(d.getDate()) + '/' + p(d.getMonth() + 1) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  };
  const hms = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toTimeString().slice(0, 8);
  };
  const mmss = (s) => {
    s = Math.max(0, Math.floor(s));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  };
  const dur = (s) => (s >= 60 ? Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's' : s + 's');
  const short = (id) => (id && id.length > 20 ? id.slice(0, 8) + '…' : id || '');
  const pillFor = (res) => {
    const r = String(res || '');
    if (r === 'PLAN-OK' || r === 'READY' || r === 'OK' || r.startsWith('OK')) return 'pill ok';
    if (r.startsWith('PLAN-FAIL') || r.startsWith('FAIL') || r === 'LOST') return 'pill fail';
    if (r === 'RUNNING' || r === 'QUEUED') return 'pill run';
    if (r === 'CANCELLED' || r === 'DETACHED' || r.startsWith('WARN')) return 'pill warn';
    return 'pill skip';
  };
  const basename = (p) => String(p || '').split('/').pop();

  // --- check tables (Doctor, and the plan checks of 17): a level pill + the text, every level shown ----------------
  function checkRow(c) {
    const lv = String((c && c.level) || '');
    const cls = lv === 'OK' ? 'pill ok' : lv === 'FAIL' ? 'pill fail' : lv === 'WARN' ? 'pill warn' : 'pill skip';
    return el('tr', null, el('td', null, el('span', { class: cls, text: lv }, icon(lv === 'OK' ? 'check' : lv === 'FAIL' ? 'x' : lv === 'WARN' ? 'warn' : 'info'))), el('td', { text: c && c.text }));
  }
  function checkCounts(checks) {
    const n = { OK: 0, INFO: 0, WARN: 0, FAIL: 0 };
    for (const c of checks || []) {
      const lv = String((c && c.level) || '');
      if (lv in n) n[lv] += 1;
    }
    return n;
  }
  // The plan checks of a confirm row or of a job record; a job planned before 17 has none: "không ghi nhận".
  function checkTable(checks) {
    if (!Array.isArray(checks)) return el('div', { class: 'hint', text: 'không ghi nhận' });
    if (!checks.length) return el('div', { class: 'hint', text: 'plan không có check nào' });
    return el('div', { class: 'scroll' }, el('table', null,
      el('thead', null, el('tr', null, el('th', { text: 'Mức' }), el('th', { text: 'Kiểm tra' }))),
      el('tbody', null, checks.map(checkRow))));
  }

  // --- API ------------------------------------------------------------------------------------------------------
  async function api(method, path, body, background) {
    const headers = { 'X-Requested-With': 'migui' };
    if (background) headers['X-Migui-Background'] = '1';
    const init = { method, headers, credentials: 'same-origin', cache: 'no-store' };
    if (method === 'POST') {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body || {});
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      return { status: 0, data: { error: 'mất kết nối tới migui-web (SSH tunnel còn không?)' } };
    }
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (res.status === 401 && path !== '/api/login') showLogin();
    if (res.status === 412) { flash('Doctor phải xanh trước: chạy lại Doctor.'); location.hash = '#doctor'; }
    return { status: res.status, data: data || {} };
  }
  const err = (r) => (r.data && r.data.error ? r.data.error : 'HTTP ' + r.status);

  // --- DESIGN-UI 15: every button that calls the API is locked while its request runs --------------------------
  // busy(button, label, fn): a second click (or Enter) while fn runs is a no-op; the button is disabled and shows
  // label; afterwards (success, error or exception) its text and state come back, then applyLocks() re-applies the
  // locks that outlive a request (a running batch, a plan in flight, a SIGKILL already sent, a stop already asked).
  async function busy(btn, label, fn) {
    if (!btn || btn.dataset.busy === '1' || btn.disabled) return undefined;
    const text = btn.textContent;
    btn.dataset.busy = '1';
    btn.setAttribute('aria-busy','true');
    btn.disabled = true;
    if (label) btn.textContent = label;
    try {
      return await fn();
    } finally {
      delete btn.dataset.busy;
      btn.setAttribute('aria-busy','false');
      btn.disabled = false;
      if (label) btn.textContent = text;
      applyLocks();
    }
  }
  function lock(btn, disabled, text) {
    if (!btn || btn.dataset.busy === '1') return;
    btn.disabled = !!disabled;
    if (text !== undefined) btn.textContent = text;
  }
  function applyLocks() {
    srcLocks();
    vmCounts();
    volCounts();
    cfRunState();
    runLocks();
  }

  // --- screens --------------------------------------------------------------------------------------------------
  const SCREENS = ['login', 'overview', 'doctor', 'instance', 'options', 'volume', 'volume-options', 'confirm', 'running', 'history'];
  function route() {
    if (!S.session) return showLogin();
    const entering = (location.hash || '#overview').slice(1);
    if (S.screen !== entering) flash('');
    let name = entering;
    if (!SCREENS.includes(name) || name === 'login') name = 'overview';
    if (name === 'confirm' && !S.confirm) name = 'instance';
    if (name === 'confirm' && !S.doctorOk && S.confirm?.kind !== 'rollback') {
      // 19.1: a session that has not run Doctor yet (a reload makes a new one) keeps the confirmation it was opening:
      // run Doctor (one run, shared with the login-time one), back to the confirm screen when it passes
      flash('Doctor chưa chạy trong phiên này, đang chạy…', 'info');
      runDoctor().then((passed) => {
        if (!passed || !S.confirm) return;
        if (location.hash !== '#confirm') location.hash = '#confirm';
        else route();
      });
      name = 'doctor';
    }
    if ((name === 'instance' || name === 'options' || name === 'volume' || name === 'volume-options') && !S.doctorOk) name = 'doctor';
    S.screen = name;
    for (const s of SCREENS) show($('screen-' + s), s === name);
    updateStepper(name);
    const heading = $('screen-' + name).querySelector?.('h1, h2');
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }
    if (name === 'overview') { S.overviewAt = null; S.overviewLoading = true; renderOverview(); refreshJobs(); }
    if (name === 'options') { renderRowIps(); previewOptions(); }
    if (name === 'volume-options') previewVolume();
    if (['volume','volume-options'].includes(name)) S.resourceKind='vol';
    if (['instance','options'].includes(name)) S.resourceKind='vm';
    $('step-options').href=S.resourceKind === 'vol'?'#volume-options':'#options';
    for (const a of document.querySelectorAll('#nav a')) a.classList.toggle('active', a.dataset.screen === name);
    if (name === 'running') refreshJobs();
    if (name === 'history') refreshHistory();
  }
  function showLogin() {
    if ($('action-dialog').open) closeDialog();
    S.session = null;
    S.overviewCounts=null;S.overviewAt=null;S.activeKey='null';S.active=null;S.campaignDoctor=false;
    S.confirm = null; S.vm.batch = null; S.vol.batch = null; S.watch = null; S.watchPinned = false;
    show($('stepper'), false);
    stopStream();
    for (const s of SCREENS) show($('screen-' + s), s === 'login');
    show($('nav'), false);
    show($('logout'), false);
    $('session-info').textContent = '';
    $('session-info').title = '';
    $('login-user').focus();
  }
  function showApp() {
    show($('nav'), true);
    show($('logout'), true);
    sessionInfo();
    $('app-version').textContent = S.session.version || '';
    document.title = 'migrate' + (S.session.version ? ' · ' + S.session.version : '');
    show($('stepper'), true);
    show($('doctor-write'), !!S.session.features?.write_check);
    setDoctor(S.session.doctor_ok);
    if (S.doctorOk && !S.inv) loadInventory();
    if (!S.doctorOk) runDoctor();  // 19.1: a new session (login, reload) runs Doctor once by itself
    if (!S.poll) S.poll = setInterval(() => { if (S.session) refreshJobs(true); }, 3000);
    route();
  }
  // 16.3: "<user> · phiên hết hạn sau 7h59"; the session id and the tunnel stay in the tooltip
  function sessionInfo() {
    if (!S.session) return;
    const left = Math.max(0, S.session.expires_in - Math.floor((Date.now() - S.session.at) / 1000));
    const info = $('session-info');
    info.textContent = (S.session.user || '?') + ' · phiên hết hạn sau ' + Math.floor(left / 3600) + 'h' +
      String(Math.floor((left % 3600) / 60)).padStart(2, '0');
    info.title = 'phiên ' + S.session.session + ' · mig-host ' + location.host + ' qua SSH tunnel';
  }
  function setDoctor(ok) {
    S.doctorOk = !!ok;
    $('doctor-dot').className = ok ? 'dot-ok' : (S.doctorRan ? 'dot-fail' : 'dot-none');
    for (const a of document.querySelectorAll('#nav a')) {
      if (a.dataset.screen === 'instance' || a.dataset.screen === 'volume') a.classList.toggle('locked', !ok);
    }
  }

  // --- login ----------------------------------------------------------------------------------------------------
  // 16.3: user name + password. 15 (c): both fields and the button are locked while one login is in flight: a
  // double Enter with a wrong password would otherwise count twice toward the lockout. The password is sent as
  // typed (spaces are allowed in it) and the field is emptied after every attempt.
  $('login-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const user = $('login-user'), pass = $('login-password');
    return busy($('login-submit'), 'Đang đăng nhập…', async () => {
      user.disabled = true;
      pass.disabled = true;
      try {
        const r = await api('POST', '/api/login', { username: user.value.trim(), password: pass.value });
        pass.value = '';
        if (r.status === 429) return flash('Sai quá nhiều lần: ' + err(r));
        if (r.status === 503) return flash('Chưa có tài khoản hoặc file tài khoản hỏng: ' + err(r));
        if (r.status !== 200) return flash('Đăng nhập thất bại: ' + err(r));
        flash('');
        S.session = Object.assign({ at: Date.now() }, r.data);
        location.hash = '#overview';
        showApp();
      } finally {
        user.disabled = false;
        pass.disabled = false;
      }
    });
  });
  $('logout').addEventListener('click', () => busy($('logout'), 'Đang đăng xuất…', async () => {
    await api('POST', '/api/logout', {});
    S.inv = null;
    showLogin();
  }));

  // --- doctor ---------------------------------------------------------------------------------------------------
  // runDoctor(): the one Doctor run (button, login, route '#confirm', 19.1) -> a promise of "passed". S.doctorRunning
  // holds the run in flight: a second caller gets the same promise, never a second POST /api/doctor.
  function runDoctor() {
    if (S.doctorRunning) return S.doctorRunning;
    const run = (async () => {
      try {
        return !!(await busy($('doctor-run'), 'Đang chạy…', doctorOnce));
      } finally {
        S.doctorRunning = null;
      }
    })();
    S.doctorRunning = run;
    return run;
  }
  $('doctor-run').addEventListener('click', () => runDoctor());
  async function doctorOnce(writeCheck = false) {
    $('doctor-summary').classList.add('skeleton');
    const r = await api('POST', '/api/doctor', writeCheck ? { write_check: true } : {});
    $('doctor-summary').classList.remove('skeleton');
    S.doctorAt = new Date().toISOString();
    $('doctor-time').textContent = 'Lần kiểm tra gần nhất: ' + fmtTime(S.doctorAt);
    S.doctorRan = true;
    if (r.status !== 200) { setDoctor(false); flash('Doctor: ' + err(r)); return false; }
    flash(r.data.doctor_ok ? '' : 'Doctor chưa xanh: xem các dòng FAIL bên dưới.');
    const d = r.data.data || {};
    const body = clear($('doctor-rows'));
    const groups = new Map();
    for (const c of d.checks || []) { const g = doctorGroup(c.text); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
    for (const [g, checks] of groups) { body.appendChild(el('tr', {class:'batch-heading'}, el('td', {colSpan:2, text:g}))); for (const c of checks) body.appendChild(checkRow(c)); }
    for (const line of (!d.checks && r.data.stderr) || []) body.appendChild(el('tr', null, el('td'), el('td', { class: 'mono', text: line })));
    const cn = d.counts || {};
    clear($('doctor-summary')).appendChild(el('div', { class: 'msg ' + (r.data.doctor_ok ? 'okm' : 'err'),
      text: (r.data.doctor_ok ? 'Doctor xanh' : 'Doctor chưa xanh (exit ' + r.data.rc + ')') + ' · ' +
        (cn.ok || 0) + ' OK · ' + (cn.warn || 0) + ' WARN · ' + (cn.fail || 0) + ' FAIL · ' + (cn.info || 0) + ' INFO' +
        (d.tool_version ? ' · migrate ' + d.tool_version : '') }));
    setDoctor(r.data.doctor_ok);
    if (r.data.doctor_ok) await loadInventory();
    return !!r.data.doctor_ok;
  }

  // --- inventory -> dropdowns -----------------------------------------------------------------------------------
  function fill(sel, first, items) {
    clear(sel);
    if (first) sel.appendChild(el('option', { value: '', text: first }));
    for (const it of items) sel.appendChild(el('option', { value: it.value, text: it.text, disabled: !!it.disabled }));
  }
  const projValue = (p) => (NAME_RE.test(p.name || '') ? p.name : p.id);
  async function loadInventory() {
    const r = await api('POST', '/api/inventory', {});
    if (r.status !== 200 || !r.data.data) return flash('Inventory: ' + err(r) + ' ' + ((r.data.stderr || []).slice(-1)[0] || ''));
    const inv = (S.inv = r.data.data);
    S.help = {};
    for (const [opt, _m, h] of (r.data.catalog && r.data.catalog.help) || []) S.help[opt] = h;
    for (const n of document.querySelectorAll('[data-help]')) {
      const h = S.help[n.dataset.help];
      if (h) n.title = n.dataset.help + ': ' + h;
    }
    const src = inv.source || {}, dst = inv.destination || {};
    MSEL.vmHosts.setItems((src.hosts || []).filter((h) => NAME_RE.test(h.host || ''))
      .map((h) => ({ value: h.host, text: h.host, note: (h.zone || '?') + ', ' + h.state + '/' + h.status })));
    const projs = (src.projects || []).filter((p) => NAME_RE.test(projValue(p) || ''))
      .map((p) => ({ value: projValue(p), text: p.name || p.id, note: p.id }));
    MSEL.vmProjects.setItems(projs);
    MSEL.volProjects.setItems(projs);
    const zc = ((dst.zones || {}).compute || []).map((z) => ({ value: z, text: z }));
    const zv = ((dst.zones || {}).volume || []).map((z) => ({ value: z, text: z }));
    const conf = inv.conf || {};
    fill($('vm-dst-az'), '— không chọn' + (conf.MIG_AZ ? ' (MIG_AZ=' + conf.MIG_AZ + ')' : ' (scheduler)') + ' —', zc);
    fill($('vol-az'), '— không chọn —', zc);
    $('vm-dst-az-note').textContent = conf.AZ_TYPE_MAP ? 'AZ_TYPE_MAP ' + conf.AZ_TYPE_MAP : 'AZ_TYPE_MAP trống';
    const types = (dst.volume_types || []).map((t) => ({ value: t, text: t }));
    fill($('o-volume-type'), 'mặc định (AZ_TYPE_MAP / đích)', types);
    fill($('vol-volume-type'), 'mặc định (AZ_TYPE_MAP / đích)', types);
    fill($('o-volume-az'), '= az (MIG_VOLUME_AZ nếu có)', zv);
    fill($('vol-volume-az'), '= az (MIG_VOLUME_AZ nếu có)', zv);
    fill($('o-flavor'), 'cùng tên ở đích', (dst.flavors || []).map((f) => ({ value: f.name, text: f.name + ' (' + f.vcpus + ' vCPU, ' + f.ram + ' MB)' })));
    const dprojs = (dst.projects || []).map((p) => ({ value: p.name, text: p.name }));
    for (const id of ['vm-dst-project','vol-dst-project']) $(id).textContent = conf.PROJECT_MAP_FILE ? 'Theo PROJECT_MAP_FILE: ' + conf.PROJECT_MAP_FILE : 'Cùng tên project nguồn';
    $('vm-dst-project-note').textContent = conf.PROJECT_MAP_FILE ? 'PROJECT_MAP_FILE ' + conf.PROJECT_MAP_FILE : '= tên nguồn, PROJECT_MAP_FILE không có';
    dstHosts();
  }
  function dstHosts() {
    const az = $('vm-dst-az').value;
    const hosts = ((S.inv && S.inv.destination && S.inv.destination.hosts) || []).filter((h) => NAME_RE.test(h.host || ''));
    fill($('vm-dst-host'), 'Nova scheduler chọn (mặc định)', hosts.map((h) => {
      const up = h.state === 'up' && h.status === 'enabled';
      const inAz = !az || h.zone === az;
      return { value: h.host, disabled: !up || !inAz,
        text: h.host + ' (' + h.zone + ', ' + h.state + (h.status !== 'enabled' ? ', ' + h.status : '') + ')' + (!inAz ? ' — khác AZ' : '') };
    }));
  }
  $('vm-dst-az').addEventListener('change', dstHosts);

  // --- Instance --------------------------------------------------------------------------------------------------
  // "— mọi host —" without a project: every source nova-compute host of the inventory that is up, in inventory
  // order, as one --host each (the runner and the web take at most HOSTS_MAX).
  const HOSTS_MAX = 128;
  function srcHostsForAll(inv) {
    const seen = new Set();
    return ((inv && inv.source && inv.source.hosts) || [])
      .filter((h) => h && h.state === 'up' && NAME_RE.test(h.host || '') && !seen.has(h.host) && seen.add(h.host))
      .map((h) => h.host);
  }

  // --- DESIGN-UI 21: multi-select of the source hosts / projects --------------------------------------------------
  // One widget for the three boxes: a button showing the summary ("mọi host", "cmp1", "3 host") that opens a panel of
  // checkboxes (a search box when more than 8 entries, "Tất cả" / "Bỏ hết", a warning above HOSTS_MAX). The ticks
  // live in a Set of S (S.vm.src / S.vol.src) for the whole session; Esc or a click outside closes the panel.
  const MSEL_SEARCH_MIN = 8;
  const MSELS = [];
  const summarize = (values, noun, none) => (!values.length ? none : values.length === 1 ? values[0] : values.length + ' ' + noun);
  function inside(ev, root) {
    const p = ev && typeof ev.composedPath === 'function' ? ev.composedPath() : null;
    if (p && p.length) return p.includes(root);
    for (let n = ev && ev.target; n; n = n.parentNode) if (n === root) return true;
    return false;
  }
  function multiSelect(box, cfg) {
    // cfg: set (the Set of S it edits), noun ('host' | 'project'), none (summary of an empty set)
    const set = cfg.set;
    const ms = { box, items: [], open: false };
    const btn = el('button', { type: 'button', class: 'msel-btn', 'aria-haspopup': 'true', 'aria-expanded': 'false' });
    const search = el('input', { type: 'search', class: 'msel-search', placeholder: 'tìm ' + cfg.noun });
    const count = el('span', { class: 'hint push' });
    const warn = el('div', { class: 'msel-warn' });
    const list = el('div', { class: 'msel-list' });
    const shownItems = () => {
      const q = search.value.trim().toLowerCase();
      return ms.items.filter((it) => !q || (it.text + ' ' + (it.note || '') + ' ' + it.value).toLowerCase().includes(q));
    };
    const allBtn = el('button', { type: 'button', class: 'btn small', text: 'Tất cả', on: { click: () => {
      for (const it of shownItems()) set.add(it.value);
      ms.render();
    } } });
    const noneBtn = el('button', { type: 'button', class: 'btn small', text: 'Bỏ hết', on: { click: () => { set.clear(); ms.render(); } } });
    const panel = el('div', { class: 'msel-panel', role: 'group' }, search, el('div', { class: 'bar' }, allBtn, noneBtn, count), warn, list);
    show(panel, false);
    show(warn, false);
    clear(box).append(btn, panel);
    // the ticked values in inventory order (a value no longer in the inventory is dropped by setItems)
    ms.values = () => ms.items.filter((it) => set.has(it.value)).map((it) => it.value);
    ms.sync = () => {
      const v = ms.values();
      const over = v.length > HOSTS_MAX;
      btn.textContent = summarize(v.map((x) => (ms.items.find((it) => it.value === x) || {}).text || x), cfg.noun, cfg.none);
      btn.title = v.length ? v.join(', ') : cfg.none;
      btn.classList.toggle('over', over);
      count.textContent = 'đã tick ' + v.length + '/' + ms.items.length;
      warn.textContent = over ? 'Đã tick ' + v.length + ' ' + cfg.noun + ', tối đa ' + HOSTS_MAX + ' cho một lần tải: bỏ bớt.' : '';
      show(warn, over);
      srcLocks();
    };
    ms.render = () => {
      const items = shownItems();
      clear(list);
      for (const it of items) {
        const cb = el('input', { type: 'checkbox', value: it.value, checked: set.has(it.value) });
        cb.addEventListener('change', () => {
          if (cb.checked) set.add(it.value); else set.delete(it.value);
          ms.sync();
        });
        list.appendChild(el('label', { class: 'msel-item' }, cb, el('span', { text: it.text }),
          it.note ? el('span', { class: 'hint', text: it.note }) : null));
      }
      if (!items.length) {
        list.appendChild(el('div', { class: 'hint', text: ms.items.length ? 'không ' + cfg.noun + ' nào khớp "' + search.value + '"'
          : 'inventory chưa có ' + cfg.noun + ' nào' }));
      }
      ms.sync();
    };
    ms.setItems = (items) => {
      const seen = new Set();
      ms.items = items.filter((it) => !seen.has(it.value) && seen.add(it.value));
      for (const v of [...set]) if (!seen.has(v)) set.delete(v);
      show(search, ms.items.length > MSEL_SEARCH_MIN);
      if (ms.items.length <= MSEL_SEARCH_MIN) search.value = '';
      ms.render();
    };
    ms.setOpen = (on) => {
      ms.open = !!on;
      show(panel, ms.open);
      btn.setAttribute('aria-expanded', ms.open ? 'true' : 'false');
      if (ms.open && ms.items.length > MSEL_SEARCH_MIN) search.focus();
    };
    btn.addEventListener('click', () => {
      const on = !ms.open;
      closeMsels();
      ms.setOpen(on);
    });
    search.addEventListener('input', ms.render);
    ms.btn = btn;
    MSELS.push(ms);
    ms.render();
    return ms;
  }
  function closeMsels() { for (const ms of MSELS) if (ms.open) ms.setOpen(false); }
  window.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    const open = MSELS.find((ms) => ms.open);
    if (!open) return;
    closeMsels();
    open.btn.focus();
    ev.mselClosed = true;  // the page-wide Esc handler below leaves the History detail behind it alone
    ev.stopImmediatePropagation?.();
  });
  window.addEventListener('click', (ev) => { for (const ms of MSELS) if (ms.open && !inside(ev, ms.box)) ms.setOpen(false); });
  const MSEL = {
    vmHosts: multiSelect($('vm-src-host'), { set: S.vm.src.hosts, noun: 'host', none: 'mọi host' }),
    vmProjects: multiSelect($('vm-src-project'), { set: S.vm.src.projects, noun: 'project', none: 'mọi project' }),
    volProjects: multiSelect($('vol-src-project'), { set: S.vol.src.projects, noun: 'project', none: 'chọn project' }),
  };
  // Above HOSTS_MAX ticks the load button stays locked with the reason (15: lock() leaves a busy button alone).
  function srcLocks() {
    const h = S.vm.src.hosts.size > HOSTS_MAX, p = S.vm.src.projects.size > HOSTS_MAX;
    lock($('vm-load'), h || p, h ? 'Tối đa ' + HOSTS_MAX + ' host' : p ? 'Tối đa ' + HOSTS_MAX + ' project' : 'Tải danh sách');
    const v = S.vol.src.projects.size > HOSTS_MAX;
    lock($('vol-load'), v, v ? 'Tối đa ' + HOSTS_MAX + ' project' : 'Tải danh sách');
  }
  // test hook (no data, no side effect by itself); msel lets tests/ui/guards.mjs give a box its entries before the
  // inventory exists (a Doctor still running), as a <select>'s value could be set before
  const testMode = new URLSearchParams(location.search).get('test') === '1';
  if (testMode) window.miguiTest = { srcHostsForAll, HOSTS_MAX, MSEL_SEARCH_MIN, msel: MSEL };
  $('vm-load').addEventListener('click', () => busy($('vm-load'), 'Đang tải…', async () => {
    closeMsels();
    const picked = MSEL.vmHosts.values(), projects = MSEL.vmProjects.values();
    if (picked.length > HOSTS_MAX || projects.length > HOSTS_MAX) return flash('Tối đa ' + HOSTS_MAX + ' host và ' + HOSTS_MAX + ' project cho một lần tải.');
    let hosts = picked;
    if (!picked.length && !projects.length) {
      hosts = srcHostsForAll(S.inv);
      if (!hosts.length) return flash('Inventory không có host nguồn nào đang up: chọn project.');
      if (hosts.length > HOSTS_MAX) return flash('Hơn ' + HOSTS_MAX + ' host: chọn host hoặc project');
    }
    const t0 = Date.now();
    const r = await api('POST', '/api/list', { hosts, projects });
    if (r.status !== 200 || !r.data.data) return flash('Tải danh sách: ' + err(r) + ' ' + ((r.data.stderr || []).slice(-1)[0] || ''));
    flash('');
    $('vm-load-cmd').textContent = (r.data.argv || []).join(' ');
    $('vm-load-time').textContent = '· ' + ((Date.now() - t0) / 1000).toFixed(1).replace('.', ',') + ' s';
    S.vm.rows = r.data.data.rows || [];
    S.vm.sel.clear();
    S.vm.plan = {};
    S.vm.batch = null; S.vm.stale = false; if (S.confirm?.kind === 'vm') S.confirm = null;
    $('vm-table-title').textContent = 'Instance trên ' + summarize(picked, 'host', 'mọi host') + ' / '
      + summarize(projects, 'project', 'mọi project') + ' (' + S.vm.rows.length + ')';
    $('vm-table-title').title = 'host: ' + (picked.length ? picked.join(', ')
      : 'mọi host' + (hosts.length ? ' đang up (' + hosts.length + '): ' + hosts.join(', ') : ''))
      + '\nproject: ' + (projects.length ? projects.join(', ') : 'mọi project');
    renderVm();
  }));
  const vmIps = (row) => (row.networks || []).flatMap((n) => n.ips || []);
  function vmMatches(row, q) {
    if (!q) return true;
    const hay = [row.name, row.id, row.host, row.az, projectText(row), ...vmIps(row)].join(' ').toLowerCase();
    return hay.includes(q);
  }
  const vmEligible = (row) => row.result === 'selected' && !(row.state && row.state.live);
  function planCell(p, row) {
    const td = el('td', { class: 'nowrap' });
    if (row.state && (row.state.live || row.state.phase)) {
      td.appendChild(el('span', { class: 'small', text: migrationState(row.state) }));
      return td;
    }
    if (row.result && row.result !== 'selected') {
      td.appendChild(el('span', { class: 'pill skip', text: row.result }));
      return td;
    }
    if (!p) { td.appendChild(el('span',{class:'hint',text:row.state?.rolled_back ? 'Đã rollback' : 'Chưa chuyển'})); return td; }
    td.appendChild(el('span', { class: pillFor(p.result), text: p.result }));
    const c = p.counts;
    if (c) td.appendChild(el('span', { class: 'hint', text: ' ' + c.ok + ' OK' + (c.warn ? ' · ' + c.warn + ' WARN' : '') + (c.fail ? ' · ' + c.fail + ' FAIL' : '') }));
    if (p.info && p.result !== 'PLAN-OK') td.appendChild(el('div', { class: 'small red', text: String(p.info).split('\n').slice(0, 3).join(' · ') }));
    return td;
  }
  function rollbackBtn(row) {
    if (!(row.state && row.state.live)) return el('td');
    return el('td', null, el('button', { type: 'button', class: 'btn danger small', text: 'Rollback…',
      on: { click: (ev) => startRollback(basename(row.state.dir), ev.currentTarget) } }));
  }
  function renderVm() {
    const q = $('vm-search').value.trim().toLowerCase();
    const body = clear($('vm-rows'));
    let shown = 0;
    for (const row of S.vm.rows) {
      if (!vmMatches(row, q)) continue;
      shown++;
      const ok = vmEligible(row);
      const sel = S.vm.sel.has(row.id);
      const cb = el('input', { type: 'checkbox', 'aria-label': row.name || row.id, checked: sel, disabled: !ok });
      const tr = el('tr', { class: sel ? 'selected' : '' });
      tr.addEventListener('click', (ev) => { if (ev.target.closest?.('button,a,input,summary')) return; const checkbox = tr.querySelector?.('input[type=checkbox]'); if (checkbox && !checkbox.disabled) checkbox.click(); });
      cb.addEventListener('change', () => {
        if (cb.checked) S.vm.sel.add(row.id); else S.vm.sel.delete(row.id);
        tr.className = cb.checked ? 'selected' : '';
        vmCounts();
      });
      const vols = row.volumes || [];
      const gb = vols.reduce((a, v) => a + (v.size || 0), 0);
      const ipTd = el('td', { class: 'mono nowrap' }, vmIps(row).join(', ') || '—');
      tr.append(el('td', null, cb), el('td', null, ok ? el('strong', { text: row.name }) : row.name),
        idCell(row.id), el('td', {text: projectText(row)}),
        el('td', { class: 'nowrap' }, row.source_power || row.status || '—', row.locked ? el('span', { class: 'small red', text: ' locked' }) : null),
        el('td', null, el('span', { class: row.boot === 'bfv' ? 'pill ok' : 'pill skip', text: row.boot === 'bfv' ? 'BFV' : 'ephemeral' })),
        el('td', { class: 'nowrap', text: vols.length + ' · ' + gb + ' GB' }), ipTd,
        el('td', { text: row.flavor || '' }), el('td', { class: 'nowrap', text: (row.host || '') + ' / ' + (row.az || '') }),
        planCell(S.vm.plan[row.id] || S.vm.plan[row.ident], row), rollbackBtn(row));
      body.appendChild(tr);
    }
    if (shown < S.vm.rows.length) {
      body.appendChild(el('tr', { class: 'dim' }, el('td', { colSpan: 11, class: 'small',
        text: (S.vm.rows.length - shown) + ' instance khác bị ẩn bởi ô tìm "' + $('vm-search').value + '" · xóa ô tìm để hiện lại' })));
    }
    if (!shown && !S.vm.rows.length) body.appendChild(el('tr',null,el('td',{colSpan:12},emptyState('Chọn host hoặc project nguồn rồi tải danh sách.', '#instance','Chọn bộ lọc nguồn'))));
    $('vm-shown').textContent = 'lọc ngay trên trình duyệt, không gọi lại CLI · đang hiện ' + shown + '/' + S.vm.rows.length;
    vmCounts();
  }
  function vmCounts() {
    invalidatePlan('vm');
    const n = S.vm.sel.size;
    const selected = S.vm.rows.filter(r => S.vm.sel.has(r.id));
    $('vm-selection-summary').textContent = 'Đã chọn ' + n + ' VM · ' + selected.reduce((a,r)=>a+(r.volumes || []).length,0) + ' volume · ' + selected.reduce((a,r)=>a+(r.volumes || []).reduce((b,v)=>b+Number(v.size || 0),0),0) + ' GB';
    $('vm-next').setAttribute('aria-disabled', String(!n));
    $('vm-selected').textContent = String(n);
    $('vm-opt-title').textContent = 'Option cho ' + n + ' VM đã chọn';
    lock($('vm-plan'), n === 0 || S.planning || !rowIpsValid() || !liveBoundsValid(), S.planning ? 'Đang plan…' : 'Plan ' + n + ' VM');
    const okN = S.vm.batch ? S.vm.batch.jobs.length : 0;
    $('vm-to-confirm').textContent = S.vm.stale ? 'Cần plan lại' : 'Sang xác nhận (' + okN + ' PLAN-OK)';
    $('vm-to-confirm').disabled = !S.vm.batch || !!S.planning;
  }
  $('vm-search').addEventListener('input', renderVm);
  $('o-ip').addEventListener('change', renderRowIps);
  const validIpv4 = value => !value || /^(\d{1,3}\.){3}\d{1,3}$/.test(value) && value.split('.').every(n => Number(n) <= 255 && String(Number(n)) === n);
  function rowIpsValid() { return $('o-ip').value !== 'row' || [...S.vm.sel].every(id => validIpv4(S.vm.ip[id] || '')); }
  function renderRowIps() {
    const box = clear($('vm-row-ips'));
    show(box, $('o-ip').value === 'row');
    if ($('o-ip').value !== 'row') return;
    const body = el('tbody');
    for (const row of S.vm.rows.filter(r => S.vm.sel.has(r.id))) {
      const input = el('input', {type:'text', class:'mono', value:S.vm.ip[row.id] || '', placeholder:'same', 'aria-label':'IP mới cho ' + (row.name || row.id)});
      const error = el('span', {class:'small red'});
      const validate = () => { const valid = validIpv4(input.value.trim()); input.setAttribute('aria-invalid', String(!valid)); error.textContent = valid ? '' : 'Nhập IPv4 hợp lệ'; input.setCustomValidity?.(error.textContent); };
      input.addEventListener('input', () => { S.vm.ip[row.id] = input.value.trim(); validate(); vmCounts(); previewOptions(); });
      validate();
      body.append(el('tr', null, el('th', {scope:'row', text:row.name || row.id}), el('td', null, input, error)));
    }
    box.append(el('table', null, el('caption', {class:'hint', text:'IP mới cho từng VM đã chọn · để trống = same (giữ IP nguồn)'}), el('thead', null, el('tr', null, el('th', {text:'VM đã chọn'}), el('th', {text:'IP mới'}))), body));
  }
  $('vm-select-shown').addEventListener('click', () => {
    const q = $('vm-search').value.trim().toLowerCase();
    for (const row of S.vm.rows) if (vmMatches(row, q) && vmEligible(row)) S.vm.sel.add(row.id);
    renderVm();
  });
  $('vm-select-none').addEventListener('click', () => { S.vm.sel.clear(); renderVm(); });
  // max-passes 1-50, delta-limit 0-65536 (the runner's own bounds): checked only when the run is live, blank = default
  function defaultMode() {
    const t = String(S.inv?.conf?.MIG_DEFAULT_OPTS || '').split(/\s+/).filter(Boolean);
    let mode = 'cold';
    for (let i = 0; i < t.length - 1; i++) if (t[i] === '--mode' && (t[i + 1] === 'cold' || t[i + 1] === 'live')) mode = t[i + 1];
    return mode;
  }
  function liveBoundsValid() {
    const live = $('o-mode').value === 'live' || (!$('o-mode').value && defaultMode() === 'live');
    let good = true;
    for (const [id, lo, hi] of [['o-max-passes', 1, 50], ['o-delta-limit', 0, 65536]]) {
      const v = $(id).value.trim();
      const bad = live && (!!$(id).validity?.badInput || (v !== '' && !(/^\d+$/.test(v) && Number(v) >= lo && Number(v) <= hi)));
      $(id).setAttribute('aria-invalid', String(bad));
      $(id).setCustomValidity?.(bad ? 'Nhập số nguyên ' + lo + '–' + hi : '');
      $(id).title = bad ? 'Nhập số nguyên ' + lo + '–' + hi : '';
      if (bad) good = false;
    }
    return good;
  }
  for (const id of ['o-max-passes', 'o-delta-limit', 'o-mode']) $(id).addEventListener(id === 'o-mode' ? 'change' : 'input', () => vmCounts());
  function intOrNull(id) {
    const v = $(id).value.trim();
    return v === '' ? null : parseInt(v, 10);
  }
  function vmOptions() {
    const o = {};
    const sv = (k, id) => { const v = $(id).value; if (v) o[k] = v; };
    sv('mode', 'o-mode');
    if ($('o-ip').value === 'same' || $('o-ip').value === 'new') o.ip = $('o-ip').value;
    sv('volume_type', 'o-volume-type');
    sv('az', 'vm-dst-az');
    sv('volume_az', 'o-volume-az');
    sv('dst_host', 'vm-dst-host');
    sv('flavor', 'o-flavor');
    sv('verify', 'o-verify');
    const mp = intOrNull('o-max-passes'), dl = intOrNull('o-delta-limit');
    const live = o.mode === 'live' || (!o.mode && defaultMode() === 'live');
    if (live && mp !== null) o.max_passes = mp;
    if (live && dl !== null) o.delta_limit = dl;
    const sg = $('o-security-group').value.split(',').map((x) => x.trim()).filter(Boolean);
    if (sg.length) o.security_group = sg;
    if ($('o-server-group').value.trim()) o.server_group = $('o-server-group').value.trim();
    if (!$('o-copy-sg').checked) o.no_copy_secgroup = true;
    if (!$('o-copy-srvgrp').checked) o.no_server_group = true;
    for (const [k, id] of [['flavor_fallback', 'o-flavor-fallback'], ['start_source', 'o-start-source'],
      ['keep_source_ports', 'o-keep-ports'], ['volumes_only', 'o-volumes-only']]) if ($(id).checked) o[k] = true;
    return o;
  }
  function setVmOptions(o) {
    o = o || {};
    const sv = (id, v) => { $(id).value = v === undefined || v === null ? '' : String(v); };
    sv('o-mode', o.mode); sv('o-ip', o.ip_addr ? 'row' : o.ip); sv('o-volume-type', o.volume_type); sv('vm-dst-az', o.az);
    dstHosts();
    sv('o-volume-az', o.volume_az); sv('vm-dst-host', o.dst_host); sv('o-flavor', o.flavor); sv('o-verify', o.verify);
    sv('o-max-passes', o.max_passes); sv('o-delta-limit', o.delta_limit);
    sv('o-security-group', (o.security_group || []).join(','));
    sv('o-server-group', o.server_group);
    $('o-copy-sg').checked = !o.no_copy_secgroup;
    $('o-copy-srvgrp').checked = !o.no_server_group;
    $('o-flavor-fallback').checked = !!o.flavor_fallback;
    $('o-start-source').checked = !!o.start_source;
    $('o-keep-ports').checked = !!o.keep_source_ports;
    $('o-volumes-only').checked = !!o.volumes_only;
  }
  // --- 18: the plan as it happens ---------------------------------------------------------------------------------
  // planStream(body, onEvent): POST /api/plan read as server-sent events (EventSource cannot POST), with the headers
  // of api() (T5). onEvent(name, data) for planning, row, batch, end, error. -> {status, data} when the server refused
  // with a JSON error (400, 409, 412, 429...), {status: 200, ended} after the stream (ended: its 'end' came).
  async function planStream(body, onEvent) {
    const init = { method: 'POST', credentials: 'same-origin', cache: 'no-store', body: JSON.stringify(body),
      headers: { 'X-Requested-With': 'migui', 'Content-Type': 'application/json' } };
    let res;
    try {
      res = await fetch('/api/plan', init);
    } catch (e) {
      return { status: 0, data: { error: 'mất kết nối tới migui-web (SSH tunnel còn không?)' } };
    }
    const ctype = (res.headers && res.headers.get('Content-Type')) || '';
    if (res.status !== 200 || !ctype.startsWith('text/event-stream') || !res.body) {
      let data = {};
      try { data = await res.json(); } catch (e) { data = {}; }
      if (res.status === 401) showLogin();
      if (res.status === 412) { flash('Doctor phải xanh trước: chạy lại Doctor.'); location.hash = '#doctor'; }
      return { status: res.status === 200 ? 502 : res.status, data: data || {} };
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '', name = '', data = [], ended = false;
    const feed = (raw) => {
      const l = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
      if (l === '') {
        if (name && data.length) {
          const d = JSON.parse(data.join('\n'));
          if (name === 'end') ended = true;
          onEvent(name, d);
        }
        name = '';
        data = [];
      } else if (l.startsWith('event:')) {
        name = l.slice(6).trim();
      } else if (l.startsWith('data:')) {
        data.push(l.slice(5).replace(/^ /, ''));
      }  // ':' lines are the server's pings
    };
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          feed(buf.slice(0, i));
          buf = buf.slice(i + 1);
        }
      }
    } catch (e) {
      // the stream broke (tunnel, network): ended stays false
    }
    return { status: 200, ended };
  }
  // The "Đang plan" panel under the Plan button: one line per ident (chờ -> đang plan -> PLAN-OK / PLAN-FAIL) and a
  // total line; it stays as it is until the next plan.
  const LOST = 'mất kết nối, plan vẫn chạy trên mig-host; bấm Plan lại sẽ chờ runner rảnh';
  // 20: the lines of one migrate plan run (one VM, or the volumes of one project: its planning event's idents) are a
  // group G = {ident (first of idents), label, kind, lines [{text, level}], checks, preflight, passed, last, open}.
  // The runner sends at most 200 lines per run plus one "… (còn N dòng, xem log plan)": kept, nothing more.
  const LIVE_KEEP = 201;
  function planStage(G) {
    if (G.kind === 'vol') return 'đang plan ' + G.label;
    if (G.passed) return 'xong';
    if (G.preflight) return 'preflight đích · ' + G.checks + ' check';
    return 'đang export VM';
  }
  function planLine(G, d) {
    if (G.lines.length >= LIVE_KEEP) return;
    const text = String(d.text === undefined || d.text === null ? '' : d.text);
    const level = ['OK', 'WARN', 'FAIL', 'INFO'].includes(d.level) ? d.level : null;
    G.lines.push({ text, level });
    if (level) G.checks++;
    const t = text.trim();
    if (t.startsWith('Destination preflight')) G.preflight = true;
    if (t === 'Preflight passed.') G.passed = true;
    if (t) G.last = t;
  }
  // The "log plan live" <details> of a group: open while it plans, closed once its row came (the operator's own
  // toggle is kept across redraws); the scroll box follows the newest line unless the operator scrolled up.
  function planLog(G) {
    const pre = el('pre', { class: 'planlog', text: G.lines.map((l) => l.text).join('\n') });
    const det = el('details', { on: { toggle: (e) => { if (e.currentTarget === G.det) G.open = !!e.currentTarget.open; } } },
      el('summary', { text: 'log plan live · ' + G.lines.length + ' dòng' }), pre);
    det.open = G.open;
    G.det = det;
    G.pre = pre;
    return det;
  }
  function renderPlanPanel(P) {
    const box = $(P.kind + '-plan-panel');
    show(box, true);
    const groups = [...new Set(P.rows.map((r) => r.g).filter(Boolean))];
    for (const G of groups) {
      const p = G.pre;
      if (p) {
        G.scroll = p.scrollTop;
        G.stick = !(p.scrollHeight > 0) || p.scrollTop + (p.clientHeight || 0) >= p.scrollHeight - 8;
      }
      G.pre = null;
    }
    clear(box);
    const noun = P.kind === 'vol' ? 'volume' : 'VM';
    const secs = (Date.now() - P.t0) / 1000;
    const okN = P.rows.filter((r) => r.st === 'ok').length;
    let total;
    let cls = 'hint';
    if (P.msg) { total = P.msg; cls = 'small red'; }
    else if (P.lost) { total = LOST + ' · đã mất ' + mmss(secs); cls = 'small red'; }
    else if (P.done) total = 'plan ' + P.n + ' ' + noun + ' mất ' + mmss(P.secs) + ' · ' + okN + ' OK · ' + (P.n - okN) + ' FAIL';
    else total = 'plan ' + P.cur + '/' + P.n + ' · đã mất ' + mmss(secs);
    box.appendChild(el('h3', { text: P.done || P.lost || P.msg ? 'Plan' : 'Đang plan' }));
    const completed = P.rows.filter(r => r.st === 'ok' || r.st === 'fail').length;
    box.appendChild(el('div', { class: cls, text: total }));
    if (!P.done && completed) box.appendChild(el('div',{class:'hint',text:'Còn khoảng ' + mmss(secs / completed * (P.n - completed)) + ' (ước lượng)'}));
    box.appendChild(el('progress', { max: P.n || 1, value: completed, 'aria-label': 'Tiến độ plan' }));
    const ul = el('ul', { class: 'plain' });
    for (const r of P.rows) {
      const li = el('li', null, el('strong', { text: r.name }), ' · ');
      if (r.st === 'wait') {
        li.appendChild(el('span', { class: 'pill skip', text: 'chờ' }));
      } else if (r.st === 'run') {
        li.append(el('span', { class: 'pill run', text: 'đang plan' }),
          el('span', { class: 'hint', text: ' từ ' + hms(new Date(r.since)) + ' · ' + Math.floor((Date.now() - r.since) / 1000) + ' s' }));
        if (r.g) {
          li.append(el('span', { class: 'hint', text: ' · ' }), el('span', { class: 'hint planstage', text: planStage(r.g) }));
          if (r.g.last) li.appendChild(el('div', { class: 'mono planline', text: r.g.last }));
        }
      } else {
        const row = r.row || {};
        const c = row.counts;
        li.appendChild(el('span', { class: pillFor(row.result), text: row.result || '?' }));
        if (r.st === 'ok') {
          li.appendChild(el('span', { class: 'hint', text: (c ? ' (' + (c.ok || 0) + ' OK · ' + (c.warn || 0) + ' WARN · ' + (c.fail || 0) + ' FAIL)' : '') + (r.secs !== undefined ? ' · ' + r.secs + ' s' : '') }));
        } else {
          li.appendChild(el('span', { class: 'small red', text: ' ' + (String(row.info || '').split('\n')[0] || 'không có lý do') }));
          if (Array.isArray(row.checks) && row.checks.length) {
            li.appendChild(el('details', null, el('summary', { text: row.checks.length + ' check' }), checkTable(row.checks)));
          }
        }
      }
      if (r.g && r.g.ident === r.ident && r.g.lines.length && r.st !== 'wait') li.appendChild(planLog(r.g));
      ul.appendChild(li);
    }
    box.appendChild(ul);
    for (const G of groups) {
      if (G.pre) G.pre.scrollTop = G.stick === false ? (G.scroll || 0) : G.pre.scrollHeight;
    }
  }
  // One plan of the VM or Volume screen through the stream; K = S.vm or S.vol. The table's Plan column follows each
  // row; the batch (or none) comes last and opens "Sang xác nhận" as before.
  async function planRun(kind, items, options) {
    const K = S[kind];
    const requestedFingerprint = selectionFingerprint(kind);
    K.stale = false;
    K.plan = {};
    K.batch = null;
    const names = new Map(K.rows.map((r) => [r.id, r.name || '(không tên)']));
    const P = { kind, t0: Date.now(), n: items.length, cur: 0, done: false, lost: false, msg: '',
      rows: items.map((it) => ({ ident: it.ident, name: names.get(it.ident) || it.ident, st: 'wait' })) };
    K.panel = P;
    const rows = [];
    const redraw = () => (kind === 'vm' ? renderVm() : renderVol());
    renderPlanPanel(P);
    clearInterval(S.planTick);
    S.planTick = setInterval(() => { if (K.panel === P && !P.done && !P.lost && !P.msg) renderPlanPanel(P); }, 1000);
    try {
      const r = await planStream({ kind, items, options }, (ev, d) => {
        if (ev === 'planning') {
          const ids = Array.isArray(d.idents) && d.idents.length ? d.idents : [d.ident];
          const G = { ident: ids[0], label: String(d.name || ''), kind, lines: [], checks: 0, preflight: false,
            passed: false, last: '', open: true };
          for (const x of P.rows) if (ids.includes(x.ident)) { x.st = 'run'; x.since = Date.now(); x.g = G; }
          P.cur = (Number(d.i) || 1) + ids.length - 1;
        } else if (ev === 'line') {
          const x = P.rows.find((y) => y.ident === d.ident && y.g);
          if (x) planLine(x.g, d);
        } else if (ev === 'row') {
          rows.push(d);
          K.plan[d.ident] = d;
          const x = P.rows.find((y) => y.ident === d.ident);
          if (x) {
            x.secs = x.since ? Math.round((Date.now() - x.since) / 1000) : undefined;
            x.st = d.result === 'PLAN-OK' || d.result === 'READY' ? 'ok' : 'fail';
            x.row = d;
            if (x.g) x.g.open = false;
          }
          redraw();
        } else if (ev === 'batch') {
          K.batch = d ? Object.assign({ kind, rows: rows.slice(), at: Date.now() }, d, { uiFingerprint: requestedFingerprint }) : null;
          flash(d ? '' : (kind === 'vm' ? 'Không VM nào PLAN-OK: sửa option rồi plan lại.' : 'Không volume nào sẵn sàng.'), 'info');
        } else if (ev === 'end') {
          P.done = true;
          P.secs = (Date.now() - P.t0) / 1000;
        } else if (ev === 'error') {
          P.msg = 'Plan dừng giữa chừng: ' + (d.error || 'lỗi runner') + ' (HTTP ' + d.status + ')';
        }
        renderPlanPanel(P);
      });
      if (r.status !== 200) {
        P.msg = (r.status === 429 ? 'Runner còn đang plan cho phiên này (429): chờ xong rồi bấm Plan lại · ' :
          'Plan bị từ chối (HTTP ' + r.status + '): ') + err(r);
        if (r.status !== 429) flash('Plan: ' + err(r));
      } else if (!r.ended && !P.msg) {
        P.lost = true;
      }
    } finally {
      if (!P.done || P.lost || P.msg) K.batch = null;
      if (requestedFingerprint !== selectionFingerprint(kind)) { K.batch = null; K.stale = true; }
      clearInterval(S.planTick);
      renderPlanPanel(P);
      redraw();
    }
  }
  // 15 (b): S.planning is set for the whole request; applyLocks() then shows both plan buttons locked.
  async function planning(fn) {
    if (S.planning) return undefined;
    S.planning = true;
    applyLocks();
    try {
      return await fn();
    } finally {
      S.planning = false;
    }
  }
  $('vm-plan').addEventListener('click', () => busy($('vm-plan'), 'Đang plan…', () => planning(async () => {
    if (!liveBoundsValid()) return flash('max-passes 1–50 và delta-limit 0–65536, số nguyên; để trống = mặc định');
    const rowMode = $('o-ip').value === 'row';
    const items = S.vm.rows.filter((r) => S.vm.sel.has(r.id)).map((r) => {
      const it = { ident: r.id };
      if (rowMode && S.vm.ip[r.id]) it.ip = S.vm.ip[r.id];
      return it;
    });
    if (!items.length) return;
    await planRun('vm', items, vmOptions());
  })));
  $('vm-to-confirm').addEventListener('click', () => openConfirm(S.vm.batch, 'options'));

  // --- Volume ----------------------------------------------------------------------------------------------------
  const volScope = () => (document.querySelector('input[name="vol-scope"]:checked') || {}).value || 'available';
  $('vol-load').addEventListener('click', () => busy($('vol-load'), 'Đang tải…', async () => {
    closeMsels();
    const projects = MSEL.volProjects.values();
    if (!projects.length) return flash('Chọn project nguồn.');
    if (projects.length > HOSTS_MAX) return flash('Tối đa ' + HOSTS_MAX + ' project cho một lần tải.');
    const r = await api('POST', '/api/list', { projects, volumes: true, scope: volScope() });
    if (r.status !== 200 || !r.data.data) return flash('Tải danh sách: ' + err(r) + ' ' + ((r.data.stderr || []).slice(-1)[0] || ''));
    flash('');
    $('vol-load-cmd').textContent = (r.data.argv || []).join(' ');
    S.vol.rows = r.data.data.rows || [];
    S.vol.sel.clear();
    S.vol.plan = {};
    S.vol.batch = null; S.vol.stale = false; if (S.confirm?.kind === 'vol') S.confirm = null;
    $('vol-table-title').textContent = 'Volume của ' + summarize(projects, 'project', '') + ' (' + S.vol.rows.length + ')';
    $('vol-table-title').title = 'project: ' + projects.join(', ');
    renderVol();
  }));
  function volMatches(row, q) {
    if (!q) return true;
    const a = row.attached_to || {};
    return [row.name, row.id, projectText(row), a.server_name, a.server_id, row.type].join(' ').toLowerCase().includes(q);
  }
  const volEligible = (row) => !(row.state && row.state.live) && (row.status === 'available' || row.status === 'in-use');
  function renderVol() {
    const q = $('vol-search').value.trim().toLowerCase();
    const body = clear($('vol-rows'));
    let shown = 0;
    for (const row of S.vol.rows) {
      if (!volMatches(row, q)) continue;
      shown++;
      const ok = volEligible(row);
      const sel = S.vol.sel.has(row.id);
      const tr = el('tr', { class: sel ? 'selected' : '' });
      tr.addEventListener('click', (ev) => { if (ev.target.closest?.('button,a,input,summary')) return; const checkbox = tr.querySelector?.('input[type=checkbox]'); if (checkbox && !checkbox.disabled) checkbox.click(); });
      const cb = el('input', { type: 'checkbox', 'aria-label': row.name || row.id, checked: sel, disabled: !ok });
      cb.addEventListener('change', () => {
        if (cb.checked) S.vol.sel.add(row.id); else S.vol.sel.delete(row.id);
        tr.className = cb.checked ? 'selected' : '';
        volCounts();
      });
      const a = row.attached_to;
      let plan = S.vol.plan[row.id];
      if (!plan && row.status === 'in-use' && !(row.state && row.state.live)) plan = { result: 'cần --in-use (1 volume) hoặc migrate VM' };
      tr.append(el('td', null, cb), el('td', null, ok ? el('strong', { text: row.name || '(không tên)' }) : (row.name || '(không tên)')),
        idCell(row.id), el('td', {text: projectText(row)}), el('td', { class: 'nowrap', text: (row.size || 0) + ' GB' }),
        el('td', { text: row.status || '' }),
        el('td', { text: a ? (a.server_name || short(a.server_id)) + ' (' + (a.device || '') + ')' : '—' }),
        el('td', { text: (row.type || '') + ' / ' + (row.az || '') }),
        planCell(plan, row.state && row.state.live ? row : { result: 'selected' }), rollbackBtn(row));
      body.appendChild(tr);
    }
    if (shown < S.vol.rows.length) {
      body.appendChild(el('tr', { class: 'dim' }, el('td', { colSpan: 9, class: 'small',
        text: (S.vol.rows.length - shown) + ' volume khác bị ẩn bởi ô tìm "' + $('vol-search').value + '"' })));
    }
    if (!shown && !S.vol.rows.length) body.appendChild(el('tr',null,el('td',{colSpan:10},emptyState('Chọn project nguồn rồi tải danh sách volume.', '#volume','Chọn project nguồn'))));
    $('vol-shown').textContent = 'đang hiện ' + shown + '/' + S.vol.rows.length;
    volCounts();
  }
  function volCounts() {
    invalidatePlan('vol');
    const n = S.vol.sel.size;
    $('vol-selected').textContent = String(n);
    $('vol-selection-summary').textContent = 'Đã chọn ' + n + ' volume · ' + S.vol.rows.filter(r=>S.vol.sel.has(r.id)).reduce((a,r)=>a+Number(r.size || 0),0) + ' GB';
    lock($('vol-plan'), n === 0 || S.planning, S.planning ? 'Đang plan…' : 'Plan ' + n + ' volume');
    const sel = S.vol.rows.filter((r) => S.vol.sel.has(r.id));
    const one = sel.length === 1 && sel[0].status === 'in-use';
    $('vol-in-use').disabled = !one;
    if (!one) $('vol-in-use').checked = false;
    previewVolume();
    $('vol-to-confirm').textContent = S.vol.stale ? 'Cần plan lại' : 'Sang xác nhận (' + (S.vol.batch ? S.vol.batch.jobs.length : 0) + ' PLAN-OK)';
    $('vol-to-confirm').disabled = !S.vol.batch || !!S.planning;
  }
  $('vol-search').addEventListener('input', renderVol);
  $('vol-select-shown').addEventListener('click', () => {
    const q = $('vol-search').value.trim().toLowerCase();
    for (const row of S.vol.rows) if (volMatches(row, q) && volEligible(row) && row.status === 'available') S.vol.sel.add(row.id);
    renderVol();
  });
  $('vol-select-none').addEventListener('click', () => { S.vol.sel.clear(); renderVol(); });
  function volOptions() {
    const o = {};
    for (const [k, id] of [['az', 'vol-az'], ['volume_az', 'vol-volume-az'], ['volume_type', 'vol-volume-type'], ['verify', 'vol-verify']]) {
      if ($(id).value) o[k] = $(id).value;
    }
    if ($('vol-in-use').checked) o.in_use = true;
    return o;
  }
  function setVolOptions(o) {
    o = o || {};
    for (const [k, id] of [['az', 'vol-az'], ['volume_az', 'vol-volume-az'], ['volume_type', 'vol-volume-type'], ['verify', 'vol-verify']]) {
      $(id).value = o[k] || '';
    }
  }
  $('vol-plan').addEventListener('click', () => busy($('vol-plan'), 'Đang plan…', () => planning(async () => {
    const items = S.vol.rows.filter((r) => S.vol.sel.has(r.id)).map((r) => ({ ident: r.id }));
    if (!items.length) return;
    await planRun('vol', items, volOptions());
  })));
  $('vol-to-confirm').addEventListener('click', () => openConfirm(S.vol.batch, 'volume-options'));

  // --- effective options table (13.3) -----------------------------------------------------------------------------
  function fmtValue(v) {
    if (v === null || v === undefined || v === '') return '—';
    if (v === true) return 'có';
    if (v === false) return 'không';
    if (Array.isArray(v)) {
      return v.length ? v.map((g) => (g && typeof g === 'object' ? (g.src ? g.src + ' → ' : '') + (g.dst || '?') + (g.action ? ' (' + (g.action === 'create' ? 'sẽ tạo' : 'có sẵn') + ')' : '') : String(g))).join(' · ') : '—';
    }
    if (typeof v === 'object') {
      if ('src' in v || 'dst' in v) return (v.src || '—') + ' → ' + (v.dst || '—') + (v.policy ? ' (' + v.policy + ')' : '');
      return JSON.stringify(v);
    }
    return String(v);
  }
  function srcBadge(e) {
    const from = e && SRC_LABEL[e.from] ? e.from : 'default';
    return el('span', { class: 'src src-' + from, title: e && e.detail ? e.detail : '', text: SRC_LABEL[from] + (e && e.detail ? ' · ' + e.detail : '') });
  }
  function effTable(eff, why) {
    if (!eff || typeof eff !== 'object') return el('div', { class: 'hint', text: why || 'không ghi nhận: job dừng trước khi ghi option, hoặc chạy bằng bản < 7.3' });
    const t = el('table', null, el('thead', null, el('tr', null, el('th', { text: 'Option' }), el('th', { text: 'Giá trị' }), el('th', { text: 'Nguồn' }))));
    const tb = el('tbody');
    for (const k of Object.keys(eff).sort()) {
      const e = eff[k] || {};
      tb.appendChild(el('tr', null, el('td', { class: 'mono', text: k }), el('td', { text: fmtValue(e.value) }), el('td', null, srcBadge(e))));
    }
    t.appendChild(tb);
    return t;
  }
  function confText(conf) {
    if (!conf || typeof conf !== 'object') return 'không ghi nhận';
    const keys = ['MIG_DEFAULT_OPTS', 'MIG_AZ', 'MIG_VOLUME_AZ', 'AZ_TYPE_MAP', 'RBD_POOL_MAP', 'RBD_POOL', 'PROJECT_MAP_FILE', 'FLAVOR_MAP_FILE'];
    return (conf.path ? conf.path + (conf.found === false ? ' (không có)' : '') + ' · ' : '') +
      keys.map((k) => k + '="' + (conf[k] || '') + '"').join(' ');
  }

  // --- Xác nhận (14.2) ---------------------------------------------------------------------------------------------
  function openConfirm(batch, back) {
    if (S.planning || !batch || (batch.uiFingerprint && batch.uiFingerprint !== selectionFingerprint(batch.kind))) return;
    S.confirm = Object.assign({ back }, batch);
    if (batch.kind === 'rollback') return openRollbackDialog();
    renderConfirm();
    location.hash = '#confirm';
  }
  function renderConfirm() {
    const b = S.confirm;
    const n = b.jobs.length;
    $('cf-run').className=b.kind === 'rollback'?'btn danger solid':'btn primary';
    renderImpact(b);
    renderWarnings(b);
    const noun = b.kind === 'vol' ? 'volume' : 'VM';
    $('cf-title').textContent = b.kind === 'rollback' ? 'Rollback ' + b.names[0] + ' — xác nhận hai bước' :
      'Xác nhận đợt: ' + n + ' ' + noun + ', chạy từng con theo thứ tự';
    const cmds = clear($('cf-commands'));
    b.commands.forEach((argv, i) => {
      const row = (b.rows || []).find((r) => r.ident === argv[2]) || {};
      // 17: the whole plan of this row (every check, any level), then its effective options, its plan log and the
      // plan command; open by itself when the plan has a WARN or a FAIL
      const has = Array.isArray(row.checks);
      const cn = checkCounts(row.checks);
      const det = el('details', null, el('summary', { text: 'nội dung plan · ' + (has ? row.checks.length + ' check (' + cn.OK +
        ' OK · ' + cn.WARN + ' WARN · ' + cn.FAIL + ' FAIL)' : 'không ghi nhận') + ' · option hiệu lực' }));
      if (cn.WARN || cn.FAIL) det.open = true;
      det.append(checkTable(row.checks), effTable(row.effective || (b.original && b.original.effective),
          row.result === 'READY' ? 'READY: volume rời chưa có option hiệu lực ở bước plan; migrate volume kiểm đầy đủ (preflight) lúc chạy, trước khi tạo gì ở đích' : null),
        el('div', { class: 'hint', text: 'log plan: ' + (row.log || 'không ghi nhận') }),
        ...(Array.isArray(row.argv) ? [el('div', null, el('code', { class: 'mono', text: row.argv.join(' ') }))] : []));
      const proj = row.project || (b.original && b.original.effective && b.original.effective.projects && b.original.effective.projects.value);
      cmds.appendChild(el('div', { class: 'cmd' },
        el('div', { class: 'line' }, el('span', { text: (i + 1) + '.' }), el('code', { class: 'mono', text: argv.join(' ') }),
          el('strong', { text: b.names[i] }), el('span', { class: b.kind === 'rollback' ? 'pill warn' : 'pill ok', text: b.kind === 'rollback' ? 'ROLLBACK' : (row.result || 'PLAN-OK') })),
        proj ? el('div', { class: 'hint', text: 'project ' + (proj.src || '?') + ' → ' + (proj.dst || '?') }) : null, det));
    });
    const ex = clear($('cf-excluded'));
    for (const r of (b.rows || []).filter((r) => r.result !== 'PLAN-OK' && r.result !== 'READY')) {
      ex.appendChild(el('div', { text: '— ' + (r.name || r.ident) + ': ' + r.result + (r.info ? ' (' + String(r.info).split('\n')[0] + ')' : '') + ' — không vào đợt' }));
    }
    $('cf-conf').textContent = confText(b.conf);
    $('cf-technical').textContent = 'conf_digest: ' + (b.conf_digest || 'không ghi nhận') + '\nfingerprint: ' + (b.fingerprint || 'không ghi nhận');
    const what = clear($('cf-what'));
    const items = b.kind === 'rollback' ? rollbackWhat(b) : b.kind === 'vol' ?
      ['mỗi volume: tạo volume đích cùng tên, copy dữ liệu, nguồn không đổi', 'chạy từng cái, cái sau bắt đầu khi cái trước xong'] :
      [b.names[0] + ': export + preflight lại, tạo volume đích → copy (live: pre-sync khi VM chạy) → cutover (stop, tháo port nếu --ip same, lock, tạo VM)',
        n > 1 ? b.names[0] + ' OK → ' + b.names[1] + ' bắt đầu, cùng kịch bản' : null, 'Trạng thái nguồn sau khi xong: ' + batchImpact(b).source_after];
    for (const t of items.filter(Boolean)) what.appendChild(el('li', { text: t }));
    show($('cf-stop-wrap'), b.kind !== 'rollback' && n > 1);
    const byName = b.rule.mode === 'name';
    const rbNoun = String((b.commands[0] || [])[2] || '').startsWith('vol-') ? 'volume' : 'VM';
    $('cf-type-title').textContent = b.kind === 'rollback' ? 'Gõ đúng tên ' + rbNoun : 'Xác nhận một lần cho cả đợt';
    $('cf-type-label').textContent = b.kind === 'rollback' ? 'Gõ đúng tên ' + rbNoun + ': ' + b.names[0] : byName ? (n > 1 ? 'Gõ đúng tên từng ' + noun + ' (mỗi dòng một tên)' : 'Gõ đúng tên ' + noun) : 'Gõ đúng số ' + noun + ' trong đợt';
    $('cf-type-hint').textContent = b.kind === 'rollback' ?
      'Gõ tên ' + b.names[0] + ((b.rule.alt || []).length ? ' (hoặc ID đầy đủ ' + b.rule.alt[0] + ')' : '') +
        '. Sai → 409, token hủy, phải bấm Rollback lại.' : byName ?
      'Đợt 1 ' + noun + ', rollback, keep-source-ports, --ip <addr> gõ tay hoặc --in-use: gõ tên. Sai → 409, token hủy, phải plan lại.' :
      'Server so số đã gõ với số lệnh PLAN-OK và confirm_token của phiên này. Sai → 409, token hủy, phải plan lại.';
    $('cf-typed').value = '';
    $('cf-back').textContent = b.kind === 'rollback' ? 'Quay lại' : 'Quay lại sửa option';
    S.cfBlocked = null;
    clearInterval(S.tick);
    const upd = () => {
      const left = b.expires_in - (Date.now() - b.at) / 1000;
      $('cf-sub').textContent = 'đợt ' + b.id + ' · PLANNED ' + hms(b.planned) + ' · confirm_token hết hạn sau ' + mmss(left);
      cfRunState();
      if (left <= 0) clearInterval(S.tick);
    };
    upd();
    S.tick = setInterval(upd, 1000);
  }
  function cfRunState() {
    const b = S.confirm;
    if (!b) return;
    if (S.rollbackDialog) return rollbackRunState();
    const btn = $('cf-run');
    const left = b.expires_in - (Date.now() - b.at) / 1000;
    const label = b.kind === 'rollback' ? 'Rollback ' + b.names[0] :
      'Chạy đợt ' + b.jobs.length + ' ' + (b.kind === 'vol' ? 'volume' : 'VM');
    if (left <= 0) lock(btn, true, 'Hết hạn: plan lại');
    else if (S.active) lock(btn, true, 'Đang có job khác chạy');
    else if (S.cfBlocked && Date.now() < S.cfBlocked.until) lock(btn, true, S.cfBlocked.text);
    else lock(btn, !confirmationMatches(b, $('cf-typed').value) || !warningsAccepted() || (b.uiFingerprint && b.uiFingerprint !== selectionFingerprint(b.kind)), label);
  }
  function rollbackWhat(b) {
    const eff = (b.original && b.original.effective) || {};
    const ip = eff.ip ? eff.ip.value : null;
    return [b.names[0].startsWith('vol-') || (b.original && b.original.kind === 'vol') ? 'Xóa bản copy volume ở đích; volume nguồn không đổi' :
      'Xóa VM đích' + (b.original && b.original.server_id ? '' : '') + ' + volume đích; giữ SG/server group copy',
    ip === 'same' ? 'Gắn lại port nguồn (job này chạy --ip same, port đã tháo lúc cutover)' :
      ip ? 'Port nguồn vẫn gắn (job này chạy --ip ' + ip + ')' : 'option gốc: không ghi nhận (job dừng trước khi ghi option, hoặc chạy bằng bản < 7.3)',
    'Khôi phục power và port nguồn theo state gốc; kiểm tra lại sau rollback'];
  }
  $('cf-back').addEventListener('click', () => {
    clearInterval(S.tick);
    if (S.rollbackDialog) closeDialog();
    location.hash = '#' + (S.confirm && S.confirm.back ? S.confirm.back : 'instance');
  });
  $('cf-run').addEventListener('click', () => busy($('cf-run'), 'Đang gửi…', async () => {
    const b = S.confirm;
    if (!b || !confirmationMatches(b, $('cf-typed').value) || !warningsAccepted() || (b.uiFingerprint && b.uiFingerprint !== selectionFingerprint(b.kind))) return;
    const typed = $('cf-typed').value;
    const r = await api('POST', '/api/batches/' + b.id + '/run', { typed, confirm_token: b.token,
      continue_on_error: b.kind !== 'rollback' && !$('cf-stop').checked });
    if (r.status === 202) {
      if (S.rollbackDialog) closeDialog();
      clearInterval(S.tick);
      flash('Đợt ' + b.id + ' đã bắt đầu: ' + (r.data.jobs || []).length + ' job.', 'okm');
      S.confirm = null;
      if (b.kind === 'vm') { S.vm.batch = null; vmCounts(); }
      if (b.kind === 'vol') { S.vol.batch = null; volCounts(); }
      S.watch = (r.data.jobs || [])[0] || null; S.watchPinned = false;
      S.lastActiveBatch = {id:b.id,jobs:r.data.jobs || b.jobs};
      location.hash = '#running';
      return;
    }
    if (r.status === 423) {
      // the reason on the button itself, for a few seconds (then cfRunState follows S.active again)
      S.cfBlocked = { until: Date.now() + 5000,
        text: /outside the UI/.test(err(r)) ? 'Bị khóa: migrate đang chạy ngoài UI' : 'Bị khóa: đang có job khác chạy' };
      flash('Đang có job khác chạy (một lúc một job): ' + err(r));
      refreshJobs();
      return;
    }
    flash('Không chạy: ' + err(r));
    if (r.status === 409) {
      clearInterval(S.tick);
      if (b.kind === 'vm') { S.vm.batch = null; vmCounts(); }
      if (b.kind === 'vol') { S.vol.batch = null; volCounts(); }
      S.confirm = null;
      location.hash = '#' + (b.back || 'instance');
    }
  }));
  async function startRollback(ident, btn) {
    if (S.rbBusy) return;  // one prepare at a time, whichever Rollback button was clicked
    S.rbBusy = true;
    try {
      S.dialogOpener = btn;
      await busy(btn, 'Đang chuẩn bị…', async () => {
        const r = await api('POST', '/api/rollback', { ident });
        if (r.status !== 200) return flash('Rollback: ' + err(r));
        flash('');
        openConfirm(Object.assign({ kind: 'rollback', rows: [], original: r.data.original, at: Date.now() }, r.data.batch), S.screen || 'history');
      });
    } finally {
      S.rbBusy = false;
      S.dialogOpener = null;  // consumed by dialog() when it opened; never left for another dialog
    }
  }

  // --- Đang chạy (5.5, 8) -------------------------------------------------------------------------------------------
  async function refreshJobs(background) {
    if (S.jobsRefreshing) return;
    S.jobsRefreshing=true;
    try {
    const r = await api('GET', '/api/jobs', null, true);
    $('overview-connection').textContent = r.status === 200 ? '● Đã kết nối · ' + hms(new Date()) : 'Mất kết nối · dữ liệu có thể đã cũ';
    if (r.status !== 200) return;
    S.jobs = mergeJobs(S.jobs, jobsEnvelope(r.data).jobs);
    const active = r.data.active || null;
    const activeKey = JSON.stringify(active && {id:active.id,state:active.state,jobs:active.jobs,stop:active.stop});
    const activeChanged = S.activeKey !== activeKey;
    S.active = active;
    if (activeChanged) {
      const running=await api('GET','/api/jobs?offset=0&limit=500&q=&state=RUNNING,QUEUED&kind=&from=&to=',null,true);
      if(running.status===200) S.jobs=mergeJobs(S.jobs,jobsEnvelope(running.data).jobs);
    }
    S.activeKey = activeKey;
    const batchToLoad = S.active || S.lastActiveBatch;
    if (batchToLoad && Number.isFinite(r.data.total) && (S.loadedBatch !== batchToLoad.id || !S.active)) {
      const remaining = new Set(batchToLoad.jobs || []);
      for (const j of jobsEnvelope(r.data).jobs) remaining.delete(j.id);
      // Fetch pages sequentially once at batch start/end, not one request per queued VM every poll.
      for (let offset=50; remaining.size && offset<r.data.total; offset+=50) {
        const page=await api('GET','/api/jobs?offset='+offset+'&limit=50&q=&state=&kind=&from=&to=',null,true);
        if(page.status!==200 || !jobsEnvelope(page.data).jobs.length) break;
        const jobs=jobsEnvelope(page.data).jobs;
        S.jobs=mergeJobs(S.jobs,jobs);
        for(const j of jobs)remaining.delete(j.id);
      }
      if(!remaining.size) S.loadedBatch=batchToLoad.id;
    }
    if (!S.active && !S.watchPinned && batchToLoad?.jobs.includes(S.watch)) {
      const last = [...batchToLoad.jobs].reverse().map(id=>S.jobs.find(j=>j.id === id)).find(j=>j?.t_start);
      if (last) S.watch=last.id;
    }
    S.lastActiveBatch=S.active;
    const missing = [S.watch].filter(id=>id && !jobsEnvelope(r.data).jobs.some(j=>j.id === id));
    const records = await Promise.all(missing.map(id=>api('GET','/api/jobs/' + encodeURIComponent(id),null,true)));
    for (const rec of records) if (rec.status === 200) S.jobs = mergeJobs(S.jobs,[rec.data.job || rec.data]);
    if (S.screen === 'overview' && Number.isFinite(r.data.total) && (!S.overviewAt || Date.now()-S.overviewAt>30000)) {
      const today=new Date(); const from=[today.getFullYear(),String(today.getMonth()+1).padStart(2,'0'),String(today.getDate()).padStart(2,'0')].join('-');
      const states=['RUNNING','QUEUED','OK','FAIL','LOST','DETACHED'];
      const totals=await Promise.all(states.map(state=>api('GET','/api/jobs?offset=0&limit=1&q=&state='+state+'&kind=&from='+(state==='OK'?from:'')+'&to=',null,true)));
      if(totals.every(x=>x.status===200 && Number.isFinite(x.data.total))) { S.overviewAt=Date.now(); S.overviewCounts=[totals[0].data.total,totals[1].data.total,totals[2].data.total,totals.slice(3).reduce((n,x)=>n+x.data.total,0)]; }
    }
    S.overviewLoading = false;
    renderOverview();
    const live = S.active ? S.jobs.filter((j) => S.active.jobs.includes(j.id) && (j.state === 'QUEUED' || j.state === 'RUNNING')).length : 0;
    $('queue-count').textContent = String(live);
    if (!$('screen-running').classList.contains('hidden')) renderRunning();
    applyLocks();
    } finally { S.jobsRefreshing=false; S.overviewLoading=false; renderOverview(); }
  }
  function runLocks() {
    const j = currentJob();
    if (!j) return;
    const running = j.state === 'RUNNING';
    show($('run-cancel-panel'), running);
    syncCancel(j);
    const sigint = !!(j.cancel && j.cancel.sigint_at);
    const killed = !!(j.cancel && j.cancel.sigkill_at) || S.killSent === j.id;
    lock($('run-cancel'), !running || sigint);
    if (!running || killed) {
      lock($('run-kill'), true, killed ? 'Đã gửi SIGKILL' : undefined);
      $('run-kill-typed').disabled = true;
    }
    lock($('run-stop-after'), !S.active || !S.active.jobs.includes(j.id) || !!S.active.stop);
  }
  function currentJob() {
    if (S.watchPinned) {
      const pinned = S.jobs.find((j) => j.id === S.watch);
      if (pinned) return pinned;
    }
    if (S.active) {
      const run = S.jobs.find((j) => S.active.jobs.includes(j.id) && j.state === 'RUNNING');
      if (run) return run;
    }
    return S.jobs.find((j) => j.id === S.watch) || null;
  }
  function renderRunning() {
    const j = currentJob();
    show($('run-empty'), !j);
    show($('run-panel'), !!j);
    if (!j) { show($('run-pin-note'), false); return; }
    if (S.esJob !== j.id) openStream(j);
    renderJobHead(j);
    const q = clear($('run-queue'));
    const inActive = !!(S.active && S.active.jobs.includes(j.id));
    const live = S.active ? S.jobs.find((x) => S.active.jobs.includes(x.id) && x.state === 'RUNNING') : null;
    const away = !!(S.watchPinned && live && live.id !== j.id);
    show($('run-pin-note'), away);
    if (away) $('run-pin-text').textContent = 'Đang xem ' + j.name + ' (chọn ở Tổng quan). Job đang chạy: ' + live.name + '.';
    const ids = inActive ? S.active.jobs : S.jobs.filter(x=>x.id === j.id || (j.batch && x.batch === j.batch)).map(x=>x.id).reverse();
    for (const id of ids) {
      const x = S.jobs.find((y) => y.id === id);
      if (!x) { const index=S.active?.jobs.indexOf(id); q.appendChild(el('li',null,el('strong',{text:S.active?.names?.[index] || short(id)}),' — ',el('span',{class:'hint',text:'Chưa tải trạng thái'}))); continue; }
      q.appendChild(el('li', null, el('strong', { text: x.name }), ' — ', el('span', { class: pillFor(x.state), text: STATE_LABEL[x.state] || x.state })));
    }
    $('run-stop-on-fail').checked = inActive ? !S.active.continue_on_error : true;
    lock($('run-stop-after'), !inActive || !!S.active.stop,
      inActive && S.active.stop ? 'Đợt sẽ dừng: ' + (S.active.stop_reason || '') : 'Dừng sau con hiện tại');
    $('run-stop-after').dataset.batch = inActive ? S.active.id : '';  // the batch this screen shows: the only one stop-after acts on
  }
  function renderJobHead(j) {
    $('run-title').textContent = j.name + ' — ' + j.state + (j.phase ? ' · giai đoạn: ' + j.phase : '');
    $('run-sub').textContent = 'job ' + j.id + (j.pid ? ' · pid ' + j.pid : '') + ' · bắt đầu ' + (j.t_start ? hms(j.t_start) : '—') +
      (j.t_end ? ' · xong ' + hms(j.t_end) : '') + ' · operator ' + (j.operator || 'không ghi nhận');
    clear($('run-options')).appendChild(effTable(j.effective));
    clear($('run-plan')).appendChild(checkTable(j.checks));
    $('run-cmd').textContent = (j.argv || []).join(' ');
    $('run-log-path').textContent = j.batch_log ? 'Log đầy đủ: ' + j.batch_log : '';
    $('run-download').href = '/api/jobs/' + j.id + '/log';
    const running = j.state === 'RUNNING';
    if (j.cancel && j.cancel.sigint_at && running) lock($('run-cancel'), true, 'Đã gửi SIGINT ' + hms(j.cancel.sigint_at));
    else if (!S.armed || !running) lock($('run-cancel'), !running, 'Hủy job (SIGINT)');
    if (!running) { lock($('run-kill'), true, S.killSent === j.id || j.cancel?.sigkill_at ? 'Đã gửi SIGKILL' : 'Buộc dừng (SIGKILL)'); $('run-kill').title = ''; $('run-kill-typed').disabled = true; }
    if (!['RUNNING','QUEUED','PLANNED'].includes(j.state)) {
      $('st-res').textContent = j.state;
      $('st-res').className = 'v ' + (j.state === 'OK' ? 'dot-ok' : 'red');
      const exitText = {3:'lỗi trước cutover — nguồn vẫn chạy',4:'lỗi sau cutover — nguồn đang tắt, cần rollback',5:'đợt có job FAIL'}[j.exit_class];
      $('st-res-note').textContent = (exitText || '') + (j.rc != null ? (exitText ? ' · ' : '') + 'exit ' + j.rc : '') + (j.dst_server_id ? ' · server đích ' + short(j.dst_server_id) : '') + (j.cancel?.late ? ' · ' + LATE_CANCEL : '');
    } else {
      $('st-res').textContent = '…';
      $('st-res').className = 'v';
      $('st-res-note').textContent = '';
    }
    show($('run-rollback-wrap'), (j.state === 'FAIL' || j.state === 'CANCELLED') && (j.kind === 'vm' ? !!j.t_stop : j.kind === 'vol'));
    const sourceDown = sourceStillDown(j);
    show($('run-failure'), sourceDown);
    $('run-failure').textContent = sourceDown ? 'Nguồn vẫn tắt từ ' + hms(j.t_stop) + ' — cần Rollback hoặc xử lý trước khi bật nguồn. Xem log để xác định bước lỗi.' : '';
    $('st-pass').textContent = j.phase_detail?.name || j.phase || (running ? 'đang khởi động' : 'không ghi nhận');
    $('st-pass-note').textContent = [j.phase_detail?.volume, j.phase_detail?.pct != null ? j.phase_detail.pct + '%' : null].filter(Boolean).join(' · ');
    $('st-vol').textContent = volumeLabel(j);
    if (j.state === 'OK') { $('run-progress').value=100; for (const id of ['copy','verify','create']) $('segment-'+id).classList.add('active'); }
    if (sourceDown) startClock(j);
    else if (j.downtime) {
      clearInterval(S.clock);
      $('st-down').textContent = dur(j.downtime.total || 0);
      $('st-down-note').textContent = 'copy ' + (j.downtime.copy || 0) + 's · verify ' + (j.downtime.verify || 0) + 's · control-plane ' + (j.downtime.control_plane || 0) + 's';
    } else if (j.t_stop) {
      startClock(j);
    }
  }
  function startClock(j) {
    if (S.clockFor === j.id + j.t_stop && (j.state === 'RUNNING')) return;
    clearInterval(S.clock);
    S.clockFor = j.id + j.t_stop;
    const t0 = Date.parse(j.t_stop);
    const upd = () => {
      const end = j.t_end && !sourceStillDown(j) ? Date.parse(j.t_end) : Date.now();
      $('st-down').textContent = mmss((end - t0) / 1000);
      $('st-down-note').textContent = sourceStillDown(j) ? 'Nguồn vẫn tắt — rollback hoặc xử lý' : 'từ t-stop ' + hms(j.t_stop) + (j.t_end ? '' : ', đang đếm');
    };
    upd();
    if (!j.t_end || sourceStillDown(j)) S.clock = setInterval(upd, 1000);
  }
  function stopStream() {
    if (S.es) S.es.close();
    S.es = null;
    S.esJob = null;
  }
  function openStream(j) {
    stopStream();
    S.esJob = j.id;
    S.watch = j.id; // once its batch is over the Running screen keeps showing the job it followed last
    S.vols = new Set();
    S.volsDone = new Set();
    clear($('run-log'));
    $('run-progress').value = 0;
    S.logLines = [];
    S.armedJobId = null;
    $('run-kill-typed').value = '';
    setConnection('connecting');
    renderLog();
    $('run-progress-label').textContent = 'tiến độ';
    for (const id of ['st-pass', 'st-vol', 'st-down']) $(id).textContent = '—';
    $('st-pass-note').textContent = '';
    $('st-vol-note').textContent = '';
    $('st-down-note').textContent = 'chưa t-stop';
    clearInterval(S.clock);
    S.clockFor = null;
    const es = new EventSource('/api/jobs/' + j.id + '/stream');
    S.es = es;
    // Native EventSource reconnects automatically and supplies Last-Event-ID from SSE id fields.
    es.addEventListener('open', () => { if (S.es === es) setConnection('connected'); });
    es.addEventListener('error', () => { if (S.es === es) setConnection('reconnecting'); });
    es.addEventListener('state', (e) => {
      const rec = JSON.parse(e.data);
      if (!rec) return;
      const i = S.jobs.findIndex((x) => x.id === rec.id);
      if (i >= 0) S.jobs[i] = rec; else S.jobs.unshift(rec);
      if (S.esJob === rec.id) renderJobHead(rec);
    });
    es.addEventListener('line', (e) => {
      const d = JSON.parse(e.data);
      logLine('[' + hms(d.t) + '] ' + d.text);
    });
    es.addEventListener('progress', (e) => onProgress(JSON.parse(e.data)));
    es.addEventListener('end', () => {
      es.close();
      setConnection('complete');
      if (S.es === es) S.es = null;
      renderLog();
      refreshJobs();
    });
  }
  function logLine(text) {
    S.logLines = S.logLines || [];
    S.logLines.push(text);
    if (S.logLines.length > 5000) S.logLines.shift();
    renderLog();
  }

  function onProgress(d) {
    const ev = d.event || {};
    const e = ev.event;
    if (e === 'copy' || e === 'verify' || e === 'blocksync') {
      const pct = Math.max(0, Math.min(100, Number(ev.pct) || 0));
      $('run-progress').value = pct;
      setSegment(e === 'verify' ? 'verify' : 'copy');
      $('run-progress-label').textContent = e + (ev.volume ? ' ' + short(ev.volume) : '') + (ev.pass ? ' pass ' + ev.pass : '') + ': ' + pct + '%' +
        (ev.mbps ? ' · ' + ev.mbps + ' MB/s' : '') + (ev.changed_mib !== undefined ? ' · đổi ' + ev.changed_mib + ' MiB' : '');
      if (ev.volume) { S.vols.add(ev.volume); if (pct >= 100 && e !== 'blocksync') S.volsDone.add(ev.volume); }
      $('st-vol').textContent = volumeLabel(currentJob());
      $('st-vol-note').textContent = Array.from(S.vols).map(short).join(', ');
    } else if (e === 'pass') {
      if (ev.volume) { S.vols.add(ev.volume); if (ev.n === 'final') S.volsDone.add(ev.volume); }
      $('st-vol').textContent = volumeLabel(currentJob());
      $('st-pass').textContent = ev.n + ' pass';
      $('st-pass-note').textContent = (ev.mb !== undefined ? 'delta ' + ev.mb + ' MB' : '') + (ev.secs ? ' · ' + ev.secs + ' s' : '');
    } else if (e === 'phase') {
      setSegment(ev.name);
      const j = S.jobs.find((x) => x.id === S.esJob);
      if (j) { j.phase = ev.name; $('run-title').textContent = j.name + ' — ' + j.state + ' · giai đoạn: ' + ev.name; }
    } else if (e === 't_stop') {
      const j = S.jobs.find((x) => x.id === S.esJob);
      if (j) { j.t_stop = ev.at; startClock(j); }
    } else if (e === 'downtime') {
      clearInterval(S.clock);
      $('st-down').textContent = dur(ev.total || 0);
      $('st-down-note').textContent = 'copy ' + (ev.copy || 0) + 's · verify ' + (ev.verify || 0) + 's · control-plane ' + (ev.control_plane || 0) + 's';
    }
    logLine('[' + hms(d.t) + '] @@ ' + JSON.stringify(ev));
  }
  $('run-cancel').addEventListener('click', () => {
    const j = currentJob();
    if (!j || j.state !== 'RUNNING' || j.cancel?.sigint_at) return;
    S.armedJobId = j.id;
    dialog('Dừng ' + j.name + ' (SIGINT)', el('p',{text:'Gửi SIGINT tới job ' + j.id + '. Tool tháo clone và giữ state để phục hồi. Nếu đã cutover, nguồn vẫn có thể đang tắt; cần kiểm tra trước khi rollback.'}), async()=>{
      const cur = currentJob();
      if (!cur || cur.id !== S.armedJobId || cur.id !== j.id || cur.state !== 'RUNNING') { flash('Job đã thay đổi; chưa gửi tín hiệu.'); return; }
      await busy($('run-cancel'),'Đang gửi SIGINT…',()=>sendSigint(cur));
      S.armedJobId = null;
    },'Gửi SIGINT');
  });
  async function sendSigint(j) {
    const r = await api('POST', '/api/jobs/' + j.id + '/cancel', { force: false });
    if (r.status !== 200) return flash('Hủy: ' + err(r));
    flash('Đã gửi SIGINT: migrate tự dọn (tháo clone, giữ state). Sau khi dừng có thể Rollback.', 'info');
    j.cancel = Object.assign({}, j.cancel, r.data.cancel || {});
    if (r.data.force_in != null && j.cancel.sigint_at) { S.cancelDurations ||= {}; S.cancelDurations[j.id] = Math.max(0,Number(r.data.force_in) + (Date.now()-Date.parse(j.cancel.sigint_at))/1000); }
    syncCancel(j);
    clearInterval(S.killTimer);
    S.killTimer = setInterval(() => { const cur = currentJob(); if (cur) syncCancel(cur); }, 1000);
  }

  $('run-kill').addEventListener('click', () => {
    const j = currentJob(), typed=$('run-kill-typed').value;
    if (!j || j.state !== 'RUNNING' || cancelRemaining(j) !== 0 || typed.trim() !== j.name) return;
    dialog('Buộc dừng ' + j.name + ' (SIGKILL)',el('p',{text:'SIGKILL chấm dứt ngay tiến trình, không cho tool tự tháo clone. Job ' + j.id + ' có thể để lại attachment và tài nguyên đích; phải kiểm tra log và state trước khi phục hồi.'}),()=>busy($('run-kill'),'Đang gửi SIGKILL…',async()=>{
      const cur=currentJob();
      if(!cur || cur.id!==j.id || cur.state!=='RUNNING' || cancelRemaining(cur)!==0)return;
      const r=await api('POST','/api/jobs/'+j.id+'/cancel',{force:true,typed});
      $('run-kill-typed').value='';
      if(r.status!==200)return flash('SIGKILL: '+err(r));
      S.killSent=j.id;
      flash('Đã gửi SIGKILL (ghi audit). Kiểm tra state và log batch trước khi rollback.','info');
    }),'Gửi SIGKILL');
  });
  // the nav link Hàng đợi shows what runs now: it releases a job followed from Tổng quan, at once when Chạy is already open
  function releasePin() { S.watchPinned = false; if (S.screen === 'running') { renderRunning(); runLocks(); } }
  $('nav-running').addEventListener('click', releasePin);
  $('run-pin-back').addEventListener('click', () => {
    S.watchPinned = false; renderRunning(); runLocks();  // the button lives on Chạy: always redraw
    const title = $('run-title'); title.setAttribute('tabindex', '-1'); title.focus?.();  // the button just hid itself
  });
  $('run-stop-after').addEventListener('click', () => busy($('run-stop-after'), 'Đang gửi…', async () => {
    // the batch rendered on this screen, not whatever S.active became during a refresh that has not rendered yet
    const shown = $('run-stop-after').dataset.batch;
    if (!shown || !S.active || S.active.id !== shown) {
      flash('Đợt đang chạy vừa đổi; màn Chạy đã cập nhật. Kiểm tra rồi bấm lại.', 'info');
      renderRunning(); runLocks();
      return;
    }
    const r = await api('POST', '/api/batches/' + shown + '/stop', {});
    if (r.status !== 200) return flash('Dừng đợt: ' + err(r));
    if (S.active?.id === shown) S.active.stop = true;  // stays locked (runLocks) until the batch is over; not another batch's
    await refreshJobs();
  }));
  $('run-rollback').addEventListener('click', () => {
    const j = currentJob();
    if (j) startRollback(j.kind === 'vol' ? 'vol-' + j.ident : j.ident, $('run-rollback'));
  });

  // --- Lịch sử (13.3, 14.5) ----------------------------------------------------------------------------------------
  async function refreshHistory() {
    const ticket = S.historyTicket = (S.historyTicket || 0) + 1;
    const r = await api('POST', '/api/status', {});
    if (r.status !== 200) return flash('Lịch sử: ' + err(r));
    S.cli = r.data.cli || {};
    const page = await api('GET', '/api/jobs?' + historyQuery(), null, false);
    if (ticket !== S.historyTicket) return;
    if (page.status === 401) return;
    if (page.status !== 200) return flash('Lịch sử: ' + err(page));
    const data = jobsEnvelope(page.data);
    S.historyTotal = data.total;
    S.histJobs = data.jobs;
    S.jobs = mergeJobs(S.jobs, S.histJobs);
    S.active = data.active;
    $('hist-csv').href = '/api/jobs.csv?' + historyQuery();
    $('hist-csv').textContent = 'Xuất CSV';
    $('hist-page').textContent = (S.historyTotal ? (S.historyOffset || 0) + 1 : 0) + '–' + Math.min((S.historyOffset || 0) + 50, S.historyTotal) + ' / ' + S.historyTotal + ' job';
    lock($('hist-prev'), !(S.historyOffset > 0));
    lock($('hist-next'), (S.historyOffset || 0) + 50 >= S.historyTotal);
    renderHistory();
  }
  const liveIdents = () => new Set(((S.cli && S.cli.live) || []).map((e) => e.ident));
  function stateIdent(j) {
    return j.kind === 'vol' ? 'vol-' + j.ident : j.kind === 'rollback' ? null : j.ident;
  }
  function shortCmd(argv) {
    const a = (argv || []).slice();
    if (a.length < 3) return a.join(' ');
    return [a[0], a[1]].concat(a.slice(3).filter((x) => x !== '--yes' && x !== '--progress-json')).join(' ');
  }
  function renderHistory() {
    const body = clear($('hist-rows'));
    const live = liveIdents();
    const liveOnly = $('hist-live-only').checked;
    const rolled = new Set(S.jobs.filter((j) => j.kind === 'rollback' && j.state === 'OK').map((j) => j.ident));
    const owned = new Set();
    let lastBatch = null;
    for (const j of S.histJobs || S.jobs) {
      const batch = j.batch_id || j.batch || 'Ngoài đợt';
      if (batch !== lastBatch) { body.appendChild(el('tr', {class:'batch-heading'}, el('td', {colSpan:9, text:'Đợt ' + batch}))); lastBatch = batch; }
      const sid = stateIdent(j);
      if (sid) owned.add(sid);
      if (liveOnly && !(sid && live.has(sid))) continue;
      const proj = j.projects || eff(j,'projects',{}) || {};
      let res = STATE_LABEL[j.state] || j.state;
      if (j.state === 'OK' && sid && rolled.has(sid) && !live.has(sid)) res = 'OK → đã rollback';
      const down = j.downtime ? dur(j.downtime.total || 0) : j.kind === 'vm' && j.state === 'FAIL' && !j.t_stop ? '0s (chưa stop)' : '—';
      const open = S.histOpen.has(j.id);
      const toggle = el('button', { type: 'button', class: 'btn link', 'data-job': j.id, 'aria-expanded': String(open), text: open ? 'chi tiết ▴' : 'chi tiết ▾' });
      toggle.addEventListener('click', () => { if (open) S.histOpen.delete(j.id); else S.histOpen.add(j.id); renderHistory(); });
      body.appendChild(el('tr', { class: open ? 'selected' : '' }, el('td', { class: 'nowrap', text: fmtTime(j.t_start || j.t_plan) }),
        el('td', null, el('strong', { text: j.name })), el('td', { class: 'mono', text: shortCmd(j.argv) }),
        el('td', { class: 'nowrap', text: (proj.src || '?') + ' → ' + (proj.dst || '?') }),
        el('td', null, el('span', { class: pillFor(j.state), text: res })), el('td', { text: down }),
        el('td', {text: j.phase_detail?.name || j.phase || ((S.cli?.live || []).find(e => e.ident === sid)?.phase) || '—'}),
        el('td', { title: j.session ? 'phiên ' + j.session : null, text: j.operator || 'không ghi nhận' }), el('td', null, toggle)));
      if (open) body.appendChild(el('tr', null, el('td', { colSpan: 9 }, histDetail(j, sid, live))));
    }
    for (const e of (S.cli && S.cli.live) || []) {
      if (owned.has(e.ident) || (S.historyOffset || 0)>0 || $('hist-state').value) continue;
      if ($('hist-search').value && !String(e.ident).toLowerCase().includes($('hist-search').value.trim().toLowerCase())) continue;
      if ($('hist-kind').value && $('hist-kind').value !== (String(e.ident).startsWith('vol-')?'vol':'vm')) continue;
      const day=String(e.options?.started || e.mtime || '').slice(0,10);
      if (($('hist-from').value && day<$('hist-from').value) || ($('hist-to').value && day>$('hist-to').value)) continue;
      const o = e.options || null;
      body.appendChild(el('tr', null, el('td', { class: 'nowrap', text: fmtTime(o && o.started || e.mtime) }),
        el('td', null, el('strong', { text: e.ident }), el('span', { class: 'hint', text: ' (chạy ngoài UI' + (o ? ', bản ' + o.tool_version : '') + ')' })),
        el('td', { class: 'mono', text: o ? shortCmd(o.command) : 'không ghi nhận' }), el('td', { text: '—' }),
        el('td', null, el('span', { class: 'pill skip', text: migrationState(e) })), el('td', { text: e.t_stop ? 't-stop ' + fmtTime(e.t_stop) : '—' }),
        el('td', {text:e.phase || '—'}), el('td', { text: o ? '' : 'không ghi nhận' }),
        el('td', null, el('button', { type: 'button', class: 'btn danger small', text: 'Rollback…', on: { click: (ev) => startRollback(e.ident, ev.currentTarget) } }))));
    }
  }
  function histDetail(j, sid, live) {
    const wrap = el('div', { class: 'row' });
    const left = el('div', { class: 'card grow3' }, el('h3', { text: 'Option hiệu lực (' + (j.kind === 'rollback' ? 'của job gốc' : 'plan --json / options.json') + ')' }),
      effTable(j.effective), el('h3', { text: 'Plan lúc đó' }), checkTable(j.checks),
      el('div', { class: 'conf' }, el('div', { class: 'lbl', text: 'tool / conf (snapshot lúc plan)' }),
        el('div', { class: 'mono', text: confText(j.conf) })));
    const res = [];
    res.push('state ' + j.state + (j.rc !== null && j.rc !== undefined ? ' · exit ' + j.rc : ''));
    res.push('operator ' + (j.operator || 'không ghi nhận (job chạy trước khi có tài khoản)') + (j.session ? ' · phiên ' + j.session : ''));
    if (j.downtime) res.push('downtime breakdown: total ' + j.downtime.total + 's, copy ' + (j.downtime.copy ?? '—') + 's, verify ' + (j.downtime.verify ?? '—') + 's, control-plane ' + (j.downtime.control_plane ?? '—') + 's');
    if (j.dst_server_id) res.push('dst server ' + j.dst_server_id);
    if (j.cancel && j.cancel.sigint_at) res.push('hủy: SIGINT ' + j.cancel.sigint_at + (j.cancel.sigkill_at ? ', SIGKILL ' + j.cancel.sigkill_at : '') + (j.cancel.late ? ' — ' + LATE_CANCEL + ' (migrate thoát ' + j.cancel.exit + ')' : ''));
    if (j.state_dir) res.push('State: ' + j.state_dir + (sid && live.has(sid) ? ' (sống)' : ''));
    if (j.batch_log) res.push('log batch: ' + j.batch_log);
    const right = el('div', { class: 'card side' }, el('h3', { text: 'Kết quả' }), el('pre', { class: 'mono', text: res.join('\n') }));
    const acts = el('div', { class: 'inline' });
    if (j.kind === 'vm' || j.kind === 'vol') {
      acts.appendChild(el('button', { type: 'button', class: 'btn small', text: 'Chạy lại với cùng option', on: { click: () => rerun(j) } }));
    }
    acts.appendChild(el('button', { type: 'button', class: 'btn small', text: S.cmpA && S.cmpA !== j.id ? 'So với job đã chọn' : 'So với job khác', on: { click: () => compare(j) } }));
    if (sid && live.has(sid)) {
      acts.appendChild(el('button', { type: 'button', class: 'btn danger small', text: 'Rollback ' + j.name + '…', on: { click: (ev) => startRollback(sid, ev.currentTarget) } }));
    }
    acts.appendChild(el('a', { class: 'btn small', href: '/api/jobs/' + j.id + '/log', text: 'Tải log' }));
    right.appendChild(acts);
    wrap.append(left, right);
    return wrap;
  }
  function rerun(j) {
    const o = j.request_options || {};
    if (j.kind === 'vm') {
      setVmOptions(o);
      if (o.ip_addr) S.vm.ip[j.ident] = o.ip_addr;
      S.vm.batch = null;
      if (S.vm.rows.some((r) => r.id === j.ident)) { S.vm.sel = new Set([j.ident]); renderVm(); flash('Đã điền option của job ' + j.id + ', đã tick ' + j.name + ': Plan lại rồi xác nhận.', 'info'); }
      else flash('Đã điền option của job ' + j.id + '. Tải danh sách nguồn rồi tick ' + j.name + ', Plan lại rồi xác nhận.', 'info');
      location.hash = '#instance';
    } else {
      setVolOptions(o);
      S.vol.batch = null;
      if (S.vol.rows.some((r) => r.id === j.ident)) { S.vol.sel = new Set([j.ident]); renderVol(); }
      flash('Đã điền option của job ' + j.id + ' (' + j.name + '): Plan lại rồi xác nhận.', 'info');
      location.hash = '#volume';
    }
  }
  function compare(j) {
    if (!S.cmpA || S.cmpA === j.id) {
      S.cmpA = j.id; S.compareRecord = j;
      flash('Đã chọn ' + j.name + ' (' + fmtTime(j.t_start) + '): mở chi tiết job khác và bấm "So với job đã chọn".', 'info');
      renderHistory();
      return;
    }
    const a = S.compareRecord || S.jobs.find((x) => x.id === S.cmpA), b = j;
    if (!a) return flash('Job so sánh không còn trong trang hiện tại. Chọn lại.');
    S.cmpA = null;
    flash('');
    show($('hist-diff-card'), true);
    $('hist-diff-title').textContent = 'So sánh option: ' + a.name + ' (' + fmtTime(a.t_start) + ') ↔ ' + b.name + ' (' + fmtTime(b.t_start) + ')';
    $('hist-diff-a').textContent = a.name;
    $('hist-diff-b').textContent = b.name;
    const body = clear($('hist-diff-rows'));
    const ea = a.effective || {}, eb = b.effective || {};
    const keys = Array.from(new Set(Object.keys(ea).concat(Object.keys(eb)))).sort();
    const cell = (e) => el('td', null, e ? fmtValue(e.value) + ' ' : 'không ghi nhận', e ? srcBadge(e) : null);
    for (const k of keys) {
      const same = JSON.stringify(ea[k] || null) === JSON.stringify(eb[k] || null);
      body.appendChild(el('tr', { class: same ? '' : 'diff' }, el('td', { class: 'mono', text: k }), cell(ea[k]), cell(eb[k])));
    }
    const ca = a.conf || {}, cb = b.conf || {};
    for (const k of ['MIG_DEFAULT_OPTS', 'AZ_TYPE_MAP', 'RBD_POOL_MAP', 'MIG_AZ']) {
      const same = (ca[k] || '') === (cb[k] || '');
      body.appendChild(el('tr', { class: same ? '' : 'diff' }, el('td', { class: 'mono', text: 'conf ' + k }), el('td', { text: ca[k] || '—' }), el('td', { text: cb[k] || '—' })));
    }
    body.appendChild(el('tr', { class: a.runner_version === b.runner_version ? '' : 'diff' }, el('td', { class: 'mono', text: 'tool' }),
      el('td', { text: a.runner_version || '—' }), el('td', { text: b.runner_version || '—' })));
    $('hist-diff-card').scrollIntoView();
  }
  $('hist-refresh').addEventListener('click', () => busy($('hist-refresh'), 'Đang tải…', refreshHistory));
  $('hist-live-only').addEventListener('change', renderHistory);


  // --- Operator workflow: local guards supplement, never replace, server authorization. -----------------------
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
    return value;
  }
  function selectionFingerprint(kind) {
    const k = S[kind];
    if (!k) return '';
    const items = [...k.sel].sort().map(ident => ({ident, ...(kind === 'vm' && $('o-ip').value === 'row' && k.ip[ident] ? {ip:k.ip[ident]} : {})}));
    return JSON.stringify(canonical({items, options:kind === 'vm' ? vmOptions() : volOptions()}));
  }
  function invalidatePlan(kind) {
    const k = S[kind];
    if (k.batch?.uiFingerprint && k.batch.uiFingerprint !== selectionFingerprint(kind)) {
      k.batch = null; k.plan = {}; k.stale = true;
      if (S.confirm?.kind === kind) S.confirm = null;
    }
  }
  function confirmationMatches(b, text) {
    if (!b?.rule) return false;
    const expected = b.rule.expect || (b.rule.mode === 'count' ? [String(b.jobs.length)] : b.names);
    const values = text.replace(/\r\n/g, '\n').split('\n').filter(x=>x.trim()).map(x=>x.replace(/^\r+|\r+$/g,''));
    if (b.rule.mode === 'count') return values.length === 1 && values[0].trim() === String(expected[0]);
    if (b.rule.alt && values.length === b.rule.alt.length && values.every((v,i)=>v === b.rule.alt[i])) return true;
    if (values.length !== expected.length) return false;
    const free = new Set(expected.map((_,i)=>i)), rest = [];
    for (const value of values) {
      const i = [...free].find(i=>b.rule.ids?.[i] === value);
      if (i === undefined) rest.push(value); else free.delete(i);
    }
    for (const value of rest) {
      const i = [...free].find(i=>expected[i] === value);
      if (i === undefined) return false;
      free.delete(i);
    }
    return !free.size;
  }
  const eff = (row,k,fallback) => row?.effective?.[k]?.value ?? fallback;
  function batchImpact(b) {
    const allowed = new Set((b.commands || []).map(a=>a[2]));
    const rows = (b.rows || []).filter(r=>allowed.has(r.ident));
    const total = {...(b.impact || {}), servers:0, modes:new Set(), verifies:new Set(), after:new Set()};
    for (const r of rows) {
      total.servers += Number(r.impact?.creates?.server || 0);
      total.modes.add(eff(r,'mode','không ghi nhận'));
      total.verifies.add(eff(r,'verify','không ghi nhận'));
      if (r.impact?.source_after) total.after.add(r.impact.source_after);
    }
    return {...total, mode:[...total.modes].join(', ') || 'chưa ghi nhận', verify:[...total.verifies].join(', ') || 'chưa ghi nhận', source_after:[...total.after].join('; ') || 'chưa ghi nhận'};
  }
  function renderImpact(b) {
    const box = clear($('cf-impact'));
    const i = batchImpact(b);
    box.append(el('div',{class:'impact-grid'}, [['VM nguồn sẽ dừng',i.stop_source],['Port nguồn sẽ tháo',i.detach_ports],['Server đích sẽ tạo',i.servers],['Volume đích · GB',(i.volumes ?? '—') + ' · ' + (i.gb ?? '—')]].map(([label,value])=>el('div',null,el('strong',{text:value ?? '—'}),el('span',{text:label})))),
      el('p',{text:'Mode: ' + i.mode + ' · Verify: ' + i.verify + ' · Nguồn sau khi xong: ' + i.source_after}),
      el('p',{class:'hint',text:(b.rows || []).some(r => r.result === 'READY') ? 'Tác động lấy từ plan. Volume READY chưa qua preflight: migrate volume kiểm đầy đủ lúc chạy, trước khi tạo gì ở đích.' : 'Tác động lấy từ plan đã xác minh (preflight từng VM).'}));
    const recent = S.jobs.filter(j=>j.state === 'OK' && j.kind === b.kind).sort((a,b)=>Date.parse(b.t_end)-Date.parse(a.t_end)).slice(0,10);
    const speeds = recent.map(j=>Number(j.progress?.copy?.mbps)).filter(n=>Number.isFinite(n) && n>0);
    const timed = recent.filter(j=>j.t_start && j.t_end && Date.parse(j.t_end)>=Date.parse(j.t_start));
    const bySpeed = speeds.length && Number.isFinite(i.gb);
    const seconds = bySpeed ? i.gb*1024/(speeds.reduce((a,n)=>a+n,0)/speeds.length) : timed.length ? timed.reduce((a,j)=>a+(Date.parse(j.t_end)-Date.parse(j.t_start))/1000,0)/timed.length*b.jobs.length : null;
    box.append(el('p',{class:'hint',text:seconds != null ? 'Thời gian ước lượng: ' + Math.max(1,Math.ceil(seconds/60)) + ' phút · ' + (bySpeed ? 'theo MB/s copy của ' + speeds.length : 'theo thời lượng trung bình của ' + timed.length) + ' job OK gần nhất.' : 'Chưa đủ số đo gần đây để ước lượng thời gian.'}));
  }
  function renderWarnings(b) {
    S.warningBoxes = [];
    const box = clear($('cf-warnings'));
    const byText = new Map();
    for (const r of (b.rows || []).filter(r=>(b.commands || []).some(a=>a[2] === r.ident))) {
      for (const c of (r.checks || []).filter(c=>c.level === 'WARN')) { if (!byText.has(c.text)) byText.set(c.text, new Map()); byText.get(c.text).set(r.ident, r.name || r.ident); }
    }
    const warnings = [...byText].map(([text, rows]) => rows.size + (b.kind === 'vol' ? ' volume' : ' VM') + ' (' + [...rows.values()].slice(0,3).join(', ') + (rows.size>3 ? ', …' : '') + '): ' + text);
    for (const text of warnings) {
      const cb = el('input',{type:'checkbox',on:{change:cfRunState}});
      S.warningBoxes.push(cb); box.append(el('label',{class:'warning-ack'},cb,el('span',{text})));
    }
  }
  function warningsAccepted() { return (S.warningBoxes || []).every(cb=>cb.checked); }
  function projectText(r) {
    const p = r.project || r.projects || eff(r,'projects',{});
    if (typeof p === 'string') return p + ' → ' + (S.inv?.conf?.PROJECT_MAP_FILE ? 'theo map' : p);
    return (p.src || p.name || r.project_name || r.project_id || '—') + ' → ' + (p.dst || (S.inv?.conf?.PROJECT_MAP_FILE ? 'theo map' : p.src || p.name || r.project_name || '?'));
  }
  function migrationState(state) {
    if (state.rolled_back) return 'Đã rollback';
    const phase = {preparing:'Cần xử lý · nguồn chưa dừng',cutover:'Cần xử lý · nguồn đã tắt',migrated:'Đã cutover'}[state.phase];
    if (phase) return phase;
    if (state.phase === 'migrated' || state.dst_server_id) return 'Đã cutover';
    if (state.running || state.state === 'RUNNING') return 'Đang chạy';
    if (state.phase === 'cutover' || state.t_stop) return 'Cần xử lý · nguồn đã tắt';
    return state.phase === 'preparing' ? 'Cần xử lý · nguồn chưa dừng' : 'State sống · cần kiểm tra giai đoạn';
  }
  function icon(name) {
    if (!document.createElementNS) return el('span',{'aria-hidden':'true',text:'?'});
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('class','icon'); svg.setAttribute('aria-hidden','true');
    const use = document.createElementNS('http://www.w3.org/2000/svg','use'); use.setAttribute('href','#i-' + name); svg.append(use); return svg;
  }
  function idCell(id) {
    return el('td',{class:'mono',title:id},el('span',{text:short(id)}),el('button',{type:'button',class:'btn link','aria-label':'Sao chép ID ' + id,on:{click:async()=>{
      try { await navigator.clipboard.writeText(id); flash('Đã sao chép ID.', 'okm'); } catch (_) { flash('Không thể sao chép tự động. ID: ' + id); }
    }}},icon('copy')));
  }
  function doctorGroup(text) {
    if (/python|brick|librbd/i.test(text)) return 'Python';
    if (/source/i.test(text)) return 'Cloud nguồn';
    if (/destination/i.test(text)) return 'Cloud đích';
    if (/directory|conf /i.test(text)) return 'Thư mục & cấu hình';
    if (/pool|daemon|canary|write.check/i.test(text)) return 'Lưu trữ';
    return 'Chương trình';
  }
  function jobsEnvelope(data) { return {jobs:data.jobs || [],total:data.total,active:data.active || null}; }
  function mergeJobs(old, jobs) { const map = new Map(old.map(j=>[j.id,j])); for(const j of jobs) map.set(j.id,j); return [...map.values()]; }
  function renderOverview() {
    const when = (j) => Date.parse(j.t_start || j.t_plan || '') || 0;
    const jobs = [...S.jobs].sort((a, b) => when(b) - when(a));
    clear($('overview-stats')).append(...['ĐANG CHẠY','ĐANG CHỜ','OK HÔM NAY','CẦN XỬ LÝ'].map((label,i)=>el('div',{class:'stat' + (S.overviewLoading ? ' skeleton' : ''), 'aria-busy':String(!!S.overviewLoading)},el('div',{class:'k',text:label}),el('div',{class:'v',text:S.overviewLoading ? 'Đang tải…' : S.overviewCounts?.[i] ?? 'Không tải được'}))));
    $('overview-doctor').textContent = (S.doctorOk ? 'Doctor xanh' : 'Doctor chưa chạy trong phiên này') + ' · ' + (S.doctorAt ? fmtTime(S.doctorAt) : 'chạy để mở Chọn');
    const box = clear($('overview-jobs'));
    if (S.overviewLoading) { box.append(el('p',{class:'skeleton',text:'Đang tải hoạt động…'})); return; }
    if (!jobs.length) box.append(emptyState('Chưa có job nào. Chạy Doctor rồi chọn VM để bắt đầu.', '#doctor','Mở Doctor'));
    for (const j of jobs.slice(0,5)) box.append(el('div',{class:'bar recent'},el('strong',{text:j.name}),el('span',{class:'hint',text:fmtTime(j.t_start || j.t_plan)}),el('span',{class:pillFor(j.state),text:j.state}),el('a',{class:'push',href:'#running','data-job':j.id,text:'Theo dõi →',on:{click:()=>{S.watch=j.id; S.watchPinned=true;}}})));
  }
  function emptyState(text,href,label) { return el('div',{class:'empty-state'},el('p',{text}),el('a',{href,class:'btn',text:label})); }
  for (const id of ['doctor','instance','options','confirm','running']) $('step-' + id).addEventListener('click', (ev) => {
    if ($('step-' + id).getAttribute('aria-disabled') === 'true') { ev.preventDefault(); flash(id === 'options' ? 'Chọn ít nhất một VM hoặc volume trước.' : 'Chưa có đợt nào để xác nhận: plan trước.', 'info'); }
  });
  function updateStepper(name) {
    const campaign = ['instance','options','volume','volume-options','confirm','running'].includes(name) || name === 'doctor' && S.campaignDoctor;
    show($('stepper'), campaign);
    show($('step-doctor').parentElement || $('step-doctor'), !!S.campaignDoctor);
    const labels = {doctor:'Doctor',instance:'Chọn',options:'Tùy chọn',confirm:'Xác nhận',running:'Chạy'};
    let number = S.campaignDoctor ? 1 : 0;
    for (const id of ['doctor','instance','options','confirm','running']) clear($('step-'+id)).append(el('span',{text:number++}),labels[id]);
    const step = name === 'volume' ? 'instance' : name === 'volume-options' ? 'options' : name;
    for (const id of ['doctor','instance','options','confirm','running']) {
      const a = $('step-' + id); a.classList.toggle('active',id === step); a.setAttribute('aria-current',id === step ? 'step' : 'false');
      a.setAttribute('aria-disabled',String((id === 'confirm' && !S.confirm) || (id === 'options' && !(S.resourceKind === 'vol' ? S.vol.sel.size : S.vm.sel.size))));
    }
  }
  function previewVolume() {
    const rows=S.vol.rows.filter(r=>S.vol.sel.has(r.id));
    const box=clear($('vol-preview'));
    const options=volOptions();
    box.append(el('p',{text:rows.length + ' volume · ' + rows.reduce((a,r)=>a+Number(r.size || 0),0) + ' GB'}),el('code',{class:'mono',text:['migrate','volume',rows[0]?.id || '<volume>',...Object.entries(options).flatMap(([k,v])=>['--'+k.replaceAll('_','-'),...(v === true?[]:[v])])].join(' ')}));
    if(options.in_use)box.append(el('p',{class:'msg err',text:'Volume đang gắn: bản copy crash-consistent, ứng dụng nguồn vẫn có thể ghi dữ liệu.'}));
  }
  function previewOptions() {
    const o = vmOptions(), live = o.mode === 'live' || (!o.mode && defaultMode() === 'live');
    for (const id of ['o-max-passes','o-delta-limit']) { $(id).disabled = !live; $(id).closest?.('label')?.classList.toggle('hidden',!live); }
    const row = S.vm.rows.find(r=>S.vm.sel.has(r.id));
    const args = ['migrate','migrate',row?.id || '<VM>'];
    const sample = {...o};
    if(row && $('o-ip').value === 'row' && S.vm.ip[row.id]) sample.ip=S.vm.ip[row.id];
    for (const [k,v] of Object.entries(sample)) { const flag='--' + k.replaceAll('_','-'); if(Array.isArray(v)) for(const item of v)args.push(flag,String(item)); else {args.push(flag);if(v!==true)args.push(String(v));} }
    const danger = o.keep_source_ports || o.start_source || o.verify === 'none';
    const box = clear($('option-preview'));
    box.append(el('p',{text:S.vm.sel.size + ' VM · mode ' + (o.mode || 'theo mặc định') + ' · verify ' + (o.verify || 'theo mặc định')}),el('code',{class:'mono',text:args.join(' ')}),el('p',{text:o.volumes_only ? 'Chỉ sao chép volume. Nguồn giữ nguyên.' : 'Dừng nguồn khi cutover; tạo tài nguyên đích. ' + (o.start_source ? 'Bật lại nguồn sau khi xong.' : 'Nguồn để SHUTOFF + locked.')}));
    if (danger) box.append(el('p',{class:'msg err',text:'Ngoại lệ cần đối chiếu: ' + [o.keep_source_ports?'giữ port có nguy cơ trùng IP / split-brain':null,o.start_source?'bật nguồn có thể trùng dịch vụ':null,o.verify === 'none'?'không kiểm tra dữ liệu sau copy':null].filter(Boolean).join('; ')}));
  }
  function groupOptions() {
    if (!document.querySelectorAll('#option-groups label').length) return;
    const rowIps = $('vm-row-ips');
    const root = clear($('option-groups'));
    for (const [title,ids] of [['Phương thức & Live',['o-mode','o-max-passes']],['Mạng',['o-ip','o-keep-ports']],['Lưu trữ',['o-volume-type','o-volume-az','o-verify']],['Tính toán & nhóm',['o-flavor','o-flavor-fallback','o-security-group','o-server-group','o-copy-sg','o-copy-srvgrp']],['Nâng cao',['o-start-source','o-volumes-only']]]) {
      const field = el('fieldset',{class:'option-group'},el('legend',{text:title}));
      for (const id of ids) { const label = optionLabels[id]; if (label) { if (label.querySelector('input[type=checkbox]')) label.classList.add('check-option'); field.append(label); } }
      if (title === 'Mạng') field.append(rowIps);
      root.append(field);
    }
  }
  const optionLabels = {};
  for (const node of document.querySelectorAll('#option-groups input, #option-groups select')) optionLabels[node.id] = node.closest('label');
  groupOptions();
  function historyQuery() {
    return [['offset',S.historyOffset || 0],['limit',50],['q',$('hist-search').value.trim()],['state',$('hist-state').value],['kind',$('hist-kind').value],['from',$('hist-from').value],['to',$('hist-to').value]].map(([k,v])=>k+'='+encodeURIComponent(v)).join('&');
  }
  function sourceStillDown(j) { if (S.cli && Array.isArray(S.cli.live) && j?.ident && !liveIdents().has(j.kind === 'vol' ? 'vol-' + j.ident : j.ident)) return false; return !!j?.t_stop && !j.rolled_back && !S.jobs.some(r=>r.kind === 'rollback' && r.ident === j.ident && r.state === 'OK' && Date.parse(r.t_end) >= Date.parse(j.t_stop)) && (['FAIL','CANCELLED','LOST'].includes(j.state) || Number(j.exit_class) === 4); }
  function volumeLabel(j) { const total=volumeTotal(j); return (j?.phase_detail?.volumes_done ?? (Array.isArray(j?.phase_detail?.done) ? j.phase_detail.done.length : j?.state === 'OK' && total != null ? total : S.volsDone?.size || 0)) + '/' + (total ?? '?'); }
  function volumeTotal(j) {
    if (!j) return null;
    if (Number.isFinite(j.phase_detail?.volumes_total)) return j.phase_detail.volumes_total;
    if (Number.isFinite(j.volumes_total)) return j.volumes_total;
    const row = Object.values(S.vm.plan).concat(Object.values(S.vol.plan)).find(r=>r.ident === j.ident);
    if (Number.isFinite(row?.volumes_total)) return row.volumes_total;
    if (Array.isArray(row?.volumes)) return row.volumes.length;
    const source = S.vm.rows.find(r=>r.id === j.ident);
    if (source?.volumes) return source.volumes.length;
    return j.kind === 'vol' ? 1 : null;
  }
  function cancelRemaining(j, now = Date.now()) {
    const at = Date.parse(j?.cancel?.sigint_at);
    if (!Number.isFinite(at)) return null;
    return Math.max(0,Math.ceil((at + Number(S.cancelDurations?.[j.id] ?? 120)*1000 - now)/1000));
  }
  function syncCancel(j) {
    const left = cancelRemaining(j), running = j.state === 'RUNNING';
    if (S.cancelJob !== j.id) { S.cancelJob = j.id; if (S.armedJobId !== j.id) S.armedJobId = null; $('run-kill-typed').value = ''; }
    const ready = running && left === 0 && !j.cancel?.sigkill_at && S.killSent !== j.id;
    $('run-kill-typed').disabled = !ready;
    $('run-kill').title = left > 0 ? 'mở sau ' + left + ' s' : left === null ? 'Gửi SIGINT trước' : '';
    lock($('run-kill'),!ready || $('run-kill-typed').value.trim() !== j.name, S.killSent === j.id || j.cancel?.sigkill_at ? 'Đã gửi SIGKILL' : 'Buộc dừng (SIGKILL)');
  }
  function setConnection(state) {
    S.connection = state;
    if (state === 'reconnecting') S.disconnectedAt ||= new Date().toISOString(); else S.disconnectedAt = null;
    const label = {connecting:'Đang kết nối…',connected:'● Log trực tiếp',reconnecting:'Đang nối lại · mất từ ' + hms(S.disconnectedAt),complete:'✓ Log đã kết thúc'}[state];
    $('run-connection').textContent = label;
    $('run-connection').className = 'pill push ' + (state === 'connected' || state === 'complete' ? 'ok' : 'warn');
  }
  function setSegment(name) { for(const id of ['copy','verify','create']) $('segment-'+id).classList.toggle('active',name === id || id === 'copy' && name === 'blocksync'); }
  function renderLog() {
    show($('run-log-empty'), !(S.logLines || []).length);
    $('run-log-empty').textContent = S.connection === 'complete' ? 'Job kết thúc, không có dòng log.' : 'đang chờ dòng log đầu tiên…';
    $('run-log-empty').classList.toggle('skeleton', S.connection !== 'complete');
    const mode = $('run-log-filter').value || 'all', q = $('run-log-search').value.toLowerCase();
    $('run-log').textContent = (S.logLines || []).filter(l=>(!q || l.toLowerCase().includes(q)) && (mode === 'all' || mode === 'event' && l.includes('@@') || mode === 'warn' && /WARN|FAIL|ERROR|Traceback/i.test(l))).join('\n');
    if ($('run-autoscroll').checked) $('run-log').scrollTop = $('run-log').scrollHeight;
  }
  function openRollbackDialog() {
    const b = S.confirm, o = b.original || {}, scope = o.delete || {};
    const content = el('div');
    const ids = el('details', null, el('summary',{text:'ID tài nguyên sẽ xóa'}));
    const counts = [];
    for (const key of ['server','ports','volumes','images']) {
      const values = key === 'server' ? (scope.server ? [scope.server] : []) : (scope[key] || []);
      counts.push(values.length + ' ' + {server:'server',ports:'port',volumes:'volume',images:'image'}[key]);
      ids.append(el('p',null,el('strong',{text:key + ': '}),el('code',{text:values.join(', ') || '—'})));
    }
    const typed = el('input',{type:'text','aria-label':'Tên hoặc ID đầy đủ để rollback',autocomplete:'off'});
    const ack = el('input',{type:'checkbox'});
    const expiry = el('p',{class:'hint',role:'status'});
    S.rollbackFields = {typed,ack,expiry};
    content.append(el('p',{class:'red',text:'Sẽ xóa: ' + counts.join(' · ')}),ids,
      el('p',{text:'Cutover: ' + fmtTime(o.t_stop) + ' · tuổi: ' + (o.age_seconds == null ? 'không ghi nhận' : dur(Math.floor(o.age_seconds)))}),
      el('p',{text:'Trạng thái server đích: ' + (o.dst_server_status || 'không ghi nhận')}),
      el('p',{text:'Nguồn đang ' + (o.source_power || 'không ghi nhận') + ' · ' + (o.source_power === 'SHUTOFF' ? 'nguồn sẽ được giữ SHUTOFF' : 'nguồn sẽ được bật lại')}),
      el('p',{text:'Nguồn hiện tại: ' + (o.source_status === 'GONE' ? 'không còn tồn tại' : o.source_status == null ? 'không đọc được' : o.source_status)}),
      el('label',{class:'warning-ack'},ack,el('span',{text:'Tôi chấp nhận RPO: dữ liệu ghi ở đích từ ' + fmtTime(o.t_stop) + ' sẽ mất.'})),
      el('label',{class:'lbl'},'Gõ tên ' + b.names[0] + ' hoặc ID đầy đủ ' + (b.rule.ids || b.rule.alt || []).join(', '),typed),expiry);
    dialog('Rollback ' + b.names[0], content, async()=>{
      if (!rollbackMatches(b)) return false;
      const r = await api('POST','/api/batches/' + b.id + '/run',{typed:typed.value,confirm_token:b.token,continue_on_error:false});
      if (r.status !== 202) {
        flash('Không rollback: ' + err(r));
        if (r.status === 409) { closeDialog(); return false; }
        if (r.status === 423) refreshJobs();
        return false;
      }
      S.watch=(r.data.jobs || [])[0] || null; S.watchPinned=false;
      closeDialog();
      location.hash='#running';
      return false;
    },'Rollback ' + b.names[0]);
    S.rollbackDialog=true;
    $('action-dialog').classList.add('rollback-dialog');
    typed.addEventListener('input',rollbackRunState);
    ack.addEventListener('change',rollbackRunState);
    clearInterval(S.tick);
    S.tick=setInterval(rollbackRunState,1000);
    rollbackRunState();
  }
  function rollbackMatches(b) {
    return S.confirm === b && !S.active && b.expires_in > (Date.now()-b.at)/1000 && S.rollbackFields.ack.checked && confirmationMatches(b,S.rollbackFields.typed.value);
  }
  function rollbackRunState() {
    if (!S.rollbackDialog || !S.confirm) return;
    const b=S.confirm, left=b.expires_in-(Date.now()-b.at)/1000;
    S.rollbackFields.expiry.textContent=left>0 ? 'Xác nhận hết hạn sau ' + mmss(left) : 'Hết hạn: đóng và chuẩn bị Rollback lại.';
    lock($('dialog-accept'),!rollbackMatches(b),'Rollback ' + b.names[0]);
  }
  function dialog(title, content, accept, label = 'Tiếp tục') {
    $('action-dialog').classList.remove('rollback-dialog');
    S.dialogReturn = S.dialogOpener || document.activeElement; S.dialogOpener = null;
    $('dialog-title').textContent = title; clear($('dialog-body')).append(content);
    $('dialog-accept').textContent = label; $('dialog-accept').className=/SIGINT|SIGKILL|^Rollback /.test(title)?'btn danger solid':'btn primary'; S.dialogAction = accept;
    lock($('dialog-accept'),false);
    $('action-dialog').showModal();
  }
  function closeDialog() {
    $('action-dialog').close?.();
    if (S.rollbackDialog) { clearInterval(S.tick); S.confirm=null; S.rollbackDialog=false; S.rollbackFields=null; }
    S.dialogAction=null;
    const back = S.dialogReturn; S.dialogReturn = null;
    const heading = $('screen-' + S.screen)?.querySelector?.('h1, h2');
    const target = back && back.isConnected !== false ? back : heading;
    if (target === heading) heading?.setAttribute('tabindex', '-1');
    if (target?.disabled) {
      const screen = S.screen; let n = 0;
      const t = setInterval(() => {
        const moved = document.activeElement && document.activeElement !== document.body;
        if (moved || S.screen !== screen || $('action-dialog').open) { clearInterval(t); return; }
        if (target.isConnected === false) { clearInterval(t); heading?.setAttribute('tabindex', '-1'); heading?.focus?.(); return; }
        if (!target.disabled) { clearInterval(t); target.focus?.(); return; }
        if (++n > 50) { clearInterval(t); heading?.setAttribute('tabindex', '-1'); heading?.focus?.(); }
      }, 100);
    }
    else target?.focus?.();
  }
  $('dialog-close').addEventListener('click',closeDialog);
  $('dialog-accept').addEventListener('click',()=>busy($('dialog-accept'),'Đang xử lý…',async()=>{ const action = S.dialogAction; if (action && await action() !== false) closeDialog(); }));
  $('action-dialog').addEventListener('cancel',(ev)=>{ ev.preventDefault?.(); closeDialog(); });
  $('doctor-write').addEventListener('click',()=>{
    if (!S.session?.features?.write_check || S.doctorRunning) return;
    dialog('Kiểm tra ghi RBD',el('p',{text:'Tạo image canary 4 MiB trong pool đích, ghi / đọc / discard rồi xóa. Thao tác kiểm tra quyền ghi thực tế; không thay đổi volume của VM.'}),()=>busy($('doctor-write'),'Đang kiểm tra…',async()=>{
      if (S.doctorRunning) return false;
      S.doctorRunning = busy($('doctor-run'),'Đang kiểm tra ghi…',()=>doctorOnce(true));
      try { await S.doctorRunning; } finally { S.doctorRunning=null; }
    }),'Kiểm tra ghi RBD');
  });
  function pasteSelection(kind) {
    const input = el('textarea',{rows:8,'aria-label':'Tên hoặc ID mỗi dòng',placeholder:'app01\napp02'});
    const note = el('p',{class:'hint',text:'Mỗi dòng một tên hoặc ID đầy đủ. Chỉ chọn dòng khớp duy nhất và đủ điều kiện.'});
    dialog('Dán danh sách ' + (kind === 'vm'?'VM':'volume'),el('div',null,note,input),()=>{
      const notFound = [], ambiguous = [], ineligible = [], k = S[kind]; let n = 0;
      for (const text of [...new Set(input.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))]) {
        const matches = k.rows.filter(r=>(r.id === text || r.name === text));
        if (!matches.length) notFound.push(text);
        else if (matches.length > 1) ambiguous.push(text);
        else if (kind === 'vm'?vmEligible(matches[0]):volEligible(matches[0])) { k.sel.add(matches[0].id); n++; }
        else ineligible.push(text);
      }
      (kind === 'vm'?renderVm:renderVol)();
      const problems = [notFound.length ? 'không có trong danh sách đã tải: ' + notFound.join(', ') : '', ambiguous.length ? 'trùng tên, dán ID thay vì tên: ' + ambiguous.join(', ') : '', ineligible.length ? 'không chọn được (đã chuyển, đang chạy hoặc trạng thái không phù hợp): ' + ineligible.join(', ') : ''].filter(Boolean);
      if (problems.length) { note.textContent='Đã chọn ' + n + '. ' + problems.join(' · '); note.className='msg err'; return false; }
      flash('Đã chọn ' + n + ' tài nguyên.', 'okm');
    },'Chọn các dòng khớp');
  }
  for (const kind of ['vm','vol']) $(kind+'-paste').addEventListener('click',()=>pasteSelection(kind));
  for (const id of ['o-mode','o-ip','o-volume-type','o-volume-az','o-flavor','o-verify','o-max-passes','o-delta-limit','o-security-group','o-server-group','o-copy-sg','o-copy-srvgrp','o-flavor-fallback','o-start-source','o-volumes-only','o-keep-ports','vm-dst-az','vm-dst-host']) {
    for (const event of ['input','change']) $(id).addEventListener(event,()=>{ vmCounts(); previewOptions(); });
  }
  for (const id of ['vol-az','vol-volume-az','vol-volume-type','vol-verify','vol-in-use']) $(id).addEventListener('change',volCounts);
  $('o-keep-ports').addEventListener('change',()=>{
    if (!$('o-keep-ports').checked) return;
    $('o-keep-ports').checked=false; vmCounts(); previewOptions();
    const ack=el('input',{type:'checkbox'});
    dialog('Giữ port nguồn — nguy cơ split-brain',el('label',{class:'warning-ack'},ack,el('span',{text:'Tôi đã kiểm tra cách ly mạng và hiểu hai VM có thể cùng IP. Tôi chấp nhận giữ port nguồn.'})),()=>{if(!ack.checked)return false;$('o-keep-ports').checked=true;vmCounts();previewOptions();},'Áp dụng ngoại lệ');
  });
  $('cf-typed').addEventListener('input',cfRunState);
  $('cf-typed').addEventListener('keydown',ev=>{ if (ev.key === 'Enter' && !ev.shiftKey && !$('cf-run').disabled) { ev.preventDefault(); $('cf-run').click(); } });
  $('run-kill-typed').addEventListener('input',()=>{ const j=currentJob(); if(j)syncCancel(j); });
  for (const id of ['run-log-filter','run-log-search']) $(id).addEventListener('input',renderLog);
  $('hist-filter-form').addEventListener('submit',ev=>{ ev.preventDefault(); return busy($('hist-apply'),'Đang tải…',async()=>{ S.historyOffset=0; await refreshHistory(); }); });
  for (const [id,delta] of [['hist-prev',-50],['hist-next',50]]) $(id).addEventListener('click',()=>busy($(id),'Đang tải…',async()=>{S.historyOffset=Math.max(0,(S.historyOffset || 0)+delta);await refreshHistory();}));
  $('flash').addEventListener('click',()=>flash(''));
  $('overview-new').addEventListener('click',()=>{ S.campaignDoctor=true; if(location.hash === '#doctor') route(); });
  for (const a of document.querySelectorAll('#nav a, #screen-overview a:not(#overview-new)')) a.addEventListener('click',()=>{ S.campaignDoctor=false; });
  $('overview-doctor-run').addEventListener('click',()=>busy($('overview-doctor-run'),'Đang chạy Doctor…',async()=>{ S.campaignDoctor=false; await runDoctor(); location.hash='#doctor'; }));
  $('vol-next').addEventListener('click',ev=>{if(!S.vol.sel.size){ev.preventDefault();flash('Chọn ít nhất một volume để tiếp tục.','info');}});
  $('vm-next').addEventListener('click',ev=>{if(!S.vm.sel.size){ev.preventDefault();flash('Chọn ít nhất một VM để tiếp tục.','info');}});
  window.addEventListener('keydown',ev=>{
    if (ev.key === '/' && !['INPUT','TEXTAREA','SELECT'].includes(ev.target?.tagName) && !ev.target?.isContentEditable) {
      const id = {instance:'vm-search',volume:'vol-search',history:'hist-search',running:'run-log-search'}[S.screen];
      if (id) {ev.preventDefault();$(id).focus();}
    }
    if(ev.key === 'Escape'){
      if (ev.mselClosed) return;
      if($('action-dialog').open){ closeDialog(); return; }
      for(const d of document.querySelectorAll('details[open]'))d.open=false; show($('hist-diff-card'),false);
      if (S.screen === 'history' && S.histOpen?.size) {
        const last = [...S.histOpen].pop(); S.histOpen.clear(); renderHistory();
        document.querySelector?.('#hist-rows button[data-job="' + last + '"]')?.focus?.();
      }
    }
  });
  function applyTheme(value) { document.documentElement?.setAttribute('data-theme',value); $('theme-toggle').textContent=value === 'dark'?'Giao diện sáng':'Giao diện tối'; $('theme-toggle').setAttribute('aria-label',$('theme-toggle').textContent); $('theme-toggle').title=value === 'dark'?'Đang dùng giao diện tối — bấm để chuyển sang sáng':'Đang dùng giao diện sáng — bấm để chuyển sang tối'; }
  try { const saved = localStorage.getItem('migui-theme'); if (saved === 'light' || saved === 'dark') applyTheme(saved); else { const dark = !!window.matchMedia?.('(prefers-color-scheme: dark)').matches; $('theme-toggle').textContent = dark ? 'Giao diện sáng' : 'Giao diện tối'; $('theme-toggle').title = dark ? 'Đang dùng giao diện tối — bấm để chuyển sang sáng' : 'Đang dùng giao diện sáng — bấm để chuyển sang tối'; } } catch (_) {}
  $('theme-toggle').setAttribute('aria-label',$('theme-toggle').textContent || 'Đổi giao diện sáng tối');
  $('theme-toggle').addEventListener('click',()=>{ const current=document.documentElement?.getAttribute('data-theme') || (window.matchMedia?.('(prefers-color-scheme: dark)').matches?'dark':'light'); const next=current === 'dark'?'light':'dark'; applyTheme(next); try {localStorage.setItem('migui-theme',next);}catch(_){} });
  setInterval(()=>{const j=currentJob();if(j)syncCancel(j);},1000);
  if (testMode) Object.assign(window.miguiTest,{openConfirm,renderRunning,runLocks,openStream,renderConfirm,vmCounts,volCounts,batchImpact,planRun,selectionFingerprint,confirmationMatches,cancelRemaining,sourceStillDown,volumeTotal,historyQuery,jobsEnvelope,setConnection,state:S,refreshHistory,refreshJobs,renderOverview,renderRowIps,renderVm,updateStepper,sendSigint});

  // --- start ------------------------------------------------------------------------------------------------------
  window.addEventListener('hashchange', route);
  // the skip link moves the focus into the screen being shown; following its href (#workspace) would route to Tổng quan
  document.querySelector?.('.skip-link')?.addEventListener('click', (ev) => {
    ev.preventDefault();
    const main = $('workspace');  // the main region, as a skip link should: the next Tab reaches the screen's first control
    main.setAttribute('tabindex', '-1');
    main.focus();
  });
  setInterval(sessionInfo, 60000);
  (async () => {
    const r = await api('GET', '/api/session', null, true);
    if (r.status === 200) {
      S.session = Object.assign({ at: Date.now() }, r.data);
      showApp();
    } else {
      showLogin();
    }
  })();
})();
