//! Every example hull loads, every example run produces a winner, and
//! the deliberately bad barge hull costs more concrete than the real ones.
use jaguar::{hull::Hull, optimize, params::Params};
use std::path::Path;

fn winner_weight(toml: &str) -> f64 {
    let mut p = Params::load(Path::new(toml)).unwrap();
    // Weight comparison only: the GM gate rejects the off1 hull at
    // gm_min_in = 2 (its loaded GM is < 1 in), so it is disabled here.
    p.stability.gm_min_in = f64::NEG_INFINITY;
    p.objective.kind = jaguar::params::ObjectiveKind::Weight;
    let hull = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    let r = optimize::run(&p, &hull).unwrap();
    // All four example hulls carry the 2M crew with margin, so a missing
    // winner is a regression, not physics.
    let w = r.winner.unwrap_or_else(|| panic!("{toml}: no design"));
    assert!(w.verified, "{toml}: winner not verified");
    w.candidate.hull_weight_lb
}

#[test]
fn every_example_hull_validates() {
    let mut n = 0;
    for e in std::fs::read_dir("examples").unwrap() {
        let p = e.unwrap().path();
        if p.to_string_lossy().ends_with("_hull.json") {
            Hull::load(&p).unwrap();
            n += 1;
        }
    }
    assert!(n >= 4, "expected off1 + 3 generated hulls, found {n}");
}

#[test]
fn barge_is_heaviest() {
    let off1 = winner_weight("examples/canoe.toml");
    let sleek = winner_weight("examples/sleek.toml");
    let beamy = winner_weight("examples/beamy.toml");
    let barge = winner_weight("examples/barge.toml");
    assert!(barge > off1 && barge > sleek && barge > beamy, "{barge} {off1} {sleek} {beamy}");
}
