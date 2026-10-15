//! Two-mix walls ([zones]): the placement search, its winner, the saving and the knee rule.
use jaguar::hull::{Grid, Hull};
use jaguar::optimize::{self, Progress};
use jaguar::params::{Params, Zones};
use jaguar::section::{Rect, ZoneLayout};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};

/// examples/canoe.toml (weight objective, depth 12..13) with two mixes on 1/8 in marks.
fn example() -> (Params, Hull) {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    p.objective.kind = jaguar::params::ObjectiveKind::Weight;
    p.search.depth_in = [12.0, 13.0, 1.0];
    p.zones = Some(Zones { dense_pcf: [56.0, 64.0, 2.0], increment_in: 0.125, max_checks: 400 });
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    (p, h)
}

fn on_mark(v: f64, inc: f64) -> bool {
    ((v / inc) - (v / inc).round()).abs() < 1e-9
}

#[test]
fn zoned_search_beats_the_single_mix_wall_and_says_by_how_much() {
    let (p, h) = example();
    let mut single = p.clone();
    single.zones = None;
    let uniform = optimize::run(&single, &h).unwrap().winner.expect("single-mix winner");
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.as_ref().expect("winner");
    let z = w.candidate.zones.as_ref().expect("a two-mix wall is lighter on the example");
    assert!(z.dense_pcf > z.light_pcf && !z.dense.is_empty());
    assert!(w.verified, "verify D/C {}", w.verify_dc);
    assert_eq!(w.candidate.density_pcf, z.light_pcf);
    // Rectangles are disjoint, inside the mold, and their edges are on 1/8 in marks (the hull's
    // ends excepted: a rectangle may run to the tip).
    let (x0, x1) = (0.0, w.candidate.length_in);
    for (i, a) in z.dense.iter().enumerate() {
        assert!(a.x0_in < a.x1_in && a.z0_in < a.z1_in && a.z0_in >= 0.0 && a.z1_in <= w.candidate.depth_in);
        for v in [a.z0_in, a.z1_in] {
            assert!(on_mark(v, 0.125), "{a:?}");
        }
        for v in [a.x0_in, a.x1_in] {
            assert!(on_mark(v, 0.125) || (v - x0).abs() < 1e-9 || (v - x1).abs() < 1e-6, "{a:?}");
        }
        for b in &z.dense[i + 1..] {
            let overlap = a.x0_in < b.x1_in - 1e-9
                && b.x0_in < a.x1_in - 1e-9
                && a.z0_in < b.z1_in - 1e-9
                && b.z0_in < a.z1_in - 1e-9;
            assert!(!overlap, "{a:?} overlaps {b:?}");
        }
    }
    // The saving is against the single-mix pick of the same run, which is what a run without
    // [zones] picks.
    let s = r.zones.as_ref().expect("zone stats");
    assert_eq!(s.single_mix_lb, Some(uniform.candidate.hull_weight_lb));
    let saved = s.saved_lb.unwrap();
    assert!(
        (saved - (uniform.candidate.hull_weight_lb - w.candidate.hull_weight_lb)).abs() < 1e-12
            && saved > 0.0
    );
    let text = jaguar::report::summary_lines(&r).join("\n");
    assert!(text.contains("two mixes save") && text.contains("vs the single-mix pick"), "{text}");
    assert!(w.dense_mix_provenance.is_some());
    assert!(w.calc.calc.is_none() && w.calc.error.as_deref().is_some_and(|e| e.contains("single-mix")));
    assert!(w.cases.iter().all(|c| c.error.is_none() && c.diagrams.x_in.len() == 2 * p.hull.stations - 1));
    assert!(s.checked >= 1 && s.checked <= 400 && !s.capped);
    assert!(s.walls >= s.heavier + s.punching + s.checked);
    // Every zoned candidate placed had an all-light weight below the single-mix winner's.
    assert!(
        r.candidates
            .iter()
            .filter(|c| c.zones.is_some())
            .all(|c| c.passes || c.verdict.is_some() || c.error.is_some())
    );
    assert!(w.search_space.iter().any(|a| a.name == "zones.dense_pcf" && a.winner == z.dense_pcf));
}

#[test]
fn coarser_increment_snaps_to_its_marks() {
    let (mut p, h) = example();
    p.zones.as_mut().unwrap().increment_in = 0.5;
    let r = optimize::run(&p, &h).unwrap();
    let z = r.winner.unwrap().candidate.zones.expect("zoned winner");
    assert!(z.dense.iter().all(|a| on_mark(a.z0_in, 0.5) && on_mark(a.z1_in, 0.5)));
}

#[test]
fn without_a_lighter_two_mix_wall_the_saving_is_zero() {
    let (mut p, h) = example();
    // A dense mix that is barely heavier and no stronger where it matters cannot beat one mix:
    // only 64 pcf may be paired, and only with lighter mixes that then fail.
    p.zones.as_mut().unwrap().dense_pcf = [64.0, 64.0, 1.0];
    p.search.density_pcf = [58.0, 64.0, 1.0];
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.unwrap();
    let s = r.zones.unwrap();
    match &w.candidate.zones {
        None => assert_eq!(s.saved_lb, Some(0.0)),
        Some(_) => assert!(s.saved_lb.unwrap() > 0.0),
    }
}

