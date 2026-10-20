//! The search.
//!
//! 1. Expand `search.{thickness,density,depth}` into a grid of candidates.
//! 2. Evaluate every candidate against every load case in parallel (rayon).
//!    A candidate's D/C is the max over load cases. An `Err` from
//!    equilibrium marks that candidate failed with its reason; it does not
//!    abort the run.
//! 3. Winner = lightest candidate with dc <= gates.dc_search and
//!    gated freeboard >= gates.freeboard_min_in + freeboard_margin_in.
//!    With `search.depth_tol_in` / `flare_tol_deg`, a single-mix winner's depth
//!    and flare are then bisected below their grid values ([`refine_wall`]).
//! 4. Re-evaluate the winner at 2n-1 and 4n-3 stations and Richardson-
//!    extrapolate its D/C ([`convergence`]); `verified` = extrapolated (or,
//!    when irregular, the 2n-1) D/C <= dc_verify.
//! 5. Describe every searched variable (`search_space`: range, step, count,
//!    winner value, bound it sits on) and the winner density's lab-data
//!    provenance. `on_grid_edge` = a wall variable on a bound that is not a
//!    real limit (buildable floor, mix data, tested mixes).
//!
//! Must stay deterministic: same canoe.toml => byte-identical result.json.

use crate::check::{Verdict, check, check_zoned, first_moment_below, freeboard, freeboard_over};
use crate::equilibrium::{CrewLoad, Loads, solve, solve_weights};
use crate::hull::{Grid, Hull, Topsides};
use crate::hullgen::ShapeParams;
use crate::mix::{MixProvenance, StrengthFit};
use crate::params::{LoadCase, ObjectiveKind, Params, StrengthModel, range};
use crate::resistance::{resistance, weighted_resistance};
use crate::seats::{self, CaseSeats, SeatSource};
use crate::section::{
    Rect, Sections, ZoneLayout, ZonedSec, modulus_psi, normalize, sections, zoned_sections,
};
use anyhow::Result;
use rayon::prelude::*;
use serde::Serialize;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Condvar, Mutex};

#[derive(Debug, Clone, Serialize)]
pub struct Candidate {
    /// Hull length overall, in (from the hull table).
    pub length_in: f64,
    /// Maximum beam (2 x max half-breadth), in.
    pub beam_in: f64,
    /// Generator parameters when the hull was produced by the shape search;
    /// None for a hull loaded from file.
    pub shape: Option<ShapeParams>,
    pub thickness_in: f64,
    /// Mix density. With strength_model = "tested_mixes" this is the tested
    /// mix's density and `mix` names it.
    pub density_pcf: f64,
    /// Name of the tested mix used (strength_model = "tested_mixes"), else None.
    pub mix: Option<String>,
    pub depth_in: f64,
    /// Topside flare the wall was built with, deg (`hull.flare_deg`, or the
    /// `search.flare_deg` value tried).
    pub flare_deg: f64,
    /// Self weight from the section areas alone (trapz of A*rho/1728 over x).
    /// Computable even when equilibrium fails, so it is ALWAYS a real number:
    /// never NaN (serde would write NaN as `null`, a sentinel).
    pub hull_weight_lb: f64,
    /// Worst verdict over all load cases, or None if analysis failed.
    pub verdict: Option<Verdict>,
    /// Name of the governing load case.
    pub governing_case: Option<String>,
    /// Why the analysis failed, if it did.
    pub error: Option<String>,
    pub passes: bool,
    /// Loaded transverse metacentric height GM_T, in, worst load case.
    /// None until computed or if analysis failed. Gate: gm >= stability.gm_min_in.
    pub gm_in: Option<f64>,
    /// GM_T per load case (input order), in: the worst seat sample evaluated
    /// (all of them for a passing candidate). Empty until computed.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub gm_cases_in: Vec<f64>,
    /// GM_T of the empty canoe (no crew), in. Gate: >= stability.gm_empty_min_in.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gm_empty_in: Option<f64>,
    /// Total resistance at objective.speed_mps, N (wave + ITTC friction), worst
    /// (heaviest) load case, or weighted over objective.displacements. Computed for every
    /// analysed candidate whatever the objective; None if it could not be (a failure only
    /// when objective.kind = drag).
    pub drag_n: Option<f64>,
    /// Weighted race time, s, on the shape search's planned paths
    /// (`race::planned_time`). None unless objective.kind = race.
    pub race_s: Option<f64>,
    /// Swamped-flotation air pocket length needed at EACH end, in. None when
    /// `[flotation]` is absent or the pocket cannot be sized (then `error` says why).
    pub air_pocket_in: Option<f64>,
    /// Two-mix zones of this wall ([zones]); absent for a single-mix wall (then `density_pcf`
    /// is the mix; with zones it is the light one).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub zones: Option<ZoneLayout>,
    /// Tested mix of the dense zones (strength_model = "tested_mixes").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dense_mix: Option<String>,
}

/// `gate`'s search limit (gated or not: callers check [`Gate::limits`] first when it matters).
fn search_limit(p: &Params, gate: Gate) -> f64 {
    gate.limits(p).map_or(f64::NAN, |l| l.2)
}

/// ", search margin on X in" when `gate`'s search limit differs from its verification limit.
fn margin_note(p: &Params, gate: Gate) -> String {
    match gate.limits(p) {
        Some((v, _, s)) if s != v => format!(", search margin on {v} in"),
        _ => String::new(),
    }
}

/// The design gates. The search's pass test (search limits: the verification limit moved by the
/// one-sided search margin), the in-search re-check of the best candidate at 2n-1 stations
/// ([`fine_misses`]) and verification all go through this list, so no gate can be applied at
/// one resolution and not the other (tests: every gate is re-checked; every gate but the
/// overall beam, measured not to drift, has a search margin).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gate {
    Dc,
    Freeboard,
    LoadedGm,
    EmptyGm,
    AirPocket,
    Beam,
}

impl Gate {
    pub const ALL: [Gate; 6] =
        [Gate::Dc, Gate::Freeboard, Gate::LoadedGm, Gate::EmptyGm, Gate::AirPocket, Gate::Beam];

    /// Name in messages and the binding-constraint table.
    pub fn label(self) -> &'static str {
        match self {
            Gate::Dc => "D/C",
            Gate::Freeboard => "freeboard",
            Gate::LoadedGm => "loaded GM",
            Gate::EmptyGm => "empty GM",
            Gate::AirPocket => "air pocket",
            Gate::Beam => "overall beam",
        }
    }

    /// (limit at verification, true = the value must be at least it, limit in the search);
    /// None = not gated in this run. D/C: `dc_verify` / `dc_search`; the others move by their
    /// `gates.*_search_margin_in`; the overall beam (`shape_search.beam_in` max) has none.
    pub fn limits(self, p: &Params) -> Option<(f64, bool, f64)> {
        let g = &p.gates;
        match self {
            Gate::Dc => Some((g.dc_verify, false, g.dc_search)),
            Gate::Freeboard => Some((g.freeboard_required_in(), true, p.freeboard_search_in())),
            Gate::LoadedGm => Some((p.stability.gm_min_in, true, p.gm_search_in())),
            Gate::EmptyGm => p.stability.gm_empty_min_in.map(|m| (m, true, m + g.gm_empty_search_margin_in)),
            Gate::AirPocket => p
                .flotation
                .as_ref()
                .map(|f| (f.air_pocket_max_in, false, f.air_pocket_max_in - g.air_pocket_search_margin_in)),
            Gate::Beam => p.shape_search.as_ref().map(|s| (s.beam_in[1], false, s.beam_in[1])),
        }
    }

    /// `c`'s value; None = not computed (fails the gate).
    pub fn value(self, c: &Candidate) -> Option<f64> {
        match self {
            Gate::Dc => c.verdict.as_ref().map(|v| v.dc),
            Gate::Freeboard => c.verdict.as_ref().map(|v| v.freeboard_in),
            Gate::LoadedGm => c.gm_in,
            Gate::EmptyGm => c.gm_empty_in,
            Gate::AirPocket => c.air_pocket_in,
            Gate::Beam => Some(c.beam_in),
        }
    }

    /// Why `c` misses this gate, None = passes (or not gated): against the search limit when
    /// `search`, else the verification limit; `stations` names the resolution in the message.
    pub fn miss(self, p: &Params, c: &Candidate, search: bool, stations: usize) -> Option<String> {
        let (verify, at_least, search_lim) = self.limits(p)?;
        let lim = if search { search_lim } else { verify };
        let unit = if self == Gate::Dc { "" } else { " in" };
        match self.value(c) {
            Some(v) if (at_least && v >= lim) || (!at_least && v <= lim) => None,
            Some(v) => {
                let op = if at_least { "<" } else { ">" };
                let margin = if search && lim != verify {
                    format!(" (search margin on {verify}{unit})")
                } else {
                    String::new()
                };
                Some(format!("{} at {stations} stations {v:.4}{unit} {op} {lim}{unit}{margin}", self.label()))
            }
            None => Some(format!("{} not computed at {stations} stations", self.label())),
        }
    }
}

/// Every gate `c` misses at `stations` (search limits when `search`), in [`Gate::ALL`] order.
pub fn gate_misses(p: &Params, c: &Candidate, search: bool, stations: usize) -> Vec<String> {
    Gate::ALL.iter().filter_map(|g| g.miss(p, c, search, stations)).collect()
}

/// In-search re-checks of the best candidate at 2n-1 stations (`RunResult::recheck`).
#[derive(Debug, Clone, Copy, Default, Serialize)]
pub struct Recheck {
    /// Best candidates re-checked at 2n-1 stations.
    pub checked: usize,
    /// Of those, failed there (each was failed with the reason and the next best taken).
    pub failed: usize,
}

/// The candidate that comes closest when nothing passes ([`RunResult::closest`]): re-checked
/// with every load case and gate (no early exit) and analysed like a winner ([`finish_winner`]:
/// verification, every case, empty float, stands, curves, worked proof).
#[derive(Debug, Clone, Serialize)]
pub struct Closest {
    /// Every gate it misses, worst first, with its numbers.
    pub fails: Vec<String>,
    pub design: Winner,
}

/// The gates `c` misses, worst first, each with its numbers, and its score: the largest relative
/// miss over the gates ((demand - limit) / limit, or (limit - value) / limit; <= 0 when it passes
/// them all, infinite for an analysis error). Ranks candidates for [`Closest`]. The limits are the
/// search's ([`Gate::limits`]: with the search margins), so a candidate the search failed inside
/// a margin is listed with that miss.
pub fn shortfalls(params: &Params, c: &Candidate) -> (f64, Vec<String>) {
    let mut miss: Vec<(f64, String)> = Vec::new();
    if let Some(e) = &c.error {
        miss.push((f64::INFINITY, format!("analysis: {e}")));
    }
    let case = c.governing_case.as_deref().unwrap_or("-");
    match &c.verdict {
        None => miss.push((f64::INFINITY, "no verdict (the canoe could not be analysed)".into())),
        Some(v) => {
            let g = &params.gates;
            miss.push((
                v.dc / g.dc_search - 1.0,
                format!("strength: D/C {:.3} ({}, {case}) > {}", v.dc, v.governing_check, g.dc_search),
            ));
            let fb = search_limit(params, Gate::Freeboard);
            miss.push((
                (fb - v.freeboard_in) / fb.abs().max(1.0),
                format!(
                    "freeboard{}: {:.2} in < {fb} in (lowest load case{})",
                    if params.lettering.is_some() { " at the lettering" } else { "" },
                    v.freeboard_in,
                    margin_note(params, Gate::Freeboard)
                ),
            ));
        }
    }
    let st = &params.stability;
    match c.gm_in {
        Some(gm) => {
            let worst = c
                .gm_cases_in
                .iter()
                .enumerate()
                .min_by(|a, b| a.1.total_cmp(b.1))
                .and_then(|(i, _)| params.load_cases.get(i))
                .map_or(String::new(), |lc| format!(", {}", lc.name));
            let m = search_limit(params, Gate::LoadedGm);
            miss.push((
                (m - gm) / m.abs().max(1.0),
                format!(
                    "stability: loaded GM {gm:.2} in < {m} in{worst}{}",
                    margin_note(params, Gate::LoadedGm)
                ),
            ));
        }
        None if c.verdict.is_some() => miss.push((f64::INFINITY, "stability: loaded GM not computed".into())),
        None => {}
    }
    if st.gm_empty_min_in.is_some() {
        let m = search_limit(params, Gate::EmptyGm);
        match c.gm_empty_in {
            Some(g) => miss.push((
                (m - g) / m.abs().max(1.0),
                format!("stability: empty GM {g:.2} in < {m} in{}", margin_note(params, Gate::EmptyGm)),
            )),
            None => miss.push((f64::INFINITY, "stability: empty GM not computed".into())),
        }
    }
    let score = miss.iter().map(|m| m.0).fold(f64::NEG_INFINITY, f64::max);
    miss.retain(|m| m.0 > 0.0);
    miss.sort_by(|a, b| b.0.total_cmp(&a.0));
    (score, miss.into_iter().map(|m| m.1).collect())
}

#[derive(Debug, Clone, Serialize)]
pub struct Winner {
    pub candidate: Candidate,
    pub verify_stations: usize,
    pub verify_dc: f64,
    /// Gate: max(dc_extrapolated, D/C at 4n-3) <= dc_verify when the
    /// extrapolation is well behaved, else verify_dc <= dc_verify.
    pub verified: bool,
    /// Richardson-extrapolated D/C at infinite station resolution (max over
    /// load cases and checks), None when the series is not well behaved.
    pub dc_extrapolated: Option<f64>,
    /// Observed convergence order of the series that sets dc_extrapolated
    /// (None when that series had already converged).
    pub dc_order: Option<f64>,
    pub convergence: Convergence,
    pub on_grid_edge: bool,
    /// Winner density is at the edge of the range the mix data supports
    /// (lowest/highest tested density -/+ density_extrapolation_pcf). Not a
    /// search-range problem: the team needs lab data for lighter/heavier mixes.
    pub at_mix_data_edge: bool,
    /// Lab data behind the winner density: tested or predicted, nearest
    /// tested mixes and the distance to them ("reach").
    pub mix_provenance: MixProvenance,
    /// The dense mix's provenance (a zoned winner).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dense_mix_provenance: Option<MixProvenance>,
    /// Every searched variable: thickness, density, depth, then the free
    /// `[shape_search]` factors (shape runs). See [`search_space`].
    pub search_space: Vec<SearchAxis>,
    /// Along-hull diagrams for the winner at verification resolution, governing
    /// load case, for plotting.
    pub diagrams: Diagrams,
    /// Every load case at verification resolution, in `load_cases` order.
    pub cases: Vec<CaseDiagrams>,
    /// The bare hull floating with no crew, at verification resolution.
    pub empty: EmptyFloat,
    /// Every intermediate value behind the governing load case's checks
    /// (the report's calculation appendix).
    pub calc: CalcTrace,
    /// The same worked calculation for every load case (`params.load_cases` order), each at
    /// its own worst seat sample and strength combination: the PDF report's full proof.
    pub case_calcs: Vec<CalcTrace>,
    /// Why `verified` is false (station convergence or the stands case); None when verified.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verify_failure: Option<String>,
    /// `[stands]`: the empty hull on two stands at the best position ([`verify_stands`]).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stands: Option<crate::stands::StandsCase>,
    /// Knee punching D/C of the heaviest paddler at knee splits 0.5, 0.6 and 1.0.
    pub knee_split_sensitivity: Vec<crate::stands::SplitDc>,
    /// Power-speed and righting-lever curves of the winner ([`winner_curves`]); not gated.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub curves: Option<crate::curves::Curves>,
    /// Every gate with its slack, closest to binding first ([`gate_slack`]).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub gates: Vec<GateSlack>,
}

/// One gate of an analysed design and how close it is to binding (the report's binding
/// constraints table).
#[derive(Debug, Clone, Serialize)]
pub struct GateSlack {
    /// e.g. "loaded GM", "D/C bending_tension".
    pub name: String,
    /// Load case where it is worst; empty when it is not per case.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub at: String,
    pub value: f64,
    pub limit: f64,
    /// true: the value must be >= limit (a minimum); false: <= limit (a maximum).
    pub at_least: bool,
    /// Signed margin in the gate's units, >= 0 = passes: value - limit for a minimum,
    /// limit - value for a maximum.
    pub slack: f64,
    /// slack / max(|limit|, 1): the sort key (gates have different units).
    pub relative: f64,
    /// Stations the value is from: the verification count where the gate is checked there,
    /// else the search count.
    pub stations: usize,
}

/// The gates of design `c` (search values) with its verification `v` and `cases` (2n-1
/// stations) and station convergence `conv`, sorted by relative slack: verified D/C, each check's
/// search D/C, then every other gate of the shared list ([`Gate`]) at 2n-1.
fn gate_slack(
    params: &Params,
    c: &Candidate,
    v: &Candidate,
    cases: &[CaseDiagrams],
    conv: &Convergence,
) -> Vec<GateSlack> {
    let n = params.hull.stations;
    let (fine, finest) = (2 * n - 1, 4 * n - 3);
    let mut out = Vec::new();
    let mut gate = |name: String, at: &str, value: f64, limit: f64, at_least: bool, stations: usize| {
        let slack = if at_least { value - limit } else { limit - value };
        if slack.is_finite() {
            let relative = slack / limit.abs().max(1.0);
            out.push(GateSlack {
                name,
                at: at.to_string(),
                value,
                limit,
                at_least,
                slack,
                relative,
                stations,
            });
        }
    };
    let case = |pick: fn(&CaseDiagrams) -> Option<f64>| {
        cases
            .iter()
            .filter_map(|k| pick(k).map(|x| (x, k.name.as_str())))
            .min_by(|a, b| a.0.total_cmp(&b.0))
            .map_or("", |m| m.1)
    };
    let (g, dv) = (&params.gates, params.gates.dc_verify);
    match (conv.extrapolated, v.verdict.as_ref()) {
        (Some(x), _) => gate("D/C verified".into(), "", x.max(conv.dc[2]), dv, false, finest),
        (None, Some(vv)) => gate("D/C verified".into(), "", vv.dc, dv, false, fine),
        (None, None) => {}
    }
    if let Some(cv) = &c.verdict {
        for k in &cv.checks {
            let at = c.governing_case.as_deref().unwrap_or("");
            gate(format!("D/C {}", k.name), at, k.dc, g.dc_search, false, n);
        }
    }
    // The other gates of the shared list, at the verification stations and limits.
    for &gt in &Gate::ALL[1..] {
        let (Some((limit, at_least, _)), Some(value)) = (gt.limits(params), gt.value(v)) else { continue };
        let at = match gt {
            Gate::Freeboard => case(|k| k.freeboard_in),
            Gate::LoadedGm => case(|k| k.gm_in),
            _ => "",
        };
        gate(gt.label().into(), at, value, limit, at_least, fine);
    }
    out.sort_by(|a, b| a.relative.total_cmp(&b.relative));
    out
}

