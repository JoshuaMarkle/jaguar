//! Terminal output and result.json.
//!
//! * `print_summary`: one line per load case header, a compact table of the
//!   passing frontier (lightest passing design per thickness), then the
//!   winner block (one seats/D-C line per load case) with VERIFIED / NOT VERIFIED, the mix
//!   provenance line, any grid-edge warning, then the search-space table.
//!   Plain text, no colours. Kept under 40 lines.
//! * `write_json`: `jaguar-result/1`, pretty-printed, deterministic.

use crate::optimize::{Candidate, RunResult};
use anyhow::Result;
use std::collections::BTreeMap;
use std::path::Path;

/// Hard cap on summary length.
pub const MAX_LINES: usize = 40;
/// Max load case lines and frontier rows before eliding.
const MAX_CASES: usize = 6;
const MAX_FRONTIER: usize = 14;

pub fn print_summary(result: &RunResult) {
    for line in summary_lines(result) {
        println!("{line}");
    }
}

pub fn write_json(result: &RunResult, path: &Path) -> Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(result)?)?;
    Ok(())
}

fn fmt_opt(v: Option<f64>, prec: usize) -> String {
    v.map_or_else(|| "-".to_string(), |x| format!("{x:.prec$}"))
}

/// One-line description of the strength fit.
pub fn mix_fit_line(f: &crate::mix::StrengthFit) -> String {
    format!(
        "strength: final split = {:.2}*rho {:+.1} psi from {} lab tests ({:.1}-{:.1} pcf), rms {:.1} psi",
        f.split_slope_psi_per_pcf,
        f.split_intercept_psi,
        f.points.len(),
        f.density_min_pcf,
        f.density_max_pcf,
        f.rms_residual_psi
    ) + &crate::mix::Property::ALL
        .iter()
        .filter(|&&p| f.measured(p).is_some())
        .map(|p| format!("; {} measured", p.label()))
        .collect::<String>()
}

/// NOTE for a winner density at the edge of what the lab data supports:
/// which end (from the density axis's `at_bound`) and how far past the tested
/// densities at that end it already extrapolates.
fn mix_edge_note(r: &RunResult, w: &crate::optimize::Winner) -> String {
    let rho = w.candidate.density_pcf;
    let (lo, hi) = r.strength.tested_range();
    if w.search_space[1].at_bound == Some(crate::optimize::Bound::Max) {
        format!(
            "NOTE density {rho:.0} pcf is the heaviest the lab data supports (+{:.0} pcf extrapolation); test heavier, stronger mixes to go higher",
            (rho - hi).max(0.0)
        )
    } else {
        format!(
            "NOTE density {rho:.0} pcf is the lightest the lab data supports (+{:.0} pcf extrapolation); test lighter mixes to go lower",
            (lo - rho).max(0.0)
        )
    }
}

/// Strength-model header: the fit line, or (tested_mixes) up to 3 lines of mixes.
pub fn strength_lines(r: &RunResult) -> Vec<String> {
    if r.params.material.strength_model != crate::params::StrengthModel::TestedMixes {
        return vec![format!("model fit  {}", mix_fit_line(&r.strength))];
    }
    let m = r.strength.tested_mixes();
    let mut out = vec![format!("strength model tested_mixes: {} mixes, design = mean final split", m.len())];
    let items: Vec<String> = m
        .iter()
        .map(|x| {
            format!(
                "{} n{} {:.1}pcf {:.0}/{:.0}psi",
                x.name, x.n, x.density_pcf, x.split_mean_psi, x.split_min_psi
            )
        })
        .collect();
    for chunk in items.chunks(4).take(2) {
        out.push(format!("  mix (name n rho mean/min) {}", chunk.join("  ")));
    }
    out
}

fn feet(inches: f64) -> String {
    let total = inches.round() as i64;
    format!("{} ft {} in", total / 12, total % 12)
}

fn hull_line(c: &Candidate) -> String {
    let mut s =
        format!("        hull  L {:.1} in ({})  beam {:.1} in", c.length_in, feet(c.length_in), c.beam_in);
    if let Some(sh) = &c.shape {
        s.push_str(&format!("  draft {:.1} in", sh.draft_in));
    }
    s
}

