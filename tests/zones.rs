//! Two-mix zoned walls ([zones]): the search, its winner and the knee-on-flange rule.
use jaguar::hull::{Grid, Hull};
use jaguar::optimize::{self, Progress};
use jaguar::params::{Params, Zones};
use jaguar::section::ZoneLayout;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};

/// examples/canoe.toml (weight objective) with a small zones grid around its knees
/// (auto seats put them near x = 78 and 160 in).
fn example() -> (Params, Hull) {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    p.objective.kind = jaguar::params::ObjectiveKind::Weight;
    p.search.depth_in = [12.0, 13.0, 1.0];
    p.zones = Some(Zones {
        dense_pcf: [56.0, 60.0, 2.0],
        start_in: [40.0, 60.0, 20.0],
        end_in: [180.0, 200.0, 20.0],
        bottom_in: [0.0, 2.0, 1.0],
        top_in: [0.0, 1.0, 1.0],
        max_checks: 400,
    });
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    (p, h)
}

#[test]
fn zoned_search_beats_or_matches_the_single_mix_wall() {
    let (p, h) = example();
    let mut single = p.clone();
    single.zones = None;
    let uniform = optimize::run(&single, &h).unwrap().winner.expect("single-mix winner");
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.expect("winner");
    assert!(w.candidate.hull_weight_lb <= uniform.candidate.hull_weight_lb);
    let z = w.candidate.zones.expect("a zoned wall is lighter on the example");
    assert!(z.dense_pcf > z.light_pcf && z.x0_in < z.x1_in && (z.bottom_in > 0.0 || z.top_in > 0.0));
    assert!(w.verified, "verify D/C {}", w.verify_dc);
    assert_eq!(w.candidate.density_pcf, z.light_pcf);
    assert!(w.dense_mix_provenance.is_some());
    // Zoned walls' worked calculation is not a single-mix one: it says so instead.
    assert!(w.calc.calc.is_none() && w.calc.error.as_deref().is_some_and(|e| e.contains("single-mix")));
    assert!(w.cases.iter().all(|c| c.error.is_none() && c.diagrams.x_in.len() == 2 * p.hull.stations - 1));
    let s = r.zones.expect("zone stats");
    assert!(s.checked >= 1 && s.checked <= 400 && !s.capped);
    assert!(s.walls >= s.heavier + s.punching + s.service + s.checked);
    // Every zoned candidate checked is lighter than the single-mix winner.
    let zc: Vec<_> = r.candidates.iter().filter(|c| c.zones.is_some()).collect();
    assert_eq!(zc.len(), s.checked);
    assert!(zc.iter().all(|c| c.hull_weight_lb < uniform.candidate.hull_weight_lb));
    assert!(w.search_space.iter().any(|a| a.name == "zones.bottom_in" && a.winner == z.bottom_in));
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
fn max_checks_caps_the_full_checks() {
    let (mut p, h) = example();
    // One full check: the search stops there, capped unless that wall passed.
    p.zones.as_mut().unwrap().max_checks = 1;
    let r = optimize::run(&p, &h).unwrap();
    let s = r.zones.unwrap();
    assert_eq!(s.checked, 1);
    let passed = r.candidates.iter().any(|c| c.zones.is_some() && c.passes);
    assert_eq!(s.capped, !passed);
}

#[test]
fn knees_bear_on_the_flange_only_inside_the_span_and_band() {
    let (p, h) = example();
    let g = Grid::build_topsides(&h, 12.0, p.hull.stations, p.hull.topsides()).0;
    let sec = jaguar::section::sections(&g, 0.375);
    let fit = optimize::strength(&p).unwrap();
    let seats = optimize::plan_seats(&p, &fit, &g, &sec, 0.375, 58.0).unwrap();
    let zl = ZoneLayout {
        light_pcf: 51.0,
        dense_pcf: 58.0,
        x0_in: 40.0,
        x1_in: 200.0,
        bottom_in: 2.0,
        top_in: 0.0,
    };
    assert!(optimize::knees_on_flange(&p, &g, 0.375, &zl, &seats));
    // A span that stops short of the aft knee, no bottom band, or no denser mix: the light mix punches.
    assert!(!optimize::knees_on_flange(&p, &g, 0.375, &ZoneLayout { x1_in: 120.0, ..zl }, &seats));
    assert!(!optimize::knees_on_flange(
        &p,
        &g,
        0.375,
        &ZoneLayout { bottom_in: 0.0, top_in: 1.0, ..zl },
        &seats
    ));
    assert!(!optimize::knees_on_flange(&p, &g, 0.375, &ZoneLayout { dense_pcf: 51.0, ..zl }, &seats));
    // A band too low to be as wide as the knee footprint.
    assert!(!optimize::knees_on_flange(&p, &g, 0.375, &ZoneLayout { bottom_in: 0.01, ..zl }, &seats));
}

#[test]
fn zones_input_is_strict() {
    let base = std::fs::read_to_string("examples/canoe.toml").unwrap();
    let dir = Path::new("examples");
    let ok = "\n[zones]\ndense_pcf = [56.0, 60.0, 2.0]\nstart_in = [40.0, 60.0, 20.0]\nend_in = [180.0, 200.0, 20.0]\nbottom_in = [0.0, 2.0, 1.0]\ntop_in = [0.0, 1.0, 1.0]\n";
    let p = Params::parse(&(base.clone() + ok), dir).unwrap();
    assert_eq!(p.zones.unwrap().max_checks, 400);
    for bad in [
        ok.replace("top_in = [0.0, 1.0, 1.0]", "top_in = [0.0, 1.0, 1.0]\ntypo = 1"),
        ok.replace("bottom_in = [0.0, 2.0, 1.0]", "bottom_in = [0.0, 0.0, 1.0]")
            .replace("top_in = [0.0, 1.0, 1.0]", "top_in = [0.0, 0.0, 1.0]"),
        ok.replace("end_in = [180.0, 200.0, 20.0]", "end_in = [10.0, 20.0, 5.0]"),
        ok.replace("dense_pcf = [56.0, 60.0, 2.0]", "dense_pcf = [56.0, 60.0, 0.0]"),
        ok.to_string() + "max_checks = 0\n",
    ] {
        assert!(Params::parse(&(base.clone() + &bad), dir).is_err(), "{bad}");
    }
}
