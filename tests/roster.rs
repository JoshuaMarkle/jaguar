//! Roster (src/roster.rs) on the example winner: every event gets a safe crew of the right genders
//! with a steerer, the synergy numbers add up, people marked in / out are respected, and what
//! cannot be done says why.

use jaguar::params::{Params, RaceParams};
use jaguar::roster::{EventSpec, Gender, PaddleSide, Paddler, Request, Settings, assign, default_events};
use jaguar::seating::{Canoe, canoe_from_result};

/// The examples/canoe.toml winner (run once) and its `[race]`.
fn setup() -> (Canoe, RaceParams) {
    static RESULT: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    let p = Params::load(std::path::Path::new("examples/canoe.toml")).unwrap();
    let text = RESULT.get_or_init(|| serde_json::to_string(&jaguar::run(&p, &|_| {}).unwrap()).unwrap());
    (canoe_from_result(text).unwrap(), p.race.unwrap())
}

fn paddler(name: &str, g: Gender, lb: f64, w1: Option<f64>, steer: u8) -> Paddler {
    Paddler {
        name: name.into(),
        gender: g,
        weight_lb: lb,
        height_in: Some(68.0),
        watts_1min: w1,
        watts_4min: w1.map(|w| 0.7 * w),
        steer_stern: steer,
        steer_bow: 0,
    }
}

fn team() -> Vec<Paddler> {
    vec![
        paddler("A", Gender::M, 175.0, Some(260.0), 3),
        paddler("B", Gender::M, 190.0, Some(290.0), 0),
        paddler("C", Gender::M, 160.0, None, 1),
        paddler("F", Gender::F, 145.0, Some(190.0), 2),
        paddler("G", Gender::F, 130.0, Some(170.0), 0),
        paddler("H", Gender::F, 150.0, Some(200.0), 3),
    ]
}

/// The example's 2-paddler sprints and slalom (keeps the test quick).
fn events(rp: &RaceParams) -> Vec<EventSpec> {
    default_events(rp).into_iter().filter(|e| e.men + e.women == 2).take(3).collect()
}

fn req(paddlers: Vec<Paddler>, events: Vec<EventSpec>) -> Request {
    Request { paddlers, events, settings: Settings::default(), rules: None }
}

#[test]
fn every_event_gets_a_safe_crew_and_the_numbers_add_up() {
    let (c, rp) = setup();
    let ev = events(&rp);
    assert_eq!(ev.len(), 3, "{ev:?}");
    let r = req(team(), ev.clone());
    let out = assign(&c, &rp, &r).unwrap();
    let s = Settings::default();
    for (a, e) in out.assignments.iter().zip(&ev) {
        assert!(a.error.is_none(), "{}: {:?}", a.event, a.error);
        let ps = &r.paddlers;
        let men = a.crew.iter().filter(|m| ps[m.paddler].gender == Gender::M).count();
        assert_eq!((men, a.crew.len() - men), (e.men, e.women), "{}", a.event);
        // Seats 1..n bow to stern, the steerer in the stern and able to steer there.
        assert!(a.crew.iter().enumerate().all(|(k, m)| m.seat == k + 1));
        let st: Vec<_> = a.crew.iter().filter(|m| m.steerer).collect();
        assert_eq!(st.len(), 1);
        assert_eq!(st[0].seat, a.crew.len());
        assert!(ps[st[0].paddler].steer_stern >= 1);
        // Sides alternate; the synergy terms are what the members' watts give.
        assert!(a.crew.windows(2).all(|w| w[0].side != w[1].side));
        let side = |sd: PaddleSide| a.crew.iter().filter(|m| m.side == sd).map(|m| m.watts).sum::<f64>();
        let raw: f64 = a.crew.iter().map(|m| m.watts).sum();
        let mean = raw / a.crew.len() as f64;
        let pace = s.k_pace * a.crew.iter().map(|m| (m.watts - mean).abs()).sum::<f64>();
        assert!((a.raw_power_w.unwrap() - raw).abs() < 1e-9);
        assert!(
            (a.tracking_loss_w.unwrap()
                - s.k_track * (side(PaddleSide::Left) - side(PaddleSide::Right)).abs())
            .abs()
                < 1e-9
        );
        assert!((a.pace_loss_w.unwrap() - pace).abs() < 1e-9);
        assert!((a.effective_power_w.unwrap() - (raw - a.tracking_loss_w.unwrap() - pace)).abs() < 1e-9);
        // Safe on the canoe, a real time, knees on the hull bow to stern.
        assert!(a.dc.unwrap() <= 1.0 && a.time_s.unwrap() > 10.0);
        assert!(a.crew.windows(2).all(|w| w[0].knee_in < w[1].knee_in));
        // C has no 1-min test: flagged wherever C races.
        for m in &a.crew {
            assert_eq!(m.estimated, m.name == "C", "{}", m.name);
        }
    }
    assert_eq!(out.estimate_1min.men.from, "gender");
    // Deterministic.
    let again = assign(&c, &rp, &r).unwrap();
    assert_eq!(serde_json::to_string(&out).unwrap(), serde_json::to_string(&again).unwrap());
}