/// One winner line per load case: seats bow to stern (knee x in from bow @
/// paddler lb), their source, the +/- variance checked, and the D/C.
fn case_line(k: &crate::optimize::CaseDiagrams) -> String {
    let order: Vec<usize> = if k.crew_order.len() == k.seats_in.len() {
        k.crew_order.clone()
    } else {
        (0..k.seats_in.len()).collect()
    };
    let seats: Vec<String> = order
        .iter()
        .map(|&j| match k.weights_lb.get(j) {
            Some(w) => format!("{:.0}@{w:.0}", k.seats_in[j]),
            None => format!("{:.0}", k.seats_in[j]),
        })
        .collect();
    use crate::seats::SeatSource;
    let src = match k.seat_source {
        Some(SeatSource::Given) => " given",
        Some(SeatSource::Even) => " even",
        Some(SeatSource::Auto) => " auto",
        None => "",
    };
    let var = if k.seat_variance_in > 0.0 { format!(" +/-{}", k.seat_variance_in) } else { String::new() };
    let state = match (&k.error, &k.verdict) {
        (Some(e), _) => format!("error: {}", e.chars().take(40).collect::<String>()),
        (None, Some(v)) => format!("D/C {:.3}  fb {:.2} in", v.dc, v.freeboard_in),
        (None, None) => "-".to_string(),
    };
    format!("        case {:<12} seats [{}] in{src}{var}  {state}", k.name, seats.join(", "))
}

/// Richardson line: the extrapolated D/C the verify gate used, or why not.
fn convergence_line(w: &crate::optimize::Winner) -> String {
    let c = &w.convergence;
    let st = format!("{}/{}/{} stations", c.stations[0], c.stations[1], c.stations[2]);
    match (w.dc_extrapolated, &c.note) {
        (Some(x), _) => {
            let how = w.dc_order.map_or("converged".to_string(), |p| format!("order {p:.1}"));
            let gov = c.governing.as_deref().unwrap_or("-");
            format!("        D/C extrapolated {x:.4} ({how}, {gov}; {st})")
        }
        (None, Some(n)) => format!("        no extrapolation ({st}): {n}"),
        (None, None) => format!("        no extrapolation ({st})"),
    }
}

/// Number with up to 4 decimals, trailing zeros dropped (0.0625, 58, 55.634).
fn num(x: f64) -> String {
    let s = format!("{x:.4}");
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s == "-0" { "0".into() } else { s.to_string() }
}

/// Winner density's lab provenance on one line, e.g.
/// `mix 58 pcf predicted (fit); nearest tested: M2 55.6 (-2.4), final target 60.0 (+2.0); reach 2.0 pcf`.
fn provenance_line(p: &crate::mix::MixProvenance) -> String {
    let status = match p.status {
        crate::mix::Provenance::Tested => "tested",
        crate::mix::Provenance::Predicted => "predicted (fit)",
    };
    let near: Vec<String> = p
        .nearest
        .iter()
        .map(|n| format!("{} {:.1} ({:+.1})", n.name, n.density_pcf, n.distance_pcf))
        .collect();
    format!(
        "        mix {} pcf {status}; nearest tested: {}; reach {:.1} pcf",
        num(p.density_pcf),
        near.join(", "),
        p.reach_pcf
    )
}

/// Search-space table: one row per wall variable and per shape factor on a
/// bound; shape factors inside their bounds are counted on one line.
fn search_space_lines(axes: &[crate::optimize::SearchAxis]) -> Vec<String> {
    use crate::optimize::Bound;
    let mut out = vec![format!(
        "search space {:<8} {:>8} {:>8} {:>7} {:>4} {:>8}  bound",
        "", "min", "max", "step", "n", "winner"
    )];
    let mut inside = 0;
    for (i, a) in axes.iter().enumerate() {
        if i >= 3 && a.at_bound.is_none() {
            inside += 1;
            continue;
        }
        let bound = match (a.at_bound, a.limit) {
            (None, _) => String::new(),
            (Some(b), l) => {
                let b = if b == Bound::Min { "min" } else { "max" };
                l.map_or(format!("{b} (widen)"), |l| format!("{b} ({l})"))
            }
        };
        out.push(
            format!(
                "  {:<19} {:>8} {:>8} {:>7} {:>4} {:>8}  {bound}",
                a.name,
                num(a.min),
                num(a.max),
                a.step.map_or("-".into(), num),
                a.count.map_or("-".into(), |n| n.to_string()),
                num(a.winner),
            )
            .trim_end()
            .to_string(),
        );
    }
    if inside > 0 {
        out.push(format!("  + {inside} shape factors inside their bounds"));
    }
    out
}