/// Worked calculation of the winner's governing load case at the seats its
/// verdict comes from and the strength combination that governs, at
/// verification resolution. Every number is recomputed by the same functions
/// the checks use, so each demand and capacity equals that case's verdict.
#[derive(Debug, Clone, Serialize, Default)]
pub struct CalcTrace {
    /// Set when the trace could not be computed; nothing else is then present.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(flatten)]
    pub calc: Option<Calc>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Calc {
    pub case: String,
    /// Knee x per paddler (`weights_lb` order) of the governing seat sample.
    pub seats_in: Vec<f64>,
    pub weights_lb: Vec<f64>,
    pub method: crate::params::DesignMethod,
    pub factor_of_safety: f64,
    pub dynamic_load_factor: f64,
    /// (dead, live) load factors of the governing combination.
    pub dead_factor: f64,
    pub live_factor: f64,
    /// Capacity multiplier: 1/FS (asd) or phi (lrfd); punching may differ.
    pub capacity_factor: f64,
    pub punching_factor: f64,
    pub stations: usize,
    pub length_in: f64,
    pub thickness_in: f64,
    pub density_pcf: f64,
    pub depth_in: f64,
    /// Integral of the shell area A(x) dx, in^3.
    pub wall_volume_in3: f64,
    /// Service equilibrium: real crew weight (freeboard, GM, drag).
    pub service: CalcFloat,
    /// Freeboard at the nominal seats and at the best and worst seat sample (the gate is worst).
    pub freeboard: FreeboardRange,
    /// Equilibrium the stress checks use (factored for lrfd).
    pub strength: CalcFloat,
    pub split_slope_psi_per_pcf: f64,
    pub split_intercept_psi: f64,
    pub split_psi: f64,
    /// "measured" or "derived" (see `StrengthFit::source`).
    pub flexural_source: &'static str,
    pub flex_over_split: f64,
    pub flexural_psi: f64,
    pub fc_source: &'static str,
    pub fc_psi: f64,
    pub lambda: f64,
    pub bending_tension: Option<CalcFibre>,
    pub bending_compression: Option<CalcFibre>,
    pub shear: Option<CalcShear>,
    pub punching: CalcPunching,
    pub biaxial: Option<CalcBiaxial>,
    /// Swamped flotation of the bare hull (with `[flotation]`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub flotation: Option<crate::flotation::Flotation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub swamp_factor: Option<f64>,
}

/// One equilibrium: weights, draft, trim, freeboard and GM parts.
#[derive(Debug, Clone, Serialize)]
pub struct CalcFloat {
    pub hull_weight_lb: f64,
    /// Crew as floated (weights x dynamic factor x live factor), lb.
    pub crew_lb: f64,
    pub weight_lb: f64,
    pub draft_in: f64,
    pub trim_in: f64,
    /// Highest local waterline (an end of the trimmed waterline), in.
    pub waterline_max_in: f64,
    pub freeboard_in: f64,
    pub kb_in: f64,
    pub bm_in: f64,
    pub kg_in: f64,
    pub gm_in: f64,
    pub crew_cg_in: f64,
}

/// Bending stress at one fibre of one station.
#[derive(Debug, Clone, Serialize)]
pub struct CalcFibre {
    pub x_in: f64,
    pub moment_lbin: f64,
    pub area_in2: f64,
    pub zbar_in: f64,
    pub inertia_in4: f64,
    /// Fibre distance from the neutral axis, in.
    pub c_in: f64,
    /// "bottom" or "top".
    pub fibre: &'static str,
    pub stress_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalcShear {
    pub x_in: f64,
    pub shear_lb: f64,
    pub zbar_in: f64,
    pub inertia_in4: f64,
    /// First moment of the shell below the axis, in^3.
    pub q_in3: f64,
    /// Web width, two walls: 2 t, in.
    pub b_in: f64,
    pub stress_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalcPunching {
    pub method: crate::params::PunchingMethod,
    /// Heaviest paddler x dynamic factor x live factor, lb.
    pub paddler_lb: f64,
    pub knees: u32,
    pub knee_split: f64,
    pub knee_spacing_in: f64,
    pub knee_patch_in: f64,
    pub pad_in: f64,
    /// d / t (aci_22_6), 1 for plain_14_5.
    pub depth_over_t: f64,
    #[serde(flatten)]
    pub calc: crate::check::PunchingCalc,
    pub capacity_psi: f64,
    pub dc: f64,
}

/// Kupfer-Gerstle at one station: the governing of the four sign combinations.
#[derive(Debug, Clone, Serialize)]
pub struct CalcBiaxial {
    pub x_in: f64,
    pub tension_psi: f64,
    pub compression_psi: f64,
    /// Transverse turning stress (0 without `[turning]`), psi.
    pub transverse_psi: f64,
    pub shear_psi: f64,
    /// sigma_x and sigma_y of the governing combination.
    pub sigma_x_psi: f64,
    pub sigma_y_psi: f64,
    pub sigma_1_psi: f64,
    pub sigma_2_psi: f64,
    /// f_t (1 - 0.8 |sigma_2| / f'c) with f_t, f'c already x capacity factor.
    pub ft_psi: f64,
    pub fc_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

/// Build the calculation trace of `case` (the winner's governing case).
#[allow(clippy::too_many_arguments)]
pub fn calc_trace(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case_name: &str,
    seats: &CaseSeats,
) -> CalcTrace {
    match calc(params, fit, grid, sec, t, rho, case_name, seats) {
        Ok(c) => CalcTrace { error: None, calc: Some(c) },
        Err(e) => CalcTrace { error: Some(format!("{e:#}")), calc: None },
    }
}

#[allow(clippy::too_many_arguments)]
fn calc(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case_name: &str,
    seats: &CaseSeats,
) -> Result<Calc> {
    use crate::check::{first_moment_below, hoop_psi, punching_calc};
    use crate::params::{DesignMethod, PunchingMethod};
    let case = params
        .load_cases
        .iter()
        .find(|c| c.name == case_name)
        .ok_or_else(|| anyhow::anyhow!("unknown load case {case_name}"))?;
    let mat = &params.material;
    // The checks use the thickness recovered from the section (equal to t up to rounding).
    let t = crate::check::thickness(grid, sec).unwrap_or(t);
    let env = eval_envelope(params, fit, grid, sec, rho, case, &seats.seats_in, false)?;
    let e = eval_case(params, fit, grid, sec, rho, case, &env.verdict_seats_in)?;
    let (dead, live) = e.combo;
    let pu = &params.punching;
    let (k, k_punch) = match mat.method {
        DesignMethod::Asd => (1.0 / mat.factor_of_safety, 1.0 / mat.factor_of_safety),
        DesignMethod::Lrfd => (0.60, if pu.method == PunchingMethod::Aci22_6 { 0.75 } else { 0.60 }),
    };
    let crew_total: f64 = case.weights_lb.iter().sum::<f64>() * mat.dynamic_load_factor;
    let float = |l: &Loads, lf: f64| CalcFloat {
        hull_weight_lb: l.hull_weight_lb,
        crew_lb: crew_total * lf,
        weight_lb: l.weight_lb,
        draft_in: l.draft_in,
        trim_in: l.trim_in,
        waterline_max_in: waterline(grid, l).iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        freeboard_in: freeboard(grid, l),
        kb_in: l.kb_in,
        bm_in: l.bm_in,
        kg_in: l.kg_in,
        gm_in: l.gm_in,
        crew_cg_in: params.stability.crew_cg_in,
    };
    let loads = &e.strength;
    let v = &e.verdict;
    let (ft_cap, fc) = (fit.flexural_psi(rho) * k, fit.fc_psi(rho));
    let lambda = 1.0;
    let station = |name: &str| -> Option<usize> {
        let x = v.checks.iter().find(|c| c.name == name)?.x_in?;
        grid.x.iter().position(|&xi| xi == x)
    };
    let fibre = |name: &str, tension: bool| -> Option<CalcFibre> {
        let i = station(name)?;
        let (m, zb, ii) = (loads.moment[i], sec.zbar[i], sec.inertia[i]);
        let (c_top, c_bot) = (grid.depth - zb, zb);
        let (ct, cc) = crate::check::fibres(m, c_top, c_bot);
        let c = if tension { ct } else { cc };
        // Sagging (M >= 0) puts the bottom in tension, hogging the top.
        let bottom = (m >= 0.0) == tension;
        let cap = if tension {
            ft_cap
        } else if fc > 0.0 {
            0.85 * fc * k
        } else {
            f64::NAN
        };
        let s = m.abs() * c / ii;
        Some(CalcFibre {
            x_in: grid.x[i],
            moment_lbin: m,
            area_in2: sec.area[i],
            zbar_in: zb,
            inertia_in4: ii,
            c_in: c,
            fibre: if bottom { "bottom" } else { "top" },
            stress_psi: s,
            capacity_psi: cap,
            dc: s / cap,
        })
    };
    let shear = station("shear").map(|i| {
        let q = first_moment_below(grid.row(i), &grid.z, t, sec.zbar[i]);
        let tau = loads.shear[i].abs() * q / (sec.inertia[i] * 2.0 * t);
        let cap = if fc > 0.0 { 2.0 * lambda * fc.sqrt() * k } else { f64::NAN };
        CalcShear {
            x_in: grid.x[i],
            shear_lb: loads.shear[i],
            zbar_in: sec.zbar[i],
            inertia_in4: sec.inertia[i],
            q_in3: q,
            b_in: 2.0 * t,
            stress_psi: tau,
            capacity_psi: cap,
            dc: tau / cap,
        }
    });
    let max_paddler =
        case.weights_lb.iter().cloned().fold(f64::NEG_INFINITY, f64::max) * mat.dynamic_load_factor * live;
    let pc = punching_calc(max_paddler, t, fc, lambda, mat.knee_patch_in, pu);
    let p_cap = pc.v_nominal * k_punch;
    let punching = CalcPunching {
        method: pu.method,
        paddler_lb: max_paddler,
        knees: pu.knees,
        knee_split: pu.knee_split,
        knee_spacing_in: pu.knee_spacing_in,
        knee_patch_in: mat.knee_patch_in,
        pad_in: pu.pad_in,
        depth_over_t: pc.depth_in / t,
        dc: pc.v_u / p_cap,
        capacity_psi: p_cap,
        calc: pc,
    };
    let fc_k = if fc > 0.0 { fc * k } else { f64::NAN };
    let hoop = hoop_psi(grid, loads, t, params.turning.as_ref());
    let biaxial = station("biaxial").map(|i| {
        let (m, zb, ii) = (loads.moment[i], sec.zbar[i], sec.inertia[i]);
        let (ct, cc) = crate::check::fibres(m, grid.depth - zb, zb);
        let (st, sc) = (m.abs() * ct / ii, m.abs() * cc / ii);
        let q = first_moment_below(grid.row(i), &grid.z, t, zb);
        let tau = loads.shear[i].abs() * q / (ii * 2.0 * t);
        let sy = hoop[i];
        // Same four sign combinations as check::kupfer_dc; keep the worst.
        let mut best: Option<CalcBiaxial> = None;
        for (sx, sy) in [(st, sy), (-sc, sy), (st, -sy), (-sc, -sy)] {
            let r = ((0.5 * (sx - sy)).powi(2) + tau * tau).sqrt();
            let c = 0.5 * (sx + sy);
            let (s1, s2) = (c + r, c - r);
            let cap = ft_cap * (1.0 - 0.8 * s2.min(0.0).abs() / fc_k);
            let dc = if cap.is_nan() || cap <= 0.0 || !cap.is_finite() {
                f64::INFINITY
            } else {
                s1.max(0.0) / cap
            };
            if best.as_ref().is_none_or(|b| dc > b.dc) {
                best = Some(CalcBiaxial {
                    x_in: grid.x[i],
                    tension_psi: st,
                    compression_psi: sc,
                    transverse_psi: hoop[i],
                    shear_psi: tau,
                    sigma_x_psi: sx,
                    sigma_y_psi: sy,
                    sigma_1_psi: s1,
                    sigma_2_psi: s2,
                    ft_psi: ft_cap,
                    fc_psi: fc_k,
                    capacity_psi: cap,
                    dc,
                });
            }
        }
        best
    });
    let n = grid.nx();
    let flotation = match &params.flotation {
        Some(fp) => Some(crate::flotation::flotation(grid, sec, rho, fp.swamp_factor, fp.air_pocket_max_in)?),
        None => None,
    };
    Ok(Calc {
        case: case.name.clone(),
        seats_in: env.verdict_seats_in.clone(),
        weights_lb: case.weights_lb.clone(),
        method: mat.method,
        factor_of_safety: mat.factor_of_safety,
        dynamic_load_factor: mat.dynamic_load_factor,
        dead_factor: dead,
        live_factor: live,
        capacity_factor: k,
        punching_factor: k_punch,
        stations: n,
        length_in: grid.x[n - 1] - grid.x[0],
        thickness_in: t,
        density_pcf: rho,
        depth_in: grid.depth,
        wall_volume_in3: (1..n)
            .map(|i| 0.5 * (sec.area[i] + sec.area[i - 1]) * (grid.x[i] - grid.x[i - 1]))
            .sum(),
        service: float(&e.service, 1.0 / mat.dynamic_load_factor),
        freeboard: env.freeboard.clone(),
        strength: float(loads, live),
        split_slope_psi_per_pcf: fit.split_slope_psi_per_pcf,
        split_intercept_psi: fit.split_intercept_psi,
        split_psi: fit.split_psi(rho),
        flexural_source: fit.source(crate::mix::Property::Flexural),
        flex_over_split: fit.flex_over_split,
        flexural_psi: fit.flexural_psi(rho),
        fc_source: fit.source(crate::mix::Property::Compressive),
        fc_psi: fc,
        lambda,
        bending_tension: fibre("bending_tension", true),
        bending_compression: fibre("bending_compression", false),
        shear,
        punching,
        biaxial: biaxial.flatten(),
        flotation,
        swamp_factor: params.flotation.as_ref().map(|f| f.swamp_factor),
    })
}

/// Unloaded (no crew) equilibrium of the winner: hull self weight only.
#[derive(Debug, Clone, Serialize, Default)]
pub struct EmptyFloat {
    /// Set when equilibrium failed; the numbers below are then absent/empty.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub draft_in: Option<f64>,
    pub trim_in: Option<f64>,
    /// Minimum freeboard over the stations, in.
    pub freeboard_in: Option<f64>,
    /// Transverse metacentric height GM_T of the empty canoe, in.
    pub gm_in: Option<f64>,
    pub x_in: Vec<f64>,
    /// Local waterline height above keel, in.
    pub waterline_in: Vec<f64>,
}

/// Float the bare hull (no crew) and report draft, trim, freeboard and waterline.
pub fn empty_float(params: &Params, grid: &Grid, sec: &Sections, rho: f64) -> EmptyFloat {
    empty_float_wall(params, grid, sec, Wall::Uniform(rho))
}

/// [`empty_float`] for any wall.
pub fn empty_float_wall(params: &Params, grid: &Grid, sec: &Sections, wall: Wall) -> EmptyFloat {
    let (patch, cg) = (params.material.knee_patch_in, params.stability.crew_cg_in);
    match wall.solve(grid, sec, 1.0, &[], patch, cg) {
        Ok(loads) => EmptyFloat {
            error: None,
            draft_in: Some(loads.draft_in),
            trim_in: Some(loads.trim_in),
            freeboard_in: Some(freeboard(grid, &loads)),
            gm_in: Some(loads.gm_in),
            x_in: grid.x.clone(),
            waterline_in: waterline(grid, &loads),
        },
        Err(e) => EmptyFloat { error: Some(format!("{e:#}")), ..Default::default() },
    }
}

/// Local waterline height above keel per station, clamped to [0, depth].
fn waterline(grid: &Grid, loads: &Loads) -> Vec<f64> {
    let l = grid.x[grid.nx() - 1];
    grid.x
        .iter()
        .map(|&x| (loads.draft_in + loads.trim_in * (x - l / 2.0) / l).clamp(0.0, grid.depth))
        .collect()
}

/// One load case of the winner: resolved seats, equilibrium, stresses and
/// per-station D/C (0 at masked tip stations; same formulas as check.rs).
#[derive(Debug, Clone, Serialize, Default)]
pub struct CaseDiagrams {
    pub name: String,
    /// Nominal knee x per paddler (`weights_lb` order), in from bow.
    pub seats_in: Vec<f64>,
    /// Paddler indices bow to stern.
    pub crew_order: Vec<usize>,
    /// given | even | auto (None only in hand-built values).
    pub seat_source: Option<SeatSource>,
    /// Knees checked at +/- this (`seats.variance_in`).
    pub seat_variance_in: f64,
    /// Seats of the sample `verdict` comes from (= seats_in when variance is 0).
    pub verdict_seats_in: Vec<f64>,
    pub weights_lb: Vec<f64>,
    /// Set when equilibrium failed for this case; all numbers below are then
    /// absent/empty (never a sentinel). With an error, verdict is None.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// Draft and trim at the nominal seats; GM and freeboard the worst over
    /// the seat samples (what the gates see). Diagrams and per-station D/C are
    /// at the nominal seats, `verdict` is the worst sample.
    pub draft_in: Option<f64>,
    pub trim_in: Option<f64>,
    pub gm_in: Option<f64>,
    pub freeboard_in: Option<f64>,
    /// Freeboard at the nominal seats (expected), the best and the worst seat sample.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub freeboard: Option<FreeboardRange>,
    /// x, V, M, tension, waterline etc. (same shape as `Winner.diagrams`).
    #[serde(flatten)]
    pub diagrams: Diagrams,
    pub compression_psi: Vec<f64>,
    pub shear_psi: Vec<f64>,
    pub compression_allow_psi: Option<f64>,
    pub shear_allow_psi: Option<f64>,
    pub dc_bending_tension: Vec<f64>,
    pub dc_bending_compression: Vec<f64>,
    pub dc_shear: Vec<f64>,
    /// Kupfer-Gerstle biaxial D/C per station (`check::kupfer_dc` on this
    /// station's tension/compression fibre stress and shear stress).
    pub dc_biaxial: Vec<f64>,
    pub verdict: Option<Verdict>,
}

/// Per-station arrays (same length as `x_in`) for the web plot tabs.
#[derive(Debug, Clone, Serialize, Default)]
pub struct Diagrams {
    pub x_in: Vec<f64>,
    /// Shear, lb.
    pub shear_lb: Vec<f64>,
    /// Bending moment, lb*in (positive = sagging).
    pub moment_lbin: Vec<f64>,
    /// Bending tension stress |M| c / I, psi (0 at masked tip stations).
    pub tension_psi: Vec<f64>,
    /// Allowable bending tension (capacity / FS), psi, constant along the hull.
    pub tension_allow_psi: f64,
    /// Wall cross-section area, in^2, and second moment, in^4.
    pub area_in2: Vec<f64>,
    pub inertia_in4: Vec<f64>,
    /// Local waterline height above keel, in.
    pub waterline_in: Vec<f64>,
    /// Neutral axis height above the keel, in (transformed section for a two-mix wall).
    pub zbar_in: Vec<f64>,
    /// Distributed loads of the strength combination, lb/in: buoyancy and self weight
    /// (as factored); their difference integrates to shear_lb (knee patches added in closed form).
    pub buoyancy_lbpin: Vec<f64>,
    pub self_weight_lbpin: Vec<f64>,
}

/// End of a searched range.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Bound {
    Min,
    Max,
}

/// One searched variable and where the winner landed on it.
#[derive(Debug, Clone, Serialize)]
pub struct SearchAxis {
    /// `search.*` or `shape_search.*` key.
    pub name: &'static str,
    /// Lowest and highest value tried (wall axes after the buildable floor and
    /// the mix-data clip; stepped shape axes snapped to their step).
    pub min: f64,
    pub max: f64,
    /// Grid step. None = continuous (shape factors) or the tested-mix list.
    pub step: Option<f64>,
    /// Values on the axis. None = continuous.
    pub count: Option<usize>,
    pub winner: f64,
    /// Range end the winner sits on. None = inside (or a one-value axis).
    pub at_bound: Option<Bound>,
    /// Why that end is a real limit, not a range to widen: "buildable floor",
    /// "mix data" (no lab data further out), "tested mixes", "demolding draft"
    /// (low end of `search.flare_deg`). None = widen it.
    pub limit: Option<&'static str>,
}

/// Searched thicknesses: `search.thickness_in` from the buildable floor up.
fn thicknesses(params: &Params) -> Vec<f64> {
    let floor = params.gates.thickness_min_buildable_in;
    range(params.search.thickness_in).into_iter().filter(|&t| t >= floor - 1e-9).collect()
}

/// The search space around candidate `c`: thickness, density, depth (the
/// values [`run`] actually tried), then for a shape run (`c.shape` set) each
/// free `[shape_search]` factor (fixed ones and symmetric-tied aft ones are
/// left out). Wall bounds compare exactly (the winner is a grid value); shape
/// bounds within 1e-9 relative.
pub fn search_space(params: &Params, fit: &StrengthFit, c: &Candidate) -> Result<Vec<SearchAxis>> {
    let at = |x: f64, r: &[f64]| match r {
        [lo, .., hi] if x == *lo => Some(Bound::Min),
        [_, .., hi] if x == *hi => Some(Bound::Max),
        _ => None,
    };
    let axis = |name, r: &[f64], step, x| SearchAxis {
        name,
        min: r[0],
        max: r[r.len() - 1],
        step,
        count: Some(r.len()),
        winner: x,
        at_bound: at(x, r),
        limit: None,
    };
    let s = &params.search;
    let ts = thicknesses(params);
    anyhow::ensure!(!ts.is_empty(), "no searched thickness at or above thickness_min_buildable_in");
    let mut t = axis("thickness_in", &ts, Some(s.thickness_in[2]), c.thickness_in);
    // Thickness at the buildable floor is a real constraint, not a range problem.
    if (c.thickness_in - params.gates.thickness_min_buildable_in).abs() <= 1e-9 {
        t.limit = t.at_bound.map(|_| "buildable floor");
    }
    let rhos: Vec<f64> = density_axis(params, fit)?.iter().map(|a| a.0).collect();
    let rho = match params.material.strength_model {
        StrengthModel::TestedMixes => {
            let mut a = axis("density_pcf", &rhos, None, c.density_pcf);
            a.limit = a.at_bound.map(|_| "tested mixes");
            a
        }
        StrengthModel::Fit => {
            // Density at the end of the trusted fit range, where the requested
            // range extended further: the lab data, not the search range, is the limit.
            let full = range(s.density_pcf);
            let (lo, hi) = (rhos[0], rhos[rhos.len() - 1]);
            let mut a = axis("density_pcf", &rhos, Some(s.density_pcf[2]), c.density_pcf);
            if c.density_pcf == lo && full[0] < lo - 1e-9 {
                (a.at_bound, a.limit) = (Some(Bound::Min), Some("mix data"));
            } else if c.density_pcf == hi && full[full.len() - 1] > hi + 1e-9 {
                (a.at_bound, a.limit) = (Some(Bound::Max), Some("mix data"));
            }
            a
        }
    };
    let ds = range(s.depth_in);
    let mut out = vec![t, rho, axis("depth_in", &ds, Some(s.depth_in[2]), c.depth_in)];
    if let Some(fr) = s.flare_deg {
        let mut a = axis("flare_deg", &range(fr), Some(fr[2]), c.flare_deg);
        // The low end is the team's demolding draft: a real limit, not a range to widen.
        if a.at_bound == Some(Bound::Min) {
            a.limit = Some("demolding draft");
        }
        out.push(a);
    }
    if let (Some(z), Some(zl)) = (&params.zones, &c.zones) {
        let ds: Vec<f64> = dense_axis(params, fit)?.iter().map(|a| a.0).collect();
        let tested = params.material.strength_model == StrengthModel::TestedMixes;
        out.push(axis("zones.dense_pcf", &ds, (!tested).then_some(z.dense_pcf[2]), zl.dense_pcf));
    }
    if let (Some(ss), Some(sh)) = (&params.shape_search, &c.shape) {
        let w = [
            sh.length_in,
            sh.beam_in,
            sh.draft_in,
            sh.fore_wp,
            sh.section,
            sh.fore_rocker,
            sh.aft_wp,
            sh.aft_rocker,
            sh.fore_len_frac,
            sh.aft_len_frac,
            sh.gunwale_radius_in.unwrap_or(0.0),
        ];
        let steps = [ss.length_step_in, ss.beam_step_in, ss.draft_step_in];
        for (d, (name, [lo, hi])) in ss.bounds().into_iter().enumerate() {
            if hi <= lo || (ss.symmetric && matches!(d, 6 | 7 | 9)) {
                continue;
            }
            let (min, max, step, count) = match steps.get(d).copied().flatten() {
                // Same snapping as shapeopt::normalize.
                Some(st) => {
                    let (kmin, kmax) = ((lo / st - 1e-9).ceil(), (hi / st + 1e-9).floor());
                    (kmin * st, kmax * st, Some(st), Some((kmax - kmin) as usize + 1))
                }
                None => (lo, hi, None, None),
            };
            let near = |a: f64, b: f64| (a - b).abs() <= 1e-9 * a.abs().max(b.abs()).max(1.0);
            let at_bound = if near(w[d], min) {
                Some(Bound::Min)
            } else if near(w[d], max) {
                Some(Bound::Max)
            } else {
                None
            };
            out.push(SearchAxis { name, min, max, step, count, winner: w[d], at_bound, limit: None });
        }
    }
    Ok(out)
}

#[derive(Debug, Clone, Serialize)]
pub struct RunResult {
    pub format: &'static str,
    pub params: Params,
    /// The strength fit actually used (from material.mix_data).
    pub strength: StrengthFit,
    /// Winning hull geometry (jaguar-hull/1), so viewers need not regenerate it.
    pub winner_hull: Option<Hull>,
    /// Fixed hull: every grid candidate. Shape search: one representative per
    /// evaluated shape (its inner winner, or its lowest-D/C failing candidate),
    /// in evaluation order, each with `shape` set.
    pub candidates: Vec<Candidate>,
    /// None = no design passed. Never a fallback "best failing" design.
    pub winner: Option<Winner>,
    /// No design passed: the candidate that misses the gates by the least, analysed in full
    /// like a winner, so the report can show where it fails. It FAILS: never the answer, never
    /// in `winner`. See [`Closest`].
    #[serde(skip_serializing_if = "Option::is_none")]
    pub closest: Option<Closest>,
    /// objective.kind = race: the winner raced on paths re-planned for it
    /// (`race::run`; its weighted time is <= the winner's `race_s`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub race: Option<crate::race::RaceResult>,
    /// `[zones]`: what the two-mix search enumerated, skipped and checked.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub zones: Option<ZoneSearch>,
    /// Shape search ended early by the user ([`RunControl::stop`]): how far it got. The winner
    /// is the best of the shapes checked, verified as usual.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_early: Option<String>,
    /// Lab-data warnings ([`StrengthFit::data_warnings`]) and, when the winner's (or the
    /// closest design's) density lies outside the tested densities, how far its strength is
    /// extrapolated ([`extrapolation_warning`]). Empty = none.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub data_warnings: Vec<String>,
    /// The in-search re-check at 2n-1 stations ([`fine_misses`]); a shape search sums every
    /// shape's inner search and the final re-run.
    pub recheck: Recheck,
}

/// WARNING when `c`'s density (fit mode; and its dense zone mix) lies outside the densities the
/// lab tested, so its strength comes from the line extended past the data (allowed up to
/// `density_extrapolation_pcf` by [`densities`]). None inside, and under tested_mixes.
pub fn extrapolation_warning(params: &Params, fit: &StrengthFit, c: &Candidate) -> Option<String> {
    if params.material.strength_model == StrengthModel::TestedMixes {
        return None;
    }
    let (lo, hi) = fit.tested_range();
    let rhos = std::iter::once(("density", c.density_pcf))
        .chain(c.zones.as_ref().map(|z| ("dense mix", z.dense_pcf)));
    let out: Vec<String> = rhos
        .filter_map(|(what, rho)| {
            let past = (lo - rho).max(rho - hi);
            (past > 1e-9).then(|| format!("{what} {rho:.1} pcf is {past:.1} pcf"))
        })
        .collect();
    (!out.is_empty()).then(|| {
        format!(
            "WARNING {} outside the tested densities {lo:.1}-{hi:.1} pcf: strength extrapolated from the line (limit density_extrapolation_pcf = {})",
            out.join(", "),
            params.material.density_extrapolation_pcf
        )
    })
}

/// Pause and stop requests for a running search (the web UI's Pause and End buttons; the CLI
/// never sets them). Workers look at it between candidates (fixed hull) or shapes (shape
/// search): pausing holds them there, stopping makes them start nothing new.
#[derive(Debug, Default)]
pub struct RunControl {
    stop: AtomicBool,
    paused: Mutex<bool>,
    wake: Condvar,
}

impl RunControl {
    /// End the run: also releases a pause.
    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        self.wake.notify_all();
    }
    pub fn stopped(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }
    pub fn set_paused(&self, on: bool) {
        *self.paused.lock().unwrap_or_else(|e| e.into_inner()) = on;
        self.wake.notify_all();
    }
    pub fn paused(&self) -> bool {
        *self.paused.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Wait while paused (and not stopped); then true if the run should stop.
    pub fn checkpoint(&self) -> bool {
        let mut p = self.paused.lock().unwrap_or_else(|e| e.into_inner());
        while *p && !self.stopped() {
            p = self.wake.wait(p).unwrap_or_else(|e| e.into_inner());
        }
        self.stopped()
    }
}

/// The run was ended by the user before anything could be reported (a fixed-hull search, or a
/// shape search before its first shape finished). Callers tell it apart with `downcast_ref`.
#[derive(Debug)]
pub struct RunEnded;

impl std::fmt::Display for RunEnded {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("run ended by the user")
    }
}

impl std::error::Error for RunEnded {}

/// The two-mix search's bookkeeping (`zoned_search`).
#[derive(Debug, Clone, Serialize)]
pub struct ZoneSearch {
    /// (wall, depth, flare, light mix, heavier dense mix) combinations.
    pub walls: usize,
    /// Skipped: its all-light weight is not below the lightest pass found so far.
    pub heavier: usize,
    /// Skipped: knee punching fails even in the dense mix, or a knee footprint is wider than
    /// the hull.
    pub punching: usize,
    /// Placed (`max_checks` counts these), lightest first.
    pub checked: usize,
    /// Two-mix walls that miss freeboard, which dense concrete (heavier) cannot fix: skipped
    /// because their single-mix wall already misses it, or placed and ending short of it.
    pub freeboard: usize,
    /// `max_checks` was reached with lighter combinations left.
    pub capped: bool,
    /// Weight of the single-mix pick (the winner had there been no `[zones]`); None = no
    /// single-mix wall passes.
    pub single_mix_lb: Option<f64>,
    /// single_mix_lb - winner weight (0 when the winner is single-mix). None without both.
    pub saved_lb: Option<f64>,
    /// Why nothing was placed when there was no combination to try.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

pub const RESULT_FORMAT: &str = "jaguar-result/1";

/// Progress events, emitted in order. The web server streams these to the
/// browser; the CLI ignores them. Must be cheap and thread-safe to call.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)] // short-lived, passed by value to a callback
pub enum Progress {
    /// Search started with `total` candidates (for a shape search: total over
    /// all shapes known so far; may grow as refinement rounds are added). Sent again with
    /// the final count when a `[zones]` search stops before its `max_checks`.
    Started { total: usize },
    /// Shape search: shape `index` of `shapes` is being evaluated.
    Shape { index: usize, shapes: usize, shape: ShapeParams },
    /// One candidate finished (any order, from any thread). `done` counts up to `total`.
    Candidate { done: usize, total: usize, candidate: Candidate },
    /// Winner is being re-checked at `stations`.
    Verifying { stations: usize },
}

/// CLI entry: no progress reporting.
pub fn run(params: &Params, hull: &Hull) -> Result<RunResult> {
    run_with_progress(params, hull, &|_| {})
}

/// Load the mix data (only `material.mixes` when given) and fit it ([`StrengthFit::load_for`]).
pub fn strength(params: &Params) -> Result<StrengthFit> {
    StrengthFit::load_for(&params.material)
}

/// Densities from search.density_pcf clipped to the trusted fit range
/// ([`StrengthFit::trusted_range`]: every active strength line's tested range
/// +/- extrapolation, intersected). Err if empty.
pub fn densities(params: &Params, fit: &StrengthFit) -> Result<Vec<f64>> {
    let e = params.material.density_extrapolation_pcf;
    let (lo, hi) = fit.trusted_range(e);
    let (tlo, thi) = fit.tested_range();
    let v: Vec<f64> =
        range(params.search.density_pcf).into_iter().filter(|&r| r >= lo - 1e-9 && r <= hi + 1e-9).collect();
    anyhow::ensure!(
        !v.is_empty(),
        "no searched density lies within the mix data range {lo:.1}..{hi:.1} pcf (tested by every fitted \
         strength line: {tlo:.1}..{thi:.1}, +/- density_extrapolation_pcf {e}, none below the lightest \
         test of a negative-slope line); widen search.density_pcf or add lab data"
    );
    Ok(v)
}

/// The density axis: (density, strength model, tested-mix name) per entry.
/// Fit mode: [`densities`] with the global fit, no names. Tested-mixes mode:
/// one entry per tested mix (by density then name) with that mix's own
/// strength; search.density_pcf and the extrapolation limit are ignored.
pub fn density_axis(params: &Params, fit: &StrengthFit) -> Result<Vec<(f64, StrengthFit, Option<String>)>> {
    match params.material.strength_model {
        StrengthModel::Fit => {
            Ok(densities(params, fit)?.into_iter().map(|r| (r, fit.clone(), None)).collect())
        }
        StrengthModel::TestedMixes => {
            let m = fit.tested_mixes();
            anyhow::ensure!(!m.is_empty(), "no tested mixes in the mix data");
            Ok(m.iter().map(|x| (x.density_pcf, fit.for_mix(x), Some(x.name.clone()))).collect())
        }
    }
}

/// The dense-mix axis of `[zones]`, as [`density_axis`]: fit mode = `zones.dense_pcf` clipped
/// to the trusted fit range; tested mixes = those inside its [min, max]. Err if empty.
pub fn dense_axis(params: &Params, fit: &StrengthFit) -> Result<Vec<(f64, StrengthFit, Option<String>)>> {
    let Some(z) = &params.zones else { return Ok(vec![]) };
    let [lo, hi, _] = z.dense_pcf;
    let v: Vec<_> = match params.material.strength_model {
        StrengthModel::Fit => {
            let (a, b) = fit.trusted_range(params.material.density_extrapolation_pcf);
            range(z.dense_pcf)
                .into_iter()
                .filter(|&r| r >= a - 1e-9 && r <= b + 1e-9)
                .map(|r| (r, fit.clone(), None))
                .collect()
        }
        StrengthModel::TestedMixes => fit
            .tested_mixes()
            .iter()
            .filter(|x| x.density_pcf >= lo - 1e-9 && x.density_pcf <= hi + 1e-9)
            .map(|x| (x.density_pcf, fit.for_mix(x), Some(x.name.clone())))
            .collect(),
    };
    anyhow::ensure!(!v.is_empty(), "zones.dense_pcf: no dense mix within the mix data ({lo}..{hi} pcf)");
    Ok(v)
}

/// Same result as [`run`] (byte-identical JSON), plus progress events.
///
/// If the winner's analysis fails at verification resolution, it is reported
/// as not verified with `verify_dc = f64::MAX` (a finite, obviously failing
/// number, so result.json stays numeric).
pub fn run_with_progress(
    params: &Params,
    hull: &Hull,
    on_progress: &(dyn Fn(Progress) + Sync),
) -> Result<RunResult> {
    run_search(params, hull, on_progress, &RunControl::default())
}

/// [`run_with_progress`] that can be paused and ended ([`RunControl`]). Ended = Err([`RunEnded`]):
/// a part of the grid is no answer.
pub fn run_controlled(
    params: &Params,
    hull: &Hull,
    on_progress: &(dyn Fn(Progress) + Sync),
    ctl: &RunControl,
) -> Result<RunResult> {
    run_search(params, hull, on_progress, ctl)
}

/// What the shape search scores a shape on ([`run_inner`]).
pub struct Inner {
    /// The candidate [`run`] would make the winner (`winner.candidate`, before verification
    /// fills in a missing `drag_n`); None = nothing passes.
    pub best: Option<Candidate>,
    /// Every candidate tried, as [`run`] reports them.
    pub candidates: Vec<Candidate>,
    pub recheck: Recheck,
}

/// The shape search's inner loop: [`run`]'s wall search without the winner analysis
/// (verification at 2n-1 and 4n-3 stations, diagrams, worked calculation) and without the
/// closest design, which a shape's score never reads. The best shape is re-run with [`run`].
/// Called on a 1-thread pool by the shape search, so its "parallel" rayon calls run serially.
pub fn run_inner(params: &Params, hull: &Hull) -> Result<Inner> {
    let s = search_walls(params, hull, &|_| {}, &RunControl::default())?;
    let best = s.refined.map(|r| r.0).or(s.best);
    let mut candidates = s.candidates;
    candidates.extend(s.refine_tries);
    Ok(Inner { best, candidates, recheck: s.recheck })
}

/// The wall search up to the winner's candidate: [`search_walls`]'s output, kept for the
/// winner analysis.
struct Searched {
    fit: StrengthFit,
    axis: Vec<(f64, StrengthFit, Option<String>)>,
    dense: Vec<(f64, StrengthFit, Option<String>)>,
    ts: Vec<f64>,
    geoms: Vec<(f64, f64)>,
    grids: Vec<Grid>,
    pairs: Vec<(usize, usize)>,
    plans: Vec<Vec<Plan>>,
    candidates: Vec<Candidate>,
    zone_stats: Option<ZoneSearch>,
    /// The best passing grid candidate.
    best: Option<Candidate>,
    /// `best` moved to a lighter depth and flare ([`refine_wall`]), with its seats.
    refined: Option<WallTry>,
    /// Every point [`refine_wall`] tried (not yet in `candidates`).
    refine_tries: Vec<Candidate>,
    recheck: Recheck,
}

fn run_search(
    params: &Params,
    hull: &Hull,
    on_progress: &(dyn Fn(Progress) + Sync),
    ctl: &RunControl,
) -> Result<RunResult> {
    let Searched {
        fit,
        axis,
        dense,
        ts,
        geoms,
        grids,
        pairs,
        plans,
        mut candidates,
        mut zone_stats,
        best,
        refined,
        refine_tries,
        recheck,
    } = search_walls(params, hull, on_progress, ctl)?;
    let winner = match &best {
        None => None,
        Some(c) => {
            let ri = axis.iter().position(|a| a.0 == c.density_pcf && a.2 == c.mix).unwrap_or(0);
            let pi = pairs
                .iter()
                .position(|&(gi, ti)| geoms[gi] == (c.depth_in, c.flare_deg) && ts[ti] == c.thickness_in)
                .ok_or_else(|| anyhow::anyhow!("winner wall not on the search grid"))?;
            let maker: Option<Box<ZonedMaker>> = match &c.zones {
                None => None,
                Some(zl) => {
                    let di = dense
                        .iter()
                        .position(|a| a.0 == zl.dense_pcf && a.2 == c.dense_mix)
                        .ok_or_else(|| anyhow::anyhow!("winner dense mix not on the zones axis"))?;
                    let fits = [axis[ri].1.clone(), dense[di].1.clone()];
                    let seats = plans[pi][ri].as_ref().map_err(|e| anyhow::anyhow!("winner seats: {e}"))?;
                    let (t, zl) = (c.thickness_in, zl.clone());
                    Some(Box::new(move |g: &Grid| {
                        ZonedWall::new(params, g, t, zl.clone(), fits.clone(), seats)
                    }))
                }
            };
            let (c, plan) = match &refined {
                Some((rc, rp)) => (rc, rp),
                None => (c, &plans[pi][ri]),
            };
            Some(finish_winner(params, &fit, &axis[ri].1, hull, c, plan, maker.as_deref(), on_progress)?)
        }
    };
    candidates.extend(refine_tries);

    if let (Some(st), Some(w)) = (&mut zone_stats, &winner) {
        st.saved_lb = st.single_mix_lb.map(|s| s - w.candidate.hull_weight_lb);
    }
    let closest = match winner {
        Some(_) => None,
        None => {
            let space = ClosestSpace {
                axis: &axis,
                grids: &grids,
                geoms: &geoms,
                pairs: &pairs,
                ts: &ts,
                plans: &plans,
            };
            closest_design(params, &fit, hull, &candidates, &space, on_progress)?
        }
    };
    let winner_hull = (winner.is_some() || closest.is_some()).then(|| hull.clone());
    let mut data_warnings = fit.data_warnings(params.material.strength_model);
    let design = winner.as_ref().or(closest.as_ref().map(|c| &c.design));
    data_warnings.extend(design.and_then(|w| extrapolation_warning(params, &fit, &w.candidate)));
    Ok(RunResult {
        format: RESULT_FORMAT,
        params: params.clone(),
        strength: fit,
        winner_hull,
        candidates,
        winner,
        closest,
        race: None,
        zones: zone_stats,
        ended_early: None,
        data_warnings,
        recheck,
    })
}

/// The wall grid (and zones) evaluated, the best candidate picked and its depth refined.
fn search_walls(
    params: &Params,
    hull: &Hull,
    on_progress: &(dyn Fn(Progress) + Sync),
    ctl: &RunControl,
) -> Result<Searched> {
    let ts = thicknesses(params);
    let fit = strength(params)?;
    let axis = density_axis(params, &fit)?;
    let rhos: Vec<f64> = axis.iter().map(|a| a.0).collect();
    let ds = range(params.search.depth_in);
    let fs = params.flares();
    let total = ts.len() * rhos.len() * ds.len() * fs.len();
    on_progress(Progress::Started { total });
    let n = params.hull.stations;
    let done = AtomicUsize::new(0);

    // Topsides per (depth, flare), depth-major (one flare = one per depth, the old order);
    // `unbuildable[g]` = why that gunwale outline cannot be built (its candidates then
    // fail with the reason, analysed on the real geometry).
    let geoms: Vec<(f64, f64)> = ds.iter().flat_map(|&d| fs.iter().map(move |&f| (d, f))).collect();
    let (grids, unbuildable): (Vec<Grid>, Vec<Option<String>>) = geoms
        .par_iter()
        .map(|&(d, f)| Grid::build_topsides(hull, d, n, Topsides { flare_deg: f, ..params.hull.topsides() }))
        .unzip();
    let pairs: Vec<(usize, usize)> =
        (0..grids.len()).flat_map(|g| (0..ts.len()).map(move |t| (g, t))).collect();
    // Seats per wall (geometry, t) and density, [pi][ri] (src/seats.rs): auto seats depend on the
    // wall's weight and strength, so each density gets its own; verify reuses them.
    let plans: Vec<Vec<Plan>> = pairs
        .par_iter()
        .map(|&(gi, ti)| {
            let sec = sections(&grids[gi], ts[ti]);
            axis.iter()
                .map(|a| {
                    plan_seats(params, &a.1, &grids[gi], &sec, ts[ti], a.0).map_err(|e| format!("{e:#}"))
                })
                .collect()
        })
        .collect();
    let mut candidates: Vec<(usize, usize, usize, Candidate)> = pairs
        .par_iter()
        .zip(&plans)
        .flat_map_iter(|(&(gi, ti), plan_rho)| {
            let grid = &grids[gi];
            let t = ts[ti];
            let sec = sections(grid, t);
            let done = &done;
            let axis = &axis;
            let why = &unbuildable[gi];
            let flare = geoms[gi].1;
            rhos.iter()
                .enumerate()
                .filter(|_| !ctl.checkpoint())
                .map(move |(ri, &rho)| {
                    let mut c = evaluate(params, &axis[ri].1, grid, &sec, t, rho, &plan_rho[ri], true);
                    c.mix = axis[ri].2.clone();
                    c.flare_deg = flare;
                    if let Some(why) = why {
                        c.passes = false;
                        c.error = Some(why.clone());
                    }
                    let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                    on_progress(Progress::Candidate { done: k, total, candidate: c.clone() });
                    (ti, ri, gi, c)
                })
                .collect::<Vec<_>>()
        })
        .collect();
    if ctl.stopped() {
        return Err(RunEnded.into());
    }
    candidates.sort_by_key(|&(t, r, d, _)| (t, r, d));
    // (thickness, light mix, geometry) whose single-mix wall misses freeboard: a two-mix wall
    // there is heavier still, so the zoned search skips it.
    let fb_fails: std::collections::HashSet<(usize, usize, usize)> = candidates
        .iter()
        .filter(|x| {
            x.3.verdict.as_ref().is_some_and(|v| v.freeboard_in < params.gates.freeboard_required_in())
        })
        .map(|x| (x.0, x.1, x.2))
        .collect();
    let mut candidates: Vec<Candidate> = candidates.into_iter().map(|x| x.3).collect();

    let mut zone_stats = None;
    let dense = dense_axis(params, &fit)?;
    let single =
        candidates.iter().filter(|c| c.passes).reduce(|a, b| if better(params, b, a) { b } else { a });
    let single_mix_lb = single.map(|c| c.hull_weight_lb);
    if let Some(z) = &params.zones {
        let lightest = candidates.iter().filter(|c| c.passes).map(|c| c.hull_weight_lb).reduce(f64::min);
        let space = ZoneSpace {
            axis: &axis,
            dense: &dense,
            grids: &grids,
            geoms: &geoms,
            pairs: &pairs,
            ts: &ts,
            plans: &plans,
            fb_fails: &fb_fails,
        };
        let (zc, stats) =
            zoned_search(params, z, &space, &unbuildable, lightest, (&done, total), on_progress);
        candidates.extend(zc);
        zone_stats = Some(ZoneSearch { single_mix_lb, ..stats });
        if ctl.checkpoint() {
            return Err(RunEnded.into());
        }
    }

    // The best candidate, re-checked at 2n-1 stations against every gate without the search
    // margins; one that fails there is failed with the reason and the next best taken. Continuous
    // depth and flare (search.depth_tol_in, flare_tol_deg): a single-mix best moves to the lightest
    // passing point below its grid one (each point re-checked the same way, seats solved there).
    let mut recheck = Recheck::default();
    let mut refine_tries = Vec::new();
    let (best, refined) = loop {
        let Some(b) = candidates
            .iter()
            .filter(|c| c.passes)
            .reduce(|a, b| if better(params, b, a) { b } else { a })
            .cloned()
        else {
            break (None, None);
        };
        // Two-mix walls: their zones are checked at 2n-1 by verification only.
        if b.zones.is_some() {
            break (Some(b), None);
        }
        let (refined, tries) = refine_wall(params, hull, &axis, (&ds, &fs), &b);
        refine_tries.extend(tries);
        if refined.is_some() {
            break (Some(b), refined);
        }
        let ri = axis.iter().position(|a| a.0 == b.density_pcf && a.2 == b.mix);
        let pi = pairs
            .iter()
            .position(|&(gi, ti)| geoms[gi] == (b.depth_in, b.flare_deg) && ts[ti] == b.thickness_in);
        let (Some(ri), Some(pi)) = (ri, pi) else { break (Some(b), None) };
        recheck.checked += 1;
        let top = candidate_topsides(params, &b);
        let miss = fine_misses(
            params,
            hull,
            &axis[ri].1,
            (b.depth_in, top),
            b.thickness_in,
            b.density_pcf,
            &plans[pi][ri],
        );
        if miss.is_empty() {
            break (Some(b), None);
        }
        recheck.failed += 1;
        let same = |c: &Candidate| {
            c.zones.is_none()
                && (c.thickness_in, c.density_pcf, c.depth_in, c.flare_deg)
                    == (b.thickness_in, b.density_pcf, b.depth_in, b.flare_deg)
                && c.mix == b.mix
        };
        if let Some(c) = candidates.iter_mut().find(|c| same(c)) {
            c.passes = false;
            c.error = Some(miss.join("; "));
        }
    };
    Ok(Searched {
        fit,
        axis,
        dense,
        ts,
        geoms,
        grids,
        pairs,
        plans,
        candidates,
        zone_stats,
        best,
        refined,
        refine_tries,
        recheck,
    })
}

/// Seats of every load case on one wall, or why they could not be resolved.
type Plan = Result<Vec<CaseSeats>, String>;

/// Every gate a single-mix wall (`depth`, topsides `top`, thickness `t`, mix `rho` with strength
/// `fit`, seats `plan`) misses at the 2n-1 verification stations, at the verification limits
/// (no search margin). Empty = passes there.
#[allow(clippy::too_many_arguments)]
fn fine_misses(
    params: &Params,
    hull: &Hull,
    fit: &StrengthFit,
    (depth, top): (f64, Topsides),
    t: f64,
    rho: f64,
    plan: &Plan,
) -> Vec<String> {
    let fine = 2 * params.hull.stations - 1;
    let (grid, why) = Grid::build_topsides(hull, depth, fine, top);
    if let Some(why) = why {
        return vec![format!("at {fine} stations: {why}")];
    }
    let sec = sections(&grid, t);
    let v = evaluate(params, fit, &grid, &sec, t, rho, plan, false);
    match &v.error {
        Some(e) => vec![format!("at {fine} stations: {e}")],
        None => gate_misses(params, &v, false, fine),
    }
}

/// One analysis result from [`refine_wall`]: the candidate and the seats it was checked at.
type WallTry = (Candidate, Result<Vec<CaseSeats>, String>);

/// A wall variable [`refine_wall`] makes continuous.
#[derive(Clone, Copy)]
enum Refine {
    Depth,
    Flare,
}

/// Continuous mold depth and flare (`search.depth_tol_in`, `search.flare_tol_deg`) from
/// single-mix grid winner `c` (same wall and mix): each variable is bisected over `x - j * tol`
/// above its next lower grid value, which is lighter than `c` and so failed (less depth or
/// flare = less freeboard and GM). Assumes pass/fail is monotone over that one step. With both,
/// flare-then-depth and depth-then-flare are run (the two trade against each other) and the
/// better end point kept. A point passes when it passes the search and its freeboard and loaded
/// GM also pass at the 2n-1 verification stations (no search margin on those gates, unlike D/C).
/// Returns the refined winner when it beats `c` (None = keep `c`), and every candidate tried.
fn refine_wall(
    params: &Params,
    hull: &Hull,
    axis: &[(f64, StrengthFit, Option<String>)],
    (ds, fs): (&[f64], &[f64]),
    c: &Candidate,
) -> (Option<WallTry>, Vec<Candidate>) {
    let (dtol, ftol) = (params.search.depth_tol_in, params.search.flare_tol_deg);
    let Some(ri) = axis.iter().position(|a| a.0 == c.density_pcf && a.2 == c.mix) else {
        return (None, Vec::new());
    };
    let try_at = |d: f64, f: f64| -> WallTry {
        let top = Topsides { flare_deg: f, ..params.hull.topsides() };
        let (grid, why) = Grid::build_topsides(hull, d, params.hull.stations, top);
        let sec = sections(&grid, c.thickness_in);
        let plan = plan_seats(params, &axis[ri].1, &grid, &sec, c.thickness_in, c.density_pcf)
            .map_err(|e| format!("{e:#}"));
        let mut k = evaluate(params, &axis[ri].1, &grid, &sec, c.thickness_in, c.density_pcf, &plan, true);
        (k.mix, k.flare_deg) = (c.mix.clone(), f);
        if let Some(why) = why {
            k.passes = false;
            k.error = Some(why);
        }
        // Bisection drives the wall onto a gate: a point passes only if every gate also holds
        // at the 2n-1 verification stations.
        if k.passes {
            let miss = fine_misses(params, hull, &axis[ri].1, (d, top), c.thickness_in, c.density_pcf, &plan);
            if !miss.is_empty() {
                k.passes = false;
                k.error = Some(miss.join("; "));
            }
        }
        (k, plan)
    };
    // Lowest passing value of one variable below passing point `p` (None: not refinable there,
    // or nothing lower passed).
    let bisect = |p: &Candidate, which: Refine, tried: &mut Vec<Candidate>| -> Option<WallTry> {
        let (grid, tol, x) = match which {
            Refine::Depth => (ds, dtol?, p.depth_in),
            Refine::Flare => (fs, ftol?, p.flare_deg),
        };
        let gi = grid.iter().position(|&g| g == x).filter(|&i| i > 0)?;
        let step = x - grid[gi - 1];
        // Largest j with j * tol < step: values stay above the failed grid value.
        let n = ((step / tol - 1e-9).ceil() as usize).saturating_sub(1);
        // j = lo passes (`p` at 0), j = hi fails (the grid value below at n + 1).
        let (mut lo, mut hi, mut found) = (0, n + 1, None);
        while hi - lo > 1 {
            let j = (lo + hi) / 2;
            let v = x - j as f64 * tol;
            let (k, plan) = match which {
                Refine::Depth => try_at(v, p.flare_deg),
                Refine::Flare => try_at(p.depth_in, v),
            };
            tried.push(k.clone());
            if k.passes {
                lo = j;
                found = Some((k, plan));
            } else {
                hi = j;
            }
        }
        found
    };
    let orders: &[[Refine; 2]] = match (dtol, ftol) {
        (Some(_), Some(_)) => &[[Refine::Flare, Refine::Depth], [Refine::Depth, Refine::Flare]],
        _ => &[[Refine::Depth, Refine::Flare]],
    };
    let mut tried = Vec::new();
    let mut best: Option<WallTry> = None;
    for order in orders {
        let mut cur: Option<WallTry> = None;
        for &which in order {
            if let Some(t) = bisect(cur.as_ref().map_or(c, |t| &t.0), which, &mut tried) {
                cur = Some(t);
            }
        }
        if let Some(t) = cur
            && best.as_ref().is_none_or(|b| better(params, &t.0, &b.0))
        {
            best = Some(t);
        }
    }
    (best.filter(|(k, _)| better(params, k, c)), tried)
}

/// The search grid [`closest_design`] re-evaluates candidates on.
struct ClosestSpace<'a> {
    axis: &'a [(f64, StrengthFit, Option<String>)],
    grids: &'a [Grid],
    geoms: &'a [(f64, f64)],
    pairs: &'a [(usize, usize)],
    ts: &'a [f64],
    plans: &'a [Vec<Plan>],
}

/// No design passed: the single-mix candidate that misses the gates by the least
/// ([`shortfalls`]). The 16 best by the search's numbers (which stop at the first failed gate)
/// are re-checked with every load case and gate, the best of those is analysed like a winner.
/// None when no candidate could be analysed at all (every one errored, or no seats).
fn closest_design(
    params: &Params,
    fit: &StrengthFit,
    hull: &Hull,
    candidates: &[Candidate],
    sp: &ClosestSpace,
    on_progress: &(dyn Fn(Progress) + Sync),
) -> Result<Option<Closest>> {
    let mut pre: Vec<(f64, usize)> = candidates
        .iter()
        .enumerate()
        .filter(|(_, c)| c.zones.is_none() && c.verdict.is_some())
        .map(|(i, c)| (shortfalls(params, c).0, i))
        .collect();
    let lb = |i: usize| candidates[i].hull_weight_lb;
    pre.sort_by(|a, b| a.0.total_cmp(&b.0).then(lb(a.1).total_cmp(&lb(b.1))));
    let full: Vec<(f64, Candidate, usize, usize)> = pre
        .par_iter()
        .take(16)
        .filter_map(|&(_, i)| {
            let c = &candidates[i];
            let ri = sp.axis.iter().position(|a| a.0 == c.density_pcf && a.2 == c.mix)?;
            let pi = sp.pairs.iter().position(|&(gi, ti)| {
                sp.geoms[gi] == (c.depth_in, c.flare_deg) && sp.ts[ti] == c.thickness_in
            })?;
            sp.plans[pi][ri].as_ref().ok()?;
            let grid = &sp.grids[sp.pairs[pi].0];
            let sec = sections(grid, c.thickness_in);
            let mut f = evaluate(
                params,
                &sp.axis[ri].1,
                grid,
                &sec,
                c.thickness_in,
                c.density_pcf,
                &sp.plans[pi][ri],
                false,
            );
            f.mix = c.mix.clone();
            f.flare_deg = c.flare_deg;
            if f.error.is_none() {
                f.error = c.error.clone(); // e.g. under the buildable floor
            }
            Some((shortfalls(params, &f).0, f, ri, pi))
        })
        .collect();
    let Some((_, c, ri, pi)) = full
        .into_iter()
        .filter(|f| f.1.verdict.is_some())
        .min_by(|a, b| a.0.total_cmp(&b.0).then(a.1.hull_weight_lb.total_cmp(&b.1.hull_weight_lb)))
    else {
        return Ok(None);
    };
    let design = finish_winner(params, fit, &sp.axis[ri].1, hull, &c, &sp.plans[pi][ri], None, on_progress)?;
    Ok(Some(Closest { fails: shortfalls(params, &c).1, design }))
}

/// The wall search grid `zoned_search` places zones on.
struct ZoneSpace<'a> {
    axis: &'a [(f64, StrengthFit, Option<String>)],
    dense: &'a [(f64, StrengthFit, Option<String>)],
    grids: &'a [Grid],
    geoms: &'a [(f64, f64)],
    pairs: &'a [(usize, usize)],
    ts: &'a [f64],
    plans: &'a [Vec<Plan>],
    /// (thickness, light mix, geometry) indices whose single-mix wall misses freeboard.
    fb_fails: &'a std::collections::HashSet<(usize, usize, usize)>,
}

/// One combination to place: wall pair, light and dense mix index, all-light weight.
struct ZoneTry {
    pi: usize,
    ri: usize,
    di: usize,
    light_lb: f64,
}

/// Knee punching D/C of a `t` wall of mix `rho` under the heaviest paddler (every case), as
/// `check` computes it at the worst load combination.
fn punching_dc(params: &Params, fit: &StrengthFit, rho: f64, t: f64) -> f64 {
    let mat = &params.material;
    let (_, kp) = crate::check::capacity_factors(mat, &params.punching);
    let live = mat.combos().iter().map(|c| c.1).fold(0.0, f64::max);
    let heaviest = params.load_cases.iter().flat_map(|c| &c.weights_lb).cloned().fold(0.0, f64::max);
    let pc = crate::check::punching_calc(
        heaviest * mat.dynamic_load_factor * live,
        t,
        fit.fc_psi(rho),
        1.0,
        mat.knee_patch_in,
        &params.punching,
    );
    pc.v_u / (pc.v_nominal * kp)
}

/// Snap `x` down / up to a multiple of `inc` (a measurable mark), with float slack.
fn snap_down(x: f64, inc: f64) -> f64 {
    (x / inc + 1e-9).floor() * inc
}
fn snap_up(x: f64, inc: f64) -> f64 {
    (x / inc - 1e-9).ceil() * inc
}

/// Dense rectangles a failing check asks for: at every station (any case, seat sample and load
/// combination) where bending tension, compression, biaxial or shear is over `gate` in the light
/// mix, the light wall at that fibre (bending: `step` in from the extreme fibre; shear: `step`
/// around the neutral axis), one station wide, edges snapped outward to `inc`. A check failing
/// in the dense mix asks for nothing (more dense does not fix it).
#[allow(clippy::too_many_arguments)]
fn station_needs(
    params: &Params,
    grid: &Grid,
    sec: &Sections,
    zw: &ZonedWall,
    plan: &[CaseSeats],
    t: f64,
    inc: f64,
    gate: f64,
) -> Vec<Rect> {
    let mat = &params.material;
    let (k, _) = crate::check::capacity_factors(mat, &params.punching);
    let caps = [
        crate::check::MixCaps::of(&zw.fits[0], zw.zl.light_pcf, k),
        crate::check::MixCaps::of(&zw.fits[1], zw.zl.dense_pcf, k),
    ];
    let step = snap_up(0.5, inc).max(inc);
    let (patch, cg) = (mat.knee_patch_in, params.stability.crew_cg_in);
    let wall = Wall::Zoned(zw);
    let (xs, n) = (&grid.x, grid.nx());
    let mut need: Vec<Rect> = Vec::new();
    let over = |(d, c): (f64, f64)| !(c.is_finite() && c > 0.0) || d / c > gate;
    for (case, seats) in params.load_cases.iter().zip(plan) {
        for sample in seats::samples(&seats.seats_in, params.seats.variance_in, grid) {
            for (dead, live) in mat.combos() {
                let crew: Vec<CrewLoad> = sample
                    .iter()
                    .zip(&case.weights_lb)
                    .map(|(&x_in, &w)| CrewLoad { x_in, lb: w * mat.dynamic_load_factor * live })
                    .collect();
                let Ok(loads) = wall.solve(grid, sec, dead, &crew, patch, cg) else { continue };
                for (i, st) in
                    crate::check::zoned_stations(grid, &zw.zs, &loads, zw.n, caps, t, params.turning.as_ref())
                        .into_iter()
                        .enumerate()
                {
                    if st.masked {
                        continue;
                    }
                    let zb = zw.zs.zbar[i];
                    let mut zr: Vec<(f64, f64)> = Vec::new();
                    let fibre = |(z, _): (f64, bool)| if z <= zb { (z, z + step) } else { (z - step, z) };
                    for (fails, at) in [
                        (over(st.tension), st.tension_at),
                        (over(st.compression), st.compression_at),
                        (st.biaxial.is_nan() || st.biaxial > gate, st.biaxial_at),
                    ] {
                        if fails && !at.1 {
                            zr.push(fibre(at));
                        }
                    }
                    if over(st.shear) && !zw.zs.dense_at_na[i] {
                        zr.push((zb - 0.5 * step, zb + 0.5 * step));
                    }
                    let x0 = if i == 0 { xs[0] } else { 0.5 * (xs[i - 1] + xs[i]) };
                    let x1 = if i + 1 == n { xs[n - 1] } else { 0.5 * (xs[i] + xs[i + 1]) };
                    for (z0, z1) in zr {
                        need.push(Rect {
                            x0_in: snap_down(x0, inc).max(xs[0]),
                            x1_in: snap_up(x1, inc).min(xs[n - 1]),
                            z0_in: snap_down(z0, inc).max(0.0),
                            z1_in: snap_up(z1, inc).min(grid.depth),
                        });
                    }
                }
            }
        }
    }
    need
}

/// Why [`place_zones`] stopped short of a pass.
#[derive(Clone, Copy, PartialEq)]
enum Stopped {
    Other,
    /// The wall misses freeboard: more dense concrete only adds weight.
    Freeboard,
}

/// Place the dense mix for one combination: start all light; if the light mix fails knee
/// punching, make every knee footprint dense; then, while the wall fails a D/C check that more
/// dense can fix (`station_needs`), add those rectangles; while only GM is short, add a keel
/// strip (dense concrete lowest in the hull lowers G most): z 0..h centred on midships, 24 in
/// longer each round, and once it spans the hull, h one step higher. Returns the final candidate (passing
/// or failing with its reason), or None when nothing was placed, with why (an all-light wall
/// that passes is the single-mix candidate itself).
fn place_zones(
    params: &Params,
    z: &crate::params::Zones,
    sp: &ZoneSpace,
    tr: &ZoneTry,
) -> (Option<Candidate>, Stopped) {
    const MAX_ROUNDS: usize = 80;
    const GROW_IN: f64 = 24.0;
    let (gi, ti) = sp.pairs[tr.pi];
    let (grid, t) = (&sp.grids[gi], sp.ts[ti]);
    // Seats planned for the all-light wall: the dense patches are placed after the seats.
    let plan = &sp.plans[tr.pi][tr.ri];
    let Ok(seats) = plan.as_ref() else { return (None, Stopped::Other) };
    let (light, dense) = (&sp.axis[tr.ri], &sp.dense[tr.di]);
    let (gate, inc) = (params.gates.dc_search, z.increment_in);
    let sec = sections(grid, t);
    let mut rects: Vec<Rect> = Vec::new();
    if punching_dc(params, &light.1, light.0, t) > gate {
        let Some(regions) = knee_regions(params, grid, t, seats) else { return (None, Stopped::Other) };
        rects = regions
            .into_iter()
            .map(|r| Rect {
                x0_in: snap_down(r.x0_in, inc).max(grid.x[0]),
                x1_in: snap_up(r.x1_in, inc).min(grid.x[grid.nx() - 1]),
                z0_in: 0.0,
                // One increment of margin so the footprint stays covered on a finer grid.
                z1_in: (snap_up(r.z1_in, inc) + inc).min(grid.depth),
            })
            .collect();
    }
    let (xa, xb) = (grid.x[0], grid.x[grid.nx() - 1]);
    let step = snap_up(0.5, inc).max(inc);
    // keel strip: (rounds of growth, height)
    let (mut grow, mut h) = (0usize, step);
    let mut last: Option<Candidate> = None;
    for _ in 0..MAX_ROUNDS {
        let zl = ZoneLayout { light_pcf: light.0, dense_pcf: dense.0, dense: normalize(&rects) };
        let empty = zl.dense.is_empty();
        let zw = ZonedWall::new(params, grid, t, zl, [light.1.clone(), dense.1.clone()], seats);
        let mut c =
            evaluate_wall(params, &light.1, grid, &sec, t, Wall::Zoned(&zw), plan, true, search_drag(params));
        c.mix = light.2.clone();
        c.dense_mix = dense.2.clone();
        c.flare_deg = sp.geoms[gi].1;
        if c.passes && empty {
            return (None, Stopped::Other);
        }
        let fb_ok =
            c.verdict.as_ref().is_some_and(|v| v.freeboard_in >= params.gates.freeboard_required_in());
        let dc_over = c.verdict.as_ref().is_some_and(|v| v.dc > gate);
        let st = &params.stability;
        let gm_short = c.gm_in.is_some_and(|g| g < st.gm_min_in)
            || c.gm_empty_in.zip(st.gm_empty_min_in).is_some_and(|(g, m)| g < m);
        let need = if c.passes || c.error.is_some() || !fb_ok {
            Vec::new()
        } else if dc_over {
            if c.verdict.as_ref().is_some_and(|v| v.governing_check == "punching") {
                Vec::new()
            } else {
                station_needs(params, grid, &sec, &zw, seats, t, inc, gate)
            }
        } else if gm_short && h <= 0.5 * grid.depth {
            // GM by ballast: the strip grows on a quick float check (nominal seats and the crew
            // shifted fore / aft) and is then fully checked; if even the largest strip (full
            // length, half the mold depth) leaves GM short, nothing will.
            let fits = [light.1.clone(), dense.1.clone()];
            let ok = |extra: &Rect| {
                let mut r = rects.clone();
                r.push(*extra);
                let zl = ZoneLayout { light_pcf: light.0, dense_pcf: dense.0, dense: normalize(&r) };
                gm_passes(params, grid, t, zl, fits.clone(), seats)
            };
            let full = Rect { x0_in: xa, x1_in: xb, z0_in: 0.0, z1_in: snap_down(0.5 * grid.depth, inc) };
            let mut strip = None;
            if ok(&full) {
                while h <= 0.5 * grid.depth {
                    grow += 1;
                    let half = GROW_IN * grow as f64 / 2.0;
                    let mid = 0.5 * (xa + xb);
                    let r = Rect {
                        x0_in: snap_down(mid - half, inc).max(xa),
                        x1_in: snap_up(mid + half, inc).min(xb),
                        z0_in: 0.0,
                        z1_in: h.min(grid.depth),
                    };
                    if mid - half <= xa && mid + half >= xb {
                        (grow, h) = (0, h + step);
                    }
                    if ok(&r) {
                        strip = Some(r);
                        break;
                    }
                }
            }
            strip.into_iter().collect()
        } else {
            Vec::new()
        };
        if need.is_empty() {
            let why = if !fb_ok && c.error.is_none() && c.verdict.is_some() {
                Stopped::Freeboard
            } else {
                Stopped::Other
            };
            return (if empty { None } else { Some(c) }, why);
        }
        let before = normalize(&rects);
        rects.extend(need);
        rects = normalize(&rects);
        last = Some(c);
        if rects == before {
            break;
        }
    }
    (last.filter(|c| c.zones.as_ref().is_some_and(|z| !z.dense.is_empty())), Stopped::Other)
}

/// Does zoned wall `zl` meet the GM gates (every load case at its nominal seats and the crew
/// moved fore / aft by the seat variance, real crew weight; and the empty canoe)? A float that
/// cannot be solved counts as a miss.
fn gm_passes(
    params: &Params,
    grid: &Grid,
    t: f64,
    zl: ZoneLayout,
    fits: [StrengthFit; 2],
    seats: &[CaseSeats],
) -> bool {
    let zw = ZonedWall::new(params, grid, t, zl, fits, seats);
    let st = &params.stability;
    let (patch, cg) = (params.material.knee_patch_in, st.crew_cg_in);
    let gm =
        |crew: &[CrewLoad]| solve_weights(grid, &zw.zs.weight, &zw.zs.zg, crew, patch, cg).map(|l| l.gm_in);
    if let Some(m) = st.gm_empty_min_in
        && !gm(&[]).is_ok_and(|g| g >= m)
    {
        return false;
    }
    params.load_cases.iter().zip(seats).all(|(case, s)| {
        seats::shifted(&s.seats_in, params.seats.variance_in, grid).iter().all(|q| {
            let crew: Vec<CrewLoad> =
                q.iter().zip(&case.weights_lb).map(|(&x_in, &w)| CrewLoad { x_in, lb: w }).collect();
            gm(&crew).is_ok_and(|g| g >= st.gm_min_in)
        })
    })
}

/// Two-mix search over `[zones]`: every (wall, depth, flare) with resolved seats x light mix x
/// heavier dense mix, placed by [`place_zones`], lightest all-light weight first in fixed
/// batches (deterministic). A combination whose all-light weight is not below the lightest pass
/// so far (single-mix or zoned) cannot win and is not placed; the search ends there or after
/// `max_checks` placements.
fn zoned_search(
    params: &Params,
    z: &crate::params::Zones,
    sp: &ZoneSpace,
    unbuildable: &[Option<String>],
    lightest: Option<f64>,
    (done, total0): (&AtomicUsize, usize),
    on_progress: &(dyn Fn(Progress) + Sync),
) -> (Vec<Candidate>, ZoneSearch) {
    const BATCH: usize = 32;
    let gate = params.gates.dc_search;
    let mut stats = ZoneSearch {
        walls: 0,
        heavier: 0,
        punching: 0,
        checked: 0,
        freeboard: 0,
        capped: false,
        single_mix_lb: None,
        saved_lb: None,
        note: None,
    };
    let mut tries = Vec::new();
    for (pi, &(gi, ti)) in sp.pairs.iter().enumerate() {
        if unbuildable[gi].is_some() {
            continue;
        }
        let (grid, t) = (&sp.grids[gi], sp.ts[ti]);
        let light_v = hull_weight(grid, &sections(grid, t), 1.0);
        for (ri, l) in sp.axis.iter().enumerate() {
            if sp.plans[pi][ri].is_err() {
                continue;
            }
            for (di, d) in sp.dense.iter().enumerate().filter(|(_, d)| d.0 > l.0) {
                stats.walls += 1;
                // Dense concrete only adds weight: no two-mix wall here makes freeboard.
                if sp.fb_fails.contains(&(ti, ri, gi)) {
                    stats.freeboard += 1;
                    continue;
                }
                if punching_dc(params, &l.1, l.0, t) > gate && punching_dc(params, &d.1, d.0, t) > gate {
                    stats.punching += 1;
                    continue;
                }
                tries.push(ZoneTry { pi, ri, di, light_lb: light_v * l.0 });
            }
        }
    }
    if stats.walls == 0 {
        let list = |a: &[(f64, StrengthFit, Option<String>)]| {
            a.iter()
                .map(|(r, _, m)| m.as_ref().map_or(format!("{r:.1}"), |m| format!("{m} {r:.1}")))
                .collect::<Vec<_>>()
                .join(", ")
        };
        stats.note = Some(if sp.axis.iter().all(|l| sp.dense.iter().all(|d| d.0 <= l.0)) {
            format!(
                "no dense mix heavier than a light one: light (search.density_pcf) {} pcf, dense (zones.dense_pcf) {} pcf",
                list(sp.axis),
                list(sp.dense)
            )
        } else {
            "no wall (thickness, depth, flare) had buildable topsides and resolved seats".into()
        });
    }
    tries.sort_by(|a, b| a.light_lb.total_cmp(&b.light_lb));
    let mut best = lightest;
    let mut out = Vec::new();
    let mut next = 0;
    while next < tries.len() {
        let room = z.max_checks - stats.checked;
        let batch: Vec<&ZoneTry> = tries[next..]
            .iter()
            .take_while(|tr| best.is_none_or(|b| tr.light_lb < b))
            .take(BATCH.min(room))
            .collect();
        if batch.is_empty() {
            break;
        }
        next += batch.len();
        let total = total0 + stats.checked + batch.len();
        let cs: Vec<(Option<Candidate>, Stopped)> = batch
            .par_iter()
            .map(|tr| {
                let (c, light) = place_zones(params, z, sp, tr);
                let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                if let Some(c) = &c {
                    on_progress(Progress::Candidate { done: k, total, candidate: c.clone() });
                }
                (c, light)
            })
            .collect();
        stats.checked += cs.len();
        stats.freeboard += cs.iter().filter(|x| x.1 == Stopped::Freeboard).count();
        for c in cs.into_iter().filter_map(|x| x.0) {
            if c.passes && best.is_none_or(|b| c.hull_weight_lb < b) {
                best = Some(c.hull_weight_lb);
            }
            out.push(c);
        }
        if stats.checked >= z.max_checks {
            break;
        }
    }
    let left = &tries[next..];
    stats.heavier = left.iter().filter(|tr| best.is_some_and(|b| tr.light_lb >= b)).count();
    stats.capped = left.len() > stats.heavier;
    on_progress(Progress::Started { total: total0 + stats.checked });
    (out, stats)
}

/// Drag of single-mix candidate `c` on `hull` (its depth, flare, wall and seats re-solved as
/// the search did), for a reported candidate the search left without one. None when it cannot
/// be computed.
pub fn candidate_drag(params: &Params, hull: &Hull, c: &Candidate) -> Option<f64> {
    if c.zones.is_some() {
        return None;
    }
    let fit = strength(params).ok()?;
    let axis = density_axis(params, &fit).ok()?;
    let rho_fit = axis.iter().find(|a| a.0 == c.density_pcf && a.2 == c.mix)?.1.clone();
    let grid = Grid::build_topsides(hull, c.depth_in, params.hull.stations, candidate_topsides(params, c)).0;
    let sec = sections(&grid, c.thickness_in);
    let plan = plan_seats(params, &rho_fit, &grid, &sec, c.thickness_in, c.density_pcf)
        .map_err(|e| format!("{e:#}"));
    let wall = Wall::Uniform(c.density_pcf);
    evaluate_wall(params, &rho_fit, &grid, &sec, c.thickness_in, wall, &plan, false, true).drag_n
}

/// Builds a zoned wall for a grid (the winner's layout at a verification resolution).
type ZonedMaker<'a> = dyn Fn(&Grid) -> ZonedWall + Sync + 'a;

/// The winner record of candidate `c`: re-checked at 2n-1 stations, its station convergence,
/// per-case diagrams, empty float, worked calculation and search space. `wall_fit` is the
/// winner mix's strength (`fit` the run's); `zoned` builds its zoned wall on a grid.
#[allow(clippy::too_many_arguments)]
fn finish_winner(
    params: &Params,
    fit: &StrengthFit,
    wall_fit: &StrengthFit,
    hull: &Hull,
    c: &Candidate,
    plan: &Result<Vec<CaseSeats>, String>,
    zoned: Option<&ZonedMaker>,
    on_progress: &(dyn Fn(Progress) + Sync),
) -> Result<Winner> {
    let stations = 2 * params.hull.stations - 1;
    on_progress(Progress::Verifying { stations });
    let grid = Grid::build_topsides(hull, c.depth_in, stations, candidate_topsides(params, c)).0;
    let sec = sections(&grid, c.thickness_in);
    let zw = zoned.map(|mk| mk(&grid));
    let wall = zw.as_ref().map_or(Wall::Uniform(c.density_pcf), Wall::Zoned);
    let v = evaluate_wall(params, wall_fit, &grid, &sec, c.thickness_in, wall, plan, false, true);
    // A winner passed, so its seats resolved.
    let seats = plan.as_ref().map_err(|e| anyhow::anyhow!("winner seats: {e}"))?;
    let cases = all_case_diagrams_wall(params, wall_fit, &grid, &sec, c.thickness_in, wall, seats);
    let empty = empty_float_wall(params, &grid, &sec, wall);
    let calc = match (&c.governing_case, zoned) {
        (_, Some(_)) => CalcTrace {
            error: Some(
                "the worked calculation covers single-mix walls; a zoned wall's checks and D/C(x) are above"
                    .into(),
            ),
            calc: None,
        },
        (Some(g), None) => match params.load_cases.iter().position(|lc| &lc.name == g) {
            Some(gi) => {
                calc_trace(params, wall_fit, &grid, &sec, c.thickness_in, c.density_pcf, g, &seats[gi])
            }
            None => CalcTrace { error: Some(format!("unknown governing case {g}")), calc: None },
        },
        (None, None) => CalcTrace { error: Some("no governing case".into()), calc: None },
    };
    let verify_dc = v.verdict.as_ref().map_or(f64::MAX, |v| v.dc);
    let conv = convergence(params, wall_fit, hull, c, seats, zoned);
    let dc_ok = v.verdict.is_some()
        && match conv.extrapolated {
            Some(x) => x.max(conv.dc[2]) <= params.gates.dc_verify,
            None => verify_dc <= params.gates.dc_verify,
        };
    // Every other gate at the verification limit at the finer stations (the shared list): a
    // design the search passed by a hair can miss it there.
    let others: Vec<String> = match v.verdict {
        Some(_) => Gate::ALL[1..].iter().filter_map(|g| g.miss(params, &v, false, stations)).collect(),
        None => Vec::new(),
    };
    let verified = dc_ok && others.is_empty();
    let verify_failure = (!verified).then(|| {
        let mut why = Vec::new();
        match (v.verdict.is_some(), conv.extrapolated) {
            (false, _) => why.push("no verdict at the verification stations".to_string()),
            (true, _) if dc_ok => {}
            (true, Some(x)) => why.push(format!(
                "D/C at finer stations {:.4} > dc_verify {}",
                x.max(conv.dc[2]),
                params.gates.dc_verify
            )),
            (true, None) => why.push(format!(
                "D/C at {stations} stations {verify_dc:.4} > dc_verify {}",
                params.gates.dc_verify
            )),
        }
        why.extend(others.iter().cloned());
        why.join("; ")
    });
    let gates = gate_slack(params, c, &v, &cases, &conv);
    let search_space = search_space(params, fit, c)?;
    let walls = &search_space[..3];
    let mut candidate = c.clone();
    if candidate.drag_n.is_none() {
        candidate.drag_n = v.drag_n;
    }
    Ok(Winner {
        candidate,
        verify_stations: stations,
        verify_dc,
        verified,
        dc_extrapolated: conv.extrapolated,
        dc_order: conv.order,
        convergence: conv,
        on_grid_edge: walls.iter().any(|a| a.at_bound.is_some() && a.limit.is_none()),
        at_mix_data_edge: walls[1].limit == Some("mix data"),
        mix_provenance: fit.provenance(c.density_pcf, c.mix.as_deref())?,
        dense_mix_provenance: match &c.zones {
            Some(z) => Some(fit.provenance(z.dense_pcf, c.dense_mix.as_deref())?),
            None => None,
        },
        search_space,
        diagrams: cases
            .iter()
            .find(|d| v.governing_case.as_deref() == Some(d.name.as_str()) && d.error.is_none())
            .map(|d| d.diagrams.clone())
            .unwrap_or_default(),
        cases,
        empty,
        calc,
        // Once on the final winner (`winner_case_calcs`), not on every shape's inner winner.
        case_calcs: Vec::new(),
        verify_failure,
        stands: None,
        knee_split_sensitivity: Vec::new(),
        curves: None,
        gates,
    })
}

/// Power-speed and righting-lever (GZ) curves of the winner, once after the search (called by
/// [`crate::run`]): one power curve per load case (hull + real crew), one GZ curve per load case
/// and the empty canoe, on the search-resolution grid. Pictures only: nothing is gated on them.
/// The worked calculation of every load case of the final winner (`Winner.case_calcs`, the PDF
/// report's proof), once after the search (called by [`crate::run`]): at verification
/// resolution, each case at its resolved seats, with the winning mix's strength. Single-mix
/// walls only (a zoned wall keeps an empty list; its `calc` says why).
pub fn winner_case_calcs(params: &Params, r: &mut RunResult) {
    let RunResult { winner, closest, winner_hull, strength, .. } = r;
    let Some(hull) = winner_hull.as_ref() else { return };
    for w in winner.iter_mut().chain(closest.iter_mut().map(|c| &mut c.design)) {
        case_calcs_on(params, strength, hull, w);
    }
}

fn case_calcs_on(params: &Params, strength: &StrengthFit, hull: &Hull, w: &mut Winner) {
    let c = &w.candidate;
    if c.zones.is_some() {
        return;
    }
    let mut wp = params.clone();
    if let Some(gr) = c.shape.as_ref().and_then(|s| s.gunwale_radius_in) {
        wp.hull.gunwale_radius_in = Some(gr);
    }
    let err = |e: String| vec![CalcTrace { error: Some(e), calc: None }; params.load_cases.len()];
    let fit = match density_axis(&wp, strength) {
        Ok(axis) => match axis.into_iter().find(|a| a.0 == c.density_pcf && a.2 == c.mix) {
            Some(a) => a.1,
            None => {
                w.case_calcs = err("winner mix not on the density axis".into());
                return;
            }
        },
        Err(e) => {
            w.case_calcs = err(format!("{e:#}"));
            return;
        }
    };
    let stations = 2 * params.hull.stations - 1;
    let grid = Grid::build_topsides(hull, c.depth_in, stations, candidate_topsides(&wp, c)).0;
    let sec = sections(&grid, c.thickness_in);
    w.case_calcs = params
        .load_cases
        .iter()
        .zip(&w.cases)
        .map(|(lc, k)| {
            let seats = CaseSeats {
                seats_in: k.seats_in.clone(),
                crew_order: k.crew_order.clone(),
                source: k.seat_source.unwrap_or(SeatSource::Given),
                planned_rho_pcf: None,
            };
            calc_trace(&wp, &fit, &grid, &sec, c.thickness_in, c.density_pcf, &lc.name, &seats)
        })
        .collect();
}

pub fn winner_curves(params: &Params, r: &mut RunResult) {
    let RunResult { winner, closest, winner_hull, .. } = r;
    let Some(hull) = winner_hull.as_ref() else { return };
    for w in winner.iter_mut().chain(closest.iter_mut().map(|c| &mut c.design)) {
        curves_on(params, hull, w);
    }
}

fn curves_on(params: &Params, hull: &Hull, w: &mut Winner) {
    let c = &w.candidate;
    let mut out = crate::curves::Curves {
        speeds_fps: crate::curves::speeds_fps(),
        angles_deg: crate::curves::angles_deg(),
        ..Default::default()
    };
    if c.zones.is_some() {
        out.error = Some("the curves cover single-mix walls, not a two-mix wall".into());
        w.curves = Some(out);
        return;
    }
    let mut wp = params.clone();
    if let Some(gr) = c.shape.as_ref().and_then(|s| s.gunwale_radius_in) {
        wp.hull.gunwale_radius_in = Some(gr);
    }
    let grid = Grid::build_topsides(hull, c.depth_in, params.hull.stations, candidate_topsides(&wp, c)).0;
    let sec = sections(&grid, c.thickness_in);
    let table = grid_hull(&grid);
    let cg = params.stability.crew_cg_in;
    let mut loadings: Vec<(String, f64)> =
        params.load_cases.iter().map(|lc| (lc.name.clone(), lc.weights_lb.iter().sum())).collect();
    for (name, crew) in &loadings {
        let (wt, _) = crate::curves::weight_and_kg(&grid, &sec, c.density_pcf, *crew, cg);
        out.power.push(crate::curves::power_curve(
            &table,
            name,
            *crew,
            wt,
            &out.speeds_fps,
            params.hull.roughness_um,
        ));
    }
    // Drag at the objective speed, worked per load case and per objective displacement (the
    // search's own table and hull weight, so the weighted sum is the candidate's drag).
    let (speed, ks) = (params.objective.speed_mps, params.hull.roughness_um);
    out.drag_speed_mps = speed;
    out.roughness_um = ks;
    for (name, crew) in &loadings {
        out.drag.push(crate::curves::drag_case(
            &table,
            name,
            *crew,
            c.hull_weight_lb + crew,
            speed,
            ks,
            None,
        ));
    }
    for d in params.objective.displacements.iter().flatten() {
        out.objective_drag.push(crate::curves::drag_case(
            &table,
            &d.label(),
            d.crew_lb,
            c.hull_weight_lb + d.crew_lb,
            speed,
            ks,
            Some(d.weight),
        ));
    }
    loadings.push(("empty".into(), 0.0));
    for (name, crew) in &loadings {
        let (wt, kg) = crate::curves::weight_and_kg(&grid, &sec, c.density_pcf, *crew, cg);
        out.gz.push(crate::curves::gz_curve(&grid, name, wt, kg, &out.angles_deg));
    }
    w.curves = Some(out);
}

/// Post-search checks of the winner, once (called by [`crate::run`] after either search):
/// the knee-split sensitivity and, with `[stands]`, the empty hull on two stands at the
/// verification station count. A stands D/C over `dc_verify`, or a stands case that cannot be
/// computed, makes the winner unverified with the reason in `verify_failure`.
pub fn verify_stands(params: &Params, r: &mut RunResult) {
    let RunResult { winner, closest, winner_hull, strength, .. } = r;
    let Some(hull) = winner_hull.as_ref() else { return };
    for w in winner.iter_mut().chain(closest.iter_mut().map(|c| &mut c.design)) {
        stands_on(params, strength, hull, w);
    }
}

fn stands_on(params: &Params, fit: &StrengthFit, hull: &Hull, w: &mut Winner) {
    let c = &w.candidate;
    let mat = &params.material;
    let live = match mat.method {
        crate::params::DesignMethod::Asd => 1.0,
        crate::params::DesignMethod::Lrfd => mat.lrfd_live,
    };
    let paddler = params.load_cases.iter().flat_map(|lc| lc.weights_lb.iter()).cloned().fold(0.0, f64::max)
        * mat.dynamic_load_factor
        * live;
    if let Some(pc) = c.verdict.as_ref().and_then(|v| v.checks.iter().find(|k| k.name == "punching")) {
        w.knee_split_sensitivity = crate::stands::knee_split_sensitivity(
            paddler,
            c.thickness_in,
            mat.knee_patch_in,
            &params.punching,
            pc.capacity,
        );
    }
    let Some(sp) = &params.stands else { return };
    let case = stands_case(params, fit, hull, c, sp);
    let failure = match (&case.error, &case.calc) {
        (Some(e), _) => Some(format!("stands: {e}")),
        // NaN fails too.
        (None, Some(s))
            if !matches!(
                s.verdict.dc.partial_cmp(&params.gates.dc_verify),
                Some(std::cmp::Ordering::Less | std::cmp::Ordering::Equal)
            ) =>
        {
            Some(format!(
                "stands: {} D/C {:.4} > dc_verify {}",
                s.verdict.governing_check, s.verdict.dc, params.gates.dc_verify
            ))
        }
        _ => None,
    };
    if let Some(f) = failure {
        w.verified = false;
        w.verify_failure = Some(match w.verify_failure.take() {
            Some(prev) => format!("{prev}; {f}"),
            None => f,
        });
    }
    w.stands = Some(case);
}

fn stands_case(
    params: &Params,
    fit: &StrengthFit,
    hull: &Hull,
    c: &Candidate,
    sp: &crate::params::Stands,
) -> crate::stands::StandsCase {
    let calc = || -> Result<crate::stands::StandsCalc> {
        if c.zones.is_some() {
            anyhow::bail!("the stands case covers single-mix walls, not a two-mix wall");
        }
        // The winner's own strength (its tested mix in tested_mixes mode).
        let axis = density_axis(params, fit)?;
        let wall_fit = axis
            .iter()
            .find(|(rho, _, name)| *rho == c.density_pcf && name.as_deref() == c.mix.as_deref())
            .map(|a| &a.1)
            .ok_or_else(|| anyhow::anyhow!("winner density {} not on the density axis", c.density_pcf))?;
        // A shape winner's gunwale radius is in its shape (as in shapeopt::for_shape).
        let mut wp = params.clone();
        if let Some(r) = c.shape.as_ref().and_then(|s| s.gunwale_radius_in) {
            wp.hull.gunwale_radius_in = Some(r);
        }
        let stations = 2 * params.hull.stations - 1;
        let grid = Grid::build_topsides(hull, c.depth_in, stations, candidate_topsides(&wp, c)).0;
        let sec = sections(&grid, c.thickness_in);
        crate::stands::optimize_position(
            &grid,
            &sec,
            c.density_pcf,
            &params.material,
            wall_fit,
            &params.punching,
            sp,
        )
    };
    match calc() {
        Ok(s) => crate::stands::StandsCase { error: None, calc: Some(s) },
        Err(e) => crate::stands::StandsCase { error: Some(format!("{e:#}")), calc: None },
    }
}

/// Knee x per paddler for `case` without auto seats: `seats_in`, or evenly spaced (pinned
/// `positions` kept).
pub fn resolved_seats(case: &crate::params::LoadCase, grid: &Grid) -> Vec<f64> {
    match &case.seats_in {
        Some(s) => s.clone(),
        None => seats::even_placed(case, grid),
    }
}

/// Every load case of the winner, in `params.load_cases` order.
pub fn all_case_diagrams(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    seats: &[CaseSeats],
) -> Vec<CaseDiagrams> {
    all_case_diagrams_wall(params, fit, grid, sec, t, Wall::Uniform(rho), seats)
}

/// [`all_case_diagrams`] for any wall.
pub fn all_case_diagrams_wall(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    wall: Wall,
    seats: &[CaseSeats],
) -> Vec<CaseDiagrams> {
    params
        .load_cases
        .iter()
        .zip(seats)
        .map(|(lc, s)| match case_diagrams_wall(params, fit, grid, sec, t, wall, &lc.name, s) {
            Ok(c) => c,
            Err(e) => CaseDiagrams {
                name: lc.name.clone(),
                seats_in: s.seats_in.clone(),
                crew_order: s.crew_order.clone(),
                seat_source: Some(s.source),
                seat_variance_in: params.seats.variance_in,
                weights_lb: lc.weights_lb.clone(),
                error: Some(format!("{e:#}")),
                ..Default::default()
            },
        })
        .collect()
}

/// One load case at fixed seats: service equilibrium (draft, trim, GM,
/// freeboard) and the governing strength combination (`material.method`).
pub struct CaseEval {
    pub service: Loads,
    /// Loads of the combination with the highest D/C (= service for asd).
    pub strength: Loads,
    /// Verdict of that combination; freeboard from the service loads.
    pub verdict: Verdict,
    /// (dead, live) load factors of that combination ((1, 1) for asd).
    pub combo: (f64, f64),
}

/// Evaluate `case` with paddlers at `seats`. LRFD: each combination
/// (dead, live) re-solves equilibrium with self weight x dead and crew
/// (incl. dynamic factor) x live; buoyancy balances the factored weight.
/// A two-mix zoned wall on one grid, ready to check.
pub struct ZonedWall {
    pub zl: ZoneLayout,
    pub zs: ZonedSec,
    /// Dense / light elastic modulus.
    pub n: f64,
    /// Strength of the light and the dense mix.
    pub fits: [StrengthFit; 2],
    /// Every paddler's knee footprint (nominal and +/- seat variance) is dense ([`knees_dense`]).
    pub knee_dense: bool,
}

impl ZonedWall {
    pub fn new(
        params: &Params,
        grid: &Grid,
        t: f64,
        zl: ZoneLayout,
        fits: [StrengthFit; 2],
        seats: &[CaseSeats],
    ) -> Self {
        let n = modulus_psi(zl.dense_pcf, fits[1].fc_psi(zl.dense_pcf))
            / modulus_psi(zl.light_pcf, fits[0].fc_psi(zl.light_pcf));
        let zs = zoned_sections(grid, t, &zl, n);
        let knee_dense = knees_dense(params, grid, t, &zl, seats);
        ZonedWall { zl, zs, n, fits, knee_dense }
    }
}

/// What the wall is made of.
#[derive(Clone, Copy)]
pub enum Wall<'a> {
    Uniform(f64),
    Zoned(&'a ZonedWall),
}

impl Wall<'_> {
    /// The (light) mix density.
    fn rho(&self) -> f64 {
        match self {
            Wall::Uniform(r) => *r,
            Wall::Zoned(z) => z.zl.light_pcf,
        }
    }
    fn weight(&self, grid: &Grid, sec: &Sections) -> f64 {
        match self {
            Wall::Uniform(r) => hull_weight(grid, sec, *r),
            Wall::Zoned(z) => grid
                .x
                .windows(2)
                .zip(z.zs.weight.windows(2))
                .map(|(x, w)| 0.5 * (w[0] + w[1]) * (x[1] - x[0]))
                .sum(),
        }
    }
    fn solve(
        &self,
        grid: &Grid,
        sec: &Sections,
        dead: f64,
        crew: &[CrewLoad],
        patch: f64,
        cg: f64,
    ) -> Result<Loads> {
        match self {
            Wall::Uniform(r) => solve(grid, sec, r * dead, crew, patch, cg),
            Wall::Zoned(z) => {
                let w: Vec<f64> = z.zs.weight.iter().map(|w| w * dead).collect();
                solve_weights(grid, &w, &z.zs.zg, crew, patch, cg)
            }
        }
    }
}

