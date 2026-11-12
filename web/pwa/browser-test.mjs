// The built phone app (`make pwa`) in headless Chrome (`make pwa-browser-test`):
//   node web/pwa/browser-test.mjs <pwa dir> [chrome]
// Boot; the team's shape search offered phone-sized; the app closing mid-run and Resume; pause,
// resume and End; a fixed-hull run, its result, STEP export and history; offline reload from the
// service worker cache with the result restored from IndexedDB; phone width with no sideways
// scroll. No dependencies: Chrome's DevTools protocol over Node's WebSocket.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const [dir, given] = process.argv.slice(2);
const chrome = given ?? [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
].find(existsSync);
if (!dir || !chrome) {
  console.error('usage: node web/pwa/browser-test.mjs <pwa dir> [chrome] (no Chrome found)');
  process.exit(2);
}
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.wasm': 'application/wasm', '.json': 'application/json',
  '.webmanifest': 'application/manifest+json', '.png': 'image/png', '.toml': 'text/plain', '.csv': 'text/csv' };
const server = createServer(async (req, res) => {
  let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (!p.startsWith('/app/')) { res.writeHead(404); return res.end(); }
  p = p.slice(5) || 'index.html';
  if (p.endsWith('/')) p += 'index.html';
  try {
    const data = await readFile(join(dir, p));
    res.writeHead(200, { 'Content-Type': MIME[extname(p)] || 'application/octet-stream' });
    res.end(data);
  } catch { res.writeHead(404); res.end('nf'); }
}).listen(8123);

const profile = mkdtempSync(join(tmpdir(), 'jaguar-e2e-'));
const proc = spawn(chrome, ['--headless=new', '--remote-debugging-port=9333', `--user-data-dir=${profile}`, '--no-first-run', '--no-sandbox', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ver;
for (let i = 0; i < 50 && !ver; i++) { try { ver = await (await fetch('http://127.0.0.1:9333/json/version')).json(); } catch { await sleep(200); } }
const targets = await (await fetch('http://127.0.0.1:9333/json')).json();
const page = targets.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));
let id = 0; const pending = new Map(); const logs = [];
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === 'Runtime.consoleAPICalled') logs.push(`${m.params.type}: ${m.params.args.map(a => a.value ?? a.description).join(' ')}`);
  if (m.method === 'Page.javascriptDialogOpening') { logs.push(`dialog: ${m.params.type} ${m.params.message}`); ws.send(JSON.stringify({ id: ++id, method: 'Page.handleJavaScriptDialog', params: { accept: true } })); }
  if (m.method === 'Runtime.exceptionThrown') logs.push(`EXCEPTION: ${m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text}`);
};
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description ?? JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
};
const check = (cond, msg) => { console.log(`${cond ? 'ok  ' : 'FAIL'} ${msg}`); if (!cond) process.exitCode = 1; };