/// Pure summary text, one entry per output line, never more than `MAX_LINES`.
pub fn summary_lines(r: &RunResult) -> Vec<String> {
    let p = &r.params;
    let mut out = Vec::new();
    out.push(format!(
        "JAGUAR  hull {}  ({} stations)",
        p.hull.file.as_ref().map_or("(shape search)".to_string(), |f| f.display().to_string()),
        p.hull.stations
    ));
    out.extend(strength_lines(r));
    for lc in p.load_cases.iter().take(MAX_CASES) {
        let w: Vec<String> = lc.weights_lb.iter().map(|w| format!("{w:.0}")).collect();
        let total: f64 = lc.weights_lb.iter().sum();
        out.push(format!("  case {:<16} crew [{}] lb = {:.0} lb", lc.name, w.join(", "), total));
    }
    if p.load_cases.len() > MAX_CASES {
        out.push(format!("  ... {} more load cases", p.load_cases.len() - MAX_CASES));
    }

    let n = r.candidates.len();
    let passed = r.candidates.iter().filter(|c| c.passes).count();
    let errs: Vec<&str> = r.candidates.iter().filter_map(|c| c.error.as_deref()).collect();
    out.push(format!("candidates {n}  passed {passed}  errored {}", errs.len()));
    if !errs.is_empty() {
        let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
        for e in &errs {
            *counts.entry(e).or_default() += 1;
        }
        // Deterministic: highest count, ties broken by message order.
        let (msg, cnt) = counts
            .iter()
            .max_by(|a, b| a.1.cmp(b.1).then(b.0.cmp(a.0)))
            .map(|(m, c)| (*m, *c))
            .unwrap_or(("", 0));
        let msg: String = msg.chars().take(90).collect();
        out.push(format!("  most common error ({cnt}x): {msg}"));
    }

    // Shape runs: candidates differ in length/beam, so show L and B columns.
    let shape_cols = r.candidates.iter().any(|c| {
        (c.length_in - r.candidates[0].length_in).abs() > 1e-6
            || (c.beam_in - r.candidates[0].beam_in).abs() > 1e-6
    });
    // Frontier: lightest passing candidate per thickness.
    let mut frontier: Vec<&Candidate> = Vec::new();
    for c in r.candidates.iter().filter(|c| c.passes) {
        match frontier.iter_mut().find(|f| f.thickness_in == c.thickness_in) {
            Some(f) => {
                if c.hull_weight_lb < f.hull_weight_lb {
                    *f = c;
                }
            }
            None => frontier.push(c),
        }
    }
    frontier.sort_by(|a, b| a.thickness_in.total_cmp(&b.thickness_in));
    out.push(String::new());
    if frontier.is_empty() {
        out.push("frontier: (no passing candidates)".to_string());
    } else {
        out.push("frontier (lightest passing per thickness)".to_string());
        let lb_head = if shape_cols { format!(" {:>6} {:>5}", "L in", "B in") } else { String::new() };
        out.push(format!(
            "  {:>6} {:>5} {:>6} {:>8} {:>6} {:>7}{lb_head}  {}",
            "t in", "pcf", "depth", "wt lb", "D/C", "fb in", "case"
        ));
        for c in frontier.iter().take(MAX_FRONTIER) {
            let v = c.verdict.as_ref();
            let lb =
                if shape_cols { format!(" {:>6.1} {:>5.1}", c.length_in, c.beam_in) } else { String::new() };
            out.push(format!(
                "  {:>6.3} {:>5.1} {:>6.2} {:>8.2} {:>6} {:>7}{lb}  {}",
                c.thickness_in,
                c.density_pcf,
                c.depth_in,
                c.hull_weight_lb,
                fmt_opt(v.map(|v| v.dc), 3),
                fmt_opt(v.map(|v| v.freeboard_in), 2),
                c.governing_case.as_deref().unwrap_or("-")
            ));
        }
        if frontier.len() > MAX_FRONTIER {
            out.push(format!("  ... {} more thicknesses", frontier.len() - MAX_FRONTIER));
        }
    }

    out.push(String::new());
    let g = &p.gates;
    match &r.winner {
        Some(w) => {
            let c = &w.candidate;
            let v = c.verdict.as_ref();
            let rho = match &c.mix {
                Some(m) => format!("mix {m} ({:.1} pcf)", c.density_pcf),
                None => format!("rho {:.1} pcf", c.density_pcf),
            };
            out.push(format!(
                "WINNER  t {:.3} in  {rho}  depth {:.2} in  weight {:.2} lb",
                c.thickness_in, c.depth_in, c.hull_weight_lb
            ));
            out.push(format!(
                "        D/C {}  freeboard {} in  governing {}",
                fmt_opt(v.map(|v| v.dc), 3),
                fmt_opt(v.map(|v| v.freeboard_in), 2),
                c.governing_case.as_deref().unwrap_or("-")
            ));
            out.push(hull_line(c));
            if let Some(sh) = &c.shape {
                out.push(format!(
                    "        shape fore_wp {:.2} aft_wp {:.2} section {:.2} rocker {:.2}/{:.2} taper {:.2}/{:.2}",
                    sh.fore_wp, sh.aft_wp, sh.section, sh.fore_rocker, sh.aft_rocker, sh.fore_len_frac, sh.aft_len_frac
                ));
            }
            let mut extra = vec![];
            if let Some(gm) = c.gm_in {
                extra.push(format!("GM {gm:.2} in (min {:.2})", p.stability.gm_min_in));
            }
            if let Some(d) = c.drag_n {
                extra.push(format!(
                    "drag {:.2} lb ({d:.1} N) at {:.1} m/s",
                    d / crate::units::N_PER_LBF,
                    p.objective.speed_mps
                ));
            }
            if let Some(l) = c.air_pocket_in {
                extra.push(format!("swamped: air pocket {l:.1} in each end"));
            }
            if !extra.is_empty() {
                out.push(format!("        {}", extra.join("  ")));
            }
            if let Some(t) = c.race_s {
                let mut l = format!("        race {t:.1} s weighted (paths planned on the reference shape)");
                if let Some(rr) = &r.race {
                    l += &format!(
                        ", {:.1} s re-planned; straight_weight {:.2}",
                        rr.weighted_time_s, rr.recommended.straight_weight
                    );
                }
                out.push(l);
            }
            if let Some(v) = v {
                for k in &v.checks {
                    let mark = if k.name == v.governing_check { "  <- governs" } else { "" };
                    if k.name == "biaxial" {
                        out.push(format!(
                            "        {:<20} Kupfer envelope{:>29}D/C {:.3}{mark}",
                            k.name, "", k.dc
                        ));
                        continue;
                    }
                    out.push(format!(
                        "        {:<20} demand {:>8.1} psi  capacity {:>8.1} psi  D/C {:.3}{mark}",
                        k.name, k.demand, k.capacity, k.dc
                    ));
                }
            }
            for k in w.cases.iter().take(MAX_CASES) {
                out.push(case_line(k));
            }
            out.push(format!(
                "        {}VERIFIED at {} stations (D/C {:.3}){}",
                if w.verified { "" } else { "NOT " },
                w.verify_stations,
                w.verify_dc,
                if w.verified { String::new() } else { format!(", gate {:.3}", g.dc_verify) }
            ));
            out.push(convergence_line(w));
            out.push(provenance_line(&w.mix_provenance));
            if w.at_mix_data_edge {
                out.push(mix_edge_note(r, w));
            }
            if w.on_grid_edge {
                out.push("WARNING winner is on the search grid edge; widen [search] ranges".to_string());
            }
            // Last, so the line cap trims the table before any warning.
            out.extend(search_space_lines(&w.search_space));
        }
        None => {
            out.push("NO DESIGN PASSES".to_string());
            let analysed: Vec<&Candidate> = r.candidates.iter().filter(|c| c.verdict.is_some()).collect();
            let best = analysed.iter().min_by(|a, b| {
                let (a, b) = (a.verdict.as_ref(), b.verdict.as_ref());
                a.map(|v| v.dc).unwrap_or(f64::INFINITY).total_cmp(&b.map(|v| v.dc).unwrap_or(f64::INFINITY))
            });
            match best {
                None => out.push("  hint: no candidate could be analysed (see errors)".into()),
                Some(b) => {
                    let bv = b.verdict.as_ref().expect("filtered");
                    out.push(format!(
                        "  lowest D/C seen {:.3} (limit {:.3}) at t {:.3} rho {:.1} depth {:.2}",
                        bv.dc, g.dc_search, b.thickness_in, b.density_pcf, b.depth_in
                    ));
                    let dc_ok = analysed
                        .iter()
                        .filter(|c| c.verdict.as_ref().is_some_and(|v| v.dc <= g.dc_search))
                        .count();
                    let fb_ok = analysed
                        .iter()
                        .filter(|c| c.verdict.as_ref().is_some_and(|v| v.freeboard_in >= g.freeboard_min_in))
                        .count();
                    let gm_min = p.stability.gm_min_in;
                    let gm_fail = analysed.iter().filter(|c| c.gm_in.is_some_and(|g| g < gm_min)).count();
                    let best_gm = analysed.iter().filter_map(|c| c.gm_in).fold(f64::NEG_INFINITY, f64::max);
                    let blocker = if gm_fail == analysed.len() {
                        format!(
                            "stability: best GM {best_gm:.2} in < gm_min {gm_min:.2} in (hull too narrow/tender)"
                        )
                    } else if dc_ok == 0 && fb_ok == 0 {
                        "both D/C and freeboard".to_string()
                    } else if dc_ok == 0 {
                        "D/C (no candidate under the limit)".to_string()
                    } else if fb_ok == 0 {
                        format!("freeboard (none >= {:.2} in)", g.freeboard_min_in)
                    } else {
                        format!("freeboard: {dc_ok} pass D/C but none also meet {:.2} in", g.freeboard_min_in)
                    };
                    out.push(format!("  hint: blocker is {blocker}"));
                }
            }
        }
    }
    out.truncate(MAX_LINES);
    out
}