/// Half-breadth of `grid` at (x, z), linear between stations and waterlines.
fn half_breadth_at(grid: &Grid, x: f64, z: f64) -> f64 {
    let (xs, zs) = (&grid.x, &grid.z);
    let i = xs.partition_point(|&v| v <= x).clamp(1, xs.len() - 1);
    let k = zs.partition_point(|&v| v <= z).clamp(1, zs.len() - 1);
    let (u, w) = (
        ((x - xs[i - 1]) / (xs[i] - xs[i - 1])).clamp(0.0, 1.0),
        ((z - zs[k - 1]) / (zs[k] - zs[k - 1])).clamp(0.0, 1.0),
    );
    let at = |ii: usize| grid.y(ii, k - 1) * (1.0 - w) + grid.y(ii, k) * w;
    at(i - 1) * (1.0 - u) + at(i) * u
}

/// Knee punching footprint: (r, across) = reach along the hull around each knee ((patch + d)/2,
/// the punching perimeter) and half-width across it (half the knee spacing with two knees, + r).
fn knee_footprint(params: &Params, t: f64) -> (f64, f64) {
    let pu = &params.punching;
    let d = pu.d_over_t().unwrap_or(0.5) * t;
    let r = 0.5 * (params.material.knee_patch_in + d);
    (r, if pu.knees == 2 { 0.5 * pu.knee_spacing_in } else { 0.0 } + r)
}

