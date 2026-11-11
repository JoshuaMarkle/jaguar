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
//!    without improvement), clamped, already-seen shapes skipped. Ends early
//!    once the step is below 1/1024 of the range ([`MIN_STEP`]).
//!    Each shape is scored by `optimize::run_inner`: the wall search without
//!    the winner analysis, which only the final re-run needs.
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
//!
//! Stepped (the phone app, docs/SPEC.md "Phone app"): [`run_step`] replays the search with
//! the shapes scored so far ([`ShapeEval`], scored anywhere by [`score_shape`]) and stops at the
//! first batch that misses one, returning the missing [`ShapeJob`]s. Once every shape it asks for
//! is given it returns the same result as [`run_controlled`].

use crate::hull::{Grid, Hull, linspace};
use crate::hullgen::{END_FRAC, ShapeMetrics, ShapeParams, Turn, generate, keel_z, metrics};
use crate::optimize::{
    Candidate, Inner, Progress, RunControl, RunEnded, RunResult, run_inner, run_with_progress,
};
use crate::params::{ObjectiveKind, Params, SHAPE_DIMS, ShapeSearch};
use crate::race;
use crate::resistance::{displaced_volume_in3, level_draft_in, resistance, turn};
use crate::units::{IN3_PER_FT3, WATER_PCF};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

pub const STATIONS: usize = 41;
pub const WATERLINES: usize = 21;
const K_BEST: usize = 4;
/// Pattern search ends once its step (a fraction of each factor's range) falls below this:
/// 1/1024 of the range is far finer than a mold is built (0.007 in of a 7 in draft range).
const MIN_STEP: f64 = 1.0 / 1024.0;

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

/// Wall thickness and board of a board mold (`search.mold_board_in`; params validation pins
/// the thickness to one value with a shape search).
pub fn board(params: &Params) -> Option<(f64, f64)> {
    params.search.mold_board_in.map(|b| (params.search.thickness_in[0], b))
}

/// With a board mold the curved bottom of the mold (inner keel at t to the table top
/// `draft_design`, where the straight flared topsides start) is whole boards: the table top is
/// t + j b, j >= 1, so the topsides (depth - top) are whole boards too. The keel lift `generate`
/// removes is a fixed fraction of the draft (the table rows depend only on z / draft), so the
/// top per inch of draft is measured once at draft 1. No j inside the draft bounds: `v` is left
/// as is and [`board_reason`] rejects the shape.
fn board_draft(ss: &ShapeSearch, (t, b): (f64, f64), v: &mut [f64; SHAPE_DIMS]) {
    let mut s = to_shape(v);
    s.draft_in = 1.0;
    let Ok(h) = generate(&s, STATIONS, WATERLINES, "board") else { return };
    let f = h.draft_design;
    let [lo, hi] = ss.draft_in;
    let j0 = ((lo * f - t) / b - 1e-9).ceil().max(1.0);
    let j1 = ((hi * f - t) / b + 1e-9).floor();
    if j0 <= j1 {
        v[2] = (t + ((v[2] * f - t) / b).round().clamp(j0, j1) * b) / f;
    }
}

/// With a board mold the rocker (keel rise at `END_FRAC` of length from each end, the
/// `rocker_in` measure) is whole boards j b: the end's rocker exponent is solved for it. The
/// keel there is draft (1 - X^pr) with X the station's taper factor (measured as the keel at
/// pr = 1), so pr = ln(1 - j b / draft) / ln X, nearest the sampled rocker, inside the exponent
/// bounds, `rocker_in` and below the draft. No such j: `v` is left as is and [`board_reason`]
/// rejects the shape.
fn board_rocker(ss: &ShapeSearch, b: f64, v: &mut [f64; SHAPE_DIMS]) {
    let s = to_shape(v);
    let (l, t) = (s.length_in, s.draft_in);
    let mut unit = s.clone();
    (unit.fore_rocker, unit.aft_rocker) = (1.0, 1.0);
    for (d, x, [elo, ehi]) in [(5, END_FRAC * l, ss.fore_rocker), (7, (1.0 - END_FRAC) * l, ss.aft_rocker)] {
        if d == 7 && ss.symmetric {
            v[7] = v[5];
            continue;
        }
        let xx = 1.0 - keel_z(&unit, x) / t;
        if !(xx > 0.0 && xx < 1.0) {
            continue;
        }
        let rocker = |pr: f64| t * (1.0 - xx.powf(pr));
        let [rlo, rhi] = ss.rocker_in.unwrap_or([0.0, f64::INFINITY]);
        let j0 = (rocker(elo).max(rlo) / b - 1e-9).ceil().max(0.0);
        let j1 = (rocker(ehi).min(rhi) / b + 1e-9).floor().min((t / b - 1e-9).ceil() - 1.0);
        if j0 <= j1 {
            let j = (rocker(v[d]) / b).round().clamp(j0, j1);
            v[d] = if j == 0.0 { 0.0 } else { (1.0 - j * b / t).ln() / xx.ln() };
        }
    }
}

