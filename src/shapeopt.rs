//! Hull-shape optimization (outer loop).
//!
//! For each candidate shape (a `hullgen::ShapeParams` inside the
//! `[shape_search]` bounds), generate the hull (41 stations x 21 waterlines,
//! named `opt-<index>`) and run the existing wall search on it serially
//! (`optimize::run_inner`, the inner loop); shapes are evaluated in parallel
//! (`on_workers`). A shape's score is its inner winner's objective value:
//! `hull_weight_lb` for `objective.kind = "weight"`, `drag_n` for `"drag"`
//! (a winner without a drag value scores +inf), blended with the steady-turn
//! drag when `straight_weight < 1`, and for `"race"` the weighted race time
//! of the inner winner on paths planned once for the reference (midpoint)
//! shape's inner winner (`race::plan` / `race::planned_time`); the final winner
//! is raced again on paths re-planned for it (`RunResult::race`). Shapes with
//! no passing wall design score +inf.
//!
//! Algorithm:
//! 1. Latin hypercube of `samples` shapes (SplitMix64, seeded). min == max
//!    parameters are held fixed.
//! 2. `refine_rounds` rounds of pattern search: around the best K = 4 shapes,
//!    +/- step on each free parameter (step = range/8, halved after a round
//!    without improvement), clamped, already-seen shapes skipped.
//! 3. The best shape is re-run with the normal (parallel) inner search, so the
//!    winner is verified at 2n-1 stations as usual.
//!
//! Progress: `Started { total }` counts SHAPES (re-emitted with a larger total
//! when a refinement round adds shapes); `Shape` per shape; `Candidate` once
//! per shape with its representative candidate (`done`/`total` in shapes).
//! The final re-run only forwards `Verifying`.
//!
//! Deterministic: shapes are indexed in generation order and every ranking
//! breaks ties by index, so thread scheduling never changes the result.

use crate::hull::{Grid, Hull, linspace};
use crate::hullgen::{ShapeMetrics, ShapeParams, Turn, generate, keel_z, metrics};
use crate::optimize::{Candidate, Progress, RunControl, RunEnded, RunResult, run_inner, run_with_progress};
use crate::params::{ObjectiveKind, Params, SHAPE_DIMS, ShapeSearch};
use crate::race;
use crate::resistance::{displaced_volume_in3, level_draft_in, resistance, turn};
use crate::units::{IN3_PER_FT3, WATER_PCF};
use anyhow::{Context, Result};
use std::collections::HashSet;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

pub const STATIONS: usize = 41;
pub const WATERLINES: usize = 21;
const K_BEST: usize = 4;

struct SplitMix64(u64);
impl SplitMix64 {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    /// Uniform in [0, 1).
    fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }
}

fn to_shape(v: &[f64; SHAPE_DIMS]) -> ShapeParams {
    ShapeParams {
        length_in: v[0],
        beam_in: v[1],
        draft_in: v[2],
        fore_wp: v[3],
        section: v[4],
        fore_rocker: v[5],
        aft_wp: v[6],
        aft_rocker: v[7],
        fore_len_frac: v[8],
        aft_len_frac: v[9],
        gunwale_radius_in: (v[10] > 0.0).then_some(v[10]),
        metrics: None,
    }
}

/// Apply manufacturability rules to a raw point: snap draft/length/beam to
/// their steps (nearest multiple inside the bounds) and tie aft = fore when
/// `symmetric`.
pub fn normalize(ss: &ShapeSearch, mut v: [f64; SHAPE_DIMS]) -> [f64; SHAPE_DIMS] {
    for (d, step, [lo, hi]) in [
        (0, ss.length_step_in, ss.length_in),
        (1, ss.beam_step_in, ss.beam_in),
        (2, ss.draft_step_in, ss.draft_in),
    ] {
        if let Some(st) = step {
            let (kmin, kmax) = ((lo / st - 1e-9).ceil(), (hi / st + 1e-9).floor());
            v[d] = (v[d] / st).round().clamp(kmin, kmax) * st;
        }
    }
    if ss.symmetric {
        v[6] = v[3];
        v[7] = v[5];
        v[9] = v[8];
    }
    v
}

