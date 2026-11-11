//! Race-day seating on the example winner (src/seating.rs): the same checks as the run at the run's
//! seats, safe bands, seat-rule warnings, best seats, and refusals.

use jaguar::seating::{self, Canoe, Request, Rules, assess, best, canoe_from_result};

/// The examples/canoe.toml winner as a result.json (run once for all tests).
fn example() -> &'static str {
    static RESULT: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    RESULT.get_or_init(|| {
        let p = jaguar::params::Params::load(std::path::Path::new("examples/canoe.toml")).unwrap();
        serde_json::to_string(&jaguar::run(&p, &|_| {}).unwrap()).unwrap()
    })
}

fn canoe() -> Canoe {
    canoe_from_result(example()).unwrap()
}

fn req(c: &Canoe, case: usize, bands: bool) -> Request {
    let k = &c.cases[case];
    Request {
        weights_lb: k.weights_lb.clone(),
        seats_in: k.seats_in.clone(),
        rules: None,
        positions: None,
        bands,
        plots: false,
    }
}

#[test]
fn run_seats_reproduce_the_runs_case_verdicts() {
    let c = canoe();
    let v: serde_json::Value = serde_json::from_str(example()).unwrap();
    assert!(!c.cases.is_empty());
    for (i, k) in v["winner"]["cases"].as_array().unwrap().iter().enumerate() {
        let a = assess(&c, &req(&c, i, false)).unwrap();
        // Same grid (2n-1) and checks as the run, at the nominal seats only: the run's case
        // verdict is the worst over the nominal seats and every +/- variance corner, so it bounds
        // ours, and its nominal service float is ours exactly.
        let run_dc = k["verdict"]["dc"].as_f64().unwrap();
        assert!(a.dc <= run_dc + 1e-12, "case {i}: seating {} above the run's worst sample {run_dc}", a.dc);
        assert!((a.draft_in - k["draft_in"].as_f64().unwrap()).abs() < 1e-9, "case {i} draft");
        assert!((a.trim_in - k["trim_in"].as_f64().unwrap()).abs() < 1e-9, "case {i} trim");
        assert!(a.gm_in >= k["gm_in"].as_f64().unwrap() - 1e-9, "case {i} GM");
        assert!(a.safe && a.reasons.is_empty(), "the winner is safe at its seats: {:?}", a.reasons);
        assert!(a.warnings.is_empty(), "run seats keep the run's rules: {:?}", a.warnings);
        assert_eq!(a.waterline_in.len(), c.grid.nx());
        assert!(a.bands.is_none());
    }
}

#[test]
fn bands_contain_the_seat_and_stop_where_a_check_fails() {
    let c = canoe();
    let a = assess(&c, &req(&c, 0, true)).unwrap();
    let bands = a.bands.unwrap();
    let l = c.length_in();
    for (i, b) in bands.iter().enumerate() {
        let [lo, hi] = b.range_in.expect("a safe seat has a band");
        let x = c.cases[0].seats_in[i];
        assert!(lo <= x && x <= hi && lo >= 0.0 && hi <= l, "paddler {i}: {lo}..{hi} around {x}");
        // One step past an inner end the crew is not safe (and the end says why).
        for (end, why, dir) in [(lo, &b.fore_limit, -1.0), (hi, &b.aft_limit, 1.0)] {
            let why = why.as_deref().unwrap();
            if why == "hull end" {
                continue;
            }
            let mut s = c.cases[0].seats_in.clone();
            s[i] = end + dir * seating::BAND_STEP_IN;
            let past = Request { seats_in: s.clone(), ..req(&c, 0, false) };
            match assess(&c, &past) {
                Ok(p) => assert_ne!(p.state, seating::State::Safe, "paddler {i} at {}: {why} but safe", s[i]),
                Err(e) => panic!("paddler {i} at {}: {e:#}", s[i]),
            }
            s[i] = end;
            let at = assess(&c, &Request { seats_in: s, ..req(&c, 0, false) }).unwrap();
            assert_eq!(at.state, seating::State::Safe);
        }
    }
}

#[test]
fn rule_warnings_do_not_change_the_checks() {
    let c = canoe();
    let base = assess(&c, &req(&c, 0, false)).unwrap();
    let tight = Rules { spacing_min_in: 400.0, bow_clear_in: 200.0, stern_clear_in: 200.0 };
    let a = assess(&c, &Request { rules: Some(tight), ..req(&c, 0, false) }).unwrap();
    assert!(a.warnings.iter().any(|w| w.contains("bow")), "{:?}", a.warnings);
    assert!(a.warnings.iter().any(|w| w.contains("stern")), "{:?}", a.warnings);
    assert!(a.warnings.iter().any(|w| w.contains("apart")), "{:?}", a.warnings);
    assert_eq!(a.dc, base.dc);
}

