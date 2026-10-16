#!/usr/bin/env node
// Step 1 of the promo: run JAGUAR and keep its raw output in promo/data/.
// Every number, curve and hull in the video comes from the files this script writes.
//
//   node scripts/export-data.mjs            # build + every run (about 10 min on 2 cores)
//   node scripts/export-data.mjs --no-build # reuse target/release/jaguar
//   node scripts/export-data.mjs --only=search-path,mix
//
// Steps: off1, zones, shape-opt, shape-race, race, mix, search-path. See promo/data/README.md.
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const data = path.join(repo, 'promo/data');
const J = path.join(repo, 'target/release/jaguar');
const args = process.argv.slice(2);
const only = (args.find((a) => a.startsWith('--only=')) || '').slice(7).split(',').filter(Boolean);
const want = (s) => !only.length || only.includes(s);
const rel = (p) => path.relative(repo, p);

fs.mkdirSync(data, { recursive: true });

function run(argv, outTxt) {
  // Terminal output exactly as JAGUAR prints it (stdout + stderr), exit code kept.
  console.log(`$ jaguar ${argv.join(' ')}`);
  let out = '';
  let code = 0;
  try {
    out = execFileSync(J, argv, { cwd: repo, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 1 << 28 });
  } catch (e) {
    out = (e.stdout || '') + (e.stderr || '');
    code = e.status ?? 1;
  }
  if (outTxt) fs.writeFileSync(outTxt, out);
  if (code === 1) throw new Error(`jaguar ${argv.join(' ')} failed (exit 1):\n${out}`);
  return { out, code };
}

if (!args.includes('--no-build')) {
  console.log('$ cargo build --release');
  execFileSync('cargo', ['build', '--release'], { cwd: repo, stdio: 'inherit' });
}

if (want('off1')) {
  run(['run', 'examples/canoe.toml', '-o', rel(path.join(data, 'off1_result.json'))], path.join(data, 'terminal_run_canoe.txt'));
}