/// Why shape `s` (`hull` generated from it) breaks the board mold, if it does: its curved bottom
/// or its rocker is not whole boards.
fn board_reason(params: &Params, s: &ShapeParams, hull: &Hull) -> Option<String> {
    let (t, b) = board(params)?;
    let whole = |v: f64| (v - v.round()).abs() <= 1e-6;
    let j = (hull.draft_design - t) / b;
    if j < 1.0 - 1e-6 || !whole(j) {
        return Some(format!(
            "curved bottom {:.3} in is not the thickness {t} in plus whole mold boards of {b} in",
            hull.draft_design
        ));
    }
    let m = metrics(s);
    [("bow", m.rocker_bow_in), ("stern", m.rocker_stern_in)]
        .into_iter()
        .find(|&(_, r)| !whole(r / b))
        .map(|(end, r)| format!("{end} rocker {r:.3} in is not whole mold boards of {b} in"))
}

/// Apply manufacturability rules to a raw point: snap draft/length/beam to
/// their steps (nearest multiple inside the bounds), tie aft = fore when
/// `symmetric`, and with a board mold set the draft to whole boards ([`board_draft`]).
pub fn normalize(ss: &ShapeSearch, board: Option<(f64, f64)>, mut v: [f64; SHAPE_DIMS]) -> [f64; SHAPE_DIMS] {
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
    if let Some(tb) = board {
        // The rocker scales with the draft and the keel lift (so the draft for a given top) with
        // the rocker: alternate until both stay put (one or two rounds; board_reason rejects
        // what does not settle).
        for _ in 0..8 {
            let before = v;
            board_draft(ss, tb, &mut v);
            board_rocker(ss, tb.1, &mut v);
            if v == before {
                break;
            }
        }
    }
    v
}

/// Dimensions the search moves (fixed bounds and symmetric-tied aft ones excluded).
/// Shapes a search is expected to evaluate (the web UI's time estimate): the samples plus up to
/// [`TYPICAL_ROUNDS`] refine rounds at full size (the best K_BEST shapes stepped both ways along
/// each free factor). Not a bound: the search stops at [`MIN_STEP`] (7 rounds without
/// improvement from the start), but rounds that improve keep their step, and the web UI's
/// estimate grows as rounds are added.
pub fn planned_shapes(ss: &ShapeSearch) -> usize {
    ss.samples + ss.refine_rounds.min(TYPICAL_ROUNDS) * K_BEST * 2 * free_dims(ss).len()
}

/// The search cut to about `budget` planned shapes ([`planned_shapes`]), as (samples,
/// refine_rounds); unchanged when it already fits. The phone app's "phone-sized" run (docs/SPEC.md
/// "Phone app"). Refinement gets at most half the budget (it is what converges, but each round
/// costs `K_BEST` x 2 x free factors shapes), the samples the rest; never more than the search
/// asked for, never fewer than [`MIN_SIZED_SAMPLES`] samples.
pub fn sized(ss: &ShapeSearch, budget: usize) -> (usize, usize) {
    if planned_shapes(ss) <= budget {
        return (ss.samples, ss.refine_rounds);
    }
    let per_round = K_BEST * 2 * free_dims(ss).len();
    let rounds = match per_round {
        0 => ss.refine_rounds,
        r => ss.refine_rounds.min(TYPICAL_ROUNDS).min(budget / 2 / r),
    };
    let samples = budget.saturating_sub(rounds * per_round).max(MIN_SIZED_SAMPLES).min(ss.samples);
    (samples, rounds)
}

/// Fewest samples [`sized`] leaves: below this the space is barely covered.
pub const MIN_SIZED_SAMPLES: usize = 16;