/// Dimensions the search moves (fixed bounds and symmetric-tied aft ones excluded).
fn free_dims(ss: &ShapeSearch) -> Vec<usize> {
    let b = ss.bounds();
    (0..SHAPE_DIMS).filter(|&d| b[d].1[1] > b[d].1[0] && !(ss.symmetric && matches!(d, 6 | 7 | 9))).collect()
}

/// Steady turn of shape `s` floating level at `draft_in` with total weight
/// `lb`, at `objective.speed_mps` and `objective.turn_radius_in`.
fn turn_at(params: &Params, s: &ShapeParams, draft_in: f64, lb: f64) -> Result<Turn> {
    let x = linspace(0.0, s.length_in, 401);
    let depth: Vec<f64> = x.iter().map(|&v| (draft_in - keel_z(s, v)).max(0.0)).collect();
    turn(&x, &depth, lb, params.objective.speed_mps, params.objective.turn_radius_in)
}

/// Steady turn of shape `s` (generated as `hull`) with wall weight `hull_lb`
/// and mold depth `depth_in`, weighted like the drag objective: over
/// `objective.displacements`, else the heaviest load case's crew. Each floats
/// level where hull + crew displaces its weight (topsides built as the inner
/// search builds them, flare included).
pub fn loaded_turn(
    params: &Params,
    s: &ShapeParams,
    hull: &Hull,
    hull_lb: f64,
    depth_in: f64,
    flare_deg: f64,
) -> Result<Turn> {
    let loads: Vec<(f64, f64)> = match &params.objective.displacements {
        Some(ds) => ds.iter().map(|d| (d.crew_lb, d.weight)).collect(),
        None => {
            let crew = params.load_cases.iter().map(|c| c.weights_lb.iter().sum::<f64>()).fold(0.0, f64::max);
            vec![(crew, 1.0)]
        }
    };
    let ts = crate::hull::Topsides { flare_deg, ..params.hull.topsides() };
    let g = Grid::build_topsides(hull, depth_in, STATIONS, ts).0;
    let nz = g.nz();
    let topsides = Hull {
        name: "loaded".into(),
        draft_design: g.z[nz - 1],
        x: g.x.clone(),
        z: g.z.clone(),
        half_breadth: (0..g.nx()).map(|i| g.row(i).to_vec()).collect(),
        stem: None,
        ..hull.clone()
    };
    let mut sum = Turn { drag_n: 0.0, moment_nm: 0.0, drift_deg: 0.0 };
    for (crew, w) in loads {
        let lb = hull_lb + crew;
        let t = turn_at(params, s, level_draft_in(&topsides, lb)?, lb)?;
        sum.drag_n += w * t.drag_n;
        sum.moment_nm += w * t.moment_nm;
        sum.drift_deg += w * t.drift_deg;
    }
    Ok(sum)
}

/// Measured metrics plus the reason the shape breaks a [shape_search] constraint, if it does.
pub fn shape_metrics(ss: &ShapeSearch, s: &ShapeParams) -> ShapeMetrics {
    let mut m = metrics(s);
    if let Some(a) = ss.min_entry_angle_deg
        && m.entry_angle_deg < a
    {
        m.rejected = Some(format!("entry half-angle {:.1} deg < min_entry_angle_deg {a}", m.entry_angle_deg));
    } else if let Some([lo, hi]) = ss.rocker_in {
        for (end, r) in [("bow", m.rocker_bow_in), ("stern", m.rocker_stern_in)] {
            if r < lo - 1e-9 || r > hi + 1e-9 {
                m.rejected = Some(format!("{end} rocker {r:.2} in outside rocker_in [{lo}, {hi}]"));
                break;
            }
        }
    }
    m
}

fn key(v: &[f64; SHAPE_DIMS]) -> [u64; SHAPE_DIMS] {
    v.map(f64::to_bits)
}