if (want('zones')) {
  // examples/canoe.toml with its documented [zones] block switched on (docs/HULLS.md row 2).
  const src = fs.readFileSync(path.join(repo, 'examples/canoe.toml'), 'utf8');
  const toml = src
    .replace(/^file = "off1_hull.json"/m, 'file = "../../examples/off1_hull.json"')
    .replace(/^mix_data = "mix_tests.csv"/m, 'mix_data = "../../examples/mix_tests.csv"')
    .replace(/^# \[zones\]/m, '[zones]')
    .replace(/^# dense_pcf = /m, 'dense_pcf = ')
    .replace(/^# increment_in = /m, 'increment_in = ')
    .replace(/^# max_checks = /m, 'max_checks = ');
  fs.writeFileSync(path.join(data, 'canoe_zones.toml'), toml);
  run(['run', rel(path.join(data, 'canoe_zones.toml')), '-o', rel(path.join(data, 'off1_zones_result.json'))], path.join(data, 'terminal_run_canoe_zones.txt'));
}

if (want('shape-opt')) {
  run(['run', 'examples/shape_opt.toml', '-o', rel(path.join(data, 'shape_opt_result.json'))], path.join(data, 'terminal_run_shape_opt.txt'));
}

if (want('winner-walls')) {
  // The shape-search winner's own wall grid: examples/shape_opt.toml without [shape_search],
  // hull.file = the winner hull (its result.json winner_hull, stem arcs reapplied by the run).
  const r = JSON.parse(fs.readFileSync(path.join(data, 'shape_opt_result.json'), 'utf8'));
  const h = { ...r.winner_hull };
  delete h.stem;
  fs.writeFileSync(path.join(data, 'shape_opt_winner_hull.json'), JSON.stringify(h));
  let t = fs.readFileSync(path.join(repo, 'examples/shape_opt.toml'), 'utf8');
  t = t.slice(0, t.search(/^# Optimize the hull SHAPE/m))
    .replace('# no hull.file: the hull shape is optimized ([shape_search] below)', 'file = "shape_opt_winner_hull.json"')
    .replace('mix_data = "mix_tests.csv"', 'mix_data = "../../examples/mix_tests.csv"');
  fs.writeFileSync(path.join(data, 'shape_opt_winner_walls.toml'), t);
  run(['run', rel(path.join(data, 'shape_opt_winner_walls.toml')), '-o', rel(path.join(data, 'shape_opt_winner_walls_result.json'))], path.join(data, 'terminal_run_shape_opt_winner_walls.txt'));
}

if (want('shape-race')) {
  run(['run', 'examples/shape_race.toml', '-o', rel(path.join(data, 'shape_race_result.json'))], path.join(data, 'terminal_run_shape_race.txt'));
}

if (want('race')) {
  // Race-time winner on its own re-planned paths, plus the off1 and weight-pick winners for comparison.
  run(['race', rel(path.join(data, 'shape_race_result.json')), '-o', rel(path.join(data, 'race_shape_race.json'))], path.join(data, 'terminal_race_shape_race.txt'));
  run(['race', rel(path.join(data, 'off1_result.json')), '-o', rel(path.join(data, 'race_off1.json'))], path.join(data, 'terminal_race_off1.txt'));
  run(['race', rel(path.join(data, 'shape_opt_result.json')), '--params', 'examples/shape_race.toml', '-o', rel(path.join(data, 'race_shape_opt.json'))], path.join(data, 'terminal_race_shape_opt.txt'));
}

if (want('mix')) {
  run(['mix', 'data/mix_tests.csv'], path.join(data, 'terminal_mix.txt'));
  // The fit as JSON: `jaguar serve` GET /api/mix (mix::StrengthFit) on a copy of canoe.toml whose
  // material.mix_data is data/mix_tests.csv. Temp files only; nothing is written to the repo.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jaguar-mix-'));
  const toml = fs
    .readFileSync(path.join(repo, 'examples/canoe.toml'), 'utf8')
    .replace(/^file = "off1_hull.json"/m, `file = "${path.join(repo, 'examples/off1_hull.json')}"`)
    .replace(/^mix_data = "mix_tests.csv"/m, `mix_data = "${path.join(repo, 'data/mix_tests.csv')}"`);
  fs.writeFileSync(path.join(tmp, 'canoe.toml'), toml);
  const port = 18000 + Math.floor(Math.random() * 1000);
  const srv = spawn(J, ['serve', path.join(tmp, 'canoe.toml'), '-p', String(port), '--prefs', path.join(tmp, 'prefs.toml')], { stdio: 'ignore' });
  try {
    let fit = null;
    for (let i = 0; i < 50 && !fit; i++) {
      await new Promise((r) => setTimeout(r, 200));
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/mix`);
        if (res.ok) fit = await res.json();
      } catch {}
    }
    if (!fit) throw new Error('jaguar serve did not answer GET /api/mix');
    delete fit.csv; // the raw CSV text is data/mix_tests.csv itself
    fs.writeFileSync(path.join(data, 'mix_fit.json'), JSON.stringify(fit, null, 1));
    console.log('$ jaguar serve ... ; GET /api/mix -> promo/data/mix_fit.json');
  } finally {
    srv.kill();
  }
}

if (want('search-path')) {
  // The shape search's own path: result.json `candidates` has one entry per shape in evaluation
  // order (Latin hypercube first, then pattern-search rounds; docs/SHAPE_SEARCH.md). Each shape's
  // hull is regenerated with `jaguar gen-hull` (41 x 21, the search's grid) from its exact factors.
  const r = JSON.parse(fs.readFileSync(path.join(data, 'shape_opt_result.json'), 'utf8'));
  const samples = r.params.shape_search.samples;
  const cands = r.candidates;
  const FIELDS = ['length_in', 'beam_in', 'draft_in', 'fore_wp', 'section', 'fore_rocker', 'aft_wp', 'aft_rocker', 'fore_len_frac', 'aft_len_frac'];
  // Best passing shape so far (the incumbent), every time it improves.
  const incumbents = [];
  let best = Infinity;
  cands.forEach((c, i) => {
    if (c.passes && c.hull_weight_lb < best) {
      best = c.hull_weight_lb;
      incumbents.push(i);
    }
  });
  // A spread of the Latin-hypercube samples (farthest-point over normalized length, beam, draft,
  // fore rocker), starting at sample 0: the variety the search starts from.
  const ss = r.params.shape_search;
  const norm = (c) => ['length_in', 'beam_in', 'draft_in', 'fore_rocker'].map((k) => (c.shape[k] - ss[k][0]) / (ss[k][1] - ss[k][0] || 1));
  const lhs = cands.slice(0, samples).map((c, i) => ({ i, v: norm(c) }));
  const spread = [0];
  while (spread.length < 6) {
    let far = null;
    for (const p of lhs) {
      if (spread.includes(p.i)) continue;
      const d = Math.min(...spread.map((j) => Math.hypot(...p.v.map((x, k) => x - lhs[j].v[k]))));
      if (!far || d > far.d) far = { i: p.i, d };
    }
    spread.push(far.i);
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jaguar-path-'));
  const gen = (i) => {
    const s = cands[i].shape;
    const toml = FIELDS.map((k) => `${k} = ${s[k]}`).join('\n') + (s.gunwale_radius_in ? `\ngunwale_radius_in = ${s.gunwale_radius_in}` : '') + '\n';
    const t = path.join(tmp, `opt-${i}.toml`);
    fs.writeFileSync(t, toml);
    const out = path.join(tmp, `opt-${i}.json`);
    execFileSync(J, ['gen-hull', t, '-o', out, '--stations', '41', '--waterlines', '21', '--name', `opt-${i}`], { cwd: repo });
    const c = cands[i];
    return {
      index: i,
      phase: i < samples ? 'latin hypercube' : 'pattern search',
      shape: s,
      passes: c.passes,
      hull_weight_lb: c.hull_weight_lb,
      thickness_in: c.thickness_in,
      density_pcf: c.density_pcf,
      depth_in: c.depth_in,
      flare_deg: c.flare_deg,
      dc: c.verdict ? c.verdict.dc : null,
      command: `jaguar gen-hull opt-${i}.toml --stations 41 --waterlines 21`,
      hull: JSON.parse(fs.readFileSync(out, 'utf8')),
    };
  };
  const pack = {
    source: 'promo/data/shape_opt_result.json candidates (evaluation order); hulls from jaguar gen-hull',
    shapes_evaluated: cands.length,
    latin_hypercube_samples: samples,
    incumbents: incumbents.map(gen),
    spread: spread.map(gen),
  };
  // Check: the regenerated last incumbent is the result's winner_hull table (stem arcs aside).
  const last = pack.incumbents[pack.incumbents.length - 1].hull;
  const wh = r.winner_hull;
  const same = JSON.stringify([last.x, last.z, last.half_breadth]) === JSON.stringify([wh.x, wh.z, wh.half_breadth]);
  pack.last_incumbent_matches_winner_hull = same;
  if (!same) throw new Error('regenerated winner hull differs from result.json winner_hull');
  fs.writeFileSync(path.join(data, 'search_path_hulls.json'), JSON.stringify(pack));
  console.log(`search path: ${incumbents.length} incumbents, ${spread.length} spread samples -> promo/data/search_path_hulls.json`);
}
