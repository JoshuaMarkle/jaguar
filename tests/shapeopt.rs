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

fn turn_drag(c: &jaguar::optimize::Candidate) -> f64 {
    c.shape.as_ref().unwrap().metrics.as_ref().unwrap().turn.unwrap().drag_n
}

#[test]
fn turning_weight_moves_toward_lower_turn_drag() {
    let mut p = small(false);
    p.objective.kind = jaguar::params::ObjectiveKind::Drag;
    p.objective.straight_weight = 0.0;
    let b = jaguar::run(&p, &|_| {}).unwrap();
    // Pure turning: winner has the least steady-turn drag of all passing shapes.
    let least = b.candidates.iter().filter(|c| c.passes).map(turn_drag).fold(f64::INFINITY, f64::min);
    let w = turn_drag(&b.winner.as_ref().unwrap().candidate);
    assert!(w <= least + 1e-9, "{w} vs {least}");
    // Every shape carries a computed turn.
    assert!(b.candidates.iter().all(|c| turn_drag(c) > 0.0));
}

#[test]
fn turn_drag_falls_with_rocker_and_rises_with_length() {
    use jaguar::hullgen::ShapeParams;
    let p = small(false);
    let base = ShapeParams {
        length_in: 240.0,
        beam_in: 26.0,
        draft_in: 6.0,
        fore_wp: 0.8,
        section: 0.4,
        fore_rocker: 0.1,
        aft_wp: 0.8,
        aft_rocker: 0.1,
        fore_len_frac: 0.4,
        aft_len_frac: 0.4,
        gunwale_radius_in: None,
        metrics: None,
    };
    // 90 lb wall, 12 in mold, heaviest crew (340 lb), each floating at its own level draft.
    let td = |s: &ShapeParams| {
        let h = jaguar::hullgen::generate(s, 41, 21, "t").unwrap();
        jaguar::shapeopt::loaded_turn(&p, s, &h, 90.0, 12.0, p.hull.flare_deg).unwrap().drag_n
    };
    let t0 = td(&base);
    let rockered = ShapeParams { fore_rocker: 0.5, aft_rocker: 0.5, ..base.clone() };
    assert!(td(&rockered) < t0, "rocker turns easier");
    let long = ShapeParams { length_in: 264.0, ..base.clone() };
    assert!(td(&long) > t0, "longer hull turns harder");
    assert_eq!(td(&base), t0, "deterministic");
    // One displacement at the heaviest crew = the default.
    let mut q = small(false);
    q.objective.displacements = Some(vec![jaguar::params::Displacement { crew_lb: 340.0, weight: 1.0 }]);
    let h = jaguar::hullgen::generate(&base, 41, 21, "t").unwrap();
    assert_eq!(
        jaguar::shapeopt::loaded_turn(&q, &base, &h, 90.0, 12.0, q.hull.flare_deg).unwrap().drag_n,
        t0
    );
}

/// The small search with the race objective on two sprint events (fast in debug).
fn small_race() -> Params {
    let mut p = small(false);
    let canoe = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let mut rp = canoe.race.unwrap();
    rp.events
        .retain(|e| e.course == "sprint_200" && e.name.starts_with("Symposium") && e.weights_lb.len() == 2);
    for e in rp.events.iter_mut() {
        e.weight = 0.5;
    }
    p.race = Some(rp);
    p.objective.kind = jaguar::params::ObjectiveKind::Race;
    p.validate().unwrap();
    p
}

