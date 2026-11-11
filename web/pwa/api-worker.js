// The phone app's server: one engine instance running `serve::Embedded` (jaguar-wasm `api_*`)
// on the canoe folder, kept in IndexedDB. The page (shim.js) talks to it by messages:
// { id, op, ... } -> { id, ok, ...answer } | { id, ok: false, error }.

import { Engine, EngineCrash, compileUrl } from './engine.js';
import { MemFS } from './wasi.js';
import * as store from './store.js';

export const DIR = '/JAGUAR';
const CANOE = DIR + '/canoe.toml';
const PREFS = '/config/prefs.toml';
// The desktop app's first-launch files (launch.rs): never written over an existing file.
const STARTER = ['canoe.toml', 'off1_hull.json', 'mix_tests.csv'];

let module, fs, api, workers;

async function start() {
  api = await Engine.create(module, { fs, log: t => console.debug('[jaguar]', t.trimEnd()) });
  api.json('api_init', { params_path: CANOE, prefs_path: PREFS, workers });
}

async function persist() {
  await store.applyChanges(fs.takeChanges().filter(c => !/\.tmp[^/]*$/.test(c.path)));
}

async function boot({ wasmUrl, starterUrl, workers: n }) {
  workers = n;
  module = await compileUrl(wasmUrl);
  fs = new MemFS();
  fs.mkdirs(DIR);
  fs.mkdirs('/config');
  fs.load(await store.allFiles());
  fs.takeChanges();
  for (const name of STARTER) {
    if (fs.read(`${DIR}/${name}`)) continue;
    const r = await fetch(`${starterUrl}/${name}`);
    if (!r.ok) throw new Error(`starter ${name}: HTTP ${r.status}`);
    fs.write(`${DIR}/${name}`, new Uint8Array(await r.arrayBuffer()));
  }
  await persist();
  await start();
}

/** A copy of the canoe folder for a compute worker (no history: a run never reads it). */
const runFiles = () => fs.snapshot(DIR, p => p.startsWith(DIR + '/runs/') || p === DIR + '/result.json');

const ops = {
  boot,
  async request({ method, url, body }) {
    let r;
    try {
      r = api.call('api_request', { method, url }, new Uint8Array(body ?? 0));
    } catch (e) {
      if (!(e instanceof EngineCrash)) throw e;
      await persist();
      await start();
      const msg = new TextEncoder().encode(JSON.stringify({ error: `JAGUAR's engine stopped: ${e.message}` }));
      return { head: { status: 500, content_type: 'application/json', filename: null, warnings: [] }, body: msg.buffer };
    }
    await persist();
    const body2 = r.body.slice();
    return { head: r.head, body: body2.buffer };
  },
  takeJob() {
    const j = api.json('api_take_job', {});
    return j.job ? { job: j.job, shapeSearch: j.shape_search, files: runFiles(), dir: DIR } : { job: null };
  },
  progress({ event }) { api.json('api_progress', event); },
  async finish({ outcome }) { api.json('api_finish', outcome); await persist(); },
  control() { return api.json('api_control', {}); },
  /** A run's shape search and its phone-sized cut (jaguar-wasm `shape_size`). */
  sizeRun({ toml, budget }) { return api.json('shape_size', { toml, base_dir: DIR, budget }); },
  /** The canoe folder and prefs, for "Download project". */
  exportFiles() { return { files: fs.snapshot(DIR).concat(fs.snapshot('/config')) }; },
  /** Replace the canoe folder (and prefs when given) with `files`; restart the server. */
  async importFiles({ files }) {
    for (const p of fs.list(DIR)) fs.remove(p);
    if (files.some(f => f.path.startsWith('/config/'))) for (const p of fs.list('/config')) fs.remove(p);
    fs.load(files);
    await persist();
    await start();
  },
};

self.onmessage = async ({ data }) => {
  const { id, op, ...args } = data;
  try {
    const out = await ops[op](args);
    const transfer = out?.body instanceof ArrayBuffer ? [out.body] : [];
    self.postMessage({ id, ok: true, ...out }, transfer);
  } catch (e) {
    self.postMessage({ id, ok: false, error: e?.message ?? String(e) });
  }
};