/// Height at station x below which all the wall lies within `across` of the centreline (the
/// first z where the half-breadth reaches it, linear between waterlines); None if it never does.
fn footprint_top(grid: &Grid, x: f64, across: f64) -> Option<f64> {
    let z = &grid.z;
    let hb = |k: usize| half_breadth_at(grid, x, z[k]);
    if hb(0) >= across {
        return Some(z[0]);
    }
    (1..z.len()).find(|&k| hb(k) >= across).map(|k| {
        let (a, b) = (hb(k - 1), hb(k));
        z[k - 1] + (z[k] - z[k - 1]) * ((across - a) / (b - a)).clamp(0.0, 1.0)
    })
}

/// Sample positions over x0..x1: both ends and the stations between.
fn xs_over(grid: &Grid, x0: f64, x1: f64) -> Vec<f64> {
    let mut v = vec![x0];
    v.extend(grid.x.iter().copied().filter(|&x| x > x0 && x < x1));
    v.push(x1);
    v
}

/// Side-view region of each knee's punching footprint over its seat range (nominal +/- seat
/// variance), one per paddler and case: x = seat -/+ (variance + r), z from the keel up to the
/// highest [`footprint_top`] over it. None if a footprint is wider than the hull somewhere.
pub fn knee_regions(params: &Params, grid: &Grid, t: f64, seats: &[CaseSeats]) -> Option<Vec<Rect>> {
    let (r, across) = knee_footprint(params, t);
    let var = params.seats.variance_in;
    let (lo, hi) = (grid.x[0], grid.x[grid.nx() - 1]);
    seats
        .iter()
        .flat_map(|s| &s.seats_in)
        .map(|&x| {
            let (x0, x1) = ((x - var - r).max(lo), (x + var + r).min(hi));
            let top = xs_over(grid, x0, x1)
                .into_iter()
                .map(|xx| footprint_top(grid, xx, across))
                .try_fold(0.0_f64, |m, z| z.map(|z| m.max(z)))?;
            Some(Rect { x0_in: x0, x1_in: x1, z0_in: 0.0, z1_in: top })
        })
        .collect()
}

