// A compute worker of the phone app: runs (jaguar-wasm `run_fixed`, `shape_step`, `shape_score`)
// on its own engine and its own copy of the canoe folder. One engine per run (`runId`), so the
// shape search's prep is worked out once per worker.
// { id, op, ... } -> { id, ok, ...answer } | { id, ok: false, error }; progress of a fixed hull
// run as { progress: <Progress JSON text> }.

import { Engine, compileUrl } from './engine.js';
import { MemFS } from './wasi.js';

let module = null, engine = null, runId = null;

async function engineFor(id, files) {
  if (engine && runId === id && !engine.dead) return engine;
  const fs = new MemFS();
  fs.load(files);
  engine = await Engine.create(module, {
    fs,
    onProgress: text => self.postMessage({ progress: text }),
    log: t => console.debug('[jaguar compute]', t.trimEnd()),
  });
  runId = id;
  return engine;
}

const ops = {
  async init({ wasmUrl }) {
    module = await compileUrl(wasmUrl);
  },
  async runFixed({ runId: id, files, toml, dir }) {
    const e = await engineFor(id, files);
    return { outcome: e.json('run_fixed', { toml, base_dir: dir }) };
  },
  async shapeStep({ runId: id, files, toml, dir, evals, stopped }) {
    const e = await engineFor(id, files);
    return e.json('shape_step', { toml, base_dir: dir, evals, stopped });
  },
  async shapeScore({ runId: id, files, toml, dir, job }) {
    const e = await engineFor(id, files);
    return { eval: e.json('shape_score', { toml, base_dir: dir, job }) };
  },
};

self.onmessage = async ({ data }) => {
  const { id, op, ...args } = data;
  try {
    self.postMessage({ id, ok: true, ...(await ops[op](args)) });
  } catch (e) {
    self.postMessage({ id, ok: false, error: e?.message ?? String(e) });
  }
};
