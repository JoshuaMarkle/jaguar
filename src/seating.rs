//! Race-day seating on a finished canoe (`jaguar serve` Seating view, docs/SPEC.md "Seating").
//!
//! The wall, hull and strength come from a `jaguar-result/1` (its winner); nothing here changes
//! them. Given a crew (weights) and knee positions it answers, with the same checks the search
//! gates on ([`optimize::eval_envelope`], at 2n-1 stations like the verify):
//! * [`assess`]: every check's D/C, freeboard and GM at exactly these knees;
//! * the safe band of each paddler: how far their knee can move fore and aft, the others staying
//!   put, before a check fails (D/C > 1, or freeboard under `gates.freeboard_min_in`);
//! * [`best`]: the run's own seat picker (`seats::auto`) for this crew on this wall.
//!
//! Three states, not two ([`State`]): **safe** (the code checks: D/C <= 1 with the load and
//! strength factors), **reduced** (a code check is over but the canoe
//! still holds at the real, unfactored load: [`Assessment::safety_factor`] says how much is left),
//! **fails** (no safety factor left: the real load reaches the tested strength, water comes over
//! the gunwale, or the canoe cannot float level). The unfactored check is the same check with
//! `material.method = "asd"` and `factor_of_safety = 1` (the dynamic load factor kept: it is load,
//! not margin), whatever the design method.
//!
//! Freeboard: the judges check it at staging, with the crew at their marked seats, so the RFP
//! minimum `gates.freeboard_min_in` (not the design's `freeboard_margin_in`) is reported
//! ([`Assessment::freeboard_ok`], and [`Assessment::safe`] for crews proposed at their marked
//! seats: best seating, the roster) but does not shape the states or bands: a paddler sliding
//! during a race only fails the canoe when water comes over the gunwale. Stability (loaded GM) is reported, never a verdict: it hardly depends
//! on where the paddlers kneel (only on the crew's weight and how high they sit), so seating cannot
//! fix it; the design run gates it.
//!
//! No `seats.variance_in` here: the run applies it because it cannot know where paddlers will
//! really kneel; on race day the knees are known and the bands show how far each may drift.
//!
//! "Safe" is D/C <= 1.0 under `material.method` (asd: capacity / `factor_of_safety`; lrfd:
//! factored loads, phi), i.e. the design code check holds, without the search's `dc_search` /
//! `dc_verify` margins (those are reported so the page can mark near-limit values).

use crate::check::Verdict;
use crate::hull::{Grid, Hull};
use crate::mix::StrengthFit;
use crate::optimize;
use crate::params::{LoadCase, Params};
use crate::seats::CaseSeats;
use crate::section::{Sections, sections};
use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

/// Step of the safe-band sweep, in: band ends are found to this.
pub const BAND_STEP_IN: f64 = 1.0;
/// The sweep walks out in this many steps at a time, then halves its way back to the exact step
/// where the state changes (assumes no safe pocket narrower than this inside an unsafe stretch).
const BAND_COARSE: usize = 4;

/// The finished canoe, ready to check crews on.
pub struct Canoe {
    pub params: Params,
    pub fit: StrengthFit,
    pub grid: Grid,
    pub sec: Sections,
    pub thickness_in: f64,
    pub density_pcf: f64,
    pub depth_in: f64,
    pub hull_weight_lb: f64,
    pub mix: Option<String>,
    /// Outer mold line and the topsides the wall was built with (the race simulator's canoe).
    pub hull: Hull,
    pub topsides: crate::hull::Topsides,
    /// The winner's load cases as the run seated them (`params.load_cases` order).
    pub cases: Vec<RunCase>,
    /// Fails the run's gates: the result's closest design, not a winner (the page says so).
    pub fails: Option<String>,
}

/// One load case of the run, with the seats it was checked at.
#[derive(Debug, Clone, Serialize)]
pub struct RunCase {
    pub name: String,
    pub weights_lb: Vec<f64>,
    /// Knee x per paddler (`weights_lb` order), in from the bow.
    pub seats_in: Vec<f64>,
    /// The load case's seat pins (`positions`), if it had any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub positions: Option<Vec<usize>>,
}

