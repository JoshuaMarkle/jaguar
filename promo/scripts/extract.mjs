#!/usr/bin/env node
// Compacts promo/data (raw JAGUAR output) into src/generated/promo-data.json, the only data file
// the video imports. Selection and renaming only: no value is computed here except the
// "best so far" filter of the shape search, which is a running minimum of JAGUAR's own weights.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const promo = path.resolve(here, '..');
const repo = path.resolve(promo, '..');
const read = (f) => JSON.parse(fs.readFileSync(path.join(promo, 'data', f), 'utf8'));
const text = (f) => fs.readFileSync(path.join(promo, 'data', f), 'utf8');

const off1 = read('off1_result.json');
const opt = read('shape_opt_result.json');
const optWalls = read('shape_opt_winner_walls_result.json');
const raceRun = read('shape_race_result.json');
const racePick = read('race_shape_race.json');
const raceOff1 = read('race_off1.json');
const raceWeight = read('race_shape_opt.json');
const mix = read('mix_fit.json');
const path_ = read('search_path_hulls.json');
const off1Hull = JSON.parse(fs.readFileSync(path.join(repo, 'examples/off1_hull.json'), 'utf8'));

const r4 = (v) => (typeof v === 'number' ? Math.round(v * 1e4) / 1e4 : v);
const arr = (a) => a.map(r4);

function winner(res) {
  const w = res.winner;
  const c = w.candidate;
  return {
    hull_weight_lb: c.hull_weight_lb,
    thickness_in: c.thickness_in,
    density_pcf: c.density_pcf,
    depth_in: c.depth_in,
    flare_deg: c.flare_deg,
    length_in: c.length_in,
    beam_in: c.beam_in,
    gm_in: c.gm_in,
    gm_empty_in: c.gm_empty_in,
    drag_n: c.drag_n,
    race_s: c.race_s,
    dc: c.verdict.dc,
    governing_check: c.verdict.governing_check,
    freeboard_in: c.verdict.freeboard_in,
    checks: c.verdict.checks.map((k) => ({ name: k.name, dc: k.dc, demand: k.demand, capacity: k.capacity })),
    verified: w.verified,
    verify_stations: w.verify_stations,
    verify_dc: w.verify_dc,
    shape: c.shape ? { ...c.shape } : null,
    convergence: w.convergence ? { stations: w.convergence.stations, dc: w.convergence.dc } : null,
    search_space: w.search_space.map((s) => ({ name: s.name, min: s.min, max: s.max, count: s.count, winner: s.winner })),
  };
}

const gates = (res) => ({
  dc_search: res.params.gates.dc_search,
  dc_verify: res.params.gates.dc_verify,
  freeboard_min_in: res.params.gates.freeboard_min_in,
  gm_min_in: res.params.stability.gm_min_in,
});

function cases(res) {
  return res.winner.cases.map((k) => ({
    name: k.name,
    seats_in: k.seats_in,
    weights_lb: k.weights_lb,
    freeboard_in: k.freeboard_in,
    gm_in: k.gm_in,
    x_in: arr(k.x_in),
    shear_lb: arr(k.shear_lb),
    moment_lbin: arr(k.moment_lbin),
    tension_psi: arr(k.tension_psi),
    tension_allow_psi: r4(k.tension_allow_psi),
    shear_psi: arr(k.shear_psi),
    shear_allow_psi: r4(k.shear_allow_psi),
    waterline_in: arr(k.waterline_in),
    dc_bending_tension: arr(k.dc_bending_tension),
    dc_bending_compression: arr(k.dc_bending_compression),
    dc_shear: arr(k.dc_shear),
    dc_biaxial: arr(k.dc_biaxial),
  }));
}

// Candidates as [weight lb, D/C or null, passes 0/1]; shape runs add [length, beam].
const candRows = (res, shape) =>
  res.candidates.map((k) => {
    const row = [r4(k.hull_weight_lb), k.verdict ? r4(k.verdict.dc) : null, k.passes ? 1 : 0];
    return shape ? row.concat([r4(k.length_in), r4(k.beam_in)]) : row;
  });

const hullOf = (h) => ({ name: h.name, length: h.length, draft_design: h.draft_design, x: h.x, z: h.z, half_breadth: h.half_breadth, stem: h.stem || null });