try {
  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:8123/app/' });
  let ready = false;
  for (let i = 0; i < 100 && !ready; i++) { await sleep(300); ready = await evaluate(`!!document.querySelector('#run') && document.querySelector('#prefnote')?.textContent !== ''`).catch(() => false); }
  check(ready, 'page booted (form filled from /api/params)');
  check(await evaluate('window.JAGUAR_PWA === true'), 'shim loaded');
  check(await evaluate(`!!document.getElementById('pwasave') && document.getElementById('deskbtns').hidden`), 'project buttons shown, desktop buttons hidden');
  const params = await evaluate(`fetch('/api/params').then(r => r.json()).then(j => j.source + ' ' + j.dir)`);
  check(params === 'file /JAGUAR', `params: ${params}`);
  // Shape search (the starter canoe.toml is the team's): pause holds it, resume goes on, End
  // verifies the best shape so far.
  // The team's 4000-sample search is offered phone-sized first.
  await evaluate(`document.getElementById('run').click()`);
  let dlg = '';
  for (let i = 0; i < 40 && !dlg; i++) { await sleep(250); dlg = await evaluate(`document.querySelector('.pwa-dialog')?.textContent ?? ''`); }
  check(/plans about [\d,]+ shapes/.test(dlg) && /Phone-sized \(about \d+ min\)/.test(dlg), `phone-sized offered: ${dlg.slice(0, 160)}...`);
  const sizedSamples = Number(/with ([\d,]+) samples/.exec(dlg)?.[1].replace(/,/g, ''));
  await evaluate(`document.querySelector('.pwa-dialog .btn.primary').click()`);
  await sleep(10000);
  // The app closes mid-run (a reload drops every worker): the next launch offers Resume, which
  // goes on from the shapes saved.
  let before = {};
  for (let i = 0; i < 240; i++) { before = await evaluate(`fetch('/api/status').then(r => r.json())`); if (before.done >= 3) break; await sleep(500); }
  await sleep(300); // the last shape's record lands in IndexedDB
  await send('Page.reload', {});
  let banner = false;
  for (let i = 0; i < 60 && !banner; i++) { await sleep(300); banner = await evaluate(`[...document.querySelectorAll('.pwa-banner button')].some(b => b.textContent === 'Resume')`).catch(() => false); }
  const text = await evaluate(`document.querySelector('.pwa-banner')?.textContent ?? ''`).catch(() => '');
  check(banner, `cut-off run offered: ${text}`);
  await evaluate(`[...document.querySelectorAll('.pwa-banner button')].find(b => b.textContent === 'Resume').click()`);
  let resumed = {};
  for (let i = 0; i < 60; i++) { await sleep(500); resumed = await evaluate(`fetch('/api/status').then(r => r.json())`).catch(() => ({})); if (resumed.state === 'running' && resumed.done > 0) break; }
  check(resumed.state === 'running' && before.done >= 3 && resumed.done >= before.done, `resumed with the saved shapes: ${before.done} before, ${resumed.done} after`);
  await sleep(3000);
  await evaluate(`fetch('/api/pause', { method: 'POST' })`);
  await sleep(8000); // shapes in progress finish
  const p1 = await evaluate(`fetch('/api/status').then(r => r.json())`);
  await sleep(4000);
  const p2 = await evaluate(`fetch('/api/status').then(r => r.json())`);
  check(p1.paused && p2.done === p1.done && p1.done > 0, `pause holds the shape search at ${p1.done}/${p1.total} (${p2.done})`);
  await evaluate(`fetch('/api/resume', { method: 'POST' })`);
  // Until 4 more shapes are scored (the device's seconds per shape is saved from 4 timed
  // shapes); a slow machine (CI) takes a while.
  let p3 = {};
  for (let i = 0; i < 300; i++) { await sleep(500); p3 = await evaluate(`fetch('/api/status').then(r => r.json())`); if (p3.done >= p2.done + 4) break; }
  check(!p3.paused && p3.done >= p2.done + 4, `resume goes on: ${p2.done} -> ${p3.done}`);
  await evaluate(`fetch('/api/stop', { method: 'POST' })`);
  let sst = {};
  for (let i = 0; i < 600; i++) { await sleep(500); sst = await evaluate(`fetch('/api/status').then(r => r.json())`); if (sst.state !== 'running') break; }
  const ended = await evaluate(`fetch('/api/result').then(r => r.json()).then(j => j.ended_early)`);
  check(sst.state === 'done' && /ended by the user after \d+ shapes/.test(ended ?? ''), `End verifies the best shape: ${sst.state} ${sst.error ?? ''} ${ended}`);
  check(sizedSamples > 0 && (ended ?? '').includes(`of ${sizedSamples} samples`), `the run was the phone-sized one (${sizedSamples} samples)`);
  const rate = await evaluate(`import('./pwa/store.js').then(s => s.getState('shape-rate'))`);
  check(rate?.s > 0, `seconds per shape measured: ${rate?.s?.toFixed(2)} over ${rate?.n}`);


  const t0 = Date.now();
  const example = await (await import('node:fs/promises')).readFile(join(repo, 'examples', 'canoe.toml'), 'utf8');
  check(await evaluate(`fetch('/api/run', { method: 'POST', body: ${JSON.stringify(example)} }).then(r => r.status)`) === 202, 'examples/canoe.toml run started');
  let st = {};
  for (let i = 0; i < 300; i++) { await sleep(500); st = await evaluate(`fetch('/api/status').then(r => r.json())`); if (st.state !== 'running') break; }
  check(st.state === 'done', `run finished: ${st.state} ${st.error ?? ''} in ${((Date.now() - t0) / 1000).toFixed(1)} s, ${st.done}/${st.total}`);
  const res = await evaluate(`fetch('/api/result').then(r => r.json()).then(j => j.winner && [j.winner.candidate.thickness_in, j.winner.candidate.density_pcf, j.winner.candidate.depth_in, j.winner.candidate.hull_weight_lb.toFixed(3)].join(' '))`);
  check(/^0\.\d+ \d+ \d+(\.\d+)? \d+\.\d{3}$/.test(res ?? ''), `winner (starter lab data): ${res}`);
  const exp = await evaluate(`fetch('/api/export?format=step').then(async r => r.headers.get('Content-Disposition') + ' ' + (await r.arrayBuffer()).byteLength)`);
  check(/off1-f4\.step" \d{6,}/.test(exp), `STEP export: ${exp}`);
  // Roster on the compute workers: the app answers while it assigns.
  await evaluate(`(async () => {
    const ev = await (await fetch('/api/roster')).json();
    const ppl = Array.from({ length: 8 }, (_, i) => ({ name: 'P' + i, gender: i < 4 ? 'M' : 'F', weight_lb: 140 + 6 * i, watts_1min: 280 + 12 * i, steer_stern: i % 4 }));
    window.__roster = { t0: performance.now() };
    fetch('/api/roster', { method: 'POST', body: JSON.stringify({ paddlers: ppl, events: ev.events }) })
      .then(async r => { window.__roster.status = r.status; window.__roster.n = (await r.json()).assignments?.length; window.__roster.s = (performance.now() - window.__roster.t0) / 1000; });
  })()`);
  await sleep(1500);
  const lag = await evaluate(`(async () => { const t = performance.now(); await fetch('/api/status'); return performance.now() - t; })()`);
  let ro = {};
  for (let i = 0; i < 600 && !ro.status; i++) { await sleep(500); ro = await evaluate('window.__roster'); }
  check(ro.status === 200 && ro.n > 0, `roster assigned: ${ro.n} events in ${ro.s?.toFixed(1)} s`);
  check(lag < 1000, `the app answers during the roster (${Math.round(lag)} ms)`);
  const runs = await evaluate(`fetch('/api/runs').then(r => r.json()).then(j => j.runs.length)`);
  check(runs === 2, `history: ${runs} runs`);
  const sw = await evaluate(`navigator.serviceWorker.ready.then(r => !!r.active)`);
  check(sw, 'service worker active');
  await sleep(1500);
  // Offline: stop the server, reload; the cached app must boot and show the saved result.
  server.close(); server.closeAllConnections?.();
  await send('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
  await send('Page.reload', { ignoreCache: false });
  ready = false;
  for (let i = 0; i < 100 && !ready; i++) { await sleep(300); ready = await evaluate(`!!document.querySelector('#run') && document.querySelector('#prefnote')?.textContent !== ''`).catch(() => false); }
  check(ready, 'offline reload booted from the cache');
  const again = await evaluate(`fetch('/api/result').then(r => r.status)`);
  check(again === 200, `offline: last result restored from IndexedDB (${again})`);
  const runs2 = await evaluate(`fetch('/api/runs').then(r => r.json()).then(j => j.runs.length)`);
  check(runs2 === 2, `offline: history kept (${runs2})`);
  // Phone width (iPhone 14): one column, nothing wider than the screen, Run on screen.
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await send('Page.reload', {});
  await sleep(3000);
  const phone = await evaluate(`({ w: document.documentElement.scrollWidth, run: document.getElementById('run').getBoundingClientRect().right })`);
  check(phone.w <= 390 && phone.run <= 390, `phone width: page ${phone.w} px, Run ends at ${Math.round(phone.run)} px`);
  // A finger swiping up on the canoe scrolls the page (only sideways swipes turn the canoe).
  await evaluate('window.scrollTo(0, 0)');
  const vc = await evaluate(`(() => { const r = document.getElementById('view').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
  await send('Emulation.setTouchEmulationEnabled', { enabled: true });
  const touch = (type, pts) => send('Input.dispatchTouchEvent', { type, touchPoints: pts });
  await touch('touchStart', [{ x: vc[0], y: vc[1] }]);
  for (let d = 15; d <= 300; d += 15) { await touch('touchMove', [{ x: vc[0], y: vc[1] - d }]); await sleep(16); }
  await touch('touchEnd', []);
  await sleep(800);
  const sy = await evaluate('scrollY');
  check(sy > 100, `a swipe up on the canoe scrolls the page (${Math.round(sy)} px)`);
  const fonts = await evaluate(`[...new Set([...document.querySelectorAll('#left label, #left input, #right .kv, #top button')].filter(e => e.offsetParent).map(e => getComputedStyle(e).fontSize))]`);
  check(fonts.length === 1, `one text size for labels, fields, results and the bar: ${fonts.join(', ')}`);
  const cube = await evaluate(`[getComputedStyle(document.querySelector('#viewcube canvas')).display, !!document.querySelector('#viewcube button')?.offsetParent]`);
  check(cube[0] === 'none' && cube[1], `no orientation cube on a phone, Home kept (${cube})`);
  // Landscape (Android phone on its side): the same one-column phone layout, no sideways scroll.
  await send('Emulation.setDeviceMetricsOverride', { width: 915, height: 412, deviceScaleFactor: 2.6, mobile: true, screenOrientation: { type: 'landscapePrimary', angle: 90 } });
  await sleep(1500);
  const land = await evaluate(`(() => { const v = document.getElementById('view').getBoundingClientRect(), r = document.getElementById('right').getBoundingClientRect();
    return { w: document.documentElement.scrollWidth, stacked: r.top >= v.bottom - 1, view: Math.round(v.height), run: document.getElementById('run').getBoundingClientRect().right }; })()`);
  await evaluate(`document.getElementById('seatbtn').click()`);
  await sleep(2500);
  const seat = await evaluate(`(() => { const v = document.getElementById('stview').getBoundingClientRect(), s = document.querySelector('#seating > section, #seating > aside:last-child').getBoundingClientRect();
    return { w: document.getElementById('seating').scrollWidth, cols: getComputedStyle(document.getElementById('seating')).display }; })()`);
  check(seat.w <= 915 && seat.cols === 'block', `landscape Seating: one column (${seat.cols}), ${seat.w} px`);
  await evaluate(`document.getElementById('seatbtn').click()`);
  check(land.w <= 915 && land.stacked && land.view >= 200 && land.run <= 915, `landscape: page ${land.w} px, one column (${land.stacked}), 3D view ${land.view} px tall`);
} catch (e) {
  console.log('FAIL', e.message);
  process.exitCode = 1;
} finally {
  const bad = logs.filter(l => /^(error|EXCEPTION)/.test(l));
  console.log(bad.length ? `console errors:\n  ${bad.slice(0, 10).join('\n  ')}` : 'no console errors');
  ws.close(); proc.kill(); server.close();
  setTimeout(() => process.exit(), 500);
}