/// Rebuild the winner (or, when nothing passed, the closest design) of a result.
pub fn canoe_from_result(text: &str) -> Result<Canoe> {
    let v: serde_json::Value = serde_json::from_str(text).context("parsing result JSON")?;
    ensure!(v["format"] == optimize::RESULT_FORMAT, "not a jaguar-result/1 file");
    let (w, fails) = if v["winner"]["candidate"].is_object() {
        (&v["winner"], None)
    } else if v["closest"]["design"]["candidate"].is_object() {
        let f: Vec<&str> = v["closest"]["fails"]
            .as_array()
            .map_or(vec![], |a| a.iter().filter_map(|x| x.as_str()).collect());
        (&v["closest"]["design"], Some(format!("closest design, fails: {}", f.join("; "))))
    } else {
        bail!("the result has no winner: run first");
    };
    let c = &w["candidate"];
    ensure!(c["zones"].is_null(), "seating does not support two-mix (zoned) walls yet");
    let mut params: Params = serde_json::from_value(v["params"].clone()).context("result params")?;
    if let Some(gr) = c["shape"]["gunwale_radius_in"].as_f64() {
        params.hull.gunwale_radius_in = Some(gr);
    }
    let strength: StrengthFit = serde_json::from_value(v["strength"].clone()).context("result strength")?;
    let hull: Hull = serde_json::from_value(v["winner_hull"].clone()).context("result winner_hull")?;
    let num = |f: &str| c[f].as_f64().with_context(|| format!("candidate has no {f}"));
    let (t, rho, depth, flare) =
        (num("thickness_in")?, num("density_pcf")?, num("depth_in")?, num("flare_deg")?);
    let mix = c["mix"].as_str().map(String::from);
    let fit = optimize::density_axis(&params, &strength)?
        .into_iter()
        .find(|a| a.0 == rho && a.2 == mix)
        .map(|a| a.1)
        .context("the winner's mix is not on the density axis")?;
    let topsides = crate::hull::Topsides { flare_deg: flare, ..params.hull.topsides() };
    let grid = Grid::build_topsides(&hull, depth, 2 * params.hull.stations - 1, topsides).0;
    let sec = sections(&grid, t);
    let cases = w["cases"]
        .as_array()
        .context("winner has no cases")?
        .iter()
        .map(|k| {
            let name = k["name"].as_str().context("case name")?.to_string();
            let positions =
                params.load_cases.iter().find(|lc| lc.name == name).and_then(|lc| lc.positions.clone());
            Ok(RunCase {
                positions,
                name,
                weights_lb: serde_json::from_value(k["weights_lb"].clone()).context("case weights")?,
                seats_in: serde_json::from_value(k["seats_in"].clone()).context("case seats")?,
            })
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Canoe {
        hull,
        topsides,
        hull_weight_lb: num("hull_weight_lb")?,
        params,
        fit,
        grid,
        sec,
        thickness_in: t,
        density_pcf: rho,
        depth_in: depth,
        mix,
        cases,
        fails,
    })
}

/// Seat rules the page may change for this session (defaults: the result's `[seats]`).
#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rules {
    pub spacing_min_in: f64,
    pub bow_clear_in: f64,
    pub stern_clear_in: f64,
}

/// A crew at given knees (`POST /api/seating`).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub weights_lb: Vec<f64>,
    /// Knee x per paddler, in from the bow (`weights_lb` order). `best` ignores it.
    #[serde(default)]
    pub seats_in: Vec<f64>,
    /// Absent = the result's `[seats]`.
    #[serde(default)]
    pub rules: Option<Rules>,
    /// Pinned seat per paddler (`weights_lb` order), as `load_case.positions`: 1 = bow seat ...
    /// k = stern seat, 0 = free. `best` keeps pinned paddlers there; `assess` warns when the knees
    /// put a pinned paddler in another seat. Absent = all free.
    #[serde(default)]
    pub positions: Option<Vec<usize>>,
    /// Compute each paddler's safe band (slower). Default true.
    #[serde(default = "yes")]
    pub bands: bool,
    /// Shear, moment and D/C along the hull ([`Plots`]). Default false.
    #[serde(default)]
    pub plots: bool,
}

fn yes() -> bool {
    true
}

