//! The winner's power and righting-lever curves end to end (examples/canoe.toml).

use jaguar::params::Params;
use std::path::Path;

#[test]
fn winner_gets_power_and_gz_curves() {
    let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    let r = jaguar::run(&p, &|_| {}).unwrap();
    let w = r.winner.as_ref().expect("winner");
    let c = w.curves.as_ref().expect("curves");
    assert!(c.error.is_none(), "{:?}", c.error);
    // One power curve per load case, one GZ curve per load case plus the empty canoe.
    let names: Vec<&str> = p.load_cases.iter().map(|lc| lc.name.as_str()).collect();
    assert_eq!(c.power.iter().map(|k| k.name.as_str()).collect::<Vec<_>>(), names);
    assert_eq!(c.gz.len(), names.len() + 1);
    assert_eq!(c.gz.last().unwrap().name, "empty");
    for k in &c.power {
        assert!(k.error.is_none(), "{}: {:?}", k.name, k.error);
        assert_eq!(k.power_w.len(), c.speeds_fps.len());
        assert!(k.power_w.windows(2).all(|w| w[1] > w[0]), "{}: power rises with speed", k.name);
        // Hull + crew as floated.
        assert!(
            (k.displacement_lb - k.crew_lb - w.candidate.hull_weight_lb).abs() < 0.02 * k.displacement_lb
        );
    }
    // Small heel: GZ = GM phi. The empty canoe's GZ slope matches the GM check's empty GM
    // (that one at its trimmed waterline, the curve level: a few percent apart at most).
    let e = c.gz.last().unwrap();
    assert!(e.error.is_none(), "{:?}", e.error);
    let slope = e.gz_in[1] / c.angles_deg[1].to_radians();
    let gm = w.empty.gm_in.unwrap();
    assert!((slope / gm - 1.0).abs() < 0.05, "GZ slope {slope} vs GM {gm}");
    assert!(e.max_gz_in.unwrap() > 0.0 && e.max_gz_deg.unwrap() > 0.0);
    let text = jaguar::report::summary_lines(&r).join("\n");
    // The summary line reports the heaviest loading.
    let heaviest =
        c.gz.iter().filter(|g| g.name != "empty").max_by(|a, b| a.weight_lb.total_cmp(&b.weight_lb)).unwrap();
    assert!(text.contains(&format!("stability {}: max GZ", heaviest.name)), "{text}");
}
