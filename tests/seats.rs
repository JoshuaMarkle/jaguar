//! Auto seats and the seat-variance envelope (src/seats.rs, PLAN B2).
use jaguar::hull::{Grid, Hull};
use jaguar::mix::StrengthFit;
use jaguar::optimize::{self, eval_case, eval_envelope, plan_seats};
use jaguar::params::{ObjectiveKind, Params};
use jaguar::seats::{self, SeatSource};
use jaguar::section::{Sections, sections};
use std::path::Path;

/// examples/canoe.toml (weight objective) and the off1 winner wall 0.375 in / 58 pcf / 12 in.
fn setup() -> (Params, Hull, StrengthFit, Grid, Sections) {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    p.objective.kind = ObjectiveKind::Weight;
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    let fit = optimize::strength(&p).unwrap();
    let g = Grid::build_flared(&h, 12.0, p.hull.stations, p.hull.flare_deg);
    let s = sections(&g, 0.375);
    (p, h, fit, g, s)
}

fn exact(p: &Params, fit: &StrengthFit, g: &Grid, s: &Sections, case: usize, seats: &[f64]) -> f64 {
    let e = eval_case(p, fit, g, s, 58.0, &p.load_cases[case], seats).unwrap();
    seats::score(p, &e.verdict, e.service.gm_in)
}

#[test]
fn auto_seats_never_worse_than_even_and_respect_constraints() {
    let (mut p, _, fit, g, s) = setup();
    // A lopsided crew: even spacing trims the canoe, auto should do strictly better.
    p.load_cases[1].weights_lb = vec![120.0, 250.0];
    let plan = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    let sp = &p.seats;
    let l = g.x[g.nx() - 1];
    for (i, cs) in plan.iter().enumerate() {
        assert_eq!(cs.source, SeatSource::Auto);
        let even = seats::even(cs.seats_in.len(), &g);
        let (a, e) = (exact(&p, &fit, &g, &s, i, &cs.seats_in), exact(&p, &fit, &g, &s, i, &even));
        eprintln!("case {i}: auto {:?} score {a:.4}, even {even:?} score {e:.4}", cs.seats_in);
        assert!(a <= e, "case {i}: auto {a} > even {e}");
        let mut x: Vec<f64> = cs.crew_order.iter().map(|&j| cs.seats_in[j]).collect();
        assert!(x.windows(2).all(|w| w[1] - w[0] >= sp.spacing_min_in - 1e-9), "{x:?}");
        assert!(x[0] >= sp.bow_clear_in - 1e-9 && x[x.len() - 1] <= l - sp.stern_clear_in + 1e-9);
        x.sort_by(f64::total_cmp);
        assert_eq!(x, cs.crew_order.iter().map(|&j| cs.seats_in[j]).collect::<Vec<_>>());
    }
    let (a, e) =
        (exact(&p, &fit, &g, &s, 1, &plan[1].seats_in), exact(&p, &fit, &g, &s, 1, &seats::even(2, &g)));
    assert!(a < 0.98 * e, "lopsided crew: auto {a} not clearly better than even {e}");
}

#[test]
fn given_seats_override_auto() {
    let (mut p, _, fit, g, s) = setup();
    p.load_cases[0].seats_in = Some(vec![90.0, 170.0]);
    let plan = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    assert_eq!(plan[0].seats_in, vec![90.0, 170.0]);
    assert_eq!(plan[0].source, SeatSource::Given);
    assert_eq!(plan[1].source, SeatSource::Auto);
    p.seats.auto = false;
    let plan = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    assert_eq!((plan[1].source, plan[1].seats_in.clone()), (SeatSource::Even, seats::even(2, &g)));
}

