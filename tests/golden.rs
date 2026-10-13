//! Golden tests: the pipeline must reproduce `tests/golden/reference.py`,
//! which generated `tests/golden/*_v0.json`.
//! Never loosen a tolerance to make one pass: find the bug.

use jaguar::equilibrium::{self, CrewLoad};
use jaguar::hull::{Grid, Hull};
use jaguar::optimize::eval_case;
use jaguar::params::{DesignMethod, Params, PunchingMethod};
use jaguar::{check, flotation, mix::StrengthFit, params::Material, section};
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
            method: jaguar::params::DesignMethod::Asd,
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
            flexural_fit: None,
            compressive_fit: None,
        };
        let pu = jaguar::params::Punching {
            method: jaguar::params::PunchingMethod::Aci22_6,
            knees: 1,
            knee_split: 0.5,
            knee_spacing_in: 8.0,
            pad_in: 0.0,
            d_over_t: Some(0.5),
            layers: None,
            mesh_after_layer: None,
            mold: None,
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

fn load_json(path: &str) -> (Vec<Value>, f64) {
    let g: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    (g["cases"].as_array().unwrap().clone(), f(&g, "tolerance_rel"))
}

/// The reference's split-tensile line as a StrengthFit (flexural = flex_over_split x split).
fn ref_fit(i: &Value) -> StrengthFit {
    StrengthFit {
        points: vec![],
        split_slope_psi_per_pcf: f(i, "ft_slope"),
        split_intercept_psi: f(i, "ft_icpt"),
        rms_residual_psi: 0.0,
        density_min_pcf: 0.0,
        density_max_pcf: 0.0,
        flex_over_split: f(i, "flex_over_split"),
        flexural_fit: None,
        compressive_fit: None,
    }
}

/// examples/canoe.toml with the golden case's material and its crew as the only load case.
fn ref_params(i: &Value) -> Params {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let m = &mut p.material;
    m.method = match i["method"].as_str().unwrap() {
        "asd" => DesignMethod::Asd,
        "lrfd" => DesignMethod::Lrfd,
        other => panic!("unknown method {other}"),
    };
    m.flex_over_split = f(i, "flex_over_split");
    m.factor_of_safety = f(i, "fs");
    m.dynamic_load_factor = f(i, "dyn");
    m.knee_patch_in = f(i, "knee_patch_in");
    let nums =
        |k: &str| -> Vec<f64> { i[k].as_array().unwrap().iter().map(|v| v.as_f64().unwrap()).collect() };
    p.load_cases.truncate(1);
    p.load_cases[0].weights_lb = nums("crew_lb");
    p.load_cases[0].seats_in = Some(nums("seats_in"));
    p
}

const STRESS_CHECKS: [&str; 4] = ["bending_tension", "bending_compression", "shear", "biaxial"];

fn dc_of(v: &check::Verdict, name: &str) -> f64 {
    v.checks.iter().find(|c| c.name == name).unwrap_or_else(|| panic!("no check {name}")).dc
}

/// Bending tension/compression, shell shear and Kupfer-Gerstle biaxial D/C per
/// strength combination (ASD at service loads; LRFD 1.2D+1.6L and 0.9D+1.6L
/// with phi): factored equilibrium + check::check against the reference.
#[test]
fn golden_strength_checks_per_combination() {
    let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let (cases, tol) = load_json("tests/golden/strength_v0.json");
    for (n, c) in cases.iter().enumerate() {
        let (i, o) = (&c["inputs"], &c["outputs"]);
        let p = ref_params(i);
        let grid = Grid::build(&hull, f(i, "depth"), f(i, "stations") as usize);
        let sec = section::sections(&grid, f(i, "thickness"));
        let rho = f(i, "density");
        let combos = o["combos"].as_array().unwrap();
        assert_eq!(combos.len(), p.material.method.combos().len(), "case {n}: combination count");
        for (cb, &(dead, live)) in combos.iter().zip(p.material.method.combos()) {
            assert_eq!((f(cb, "dead"), f(cb, "live")), (dead, live), "case {n}: combination factors");
            let crew: Vec<CrewLoad> =
                crew(i).into_iter().map(|c| CrewLoad { lb: c.lb * live, ..c }).collect();
            let loads =
                equilibrium::solve(&grid, &sec, rho * dead, &crew, f(i, "knee_patch_in"), 12.0).unwrap();
            let tag = format!("case {n} {dead}D+{live}L");
            assert_rel(&format!("{tag} weight"), loads.hull_weight_lb, f(cb, "weight"), tol);
            assert_rel(&format!("{tag} draft"), loads.draft_in, f(cb, "draft"), tol);
            let mmax = loads.moment.iter().fold(0.0_f64, |a, m| a.max(m.abs()));
            assert_rel(&format!("{tag} Mmax"), mmax, f(cb, "Mmax"), tol);
            let v = check::check(&grid, &sec, &loads, rho, &p.material, &ref_fit(i), 175.0, &p.punching);
            for name in STRESS_CHECKS {
                assert_rel(&format!("{tag} {name}"), dc_of(&v, name), f(&cb["dc"], name), tol);
            }
        }
    }
}

/// optimize::eval_case picks the worse LRFD combination (or the one ASD case)
/// and reports its stress checks. Punching is kept below the stress checks (the
/// reference does not compute it) so they decide the governing combination.
#[test]
fn golden_lrfd_envelope_via_eval_case() {
    let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let (cases, tol) = load_json("tests/golden/strength_v0.json");
    for (n, c) in cases.iter().enumerate() {
        let (i, o) = (&c["inputs"], &c["outputs"]);
        let mut p = ref_params(i);
        p.punching.knees = 2;
        p.punching.knee_split = 0.5;
        p.punching.pad_in = 0.5;
        (p.punching.layers, p.punching.mesh_after_layer, p.punching.mold) = (None, None, None);
        p.punching.d_over_t = Some(1.0);
        let grid = Grid::build(&hull, f(i, "depth"), f(i, "stations") as usize);
        let sec = section::sections(&grid, f(i, "thickness"));
        let case = &p.load_cases[0];
        let seats = case.seats_in.clone().unwrap();
        let ev = eval_case(&p, &ref_fit(i), &grid, &sec, f(i, "density"), case, &seats).unwrap();
        let punch = dc_of(&ev.verdict, "punching");
        assert!(punch < f(o, "dc"), "case {n}: punching {punch} governs, so it would pick the combination");
        let gov = &o["combos"][o["governing"].as_u64().unwrap() as usize];
        assert_rel(&format!("case {n} governing weight"), ev.strength.hull_weight_lb, f(gov, "weight"), tol);
        assert_rel(
            &format!("case {n} service weight"),
            ev.service.hull_weight_lb,
            f(gov, "weight") / f(gov, "dead"),
            tol,
        );
        assert_rel(&format!("case {n} envelope dc"), ev.verdict.dc, f(o, "dc"), tol);
        for name in STRESS_CHECKS {
            assert_rel(&format!("case {n} {name}"), dc_of(&ev.verdict, name), f(&gov["dc"], name), tol);
        }
    }
}

/// LRFD punching: demand x live factor 1.6 and capacity x phi (0.75 aci_22_6,
/// 0.60 plain_14_5) instead of / FS. v_c does not depend on the load, so
/// D/C_lrfd = D/C_asd x 1.6 / (phi FS) exactly.
#[test]
fn lrfd_punching_scales_by_live_factor_over_phi() {
    let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let (cases, _) = load_json("tests/golden/strength_v0.json");
    let i = &cases[0]["inputs"];
    let grid = Grid::build(&hull, f(i, "depth"), f(i, "stations") as usize);
    let sec = section::sections(&grid, f(i, "thickness"));
    let rho = f(i, "density");
    let loads = equilibrium::solve(&grid, &sec, rho, &crew(i), f(i, "knee_patch_in"), 12.0).unwrap();
    for (pm, phi) in [(PunchingMethod::Aci22_6, 0.75), (PunchingMethod::Plain14_5, 0.60)] {
        let mut p = ref_params(i);
        p.punching.method = pm;
        let fs = p.material.factor_of_safety;
        let punch = |p: &Params, lb: f64| {
            dc_of(
                &check::check(&grid, &sec, &loads, rho, &p.material, &ref_fit(i), lb, &p.punching),
                "punching",
            )
        };
        p.material.method = DesignMethod::Asd;
        let asd = punch(&p, 210.0);
        p.material.method = DesignMethod::Lrfd;
        let lrfd = punch(&p, 210.0 * 1.6);
        assert_rel(&format!("{pm:?} lrfd/asd"), lrfd / asd, 1.6 / (phi * fs), 1e-12);
    }
}

/// Swamped flotation: wall volume, weight, pocket volume and pocket length.
#[test]
fn golden_flotation() {
    let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let (cases, tol) = load_json("tests/golden/flotation_v0.json");
    for (n, c) in cases.iter().enumerate() {
        let (i, o) = (&c["inputs"], &c["outputs"]);
        let grid = Grid::build(&hull, f(i, "depth"), f(i, "stations") as usize);
        let t = f(i, "thickness");
        let sec = section::sections(&grid, t);
        let fl = flotation::flotation(&grid, &sec, f(i, "density"), f(i, "swamp_factor"), 1e9).unwrap();
        assert_rel(&format!("case {n} wall"), fl.wall_in3, f(o, "wall_in3"), tol);
        assert_rel(&format!("case {n} weight"), fl.weight_lb, f(o, "weight"), tol);
        for (k, got) in [("pocket_in3", fl.pocket_in3), ("pocket_len_in", fl.pocket_len_in)] {
            let want = f(o, k);
            if want == 0.0 {
                assert_eq!(got, 0.0, "case {n} {k}: the wall alone floats");
            } else {
                assert_rel(&format!("case {n} {k}"), got, want, tol);
            }
        }
    }
}
