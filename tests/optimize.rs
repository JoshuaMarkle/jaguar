use jaguar::hull::Hull;
use jaguar::optimize::{self, Progress};
use jaguar::params::Params;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

fn example() -> (Params, Hull) {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    (p, h)
}

/// The example with the GM gate relaxed to 0 (upright stable). The real hull
/// has loaded GM_T 0.29 in at the winner and at most 0.81 in over the grid with
/// crew CG 12 in; examples/canoe.toml uses gm_min_in = 0.25 so it still passes.
fn example_gm0() -> (Params, Hull) {
    let (mut p, h) = example();
    p.stability.gm_min_in = 0.0;
    (p, h)
}

#[test]
fn example_gm_gate_2in_rejects_all() {
    let (mut p, h) = example();
    p.stability.gm_min_in = 2.0;
    let r = optimize::run(&p, &h).unwrap();
    assert!(r.winner.is_none());
    let mx = r.candidates.iter().filter_map(|c| c.gm_in).fold(f64::MIN, f64::max);
    assert!(mx > 0.0 && mx < 2.0, "{mx}");
}

#[test]
fn shipped_example_passes_its_own_gm_gate() {
    let (p, h) = example();
    let w = optimize::run(&p, &h).unwrap().winner.expect("examples/canoe.toml must have a winner");
    assert!(w.verified && w.candidate.gm_in.unwrap() >= p.stability.gm_min_in);
}

#[test]
fn example_finds_verified_winner() {
    let (p, h) = example_gm0();
    let t0 = Instant::now();
    let r = optimize::run(&p, &h).unwrap();
    eprintln!("run: {} candidates in {:?}", r.candidates.len(), t0.elapsed());
    let w = r.winner.expect("winner");
    let c = &w.candidate;
    // Strength from data/mix_tests.csv; knee punching (ACI 318-19 22.6.5.2,
    // demand on b0*d, two knees with a 60/40 split) governs and needs 58 pcf.
    assert_eq!((c.thickness_in, c.density_pcf, c.depth_in), (0.375, 58.0, 12.0));
    assert!((c.hull_weight_lb / 105.35 - 1.0).abs() < 1e-3, "{}", c.hull_weight_lb);
    let v = c.verdict.as_ref().unwrap();
    assert!((v.dc - 0.904).abs() < 2e-3, "{}", v.dc);
    assert_eq!(v.governing_check, "punching");
    assert!(w.verified, "verify_dc {}", w.verify_dc);
    assert!(
        (c.length_in - 240.12).abs() < 0.1 && (c.beam_in - 20.7).abs() < 0.2,
        "{} {}",
        c.length_in,
        c.beam_in
    );
    // At the buildable floor (0.375) and at the mix-data limit: real constraints,
    // not a too-narrow search range.
    assert!(!w.on_grid_edge);
    assert!(!w.at_mix_data_edge);
    assert!(r.candidates.iter().all(|c| c.density_pcf >= 51.0 - 1e-9));
    assert!(r.candidates.iter().all(|c| c.thickness_in >= 0.375 - 1e-9));
}

#[test]
fn deterministic_json() {
    let (p, h) = example_gm0();
    let a = serde_json::to_string(&optimize::run(&p, &h).unwrap()).unwrap();
    let b = serde_json::to_string(&optimize::run(&p, &h).unwrap()).unwrap();
    assert_eq!(a, b);
}

#[test]
fn progress_counts() {
    let (p, h) = example();
    let total = AtomicUsize::new(0);
    let n = AtomicUsize::new(0);
    let r = optimize::run_with_progress(&p, &h, &|e| match e {
        Progress::Started { total: t } => total.store(t, Ordering::SeqCst),
        Progress::Candidate { .. } => {
            n.fetch_add(1, Ordering::SeqCst);
        }
        Progress::Verifying { .. } | Progress::Shape { .. } => {}
    })
    .unwrap();
    assert_eq!(n.load(Ordering::SeqCst), total.load(Ordering::SeqCst));
    assert_eq!(r.candidates.len(), total.load(Ordering::SeqCst));
}

#[test]
fn heavy_crew_no_winner() {
    let (mut p, h) = example();
    for c in &mut p.load_cases {
        for w in &mut c.weights_lb {
            *w = 900.0;
        }
    }
    let r = optimize::run(&p, &h).unwrap();
    assert!(r.winner.is_none());
    for c in &r.candidates {
        assert!(c.error.is_some() && c.verdict.is_none() && !c.passes);
        assert!(c.hull_weight_lb.is_finite() && c.hull_weight_lb > 0.0);
    }
}

#[test]
fn gm_gate_100_in_no_winner() {
    let (mut p, h) = example();
    p.stability.gm_min_in = 100.0;
    let r = optimize::run(&p, &h).unwrap();
    assert!(r.winner.is_none());
    assert!(r.candidates.iter().any(|c| c.gm_in.is_some()));
}

#[test]
fn drag_objective_picks_min_drag_passing() {
    let (mut p, h) = example_gm0();
    p.objective.kind = jaguar::params::ObjectiveKind::Drag;
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.expect("winner").candidate;
    let d = w.drag_n.expect("drag");
    eprintln!(
        "drag winner: t {} rho {} depth {} drag {d:.2} N gm {:?}",
        w.thickness_in, w.density_pcf, w.depth_in, w.gm_in
    );
    assert!(w.passes);
    for c in r.candidates.iter().filter(|c| c.passes) {
        assert!(c.drag_n.unwrap() >= d);
    }
}

#[test]
fn example_winner_gm_and_drag() {
    let (mut p, h) = example_gm0();
    let r = optimize::run(&p, &h).unwrap();
    let c = r.winner.unwrap().candidate;
    assert!(c.drag_n.is_none(), "weight objective computes no drag");
    eprintln!("weight winner GM {:?} in", c.gm_in);
    assert!(c.gm_in.unwrap() >= p.stability.gm_min_in);
    p.objective.kind = jaguar::params::ObjectiveKind::Drag;
    let r = optimize::run(&p, &h).unwrap();
    eprintln!("drag-mode winner drag {:?} N", r.winner.unwrap().candidate.drag_n);
}

#[test]
fn winner_carries_diagrams_matching_bending_check() {
    let (p, h) = example();
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    let d = &w.diagrams;
    let n = d.x_in.len();
    assert_eq!(n, w.verify_stations);
    for v in [&d.shear_lb, &d.moment_lbin, &d.tension_psi, &d.area_in2, &d.inertia_in4, &d.waterline_in] {
        assert_eq!(v.len(), n);
        assert!(v.iter().all(|x| x.is_finite()));
    }
    let v = w.candidate.verdict.as_ref().unwrap();
    let bt = v.checks.iter().find(|c| c.name == "bending_tension").unwrap();
    let smax = d.tension_psi.iter().cloned().fold(0.0, f64::max);
    // verification grid is finer than the search grid: close, not identical
    assert!((smax - bt.demand).abs() < 0.05 * bt.demand, "{smax} vs {}", bt.demand);
    assert!((d.tension_allow_psi - bt.capacity).abs() < 1e-6 * bt.capacity);
    assert!(d.waterline_in.iter().all(|&z| z > 0.0 && z < w.candidate.depth_in));
}