/// Refine rounds a search typically runs before [`MIN_STEP`] stops it (the team's 2026-10-20
/// settings, 7 free factors: 19).
const TYPICAL_ROUNDS: usize = 24;

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
fn lhs(ss: &ShapeSearch, board: Option<(f64, f64)>, n: usize) -> Vec<[f64; SHAPE_DIMS]> {
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
    pts.into_iter().map(|v| normalize(ss, board, v)).collect()
}

struct Eval {
    score: f64,
    rep: Candidate,
    /// The inner search's re-checks at 2n-1 stations.
    recheck: crate::optimize::Recheck,
}

/// f64s through JSON bit for bit (the phone app's workers trade shapes as JSON): each float as
/// the string `"f64:<hex bits>"`, read back through a `serde_json::Value`, which never re-parses
/// a decimal. (serde_json's `float_roundtrip` would do it too, but it changes how every hull file
/// parses: a last-bit change that moves seat ties.)
pub(crate) mod exact {
    use serde_json::{Number, Value};

    pub fn encode(v: Value) -> Value {
        match v {
            Value::Number(n) if n.is_f64() => Value::String(to_hex(n.as_f64().expect("is_f64"))),
            Value::Array(a) => Value::Array(a.into_iter().map(encode).collect()),
            Value::Object(o) => Value::Object(o.into_iter().map(|(k, v)| (k, encode(v))).collect()),
            v => v,
        }
    }

    pub fn decode(v: Value) -> Result<Value, String> {
        Ok(match v {
            Value::String(s) if s.starts_with("f64:") => {
                Value::Number(Number::from_f64(from_hex(&s)?).ok_or_else(|| format!("{s}: not finite"))?)
            }
            Value::Array(a) => Value::Array(a.into_iter().map(decode).collect::<Result<_, _>>()?),
            Value::Object(o) => {
                Value::Object(o.into_iter().map(|(k, v)| Ok((k, decode(v)?))).collect::<Result<_, String>>()?)
            }
            v => v,
        })
    }

    /// One f64 as `"f64:<hex bits>"` (a string: JavaScript numbers cannot hold 64-bit integers).
    pub fn to_hex(x: f64) -> String {
        format!("f64:{:016x}", x.to_bits())
    }

    pub fn from_hex(s: &str) -> Result<f64, String> {
        let h = s.strip_prefix("f64:").ok_or_else(|| format!("{s}: not f64:<hex>"))?;
        u64::from_str_radix(h, 16).map(f64::from_bits).map_err(|e| format!("{s}: {e}"))
    }

    /// `[f64; N]` as [`to_hex`] strings.
    pub mod bits {
        use serde::{Deserialize, Deserializer, Serializer};

        pub fn serialize<S: Serializer, const N: usize>(v: &[f64; N], s: S) -> Result<S::Ok, S::Error> {
            s.collect_seq(v.iter().map(|&x| super::to_hex(x)))
        }

        pub fn deserialize<'de, D: Deserializer<'de>, const N: usize>(d: D) -> Result<[f64; N], D::Error> {
            let v = Vec::<String>::deserialize(d)?
                .iter()
                .map(|s| super::from_hex(s))
                .collect::<Result<Vec<f64>, _>>()
                .map_err(serde::de::Error::custom)?;
            v.try_into().map_err(|_| serde::de::Error::custom(format!("need {N} factors")))
        }
    }
}

/// One shape to score: its index in generation order and its factors (as their bits in JSON).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ShapeJob {
    pub index: usize,
    #[serde(with = "exact::bits")]
    pub v: [f64; SHAPE_DIMS],
}

/// A scored shape as it travels between workers (JSON), exactly: the score as
/// [`exact::to_hex`] (a shape with no passing wall scores +inf, which JSON has no number for),
/// the rest through [`exact`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShapeEval {
    pub job: ShapeJob,
    score: String,
    /// `(rep, rep.shape.metrics, recheck)` [`exact`]-encoded; the metrics apart, as a shape read
    /// from JSON drops them (canoe.toml may not set them).
    payload: serde_json::Value,
}

impl ShapeEval {
    fn new(job: ShapeJob, mut e: Eval) -> Result<ShapeEval> {
        let metrics = e.rep.shape.as_mut().and_then(|s| s.metrics.take());
        let payload = exact::encode(serde_json::to_value((&e.rep, &metrics, &e.recheck))?);
        Ok(ShapeEval { job, score: exact::to_hex(e.score), payload })
    }

