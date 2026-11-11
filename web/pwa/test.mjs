// Phone-app engine tests (`make wasm-parity`): jaguar-wasm on the in-memory WASI file system,
// in Node, against the native build.
//   node web/pwa/test.mjs <jaguar_wasm.wasm> <native jaguar binary>
// 1. Parity: fixed-hull examples run in wasm give the native result.json: same structure and
//    strings (file paths by name), every number within 1e-9 relative (docs/SPEC.md "Phone app":
//    wasm brings its own libm, so the last bits of sin/exp/pow may differ).
// 2. A small shape search stepped over two engines (as the phone's workers do) gives the native
//    result.json by the same rule.
// 3. The API: prefs, a run handed to the host, its result kept, exports, project zip round trip.

import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Engine } from './engine.js';
import { MemFS } from './wasi.js';
import { readZip, writeZip } from './zip.js';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..');
const [wasmPath, nativeArg] = process.argv.slice(2);
const native = process.platform === 'win32' && nativeArg && !/\.exe$/i.test(nativeArg) ? `${nativeArg}.exe` : nativeArg;
if (!wasmPath || !native) {
  console.error('usage: node web/pwa/test.mjs <jaguar_wasm.wasm> <native jaguar>');
  process.exit(2);
}
const module = new WebAssembly.Module(readFileSync(wasmPath));
const DIR = '/JAGUAR';
let failed = 0;
const test = async (name, fn) => {
  const t = performance.now();
  try {
    await fn();
    console.log(`ok   ${name} (${((performance.now() - t) / 1000).toFixed(1)} s)`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}\n     ${e.stack ?? e}`);
  }
};
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

/** A MemFS holding examples/ as the canoe folder (plus `extra` files). */
function canoeFolder(extra = {}) {
  const fs = new MemFS();
  for (const f of readdirSync(join(repo, 'examples'))) {
    if (/\.(toml|json|csv)$/.test(f) && !/result\.json$/.test(f)) fs.write(`${DIR}/${f}`, readFileSync(join(repo, 'examples', f)));
  }
  for (const [name, text] of Object.entries(extra)) fs.write(`${DIR}/${name}`, text);
  return fs;
}

/** Native `jaguar run` of `toml` (text) in a copy of examples/: the result.json value. */
function nativeRun(toml) {
  const dir = mkdtempSync(join(tmpdir(), 'jaguar-parity-'));
  for (const f of readdirSync(join(repo, 'examples'))) {
    if (/\.(json|csv)$/.test(f) && !/result\.json$/.test(f)) writeFileSync(join(dir, f), readFileSync(join(repo, 'examples', f)));
  }
  writeFileSync(join(dir, 'canoe.toml'), toml);
  try {
    execFileSync(native, ['run', join(dir, 'canoe.toml'), '-o', join(dir, 'out.json')], { stdio: 'pipe' });
  } catch (e) {
    // Exit 2 = no verified design: a result all the same.
    if (e.status !== 2) throw new Error(`native run: exit ${e.status}: ${e.stderr}`);
  }
  return JSON.parse(readFileSync(join(dir, 'out.json'), 'utf8'));
}

/** Every difference between two result.json values beyond the parity rule. `length`: the hull
 * length of the candidate being walked: on a symmetric hull a governing value ties at x and at
 * length - x (equal to the last bits), and which one is reported may flip with them. */
function differences(a, b, path = '', out = [], length = null) {
  if (out.length > 10) return out;
  if (typeof a === 'number' && typeof b === 'number') {
    const close = (p, q) => Math.abs(p - q) <= 1e-9 * Math.max(Math.abs(p), Math.abs(q), 1e-300);
    const position = /\.x_(governing_)?in$/.test(path);
    if (!close(a, b) && !(position && length !== null && Math.abs(a + b - length) < 1e-6)) out.push(`${path}: ${a} vs ${b}`);
  } else if (typeof a === 'string' && typeof b === 'string') {
    const file = s => (/\.(json|csv|toml|png)$/i.test(s) ? basename(s.replaceAll('\\', '/')) : s);
    if (file(a) !== file(b)) out.push(`${path}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);
  } else if (a && b && typeof a === 'object' && typeof b === 'object' && Array.isArray(a) === Array.isArray(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    const len = typeof a.length_in === 'number' ? a.length_in : length;
    for (const k of keys) differences(a[k], b[k], `${path}.${k}`, out, len);
  } else if (a !== b) out.push(`${path}: ${JSON.stringify(a)?.slice(0, 80)} vs ${JSON.stringify(b)?.slice(0, 80)}`);
  return out;
}

