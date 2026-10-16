//! The stands case end to end: examples/canoe.toml has `[stands]`, so `jaguar::run` checks
//! the winner on two stands once, after the search.

use jaguar::optimize;
use jaguar::params::Params;
use std::path::Path;

#[test]
fn winner_gets_the_stands_case_and_fails_loudly() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let r = jaguar::run(&p, &|_| {}).unwrap();
    let w = r.winner.as_ref().expect("winner");
    let s = w.stands.as_ref().and_then(|s| s.calc.as_ref()).expect("stands computed");
    // Reactions balance the self weight (asd: unfactored), stands symmetric about midship.
    assert_eq!(s.dead_factor, 1.0);
    assert!(
        (s.reactions_lb[0] + s.reactions_lb[1] - s.factored_weight_lb).abs() < 1e-6 * s.factored_weight_lb
    );
    assert!((s.spacing_in + 2.0 * s.x_stand_in - s.length_in).abs() < 1e-9);
    // A canoe is not uniform (light ends), so the optimum is near, not at, 0.207 L.
    assert!(s.x_over_l > 0.1 && s.x_over_l < 0.3, "{}", s.x_over_l);
    assert!(s.verdict.dc < p.gates.dc_verify, "stands D/C {}", s.verdict.dc);
    assert!(w.verified && w.verify_failure.is_none());
    assert_eq!(w.knee_split_sensitivity.len(), 3);
    let pu = w.candidate.verdict.as_ref().unwrap().checks.iter().find(|c| c.name == "punching").unwrap();
    // knee_split 0.6 in the example: its sensitivity entry is the winner's punching D/C.
    assert!((w.knee_split_sensitivity[1].dc - pu.dc).abs() < 1e-12);
    let text = jaguar::report::summary_lines(&r).join("\n");
    assert!(text.contains("stands (upright, 6 in pads"), "{text}");
    assert!(text.contains("punching D/C by knee_split"));

    // Over dc_verify: the winner is marked unverified and says which stands check failed.
    let mut tight = p.clone();
    tight.gates.dc_verify = 0.5 * s.verdict.dc;
    let mut r2 = r.clone();
    optimize::verify_stands(&tight, &mut r2);
    let w2 = r2.winner.unwrap();
    assert!(!w2.verified);
    let f = w2.verify_failure.unwrap();
    assert!(f.starts_with(&format!("stands: {} D/C", s.verdict.governing_check)), "{f}");
}
