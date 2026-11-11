// JAGUAR phone app (docs/SPEC.md "Phone app"): loaded before web/index.html's own scripts by the
// PWA build only (web/pwa/build.mjs). It answers the page's fetch('/api/...') calls with the API
// worker (serve::Embedded in wasm), runs optimizations on compute workers, and adds what a phone
// needs: downloads through the share sheet, the screen kept awake during a run, a run cut off by
// the app closing offered again, project backup / restore, offline caching and updates.
// A classic script (not a module) so fetch is patched before the page's first request.
(() => {
  'use strict';
  const BASE = new URL('.', document.currentScript.src); // .../pwa/
  const ROOT = new URL('..', BASE); // the app's folder
  const WASM = new URL('jaguar.wasm', ROOT).href;
  const WORKERS = Math.max(2, Math.min(6, (navigator.hardwareConcurrency || 4) - 1));
  const CHECKPOINT = 'run';
  window.JAGUAR_PWA = true;

  // ---------- workers: request / answer by id ----------
  function rpcWorker(url) {
    const w = new Worker(url, { type: 'module' });
    const waiting = new Map();
    let next = 1;
    w.onmessage = ({ data }) => {
      if (data.progress !== undefined) return w.onprogress?.(data.progress);
      const p = waiting.get(data.id);
      if (!p) return;
      waiting.delete(data.id);
      data.ok ? p.resolve(data) : p.reject(new Error(data.error));
    };
    w.onerror = e => {
      for (const p of waiting.values()) p.reject(new Error(e.message || 'worker failed'));
      waiting.clear();
    };
    w.call = (op, args = {}, transfer = []) => new Promise((resolve, reject) => {
      const id = next++;
      waiting.set(id, { resolve, reject });
      w.postMessage({ id, op, ...args }, transfer);
    });
    w.stop = () => {
      w.terminate();
      for (const p of waiting.values()) p.reject(new Error('stopped'));
      waiting.clear();
    };
    return w;
  }

  const apiWorker = rpcWorker(new URL('api-worker.js', BASE));
  // `ready`: the engine is up (the shim's own calls wait for it). `booted`: a run cut off by the
  // app closing has also been resumed when asked (the page's requests wait for it, so its first
  // /api/status already sees that run).
  const ready = apiWorker.call('boot', { wasmUrl: WASM, starterUrl: new URL('starter', ROOT).href, workers: WORKERS })
    .catch(e => {
      fatal(`JAGUAR could not start: ${e.message}`);
      throw e;
    });
  const api = (op, args, transfer) => ready.then(() => apiWorker.call(op, args, transfer));
  const booted = ready.then(() => resumeIfAsked()).catch(e => console.warn('JAGUAR: resume failed', e));

  // ---------- fetch('/api/...') ----------
  const realFetch = window.fetch.bind(window);
  async function bytesOf(body) {
    if (body == null) return new ArrayBuffer(0);
    if (typeof body === 'string') return new TextEncoder().encode(body).buffer;
    if (body instanceof ArrayBuffer) return body.slice(0);
    if (ArrayBuffer.isView(body)) return body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength);
    if (body instanceof Blob) return body.arrayBuffer();
    if (body instanceof URLSearchParams) return new TextEncoder().encode(String(body)).buffer;
    throw new Error('JAGUAR phone app: unsupported request body');
  }
  window.fetch = async (input, init = {}) => {
    const req = input instanceof Request ? input : null;
    const url = new URL(req ? req.url : String(input), location.href);
    const at = url.pathname.indexOf('/api/');
    if (url.origin !== location.origin || at < 0) return realFetch(input, init);
    const path = url.pathname.slice(at) + url.search;
    const method = (init.method || req?.method || 'GET').toUpperCase();
    let body = await bytesOf(init.body ?? (req && method !== 'GET' ? await req.arrayBuffer() : null));
    await booted;
    if (method === 'POST' && path.split('?')[0] === '/api/run' && !run) {
      const text = await phoneSized(new TextDecoder().decode(body));
      if (text === null) {
        return new Response(JSON.stringify({ error: 'not run (cancelled)' }), { status: 409, headers: { 'Content-Type': 'application/json' } });
      }
      body = new TextEncoder().encode(text).buffer;
    }
    const r = await api('request', { method, url: path, body }, [body]);
    const headers = { 'Content-Type': r.head.content_type };
    if (r.head.filename) headers['Content-Disposition'] = `attachment; filename="${r.head.filename}"`;
    const w = (r.head.warnings || []).join(' | ').replace(/[^\x20-\x7e]/g, '?');
    if (w) headers['X-Jaguar-Warnings'] = w;
    const status = r.head.status;
    if (status === 202 && path.split('?')[0] === '/api/run') startRun().catch(runFailed);
    const what = /^\/api\/(pause|resume|stop)(\?|$)/.exec(path)?.[1];
    if (status === 200 && what) control(what);
    return new Response(status === 204 ? null : r.body, { status, headers });
  };

  // ---------- phone-sized shape search ----------
  // A shape search that would take well over BUDGET_S here is offered cut to about that long
  // (shapeopt::sized: same bounds and seed, fewer samples and refine rounds), or in full.
  // Seconds per shape per worker: measured on this device by earlier shape searches, else a
  // first guess: a laptop's browser measured 9.6 s per shape per worker on the team's starter
  // settings (6 workers), a phone is slower; too high a guess only offers the cut too readily.
  const BUDGET_S = 15 * 60;
  const GUESS_S = 20;
  const RATE = 'shape-rate';
  async function phoneSized(text) {
    const measured = await getState(RATE);
    const rate = measured?.s > 0 ? measured.s : GUESS_S;
    let info;
    try {
      info = await api('sizeRun', { toml: text, budget: Math.max(1, Math.floor(BUDGET_S * WORKERS / rate)) });
    } catch (e) {
      return text; // the server reports what is wrong with it
    }
    if (!info.shape_search || info.sized.planned >= info.planned) return text;
    const full = info.planned * rate / WORKERS, cut = info.sized.planned * rate / WORKERS;
    if (full <= 1.5 * BUDGET_S) return text;
    const n = x => x.toLocaleString('en-US');
    const how = measured?.s > 0 ? `about ${rate.toFixed(1)} s per shape on each of ${WORKERS} workers, measured here`
      : `${WORKERS} workers; a first guess of ${GUESS_S} s per shape until a shape search has run here`;
    const pick = await dialog('Phone-sized shape search?', [
      `This hull shape search plans about ${n(info.planned)} shapes: about ${dur(full)} on this device (${how}).`,
      `Phone-sized keeps your bounds and seed with ${n(info.sized.samples)} samples and ${info.sized.refine_rounds} refine rounds (canoe.toml: ${n(info.samples)} and ${info.refine_rounds}): about ${dur(cut)}. Fewer samples cover the shape space less finely; the desktop app runs the full search far faster.`,
    ], [['sized', `Phone-sized (about ${dur(cut)})`, 'primary'], ['full', `Full search (about ${dur(full)})`, 'soft'], ['cancel', 'Cancel', 'soft']]);
    return pick === 'sized' ? info.sized.toml : pick === 'full' ? text : null;
  }
  const dur = s => (s < 90 ? '1 min' : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${String(Math.round(s % 3600 / 60)).padStart(2, '0')} min`);

  /** Seconds per shape per worker, from one run's shape times (kept for the next estimate). */
  function noteRate(times) {
    if (times.length < 4) return;
    const s = times.reduce((a, b) => a + b, 0) / times.length / 1000;
    setState(RATE, { s, n: times.length, at: Date.now() });
  }

  // ---------- runs ----------
  let run = null;
  const files = new Map(); // runId -> files for the compute workers

  const runFailed = e => api('finish', { outcome: { state: 'error', error: `phone app: ${e.message}` } });

  async function startRun(resume) {
    const j = await api('takeJob');
    if (!j.job) return;
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    files.set(id, j.files);
    run = {
      id, toml: j.job, dir: j.dir, shape: j.shapeSearch, paused: false, stopped: false,
      evals: resume?.evals ?? [], forwarded: 0, workers: [], ended: false, gen: 0, gate: null, open: null, times: [],
    };
    await saveCheckpoint();
    wakeLock(true);
    if (run.shape) shapeSearch(run); else fixedHull(run);
  }

  async function compute() {
    const w = rpcWorker(new URL('compute-worker.js', BASE));
    await w.call('init', { wasmUrl: WASM });
    return w;
  }

  async function end(r, outcome) {
    if (r.ended) return;
    r.ended = true;
    for (const w of r.workers) w.stop();
    files.delete(r.id);
    if (run === r) run = null;
    noteRate(r.times);
    await api('finish', { outcome });
    await clearCheckpoint();
    wakeLock(false);
  }

  // A fixed hull is one engine call on one worker (~10 s on a laptop, a minute or so on a
  // phone). Pause ends that worker; Resume runs it again from the start, the progress the page
  // has already seen skipped (one thread: the events come in the same order every time).
  async function fixedHull(r) {
    const gen = ++r.gen;
    const w = await compute();
    if (r.ended || r.paused || gen !== r.gen) return w.stop();
    r.workers = [w];
    let seen = 0;
    w.onprogress = text => {
      if (++seen <= r.forwarded) return;
      r.forwarded = seen;
      api('progress', { event: JSON.parse(text) });
    };
    try {
      const { outcome } = await w.call('runFixed', { runId: r.id, files: files.get(r.id), toml: r.toml, dir: r.dir });
      await end(r, outcome);
    } catch (e) {
      if (r.ended || r.paused || r.workers[0] !== w) return;
      await end(r, { state: 'error', error: e.message });
    }
  }

  // A shape search: the replay (shape_step) on worker 0 asks for a batch of shapes, every
  // worker scores them, repeat until it returns the outcome. Each scored shape is checkpointed.
  async function shapeSearch(r) {
    try {
      r.workers = await Promise.all(Array.from({ length: WORKERS }, compute));
      if (r.ended) return r.workers.forEach(w => w.stop());
      const args = () => ({ runId: r.id, files: files.get(r.id), toml: r.toml, dir: r.dir });
      for (const e of r.evals) report(r, e, r.evals.length);
      for (;;) {
        const step = await r.workers[0].call('shapeStep', { ...args(), evals: r.evals, stopped: r.stopped });
        if (step.outcome) return end(r, step.outcome);
        const queue = step.need.slice();
        const total = r.evals.length + queue.length;
        api('progress', { event: { event: 'started', total } });
        await Promise.all(r.workers.map(async w => {
          for (;;) {
            while (r.paused && !r.stopped && !r.ended) await r.gate;
            if (r.stopped || r.ended || !queue.length) return;
            const job = queue.shift();
            const t = performance.now();
            const { eval: e } = await w.call('shapeScore', { ...args(), job });
            r.times.push(performance.now() - t);
            if (r.times.length % 10 === 0) noteRate(r.times);
            r.evals.push(e);
            report(r, e, total);
            saveShape(e);
          }
        }));
        if (r.ended) return;
      }
    } catch (e) {
      if (!r.ended) await end(r, { state: 'error', error: e.message });
    }
  }

  function report(r, e, total) {
    api('progress', { event: { shape: e, done: r.evals.length, total: Math.max(total, r.evals.length) } });
  }

  function control(what) {
    const r = run;
    if (!r) return;
    const release = () => { r.open?.(); r.gate = r.open = null; };
    if (what === 'pause') {
      r.paused = true;
      r.gate ??= new Promise(ok => (r.open = ok));
      if (!r.shape) { r.gen++; for (const w of r.workers) w.stop(); r.workers = []; }
    } else if (what === 'resume') {
      r.paused = false;
      release();
      if (!r.shape && !r.workers.length) fixedHull(r);
    } else if (what === 'stop') {
      r.stopped = true;
      release();
      if (!r.shape) end(r, { state: 'stopped' });
    }
  }

  // ---------- run cut off by the app closing ----------
  const store = import(new URL('store.js', BASE).href);
  const setState = (k, v) => store.then(s => (v == null ? s.delState(k) : s.setState(k, v))).catch(() => {});
  const getState = k => store.then(s => s.getState(k)).catch(() => null);
  // The run (`run`) and each scored shape on its own (`run-shape-<index>`): no rewriting the
  // whole list per shape.
  const SHAPE = `${CHECKPOINT}-shape-`;
  const saveCheckpoint = () => run && store.then(s => s.delStatesWith(SHAPE))
    .then(() => Promise.all(run.evals.map(saveShape)))
    .then(() => setState(CHECKPOINT, { toml: run.toml, shape: run.shape, at: Date.now() }));
  const saveShape = e => setState(SHAPE + String(e.job.index).padStart(8, '0'), e);
  const clearCheckpoint = () => setState(CHECKPOINT, null).then(() => store.then(s => s.delStatesWith(SHAPE))).catch(() => {});

  async function resumeIfAsked() {
    const cp = await getState(CHECKPOINT);
    if (!cp) return;
    cp.evals = cp.shape ? await store.then(s => s.statesWith(SHAPE)).catch(() => []) : [];
    if (sessionStorage.getItem('jaguar-resume') === '1') {
      sessionStorage.removeItem('jaguar-resume');
      const body = new TextEncoder().encode(cp.toml).buffer;
      const r = await apiWorker.call('request', { method: 'POST', url: '/api/run', body }, [body]);
      if (r.head.status === 202) await startRun({ evals: cp.evals });
      return;
    }
    const done = cp.shape ? ` (${cp.evals.length} hull shape${cp.evals.length === 1 ? '' : 's'} already done)` : '';
    banner(`A run was cut off when JAGUAR closed${done}.`, [
      ['Resume', () => { sessionStorage.setItem('jaguar-resume', '1'); location.reload(); }],
      ['Discard', clearCheckpoint],
    ]);
  }

  // ---------- the screen stays on while a run goes ----------
  let lock = null;
  async function wakeLock(on) {
    try {
      if (on && !lock && navigator.wakeLock && document.visibilityState === 'visible') {
        lock = await navigator.wakeLock.request('screen');
        lock.addEventListener('release', () => { lock = null; });
      } else if (!on && lock) {
        await lock.release();
        lock = null;
      }
    } catch (e) { lock = null; }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && run) wakeLock(true);
  });

  // ---------- downloads: the share sheet on a phone, else a download ----------
  const coarse = () => matchMedia('(pointer: coarse)').matches;
  async function saveFile(blob, name) {
    const file = new File([blob], name, { type: blob.type || 'application/octet-stream' });
    if (coarse() && navigator.canShare?.({ files: [file] })) {
      try { await navigator.share({ files: [file] }); return; } catch (e) { if (e.name === 'AbortError') return; }
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(file);
    a.download = name;
    origClick.call(a);
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }
  const nameFrom = (res, fallback) => /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '')?.[1] || fallback;
  async function download(href, fallback) {
    const res = await window.fetch(href);
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
      return alert(msg);
    }
    await saveFile(await res.blob(), nameFrom(res, fallback));
  }
  const origClick = HTMLAnchorElement.prototype.click;
  // The page's own downloads: blob links clicked from code, and /api/ links clicked by hand.
  HTMLAnchorElement.prototype.click = function () {
    if (this.hasAttribute('download') && this.href.startsWith('blob:') && coarse()) {
      const name = this.download || 'download';
      fetch(this.href).then(r => r.blob()).then(b => saveFile(b, name));
      return;
    }
    return origClick.call(this);
  };
  document.addEventListener('click', ev => {
    const a = ev.target.closest?.('a[href]');
    if (!a || ev.defaultPrevented) return;
    const url = new URL(a.href, location.href);
    if (url.origin === location.origin && url.pathname.includes('/api/')) {
      ev.preventDefault();
      download(url.href, 'download');
    } else if (a.hasAttribute('download') && url.protocol === 'blob:' && coarse()) {
      ev.preventDefault();
      HTMLAnchorElement.prototype.click.call(a);
    }
  }, true);

  // ---------- project backup / restore ----------
  const zip = () => import(new URL('zip.js', BASE).href);
  const PREFS_NAME = 'phone-settings.toml';
  async function saveProject() {
    const { files: list } = await api('exportFiles');
    const { writeZip } = await zip();
    const entries = list.map(f => ({
      name: f.path.startsWith('/config/') ? `JAGUAR/${PREFS_NAME}` : f.path.slice(1),
      data: f.data, date: new Date(Number(f.mtime / 1000000n)),
    }));
    const stamp = new Date().toISOString().slice(0, 10);
    await saveFile(new Blob([writeZip(entries)], { type: 'application/zip' }), `JAGUAR-${stamp}.zip`);
  }
  function openProject() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.zip,application/zip';
    input.onchange = async () => {
      const f = input.files?.[0];
      if (!f) return;
      try {
        const { readZip } = await zip();
        let entries = await readZip(new Uint8Array(await f.arrayBuffer()));
        const top = entries[0]?.name.split('/')[0];
        if (entries.length && entries.every(e => e.name.includes('/') && e.name.split('/')[0] === top)) {
          entries = entries.map(e => ({ ...e, name: e.name.slice(top.length + 1) }));
        }
        if (!entries.some(e => e.name === 'canoe.toml')) throw new Error('no canoe.toml in this zip');
        if (run) throw new Error('a run is going: open the project when it is done');
        if (!confirm(`Replace the canoe folder on this device with ${f.name} (${entries.length} files)?`)) return;
        const out = entries.map(e => ({
          path: e.name === PREFS_NAME ? '/config/prefs.toml' : `/JAGUAR/${e.name}`, data: e.data,
        }));
        await api('importFiles', { files: out });
        location.reload();
      } catch (e) {
        alert(`Could not open ${f.name}: ${e.message}`);
      }
    };
    input.click();
  }

  // ---------- page chrome: project buttons, notices ----------
  function banner(text, actions) {
    const b = document.createElement('div');
    b.className = 'pwa-banner';
    b.innerHTML = '<span></span>';
    b.firstChild.textContent = text;
    for (const [label, fn] of actions) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn soft';
      btn.textContent = label;
      btn.onclick = () => { b.remove(); fn(); };
      b.appendChild(btn);
    }
    const show = () => document.body.appendChild(b);
    document.body ? show() : addEventListener('DOMContentLoaded', show);
    return b;
  }
  /** A modal choice: resolves with the key of the button pressed ('cancel' on Escape). */
  function dialog(title, paragraphs, buttons) {
    return new Promise(resolve => {
      const veil = document.createElement('div');
      veil.className = 'pwa-veil';
      const card = document.createElement('div');
      card.className = 'pwa-dialog';
      card.setAttribute('role', 'dialog');
      card.setAttribute('aria-modal', 'true');
      const h = document.createElement('b');
      h.textContent = title;
      card.appendChild(h);
      for (const t of paragraphs) {
        const p = document.createElement('p');
        p.textContent = t;
        card.appendChild(p);
      }
      const row = document.createElement('div');
      row.className = 'pwa-row';
      const done = key => { veil.remove(); removeEventListener('keydown', esc, true); resolve(key); };
      const esc = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); done('cancel'); } };
      for (const [key, label, kind] of buttons) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = `btn ${kind}`;
        b.textContent = label;
        b.onclick = () => done(key);
        row.appendChild(b);
      }
      card.appendChild(row);
      veil.appendChild(card);
      document.body.appendChild(veil);
      addEventListener('keydown', esc, true);
      row.firstChild.focus();
    });
  }
  function fatal(text) {
    banner(text, [['Reload', () => location.reload()]]);
  }
  const css = `.pwa-banner{position:fixed;left:16px;right:16px;bottom:16px;margin:0 auto;z-index:1000;display:flex;gap:8px;align-items:center;
    max-width:520px;padding:8px 8px 8px 14px;background:var(--canvas,#fff);color:var(--ink,#141414);border:1px solid var(--hair,#e0e0e0);border-radius:var(--r-md,8px);font:inherit}
    .pwa-banner span{flex:1}
    .pwa-veil{position:fixed;inset:0;z-index:1001;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(20,20,20,.3)}
    .pwa-dialog{max-width:520px;width:100%;padding:16px;background:var(--canvas,#fff);color:var(--ink,#141414);border:1px solid var(--hair,#e0e0e0);border-radius:var(--r-md,8px)}
    .pwa-dialog p{margin:8px 0 0;color:var(--muted,#707070)}
    .pwa-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}`;
  addEventListener('DOMContentLoaded', () => {
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
    const desk = document.getElementById('deskbtns');
    if (desk) {
      const span = document.createElement('span');
      span.id = 'pwabtns';
      span.style.display = 'contents';
      span.innerHTML = '<div class="sep"></div>'
        + '<button type="button" class="btn soft" id="pwasave" title="Save the canoe folder (canoe.toml, hulls, lab data, results and run history) and your settings as one zip. Keep it somewhere safe: the browser may clear what JAGUAR keeps on this device.">Save project</button>'
        + '<button type="button" class="btn soft" id="pwaopen" title="Open a project zip saved by JAGUAR (or a zip of a desktop JAGUAR folder): it replaces the canoe folder on this device.">Open project</button>';
      desk.before(span);
      document.getElementById('pwasave').onclick = () => saveProject().catch(e => alert(e.message));
      document.getElementById('pwaopen').onclick = openProject;
    }
  });

  // ---------- offline: service worker, updates, storage ----------
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register(new URL('sw.js', ROOT).href).then(reg => {
      const offer = () => {
        if (!reg.waiting || !navigator.serviceWorker.controller) return;
        banner('A new version of JAGUAR is ready.', [['Reload', function update() {
          if (run) return banner('Reload when the run is done.', [['Reload', update]]);
          navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(), { once: true });
          reg.waiting.postMessage('skipWaiting');
        }]]);
      };
      offer();
      reg.addEventListener('updatefound', () => reg.installing?.addEventListener('statechange', offer));
    }).catch(() => { /* no offline copy; the app still works online */ });
  }
  navigator.storage?.persist?.().catch(() => {});
  addEventListener('beforeunload', ev => {
    if (run && !run.ended) { ev.preventDefault(); ev.returnValue = ''; }
  });
})();