/// One check at the seats.
#[derive(Debug, Clone, Serialize)]
pub struct CheckOut {
    /// "bending_tension" | "bending_compression" | "shear" | "punching" | "biaxial".
    pub name: &'static str,
    /// D/C at exactly these seats (code: factored load / reduced strength).
    pub dc: f64,
    /// D/C at the real, unfactored load against the tested strength: 1 = no safety factor left.
    /// For the linear checks 1 / `dc_true` is the factor the load could still grow by; for biaxial
    /// (Kupfer interaction) it is the interaction ratio, close to but not exactly a load multiple.
    pub dc_true: f64,
    /// Station of the governing value at these seats, in from the bow (None: local check).
    pub x_in: Option<f64>,
}

/// Safe, reduced margin, or failing (see the module doc).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Safe,
    Reduced,
    Fails,
}

/// Where one paddler's knee may go with the others where they are.
#[derive(Debug, Clone, Serialize)]
pub struct Band {
    /// Contiguous safe range around the seat, in from the bow, to `BAND_STEP_IN`. None: the seat
    /// itself is not safe.
    pub range_in: Option<[f64; 2]>,
    /// What ends the safe range at each end (the code checks over; "hull end" at a tip).
    pub fore_limit: Option<String>,
    pub aft_limit: Option<String>,
    /// Contiguous range around the seat where the canoe does not fail (safe or reduced margin):
    /// beyond it no safety factor is left. None: the seat itself fails.
    pub hold_in: Option<[f64; 2]>,
    /// What ends that range at each end.
    pub fore_fail: Option<String>,
    pub aft_fail: Option<String>,
}

/// One position judged both ways.
#[derive(Clone)]
struct Judged {
    state: State,
    /// Why not safe (code checks, freeboard minimum) / why it fails (empty when it holds).
    reasons: Vec<String>,
    fail: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Assessment {
    pub checks: Vec<CheckOut>,
    /// Highest D/C at these seats.
    pub dc: f64,
    pub governing_check: &'static str,
    /// Gated freeboard at these seats (`gates.freeboard_seat_shift_in` applied), in.
    pub freeboard_in: f64,
    /// `gates.freeboard_min_in`: the RFP minimum, without the design margin.
    pub freeboard_required_in: f64,
    /// Loaded GM_T at these seats, in. Information only (never makes a seat unsafe).
    pub gm_in: f64,
    /// The design run's GM gate, for reference.
    pub gm_min_in: f64,
    pub draft_in: f64,
    pub trim_in: f64,
    /// Local waterline above the keel per `grid.x` station (nominal seats, service float), in.
    pub waterline_in: Vec<f64>,
    /// Freeboard at these seats >= `gates.freeboard_min_in` (the staging rule).
    pub freeboard_ok: bool,
    /// Every code D/C <= 1 and `freeboard_ok`: fit to stage at these seats (GM is not judged).
    pub safe: bool,
    /// Safe, reduced margin or fails.
    pub state: State,
    /// Smallest safety factor left over the strength checks: 1 / the highest `dc_true`
    /// (how many times the real load could grow before the concrete reaches its tested strength).
    pub safety_factor: f64,
    /// Code checks over 1 at these seats (empty when the state is safe).
    pub reasons: Vec<String>,
    /// Why it fails (empty unless `state` is fails).
    pub fail_reasons: Vec<String>,
    /// Seat rule warnings (clearance, spacing). Not structural: the checks above decide safety.
    pub warnings: Vec<String>,
    /// Per paddler (`weights_lb` order), when asked.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bands: Option<Vec<Band>>,
    /// Along the hull, when asked.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub plots: Option<Plots>,
}

/// Shear, moment and D/C along the hull at these knees (`optimize::case_diagrams`, the same
/// per-station values the checks take their maxima from): the code's (factored) loads and the
/// real (unfactored) ones.
#[derive(Debug, Clone, Serialize)]
pub struct Plots {
    pub x_in: Vec<f64>,
    /// Factored as the governing code combination (asd: service x dynamic factor), lb, lb*in.
    pub shear_lb: Vec<f64>,
    pub moment_lbin: Vec<f64>,
    /// Real load (no load factors; dynamic factor kept), lb, lb*in.
    pub shear_real_lb: Vec<f64>,
    pub moment_real_lbin: Vec<f64>,
    /// Highest of bending tension, compression, shear and biaxial at each station: code D/C, and
    /// the real load against the tested strength (1 = no safety factor left).
    pub dc: Vec<f64>,
    pub dc_real: Vec<f64>,
}

/// Highest of the four per-station D/C series.
fn dc_max(d: &optimize::CaseDiagrams) -> Vec<f64> {
    (0..d.diagrams.x_in.len())
        .map(|i| {
            [&d.dc_bending_tension, &d.dc_bending_compression, &d.dc_shear, &d.dc_biaxial]
                .iter()
                .filter_map(|v| v.get(i).copied())
                .fold(0.0_f64, f64::max)
        })
        .collect()
}

/// An equilibrium error in words a paddler understands.
fn plain(e: &str, depth: f64) -> String {
    if e.contains("trim balance not bracketed") {
        format!("tilts over {:.0} in end to end (past swamping)", 2.0 * depth)
    } else if e.contains("hull sinks") {
        "sinks".into()
    } else {
        e.to_string()
    }
}

fn case_of(weights: &[f64]) -> LoadCase {
    LoadCase { name: "seating".into(), weights_lb: weights.to_vec(), seats_in: None, positions: None }
}

/// Pins as `load_case.positions` (params.rs checks the same): one per paddler, seats 1..=k, no
/// seat twice.
fn validate_pins(k: usize, pins: &[usize]) -> Result<()> {
    ensure!(pins.len() == k, "{} seat pins for {k} paddlers", pins.len());
    for (i, &s) in pins.iter().enumerate() {
        ensure!(s <= k, "paddler {} pinned to seat {s}: seats are 1 (bow) to {k} (stern)", i + 1);
        ensure!(s == 0 || !pins[..i].contains(&s), "two paddlers pinned to seat {s}");
    }
    Ok(())
}

fn validate(c: &Canoe, weights: &[f64], seats_in: Option<&[f64]>) -> Result<()> {
    ensure!(!weights.is_empty(), "add at least one paddler");
    ensure!(weights.len() <= 8, "at most 8 paddlers");
    for &w in weights {
        ensure!(w.is_finite() && w > 0.0 && w < 1000.0, "paddler weight {w} lb: give 1 to 999 lb");
    }
    if let Some(s) = seats_in {
        ensure!(s.len() == weights.len(), "{} seats for {} paddlers", s.len(), weights.len());
        let l = c.length_in();
        for &x in s {
            ensure!(x.is_finite() && (0.0..=l).contains(&x), "knee at {x} in is off the {l:.0} in hull");
        }
    }
    Ok(())
}

impl Canoe {
    pub fn length_in(&self) -> f64 {
        self.grid.x[self.grid.nx() - 1]
    }