const decode = b => new TextDecoder().decode(b);
const resultOf = outcome => {
  assert(outcome.state === 'done', `run ${outcome.state}: ${outcome.error ?? ''}`);
  return JSON.parse(outcome.record.pretty);
};

for (const name of ['canoe.toml', 'canoe_mixes.toml', 'sleek.toml']) {
  await test(`parity: ${name} (fixed hull) wasm = native`, async () => {
    const toml = readFileSync(join(repo, 'examples', name), 'utf8');
    const e = await Engine.create(module, { fs: canoeFolder() });
    const got = resultOf(e.json('run_fixed', { toml, base_dir: DIR }));
    const d = differences(got, nativeRun(toml));
    assert(!d.length, d.join('\n     '));
  });
}

/** examples/shape_opt.toml cut down (as tests/shapeopt.rs `small(true)`). */
const smallShape = readFileSync(join(repo, 'examples', 'shape_opt.toml'), 'utf8')
  .replace(/^stations = \d+/m, 'stations = 61')
  .replace(/^thickness_in = .*$/m, 'thickness_in = [0.375, 0.5, 0.125]')
  .replace(/^density_pcf {2}= .*$/m, 'density_pcf  = [51.0, 60.0, 3.0]')
  .replace(/^depth_in {5}= .*$/m, 'depth_in     = [11.0, 15.0, 2.0]')
  .replace(/^beam_in {7}= .*$/m, 'beam_in       = [24.0, 24.0]')
  .replace(/^section {7}= .*$/m, 'section       = [0.5, 0.5]')
  .replace(/^samples {7}= \d+/m, 'samples       = 8')
  .replace(/^refine_rounds = \d+/m, 'refine_rounds = 1');

await test('parity: small shape search stepped over two engines = native', async () => {
  assert(smallShape.includes('samples       = 8') && smallShape.includes('stations = 61'), 'shape_opt.toml changed: fix smallShape');
  const engines = [await Engine.create(module, { fs: canoeFolder() }), await Engine.create(module, { fs: canoeFolder() })];
  const evals = [];
  let outcome;
  for (let steps = 0; !outcome; steps++) {
    assert(steps < 20, 'too many steps');
    const s = engines[0].json('shape_step', { toml: smallShape, base_dir: DIR, evals, stopped: false });
    if (s.outcome) { outcome = s.outcome; break; }
    s.need.forEach((job, i) => evals.push(engines[i % 2].json('shape_score', { toml: smallShape, base_dir: DIR, job })));
  }
  const d = differences(resultOf(outcome), nativeRun(smallShape));
  assert(!d.length, d.join('\n     '));
});