/// Is every knee's punching footprint dense, over its whole seat range (see [`knee_regions`])?
/// Then punching uses the dense mix, else the light one.
pub fn knees_dense(params: &Params, grid: &Grid, t: f64, zl: &ZoneLayout, seats: &[CaseSeats]) -> bool {
    if zl.dense_pcf <= zl.light_pcf || zl.dense.is_empty() {
        return false;
    }
    let (r, across) = knee_footprint(params, t);
    let var = params.seats.variance_in;
    let (lo, hi) = (grid.x[0], grid.x[grid.nx() - 1]);
    seats.iter().flat_map(|s| &s.seats_in).all(|&x| {
        xs_over(grid, (x - var - r).max(lo), (x + var + r).min(hi))
            .into_iter()
            .all(|xx| footprint_top(grid, xx, across).is_some_and(|top| zl.covers(xx, 0.0, top)))
    })
}

pub fn eval_case(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    case: &LoadCase,
    seats: &[f64],
) -> Result<CaseEval> {
    eval_case_wall(params, fit, grid, sec, Wall::Uniform(rho), case, seats)
}

/// [`eval_case`] for any wall (`fit` is the single mix's; a zoned wall carries its own).
#[allow(clippy::too_many_arguments)]
pub fn eval_case_wall(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    wall: Wall,
    case: &LoadCase,
    seats: &[f64],
) -> Result<CaseEval> {
    let mat = &params.material;
    // Crew at `f` x its weight: 1 for the float (freeboard, GM, drag: the real crew), dynamic
    // factor x live factor for the stress checks.
    let crew = |f: f64| -> Vec<CrewLoad> {
        seats.iter().zip(&case.weights_lb).map(|(&x_in, &w)| CrewLoad { x_in, lb: w * f }).collect()
    };
    let (patch, cg) = (mat.knee_patch_in, params.stability.crew_cg_in);
    let service = wall.solve(grid, sec, 1.0, &crew(1.0), patch, cg)?;
    // At these seats, where the gate looks (the lettering, else the whole hull).
    let fb = freeboard_over(grid, &service, &fb_spans(params, grid)?.1).0;
    let max_paddler =
        case.weights_lb.iter().cloned().fold(f64::NEG_INFINITY, f64::max) * mat.dynamic_load_factor;
    let mut best: Option<(Loads, Verdict, (f64, f64))> = None;
    for (dead, live) in mat.combos() {
        let f = mat.dynamic_load_factor * live;
        let loads = if (dead, f) == (1.0, 1.0) {
            service.clone()
        } else {
            wall.solve(grid, sec, dead, &crew(f), patch, cg)?
        };
        let mut v = match wall {
            Wall::Uniform(rho) => check(
                grid,
                sec,
                &loads,
                rho,
                mat,
                fit,
                max_paddler * live,
                &params.punching,
                params.turning.as_ref(),
            ),
            Wall::Zoned(z) => check_zoned(
                grid,
                sec,
                &z.zs,
                &loads,
                &z.zl,
                z.n,
                mat,
                [&z.fits[0], &z.fits[1]],
                max_paddler * live,
                z.knee_dense,
                &params.punching,
                params.turning.as_ref(),
            ),
        };
        v.freeboard_in = fb;
        if best.as_ref().is_none_or(|(_, b, _)| v.dc.is_nan() || v.dc > b.dc) {
            best = Some((loads, v, (dead, live)));
        }
    }
    let (strength, verdict, combo) = best.ok_or_else(|| anyhow::anyhow!("no load combinations"))?;
    Ok(CaseEval { service, strength, verdict, combo })
}

