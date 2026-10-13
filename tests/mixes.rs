//! strength_model = "tested_mixes" designs only at tested mixes.
use jaguar::hull::Hull;
use jaguar::optimize;
use jaguar::params::{Params, StrengthModel};
use std::path::Path;

#[test]
fn tested_mixes_uses_only_tested_densities() {
    let p = Params::load(Path::new("examples/canoe_mixes.toml")).unwrap();
    assert_eq!(p.material.strength_model, StrengthModel::TestedMixes);
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    let r = optimize::run(&p, &h).unwrap();
    let mixes = r.strength.tested_mixes();
    assert!(!r.candidates.is_empty());
    for c in &r.candidates {
        let name = c.mix.as_ref().expect("every candidate names its mix");
        let m = mixes.iter().find(|m| &m.name == name).unwrap();
        assert_eq!(c.density_pcf, m.density_pcf);
    }
    let w = r.winner.expect("canoe_mixes.toml should have a winner");
    assert!(w.candidate.mix.is_some());
    assert!(!w.at_mix_data_edge);
    // A tested mix: provenance names it at zero reach; the density axis is the mix list.
    let pv = &w.mix_provenance;
    assert_eq!(pv.status, jaguar::mix::Provenance::Tested);
    assert_eq!(pv.nearest[0].name, *w.candidate.mix.as_ref().unwrap());
    assert_eq!(pv.reach_pcf, 0.0);
    let d = &w.search_space[1];
    assert_eq!((d.step, d.count), (None, Some(mixes.len())));
    assert!(d.at_bound.is_none() || d.limit == Some("tested mixes"));
}

#[test]
fn fit_mode_has_no_mix_names() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    let r = optimize::run(&p, &h).unwrap();
    assert!(r.candidates.iter().all(|c| c.mix.is_none()));
    assert!(r.winner.unwrap().candidate.mix.is_none());
}

/// A measured compressive line from 2 rows with a NEGATIVE slope (900 psi at
/// 56.41 pcf, 960 at 54.86) once predicted f'c = 1849 psi at 51 pcf and the
/// optimizer picked 51 pcf. The search must stay inside every active line's
/// tested range (+ extrapolation, none below the lightest test of a
/// negative-slope line), so here densities run 55..60 pcf.
#[test]
fn measured_fit_limits_density_range() {
    let csv = std::fs::read_to_string("data/mix_tests.csv").unwrap();
    let mut out = String::new();
    for line in csv.lines() {
        let extra = if line.starts_with('#') {
            ""
        } else if line.starts_with("mix,") {
            ",compressive_psi"
        } else if line.starts_with("M2 A,") {
            ",900"
        } else if line.starts_with("M2 B,") {
            ",960"
        } else {
            ","
        };
        out += &format!("{line}{extra}\n");
    }
    let path = std::env::temp_dir().join(format!("jaguar_t60_mix_{}.csv", std::process::id()));
    std::fs::write(&path, out).unwrap();

    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    p.material.mix_data = path.clone();
    let fit = optimize::strength(&p);
    let r = optimize::run(&p, &h);
    std::fs::remove_file(&path).ok();
    let (fit, r) = (fit.unwrap(), r.unwrap());

    let fc = fit.compressive_fit.as_ref().expect("compressive measured");
    assert!((fc.slope_psi_per_pcf + 64.6).abs() < 0.1, "{fc:?}");
    assert!(fit.weak_fit_warnings().iter().any(|w| w.contains("f'c fit has a negative slope")));
    let (lo, hi) = fit.trusted_range(p.material.density_extrapolation_pcf);
    assert!((lo - 54.86).abs() < 1e-9 && (hi - 60.408).abs() < 1e-9, "{lo} {hi}");
    let rhos = optimize::densities(&p, &fit).unwrap();
    assert_eq!((rhos[0], rhos[rhos.len() - 1]), (55.0, 60.0));

    assert!(r.candidates.iter().all(|c| c.density_pcf >= 55.0 && c.density_pcf <= 60.0));
    if let Some(w) = r.winner {
        let d = &w.search_space[1];
        assert_eq!((d.min, d.max), (55.0, 60.0));
        if w.candidate.density_pcf == 55.0 {
            assert_eq!((d.at_bound, d.limit), (Some(optimize::Bound::Min), Some("mix data")));
        }
    }
}