await test('api: prefs, host run, result kept, exports, history', async () => {
  const fs = canoeFolder();
  const api = await Engine.create(module, { fs });
  api.json('api_init', { params_path: `${DIR}/canoe.toml`, prefs_path: '/config/prefs.toml', workers: 2 });
  const req = (method, url, body = '') => {
    const r = api.call('api_request', { method, url }, body);
    return { status: r.head.status, head: r.head, body: r.body, json: () => JSON.parse(decode(r.body)) };
  };
  const toml = fs.readText(`${DIR}/canoe.toml`);
  assert(req('GET', '/api/params').json().source === 'file', 'params from canoe.toml');
  assert(req('POST', '/api/prefs', 'bogus = 1').status === 400, 'bad prefs refused');
  fs.takeChanges();
  assert(req('POST', '/api/prefs', toml).status === 200, 'prefs saved');
  assert(fs.takeChanges().some(c => c.path === '/config/prefs.toml' && !c.removed), 'prefs change journaled');
  assert(req('GET', '/api/params').json().source === 'prefs', 'params from prefs');
  assert(req('GET', '/api/result').status === 404, 'no result yet');
  assert(req('POST', '/api/run', toml).status === 202, 'run accepted');
  const job = api.json('api_take_job', {});
  assert(job.job && job.shape_search === false, 'job taken');
  const comp = await Engine.create(module, {
    fs: (() => { const f = new MemFS(); f.load(fs.snapshot(DIR)); return f; })(),
    onProgress: t => api.json('api_progress', JSON.parse(t)),
  });
  const outcome = comp.json('run_fixed', { toml: job.job, base_dir: DIR });
  const st = req('GET', '/api/status?since=0').json();
  assert(st.state === 'running' && st.done > 0 && st.candidates.length === st.done, `progress: ${st.done}`);
  api.json('api_finish', outcome);
  assert(req('GET', '/api/status').json().state === 'done', 'done');
  const shown = req('GET', '/api/result');
  assert(shown.status === 200 && decode(shown.body) === fs.readText(`${DIR}/result.json`), 'result.json is the shown result');
  const runs = req('GET', '/api/runs').json();
  assert(runs.runs.length === 1 && runs.current === runs.runs[0].file, 'run kept in the history');
  const step = req('GET', '/api/export?format=step');
  assert(step.status === 200 && step.head.filename.endsWith('.step') && decode(step.body.subarray(0, 13)) === 'ISO-10303-21;', 'STEP export');
  const stl = req('GET', '/api/export?format=stl&shell=mold');
  assert(stl.status === 200 && stl.head.filename.endsWith('_mold.stl') && stl.body.length > 84, 'STL export');
  assert(req('GET', '/api/seating').status === 200, 'seating');
  const mold = req('POST', '/api/manufacture', '{}');
  assert(mold.status === 200 && mold.json().dxf.length > 0, 'mold DXF');
  assert(req('GET', '/api/session').json().desktop === false, 'not the desktop app');
  assert(req('POST', '/api/quit').status === 403, 'no quit');
});

await test('zip: project round trip (stored, and deflated by another tool)', async () => {
  const entries = [{ name: 'JAGUAR/canoe.toml', data: new TextEncoder().encode('[hull]\n') },
    { name: 'JAGUAR/runs/a.json', data: new Uint8Array(1000).map((_, i) => i % 7) }];
  const back = await readZip(writeZip(entries));
  assert(back.length === 2 && back.every((e, i) => e.name === entries[i].name && e.data.join() === entries[i].data.join()), 'stored');
  // A deflated entry, as Windows / macOS zip a folder.
  const { deflateRawSync } = await import('node:zlib');
  const data = new TextEncoder().encode('x'.repeat(5000));
  const z = writeZip([{ name: 'a.txt', data }]);
  const comp = deflateRawSync(data);
  const head = z.subarray(0, 30 + 5), tail = z.subarray(30 + 5 + data.length);
  const v = new DataView(head.buffer.slice(head.byteOffset, head.byteOffset + head.length));
  v.setUint16(8, 8, true); v.setUint32(18, comp.length, true);
  const cen = new DataView(tail.buffer.slice(tail.byteOffset, tail.byteOffset + tail.length));
  cen.setUint16(10, 8, true); cen.setUint32(20, comp.length, true);
  const endAt = cen.byteLength - 22;
  cen.setUint32(endAt + 16, 30 + 5 + comp.length, true);
  const deflated = new Uint8Array([...new Uint8Array(v.buffer), ...comp, ...new Uint8Array(cen.buffer)]);
  const got = await readZip(deflated);
  assert(got.length === 1 && decode(got[0].data) === 'x'.repeat(5000), 'deflated');
  let threw = false;
  try { await readZip(new Uint8Array(10)); } catch (e) { threw = true; }
  assert(threw, 'not a zip');
});

if (failed) {
  console.log(`${failed} failed`);
  process.exit(1);
}
console.log('all passed');