#[test]
fn tracking_straight_beats_raw_power_when_it_costs_enough() {
    let (c, rp) = setup();
    let men = default_events(&rp).into_iter().find(|e| (e.men, e.women) == (2, 0)).expect("a 2M event");
    let ps = vec![
        paddler("Strong", Gender::M, 170.0, Some(400.0), 3),
        paddler("Even1", Gender::M, 170.0, Some(230.0), 3),
        paddler("Even2", Gender::M, 170.0, Some(230.0), 3),
    ];
    let names = |k_track: f64| {
        let mut r = req(ps.clone(), vec![men.clone()]);
        r.settings.k_track = k_track;
        let a = assign(&c, &rp, &r).unwrap().assignments.remove(0);
        assert!(a.error.is_none(), "{:?}", a.error);
        let mut n: Vec<String> = a.crew.iter().map(|m| m.name.clone()).collect();
        n.sort();
        n
    };
    // No tracking cost: the strongest pair (630 W). A big one: the balanced pair (460 W, no loss)
    // beats 630 - 2 x 170 = 290 W.
    assert!(names(0.0).contains(&"Strong".to_string()));
    assert_eq!(names(2.0), vec!["Even1", "Even2"]);
}

#[test]
fn people_marked_in_and_out_are_respected() {
    let (c, rp) = setup();
    let mut ev = events(&rp);
    let ps = team();
    // A (best steerer, index 0) out everywhere, B (index 1) in every event with men.
    for e in &mut ev {
        e.exclude = vec![0];
        if e.men > 0 {
            e.include = vec![1];
        }
    }
    let out = assign(&c, &rp, &req(ps.clone(), ev.clone())).unwrap();
    for (a, e) in out.assignments.iter().zip(&ev) {
        if let Some(err) = &a.error {
            // B cannot steer and C is the only other man: a 2M crew of B + C works (C steers).
            panic!("{}: {err}", a.event);
        }
        assert!(a.crew.iter().all(|m| m.paddler != 0), "{}", a.event);
        if e.men > 0 {
            assert!(a.crew.iter().any(|m| m.paddler == 1), "{}", a.event);
        }
    }
}

#[test]
fn impossible_events_say_why() {
    let (c, rp) = setup();
    let women = default_events(&rp).into_iter().find(|e| (e.men, e.women) == (0, 2)).expect("a 2F event");
    let two_f = |steer: u8| {
        vec![
            paddler("F", Gender::F, 140.0, Some(180.0), steer),
            paddler("G", Gender::F, 130.0, Some(170.0), steer),
        ]
    };
    let err = |ps: Vec<Paddler>, e: EventSpec| {
        assign(&c, &rp, &req(ps, vec![e])).unwrap().assignments.remove(0).error
    };
    assert!(err(two_f(0), women.clone()).unwrap().contains("steerer"));
    assert!(err(two_f(2)[..1].to_vec(), women.clone()).unwrap().contains("needs 2 women, 1 available"));
    let mut both = women.clone();
    both.include = vec![0];
    both.exclude = vec![0];
    assert!(err(two_f(2), both).unwrap().contains("both in and out"));
    assert!(err(two_f(2), EventSpec { men: 0, women: 0, ..women.clone() }).unwrap().contains("how many"));
    // Bad input is refused outright.
    let mut bad = two_f(2);
    bad[0].weight_lb = -1.0;
    assert!(assign(&c, &rp, &req(bad, vec![women.clone()])).is_err());
    assert!(assign(&c, &rp, &req(two_f(2), vec![EventSpec { name: "nope".into(), ..women }])).is_err());
}