/// Latin hypercube sample of `n` points in the bounds.
fn lhs(ss: &ShapeSearch, n: usize) -> Vec<[f64; SHAPE_DIMS]> {
    let b = ss.bounds();
    let mut rng = SplitMix64(ss.seed);
    let mut pts = vec![[0.0; SHAPE_DIMS]; n];
    for (d, (_, [lo, hi])) in b.iter().enumerate() {
        let mut perm: Vec<usize> = (0..n).collect();
        for i in (1..n).rev() {
            let j = (rng.next() % (i as u64 + 1)) as usize;
            perm.swap(i, j);
        }
        for (i, p) in pts.iter_mut().enumerate() {
            let u = rng.unit();
            p[d] = if hi > lo { lo + (perm[i] as f64 + u) / n as f64 * (hi - lo) } else { *lo };
        }
    }
    pts.into_iter().map(|v| normalize(ss, v)).collect()
}

struct Eval {
    score: f64,
    rep: Candidate,
}

/// Normalizers for the straight/turning blend: straight drag and steady-turn
/// drag (N) of the fixed reference shape, the midpoint of the bounds.
#[derive(Clone, Copy)]
pub struct Refs {
    drag_n: f64,
    turn_n: f64,
}

fn refs(params: &Params, ss: &ShapeSearch) -> Result<Refs> {
    let mid = normalize(ss, ss.bounds().map(|(_, [lo, hi])| 0.5 * (lo + hi)));
    let s = to_shape(&mid);
    let hull = generate(&s, STATIONS, WATERLINES, "reference")?;
    let drag_n = resistance(&hull, s.draft_in, params.objective.speed_mps)
        .context("drag of the reference (midpoint) shape")?
        .total_n;
    let lb = displaced_volume_in3(&hull, s.draft_in) * WATER_PCF / IN3_PER_FT3;
    let turn_n =
        turn_at(params, &s, s.draft_in, lb).context("turn of the reference (midpoint) shape")?.drag_n;
    anyhow::ensure!(drag_n > 0.0 && turn_n > 0.0, "reference shape has zero straight or turning drag");
    Ok(Refs { drag_n, turn_n })
}

/// Race paths for the shape scores: planned on the reference (midpoint) shape
/// with its inner winner's wall (or its representative candidate's, so the
/// plan exists even when that shape has no passing wall).
fn race_plan(params: &Params, ss: &ShapeSearch) -> Result<race::Plan> {
    let rp = params.race.as_ref().context("objective.kind = race needs [race]")?;
    let mid = normalize(ss, ss.bounds().map(|(_, [lo, hi])| 0.5 * (lo + hi)));
    let s = to_shape(&mid);
    let hull = templated(params, generate(&s, STATIONS, WATERLINES, "reference")?)?;
    let params = &for_shape(params, &s);
    let r = run_inner(params, &hull, true, &|_| {})?;
    let c = representative(&r).context("reference shape: inner search produced no candidates")?;
    let top = race::topsides(&hull, c.depth_in, crate::optimize::candidate_topsides(params, &c));
    race::plan(&top, c.hull_weight_lb, rp).context("race paths on the reference (midpoint) shape")
}

fn score_of(
    params: &Params,
    r: &RunResult,
    m: &ShapeMetrics,
    refs: Option<Refs>,
    race_s: Option<f64>,
) -> f64 {
    if m.rejected.is_some() {
        return f64::INFINITY;
    }
    match &r.winner {
        None => f64::INFINITY,
        Some(w) => match params.objective.kind {
            ObjectiveKind::Weight => w.candidate.hull_weight_lb,
            ObjectiveKind::Race => race_s.unwrap_or(f64::INFINITY),
            ObjectiveKind::Drag => {
                let d = w.candidate.drag_n.unwrap_or(f64::INFINITY);
                match refs {
                    None => d,
                    Some(rf) => {
                        let sw = params.objective.straight_weight;
                        let t = m.turn.map_or(f64::INFINITY, |t| t.drag_n);
                        sw * d / rf.drag_n + (1.0 - sw) * t / rf.turn_n
                    }
                }
            }
        },
    }
}

/// Inner winner, or best failing candidate (lowest D/C; else the first).
fn representative(r: &RunResult) -> Option<Candidate> {
    if let Some(w) = &r.winner {
        return Some(w.candidate.clone());
    }
    r.candidates
        .iter()
        .min_by(|a, b| {
            let da = a.verdict.as_ref().map_or(f64::INFINITY, |v| v.dc);
            let db = b.verdict.as_ref().map_or(f64::INFINITY, |v| v.dc);
            da.total_cmp(&db)
        })
        .cloned()
}

