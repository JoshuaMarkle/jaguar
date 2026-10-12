//! Terminal output and result.json. PLAN task T6.
//!
//! * `print_summary`: one line per load case header, a compact table of the
//!   passing frontier (lightest passing design per thickness), then the
//!   winner block with VERIFIED / NOT VERIFIED and any grid-edge warning.
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

/// Pure summary text, one entry per output line, never more than `MAX_LINES`.
pub fn summary_lines(r: &RunResult) -> Vec<String> {
    let p = &r.params;
    let mut out = Vec::new();
    out.push(format!(
        "JAGUAR  hull {}  ({} stations)",
        p.hull.file.as_ref().map_or("(shape search)".to_string(), |f| f.display().to_string()),
        p.hull.stations
    ));
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
        out.push(format!(
            "  {:>6} {:>5} {:>6} {:>8} {:>6} {:>7}  {}",
            "t in", "pcf", "depth", "wt lb", "D/C", "fb in", "case"
        ));
        for c in frontier.iter().take(MAX_FRONTIER) {
            let v = c.verdict.as_ref();
            out.push(format!(
                "  {:>6.3} {:>5.1} {:>6.2} {:>8.2} {:>6} {:>7}  {}",
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
            out.push(format!(
                "WINNER  t {:.3} in  rho {:.1} pcf  depth {:.2} in  weight {:.2} lb",
                c.thickness_in, c.density_pcf, c.depth_in, c.hull_weight_lb
            ));
            out.push(format!(
                "        D/C {}  freeboard {} in  governing {}",
                fmt_opt(v.map(|v| v.dc), 3),
                fmt_opt(v.map(|v| v.freeboard_in), 2),
                c.governing_case.as_deref().unwrap_or("-")
            ));
            if let Some(v) = v {
                for k in &v.checks {
                    let mark = if k.name == v.governing_check { "  <- governs" } else { "" };
                    out.push(format!(
                        "        {:<20} demand {:>8.1} psi  capacity {:>8.1} psi  D/C {:.3}{mark}",
                        k.name, k.demand, k.capacity, k.dc
                    ));
                }
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
                    let blocker = if dc_ok == 0 && fb_ok == 0 {
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
            thickness_in: t,
            density_pcf: rho,
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
        assert!(!s.contains("NOT VERIFIED"));
        assert!(!s.contains("WARNING"));
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
    fn no_winner() {
        let l = summary_lines(&base(None, true));
        show(&l);
        let s = l.join("\n");
        assert!(s.contains("NO DESIGN PASSES"));
        assert!(s.contains("lowest D/C seen 0.550"));
        assert!(s.contains("hint: blocker is"));
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
