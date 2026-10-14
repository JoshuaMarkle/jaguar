//! `jaguar race` end to end: the winner of a run, read back from its result.json, raced over
//! a sprint; deterministic, and the hull weight is the winner's.

use jaguar::params::Params;
use jaguar::{race, report};
use std::path::Path;

#[test]
fn races_the_winner_of_a_result_file() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let result = jaguar::run(&p, &|_| {}).unwrap();
    let text = serde_json::to_string(&result).unwrap();
    let input = report::race_input(&text).unwrap();
    let w = result.winner.as_ref().unwrap();
    // serde_json may round the last digit.
    assert!((input.hull_lb / w.candidate.hull_weight_lb - 1.0).abs() < 1e-12);
    assert_eq!(input.depth_in, w.candidate.depth_in);
    let mut rp = input.race.clone().expect("example has [race]");
    rp.events.retain(|e| e.name == "Symposium men's 200 m sprint");
    rp.events[0].weight = 1.0;
    let a = race::run(&input.hull, input.hull_lb, input.depth_in, input.topsides, &rp).unwrap();
    let b = race::run(&input.hull, input.hull_lb, input.depth_in, input.topsides, &rp).unwrap();
    assert_eq!(serde_json::to_string(&a).unwrap(), serde_json::to_string(&b).unwrap());
    let e = &a.events[0];
    assert_eq!(e.course, "sprint_200");
    assert!((e.straight_m + e.turn_m - e.distance_m).abs() < 1e-9);
    assert!((e.straight_s + e.turn_s - e.time_s).abs() < 1e-9);
    assert!((a.weighted_time_s - e.time_s).abs() < 1e-12);
    let lines = report::race_lines(&a);
    assert!(lines.iter().any(|l| l.contains("straight_weight")), "{lines:?}");
}

#[test]
fn printed_displacements_sum_to_one() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let mut rp = p.race.unwrap();
    // Three crews, shares that do not round to a sum of 1.
    rp.events.retain(|e| e.course == "sprint_200" && e.name.starts_with("Symposium"));
    for e in rp.events.iter_mut() {
        e.weight = 1.0 / 3.0;
    }
    let hull = jaguar::hull::Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
    let r = race::run(&hull, 100.0, 12.0, jaguar::hull::Topsides::default(), &rp).unwrap();
    let line = report::race_lines(&r).into_iter().find(|l| l.contains("displacements")).unwrap();
    let sum: f64 = line.split("weight = ").skip(1).map(|s| s[..5].parse::<f64>().unwrap()).sum();
    assert!((sum - 1.0).abs() < 1e-9, "{line}");
}
