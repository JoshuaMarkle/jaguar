#!/usr/bin/env node
// Lifts the data embedded in promo/data/search_space_report.html (the JAGUAR explorer's "Canoe
// Search Space" report: jaguar serve -> /search, its own settings) into
// src/generated/report-data.json for the analysis montage. Selection, subsampling and one
// derived statistic (median race time of valid shapes per tenth of each parameter's range, the
// report's own trend line) only.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const promo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(promo, 'data/search_space_report.html'), 'utf8');
const m = html.match(/<script[^>]*id="data"[^>]*>([\s\S]*?)<\/script>/);
if (!m) throw new Error('no <script id="data"> in the report');
const D = JSON.parse(m[1]);
const r3 = (v) => (typeof v === 'number' ? Math.round(v * 1000) / 1000 : v);
const N = D.names;
const L = D.lhs;
const n = L.race.length;

// Every k-th of the 3000 Latin-hypercube shapes (deterministic subsample for SVG).
const every = (k) => Array.from({ length: Math.ceil(n / k) }, (_, i) => i * k).filter((i) => L.race[i] != null);
const sub = every(2);

const trends = N.map((name, p) => {
  const lo = D.lo[p];
  const hi = D.hi[p];
  const med = [];
  for (let b = 0; b < 10; b++) {
    const a = lo + ((hi - lo) * b) / 10;
    const z = lo + ((hi - lo) * (b + 1)) / 10;
    const ys = [];
    for (let i = 0; i < n; i++) if (L.valid[i] && L.race[i] != null && L.X[i][p] >= a && L.X[i][p] < z) ys.push(L.race[i]);
    ys.sort((u, v) => u - v);
    if (ys.length >= 3) med.push([r3((a + z) / 2), r3(ys[Math.floor(ys.length / 2)])]);
  }
  return { name, lo, hi, median: med };
});

// The report's default terrain: bow fineness (fore_wp) x taper (fore_len_frac).
const g = D.grids.find((q) => q.dims[0] === 3 && q.dims[1] === 8);
const out = {
  source: 'promo/data/search_space_report.html (JAGUAR explorer report, its own saved settings)',
  names: N,
  free: D.free,
  counts: D.counts,
  lo: D.lo,
  hi: D.hi,
  // [x per param..., race, valid, gm] for the subsample.
  shapes: sub.map((i) => [...L.X[i].map(r3), r3(L.race[i]), L.valid[i], r3(L.gm[i])]),
  trends,
  sens: D.sens.map((s) => ({ name: s.name, drop: r3(s.race_all_drop) })),
  topk: D.topk.race_all.map(([name, share]) => ({ name, share: r3(+share) })),
  gmsweep: D.gmsweep.map((s) => ({ gate: s.gm_min, race: r3(s.race_replanned ?? s.race), validFrac: r3(s.valid_frac) })),
  catcount: D.catcount.map(([cat, c]) => ({ cat, count: +c })),
  slices: { steps: D.slices.steps, v: D.slices.v.map((row) => row.map(r3)), race: D.slices.race.map(r3), valid: D.slices.valid, center: D.slices.center },
  terrain: { dims: g.dims, steps: g.steps, race: g.race.map(r3), valid: g.valid, v: g.v.map((p) => p.map(r3)) },
};
const dst = path.join(promo, 'src/generated/report-data.json');
fs.writeFileSync(dst, JSON.stringify(out));
console.log(`wrote src/generated/report-data.json (${(fs.statSync(dst).size / 1024).toFixed(0)} KB)`);