/// Seats of every load case on this wall (`params.load_cases` order): given,
/// even, or auto (src/seats.rs, scored with [`eval_case`]).
pub fn plan_seats(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
) -> Result<Vec<CaseSeats>> {
    params
        .load_cases
        .iter()
        .map(|case| match seats::fixed(params, case, grid) {
            Some(s) => Ok(s),
            None => {
                let exact = |s: &[f64], bound: Option<&seats::SeatScore>| {
                    seat_score_bounded(params, fit, grid, sec, rho, case, s, bound)
                };
                seats::auto(params, fit, grid, sec, t, rho, case, &exact)
                    .map_err(|e| anyhow::anyhow!("{}: {e:#}", case.name))
            }
        })
        .collect()
}

/// The exact [`seats::SeatScore`] of `case` with knees at `seats` on a uniform `rho` wall: the
/// worst seat-affected D/C and the lowest GM over the same seat samples the gate checks
/// ([`seats::samples`]: nominal and every corner), and the freeboard the gate sees at these seats.
#[allow(clippy::too_many_arguments)]
pub fn seat_score(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    case: &LoadCase,
    seats: &[f64],
) -> Result<seats::SeatScore> {
    let s = seat_score_bounded(params, fit, grid, sec, rho, case, seats, None)?;
    Ok(s.expect("an unbounded score is always computed"))
}