#[test]
fn variance_is_never_less_conservative() {
    let (mut p, h, fit, g, s) = setup();
    for case in &p.load_cases {
        let x = [80.0, 160.0];
        p.seats.variance_in = 0.0;
        let a = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
        p.seats.variance_in = 6.0;
        let b = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
        assert!(b.verdict.dc >= a.verdict.dc && b.gm_in <= a.gm_in);
        // Freeboard is gated at the marked seats (gates.freeboard_seat_shift_in = 0): the
        // strength variance does not move it, but its range shows the cost of shifted knees.
        assert_eq!(b.verdict.freeboard_in, a.verdict.freeboard_in);
        assert!(
            b.freeboard.worst.freeboard_in < a.freeboard.worst.freeboard_in,
            "shifting knees must cost freeboard"
        );
        let bend = |v: &jaguar::check::Verdict| v.checks[0].dc;
        assert!(bend(&b.verdict) >= bend(&a.verdict));
    }
    // Whole run: the variance winner is never lighter.
    p.seats.variance_in = 0.0;
    let w0 = optimize::run(&p, &h).unwrap().winner.unwrap();
    p.seats.variance_in = 6.0;
    let w6 = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert!(w6.candidate.hull_weight_lb >= w0.candidate.hull_weight_lb);
    for k in &w6.cases {
        assert_eq!(k.seat_variance_in, 6.0);
        assert!(
            k.freeboard_in.unwrap() >= p.gates.freeboard_required_in(),
            "{}: gate covers every case",
            k.name
        );
    }
}

#[test]
fn freeboard_range_spans_the_seat_samples() {
    let (mut p, _, fit, g, s) = setup();
    p.seats.variance_in = 6.0;
    // Lopsided crew so moving every knee one way trims the canoe.
    let case = &p.load_cases[1];
    let x = [80.0, 160.0];
    let env = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
    let f = &env.freeboard;
    // Gated at the marked seats (shift 0): gate = expected; best >= expected > worst.
    assert_eq!(f.expected.seats_in, x.to_vec());
    assert_eq!(f.expected.freeboard_in, env.nominal.verdict.freeboard_in);
    assert_eq!((f.gate.freeboard_in, f.gate.seats_in.clone()), (f.expected.freeboard_in, x.to_vec()));
    assert_eq!(env.verdict.freeboard_in, f.gate.freeboard_in);
    assert_eq!(f.required_in, p.gates.freeboard_required_in());
    assert!(f.best.freeboard_in >= f.expected.freeboard_in && f.expected.freeboard_in > f.worst.freeboard_in);
    for a in [&f.expected, &f.best, &f.worst] {
        // No lettering: the whole hull; freeboard = depth - highest local waterline (d + |trim| / 2 at an end).
        assert!((a.freeboard_in - (g.depth - a.waterline_in)).abs() < 1e-12);
        assert!((a.waterline_in - (a.draft_in + 0.5 * a.trim_in.abs())).abs() < 1e-9);
        assert_eq!(a.freeboard_in, a.hull_min_in);
        let e = eval_case(&p, &fit, &g, &s, 58.0, case, &a.seats_in).unwrap();
        assert_eq!(e.verdict.freeboard_in, a.freeboard_in, "reproduced at its seats");
    }
    // The worst sample moves every knee the same way (the most trim).
    let d: Vec<f64> = f.worst.seats_in.iter().zip(&x).map(|(a, b)| a - b).collect();
    assert!(d.iter().all(|v| (v.abs() - 6.0).abs() < 1e-9) && d.windows(2).all(|w| w[0] == w[1]), "{d:?}");
    // Gated over the same shift as the strength variance: the gate is that worst sample.
    p.gates.freeboard_seat_shift_in = 6.0;
    let env6 = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
    assert!((env6.freeboard.gate.freeboard_in - f.worst.freeboard_in).abs() < 1e-12);
    assert_eq!(env6.verdict.freeboard_in, env6.freeboard.gate.freeboard_in);
    // A 2 in shift sits in between.
    p.gates.freeboard_seat_shift_in = 2.0;
    let env2 = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
    let g2 = env2.freeboard.gate.freeboard_in;
    assert!(g2 < f.expected.freeboard_in && g2 > f.worst.freeboard_in, "{g2}");
    // The margin adds to the rule minimum.
    p.gates.freeboard_margin_in = 0.25;
    assert_eq!(p.gates.freeboard_required_in(), p.gates.freeboard_min_in + 0.25);
}

