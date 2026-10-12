//! Hull-shape optimization (outer loop).
//!
//! For each candidate shape (a `hullgen::ShapeParams` inside the
//! `[shape_search]` bounds), generate the hull (41 stations x 21 waterlines,
//! named `opt-<index>`) and run the existing wall search on it serially
//! (`optimize::run_inner`, the inner loop); shapes are evaluated in parallel.
//! A shape's score is its inner winner's objective value: `hull_weight_lb`
//! for `objective.kind = "weight"`, `drag_n` for `"drag"` (a winner without a
//! drag value scores +inf). Shapes with no passing wall design score +inf.
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
//! breaks ties by index, so rayon scheduling never changes the result.

use crate::hull::Hull;
use crate::hullgen::{ShapeMetrics, ShapeParams, generate, metrics};
use crate::optimize::{Candidate, Progress, RunResult, run_inner, run_with_progress};
use crate::params::{ObjectiveKind, Params, ShapeSearch};
use crate::resistance::resistance;
use anyhow::{Context, Result};
use rayon::prelude::*;
use std::collections::HashSet;
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

fn to_shape(v: &[f64; 10]) -> ShapeParams {
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
        metrics: None,
    }
}

/// Apply manufacturability rules to a raw point: snap draft/length/beam to
/// their steps (nearest multiple inside the bounds) and tie aft = fore when
/// `symmetric`.
pub fn normalize(ss: &ShapeSearch, mut v: [f64; 10]) -> [f64; 10] {
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
    (0..10).filter(|&d| b[d].1[1] > b[d].1[0] && !(ss.symmetric && matches!(d, 6 | 7 | 9))).collect()
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

fn key(v: &[f64; 10]) -> [u64; 10] {
    v.map(f64::to_bits)
}

/// Latin hypercube sample of `n` points in the bounds.
fn lhs(ss: &ShapeSearch, n: usize) -> Vec<[f64; 10]> {
    let b = ss.bounds();
    let mut rng = SplitMix64(ss.seed);
    let mut pts = vec![[0.0; 10]; n];
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

/// Normalizers for the straight/turning blend: drag (N) and lateral moment
/// (in^4) of the fixed reference shape, the midpoint of the bounds.
#[derive(Clone, Copy)]
pub struct Refs {
    drag_n: f64,
    moment_in4: f64,
}

fn refs(params: &Params, ss: &ShapeSearch) -> Result<Refs> {
    let mid = normalize(ss, ss.bounds().map(|(_, [lo, hi])| 0.5 * (lo + hi)));
    let s = to_shape(&mid);
    let hull = generate(&s, STATIONS, WATERLINES, "reference")?;
    let drag_n = resistance(&hull, s.draft_in, params.objective.speed_mps)
        .context("drag of the reference (midpoint) shape")?
        .total_n;
    let moment_in4 = metrics(&s).lateral_moment_in4;
    anyhow::ensure!(drag_n > 0.0 && moment_in4 > 0.0, "reference shape has zero drag or lateral moment");
    Ok(Refs { drag_n, moment_in4 })
}

fn score_of(params: &Params, r: &RunResult, m: &ShapeMetrics, refs: Option<Refs>) -> f64 {
    if m.rejected.is_some() {
        return f64::INFINITY;
    }
    match &r.winner {
        None => f64::INFINITY,
        Some(w) => match params.objective.kind {
            ObjectiveKind::Weight => w.candidate.hull_weight_lb,
            ObjectiveKind::Drag => {
                let d = w.candidate.drag_n.unwrap_or(f64::INFINITY);
                match refs {
                    None => d,
                    Some(rf) => {
                        let sw = params.objective.straight_weight;
                        sw * d / rf.drag_n + (1.0 - sw) * m.lateral_moment_in4 / rf.moment_in4
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

fn hull_for(v: &[f64; 10], index: usize) -> Result<Hull> {
    generate(&to_shape(v), STATIONS, WATERLINES, &format!("opt-{index}"))
}

fn eval_shape(
    params: &Params,
    ss: &ShapeSearch,
    refs: Option<Refs>,
    v: &[f64; 10],
    index: usize,
) -> Result<Eval> {
    let hull = hull_for(v, index)?;
    let r = run_inner(params, &hull, false, &|_| {})?;
    let mut rep = representative(&r).context("inner search produced no candidates")?;
    let mut shape = to_shape(v);
    let m = shape_metrics(ss, &shape);
    let score = score_of(params, &r, &m, refs);
    if let Some(why) = &m.rejected {
        rep.passes = false;
        rep.error = Some(format!("shape rejected: {why}"));
    }
    shape.metrics = Some(m);
    rep.shape = Some(shape);
    Ok(Eval { score, rep })
}

pub fn run(params: &Params, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
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
    let free = free_dims(ss);

    let mut shapes: Vec<[f64; 10]> = Vec::new();
    let mut evals: Vec<Eval> = Vec::new();
    let mut seen: HashSet<[u64; 10]> = HashSet::new();
    let done = AtomicUsize::new(0);

    let batch_eval =
        |batch: Vec<[f64; 10]>, shapes: &mut Vec<[f64; 10]>, evals: &mut Vec<Eval>| -> Result<()> {
            let base = shapes.len();
            let total = base + batch.len();
            on_progress(Progress::Started { total });
            let out: Vec<Result<Eval>> = batch
                .par_iter()
                .enumerate()
                .map(|(i, v)| {
                    let index = base + i;
                    on_progress(Progress::Shape { index, shapes: total, shape: to_shape(v) });
                    let e = eval_shape(params, ss, refs, v, index);
                    if let Ok(e) = &e {
                        let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                        on_progress(Progress::Candidate { done: k, total, candidate: e.rep.clone() });
                    }
                    e
                })
                .collect();
            for (v, e) in batch.into_iter().zip(out) {
                evals.push(e?);
                shapes.push(v);
            }
            Ok(())
        };

    let init: Vec<[f64; 10]> = lhs(ss, ss.samples).into_iter().filter(|v| seen.insert(key(v))).collect();
    batch_eval(init, &mut shapes, &mut evals)?;

    let ranked = |evals: &[Eval]| {
        let mut idx: Vec<usize> = (0..evals.len()).collect();
        idx.sort_by(|&a, &b| evals[a].score.total_cmp(&evals[b].score).then(a.cmp(&b)));
        idx
    };
    let mut step = 1.0 / 8.0;
    for _ in 0..ss.refine_rounds {
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

    let best = ranked(&evals)[0];
    let reps: Vec<Candidate> = evals.iter().map(|e| e.rep.clone()).collect();
    let mut shape = to_shape(&shapes[best]);
    shape.metrics = Some(shape_metrics(ss, &shape));
    let hull = hull_for(&shapes[best], best)?;
    let forward = |p: Progress| {
        if matches!(p, Progress::Verifying { .. }) {
            on_progress(p)
        }
    };
    let mut r = run_with_progress(params, &hull, &forward)?;
    if shape.metrics.as_ref().is_some_and(|m| m.rejected.is_some()) {
        // Every shape broke a [shape_search] constraint: no winner, never a rejected one.
        r.winner = None;
    }
    if let Some(w) = &mut r.winner {
        w.candidate.shape = Some(shape);
    }
    if r.winner.is_some() {
        r.winner_hull = Some(hull);
    }
    r.candidates = reps;
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