/// The designed canoe in a `jaguar-result/1` file, as `jaguar race` needs it.
pub struct RaceInput {
    pub hull: crate::hull::Hull,
    pub hull_lb: f64,
    pub depth_in: f64,
    /// Flare and gunwale outline the winner was built with.
    pub topsides: crate::hull::Topsides,
    /// `[race]` of the run's canoe.toml, if it had one.
    pub race: Option<crate::params::RaceParams>,
}

/// Winner hull (generated, or `hull.file`), wall weight, depth and flare of a result file.
pub fn race_input(text: &str) -> Result<RaceInput> {
    use anyhow::Context;
    let v: serde_json::Value = serde_json::from_str(text).context("parsing result JSON")?;
    anyhow::ensure!(v["format"] == crate::optimize::RESULT_FORMAT, "not a jaguar-result/1 file");
    let c = &v["winner"]["candidate"];
    anyhow::ensure!(c.is_object(), "the result has no winner: no canoe to race");
    let num = |f: &str| c[f].as_f64().with_context(|| format!("winner has no {f}"));
    let hull: crate::hull::Hull = if v["winner_hull"].is_object() {
        serde_json::from_value(v["winner_hull"].clone()).context("winner_hull")?
    } else {
        let f =
            v["params"]["hull"]["file"].as_str().context("result has neither winner_hull nor hull.file")?;
        crate::hull::Hull::load(Path::new(f))?
    };
    hull.validate()?;
    let race = match &v["params"]["race"] {
        serde_json::Value::Null => None,
        r => Some(serde_json::from_value(r.clone()).context("params.race")?),
    };
    Ok(RaceInput {
        hull,
        hull_lb: num("hull_weight_lb")?,
        depth_in: num("depth_in")?,
        topsides: result_topsides(&v),
        race,
    })
}