/// [`seat_score`], stopping with None once the samples so far already score no better than
/// `bound`: more samples only raise the worst D/C and lower the worst GM (the freeboard is the
/// nominal one), so the full score could not beat it either.
#[allow(clippy::too_many_arguments)]
fn seat_score_bounded(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    case: &LoadCase,
    seats: &[f64],
    bound: Option<&seats::SeatScore>,
) -> Result<Option<seats::SeatScore>> {
    let spans = fb_spans(params, grid)?.1;
    let (mut dc, mut gm, mut fb) = (0.0_f64, f64::INFINITY, f64::NAN);
    // Nominal first (the freeboard), then the whole crew aft and forward (GM's worst corners),
    // then the rest: a set that cannot win is usually dropped after 3 samples. The order does
    // not change the score.
    let mut all = seats::samples(seats, params.seats.variance_in, grid);
    if all.len() > 2 {
        let aft = all.pop().expect("more than 2 samples");
        all.insert(1, aft);
    }
    for (k, q) in all.into_iter().enumerate() {
        let e = eval_case(params, fit, grid, sec, rho, case, &q)?;
        if k == 0 {
            // The first sample is the nominal seats, where the gate looks.
            fb = gate_freeboard(params, grid, sec, Wall::Uniform(rho), case, &q, &e.service, &spans)?
                .freeboard_in;
        }
        (dc, gm) = (dc.max(seats::seat_dc(&e.verdict)), gm.min(e.service.gm_in));
        if bound.is_some_and(|b| !seats::SeatScore::of(params, dc, fb, gm).better(b)) {
            return Ok(None);
        }
    }
    Ok(Some(seats::SeatScore::of(params, dc, fb, gm)))
}

/// A load case checked over its seat samples (`seats::samples`: nominal,
/// then the +/- `seats.variance_in` corners).
pub struct CaseEnvelope {
    /// At the nominal seats.
    pub nominal: CaseEval,
    /// Highest-D/C sample's verdict, freeboard = the gated one (`freeboard.gate`).
    pub verdict: Verdict,
    /// Min GM_T over the samples.
    pub gm_in: f64,
    /// Seats of the sample `verdict` comes from.
    pub verdict_seats_in: Vec<f64>,
    /// Gated freeboard and its range over the seat samples.
    pub freeboard: FreeboardRange,
}

/// The service float at one seat sample, as far as freeboard goes.
#[derive(Debug, Clone, Serialize, Default)]
pub struct FreeboardAt {
    /// Knee x per paddler (`weights_lb` order), in.
    pub seats_in: Vec<f64>,
    pub draft_in: f64,
    pub trim_in: f64,
    /// Where the gated freeboard is lowest (a lettering end, else a hull end), in from the bow,
    /// and the local waterline there above the keel, in (freeboard = depth - waterline).
    pub at_x_in: f64,
    pub waterline_in: f64,
    /// Freeboard where the gate looks (the lettering, else the whole hull), in.
    pub freeboard_in: f64,
    /// Lowest freeboard anywhere on the hull, in (= `freeboard_in` without lettering).
    pub hull_min_in: f64,
}

impl FreeboardAt {
    fn of(grid: &Grid, seats: &[f64], l: &Loads, spans: &[[f64; 2]]) -> Self {
        let (fb, x) = freeboard_over(grid, l, spans);
        FreeboardAt {
            seats_in: seats.to_vec(),
            draft_in: l.draft_in,
            trim_in: l.trim_in,
            at_x_in: x,
            waterline_in: grid.depth - fb,
            freeboard_in: fb,
            hull_min_in: freeboard(grid, l),
        }
    }
}

/// Freeboard of a load case. `gate` is what the freeboard gate sees: the lowest over the
/// marked seats and the whole crew moved +/- `gates.freeboard_seat_shift_in` (0 = the marked
/// seats only). `expected` (marked seats), `best` and `worst` span every strength seat sample
/// (each knee +/- `seats.variance_in`): how much the crew's position can move it.
#[derive(Debug, Clone, Serialize, Default)]
pub struct FreeboardRange {
    pub gate: FreeboardAt,
    pub expected: FreeboardAt,
    pub best: FreeboardAt,
    pub worst: FreeboardAt,
    /// `gates.freeboard_required_in()`, in.
    pub required_in: f64,
    pub seat_shift_in: f64,
    /// Where the gate looks (empty = the whole hull).
    pub lettering: Vec<crate::params::LetterSpan>,
}

/// Lettering spans of this hull (empty without `[lettering]`) and their x ranges.
fn fb_spans(params: &Params, grid: &Grid) -> Result<(Vec<crate::params::LetterSpan>, Vec<[f64; 2]>)> {
    let spans = params.freeboard_spans(grid.x[grid.nx() - 1])?;
    let x = spans.iter().map(|s| [s.x0_in, s.x1_in]).collect();
    Ok((spans, x))
}

/// The gated freeboard of `case` at `seats` (`nominal` = its service float there): the lowest
/// over the seats and the whole crew moved fore and aft by `gates.freeboard_seat_shift_in`
/// (freeboard depends on the crew only through its centroid, so those are the extremes).
#[allow(clippy::too_many_arguments)]
fn gate_freeboard(
    params: &Params,
    grid: &Grid,
    sec: &Sections,
    wall: Wall,
    case: &LoadCase,
    seats: &[f64],
    nominal: &Loads,
    spans: &[[f64; 2]],
) -> Result<FreeboardAt> {
    let (patch, cg) = (params.material.knee_patch_in, params.stability.crew_cg_in);
    let mut out = FreeboardAt::of(grid, seats, nominal, spans);
    for q in seats::shifted(seats, params.gates.freeboard_seat_shift_in, grid).into_iter().skip(1) {
        let crew: Vec<CrewLoad> =
            q.iter().zip(&case.weights_lb).map(|(&x_in, &lb)| CrewLoad { x_in, lb }).collect();
        let at = FreeboardAt::of(grid, &q, &wall.solve(grid, sec, 1.0, &crew, patch, cg)?, spans);
        if at.freeboard_in < out.freeboard_in {
            out = at;
        }
    }
    Ok(out)
}

/// Fails a gate (search D/C, freeboard, GM)?
fn misses_gate(params: &Params, v: &Verdict, gm: f64) -> bool {
    !(v.dc <= params.gates.dc_search
        && v.freeboard_in >= params.gates.freeboard_required_in()
        && gm >= params.stability.gm_min_in)
}

/// [`eval_case`] over the seat samples. `early_exit`: stop at the first
/// sample that misses a gate (the case fails either way, so the verdict is
/// then the worst sample evaluated, not necessarily the worst of all).
#[allow(clippy::too_many_arguments)]
pub fn eval_envelope(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    case: &LoadCase,
    seats: &[f64],
    early_exit: bool,
) -> Result<CaseEnvelope> {
    eval_envelope_wall(params, fit, grid, sec, Wall::Uniform(rho), case, seats, early_exit)
}

/// [`eval_envelope`] for any wall.
#[allow(clippy::too_many_arguments)]
pub fn eval_envelope_wall(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    wall: Wall,
    case: &LoadCase,
    seats: &[f64],
    early_exit: bool,
) -> Result<CaseEnvelope> {
    let mut all = seats::samples(seats, params.seats.variance_in, grid).into_iter();
    let first = all.next().unwrap_or_default();
    let nominal = eval_case_wall(params, fit, grid, sec, wall, case, &first)?;
    let (lettering, spans) = fb_spans(params, grid)?;
    let gate = gate_freeboard(params, grid, sec, wall, case, &first, &nominal.service, &spans)?;
    let at = FreeboardAt::of(grid, &first, &nominal.service, &spans);
    let mut verdict = nominal.verdict.clone();
    verdict.freeboard_in = gate.freeboard_in;
    let mut env = CaseEnvelope {
        verdict,
        gm_in: nominal.service.gm_in,
        verdict_seats_in: first,
        nominal,
        freeboard: FreeboardRange {
            gate,
            expected: at.clone(),
            best: at.clone(),
            worst: at,
            required_in: params.gates.freeboard_required_in(),
            seat_shift_in: params.gates.freeboard_seat_shift_in,
            lettering,
        },
    };
    for s in all {
        if early_exit && misses_gate(params, &env.verdict, env.gm_in) {
            break;
        }
        let e = eval_case_wall(params, fit, grid, sec, wall, case, &s)?;
        let at = FreeboardAt::of(grid, &s, &e.service, &spans);
        if at.freeboard_in > env.freeboard.best.freeboard_in {
            env.freeboard.best = at.clone();
        }
        if at.freeboard_in < env.freeboard.worst.freeboard_in {
            env.freeboard.worst = at;
        }
        env.gm_in = env.gm_in.min(e.service.gm_in);
        if e.verdict.dc.is_nan() || e.verdict.dc > env.verdict.dc {
            (env.verdict, env.verdict_seats_in) = (e.verdict, s);
        }
        // The freeboard gate is the gated value, not this strength sample's.
        env.verdict.freeboard_in = env.freeboard.gate.freeboard_in;
    }
    Ok(env)
}

/// Full per-case diagrams and per-station D/C (same masks and formulas as check.rs).
#[allow(clippy::too_many_arguments)]
pub fn case_diagrams(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case_name: &str,
    seats: &CaseSeats,
) -> Result<CaseDiagrams> {
    case_diagrams_wall(params, fit, grid, sec, t, Wall::Uniform(rho), case_name, seats)
}

/// [`case_diagrams`] for any wall: a zoned wall's per-station stresses and D/C are its governing
/// mix's (`check::zoned_stations`), inertia the transformed one.
#[allow(clippy::too_many_arguments)]
pub fn case_diagrams_wall(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    wall: Wall,
    case_name: &str,
    seats: &CaseSeats,
) -> Result<CaseDiagrams> {
    let case = params
        .load_cases
        .iter()
        .find(|c| c.name == case_name)
        .ok_or_else(|| anyhow::anyhow!("unknown load case {case_name}"))?;
    let n = grid.nx();
    let env = eval_envelope_wall(params, fit, grid, sec, wall, case, &seats.seats_in, false)?;
    let CaseEval { service, strength: loads, .. } = env.nominal;
    let verdict = env.verdict;
    let cap = |name: &str| verdict.checks.iter().find(|c| c.name == name).map_or(f64::NAN, |c| c.capacity);
    let (ft_cap, fc_cap, v_cap) = (cap("bending_tension"), cap("bending_compression"), cap("shear"));
    let dc = |s: f64, c: f64| if c.is_finite() && c > 0.0 { s / c } else { f64::INFINITY };
    // check.rs gives kupfer_dc f'c times the capacity factor; compression's
    // capacity is 0.85 of that (NaN when f'c <= 0, which kupfer_dc fails).
    let fc_kupfer = fc_cap / 0.85;
    let imax = sec.inertia.iter().cloned().fold(0.0, f64::max);
    let mut out = CaseDiagrams {
        name: case.name.clone(),
        seats_in: seats.seats_in.clone(),
        crew_order: seats.crew_order.clone(),
        seat_source: Some(seats.source),
        seat_variance_in: params.seats.variance_in,
        verdict_seats_in: env.verdict_seats_in,
        weights_lb: case.weights_lb.clone(),
        draft_in: Some(service.draft_in),
        trim_in: Some(service.trim_in),
        gm_in: Some(env.gm_in),
        freeboard_in: Some(verdict.freeboard_in),
        freeboard: Some(env.freeboard),
        compression_allow_psi: Some(fc_cap),
        shear_allow_psi: Some(v_cap),
        ..Default::default()
    };
    let mut tension_psi = Vec::with_capacity(n);
    if let Wall::Zoned(z) = wall {
        let (k, _) = crate::check::capacity_factors(&params.material, &params.punching);
        let caps = [
            crate::check::MixCaps::of(&z.fits[0], z.zl.light_pcf, k),
            crate::check::MixCaps::of(&z.fits[1], z.zl.dense_pcf, k),
        ];
        let r = |(d, c): (f64, f64)| if c.is_finite() && c > 0.0 { d / c } else { f64::INFINITY };
        for st in crate::check::zoned_stations(grid, &z.zs, &loads, z.n, caps, t, params.turning.as_ref()) {
            if st.masked {
                for v in [&mut tension_psi, &mut out.compression_psi, &mut out.shear_psi] {
                    v.push(0.0);
                }
                for v in [
                    &mut out.dc_bending_tension,
                    &mut out.dc_bending_compression,
                    &mut out.dc_shear,
                    &mut out.dc_biaxial,
                ] {
                    v.push(0.0);
                }
                continue;
            }
            tension_psi.push(st.tension.0);
            out.compression_psi.push(st.compression.0);
            out.shear_psi.push(st.shear.0);
            out.dc_bending_tension.push(r(st.tension));
            out.dc_bending_compression.push(r(st.compression));
            out.dc_shear.push(r(st.shear));
            out.dc_biaxial.push(st.biaxial);
        }
        out.diagrams = Diagrams {
            x_in: grid.x.clone(),
            tension_psi,
            tension_allow_psi: ft_cap,
            area_in2: sec.area.clone(),
            inertia_in4: z.zs.inertia.clone(),
            waterline_in: waterline(grid, &service),
            zbar_in: z.zs.zbar.clone(),
            buoyancy_lbpin: loads.buoyancy,
            self_weight_lbpin: loads.self_weight,
            shear_lb: loads.shear,
            moment_lbin: loads.moment,
        };
        out.verdict = Some(verdict);
        return Ok(out);
    }
    let hoop = crate::check::hoop_psi(grid, &loads, t, params.turning.as_ref());
    for (i, &hoop) in hoop.iter().enumerate() {
        let ii = sec.inertia[i];
        if ii <= 1e-3 * imax {
            for v in [&mut tension_psi, &mut out.compression_psi, &mut out.shear_psi] {
                v.push(0.0);
            }
            for v in [
                &mut out.dc_bending_tension,
                &mut out.dc_bending_compression,
                &mut out.dc_shear,
                &mut out.dc_biaxial,
            ] {
                v.push(0.0);
            }
            continue;
        }
        let m = loads.moment[i];
        let (c_top, c_bot) = (grid.depth - sec.zbar[i], sec.zbar[i]);
        let (ct, cc) = crate::check::fibres(m, c_top, c_bot);
        let st = m.abs() * ct / ii;
        let sc = m.abs() * cc / ii;
        let q = first_moment_below(grid.row(i), &grid.z, t, sec.zbar[i]);
        let tau = loads.shear[i].abs() * q / (ii * 2.0 * t);
        tension_psi.push(st);
        out.compression_psi.push(sc);
        out.shear_psi.push(tau);
        out.dc_bending_tension.push(dc(st, ft_cap));
        out.dc_bending_compression.push(dc(sc, fc_cap));
        out.dc_shear.push(dc(tau, v_cap));
        out.dc_biaxial.push(crate::check::kupfer_dc(st, sc, hoop, tau, ft_cap, fc_kupfer));
    }
    let waterline_in = waterline(grid, &service);
    out.diagrams = Diagrams {
        x_in: grid.x.clone(),
        tension_psi,
        tension_allow_psi: ft_cap,
        area_in2: sec.area.clone(),
        inertia_in4: sec.inertia.clone(),
        waterline_in,
        zbar_in: sec.zbar.clone(),
        buoyancy_lbpin: loads.buoyancy,
        self_weight_lbpin: loads.self_weight,
        shear_lb: loads.shear,
        moment_lbin: loads.moment,
    };
    out.verdict = Some(verdict);
    Ok(out)
}