#[test]
fn best_seats_are_safe_and_respect_rules() {
    let c = canoe();
    for (i, k) in c.cases.iter().enumerate() {
        let s = best(&c, &k.weights_lb, None, None).unwrap();
        assert!(assess(&c, &Request { seats_in: s.seats_in.clone(), ..req(&c, i, false) }).unwrap().safe);
    }
    let r = Rules { spacing_min_in: 40.0, bow_clear_in: 60.0, stern_clear_in: 60.0 };
    let s = best(&c, &c.cases[0].weights_lb, None, Some(r)).unwrap();
    let l = c.length_in();
    let mut x = s.seats_in.clone();
    x.sort_by(f64::total_cmp);
    assert!(x[0] >= 60.0 - 1e-9 && x[x.len() - 1] <= l - 60.0 + 1e-9 && x[1] - x[0] >= 40.0 - 1e-9, "{x:?}");
}

#[test]
fn bad_crews_and_results_are_refused() {
    let c = canoe();
    let bad = |weights_lb: Vec<f64>, seats_in: Vec<f64>| {
        assess(
            &c,
            &Request { weights_lb, seats_in, rules: None, positions: None, bands: false, plots: false },
        )
        .unwrap_err()
        .to_string()
    };
    assert!(bad(vec![], vec![]).contains("at least one"));
    assert!(bad(vec![150.0, 150.0], vec![60.0]).contains("1 seats for 2"));
    assert!(bad(vec![-5.0], vec![60.0]).contains("weight"));
    assert!(bad(vec![150.0], vec![-1.0]).contains("off the"));
    assert!(bad(vec![150.0], vec![f64::NAN]).contains("off the"));
    assert!(canoe_from_result(r#"{"format":"jaguar-result/1","winner":null}"#).is_err());
    assert!(canoe_from_result(r#"{"format":"other"}"#).is_err());
    // A paddler at the very bow tip: an honest "not safe" (or an error), never a pass.
    let mut s = c.cases[0].seats_in.clone();
    s[0] = 0.0;
    if let Ok(a) = assess(&c, &Request { seats_in: s, ..req(&c, 0, false) }) {
        assert!(!a.safe || !a.warnings.is_empty());
    }
}

#[test]
fn summary_has_profile_rules_and_cases() {
    let c = canoe();
    let s = seating::summary(&c);
    assert_eq!(s.x_in.len(), s.keel_in.len());
    assert!(s.keel_in.iter().all(|&z| (0.0..=s.depth_in).contains(&z)));
    // A smooth keel line, not stairs of the waterline spacing: neighbouring stations (0.66 in apart)
    // differ by much less than a waterline step, except up the stem.
    let mid = s.keel_in.len() / 2;
    assert!(s.keel_in[mid] < 1e-9, "flat at midships");
    let steps: Vec<f64> = s.keel_in.windows(2).map(|w| (w[1] - w[0]).abs()).collect();
    let n = steps.len();
    assert!(steps[n / 10..n - n / 10].iter().all(|&d| d < 0.2), "{:?}", &steps[n / 10..n - n / 10]);
    assert_eq!(s.cases.len(), c.params.load_cases.len());
    assert_eq!(s.rules.bow_clear_in, c.params.seats.bow_clear_in);
    assert!(s.fails.is_none());
    // examples/canoe.toml is ASD with factor_of_safety 1.5: D/C 1 leaves FS x dynamic factor.
    let f = &s.safety;
    assert_eq!(f.method, "asd");
    assert!((f.strength_factor - 1.0 / 1.5).abs() < 1e-12 && f.hull_factor == 1.0);
    assert!((f.crew_factor - c.params.material.dynamic_load_factor).abs() < 1e-12);
    let mut p = c.params.clone();
    p.material.method = jaguar::params::DesignMethod::Lrfd;
    let l = seating::safety(&p);
    assert_eq!(
        (l.strength_factor, l.hull_factor, l.hull_only_factor),
        (0.60, p.material.lrfd_dead, Some(p.material.lrfd_dead_only))
    );
    assert!(l.factor_of_safety.is_none());
}

#[test]
fn pinned_paddlers_keep_their_seat() {
    let c = canoe();
    let w = vec![150.0, 210.0, 170.0];
    let order = |s: &[f64]| {
        let mut o: Vec<usize> = (0..s.len()).collect();
        o.sort_by(|&a, &b| s[a].total_cmp(&s[b]));
        o
    };
    // The steerer (paddler 1, the lightest) pinned in the stern, and paddler 3 pinned at the bow.
    for pins in [vec![3, 0, 0], vec![3, 0, 1], vec![0, 2, 0]] {
        let s = best(&c, &w, Some(&pins), None).unwrap();
        let o = order(&s.seats_in);
        for (i, &seat) in pins.iter().enumerate() {
            if seat > 0 {
                assert_eq!(o[seat - 1], i, "pins {pins:?}: bow to stern {o:?}");
            }
        }
        let r = Request {
            weights_lb: w.clone(),
            seats_in: s.seats_in.clone(),
            rules: None,
            positions: Some(pins),
            bands: false,
            plots: false,
        };
        assert!(assess(&c, &r).unwrap().warnings.is_empty());
    }
    // Knees that put a pinned paddler elsewhere: a warning naming the seats, the same checks.
    let s = best(&c, &w, None, None).unwrap();
    let o = order(&s.seats_in);
    let mut pins = vec![0; 3];
    pins[o[0]] = 3; // the bow paddler "should" steer
    let r = Request {
        weights_lb: w.clone(),
        seats_in: s.seats_in.clone(),
        rules: None,
        positions: Some(pins),
        bands: false,
        plots: false,
    };
    let a = assess(&c, &r).unwrap();
    assert!(
        a.warnings.iter().any(|m| m.contains("pinned to seat 3 (stern) but kneels in seat 1 (bow)")),
        "{:?}",
        a.warnings
    );
    // Bad pins are refused, and so is a search over too many free paddlers.
    for (pins, why) in
        [(vec![3, 3, 0], "two paddlers"), (vec![4, 0, 0], "seats are 1"), (vec![1, 0], "2 seat pins for 3")]
    {
        let e = best(&c, &w, Some(&pins), None).unwrap_err().to_string();
        assert!(e.contains(why), "{pins:?}: {e}");
    }
    let five = vec![150.0; 5];
    assert!(best(&c, &five, None, None).unwrap_err().to_string().contains("at most 4 free"));
}

#[test]
fn freeboard_minimum_is_a_staging_rule_and_gm_never_fails() {
    let mut c = canoe();
    let base = assess(&c, &req(&c, 0, false)).unwrap();
    assert!(base.safe);
    // A design margin on freeboard and an impossible GM gate change nothing here.
    c.params.gates.freeboard_margin_in = 100.0;
    c.params.stability.gm_min_in = 100.0;
    let a = assess(&c, &req(&c, 0, true)).unwrap();
    assert!(a.safe && a.reasons.is_empty(), "{:?}", a.reasons);
    assert!(a.bands.unwrap().iter().all(|b| b.range_in.is_some()));
    assert_eq!(seating::summary(&c).freeboard_required_in, c.params.gates.freeboard_min_in);
    best(&c, &c.cases[0].weights_lb.clone(), None, None).unwrap();
    // The RFP minimum above the actual freeboard: not fit to stage there (`safe`, what the roster
    // and best seating use), but structurally still safe: the judges check freeboard at staging.
    c.params.gates.freeboard_min_in = base.freeboard_in + 0.01;
    let a = assess(&c, &req(&c, 0, true)).unwrap();
    assert!(!a.safe && !a.freeboard_ok, "{a:?}");
    assert_eq!(a.state, seating::State::Safe);
    assert!(a.reasons.is_empty(), "{:?}", a.reasons);
    let b = a.bands.unwrap();
    assert!(b.iter().all(|b| b.range_in.is_some()), "freeboard does not shrink the bands");
}

#[test]
fn past_the_code_limit_it_reports_the_safety_factor_left_until_none_is() {
    let mut c = canoe();
    // examples/canoe.toml is ASD, factor of safety 1.5: a linear check's unfactored D/C is its code
    // D/C / 1.5; biaxial (an interaction, not linear in strength) just drops.
    let a = assess(&c, &req(&c, 0, true)).unwrap();
    assert_eq!(a.state, seating::State::Safe);
    for k in &a.checks {
        if k.name == "biaxial" {
            assert!(k.dc_true < k.dc && k.dc_true > 0.0);
        } else {
            assert!((k.dc_true * 1.5 - k.dc).abs() < 1e-9, "{}: {} vs {}", k.name, k.dc_true, k.dc);
        }
    }
    let worst = a.checks.iter().map(|k| k.dc_true).fold(0.0, f64::max);
    assert!((a.safety_factor - 1.0 / worst).abs() < 1e-12 && a.safety_factor > 1.5);
    // Every band: the full-margin range inside the holding range; just past a holding end it
    // fails, at the end it holds; between the two ranges the margin is reduced.
    let at = |i: usize, x: f64| {
        let mut s = c.cases[0].seats_in.clone();
        s[i] = x;
        // No float at all (the canoe trims past what JAGUAR solves) fails too, as an error.
        assess(&c, &Request { seats_in: s, ..req(&c, 0, false) }).map_or(seating::State::Fails, |a| a.state)
    };
    let mut saw_reduced = false;
    for (i, b) in a.bands.unwrap().iter().enumerate() {
        let [lo, hi] = b.range_in.unwrap();
        let [hlo, hhi] = b.hold_in.unwrap();
        assert!(hlo <= lo && hi <= hhi, "paddler {i}: {hlo}..{hhi} around {lo}..{hi}");
        for (end, why, dir) in
            [(hlo, b.fore_fail.as_deref().unwrap(), -1.0), (hhi, b.aft_fail.as_deref().unwrap(), 1.0)]
        {
            assert_ne!(at(i, end), seating::State::Fails, "paddler {i} at {end}");
            if why != "hull end" {
                assert_eq!(
                    at(i, end + dir * seating::BAND_STEP_IN),
                    seating::State::Fails,
                    "paddler {i}: {why}"
                );
            }
        }
        if lo - seating::BAND_STEP_IN >= hlo {
            assert_eq!(at(i, lo - seating::BAND_STEP_IN), seating::State::Reduced, "paddler {i}");
            saw_reduced = true;
        }
    }
    assert!(saw_reduced, "the example's bow paddler has a reduced-margin zone");
    // The unfactored check does not depend on the design method; the code check does.
    c.params.material.method = jaguar::params::DesignMethod::Lrfd;
    let l = assess(&c, &req(&c, 0, false)).unwrap();
    for (x, y) in a.checks.iter().zip(&l.checks) {
        assert!((x.dc_true - y.dc_true).abs() < 1e-9, "{}", x.name);
    }
    assert!(l.checks.iter().zip(&a.checks).any(|(x, y)| (x.dc - y.dc).abs() > 1e-6));
}

#[test]
fn a_crew_far_forward_floats_bow_down_past_half_the_depth() {
    // Both paddlers sliding into the bow: past some point the trim exceeds +/- half the depth
    // (the first bracket), which used to be "not bracketed"; now it floats, nose down, and the
    // screen judges it (swamped once the bow freeboard goes under 0).
    let c = canoe();
    let deep = (0..40).map(|k| 60.0 - 1.5 * k as f64).find_map(|x| {
        let r = Request { seats_in: vec![x, x + 30.0], ..req(&c, 0, false) };
        assess(&c, &r).ok().filter(|a| a.trim_in < -0.5 * c.depth_in)
    });
    let a = deep.expect("some bow position floats past half the depth of trim");
    assert!(a.freeboard_in < c.params.gates.freeboard_min_in, "{}", a.freeboard_in);
    assert_eq!(a.state == seating::State::Fails, a.freeboard_in <= 0.0 || a.safety_factor <= 1.0);
}

#[test]
fn plots_follow_the_hull_and_agree_with_the_checks() {
    let mut c = canoe();
    let a = assess(&c, &Request { plots: true, ..req(&c, 0, false) }).unwrap();
    let p = a.plots.expect("asked for plots");
    let n = c.grid.nx();
    for v in [&p.x_in, &p.shear_lb, &p.moment_lbin, &p.shear_real_lb, &p.moment_real_lbin, &p.dc, &p.dc_real]
    {
        assert_eq!(v.len(), n);
    }
    // Per-station D/C never exceeds the case's (which adds knee punching, a local check).
    let top = p.dc.iter().cloned().fold(0.0, f64::max);
    assert!(top > 0.0 && top <= a.dc + 1e-9, "{top} vs {}", a.dc);
    assert!(p.dc_real.iter().zip(&p.dc).all(|(r, d)| *r <= *d + 1e-9));
    // ASD factors strength, not loads: the code loads are the real loads.
    assert_eq!(p.shear_lb, p.shear_real_lb);
    // LRFD factors the loads: the code moment is the bigger one.
    c.params.material.method = jaguar::params::DesignMethod::Lrfd;
    let l = assess(&c, &Request { plots: true, ..req(&c, 0, false) }).unwrap().plots.unwrap();
    let peak = |v: &[f64]| v.iter().map(|m| m.abs()).fold(0.0, f64::max);
    assert!(peak(&l.moment_lbin) > peak(&l.moment_real_lbin));
    assert!(assess(&c, &req(&c, 0, false)).unwrap().plots.is_none());
}
