use jaguar::params::Params;
use std::path::Path;

fn small(fix_beam: bool) -> Params {
    let mut p = Params::load(Path::new("examples/shape_opt.toml")).unwrap();
    p.hull.stations = 61;
    p.search.thickness_in = [0.375, 0.5, 0.125];
    p.search.density_pcf = [51.0, 60.0, 3.0];
    p.search.depth_in = [11.0, 15.0, 2.0];
    let ss = p.shape_search.as_mut().unwrap();
    ss.samples = 12;
    ss.refine_rounds = 1;
    if fix_beam {
        ss.beam_in = [24.0, 24.0];
        ss.section = [0.5, 0.5];
    }
    p
}

#[test]
fn small_shape_search_is_deterministic_and_bounded() {
    let p = small(false);
    let a = jaguar::run(&p, &|_| {}).unwrap();
    let b = jaguar::run(&p, &|_| {}).unwrap();
    assert_eq!(serde_json::to_string(&a).unwrap(), serde_json::to_string(&b).unwrap());

    let w = a.winner.as_ref().expect("winner");
    let s = w.candidate.shape.as_ref().expect("winner shape");
    let ss = p.shape_search.as_ref().unwrap();
    let v = [
        s.length_in,
        s.beam_in,
        s.draft_in,
        s.fore_wp,
        s.section,
        s.fore_rocker,
        s.aft_wp,
        s.aft_rocker,
        s.fore_len_frac,
        s.aft_len_frac,
    ];
    for (x, (n, [lo, hi])) in v.iter().zip(ss.bounds()) {
        assert!(*x >= lo && *x <= hi, "{n} = {x} outside {lo}..{hi}");
    }
    // One representative per shape (12 LHS + refinement), every one tagged.
    assert!(a.candidates.len() > 12);
    assert!(a.candidates.iter().all(|c| c.shape.is_some()));
    let best_sampled =
        a.candidates.iter().filter(|c| c.passes).map(|c| c.hull_weight_lb).fold(f64::INFINITY, f64::min);
    assert!(w.candidate.hull_weight_lb <= best_sampled + 1e-9);
    let h = a.winner_hull.as_ref().expect("winner hull");
    h.validate().unwrap();
    assert!(h.name.starts_with("opt-"));
}

#[test]
fn fixed_parameters_stay_fixed() {
    let r = jaguar::run(&small(true), &|_| {}).unwrap();
    for c in &r.candidates {
        let s = c.shape.as_ref().unwrap();
        assert_eq!((s.beam_in, s.section), (24.0, 0.5));
    }
}
