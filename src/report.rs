//! Terminal output and result.json.
//!
//! * `print_summary`: one line per load case header, a compact table of the
//!   passing frontier (lightest passing design per thickness), then the
//!   winner block (one seats/D-C line per load case) with VERIFIED / NOT VERIFIED and any grid-edge warning.
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
    )
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

/// One winner line per load case: resolved seats (in from bow) and its D/C.
fn case_line(k: &crate::optimize::CaseDiagrams) -> String {
    let seats: Vec<String> = k.seats_in.iter().map(|s| format!("{s:.0}")).collect();
    let state = match (&k.error, &k.verdict) {
        (Some(e), _) => format!("error: {}", e.chars().take(40).collect::<String>()),
        (None, Some(v)) => format!("D/C {:.3}  fb {:.2} in", v.dc, v.freeboard_in),
        (None, None) => "-".to_string(),
    };
    format!("        case {:<12} seats [{}] in  {state}", k.name, seats.join(", "))
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
                extra.push(format!("drag {d:.1} N at {:.1} m/s", p.objective.speed_mps));
            }
            if let Some(l) = c.air_pocket_in {
                extra.push(format!("swamped: air pocket {l:.1} in each end"));
            }
            if !extra.is_empty() {
                out.push(format!("        {}", extra.join("  ")));
            }
            if let Some(v) = v {
                for k in &v.checks {
                    let mark = if k.name == v.governing_check { "  <- governs" } else { "" };
                    out.push(format!(
                        "        {:<20} demand {:>8.1} psi  capacity {:>8.1} psi  D/C {:.3}{mark}",
                        k.name, k.demand, k.capacity, k.dc
                    ));
                }
            }
            for k in w.cases.iter().take(MAX_CASES) {
                out.push(case_line(k));
            }
            if w.verified {
                out.push(format!(
                    "        VERIFIED at {} stations (D/C {:.3})",
                    w.verify_stations, w.verify_dc
                ));
            } else {
                out.push(format!(
                    "        NOT VERIFIED at {} stations (D/C {:.3} > {:.3})",
                    w.verify_stations, w.verify_dc, g.dc_verify
                ));
            }
            if w.at_mix_data_edge {
                let f = &r.strength;
                out.push(format!(
                    "NOTE density {:.0} pcf is the lightest the lab data supports (+{:.0} pcf extrapolation); test lighter mixes to go lower",
                    c.density_pcf,
                    (c.density_pcf - f.density_min_pcf).abs()
                ));
            }
            if w.on_grid_edge {
                out.push("WARNING winner is on the search grid edge; widen [search] ranges".to_string());
            }
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
        RunResult { format: RESULT_FORMAT, params, strength, winner_hull: None, candidates, winner }
    }

    fn winner(verified: bool, edge: bool) -> Winner {
        Winner {
            candidate: cand(0.25, 70.0, 84.8, 0.786, 6.42, true),
            verify_stations: 81,
            verify_dc: if verified { 0.783 } else { 1.02 },
            verified,
            on_grid_edge: edge,
            at_mix_data_edge: false,
            diagrams: Default::default(),
            cases: vec![crate::optimize::CaseDiagrams {
                name: "race".into(),
                seats_in: vec![60.0, 180.0],
                weights_lb: vec![160.0, 160.0],
                ..Default::default()
            }],
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
        assert!(s.contains("case race         seats [60, 180] in"), "{s}");
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
        assert!(s.contains("drag 23.1 N at"));
        assert!(s.contains("shape fore_wp"));
        assert!(s.contains("L in"));
        assert!(l.len() <= MAX_LINES);
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