    fn rules(&self, r: Option<Rules>) -> Rules {
        r.unwrap_or(Rules {
            spacing_min_in: self.params.seats.spacing_min_in,
            bow_clear_in: self.params.seats.bow_clear_in,
            stern_clear_in: self.params.seats.stern_clear_in,
        })
    }

    /// Params with these rules and no seat variance (the knees are known).
    fn params_with(&self, r: Rules) -> Params {
        let mut p = self.params.clone();
        p.seats.spacing_min_in = r.spacing_min_in;
        p.seats.bow_clear_in = r.bow_clear_in;
        p.seats.stern_clear_in = r.stern_clear_in;
        p.seats.variance_in = 0.0;
        p.seats.auto = true;
        // A paddler may slide until the canoe swamps: the wide trim bracket (screen only).
        p.wide_trim = true;
        // The seat picker scores the same way the screen judges: D/C against 1, freeboard against
        // the RFP minimum, GM not at all (seating cannot fix it).
        p.gates.dc_search = 1.0;
        p.gates.freeboard_margin_in = 0.0;
        p.gates.freeboard_search_margin_in = 0.0;
        p.stability.gm_min_in = -1e9;
        p.gates.gm_search_margin_in = 0.0;
        p
    }

    /// The same params with no safety factor: real loads (dynamic factor kept), tested strength.
    fn unfactored(p: &Params) -> Params {
        let mut u = p.clone();
        u.material.method = crate::params::DesignMethod::Asd;
        u.material.factor_of_safety = 1.0;
        u
    }