function raceSummary(r) {
  return {
    hull: r.hull,
    hull_lb: r.hull_lb,
    weighted_time_s: r.weighted_time_s,
    events: r.events.map((e) => ({ name: e.name, course: e.course, time_s: e.time_s, distance_m: e.distance_m, crew_lb: e.crew_lb, paddlers: e.paddlers, weight: e.weight })),
  };
}
const slalom = racePick.events.find((e) => e.course === 'slalom_200');
const course = racePick.params.course.find((c) => c.name === slalom.course);

// [shape_search] block of examples/shape_opt.toml, verbatim (the canoe.toml snippet on screen).
const shapeToml = fs.readFileSync(path.join(repo, 'examples/shape_opt.toml'), 'utf8');
const block = shapeToml.slice(shapeToml.search(/^\[shape_search\]/m));
const shapeSearchToml = block
  .split('\n')
  .filter((l) => /^\[shape_search\]|^[a-z_]+\s*=/.test(l))
  .map((l) => l.replace(/\s+#.*$/, ''));

const terminal = text('terminal_run_canoe.txt');

const out = {
  provenance: 'promo/data (see promo/data/README.md); extracted by promo/scripts/extract.mjs',
  off1: {
    hull: hullOf(off1Hull),
    winner: winner(off1),
    gates: gates(off1),
    candidates: candRows(off1, false),
    cases: cases(off1),
    empty: { x_in: arr(off1.winner.empty.x_in), waterline_in: arr(off1.winner.empty.waterline_in), freeboard_in: off1.winner.empty.freeboard_in },
    terminal,
    command: 'jaguar run examples/canoe.toml',
  },
  shapeOpt: {
    winner: winner(opt),
    gates: gates(opt),
    winner_hull: hullOf(opt.winner_hull),
    candidates: candRows(opt, true),
    /** The winner hull's own wall grid (686 walls), [weight lb, D/C | null, passes 0/1]. */
    winner_walls: candRows(optWalls, false),
    shapes_evaluated: path_.shapes_evaluated,
    latin_hypercube_samples: path_.latin_hypercube_samples,
    cases: cases(opt),
    empty: { x_in: arr(opt.winner.empty.x_in), waterline_in: arr(opt.winner.empty.waterline_in), freeboard_in: opt.winner.empty.freeboard_in },
    incumbents: path_.incumbents.map((p) => ({ index: p.index, phase: p.phase, hull_weight_lb: p.hull_weight_lb, depth_in: p.depth_in, flare_deg: p.flare_deg, dc: p.dc, shape: p.shape, hull: hullOf(p.hull) })),
    spread: path_.spread.map((p) => ({ index: p.index, phase: p.phase, passes: p.passes, hull_weight_lb: p.hull_weight_lb, depth_in: p.depth_in, flare_deg: p.flare_deg, shape: p.shape, hull: hullOf(p.hull) })),
    shape_search_toml: shapeSearchToml,
  },
  shapeRace: {
    winner: winner(raceRun),
    winner_hull: hullOf(raceRun.winner_hull),
  },
  race: {
    illustrative: true,
    race_pick: raceSummary(racePick),
    weight_pick: raceSummary(raceWeight),
    off1: raceSummary(raceOff1),
    lane_width_m: racePick.params.lane_width_m,
    buoy_clearance_m: racePick.params.buoy_clearance_m,
    slalom: {
      name: slalom.name,
      hull: racePick.hull,
      hull_lb: racePick.hull_lb,
      paddlers: slalom.paddlers,
      crew_lb: slalom.crew_lb,
      course: { name: course.name, finish_x_m: course.finish_x_m, marks: course.marks },
      start_y_m: slalom.start_y_m,
      time_s: slalom.time_s,
      distance_m: slalom.distance_m,
      segments: slalom.segments.map((g) => ({ mark: g.mark, radius_m: g.radius_m, length_m: g.length_m, angle_deg: g.angle_deg, drift_deg: g.drift_deg })),
      path_m: slalom.path_m.map(([x, y]) => [r4(x), r4(y)]),
      speed_m: slalom.speed_m.map((p) => p.map(r4)),
    },
  },
  mix: mix,
};

const dst = path.join(promo, 'src/generated');
fs.mkdirSync(dst, { recursive: true });
fs.writeFileSync(path.join(dst, 'promo-data.json'), JSON.stringify(out));
console.log(`wrote src/generated/promo-data.json (${(fs.statSync(path.join(dst, 'promo-data.json')).size / 1024).toFixed(0)} KB)`);