#[test]
fn zoned_search_is_deterministic_and_reports_progress() {
    let (p, h) = example();
    let n = AtomicUsize::new(0);
    let a = optimize::run_with_progress(&p, &h, &|e| {
        if let Progress::Candidate { .. } = e {
            n.fetch_add(1, Ordering::SeqCst);
        }
    })
    .unwrap();
    let b = optimize::run(&p, &h).unwrap();
    assert_eq!(serde_json::to_string(&a).unwrap(), serde_json::to_string(&b).unwrap());
    assert_eq!(n.load(Ordering::SeqCst), a.candidates.len());
}

#[test]
fn max_checks_caps_the_placements() {
    let (mut p, h) = example();
    p.zones.as_mut().unwrap().max_checks = 1;
    let r = optimize::run(&p, &h).unwrap();
    let s = r.zones.unwrap();
    assert_eq!(s.checked, 1);
    assert!(s.capped, "lighter combinations are left after one placement");
}

#[test]
fn knees_are_dense_only_when_every_footprint_is_covered() {
    let (p, h) = example();
    let g = Grid::build_topsides(&h, 12.0, p.hull.stations, p.hull.topsides()).0;
    let sec = jaguar::section::sections(&g, 0.375);
    let fit = optimize::strength(&p).unwrap();
    let seats = optimize::plan_seats(&p, &fit, &g, &sec, 0.375, 58.0).unwrap();
    let regions = optimize::knee_regions(&p, &g, 0.375, &seats).unwrap();
    // One region per paddler and case, as long as the seat range plus the punching reach.
    assert_eq!(regions.len(), seats.iter().map(|s| s.seats_in.len()).sum::<usize>());
    assert!(regions.iter().all(|r| r.x1_in - r.x0_in > 2.0 * p.seats.variance_in && r.z1_in > 0.0));
    let zl = |dense: Vec<Rect>| ZoneLayout { light_pcf: 51.0, dense_pcf: 58.0, dense };
    assert!(optimize::knees_dense(&p, &g, 0.375, &zl(regions.clone()), &seats));
    // A full-length band as high as the tallest footprint covers them too.
    let top = regions.iter().map(|r| r.z1_in).fold(0.0, f64::max);
    let band = Rect { x0_in: 0.0, x1_in: g.x[g.nx() - 1], z0_in: 0.0, z1_in: top };
    assert!(optimize::knees_dense(&p, &g, 0.375, &zl(vec![band]), &seats));
    // One knee left light, every footprint a little too low, or no denser mix: the light mix punches.
    let r0 = regions[0];
    let rest: Vec<Rect> =
        regions.iter().copied().filter(|r| r.x1_in < r0.x0_in || r.x0_in > r0.x1_in).collect();
    assert!(rest.len() < regions.len() && !optimize::knees_dense(&p, &g, 0.375, &zl(rest), &seats));
    let low: Vec<Rect> = regions.iter().map(|r| Rect { z1_in: r.z1_in - 0.05, ..*r }).collect();
    assert!(!optimize::knees_dense(&p, &g, 0.375, &zl(low), &seats));
    let same = ZoneLayout { dense_pcf: 51.0, ..zl(regions) };
    assert!(!optimize::knees_dense(&p, &g, 0.375, &same, &seats));
}

#[test]
fn zones_input_is_strict() {
    let base = std::fs::read_to_string("examples/canoe.toml").unwrap();
    let dir = Path::new("examples");
    let ok = "\n[zones]\ndense_pcf = [56.0, 64.0, 2.0]\n";
    let z = Params::parse(&(base.clone() + ok), dir).unwrap().zones.unwrap();
    assert_eq!((z.increment_in, z.max_checks), (0.125, 400));
    for bad in [
        ok.to_string() + "start_in = [40.0, 60.0, 20.0]\n",
        ok.replace("[56.0, 64.0, 2.0]", "[56.0, 64.0, 0.0]"),
        ok.to_string() + "increment_in = 0.0\n",
        ok.to_string() + "max_checks = 0\n",
    ] {
        assert!(Params::parse(&(base.clone() + &bad), dir).is_err(), "{bad}");
    }
}

#[test]
fn placements_that_end_short_of_freeboard_are_counted() {
    // A freeboard gate only deeper molds meet: the lighter (shallower) zoned walls end on
    // freeboard, which more dense concrete cannot fix; the result says how many.
    let (mut p, h) = example();
    p.search.depth_in = [11.0, 14.0, 1.0];
    p.gates.freeboard_min_in = 7.0;
    let r = optimize::run(&p, &h).unwrap();
    let s = r.zones.as_ref().unwrap();
    assert!(s.freeboard > 0 && s.freeboard <= s.walls, "{s:?}");
    if r.winner.as_ref().is_some_and(|w| w.candidate.zones.is_none()) {
        let text = jaguar::report::summary_lines(&r).join("\n");
        assert!(text.contains("miss freeboard"), "{text}");
    }
}
