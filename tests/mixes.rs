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