    /// Why the true check fails (empty = holds): a strength check at or over the tested strength,
    /// or the water over the gunwale somewhere.
    fn fail_reasons(v: &Verdict, hull_min_fb: f64) -> Vec<String> {
        let mut out: Vec<String> = v
            .checks
            .iter()
            .filter(|k| k.dc.is_nan() || k.dc >= 1.0)
            .map(|k| format!("{} at the tested strength (no safety factor left)", k.name.replace('_', " ")))
            .collect();
        if hull_min_fb.is_nan() || hull_min_fb <= 0.0 {
            out.push(format!("water over the gunwale (freeboard {hull_min_fb:.2} in)"));
        }
        out
    }

    /// Paddlers at `seats`, judged with the code factors (`p`) and without (`u`).
    fn judge(&self, p: &Params, u: &Params, case: &LoadCase, seats: &[f64]) -> Judged {
        // Code checks first: when they hold and the gunwale is dry, the real load (smaller loads,
        // full strength) holds too, so the second check is skipped.
        let both = self.envelope(p, case, seats).and_then(|a| {
            let dry = a.freeboard.gate.hull_min_in > 0.0;
            if dry && self.reasons(&a.verdict).is_empty() {
                return Ok((a, None));
            }
            Ok((a, Some(self.envelope(u, case, seats)?)))
        });
        match both {
            Ok((a, t)) => {
                let reasons = self.reasons(&a.verdict);
                let fail = t.map_or(vec![], |t| Self::fail_reasons(&t.verdict, a.freeboard.gate.hull_min_in));
                let state = if !fail.is_empty() {
                    State::Fails
                } else if reasons.is_empty() {
                    State::Safe
                } else {
                    State::Reduced
                };
                Judged { state, reasons, fail }
            }
            Err(e) => {
                let why = plain(&format!("{e:#}"), self.grid.depth);
                Judged { state: State::Fails, reasons: vec![why.clone()], fail: vec![why] }
            }
        }
    }

    fn envelope(&self, p: &Params, case: &LoadCase, seats: &[f64]) -> Result<optimize::CaseEnvelope> {
        optimize::eval_envelope(p, &self.fit, &self.grid, &self.sec, self.density_pcf, case, seats, false)
    }

    /// The code checks over 1 (empty = the code margin holds). Freeboard is not one of them.
    fn reasons(&self, v: &Verdict) -> Vec<String> {
        let mut out = vec![];
        for k in &v.checks {
            if k.dc.is_nan() || k.dc > 1.0 {
                out.push(format!("{} D/C {:.2}", k.name.replace('_', " "), k.dc));
            }
        }
        out
    }

