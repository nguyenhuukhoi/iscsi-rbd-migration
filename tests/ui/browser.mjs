// Dependency-free Node 24 CDP smoke test. ONLY a disposable stack: rolls back app01/app02 and starts a migration.
// Start Chrome/Edge with --remote-debugging-port=9222 and a separate --user-data-dir.
// node tests/ui/browser.mjs --url http://127.0.0.1:8790/ --user test --password test --cdp 9222 --out shots
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => {
  if (v.startsWith('--')) a.push([v.slice(2), all[i + 1]]);
  return a;
}, []));
let pass = 0, fail = 0, socket, target, cdpBase;
const pending = new Map();
const errors = [];
let serial = 0;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(label, condition, detail = '') {
  console.log(`${condition ? 'PASS' : 'FAIL'} ${label}${detail ? ': ' + detail : ''}`);
  if (condition) pass++; else { fail++; throw new Error(label); }
}
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ': ' + (r.exceptionDetails.exception?.description || ''));
  return r.result.value;
}
async function wait(expression, label, timeout = 120000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    if (errors.length) throw new Error('Browser exception: ' + errors.join('\n'));
    if (await evaluate(expression)) { check(label, true); return; }
    await sleep(200);
  }
  const state = await evaluate(`({screen:location.hash,flash:document.getElementById('flash')?.textContent,plan:document.getElementById('vm-plan-panel')?.textContent})`);
  check(label, false, JSON.stringify(state));
}
const visible = id => `!!document.getElementById(${JSON.stringify(id)}) && !document.getElementById(${JSON.stringify(id)}).classList.contains('hidden')`;
const click = id => evaluate(`document.getElementById(${JSON.stringify(id)}).click()`);
const fill = (id, value) => evaluate(`(()=>{const e=document.getElementById(${JSON.stringify(id)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
async function screen(name) {
  await evaluate(`location.hash=${JSON.stringify('#' + name)}`);
  await wait(visible('screen-' + name), `screen ${name}`);
}
async function shot(name) {
  await evaluate('document.fonts.ready');
  const { cssContentSize } = await send('Page.getLayoutMetrics');
  const { data } = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true,
    clip: { x: 0, y: 0, width: Math.ceil(cssContentSize.width), height: Math.min(12000, Math.ceil(cssContentSize.height)), scale: 1 } });
  await fs.writeFile(path.join(args.out, name + '.png'), Buffer.from(data, 'base64'));
}
try {
  if (!args.url || !args.user || !args.password || !args.out) throw new Error('Required: --url URL --user U --password P --cdp 9222 --out DIR');
  if (Number(process.versions.node.split('.')[0]) !== 24) throw new Error('Use Node 24 only.');
  cdpBase = /^https?:/.test(args.cdp || '') ? args.cdp : `http://127.0.0.1:${args.cdp || 9222}`;
  await fs.mkdir(args.out, { recursive: true });
  const response = await fetch(`${cdpBase}/json/new?about:blank`, { method: 'PUT', signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error(`Cannot create CDP target: HTTP ${response.status}`);
  target = await response.json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('CDP WebSocket connection timed out')), 10000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', error => { clearTimeout(timer); reject(error); }, { once: true });
  });
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data);
    if (data.id && pending.has(data.id)) {
      const p = pending.get(data.id); clearTimeout(p.timer); pending.delete(data.id);
      if (data.error) p.reject(new Error(data.error.message)); else p.resolve(data.result);
    }
    if (data.method === 'Runtime.exceptionThrown') errors.push(data.params.exceptionDetails.exception?.description || data.params.exceptionDetails.text);
    if (data.method === 'Log.entryAdded' && data.params.entry.source === 'security' && data.params.entry.level === 'error') errors.push(data.params.entry.text);
  });
  // Exercise this checkout against the real API even when the stack serves an older UI.
  // Only static responses are replaced; preserve the web server's CSP and other headers.
  const staticRoot = fileURLToPath(new URL('../../migui/static/', import.meta.url));
  const assets = new Map(await Promise.all([['/','index.html'],['/app.js','app.js'],['/app.css','app.css']].map(async ([url,file])=>[url,await fs.readFile(path.join(staticRoot,file))])));
  socket.addEventListener('message', async event => {
    const data=JSON.parse(event.data);
    if (data.method !== 'Fetch.requestPaused') return;
    const p=data.params, body=assets.get(new URL(p.request.url).pathname);
    try {
      if (!body) await send('Fetch.continueRequest',{requestId:p.requestId});
      else await send('Fetch.fulfillRequest',{requestId:p.requestId,responseCode:200,
        responseHeaders:(p.responseHeaders || []).filter(h=>!['content-length','content-encoding'].includes(h.name.toLowerCase())),body:body.toString('base64')});
    } catch (error) { if (!/Invalid InterceptionId/.test(error.message)) errors.push(error.message); } // navigation can cancel an intercepted response
  });
  await send('Fetch.enable',{patterns:[...assets.keys()].map(p=>({urlPattern:new URL(p,args.url).href + (p==='/'?'*':''),requestStage:'Response'}))});
  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: args.url });
  await wait(`document.getElementById('login-form') && document.readyState==='complete'`, 'worktree UI loaded with server CSP');
  check('production URL exposes no test hooks',await evaluate(`typeof window.miguiTest === 'undefined'`));
  const testUrl = new URL(args.url); testUrl.searchParams.set('test','1');
  await send('Page.navigate', { url: testUrl.href });
  await wait(`document.getElementById('login-form') && (${visible('screen-login')} || ${visible('nav')})`, 'application loaded');
  // Begin consistently in light mode, then verify the alternate theme at the end.
  if (await evaluate(`(document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'))==='dark'`)) await click('theme-toggle');
  check('light theme action and accessible name agree',await evaluate(`document.getElementById('theme-toggle').textContent==='Giao diện tối' && document.getElementById('theme-toggle').getAttribute('aria-label')==='Giao diện tối'`));
  if (await evaluate(visible('nav'))) await click('logout');
  await wait(visible('screen-login'), 'login visible');
  await shot('01-login');
  await fill('login-user', args.user); await fill('login-password', args.password);
  await click('login-submit');
  await wait(visible('screen-overview'), 'login reaches overview');
  check('overview hides campaign stepper',await evaluate(`document.getElementById('stepper').classList.contains('hidden')`));
  await wait(`window.miguiTest.state.overviewCounts?.length === 4`, 'overview server totals loaded');
  await shot('02-overview');
  await screen('doctor');
  check('nav Doctor hides campaign stepper',await evaluate(`document.getElementById('stepper').classList.contains('hidden')`));
  await wait(`document.getElementById('doctor-dot').className === 'dot-ok' && !document.getElementById('doctor-run').disabled`, 'Doctor ready', 180000);
  await shot('03-doctor');
  await screen('instance');
  await wait(`document.querySelectorAll('#vm-src-host input[type=checkbox]').length || document.querySelectorAll('#vm-src-project input[type=checkbox]').length`, 'inventory loaded');
  await click('vm-load');
  await wait(`window.miguiTest.state.vm.rows.length > 0`, 'instance list loaded');
  // A repeated smoke run may leave app01/app02 migrated. Recover only these named test fixtures.
  for (const name of ['app01','app02']) {
    if (!await evaluate(`window.miguiTest.state.vm.rows.some(r=>r.name===${JSON.stringify(name)} && r.state?.live)`)) continue;
    await evaluate(`Array.from(document.querySelectorAll('#vm-rows tr')).find(r=>r.textContent.includes(${JSON.stringify(name)})).querySelector('button.danger').click()`);
    await wait(`document.getElementById('action-dialog').open`, 'rollback dialog for ' + name);
    check('rollback keeps confirmation screen outside dialog',await evaluate(`!document.getElementById('action-dialog').contains(document.getElementById('screen-confirm')) && location.hash==='#instance' && document.getElementById('dialog-accept').classList.contains('danger')`));
    await evaluate(`(()=>{const e=document.querySelector('#dialog-body input[type=text]');e.value=${JSON.stringify(name)};e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    check('rollback name alone cannot unlock action',await evaluate(`document.getElementById('dialog-accept').disabled`));
    await evaluate(`document.querySelector('#dialog-body input[type=checkbox]').click()`);
    await shot('rollback-' + name);
    await click('dialog-accept');
    await wait(`location.hash==='#running' && !document.getElementById('action-dialog').open`, 'rollback accepted for ' + name);
    await wait(`window.miguiTest.state.jobs.some(j=>j.kind==='rollback' && j.name===${JSON.stringify(name)} && j.id===window.miguiTest.state.watch && j.state==='OK')`, 'rollback finished for ' + name);
    await screen('instance'); await click('vm-load');
    await wait(`!document.getElementById('vm-load').disabled`, 'instance list refreshed');
  }
  await wait(`document.querySelectorAll('#vm-rows input[type=checkbox]:not(:disabled)').length >= 2`, 'at least two eligible instances');
  await evaluate(`Array.from(document.querySelectorAll('#vm-rows input[type=checkbox]:not(:disabled)')).slice(0,2).forEach(e=>e.click())`);
  check('exactly two selected', await evaluate(`document.getElementById('vm-selected').textContent === '2'`));
  check('row checkboxes labelled', await evaluate(`Array.from(document.querySelectorAll('#vm-rows input[type=checkbox]')).every(e=>!!e.getAttribute('aria-label'))`));
  await shot('04-selection');
  await click('vm-next'); await wait(visible('screen-options'), 'options step visible');
  await fill('o-ip','row');
  check('row IP inputs are in the Network group only',await evaluate(`document.querySelectorAll('#vm-row-ips input').length===2 && document.getElementById('vm-row-ips').closest('fieldset').querySelector('legend').textContent==='Mạng' && !document.querySelector('#vm-rows input[type=text]')`));
  await evaluate(`(()=>{const e=document.querySelector('#vm-row-ips input');e.value='999.1.2.3';e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  check('invalid row IPv4 disables planning',await evaluate(`document.getElementById('vm-plan').disabled && document.querySelector('#vm-row-ips input').getAttribute('aria-invalid')==='true'`));
  await evaluate(`(()=>{const e=document.querySelector('#vm-row-ips input');e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  await shot('05-row-ips');
  await fill('o-ip','same');
  // Select a concrete compute AZ to make copied group naming unambiguous on multi-AZ fixtures.
  const az = await evaluate(`Array.from(document.getElementById('vm-dst-az').options).find(o=>o.value&&!o.disabled)?.value`);
  if (az) await fill('vm-dst-az', az);
  await shot('05-options');
  await click('vm-plan');
  await wait(`!document.getElementById('vm-plan').disabled && !document.getElementById('vm-to-confirm').disabled`, 'plan completed with runnable batch', 600000);
  await shot('06-plan');
  await click('vm-to-confirm'); await wait(visible('screen-confirm'), 'confirmation visible');
  check('confirmation disabled before typing', await evaluate(`document.getElementById('cf-run').disabled`));
  check('impact visible', await evaluate(`document.getElementById('cf-impact').textContent.length > 30`));
  check('hashes are in closed technical details',await evaluate(`document.getElementById('cf-technical').closest('details').open===false && !document.getElementById('cf-conf').textContent.includes('fingerprint')`));
  await fill('cf-typed', '2');
  await evaluate(`document.querySelectorAll('#cf-warnings input[type=checkbox]').forEach(e=>e.click())`);
  await wait(`!document.getElementById('cf-run').disabled`, 'count and acknowledgements unlock run');
  await shot('07-confirm');
  const batchJobs = await evaluate('window.miguiTest.state.confirm.jobs');
  await click('cf-run'); await wait(visible('screen-running'), 'run accepted');
  await wait(visible('run-panel'), 'job visible'); await shot('08-running');
  await wait(`!window.miguiTest.state.active && ${JSON.stringify(batchJobs)}.length===2 && ${JSON.stringify(batchJobs)}.every(id=>window.miguiTest.state.jobs.some(j=>j.id===id && j.state==='OK'))`, 'both jobs finish OK', 600000);
  await wait(`document.getElementById('run-cancel-panel').classList.contains('hidden')`, 'cancel panel hidden after completion');
  check('completed batch retains last job phase, volumes and result',await evaluate(`(()=>{const j=window.miguiTest.state.jobs.find(j=>j.id===window.miguiTest.state.watch);return j.id===${JSON.stringify(batchJobs)}.at(-1) && document.getElementById('st-pass').textContent===j.phase_detail.name && document.getElementById('st-vol').textContent===j.phase_detail.volumes_done+'/'+j.phase_detail.volumes_total && document.getElementById('st-res').textContent==='OK';})()`));
  await shot('09-completed');
  await screen('history');
  await wait(`document.querySelectorAll('#hist-rows button').length >= 2`, 'history contains completed jobs');
  await shot('10-history');
  await evaluate(`document.querySelector('#hist-rows button').click()`);
  await wait(`document.querySelector('#hist-rows .card')`, 'history detail expanded');
  await shot('11-history-detail');
  await click('theme-toggle'); await shot('12-theme');
  check('dark mode persisted', await evaluate(`document.documentElement.getAttribute('data-theme')==='dark' && localStorage.getItem('migui-theme')==='dark'`));
  check('dark theme action and accessible name agree',await evaluate(`document.getElementById('theme-toggle').textContent==='Giao diện sáng' && document.getElementById('theme-toggle').getAttribute('aria-label')==='Giao diện sáng'`));
  await send('Emulation.setDeviceMetricsOverride', { width: 1024, height: 900, deviceScaleFactor: 1, mobile: false });
  await shot('13-responsive-1024');
  check('1024px layout stays within viewport', await evaluate(`document.documentElement.scrollWidth <= innerWidth`));
  check('no browser or CSP exceptions', errors.length === 0, errors.join('\n'));
} catch (error) {
  if (!fail) fail++;
  console.error('FAIL smoke: ' + error.message);
  if (socket?.readyState === 1) { try { await shot('FAIL'); } catch (_) {} }
} finally {
  for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error('Test ended')); }
  pending.clear(); socket?.close();
  if (target && cdpBase) { try { await fetch(`${cdpBase}/json/close/${target.id}`, { signal: AbortSignal.timeout(5000) }); } catch (_) {} }
  console.log(`TOTAL pass=${pass} fail=${fail}`);
  process.exitCode = fail ? 1 : 0;
}
