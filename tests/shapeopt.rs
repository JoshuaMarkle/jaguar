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
    // Search space: wall axes, then the 8 free shape factors (fixed ones left out).
    let w = r.winner.as_ref().expect("winner");
    let names: Vec<&str> = w.search_space.iter().map(|a| a.name).collect();
    assert_eq!(&names[..3], ["thickness_in", "density_pcf", "depth_in"]);
    assert_eq!(names.len(), 3 + 8);
    assert!(!names.contains(&"beam_in") && !names.contains(&"section"));
    let s = w.candidate.shape.as_ref().unwrap();
    let len = w.search_space.iter().find(|a| a.name == "length_in").unwrap();
    assert_eq!((len.min, len.max, len.step, len.count, len.winner), (216.0, 264.0, None, None, s.length_in));
}

fn shapes(r: &jaguar::optimize::RunResult) -> Vec<&jaguar::hullgen::ShapeParams> {
    r.candidates.iter().map(|c| c.shape.as_ref().unwrap()).collect()
}

#[test]
fn symmetric_shapes_have_identical_ends() {
    let mut p = small(false);
    p.shape_search.as_mut().unwrap().symmetric = true;
    let r = jaguar::run(&p, &|_| {}).unwrap();
    for s in shapes(&r) {
        assert_eq!((s.aft_wp, s.aft_rocker, s.aft_len_frac), (s.fore_wp, s.fore_rocker, s.fore_len_frac));
        let m = s.metrics.as_ref().unwrap();
        assert!((m.rocker_bow_in - m.rocker_stern_in).abs() < 1e-12);
    }
    let h = r.winner_hull.as_ref().unwrap();
    let n = h.x.len();
    for i in 0..n {
        for (a, b) in h.half_breadth[i].iter().zip(&h.half_breadth[n - 1 - i]) {
            assert!((a - b).abs() < 1e-9);
        }
    }
}

#[test]
fn steps_snap_draft_length_beam() {
    let mut p = small(false);
    let ss = p.shape_search.as_mut().unwrap();
    ss.draft_step_in = Some(0.5);
    ss.length_step_in = Some(12.0);
    ss.beam_step_in = Some(1.0);
    let r = jaguar::run(&p, &|_| {}).unwrap();
    let on = |v: f64, st: f64| ((v / st).round() * st - v).abs() < 1e-9;
    for s in shapes(&r) {
        assert!(on(s.draft_in, 0.5) && on(s.length_in, 12.0) && on(s.beam_in, 1.0), "{s:?}");
        assert!((216.0..=264.0).contains(&s.length_in) && (4.0..=8.0).contains(&s.draft_in));
    }
    // Stepped shape axes report their grid.
    let w = r.winner.as_ref().expect("winner");
    let len = w.search_space.iter().find(|a| a.name == "length_in").unwrap();
    assert_eq!((len.step, len.count), (Some(12.0), Some(5)));
    let at_min = len.at_bound == Some(jaguar::optimize::Bound::Min);
    assert_eq!(at_min, len.winner == 216.0);
}

#[test]
fn entry_angle_and_rocker_reject_shapes() {
    let mut p = small(false);
    p.shape_search.as_mut().unwrap().min_entry_angle_deg = Some(12.0);
    let r = jaguar::run(&p, &|_| {}).unwrap();
    let mut rejected = 0;
    for c in &r.candidates {
        let m = c.shape.as_ref().unwrap().metrics.as_ref().unwrap();
        if m.entry_angle_deg < 12.0 {
            rejected += 1;
            assert!(!c.passes && c.error.as_ref().unwrap().contains("entry half-angle"));
        }
    }
    assert!(rejected > 0, "bound should reject some shapes");
    if let Some(w) = &r.winner {
        assert!(w.candidate.shape.as_ref().unwrap().metrics.as_ref().unwrap().entry_angle_deg >= 12.0);
    }
    // An impossible rocker band rejects everything: no winner, never a rejected one.
    let mut p = small(false);
    p.shape_search.as_mut().unwrap().rocker_in = Some([50.0, 60.0]);
    let r = jaguar::run(&p, &|_| {}).unwrap();
    assert!(r.winner.is_none());
    assert!(r.candidates.iter().all(|c| c.error.as_ref().unwrap().contains("rocker")));
}

#[test]
fn turning_weight_moves_toward_lower_lateral_moment() {
    let mut p = small(false);
    p.objective.kind = jaguar::params::ObjectiveKind::Drag;
    p.objective.straight_weight = 0.0;
    let b = jaguar::run(&p, &|_| {}).unwrap();
    let mom = |r: &jaguar::optimize::RunResult| {
        r.winner
            .as_ref()
            .unwrap()
            .candidate
            .shape
            .as_ref()
            .unwrap()
            .metrics
            .as_ref()
            .unwrap()
            .lateral_moment_in4
    };
    // Pure turning: winner has the least lateral moment of all passing shapes.
    let least = b
        .candidates
        .iter()
        .filter(|c| c.passes)
        .map(|c| c.shape.as_ref().unwrap().metrics.as_ref().unwrap().lateral_moment_in4)
        .fold(f64::INFINITY, f64::min);
    assert!(mom(&b) <= least + 1e-9, "{} vs {least}", mom(&b));
}