    fn eval(&self) -> Result<Eval> {
        let v = exact::decode(self.payload.clone())
            .map_err(|e| anyhow::anyhow!("shape {}: {e}", self.job.index))?;
        let (mut rep, metrics, recheck): (Candidate, Option<ShapeMetrics>, crate::optimize::Recheck) =
            serde_json::from_value(v).with_context(|| format!("shape {}", self.job.index))?;
        if let Some(s) = &mut rep.shape {
            s.metrics = metrics;
        }
        let score =
            exact::from_hex(&self.score).map_err(|e| anyhow::anyhow!("shape {}: {e}", self.job.index))?;
        Ok(Eval { score, rep, recheck })
    }

    /// The shape's representative candidate (progress display).
    pub fn candidate(&self) -> Result<Candidate> {
        Ok(self.eval()?.rep)
    }
}

/// What every shape's score needs, worked out once per search: the straight/turn normalizers
/// and the race paths.
pub struct Prep {
    refs: Option<Refs>,
    plan: Option<race::Plan>,
}

/// Check the `[shape_search]` and work out its [`Prep`].
pub fn prepare(params: &Params) -> Result<Prep> {
    let ss = params.shape_search.as_ref().context("no [shape_search] in params")?;
    for (n, [lo, hi]) in ss.bounds() {
        anyhow::ensure!(lo.is_finite() && hi.is_finite() && lo <= hi, "shape_search.{n}: need min <= max");
    }
    anyhow::ensure!(ss.samples >= 1, "shape_search.samples must be >= 1");
    let refs = if params.objective.kind == ObjectiveKind::Drag && params.objective.straight_weight < 1.0 {
        Some(refs(params, ss)?)
    } else {
        None
    };
    let plan = if params.objective.kind == ObjectiveKind::Race { Some(race_plan(params, ss)?) } else { None };
    Ok(Prep { refs, plan })
}

/// Score one shape (a phone-app worker): what [`run_controlled`]'s workers do for it.
pub fn score_shape(params: &Params, prep: &Prep, job: &ShapeJob) -> Result<ShapeEval> {
    let ss = params.shape_search.as_ref().context("no [shape_search] in params")?;
    let e = eval_shape(params, ss, prep.refs, prep.plan.as_ref(), &job.v, job.index)?;
    ShapeEval::new(job.clone(), e)
}

/// A batch of shapes [`run_step`] has no score for yet.
#[derive(Debug)]
struct NeedShapes(Vec<ShapeJob>);

impl std::fmt::Display for NeedShapes {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} shapes to score", self.0.len())
    }
}

impl std::error::Error for NeedShapes {}

/// Where the shapes of a batch get their scores.
enum Exec<'a> {
    /// Worker threads here ([`on_workers`]).
    Threads,
    /// Scored elsewhere. Stopped: shapes not given are not started.
    Given { evals: &'a [ShapeEval], stopped: bool },
}

/// [`run_step`]'s answer.
pub enum Step {
    /// Score these (any order, anywhere), then call again with them added.
    Need(Vec<ShapeJob>),
    Done(Box<RunResult>),
}

/// One step of a shape search whose shapes are scored elsewhere (see the module docs).
/// `stopped` = the user ended the search: what is given is all there is, as [`run_controlled`]
/// after `RunControl::stop`.
pub fn run_step(params: &Params, prep: &Prep, evals: &[ShapeEval], stopped: bool) -> Result<Step> {
    let ctl = RunControl::default();
    if stopped {
        ctl.stop();
    }
    match search(params, prep, &|_| {}, &ctl, Exec::Given { evals, stopped }) {
        Ok(r) => Ok(Step::Done(Box::new(r))),
        Err(e) => match e.downcast::<NeedShapes>() {
            Ok(n) => Ok(Step::Need(n.0)),
            Err(e) => Err(e),
        },
    }
}

/// Normalizers for the straight/turning blend: straight drag and steady-turn
/// drag (N) of the fixed reference shape, the midpoint of the bounds.
#[derive(Clone, Copy)]
pub struct Refs {
    drag_n: f64,
    turn_n: f64,
}