/// Topsides of a result's winner: `hull.flare_deg`, and the gunwale radius of
/// the winner's shape (shape search) or `hull.gunwale_radius_in`.
pub fn result_topsides(v: &serde_json::Value) -> crate::hull::Topsides {
    let h = &v["params"]["hull"];
    crate::hull::Topsides {
        flare_deg: h["flare_deg"].as_f64().unwrap_or(0.0),
        gunwale_radius_in: v["winner"]["candidate"]["shape"]["gunwale_radius_in"]
            .as_f64()
            .or_else(|| h["gunwale_radius_in"].as_f64()),
        gunwale_max_flare_deg: h["gunwale_max_flare_deg"]
            .as_f64()
            .unwrap_or(crate::hull::GUNWALE_MAX_FLARE_DEG),
    }
}

/// Terminal summary of a race run: one line per event, then the recommended weightings.
pub fn race_lines(r: &crate::race::RaceResult) -> Vec<String> {
    let mut out = vec![
        format!("race: {} at {:.1} lb hull, weighted time {:.1} s", r.hull, r.hull_lb, r.weighted_time_s),
        format!(
            "{:<34} {:>6} {:>7} {:>7} {:>9} {:>9} {:>6} {:>7} {:>7}",
            "event", "crew", "time s", "dist m", "straight", "turning", "turns", "S_str", "S_turn"
        ),
    ];
    for e in &r.events {
        let turns = e.segments.iter().filter(|s| s.radius_m.is_some()).count();
        out.push(format!(
            "{:<34} {:>6.0} {:>7.1} {:>7.1} {:>4.0}m/{:>3.0}s {:>4.0}m/{:>3.0}s {:>6} {:>7.3} {:>7.3}",
            e.name,
            e.crew_lb,
            e.time_s,
            e.distance_m,
            e.straight_m,
            e.straight_s,
            e.turn_m,
            e.turn_s,
            turns,
            e.sens_straight,
            e.sens_turn
        ));
    }
    let rec = &r.recommended;
    out.push(format!(
        "recommended [objective]: straight_weight = {:.3}, turn_radius_in = {:.0}",
        rec.straight_weight, rec.turn_radius_in
    ));
    // Rounded to 0.001 with the last share taking the remainder, so the printed
    // weights sum to exactly 1 (canoe.toml checks the sum).
    let n = rec.displacements.len();
    let mut milli: Vec<i64> = rec.displacements.iter().map(|d| (d.weight * 1000.0).round() as i64).collect();
    if n > 0 {
        milli[n - 1] = 1000 - milli[..n - 1].iter().sum::<i64>();
    }
    out.push(format!(
        "  displacements = [{}]",
        rec.displacements
            .iter()
            .zip(&milli)
            .map(|(d, m)| format!("{{ crew_lb = {:.0}, weight = {:.3} }}", d.crew_lb, *m as f64 / 1000.0))
            .collect::<Vec<_>>()
            .join(", ")
    ));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::check::Verdict;
    use crate::optimize::{RESULT_FORMAT, Winner};
    use crate::params::Params;

    fn cand(t: f64, rho: f64, wt: f64, dc: f64, fb: f64, gates_ok: bool) -> Candidate {
        Candidate {
            length_in: 240.0,
            beam_in: 20.7,
            shape: None,
            gm_in: None,
            drag_n: None,
            race_s: None,
            air_pocket_in: None,
            thickness_in: t,
            density_pcf: rho,
            mix: None,
            depth_in: 12.0,
            hull_weight_lb: wt,
            verdict: Some(Verdict {
                dc,
                governing_check: "bending_tension",
                x_governing_in: 100.0,
                freeboard_in: fb,
                checks: vec![],
            }),
            governing_case: Some("race".into()),
            error: None,
            passes: gates_ok,
        }
    }

    fn base(winner: Option<Winner>, all_fail: bool) -> RunResult {
        let params = Params::load(Path::new("examples/canoe.toml")).unwrap();
        let mut candidates = vec![
            cand(0.25, 70.0, 84.8, 0.786, 6.42, !all_fail),
            cand(0.25, 80.0, 96.0, 0.70, 6.0, !all_fail),
            cand(0.375, 60.0, 110.0, 0.55, 5.5, !all_fail),
            cand(0.20, 60.0, 60.0, 1.4, 7.0, false),
        ];
        let mut e = cand(0.5, 100.0, 200.0, 0.0, 0.0, false);
        e.verdict = None;
        e.governing_case = None;
        e.error = Some("hull sinks: displacement exceeded".into());
        candidates.push(e.clone());
        candidates.push(e);
        let strength = crate::optimize::strength(&params).unwrap();
        RunResult {
            format: RESULT_FORMAT,
            params,
            strength,
            winner_hull: None,
            candidates,
            winner,
            race: None,
        }
    }

    fn winner(verified: bool, edge: bool) -> Winner {
        let params = Params::load(Path::new("examples/canoe.toml")).unwrap();
        let strength = crate::optimize::strength(&params).unwrap();
        let candidate = cand(0.25, 70.0, 84.8, 0.786, 6.42, true);
        Winner {
            mix_provenance: strength.provenance(70.0, None).unwrap(),
            search_space: crate::optimize::search_space(&params, &strength, &candidate).unwrap(),
            candidate,
            verify_stations: 81,
            verify_dc: if verified { 0.783 } else { 1.02 },
            verified,
            dc_extrapolated: Some(0.79),
            dc_order: Some(2.0),
            convergence: crate::optimize::Convergence {
                stations: [41, 81, 161],
                dc: [0.775, 0.783, 0.785],
                extrapolated: Some(0.79),
                order: Some(2.0),
                governing: Some("race/bending_tension".into()),
                note: None,
            },
            on_grid_edge: edge,
            at_mix_data_edge: false,
            diagrams: Default::default(),
            cases: vec![crate::optimize::CaseDiagrams {
                name: "race".into(),
                seats_in: vec![60.0, 180.0],
                weights_lb: vec![160.0, 150.0],
                crew_order: vec![0, 1],
                seat_source: Some(crate::seats::SeatSource::Auto),
                seat_variance_in: 3.0,
                ..Default::default()
            }],
            empty: Default::default(),
            calc: Default::default(),
        }
    }

    fn show(lines: &[String]) {
        println!("----\n{}", lines.join("\n"));
    }

    #[test]
    fn winner_verified() {
        let l = summary_lines(&base(Some(winner(true, false)), false));
        show(&l);
        let s = l.join("\n");
        assert!(s.contains("VERIFIED at 81 stations (D/C 0.783)"));
        assert!(s.contains("case race         seats [60@160, 180@150] in auto +/-3  "), "{s}");
        assert!(!s.contains("NOT VERIFIED"));
        assert!(!s.contains("WARNING"));
        assert!(!s.contains("L in"));
        assert!(s.contains("passed 3  errored 2"));
        assert!(s.contains("most common error (2x): hull sinks"));
        // Frontier: 0.25 row picks the lighter (84.80) candidate.
        assert!(s.contains("84.80"));
        assert!(!s.contains("96.00"));
        assert!(l.len() <= MAX_LINES);
    }

    #[test]
    fn provenance_and_search_space_lines() {
        let mut w = winner(true, false);
        w.search_space[2].at_bound = Some(crate::optimize::Bound::Max);
        w.search_space[0].at_bound = Some(crate::optimize::Bound::Min);
        w.search_space[0].limit = Some("buildable floor");
        let mut shape = w.search_space[2].clone();
        shape.name = "length_in";
        w.search_space.push(shape.clone());
        shape.at_bound = None;
        w.search_space.push(shape.clone());
        w.search_space.push(shape);
        let l = summary_lines(&base(Some(w), false));
        show(&l);
        let s = l.join("\n");
        assert!(
            s.contains(
                "mix 70 pcf predicted (fit); nearest tested: final target 60.0 (-10.0); reach 10.0 pcf"
            ),
            "{s}"
        );
        assert!(s.contains("thickness_in"));
        assert!(s.contains("min (buildable floor)"));
        assert!(s.contains("0.0625"));
        let depth = l.iter().find(|x| x.trim_start().starts_with("depth_in")).unwrap();
        assert!(depth.ends_with("max (widen)"), "{depth}");
        assert!(s.contains("  length_in"));
        assert!(s.contains("+ 2 shape factors inside their bounds"));
        assert!(l.len() <= MAX_LINES);
    }

    #[test]
    fn winner_on_edge_and_unverified() {
        let l = summary_lines(&base(Some(winner(false, true)), false));
        show(&l);
        let s = l.join("\n");
        assert!(s.contains("NOT VERIFIED"));
        assert!(s.contains("WARNING"));
    }

    #[test]
    fn mix_and_shape_lines() {
        let mut r = base(Some(winner(true, false)), false);
        let w = r.winner.as_mut().unwrap();
        w.at_mix_data_edge = true;
        w.candidate.gm_in = Some(4.3);
        w.candidate.drag_n = Some(23.1);
        w.candidate.race_s = Some(104.2);
        w.candidate.shape = Some(crate::hullgen::ShapeParams {
            length_in: 240.0,
            beam_in: 20.7,
            draft_in: 5.9,
            fore_wp: 1.0,
            section: 0.5,
            fore_rocker: 2.0,
            aft_wp: 1.0,
            aft_rocker: 2.0,
            fore_len_frac: 0.4,
            aft_len_frac: 0.4,
            gunwale_radius_in: None,
            metrics: None,
        });
        r.candidates[0].length_in = 220.0;
        let l = summary_lines(&r);
        show(&l);
        let s = l.join("\n");
        assert!(s.contains("strength: final split ="));
        assert!(s.contains("NOTE density 70 pcf"));
        assert!(s.contains("hull  L 240.0 in (20 ft 0 in)  beam 20.7 in  draft 5.9 in"));
        assert!(s.contains("GM 4.30 in (min"));
        assert!(s.contains("drag 5.19 lb (23.1 N) at"));
        assert!(s.contains("race 104.2 s weighted"));
        assert!(s.contains("shape fore_wp"));
        assert!(s.contains("L in"));
        assert!(l.len() <= MAX_LINES);
    }

    #[test]
    fn mix_edge_note_names_the_bound() {
        // Example lab data is tested 54.9..60 pcf; extrapolation measured from the matching end.
        let note = |rho: f64, b| {
            let mut r = base(Some(winner(true, false)), false);
            let w = r.winner.as_mut().unwrap();
            w.at_mix_data_edge = true;
            w.candidate.density_pcf = rho;
            w.search_space[1].at_bound = Some(b);
            summary_lines(&r).into_iter().find(|l| l.starts_with("NOTE density")).unwrap()
        };
        use crate::optimize::Bound;
        let lo = note(51.0, Bound::Min);
        assert!(
            lo.contains("51 pcf is the lightest") && lo.contains("(+4 pcf") && lo.contains("lighter"),
            "{lo}"
        );
        let hi = note(64.0, Bound::Max);
        assert!(
            hi.contains("64 pcf is the heaviest") && hi.contains("(+4 pcf") && !hi.contains("lighter"),
            "{hi}"
        );
    }

    #[test]
    fn no_winner() {
        let l = summary_lines(&base(None, true));
        show(&l);
        let s = l.join("\n");
        assert!(s.contains("NO DESIGN PASSES"));
        assert!(s.contains("lowest D/C seen 0.550"));
        assert!(s.contains("hint: blocker is"));
        let mut r = base(None, true);
        for c in &mut r.candidates {
            c.gm_in = Some(0.1);
        }
        assert!(summary_lines(&r).join("\n").contains("blocker is stability: best GM 0.10"));
        assert!(l.len() <= MAX_LINES);
    }

    #[test]
    fn line_cap_with_many_candidates() {
        let mut r = base(Some(winner(true, true)), false);
        for i in 0..100 {
            r.candidates.push(cand(0.1 + i as f64 * 0.01, 60.0, 50.0, 0.5, 6.0, true));
        }
        let l = summary_lines(&r);
        assert!(l.len() <= MAX_LINES, "{} lines", l.len());
        assert!(l.join("\n").contains("more thicknesses"));
    }
}
