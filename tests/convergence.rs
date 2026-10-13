//! Richardson extrapolation of the winner D/C over station refinement (PLAN B6).
use jaguar::hull::Hull;
use jaguar::optimize::{self, Series, extrapolate, richardson};
use jaguar::params::{ObjectiveKind, Params};
use std::path::Path;

/// f(h) = f0 + C h^p at h, h/2, h/4.
fn seq(f0: f64, c: f64, p: f64) -> [f64; 3] {
    [1.0, 0.5, 0.25].map(|h: f64| f0 + c * h.powf(p))
}

#[test]
fn synthetic_sequences_recover_order_and_limit() {
    for (p, c) in [(2.0, 0.05), (1.0, -0.02), (1.5, 0.01), (3.0, 0.2)] {
        match richardson(seq(0.8, c, p)) {
            Series::Extrapolated { value, order } => {
                assert!((order - p).abs() < 1e-9, "p {p}: {order}");
                assert!((value - 0.8).abs() < 1e-12, "p {p}: {value}");
            }
            s => panic!("p {p}: {s:?}"),
        }
    }
    assert_eq!(richardson([0.9, 0.9, 0.9]), Series::Converged(0.9));
    assert_eq!(richardson([0.9, 0.9 + 1e-9, 0.9]), Series::Converged(0.9));
    // Oscillating, diverging, order out of [0.5, 4], non-finite.
    assert_eq!(richardson([0.30, 0.31, 0.30]), Series::Irregular);
    assert_eq!(richardson([0.30, 0.31, 0.33]), Series::Irregular);
    assert_eq!(richardson(seq(0.8, 0.1, 6.0)), Series::Irregular);
    assert_eq!(richardson(seq(0.8, 0.1, 0.25)), Series::Irregular);
    assert_eq!(richardson([0.3, f64::NAN, 0.3]), Series::Irregular);
}

#[test]
fn extrapolate_takes_the_max_and_falls_back_on_a_relevant_irregular_series() {
    let st = [81, 161, 321];
    let t = |s: &str, f| (s.to_string(), f);
    let c = extrapolate(st, &[t("a/punching", [0.9, 0.9, 0.9]), t("a/bending", seq(0.5, -0.04, 2.0))]);
    assert_eq!((c.extrapolated, c.order, c.governing.as_deref()), (Some(0.9), None, Some("a/punching")));
    assert_eq!(c.dc, [0.9, 0.9, 0.9]);
    let c = extrapolate(st, &[t("a/punching", [0.5; 3]), t("a/bending", seq(0.7, -0.04, 2.0))]);
    assert!((c.extrapolated.unwrap() - 0.7).abs() < 1e-12 && (c.order.unwrap() - 2.0).abs() < 1e-9);
    // Irregular far below the governing value: ignored, but named.
    let c = extrapolate(st, &[t("a/punching", [0.9; 3]), t("a/shear", [0.30, 0.31, 0.30])]);
    assert_eq!(c.extrapolated, Some(0.9));
    assert!(c.note.unwrap().contains("a/shear"));
    // Irregular that could govern: no extrapolation, note says why.
    let c = extrapolate(st, &[t("a/punching", [0.5; 3]), t("b/bending", [0.60, 0.62, 0.61])]);
    assert_eq!((c.extrapolated, c.governing.as_deref()), (None, Some("b/bending")));
    assert!(c.note.unwrap().contains("verify_dc gate used"));
}

fn example() -> (Params, Hull) {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    p.objective.kind = ObjectiveKind::Weight;
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    (p, h)
}

/// examples/canoe.toml: punching governs and does not depend on stations, so
/// the extrapolation is the converged punching D/C and the gate uses it.
#[test]
fn example_extrapolation_is_sane() {
    let (p, h) = example();
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    let c = &w.convergence;
    assert_eq!(c.stations, [161, 321, 641]);
    assert_eq!(c.dc[1], w.verify_dc);
    let x = w.dc_extrapolated.expect("well behaved");
    assert!((x - w.verify_dc).abs() < 1e-6, "{x} vs {}", w.verify_dc);
    assert_eq!(c.governing.as_deref(), Some("2M/punching"));
    assert!(w.verified && x <= p.gates.dc_verify);
}

/// With punching relieved (big knee patch) bending governs: the series moves
/// with stations, the observed order is in range, and the extrapolated D/C is
/// within 1% of the 4n-3 value.
#[test]
fn bending_governed_extrapolation() {
    let (mut p, h) = example();
    p.material.knee_patch_in = 30.0;
    p.search.density_pcf = [52.0, 52.0, 1.0];
    p.search.thickness_in = [0.375, 0.375, 0.0625];
    p.search.depth_in = [12.0, 12.0, 1.0];
    p.stability.gm_min_in = 0.0;
    p.gates.freeboard_min_in = 0.0;
    p.flotation = None;
    let w = optimize::run(&p, &h).unwrap().winner.expect("winner");
    let c = &w.convergence;
    eprintln!("{c:?}");
    let gov = c.governing.as_deref().unwrap();
    assert!(!gov.ends_with("/punching"), "{gov}");
    match (w.dc_extrapolated, w.dc_order) {
        (Some(x), Some(o)) => {
            assert!((0.5..=4.0).contains(&o));
            assert!((x / c.dc[2] - 1.0).abs() < 0.01, "{x} vs {}", c.dc[2]);
            assert_eq!(w.verified, x.max(c.dc[2]) <= p.gates.dc_verify);
        }
        (None, _) => {
            assert!(c.note.is_some());
            assert_eq!(w.verified, w.verify_dc <= p.gates.dc_verify);
        }
        (Some(x), None) => assert!((x - c.dc[2]).abs() < 1e-6 * x),
    }
}