#[test]
fn race_objective_picks_the_fastest_passing_shape() {
    let p = small_race();
    let a = jaguar::run(&p, &|_| {}).unwrap();
    let b = jaguar::run(&p, &|_| {}).unwrap();
    assert_eq!(serde_json::to_string(&a).unwrap(), serde_json::to_string(&b).unwrap());
    let w = a.winner.as_ref().expect("winner");
    let t = w.candidate.race_s.expect("winner race time");
    // Every passing shape has a planned race time, and none is faster than the winner.
    for c in a.candidates.iter().filter(|c| c.passes) {
        assert!(c.race_s.expect("passing shape without race time") >= t - 1e-9);
    }
    // The winner is raced again on re-planned paths: never slower than on the held ones.
    let race = a.race.as_ref().expect("winner race");
    assert_eq!(race.events.len(), 2);
    assert!(
        race.weighted_time_s <= t + 1e-3 && race.weighted_time_s > 0.98 * t,
        "{} vs {t}",
        race.weighted_time_s
    );
    assert!((race.hull_lb - w.candidate.hull_weight_lb).abs() < 1e-12);
    // A weight search on the same shapes ranks by weight instead; race time is not computed.
    let mut pw = small_race();
    pw.objective.kind = jaguar::params::ObjectiveKind::Weight;
    let rw = jaguar::run(&pw, &|_| {}).unwrap();
    assert!(rw.race.is_none() && rw.candidates.iter().all(|c| c.race_s.is_none()));
}

/// Searched gunwale radius: every shape carries its radius inside the bounds, the winner's
/// sides stay within the lean limit, and the result exports with that gunwale.
#[test]
fn gunwale_radius_is_searched() {
    let mut p = small(false);
    let ss = p.shape_search.as_mut().unwrap();
    ss.gunwale_radius_in = Some([150.0, 600.0]);
    // The beam bound is the overall beam and the gunwale outline is wider than the waterline:
    // give the waterline room below the 26 in limit.
    ss.beam_in = [20.0, 26.0];
    // Buildable shapes need straight / hollow waterline ends: sample more than the default 12.
    ss.samples = 40;
    p.validate().unwrap();
    let r = jaguar::run(&p, &|_| {}).unwrap();
    for c in &r.candidates {
        let g = c.shape.as_ref().unwrap().gunwale_radius_in.expect("radius on every shape");
        assert!((150.0..=600.0).contains(&g), "{g}");
    }
    let w = r.winner.as_ref().expect("a buildable shape");
    assert!(w.candidate.beam_in <= 26.0 + 1e-9, "overall beam {}", w.candidate.beam_in);
    let s = w.candidate.shape.as_ref().unwrap();
    let hull = r.winner_hull.as_ref().unwrap();
    let ts = jaguar::hull::Topsides { gunwale_radius_in: s.gunwale_radius_in, ..p.hull.topsides() };
    let (_, why) = jaguar::hull::Grid::build_topsides(hull, w.candidate.depth_in, 161, ts);
    assert!(why.is_none(), "{why:?}");
    // The record keeps the input params; result readers find the radius on the winner's shape.
    assert!(r.params.hull.gunwale_radius_in.is_none());
    let v: serde_json::Value = serde_json::to_value(&r).unwrap();
    assert_eq!(jaguar::report::result_topsides(&v).gunwale_radius_in, s.gunwale_radius_in);
    let (_, spec) = jaguar::export::from_result(&v.to_string(), 81, 41).unwrap();
    assert_eq!(spec.gunwale_radius_in, s.gunwale_radius_in);
    // Fixed and searched radius together are ambiguous.
    p.hull.gunwale_radius_in = Some(300.0);
    assert!(p.validate().unwrap_err().to_string().contains("not both"));
}

/// hull.templates in a shape search: every analysed hull is the N-template loft, so the winner
/// hull (what gets built and exported) has exactly N stations, the exact sections of its
/// generated shape at cosine-spaced stations.
#[test]
fn templates_are_what_the_search_analyses() {
    let mut p = small(false);
    p.hull.templates = Some(9);
    p.validate().unwrap();
    let r = jaguar::run(&p, &|_| {}).unwrap();
    let h = r.winner_hull.as_ref().expect("winner");
    assert_eq!(h.x.len(), 9);
    let s = r.winner.as_ref().unwrap().candidate.shape.clone().unwrap();
    let smooth = jaguar::hullgen::generate(&s, 41, 21, "smooth").unwrap();
    assert_eq!(h.half_breadth, smooth.templated(9).half_breadth);
    p.hull.templates = Some(2);
    assert!(p.validate().is_err());
}
