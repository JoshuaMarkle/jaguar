//! Pause and end a running search (`optimize::RunControl`, the web UI's Pause / End).

use jaguar::optimize::{Progress, RunControl, RunEnded};
use jaguar::params::Params;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

#[test]
fn ending_a_shape_search_keeps_the_best_shape_so_far() {
    let p = Params::load(Path::new("examples/shape_opt.toml")).unwrap();
    let ctl = RunControl::default();
    let r = jaguar::run_controlled(
        &p,
        &|e| {
            if let Progress::Candidate { done, .. } = e
                && done >= 3
            {
                ctl.stop();
            }
        },
        &ctl,
    )
    .unwrap();
    let ss = p.shape_search.as_ref().unwrap();
    // The shapes in progress finish; nothing new starts, no refine round runs.
    assert!(r.candidates.len() >= 3 && r.candidates.len() < ss.samples, "{}", r.candidates.len());
    let note = r.ended_early.as_deref().expect("ended_early");
    assert!(note.contains(&format!("after {} shapes", r.candidates.len())), "{note}");
    // The winner is the best checked shape, verified as usual (never a failing one).
    if let Some(w) = &r.winner {
        assert!(w.candidate.passes);
        assert!(w.candidate.shape.is_some());
    }
    let text = jaguar::report::summary_lines(&r).join("\n");
    assert!(r.winner.is_none() || text.contains("ENDED EARLY"), "{text}");
    let json = serde_json::to_string(&r).unwrap();
    assert!(json.contains("\"ended_early\""));
}

#[test]
fn ending_a_fixed_hull_search_is_no_answer() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let ctl = RunControl::default();
    ctl.stop();
    let e = jaguar::run_controlled(&p, &|_| {}, &ctl).unwrap_err();
    assert!(e.downcast_ref::<RunEnded>().is_some(), "{e:#}");
}

#[test]
fn a_full_run_has_no_ended_early() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let r = jaguar::run(&p, &|_| {}).unwrap();
    assert!(r.ended_early.is_none());
    assert!(!serde_json::to_string(&r).unwrap().contains("ended_early"));
}

#[test]
fn a_paused_search_waits_and_resumes() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let ctl = RunControl::default();
    ctl.set_paused(true);
    let done = AtomicUsize::new(0);
    std::thread::scope(|sc| {
        let run = sc.spawn(|| {
            jaguar::run_controlled(
                &p,
                &|e| {
                    if let Progress::Candidate { .. } = e {
                        done.fetch_add(1, Ordering::SeqCst);
                    }
                },
                &ctl,
            )
        });
        std::thread::sleep(Duration::from_millis(500));
        assert_eq!(done.load(Ordering::SeqCst), 0, "paused before the first candidate");
        assert!(!run.is_finished());
        ctl.set_paused(false);
        let r = run.join().unwrap().unwrap();
        assert!(r.winner.is_some());
        assert_eq!(done.load(Ordering::SeqCst), r.candidates.len());
    });
}

#[test]
fn a_pause_holds_a_shape_search_after_the_shapes_in_progress() {
    let p = Params::load(Path::new("examples/shape_opt.toml")).unwrap();
    let ctl = RunControl::default();
    let done = AtomicUsize::new(0);
    std::thread::scope(|sc| {
        let run = sc.spawn(|| {
            jaguar::run_controlled(
                &p,
                &|e| {
                    if let Progress::Candidate { done: k, .. } = e {
                        done.fetch_max(k, Ordering::SeqCst);
                        if k == 3 {
                            ctl.set_paused(true);
                        }
                    }
                },
                &ctl,
            )
        });
        // Wait for the pause (debug builds are slow), then give in-progress shapes time to land.
        let t0 = std::time::Instant::now();
        while !ctl.paused() {
            assert!(t0.elapsed() < Duration::from_secs(600), "never reached 3 shapes");
            std::thread::sleep(Duration::from_millis(50));
        }
        std::thread::sleep(Duration::from_secs(1));
        // At most the shapes already started when the pause came (one per worker thread).
        let bound = 3 + rayon::current_num_threads();
        assert!(done.load(Ordering::SeqCst) <= bound, "shapes kept starting while paused");
        ctl.stop();
        let r = run.join().unwrap().unwrap();
        assert!(r.candidates.len() <= bound, "{} shapes after a pause at 3", r.candidates.len());
        assert_eq!(r.candidates.len(), done.load(Ordering::SeqCst));
    });
}
