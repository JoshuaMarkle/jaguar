//! Golden tests: the pipeline must reproduce `tests/golden/reference.py`,
//! which generated `tests/golden/*_v0.json`.
//! Never loosen a tolerance to make one pass: find the bug.

use jaguar::equilibrium::{self, CrewLoad};
use jaguar::hull::{Grid, Hull};
use jaguar::{check, mix::StrengthFit, params::Material, section};
use serde_json::Value;
use std::path::Path;

fn golden() -> (Hull, Vec<Value>, f64) {
    let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let g: Value =
        serde_json::from_str(&std::fs::read_to_string("tests/golden/bending_v0.json").unwrap()).unwrap();
    let tol = g["tolerance_rel"].as_f64().unwrap();
    (hull, g["cases"].as_array().unwrap().clone(), tol)
}

fn f(v: &Value, k: &str) -> f64 {
    v[k].as_f64().unwrap_or_else(|| panic!("missing {k}"))
}

fn assert_rel(name: &str, got: f64, want: f64, tol: f64) {
    let rel = (got - want).abs() / want.abs().max(1e-9);
    assert!(rel <= tol, "{name}: got {got}, want {want} (rel err {rel:.2e} > {tol})");
}

/// Hull weight and draft from equilibrium.
#[test]
fn golden_weight_and_draft() {
    let (hull, cases, tol) = golden();
    for c in &cases {
        let (i, o) = (&c["inputs"], &c["outputs"]);
        let grid = Grid::build(&hull, f(i, "depth"), f(i, "stations") as usize);
        let sec = section::sections(&grid, f(i, "thickness"));
        let crew = crew(i);
        let loads =
            equilibrium::solve(&grid, &sec, f(i, "density"), &crew, f(i, "knee_patch_in"), 12.0).unwrap();
        assert_rel("weight", loads.hull_weight_lb, f(o, "weight"), tol);
        assert_rel("draft", loads.draft_in, f(o, "draft"), tol);
        let mmax = loads.moment.iter().fold(0.0_f64, |a, m| a.max(m.abs()));
        assert_rel("Mmax", mmax, f(o, "Mmax"), tol);
    }
}

/// Governing bending D/C.
#[test]
fn golden_bending_dc() {
    let (hull, cases, tol) = golden();
    for c in &cases {
        let (i, o) = (&c["inputs"], &c["outputs"]);
        let grid = Grid::build(&hull, f(i, "depth"), f(i, "stations") as usize);
        let sec = section::sections(&grid, f(i, "thickness"));
        let loads =
            equilibrium::solve(&grid, &sec, f(i, "density"), &crew(i), f(i, "knee_patch_in"), 12.0).unwrap();
        let mat = Material {
            strength_model: jaguar::params::StrengthModel::Fit,
            mix_data: "unused".into(),
            flex_over_split: 1.0,
            density_extrapolation_pcf: 0.0,
            factor_of_safety: f(i, "fs"),
            dynamic_load_factor: f(i, "dyn"),
            knee_patch_in: f(i, "knee_patch_in"),
        };
        // The reference script's tensile line f_t = slope*rho + icpt, expressed as a
        // StrengthFit with flex/split = 1 so bending capacity is identical.
        let fit = StrengthFit {
            points: vec![],
            split_slope_psi_per_pcf: f(i, "ft_slope"),
            split_intercept_psi: f(i, "ft_icpt"),
            rms_residual_psi: 0.0,
            density_min_pcf: 0.0,
            density_max_pcf: 0.0,
            flex_over_split: 1.0,
        };
        let pu = jaguar::params::Punching {
            method: jaguar::params::PunchingMethod::Aci22_6,
            knees: 1,
            knee_split: 0.5,
            knee_spacing_in: 8.0,
            pad_in: 0.0,
            d_over_t: 0.5,
        };
        let v = check::check(&grid, &sec, &loads, f(i, "density"), &mat, &fit, 175.0 * 1.2, &pu);
        let bt = v.checks.iter().find(|c| c.name == "bending_tension").unwrap();
        assert_rel("dc", bt.dc, f(o, "dc"), tol);
    }
}

fn crew(i: &Value) -> Vec<CrewLoad> {
    let dyn_ = f(i, "dyn");
    let w = i["crew_lb"].as_array().unwrap();
    let s = i["seats_in"].as_array().unwrap();
    w.iter()
        .zip(s)
        .map(|(w, s)| CrewLoad { x_in: s.as_f64().unwrap(), lb: w.as_f64().unwrap() * dyn_ })
        .collect()
}

/// Grid::build reproduces the reference build() at depth 14, 161 stations.
#[test]
fn golden_grid_matches_reference() {
    let g: Value =
        serde_json::from_str(&std::fs::read_to_string("tests/golden/grid_v0.json").unwrap()).unwrap();
    let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let grid = Grid::build(&hull, f(&g, "depth"), f(&g, "stations") as usize);
    assert_eq!(grid.nz(), g["nz"].as_u64().unwrap() as usize);
    assert_rel("sum", grid.y.iter().sum(), f(&g, "sum"), 1e-9);
    assert_rel("sumsq", grid.y.iter().map(|v| v * v).sum(), f(&g, "sumsq"), 1e-9);
    for (k, want) in g["row80"].as_array().unwrap().iter().enumerate() {
        assert!((grid.y(80, k) - want.as_f64().unwrap()).abs() < 1e-9, "row80[{k}]");
    }
}