#[test]
fn freeboard_is_gated_where_the_lettering_sits() {
    let (mut p, _, fit, g, s) = setup();
    let l = g.x[g.nx() - 1];
    let case = &p.load_cases[1];
    // Off-centre knees: the canoe trims, one end is the lowest point of the hull.
    let x = [80.0, 160.0];
    let hull = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
    assert!(hull.freeboard.gate.trim_in.abs() > 0.05, "{}", hull.freeboard.gate.trim_in);
    assert!(hull.freeboard.gate.at_x_in == g.x[0] || hull.freeboard.gate.at_x_in == l);
    p.lettering = Some(jaguar::params::Lettering {
        school: "UVA".into(),
        canoe_name: "JAGUAR".into(),
        height_in: 5.0,
        width_ratio: 1.0,
        below_gunwale_in: 1.0,
        end_margin_in: 24.0,
    });
    let spans = p.freeboard_spans(l).unwrap();
    assert_eq!((spans[0].x0_in, spans[0].x1_in), (24.0, 39.0));
    assert_eq!((spans[1].x0_in, spans[1].x1_in), (l - 24.0 - 30.0, l - 24.0));
    let lett = eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).unwrap();
    let f = &lett.freeboard.gate;
    // Same float; gated at the lettering end nearest the low end, which sits higher than the tip.
    assert_eq!((f.draft_in, f.trim_in), (hull.freeboard.gate.draft_in, hull.freeboard.gate.trim_in));
    assert_eq!(f.at_x_in, if f.trim_in < 0.0 { 24.0 } else { l - 24.0 });
    assert!(
        f.freeboard_in > f.hull_min_in && (f.hull_min_in - hull.freeboard.gate.freeboard_in).abs() < 1e-12
    );
    let expect = g.depth - (f.draft_in + f.trim_in * (f.at_x_in - l / 2.0) / l);
    assert!((f.freeboard_in - expect).abs() < 1e-12);
    assert_eq!(lett.verdict.freeboard_in, f.freeboard_in);
    assert_eq!(lett.freeboard.lettering, spans);
    // Lettering longer than the hull: the case cannot be evaluated (an error, never a pass).
    p.lettering.as_mut().unwrap().school = "x".repeat(60);
    assert!(eval_envelope(&p, &fit, &g, &s, 58.0, case, &x, false).is_err());
}

#[test]
fn verification_rechecks_freeboard() {
    let (mut p, h, ..) = setup();
    p.seats.variance_in = 6.0;
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert!(w.verified, "{:?}", w.verify_failure);
    let worst = w.cases.iter().map(|k| k.freeboard_in.unwrap()).fold(f64::INFINITY, f64::min);
    // A freeboard gate just above the winner's: the search may pass it, verification must not.
    let mut q = p.clone();
    q.gates.freeboard_min_in = worst + 1e-6;
    let r = optimize::run(&q, &h).unwrap();
    if let Some(w2) = r.winner {
        let fb = w2.cases.iter().map(|k| k.freeboard_in.unwrap()).fold(f64::INFINITY, f64::min);
        assert!(!(w2.verified && fb < q.gates.freeboard_required_in()), "verified below the freeboard gate");
        if fb < q.gates.freeboard_required_in() {
            assert!(w2.verify_failure.unwrap().contains("freeboard"));
        }
    }
}

#[test]
fn infeasible_seat_constraints_fail_candidates() {
    let (mut p, h, ..) = setup();
    p.seats.bow_clear_in = 190.0;
    let r = optimize::run(&p, &h).unwrap();
    assert!(r.winner.is_none());
    for c in &r.candidates {
        assert!(!c.passes && c.error.as_deref().unwrap_or("").contains("auto seats"), "{:?}", c.error);
    }
}

