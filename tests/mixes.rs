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
    let csv = std::fs::read_to_string("examples/mix_tests.csv").unwrap();
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

/// A small tested-mix run on `csv` (written to a temp file): canoe_mixes.toml, 12 in deep,
/// two-mix walls with dense mixes in `dense` when given.
fn tested_run(csv: &str, tag: &str, dense: Option<[f64; 3]>) -> anyhow::Result<optimize::RunResult> {
    let path = std::env::temp_dir().join(format!("jaguar_mix_{tag}_{}.csv", std::process::id()));
    std::fs::write(&path, csv).unwrap();
    let mut p = Params::load(Path::new("examples/canoe_mixes.toml")).unwrap();
    p.material.mix_data = path.clone();
    p.search.depth_in = [12.0, 12.0, 1.0];
    p.zones = dense.map(|d| jaguar::params::Zones { dense_pcf: d, increment_in: 0.125, max_checks: 400 });
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    let r = optimize::run(&p, &h);
    let _ = std::fs::remove_file(&path);
    r
}

const HDR: &str = "mix,cured_days,density_pcf,split_tensile_psi,fraction_of_final
";

#[test]
fn every_row_is_its_own_tested_mix_never_averaged() {
    // The rows the team entered: two M2 rows 8.5 pcf apart are two mixes at their own densities.
    let csv = format!(
        "{HDR}M2 B,7,54.860,145,0.6
M2 A,,63.35,282.67,1.0
"
    );
    let r = tested_run(&csv, "two", Some([50.0, 70.0, 1.0])).unwrap();
    let m: Vec<_> = r.strength.tested_mixes().iter().map(|m| (m.name.clone(), m.density_pcf, m.n)).collect();
    assert_eq!(m, vec![("M2 B".to_string(), 54.86, 1), ("M2 A".to_string(), 63.35, 1)]);
    for c in r.candidates.iter().filter(|c| c.zones.is_none()) {
        assert!(c.density_pcf == 54.86 || c.density_pcf == 63.35, "{}", c.density_pcf);
    }
    // ... and the two-mix search pairs them (light M2 B, dense M2 A).
    let z = r.zones.unwrap();
    assert!(z.walls > 0 && z.note.is_none(), "{z:?}");
    // Same name twice: still two mixes, numbered.
    let csv = format!(
        "{HDR}M2,7,54.860,145,0.6
M2,7,56.408,143,0.6
"
    );
    let r = tested_run(&csv, "dup", None).unwrap();
    let names: Vec<_> = r.strength.tested_mixes().iter().map(|m| m.name.clone()).collect();
    assert_eq!(names, ["M2 #1", "M2 #2"]);
}

#[test]
fn two_mix_search_says_why_when_there_is_nothing_to_pair() {
    // Dense range holding only the lighter mix: no dense mix is heavier than a light one.
    let csv = format!(
        "{HDR}M3 A,,62.0,280,1.0
M3 B,,63.35,282.67,1.0
"
    );
    let r = tested_run(&csv, "one", Some([61.5, 62.5, 0.5])).unwrap();
    let z = r.zones.as_ref().unwrap();
    assert_eq!(z.walls, 0);
    assert!(z.note.as_deref().is_some_and(|n| n.contains("no dense mix heavier")), "{z:?}");
    let text = jaguar::report::summary_lines(&r).join(
        "
",
    );
    assert!(r.winner.is_none() || text.contains("two mixes: not searched"), "{text}");
}