    /// Paddler `i`'s ranges: walk out from the seat each way ([`BAND_COARSE`] steps at a time,
    /// then halving back to [`BAND_STEP_IN`]), noting where it stops being safe and where it
    /// starts failing.
    fn band(&self, p: &Params, u: &Params, case: &LoadCase, seats: &[f64], i: usize) -> Band {
        let l = self.length_in();
        let at = |x: f64| {
            let mut s = seats.to_vec();
            s[i] = x;
            self.judge(p, u, case, &s)
        };
        let here = at(seats[i]).state;
        if here == State::Fails {
            return Band {
                range_in: None,
                fore_limit: None,
                aft_limit: None,
                hold_in: None,
                fore_fail: None,
                aft_fail: None,
            };
        }
        // (end of the safe range and why, end of the holding range and why)
        let walk = |dir: f64| -> (Option<(f64, String)>, (f64, String)) {
            // Positions seat + dir k BAND_STEP_IN, k = 0..=kmax inside the hull; past kmax the tip.
            let pos = |k: usize| seats[i] + dir * k as f64 * BAND_STEP_IN;
            let room = if dir < 0.0 { seats[i] } else { l - seats[i] };
            let kmax = (room / BAND_STEP_IN + 1e-9).floor() as usize;
            let tip = if dir < 0.0 { 0.0 } else { l };
            // First k in (a, b] where `bad` holds, given it does not at a and does at b (`jb`).
            let first_bad = |mut a: usize, mut b: usize, mut jb: Judged, bad: &dyn Fn(&Judged) -> bool| {
                while b - a > 1 {
                    let m = (a + b) / 2;
                    let jm = at(pos(m));
                    if bad(&jm) {
                        (b, jb) = (m, jm);
                    } else {
                        a = m;
                    }
                }
                (b, jb)
            };
            let (mut k0, mut open, mut safe) = (0, here == State::Safe, None);
            loop {
                if k0 == kmax {
                    if open {
                        safe = Some((tip, "hull end".into()));
                    }
                    return (safe, (tip, "hull end".into()));
                }
                let k1 = (k0 + BAND_COARSE).min(kmax);
                let j1 = at(pos(k1));
                if open && j1.state != State::Safe {
                    let (b, jb) = first_bad(k0, k1, j1.clone(), &|j| j.state != State::Safe);
                    // A seat that fails outright (water over the gunwale) may have no code check
                    // over: then the failure is what ends the safe range.
                    let why = if jb.reasons.is_empty() { &jb.fail } else { &jb.reasons };
                    safe = Some((pos(b - 1), why.join(", ")));
                    open = false;
                }
                if j1.state == State::Fails {
                    let (b, jb) = first_bad(k0, k1, j1, &|j| j.state == State::Fails);
                    return (safe, (pos(b - 1), jb.fail.join(", ")));
                }
                k0 = k1;
            }
        };
        let ((s_lo, (lo, fore)), (s_hi, (hi, aft))) = rayon::join(|| walk(-1.0), || walk(1.0));
        let range_in = match (&s_lo, &s_hi) {
            (Some((a, _)), Some((b, _))) => Some([*a, *b]),
            _ => None,
        };
        Band {
            range_in,
            fore_limit: s_lo.map(|v| v.1),
            aft_limit: s_hi.map(|v| v.1),
            hold_in: Some([lo, hi]),
            fore_fail: Some(fore),
            aft_fail: Some(aft),
        }
    }
}

/// Check a crew at given knees on the canoe.
pub fn assess(c: &Canoe, req: &Request) -> Result<Assessment> {
    validate(c, &req.weights_lb, Some(&req.seats_in))?;
    let rules = c.rules(req.rules);
    let case = case_of(&req.weights_lb);
    let p0 = c.params_with(rules);
    let u0 = Canoe::unfactored(&p0);
    // A canoe that cannot float level here has no numbers to show: an error, in plain words.
    let plain_err = |e: anyhow::Error| anyhow::anyhow!("fails: {}", plain(&format!("{e:#}"), c.grid.depth));
    let at = c.envelope(&p0, &case, &req.seats_in).map_err(plain_err)?;
    let tr = c.envelope(&u0, &case, &req.seats_in).map_err(plain_err)?;
    let checks: Vec<CheckOut> = at
        .verdict
        .checks
        .iter()
        .map(|k| {
            let dc_true = tr.verdict.checks.iter().find(|t| t.name == k.name).map_or(f64::NAN, |t| t.dc);
            CheckOut { name: k.name, dc: k.dc, dc_true, x_in: k.x_in }
        })
        .collect();
    let reasons = c.reasons(&at.verdict);
    let fail_reasons = Canoe::fail_reasons(&tr.verdict, at.freeboard.gate.hull_min_in);
    let worst_true = checks
        .iter()
        .map(|k| k.dc_true)
        .fold(0.0_f64, |m, d| if d.is_nan() { f64::INFINITY } else { m.max(d) });
    let state = if !fail_reasons.is_empty() {
        State::Fails
    } else if reasons.is_empty() {
        State::Safe
    } else {
        State::Reduced
    };
    let l = c.length_in();
    let mut warnings = vec![];
    let mut order: Vec<usize> = (0..req.seats_in.len()).collect();
    order.sort_by(|&a, &b| req.seats_in[a].total_cmp(&req.seats_in[b]));
    if let Some(pins) = &req.positions {
        validate_pins(req.weights_lb.len(), pins)?;
        let k = pins.len();
        for (i, &s) in pins.iter().enumerate() {
            let at = order.iter().position(|&j| j == i).map_or(0, |j| j + 1);
            if s > 0 && at != s {
                let name = |n: usize| match n {
                    1 => " (bow)".to_string(),
                    n if n == k => " (stern)".to_string(),
                    _ => String::new(),
                };
                warnings.push(format!(
                    "paddler {} is pinned to seat {s}{} but kneels in seat {at}{}",
                    i + 1,
                    name(s),
                    name(at)
                ));
            }
        }
    }
    for (j, &i) in order.iter().enumerate() {
        let x = req.seats_in[i];
        if x < rules.bow_clear_in {
            warnings.push(format!(
                "paddler {} is {x:.0} in from the bow, inside the {:.0} in clearance",
                i + 1,
                rules.bow_clear_in
            ));
        }
        if l - x < rules.stern_clear_in {
            warnings.push(format!(
                "paddler {} is {:.0} in from the stern, inside the {:.0} in clearance",
                i + 1,
                l - x,
                rules.stern_clear_in
            ));
        }
        if let Some(&n) = order.get(j + 1) {
            let gap = req.seats_in[n] - x;
            if gap < rules.spacing_min_in - 1e-9 {
                warnings.push(format!(
                    "paddlers {} and {} are {gap:.0} in apart, under the {:.0} in spacing",
                    i + 1,
                    n + 1,
                    rules.spacing_min_in
                ));
            }
        }
    }
    let bands = req.bands.then(|| {
        (0..req.weights_lb.len()).into_par_iter().map(|i| c.band(&p0, &u0, &case, &req.seats_in, i)).collect()
    });
    let s = &at.nominal.service;
    let waterline_in = c
        .grid
        .x
        .iter()
        .map(|&x| (s.draft_in + s.trim_in * (x - l / 2.0) / l).clamp(0.0, c.grid.depth))
        .collect();
    let plots = if req.plots {
        let seats = CaseSeats {
            seats_in: req.seats_in.clone(),
            crew_order: order.clone(),
            source: crate::seats::SeatSource::Given,
            planned_rho_pcf: None,
        };
        let diag = |p: &Params| {
            let mut p = p.clone();
            p.load_cases = vec![case.clone()];
            optimize::case_diagrams(
                &p,
                &c.fit,
                &c.grid,
                &c.sec,
                c.thickness_in,
                c.density_pcf,
                &case.name,
                &seats,
            )
            .map_err(plain_err)
        };
        let (d, r) = (diag(&p0)?, diag(&u0)?);
        Some(Plots {
            x_in: d.diagrams.x_in.clone(),
            dc: dc_max(&d),
            dc_real: dc_max(&r),
            shear_lb: d.diagrams.shear_lb,
            moment_lbin: d.diagrams.moment_lbin,
            shear_real_lb: r.diagrams.shear_lb,
            moment_real_lbin: r.diagrams.moment_lbin,
        })
    } else {
        None
    };
    Ok(Assessment {
        dc: at.verdict.dc,
        governing_check: at.verdict.governing_check,
        checks,
        freeboard_in: at.verdict.freeboard_in,
        freeboard_required_in: c.params.gates.freeboard_min_in,
        gm_in: at.gm_in,
        gm_min_in: c.params.stability.gm_min_in,
        draft_in: s.draft_in,
        trim_in: s.trim_in,
        waterline_in,
        freeboard_ok: at.verdict.freeboard_in >= c.params.gates.freeboard_min_in,
        safe: reasons.is_empty() && at.verdict.freeboard_in >= c.params.gates.freeboard_min_in,
        state,
        safety_factor: 1.0 / worst_true,
        reasons,
        fail_reasons,
        warnings,
        bands,
        plots,
    })
}

/// The run's seat picker for this crew on this canoe (clearance and spacing from `rules`,
/// scored at the knees themselves, no variance).
/// Pinned paddlers (`positions`) stay in their seat; at most 4 may be free (every order is tried).
pub fn best(
    c: &Canoe,
    weights: &[f64],
    positions: Option<&[usize]>,
    rules: Option<Rules>,
) -> Result<CaseSeats> {
    validate(c, weights, None)?;
    if let Some(p) = positions {
        validate_pins(weights.len(), p)?;
    }
    let free = positions.map_or(weights.len(), |p| p.iter().filter(|&&s| s == 0).count());
    ensure!(free <= 4, "best seating tries every crew order, so at most 4 free paddlers: pin some to a seat");
    let mut p1 = c.params_with(c.rules(rules));
    let mut case = case_of(weights);
    case.positions = positions.filter(|p| p.iter().any(|&s| s > 0)).map(<[usize]>::to_vec);
    p1.load_cases = vec![case];
    let mut s = optimize::plan_seats(&p1, &c.fit, &c.grid, &c.sec, c.thickness_in, c.density_pcf)?;
    Ok(s.remove(0))
}

/// What the page draws: hull side profile and the canoe's facts.
#[derive(Debug, Clone, Serialize)]
pub struct Summary {
    pub length_in: f64,
    pub depth_in: f64,
    pub thickness_in: f64,
    pub density_pcf: f64,
    pub mix: Option<String>,
    pub hull_weight_lb: f64,
    pub method: String,
    /// Stations, in from the bow, and the keel line there above the lowest point of the hull, in
    /// (where the section's half-breadth, extrapolated from the two lowest waterlines with breadth,
    /// reaches zero; the mold depth where a station has no breadth). Same scale as x: a true profile.
    pub x_in: Vec<f64>,
    pub keel_in: Vec<f64>,
    pub rules: Rules,
    pub dc_verify: f64,
    /// How the strength checks build in safety (shown to the user next to every D/C).
    pub safety: Safety,
    pub freeboard_required_in: f64,
    pub gm_min_in: f64,
    pub cases: Vec<RunCase>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fails: Option<String>,
}

/// The factors inside every strength D/C (`material.method`): D/C = factored demand / reduced
/// capacity, so D/C = 1 still leaves `crew_factor / strength_factor` on the paddlers' load.
#[derive(Debug, Clone, Serialize)]
pub struct Safety {
    /// "asd" | "lrfd".
    pub method: String,
    /// Multiplier on each paddler's weight in the strength checks (dynamic factor x live factor).
    pub crew_factor: f64,
    /// Multiplier on the hull's own weight (and the buoyancy that floats it).
    pub hull_factor: f64,
    /// LRFD only: the dead-load-only combination (no crew), e.g. 1.4.
    pub hull_only_factor: Option<f64>,
    /// Multiplier on concrete strength: bending, shear, biaxial / knee punching (asd: 1 / FS).
    pub strength_factor: f64,
    pub punching_strength_factor: f64,
    pub dynamic_load_factor: f64,
    /// ASD only.
    pub factor_of_safety: Option<f64>,
}

pub fn safety(p: &Params) -> Safety {
    use crate::params::DesignMethod;
    let m = &p.material;
    let (phi, phi_p) = crate::check::capacity_factors(m, &p.punching);
    let lrfd = m.method == DesignMethod::Lrfd;
    Safety {
        method: if lrfd { "lrfd" } else { "asd" }.into(),
        crew_factor: m.dynamic_load_factor * if lrfd { m.lrfd_live } else { 1.0 },
        hull_factor: if lrfd { m.lrfd_dead } else { 1.0 },
        hull_only_factor: lrfd.then_some(m.lrfd_dead_only),
        strength_factor: phi,
        punching_strength_factor: phi_p,
        dynamic_load_factor: m.dynamic_load_factor,
        factor_of_safety: (!lrfd).then_some(m.factor_of_safety),
    }
}

/// Keel height of one section: the first waterline with breadth, lowered to where the half-breadth
/// extrapolated linearly from it and the next waterline reaches zero, but not below the waterline
/// under it (which has no breadth). Without this the profile steps by the waterline spacing.
fn keel_at(y: &[f64], z: &[f64], depth: f64) -> f64 {
    let Some(k) = y.iter().position(|&v| v > 1e-9) else { return depth };
    if k == 0 {
        return z[0];
    }
    let below = z[k - 1];
    match (y.get(k + 1), z.get(k + 1)) {
        (Some(&y1), Some(&z1)) if y1 > y[k] => (z[k] - y[k] * (z1 - z[k]) / (y1 - y[k])).clamp(below, z[k]),
        _ => z[k],
    }
}

pub fn summary(c: &Canoe) -> Summary {
    let g = &c.grid;
    let keel = (0..g.nx()).map(|i| keel_at(g.row(i), &g.z, g.depth)).collect();
    Summary {
        length_in: c.length_in(),
        depth_in: c.depth_in,
        thickness_in: c.thickness_in,
        density_pcf: c.density_pcf,
        mix: c.mix.clone(),
        hull_weight_lb: c.hull_weight_lb,
        method: format!("{:?}", c.params.material.method).to_lowercase(),
        x_in: g.x.clone(),
        keel_in: keel,
        rules: c.rules(None),
        dc_verify: c.params.gates.dc_verify,
        safety: safety(&c.params),
        freeboard_required_in: c.params.gates.freeboard_min_in,
        gm_min_in: c.params.stability.gm_min_in,
        cases: c.cases.clone(),
        fails: c.fails.clone(),
    }
}