/// The hull analysed for shape `v`: generated, then `Hull::prepared` (templates, stem
/// radius), so the search tunes the hull as it will be built.
fn hull_for(params: &Params, v: &[f64; SHAPE_DIMS], index: usize) -> Result<Hull> {
    templated(params, generate(&to_shape(v), STATIONS, WATERLINES, &format!("opt-{index}"))?)
}

/// [`hull_for`], or, when the shape cannot take `hull.stem_radius_in`, the shape built without
/// it and why: the shape is then rejected with that reason, its numbers those of the real
/// generated hull, and the search goes on. (An arc taller than the mold is rejected per
/// candidate instead, by `Grid::build_topsides`.)
fn hull_or_reason(params: &Params, v: &[f64; SHAPE_DIMS], index: usize) -> Result<(Hull, Option<String>)> {
    match hull_for(params, v, index) {
        Ok(h) => Ok((h, None)),
        Err(e) if params.hull.stem_radius_in > 0.0 => {
            let mut p = params.clone();
            p.hull.stem_radius_in = 0.0;
            Ok((hull_for(&p, v, index)?, Some(format!("{e:#}"))))
        }
        Err(e) => Err(e),
    }
}

fn templated(params: &Params, h: Hull) -> Result<Hull> {
    h.prepared(&params.hull)
}

/// `params` for one shape: its searched gunwale radius becomes `hull.gunwale_radius_in`
/// (the inner search and every topsides builder read it there).
fn for_shape(params: &Params, s: &ShapeParams) -> Params {
    let mut p = params.clone();
    if s.gunwale_radius_in.is_some() {
        p.hull.gunwale_radius_in = s.gunwale_radius_in;
    }
    p
}

fn eval_shape(
    params: &Params,
    ss: &ShapeSearch,
    refs: Option<Refs>,
    plan: Option<&race::Plan>,
    v: &[f64; SHAPE_DIMS],
    index: usize,
) -> Result<Eval> {
    let (hull, unbuildable) = hull_or_reason(params, v, index)?;
    let mut shape = to_shape(v);
    let params = &for_shape(params, &shape);
    shape.gunwale_radius_in = params.hull.gunwale_radius_in;
    // Called on a 1-thread pool (`on_workers`): "parallel" rayon calls run serially there.
    let r = run_inner(params, &hull, true, &|_| {})?;
    let mut rep = representative(&r).context("inner search produced no candidates")?;
    if rep.drag_n.is_none() {
        rep.drag_n = crate::optimize::candidate_drag(params, &hull, &rep);
    }
    let mut m = shape_metrics(ss, &shape);
    if m.rejected.is_none() {
        m.rejected = unbuildable;
    }
    match loaded_turn(params, &shape, &hull, rep.hull_weight_lb, rep.depth_in, rep.flare_deg) {
        Ok(t) => m.turn = Some(t),
        Err(e) if refs.is_some() && m.rejected.is_none() => {
            rep.passes = false;
            rep.error = Some(format!("turn: {e:#}"));
        }
        Err(_) => {}
    }
    if let (Some(pl), Some(rp), Some(w)) = (plan, &params.race, &r.winner)
        && m.rejected.is_none()
    {
        let c = &w.candidate;
        let top = race::topsides(&hull, c.depth_in, crate::optimize::candidate_topsides(params, c));
        match race::planned_time(&top, c.hull_weight_lb, rp, pl) {
            Ok(t) => rep.race_s = Some(t),
            Err(e) => {
                rep.passes = false;
                rep.error = Some(format!("race: {e:#}"));
            }
        }
    }
    let score = score_of(params, &r, &m, refs, rep.race_s);
    if let Some(why) = &m.rejected {
        rep.passes = false;
        rep.error = Some(format!("shape rejected: {why}"));
    }
    shape.metrics = Some(m);
    rep.shape = Some(shape);
    Ok(Eval { score, rep })
}