/// One D/C series over station refinement (spacing h, h/2, h/4).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Series {
    /// Changes below 1e-6 relative: the value at the finest grid.
    Converged(f64),
    /// Monotone and contracting with observed order p in [0.5, 4]:
    /// f3 + (f3 - f2) / (2^p - 1).
    Extrapolated { value: f64, order: f64 },
    /// Anything else (oscillating, diverging, order out of range, non-finite).
    Irregular,
}

/// Richardson extrapolation of `f` at spacings h, h/2, h/4.
pub fn richardson(f: [f64; 3]) -> Series {
    if f.iter().any(|v| !v.is_finite()) {
        return Series::Irregular;
    }
    let (d1, d2) = (f[1] - f[0], f[2] - f[1]);
    let tol = 1e-6 * f[2].abs().max(1e-3);
    if d2.abs() <= tol && (d1.abs() <= tol || d1.abs() > 4.0 * d2.abs()) {
        return Series::Converged(f[2]);
    }
    if d1 * d2 <= 0.0 || d2.abs() >= d1.abs() {
        return Series::Irregular;
    }
    let order = (d1 / d2).log2();
    if !(0.5..=4.0).contains(&order) {
        return Series::Irregular;
    }
    Series::Extrapolated { value: f[2] + d2 / (order.exp2() - 1.0), order }
}

/// Winner D/C over station refinement (`Winner.convergence`).
#[derive(Debug, Clone, Serialize)]
pub struct Convergence {
    /// n, 2n-1, 4n-3: station spacing halves each time.
    pub stations: [usize; 3],
    /// D/C (max over load cases, checks and seat samples) at each.
    pub dc: [f64; 3],
    /// max over (case, check) series of the extrapolated (or converged)
    /// value. None = an irregular series could reach it (bound: its max + 2x
    /// its largest step); then `note` says which and the gate falls back.
    /// Irregular series below that bound are ignored and listed in `note`.
    pub extrapolated: Option<f64>,
    /// Order of the series that sets `extrapolated` (None = converged).
    pub order: Option<f64>,
    /// "case/check" of that series, or of the first irregular one.
    pub governing: Option<String>,
    pub note: Option<String>,
}

/// Every (case, check) D/C of the winner wall at n, 2n-1 and 4n-3 stations,
/// each the max over the seat samples, then [`richardson`] per series.
fn convergence(
    params: &Params,
    fit: &StrengthFit,
    hull: &Hull,
    c: &Candidate,
    seats: &[CaseSeats],
    zoned: Option<&ZonedMaker>,
) -> Convergence {
    let n = params.hull.stations;
    let stations = [n, 2 * n - 1, 4 * n - 3];
    let mut out = Convergence {
        stations,
        dc: [f64::NAN; 3],
        extrapolated: None,
        order: None,
        governing: None,
        note: None,
    };
    // series[case][check][resolution]
    let mut series: Vec<Vec<(&'static str, [f64; 3])>> = vec![Vec::new(); params.load_cases.len()];
    for (r, &ns) in stations.iter().enumerate() {
        let grid = Grid::build_topsides(hull, c.depth_in, ns, candidate_topsides(params, c)).0;
        let sec = sections(&grid, c.thickness_in);
        let zw = zoned.map(|mk| mk(&grid));
        let wall = zw.as_ref().map_or(Wall::Uniform(c.density_pcf), Wall::Zoned);
        for (k, (case, s)) in params.load_cases.iter().zip(seats).enumerate() {
            for sample in seats::samples(&s.seats_in, params.seats.variance_in, &grid) {
                let v = match eval_case_wall(params, fit, &grid, &sec, wall, case, &sample) {
                    Ok(e) => e.verdict,
                    Err(e) => {
                        out.note =
                            Some(format!("{} at {ns} stations: {e:#}; verify_dc gate used", case.name));
                        return out;
                    }
                };
                for (j, ch) in v.checks.iter().enumerate() {
                    if series[k].len() <= j {
                        series[k].push((ch.name, [f64::NEG_INFINITY; 3]));
                    }
                    let e = &mut series[k][j].1[r];
                    *e = e.max(ch.dc);
                }
            }
        }
    }
    let tagged: Vec<(String, [f64; 3])> = params
        .load_cases
        .iter()
        .zip(&series)
        .flat_map(|(case, ss)| ss.iter().map(move |&(name, f)| (format!("{}/{name}", case.name), f)))
        .collect();
    extrapolate(stations, &tagged)
}

/// [`Convergence`] from tagged ("case/check") D/C series at `stations`.
pub fn extrapolate(stations: [usize; 3], series: &[(String, [f64; 3])]) -> Convergence {
    let mut out = Convergence {
        stations,
        dc: [f64::NAN; 3],
        extrapolated: None,
        order: None,
        governing: None,
        note: None,
    };
    for r in 0..3 {
        out.dc[r] = series.iter().map(|s| s.1[r]).fold(f64::NEG_INFINITY, f64::max);
    }
    let mut best: Option<(f64, Option<f64>, String)> = None;
    // Irregular series: (tag, values, bound = max + 2 max|step|).
    let mut irregular: Vec<(String, [f64; 3], f64)> = Vec::new();
    for (tag, f) in series {
        let (tag, f) = (tag.clone(), *f);
        let (value, order) = match richardson(f) {
            Series::Converged(v) => (v, None),
            Series::Extrapolated { value, order } => (value, Some(order)),
            Series::Irregular => {
                let step = (f[1] - f[0]).abs().max((f[2] - f[1]).abs());
                irregular.push((tag, f, f[0].max(f[1]).max(f[2]) + 2.0 * step));
                continue;
            }
        };
        if best.as_ref().is_none_or(|b| value > b.0) {
            best = Some((value, order, tag));
        }
    }
    let top = best.as_ref().map_or(f64::NEG_INFINITY, |b| b.0);
    // An irregular series that could reach the governing value makes the
    // whole extrapolation untrustworthy; one far below it cannot govern.
    if let Some((tag, f, _)) = irregular.iter().find(|i| i.2.is_nan() || i.2 >= top) {
        out.governing = Some(tag.clone());
        out.note = Some(format!(
            "{tag} D/C {:.4} / {:.4} / {:.4} is not monotone with an order in [0.5, 4]; verify_dc gate used",
            f[0], f[1], f[2]
        ));
        return out;
    }
    if !irregular.is_empty() {
        let tags: Vec<&str> = irregular.iter().map(|i| i.0.as_str()).collect();
        out.note = Some(format!("irregular but far below the governing D/C: {}", tags.join(", ")));
    }
    if let Some((v, o, tag)) = best {
        (out.extrapolated, out.order, out.governing) = (Some(v), o, Some(tag));
    }
    out
}

/// Topsides candidate `c` was built with: the params' gunwale settings and its own flare.
pub fn candidate_topsides(params: &Params, c: &Candidate) -> Topsides {
    Topsides { flare_deg: c.flare_deg, ..params.hull.topsides() }
}

/// Is `b` a better winner than `a` (both passing) under the objective?
/// Weight: lighter. Drag: less drag, ties broken by weight.
fn better(params: &Params, b: &Candidate, a: &Candidate) -> bool {
    match (params.objective.kind, b.drag_n, a.drag_n) {
        (ObjectiveKind::Drag, Some(db), Some(da)) if db != da => db < da,
        _ => b.hull_weight_lb < a.hull_weight_lb,
    }
}

/// The analysis grid as a hull table (all rows; `resistance` clips at the draft).
fn grid_hull(grid: &Grid) -> Hull {
    let nz = grid.nz();
    Hull {
        format: crate::hull::FORMAT.into(),
        units: "in".into(),
        name: "grid".into(),
        source: String::new(),
        length: grid.x[grid.nx() - 1],
        draft_design: grid.z[nz - 1],
        x: grid.x.clone(),
        z: grid.z.clone(),
        half_breadth: (0..grid.nx()).map(|i| grid.row(i).to_vec()).collect(),
        stem: None,
    }
}

/// Self weight in lb: trapezoid of A*rho/1728 over x.
fn hull_weight(grid: &Grid, sec: &Sections, rho: f64) -> f64 {
    let w: f64 =
        grid.x.windows(2).zip(sec.area.windows(2)).map(|(x, a)| 0.5 * (a[0] + a[1]) * (x[1] - x[0])).sum();
    w * rho / 1728.0
}

/// One candidate over every load case at its planned seats (`plan`, Err = the
/// seats could not be resolved: the candidate fails with that reason).
#[allow(clippy::too_many_arguments)]
fn evaluate(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    plan: &Result<Vec<CaseSeats>, String>,
    early_exit: bool,
) -> Candidate {
    evaluate_wall(params, fit, grid, sec, t, Wall::Uniform(rho), plan, early_exit, search_drag(params))
}

/// Whether the search computes every candidate's drag: always on a fixed hull (every candidate
/// is reported), in a shape search only when drag is the objective (the inner candidates are
/// not reported; each shape's reported candidate gets its drag, see [`candidate_drag`]).
fn search_drag(params: &Params) -> bool {
    params.shape_search.is_none() || params.objective.kind == ObjectiveKind::Drag
}

/// [`evaluate`] for any wall.
#[allow(clippy::too_many_arguments)]
fn evaluate_wall(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    wall: Wall,
    plan: &Result<Vec<CaseSeats>, String>,
    early_exit: bool,
    want_drag: bool,
) -> Candidate {
    let rho = wall.rho();
    let beam = 2.0 * grid.y.iter().cloned().fold(0.0, f64::max);
    let mut c = Candidate {
        length_in: grid.x[grid.x.len() - 1] - grid.x[0],
        beam_in: beam,
        shape: None,
        gm_in: None,
        drag_n: None,
        race_s: None,
        air_pocket_in: None,
        gm_cases_in: vec![],
        gm_empty_in: None,
        thickness_in: t,
        density_pcf: rho,
        depth_in: grid.depth,
        flare_deg: params.hull.flare_deg,
        hull_weight_lb: wall.weight(grid, sec),
        mix: None,
        verdict: None,
        governing_case: None,
        error: None,
        passes: false,
        zones: match wall {
            Wall::Uniform(_) => None,
            Wall::Zoned(z) => Some(z.zl.clone()),
        },
        dense_mix: None,
    };
    let mut worst: Option<(Verdict, String)> = None;
    let (mut gm, mut fb) = (f64::INFINITY, f64::INFINITY);
    // (crew lb, midship draft) of the heaviest load case, for drag
    let mut heaviest: Option<(f64, f64)> = None;
    // shape_search.beam_in limits the canoe's widest point, flared topsides and gunwale included.
    if let Some(ss) = &params.shape_search
        && beam > ss.beam_in[1] + 1e-9
    {
        c.error = Some(format!(
            "beam {beam:.2} in overall (topsides to {:.1} in deep) > shape_search.beam_in max {} in",
            grid.depth, ss.beam_in[1]
        ));
        return c;
    }
    let plan = match plan {
        Ok(p) => p,
        Err(e) => {
            c.error = Some(format!("seats: {e}"));
            return c;
        }
    };
    // Auto seats are planned for one wall density (its weight and strength); a wall of another
    // density must not reuse them. Guards every caller, not one call site.
    if let Some((case, r)) = params
        .load_cases
        .iter()
        .zip(plan)
        .find_map(|(case, s)| s.planned_rho_pcf.filter(|&r| r != rho).map(|r| (case, r)))
    {
        c.error = Some(format!("seats: {} planned at {r} pcf, used on a {rho} pcf wall", case.name));
        return c;
    }
    // The empty canoe: GM always reported; an error only fails the candidate when it is gated.
    let (patch, cg) = (params.material.knee_patch_in, params.stability.crew_cg_in);
    match wall.solve(grid, sec, 1.0, &[], patch, cg) {
        Ok(l) => c.gm_empty_in = Some(l.gm_in),
        Err(e) if params.stability.gm_empty_min_in.is_some() => {
            c.error = Some(format!("empty canoe: {e:#}"));
            return c;
        }
        Err(_) => {}
    }
    for (case, seats) in params.load_cases.iter().zip(plan) {
        let v = match eval_envelope_wall(params, fit, grid, sec, wall, case, &seats.seats_in, early_exit) {
            Ok(env) => {
                gm = gm.min(env.gm_in);
                c.gm_cases_in.push(env.gm_in);
                let lb: f64 = case.weights_lb.iter().sum();
                if heaviest.is_none_or(|(h, _)| lb > h) {
                    heaviest = Some((lb, env.nominal.service.draft_in));
                }
                env.verdict
            }
            Err(e) => {
                c.error = Some(format!("{}: {e:#}", case.name));
                return c;
            }
        };
        // +inf D/C is a legitimate FAIL (a check had no capacity); NaN is a bug.
        if v.dc.is_nan() || !v.freeboard_in.is_finite() {
            c.error = Some(format!("{}: non-finite check result", case.name));
            return c;
        }
        fb = fb.min(v.freeboard_in);
        if worst.as_ref().is_none_or(|(w, _)| v.dc > w.dc) {
            worst = Some((v, case.name.clone()));
        }
    }
    if let Some((mut v, name)) = worst {
        // The freeboard gate covers every case, not just the governing one.
        v.freeboard_in = fb;
        c.gm_in = Some(gm);
        // Drag is reported (it only fails the candidate when it is the objective).
        if want_drag {
            let draft = heaviest.map(|h| h.1).unwrap_or(f64::NAN);
            let hull = grid_hull(grid);
            let speed = params.objective.speed_mps;
            let drag = match &params.objective.displacements {
                None => resistance(&hull, draft, speed, params.hull.roughness_um).map(|r| r.total_n),
                Some(ds) => weighted_resistance(&hull, c.hull_weight_lb, ds, speed, params.hull.roughness_um),
            };
            match drag {
                Ok(r) => c.drag_n = Some(r),
                Err(e) if params.objective.kind == ObjectiveKind::Drag => {
                    c.error = Some(format!("drag: {e:#}"));
                    c.verdict = Some(v);
                    c.governing_case = Some(name);
                    return c;
                }
                Err(_) => {}
            }
        }
        if let Some(fp) = &params.flotation {
            let fl = match wall {
                Wall::Uniform(_) => {
                    crate::flotation::flotation(grid, sec, rho, fp.swamp_factor, fp.air_pocket_max_in)
                }
                Wall::Zoned(_) => crate::flotation::flotation_weight(
                    grid,
                    sec,
                    c.hull_weight_lb,
                    fp.swamp_factor,
                    fp.air_pocket_max_in,
                ),
            };
            match fl {
                Ok(f) => c.air_pocket_in = Some(f.pocket_len_in),
                Err(e) => {
                    c.error = Some(format!("{e:#}"));
                    c.verdict = Some(v);
                    c.governing_case = Some(name);
                    return c;
                }
            }
        }
        c.verdict = Some(v);
        c.governing_case = Some(name);
        // Every gate at its search limit (the shared list: see [`Gate`]).
        c.passes = Gate::ALL.iter().all(|g| g.miss(params, &c, true, params.hull.stations).is_none());
    } else {
        c.error = Some("no load cases".into());
    }
    c
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Auto seats planned for one wall density must never be used on a wall of another: the
    /// candidate fails with the reason instead of silently using the wrong seats.
    #[test]
    fn seats_planned_at_another_density_fail_the_candidate() {
        let mut p = Params::load(std::path::Path::new("examples/canoe.toml")).unwrap();
        p.objective.kind = ObjectiveKind::Weight;
        let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
        let fit = strength(&p).unwrap();
        let grid = Grid::build_topsides(&h, 12.0, p.hull.stations, p.hull.topsides()).0;
        let sec = sections(&grid, 0.375);
        let plan = plan_seats(&p, &fit, &grid, &sec, 0.375, 56.0).map_err(|e| format!("{e:#}"));
        assert!(plan.as_ref().unwrap().iter().all(|s| s.planned_rho_pcf == Some(56.0)));
        let ok = evaluate(&p, &fit, &grid, &sec, 0.375, 56.0, &plan, false);
        assert!(ok.error.is_none(), "{:?}", ok.error);
        let bad = evaluate(&p, &fit, &grid, &sec, 0.375, 58.0, &plan, false);
        assert!(!bad.passes);
        assert!(bad.error.as_deref().is_some_and(|e| e.contains("planned at 56 pcf, used on a 58 pcf wall")));
    }

    /// Every gate is applied by the search (with its one-sided margin) and by the re-check at
    /// 2n-1 (without): a value exactly at the verification limit passes the re-check and fails
    /// the search, except the overall beam (no drift measured, no margin), and a value just past
    /// it fails both. The match is exhaustive: a new gate must be added here.
    #[test]
    fn every_gate_is_rechecked_and_drifting_gates_have_a_margin() {
        let mut p = Params::load(std::path::Path::new("examples/canoe.toml")).unwrap();
        let sp = Params::load(std::path::Path::new("examples/shape_opt.toml")).unwrap();
        p.shape_search = sp.shape_search.clone();
        p.stability.gm_empty_min_in = Some(1.0);
        p.flotation = Some(crate::params::FlotationParams { air_pocket_max_in: 36.0, swamp_factor: 1.1 });
        let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
        let fit = strength(&p).unwrap();
        let grid = Grid::build_topsides(&h, 12.0, p.hull.stations, p.hull.topsides()).0;
        let sec = sections(&grid, 0.375);
        let plan = plan_seats(&p, &fit, &grid, &sec, 0.375, 58.0).map_err(|e| format!("{e:#}"));
        let c0 = evaluate(&p, &fit, &grid, &sec, 0.375, 58.0, &plan, false);
        let set = |c: &mut Candidate, g: Gate, x: f64| match g {
            Gate::Dc => c.verdict.as_mut().unwrap().dc = x,
            Gate::Freeboard => c.verdict.as_mut().unwrap().freeboard_in = x,
            Gate::LoadedGm => c.gm_in = Some(x),
            Gate::EmptyGm => c.gm_empty_in = Some(x),
            Gate::AirPocket => c.air_pocket_in = Some(x),
            Gate::Beam => c.beam_in = x,
        };
        for g in Gate::ALL {
            let (verify, at_least, search) = g.limits(&p).unwrap_or_else(|| panic!("{g:?} not gated"));
            if g != Gate::Beam {
                assert!(
                    if at_least { search > verify } else { search < verify },
                    "{g:?} has no search margin"
                );
            }
            let mut c = c0.clone();
            set(&mut c, g, verify);
            assert!(g.miss(&p, &c, false, 0).is_none(), "{g:?} at its limit fails the re-check");
            assert_eq!(g.miss(&p, &c, true, 0).is_some(), g != Gate::Beam, "{g:?} search margin");
            set(&mut c, g, if at_least { verify - 1e-6 } else { verify + 1e-6 });
            assert!(g.miss(&p, &c, false, 0).is_some() && g.miss(&p, &c, true, 0).is_some(), "{g:?}");
            assert!(gate_misses(&p, &c, false, 0).iter().any(|m| m.starts_with(g.label())), "{g:?}");
        }
    }
}