fn refs(params: &Params, ss: &ShapeSearch) -> Result<Refs> {
    let mid = normalize(ss, board(params), ss.bounds().map(|(_, [lo, hi])| 0.5 * (lo + hi)));
    let s = to_shape(&mid);
    let hull = generate(&s, STATIONS, WATERLINES, "reference")?;
    // At the table top (the shape's draft less the keel lift `generate` removes).
    let draft = hull.draft_design;
    let drag_n = resistance(&hull, draft, params.objective.speed_mps, params.hull.roughness_um)
        .context("drag of the reference (midpoint) shape")?
        .total_n;
    let lb = displaced_volume_in3(&hull, draft) * WATER_PCF / IN3_PER_FT3;
    let turn_n = turn_at(params, &s, draft, lb).context("turn of the reference (midpoint) shape")?.drag_n;
    anyhow::ensure!(drag_n > 0.0 && turn_n > 0.0, "reference shape has zero straight or turning drag");
    Ok(Refs { drag_n, turn_n })
}

/// Race paths for the shape scores: planned on the reference (midpoint) shape
/// with its inner winner's wall (or its representative candidate's, so the
/// plan exists even when that shape has no passing wall).
fn race_plan(params: &Params, ss: &ShapeSearch) -> Result<race::Plan> {
    let rp = params.race.as_ref().context("objective.kind = race needs [race]")?;
    let mid = normalize(ss, board(params), ss.bounds().map(|(_, [lo, hi])| 0.5 * (lo + hi)));
    let s = to_shape(&mid);
    let hull = templated(params, generate(&s, STATIONS, WATERLINES, "reference")?)?;
    let params = &for_shape(params, &s);
    let r = run_inner(params, &hull)?;
    let c = representative(&r).context("reference shape: inner search produced no candidates")?;
    let top = race::topsides(&hull, c.depth_in, crate::optimize::candidate_topsides(params, &c));
    race::plan(&top, c.hull_weight_lb, rp).context("race paths on the reference (midpoint) shape")
}