#[test]
fn auto_seats_are_deterministic() {
    let (p, h, ..) = setup();
    let a = serde_json::to_string(&optimize::run(&p, &h).unwrap()).unwrap();
    let b = serde_json::to_string(&optimize::run(&p, &h).unwrap()).unwrap();
    assert_eq!(a, b);
}

/// Auto seats are chosen at their worst forward / aft shift by the seat variance (where the
/// verdict's worst float, freeboard and GM corners are), so with a large variance they are
/// never worse than even spacing there.
#[test]
fn auto_seats_are_scored_at_their_worst_shift() {
    let (mut p, _, fit, g, s) = setup();
    p.seats.variance_in = 12.0;
    let robust = |case: usize, seats_in: &[f64]| {
        seats::shifted(seats_in, p.seats.variance_in, &g)
            .iter()
            .map(|q| exact(&p, &fit, &g, &s, case, q))
            .fold(0.0, f64::max)
    };
    let plan = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    for (i, cs) in plan.iter().enumerate() {
        let even = seats::even(cs.seats_in.len(), &g);
        let (a, e) = (robust(i, &cs.seats_in), robust(i, &even));
        assert!(a <= e + 1e-12, "case {i}: auto {a} > even {e} at the worst shift");
    }
    // The shifted sets: nominal, all forward, all aft, kept on the hull.
    let l = g.x[g.nx() - 1];
    let sh = seats::shifted(&[5.0, 120.0], 12.0, &g);
    assert_eq!(sh, vec![vec![5.0, 120.0], vec![0.0, 108.0], vec![17.0, 132.0]]);
    assert!(sh.iter().flatten().all(|&x| (0.0..=l).contains(&x)));
    assert_eq!(seats::shifted(&[50.0], 0.0, &g), vec![vec![50.0]]);
}

#[test]
fn pinned_paddlers_keep_their_seat_and_the_rest_are_chosen() {
    let (mut p, _, fit, g, s) = setup();
    // Three paddlers. Unpinned, auto seats put paddler 2 somewhere other than the bow ...
    p.load_cases[1].weights_lb = vec![250.0, 120.0, 130.0];
    let free = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    assert_ne!(free[1].crew_order[0], 2, "{:?}", free[1].crew_order);
    // ... pinned to the bow seat, it is there, and the other two are still chosen.
    p.load_cases[1].positions = Some(vec![0, 0, 1]);
    let plan = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    assert_eq!(plan[1].source, SeatSource::Auto);
    assert_eq!(plan[1].crew_order[0], 2, "pinned bow paddler moved: {:?}", plan[1].crew_order);
    // Evenly spaced seats keep the pin and fill the rest in entered order.
    p.seats.auto = false;
    p.load_cases[1].positions = Some(vec![0, 0, 1]);
    let even = plan_seats(&p, &fit, &g, &s, 0.375, 58.0).unwrap();
    assert_eq!(even[1].crew_order, vec![2, 0, 1]);
    // Invalid pins are input errors.
    let base = std::fs::read_to_string("examples/canoe.toml").unwrap().replace("\r\n", "\n");
    let bad = |pos: &str| {
        let t = base.replacen(
            "weights_lb = [175.0, 165.0]\n",
            &format!("weights_lb = [175.0, 165.0]\npositions = {pos}\n"),
            1,
        );
        assert_ne!(t, base);
        Params::parse(&t, Path::new("examples")).unwrap_err().to_string()
    };
    assert!(bad("[1, 1]").contains("same seat"));
    assert!(bad("[3, 0]").contains("seat numbers"));
    assert!(bad("[1]").contains("differ in length"));
    let ok = base.replacen(
        "weights_lb = [175.0, 165.0]\n",
        "weights_lb = [175.0, 165.0]\npositions = [0, 1]\n",
        1,
    );
    assert_eq!(Params::parse(&ok, Path::new("examples")).unwrap().load_cases[0].positions, Some(vec![0, 1]));
}
