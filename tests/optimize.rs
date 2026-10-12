use jaguar::hull::Hull;
use jaguar::optimize::{self, Progress};
use jaguar::params::Params;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

fn example() -> (Params, Hull) {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let h = Hull::load(&p.hull.file).unwrap();
    (p, h)
}

#[test]
fn example_finds_verified_winner() {
    let (p, h) = example();
    let t0 = Instant::now();
    let r = optimize::run(&p, &h).unwrap();
    eprintln!("run: {} candidates in {:?}", r.candidates.len(), t0.elapsed());
    let w = r.winner.expect("winner");
    let c = &w.candidate;
    assert_eq!((c.thickness_in, c.density_pcf, c.depth_in), (0.375, 68.0, 12.0));
    assert!((c.hull_weight_lb / 123.52 - 1.0).abs() < 1e-3, "{}", c.hull_weight_lb);
    let dc = c.verdict.as_ref().unwrap().dc;
    assert!((dc - 0.907).abs() < 2e-3, "{dc}");
    assert!(w.verified, "verify_dc {}", w.verify_dc);
    // At the buildable floor (0.375): a real constraint, not a grid-edge warning.
    assert!(!w.on_grid_edge);
    assert!(r.candidates.iter().all(|c| c.thickness_in >= 0.375 - 1e-9));
}

#[test]
fn deterministic_json() {
    let (p, h) = example();
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
        Progress::Verifying { .. } => {}
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