/// `f(0..n)` spread over `rayon::current_num_threads()` plain threads, each
/// owning one private 1-thread rayon pool for the inner search. Results in
/// index order (deterministic). Each worker passes `ctl`'s checkpoint before taking an index:
/// a pause holds it there; once stopped, the indices not started stay None.
///
/// Not `par_iter` + a per-shape pool: a rayon worker blocked in a cross-pool
/// `install` steals other shapes from its own pool while it waits, nesting
/// one shape's frames on another's until a debug build overflows the stack.
/// Plain threads block cleanly.
fn on_workers<T: Send>(
    n: usize,
    ctl: &RunControl,
    f: &(dyn Fn(usize) -> T + Sync),
) -> Result<Vec<Option<T>>> {
    let threads = rayon::current_num_threads().clamp(1, n.max(1));
    let next = AtomicUsize::new(0);
    let slots: Vec<Mutex<Option<T>>> = (0..n).map(|_| Mutex::new(None)).collect();
    std::thread::scope(|sc| -> Result<()> {
        let handles: Vec<_> = (0..threads)
            .map(|_| {
                sc.spawn(|| -> Result<()> {
                    let pool = rayon::ThreadPoolBuilder::new().num_threads(1).build()?;
                    pool.install(|| {
                        loop {
                            if ctl.checkpoint() {
                                break;
                            }
                            let i = next.fetch_add(1, Ordering::SeqCst);
                            if i >= n {
                                break;
                            }
                            let out = f(i);
                            *slots[i].lock().expect("slot lock") = Some(out);
                        }
                    });
                    Ok(())
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap_or_else(|e| std::panic::resume_unwind(e))?;
        }
        Ok(())
    })?;
    let out: Vec<Option<T>> = slots.into_iter().map(|m| m.into_inner().expect("slot lock")).collect();
    anyhow::ensure!(ctl.stopped() || out.iter().all(Option::is_some), "shape worker skipped an index");
    Ok(out)
}

pub fn run(params: &Params, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
    run_controlled(params, on_progress, &RunControl::default())
}

/// [`run`] that can be paused and ended. Ended: no new shapes; the best shape checked so far
/// becomes the winner as usual and `ended_early` says how far the search got. Ended before any
/// shape finished: Err([`RunEnded`]).
pub fn run_controlled(
    params: &Params,
    on_progress: &(dyn Fn(Progress) + Sync),
    ctl: &RunControl,
) -> Result<RunResult> {
    let ss = params.shape_search.as_ref().context("no [shape_search] in params")?;
    let b = ss.bounds();
    for (n, [lo, hi]) in b {
        anyhow::ensure!(lo.is_finite() && hi.is_finite() && lo <= hi, "shape_search.{n}: need min <= max");
    }
    anyhow::ensure!(ss.samples >= 1, "shape_search.samples must be >= 1");
    let refs = if params.objective.kind == ObjectiveKind::Drag && params.objective.straight_weight < 1.0 {
        Some(refs(params, ss)?)
    } else {
        None
    };
    let plan = if params.objective.kind == ObjectiveKind::Race { Some(race_plan(params, ss)?) } else { None };
    let free = free_dims(ss);

    let mut shapes: Vec<[f64; SHAPE_DIMS]> = Vec::new();
    let mut evals: Vec<Eval> = Vec::new();
    let mut seen: HashSet<[u64; SHAPE_DIMS]> = HashSet::new();
    let done = AtomicUsize::new(0);

    let batch_eval = |batch: Vec<[f64; SHAPE_DIMS]>,
                      shapes: &mut Vec<[f64; SHAPE_DIMS]>,
                      evals: &mut Vec<Eval>|
     -> Result<()> {
        let base = shapes.len();
        let total = base + batch.len();
        on_progress(Progress::Started { total });
        let out: Vec<Option<Result<Eval>>> = on_workers(batch.len(), ctl, &|i| {
            let (v, index) = (&batch[i], base + i);
            on_progress(Progress::Shape { index, shapes: total, shape: to_shape(v) });
            let e = eval_shape(params, ss, refs, plan.as_ref(), v, index);
            if let Ok(e) = &e {
                let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                on_progress(Progress::Candidate { done: k, total, candidate: e.rep.clone() });
            }
            e
        })?;
        // Ended: only the shapes that were started (each finishes) are kept.
        for (v, e) in batch.into_iter().zip(out) {
            if let Some(e) = e {
                evals.push(e?);
                shapes.push(v);
            }
        }
        Ok(())
    };

    let init: Vec<[f64; SHAPE_DIMS]> =
        lhs(ss, ss.samples).into_iter().filter(|v| seen.insert(key(v))).collect();
    batch_eval(init, &mut shapes, &mut evals)?;

    let ranked = |evals: &[Eval]| {
        let mut idx: Vec<usize> = (0..evals.len()).collect();
        idx.sort_by(|&a, &b| evals[a].score.total_cmp(&evals[b].score).then(a.cmp(&b)));
        idx
    };
    let mut step = 1.0 / 8.0;
    let mut rounds = 0;
    for _ in 0..ss.refine_rounds {
        if ctl.stopped() {
            break;
        }
        rounds += 1;
        let before = evals[ranked(&evals)[0]].score;
        let mut batch = Vec::new();
        for &c in ranked(&evals).iter().take(K_BEST) {
            for &d in &free {
                let [lo, hi] = b[d].1;
                for sgn in [1.0, -1.0] {
                    let mut v = shapes[c];
                    v[d] = (v[d] + sgn * step * (hi - lo)).clamp(lo, hi);
                    let v = normalize(ss, v);
                    if seen.insert(key(&v)) {
                        batch.push(v);
                    }
                }
            }
        }
        if batch.is_empty() {
            step *= 0.5;
            continue;
        }
        batch_eval(batch, &mut shapes, &mut evals)?;
        if evals[ranked(&evals)[0]].score >= before {
            step *= 0.5;
        }
    }

    if evals.is_empty() {
        return Err(RunEnded.into());
    }
    let ended_early = ctl.stopped().then(|| {
        format!(
            "ended by the user after {} shapes ({} of {} samples, {} of {} refine rounds started)",
            evals.len(),
            evals.len().min(ss.samples),
            ss.samples,
            rounds,
            ss.refine_rounds
        )
    });
    let best = ranked(&evals)[0];
    let reps: Vec<Candidate> = evals.iter().map(|e| e.rep.clone()).collect();
    // Shape with its metrics (turn at the same inner winner the re-run finds).
    let shape = reps[best].shape.clone().context("best shape lost its parameters")?;
    let hull = hull_or_reason(params, &shapes[best], best)?.0;
    let forward = |p: Progress| {
        if matches!(p, Progress::Verifying { .. }) {
            on_progress(p)
        }
    };
    let winner_params = for_shape(params, &shape);
    let mut r = run_with_progress(&winner_params, &hull, &forward)?;
    // The record keeps the input params; the winner's radius is in its shape.
    r.params = params.clone();
    if shape.metrics.as_ref().is_some_and(|m| m.rejected.is_some()) {
        // Every shape broke a [shape_search] constraint: no winner, never a rejected one.
        r.winner = None;
    }
    if let Some(w) = &mut r.winner {
        w.candidate.shape = Some(shape);
        w.candidate.race_s = reps[best].race_s;
        // Now that the winner carries its shape, add the shape axes.
        w.search_space = crate::optimize::search_space(params, &r.strength, &w.candidate)?;
    }
    if let (Some(w), Some(rp), ObjectiveKind::Race) = (&r.winner, &params.race, params.objective.kind) {
        let c = &w.candidate;
        let ts = crate::optimize::candidate_topsides(&winner_params, c);
        r.race = Some(
            race::run(&hull, c.hull_weight_lb, c.depth_in, ts, rp)
                .context("race of the winner on re-planned paths")?,
        );
    }
    if r.winner.is_some() {
        r.winner_hull = Some(hull);
    }
    r.candidates = reps;
    r.ended_early = ended_early;
    Ok(r)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitmix_is_deterministic_and_unit() {
        let (mut a, mut b) = (SplitMix64(7), SplitMix64(7));
        for _ in 0..100 {
            let u = a.unit();
            assert!((0.0..1.0).contains(&u));
            assert_eq!(u, b.unit());
        }
    }
}