fn score_of(params: &Params, r: &Inner, m: &ShapeMetrics, refs: Option<Refs>, race_s: Option<f64>) -> f64 {
    if m.rejected.is_some() {
        return f64::INFINITY;
    }
    match &r.best {
        None => f64::INFINITY,
        Some(w) => match params.objective.kind {
            ObjectiveKind::Weight => w.hull_weight_lb,
            ObjectiveKind::Race => race_s.unwrap_or(f64::INFINITY),
            ObjectiveKind::Drag => {
                let d = w.drag_n.unwrap_or(f64::INFINITY);
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
fn representative(r: &Inner) -> Option<Candidate> {
    if let Some(w) = &r.best {
        return Some(w.clone());
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
    let unbuildable = unbuildable.or_else(|| board_reason(params, &to_shape(v), &hull));
    let mut shape = to_shape(v);
    let params = &for_shape(params, &shape);
    shape.gunwale_radius_in = params.hull.gunwale_radius_in;
    let r = run_inner(params, &hull)?;
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
    if let (Some(pl), Some(rp), Some(c)) = (plan, &params.race, &r.best)
        && m.rejected.is_none()
    {
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
    Ok(Eval { score, rep, recheck: r.recheck })
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
    let prep = prepare(params)?;
    search(params, &prep, on_progress, ctl, Exec::Threads)
}

fn search(
    params: &Params,
    prep: &Prep,
    on_progress: &(dyn Fn(Progress) + Sync),
    ctl: &RunControl,
    exec: Exec,
) -> Result<RunResult> {
    let ss = params.shape_search.as_ref().context("no [shape_search] in params")?;
    let b = ss.bounds();
    let (refs, plan) = (prep.refs, prep.plan.as_ref());
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
        let out: Vec<Option<Result<Eval>>> = match &exec {
            Exec::Threads => on_workers(batch.len(), ctl, &|i| {
                let (v, index) = (&batch[i], base + i);
                on_progress(Progress::Shape { index, shapes: total, shape: to_shape(v) });
                let e = eval_shape(params, ss, refs, plan, v, index);
                if let Ok(e) = &e {
                    let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                    on_progress(Progress::Candidate { done: k, total, candidate: e.rep.clone() });
                }
                e
            })?,
            Exec::Given { evals, stopped } => {
                let mut out = Vec::with_capacity(batch.len());
                let mut need = Vec::new();
                for (i, v) in batch.iter().enumerate() {
                    let index = base + i;
                    match evals.iter().find(|e| e.job.index == index) {
                        Some(e) => {
                            anyhow::ensure!(e.job.v == *v, "shape {index} was scored with other factors");
                            out.push(Some(e.eval()));
                        }
                        None if *stopped => out.push(None),
                        None => need.push(ShapeJob { index, v: *v }),
                    }
                }
                if !need.is_empty() {
                    return Err(NeedShapes(need).into());
                }
                out
            }
        };
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
        lhs(ss, board(params), ss.samples).into_iter().filter(|v| seen.insert(key(v))).collect();
    batch_eval(init, &mut shapes, &mut evals)?;

    let ranked = |evals: &[Eval]| {
        let mut idx: Vec<usize> = (0..evals.len()).collect();
        idx.sort_by(|&a, &b| evals[a].score.total_cmp(&evals[b].score).then(a.cmp(&b)));
        idx
    };
    let mut step = 1.0 / 8.0;
    let mut rounds = 0;
    for _ in 0..ss.refine_rounds {
        if ctl.stopped() || step < MIN_STEP {
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
                    let v = normalize(ss, board(params), v);
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
    for e in &evals {
        r.recheck.checked += e.recheck.checked;
        r.recheck.failed += e.recheck.failed;
    }
    if let Some(why) = shape.metrics.as_ref().and_then(|m| m.rejected.clone()) {
        // Every shape broke a [shape_search] constraint: no winner, never a rejected one. Its
        // design is shown as the closest, failing that constraint.
        if let Some(w) = r.winner.take() {
            r.closest = Some(crate::optimize::Closest { fails: vec![format!("shape: {why}")], design: w });
        }
    }
    if let Some(w) = &mut r.winner {
        w.candidate.shape = Some(shape.clone());
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
    if let Some(cl) = &mut r.closest {
        // Nothing passed on any shape: the best-ranked shape's closest design, with its shape.
        cl.design.candidate.shape = Some(shape.clone());
        cl.design.search_space = crate::optimize::search_space(params, &r.strength, &cl.design.candidate)?;
    }
    if r.winner.is_some() || r.closest.is_some() {
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

    #[test]
    fn board_mold_curved_bottom_is_whole_boards() {
        let mut p = Params::load(std::path::Path::new("examples/shape_opt.toml")).unwrap();
        p.search.mold_board_in = Some(1.875);
        p.search.thickness_in = [0.375, 0.375, 0.125];
        let ss = p.shape_search.as_mut().unwrap();
        ss.draft_step_in = None;
        ss.draft_in = [1.375, 8.375];
        let ss = ss.clone();
        let tb = board(&p);
        assert_eq!(tb, Some((0.375, 1.875)));
        let whole = |v: f64| (v / 1.875 - (v / 1.875).round()).abs() < 1e-9;
        for (i, v) in lhs(&ss, tb, 40).iter().enumerate() {
            let h = hull_for(&p, v, i).unwrap();
            let j = (h.draft_design - 0.375) / 1.875;
            assert!((j - j.round()).abs() < 1e-9 && j.round() >= 1.0, "{} {j}", h.draft_design);
            assert!((ss.draft_in[0] - 1e-9..=ss.draft_in[1] + 1e-9).contains(&v[2]), "{}", v[2]);
            let m = metrics(&to_shape(v));
            assert!(
                whole(m.rocker_bow_in) && whole(m.rocker_stern_in),
                "{} {}",
                m.rocker_bow_in,
                m.rocker_stern_in
            );
            assert!((ss.fore_rocker[0]..=ss.fore_rocker[1]).contains(&v[5]), "{}", v[5]);
            assert_eq!(board_reason(&p, &to_shape(v), &h), None);
        }
        // rocker_in pinned to one board: every shape gets exactly that rocker at both ends.
        let mut one = ss.clone();
        (one.rocker_in, one.fore_rocker, one.symmetric) = (Some([1.875, 1.875]), [0.0, 2.0], true);
        for v in lhs(&one, tb, 20) {
            let m = metrics(&to_shape(&v));
            assert!((m.rocker_bow_in - 1.875).abs() < 1e-9 && (m.rocker_stern_in - 1.875).abs() < 1e-9);
            assert_eq!(shape_metrics(&one, &to_shape(&v)).rejected, None);
        }
        // No board fits the bounds: the shape is left as sampled and rejected.
        let mut tight = ss.clone();
        tight.draft_in = [5.0, 5.5];
        let v = lhs(&tight, tb, 1)[0];
        let h = hull_for(&p, &v, 0).unwrap();
        assert!(board_reason(&p, &to_shape(&v), &h).unwrap().contains("whole mold boards"));
        p.search.mold_board_in = None;
        assert_eq!(board_reason(&p, &to_shape(&v), &h), None, "no board mold, no rule");
    }
}
