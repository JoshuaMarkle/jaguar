//! The PDF report's design proof data: a worked calculation per load case that matches the
//! checks, and distributed loads that integrate to the shear and close the equilibrium.

use jaguar::params::Params;
use std::path::Path;

#[test]
fn every_load_case_is_worked_and_its_loads_close() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let r = jaguar::run(&p, &|_| {}).unwrap();
    let w = r.winner.as_ref().expect("winner");
    assert_eq!(w.case_calcs.len(), p.load_cases.len());
    for (lc, (tr, case)) in p.load_cases.iter().zip(w.case_calcs.iter().zip(&w.cases)) {
        assert!(tr.error.is_none(), "{}: {:?}", lc.name, tr.error);
        let k = tr.calc.as_ref().unwrap();
        assert_eq!(k.case, lc.name);
        // Each worked check equals that case's verdict (same code, same station).
        let v = case.verdict.as_ref().unwrap();
        let check = |name: &str| v.checks.iter().find(|c| c.name == name).unwrap();
        let close = |a: f64, b: f64| (a - b).abs() <= 1e-9 * b.abs().max(1.0);
        if let Some(b) = &k.bending_tension {
            assert!(close(b.dc, check("bending_tension").dc), "{}: tension", lc.name);
        }
        if let Some(s) = &k.shear {
            assert!(close(s.dc, check("shear").dc), "{}: shear", lc.name);
        }
        assert!(close(k.punching.dc, check("punching").dc), "{}: punching", lc.name);

        // q = buoyancy - self weight integrates (trapezoid) to the distributed part of V; the
        // knee patches bring V and M back to zero at the stern.
        let d = &case.diagrams;
        let n = d.x_in.len();
        assert_eq!((d.buoyancy_lbpin.len(), d.self_weight_lbpin.len(), d.zbar_in.len()), (n, n, n));
        let trapz =
            |f: &[f64]| (1..n).map(|i| 0.5 * (f[i] + f[i - 1]) * (d.x_in[i] - d.x_in[i - 1])).sum::<f64>();
        let (b, sw) = (trapz(&d.buoyancy_lbpin), trapz(&d.self_weight_lbpin));
        let crew: f64 = k.strength.crew_lb;
        assert!((b - (sw + crew)).abs() < 1e-6 * b, "{}: buoyancy {b} vs weight {}", lc.name, sw + crew);
        assert!((sw - k.strength.hull_weight_lb).abs() < 1e-6 * sw);
        let vmax = d.shear_lb.iter().fold(0.0f64, |a, v| a.max(v.abs()));
        let mmax = d.moment_lbin.iter().fold(0.0f64, |a, v| a.max(v.abs()));
        assert!(d.shear_lb[n - 1].abs() < 1e-3 * vmax && d.moment_lbin[n - 1].abs() < 1e-3 * mmax);
    }
}

#[test]
fn when_nothing_passes_the_closest_design_is_analysed_in_full_and_says_where_it_fails() {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    p.stability.gm_min_in = 50.0; // no canoe is that stiff
    let r = jaguar::run(&p, &|_| {}).unwrap();
    assert!(r.winner.is_none(), "never a winner that fails a gate");
    let cl = r.closest.as_ref().expect("closest design");
    assert!(cl.fails.iter().any(|f| f.starts_with("stability: loaded GM")), "{:?}", cl.fails);
    let w = &cl.design;
    assert!(!w.candidate.passes);
    assert_eq!((w.cases.len(), w.case_calcs.len()), (p.load_cases.len(), p.load_cases.len()));
    assert!(w.curves.is_some() && r.winner_hull.is_some());
    // The race reads it, saying it fails.
    let text = serde_json::to_string(&r).unwrap();
    let ri = jaguar::report::race_input(&text).unwrap();
    assert!(ri.fails.as_deref().unwrap().contains("stability"), "{:?}", ri.fails);
    assert_eq!(ri.hull_lb, w.candidate.hull_weight_lb);
    // And a passing run has none.
    let ok = jaguar::run(&Params::load(Path::new("examples/canoe.toml")).unwrap(), &|_| {}).unwrap();
    assert!(ok.winner.is_some() && ok.closest.is_none());
}
