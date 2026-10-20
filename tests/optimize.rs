use jaguar::hull::Hull;
use jaguar::optimize::{self, Progress};
use jaguar::params::Params;
use jaguar::seats::SeatSource;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

/// examples/canoe.toml with the weight objective (it ships with drag); these
/// tests assert lightest-passing winners.
fn example() -> (Params, Hull) {
    let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
    p.objective.kind = jaguar::params::ObjectiveKind::Weight;
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap();
    (p, h)
}

/// The example with the GM gate relaxed to 0 (upright stable). The real hull has a loaded
/// GM_T of about 1.06 in at the winner (real crew weight, crew CG 12 in); examples/canoe.toml
/// uses gm_min_in = 0.25 so it passes either way.
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
    // Strength from examples/mix_tests.csv; knee punching (ACI 318-19 22.6.5.2,
    // demand on b0*d, two knees with a 60/40 split) governs and needs 58 pcf.
    assert_eq!((c.thickness_in, c.density_pcf, c.depth_in), (0.375, 58.0, 12.0));
    // 98.39 lb on the old fixed-height table blend; 98.24 on the smooth-keel loft (no hull
    // between stations below the keel line).
    assert!((c.hull_weight_lb / 98.24 - 1.0).abs() < 1e-3, "{}", c.hull_weight_lb);
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
    // Search space: thickness on its min, but that is the buildable floor.
    use optimize::Bound;
    let ax: Vec<_> =
        w.search_space.iter().map(|a| (a.name, a.min, a.max, a.step, a.count, a.winner)).collect();
    assert_eq!(
        ax,
        [
            ("thickness_in", 0.375, 0.75, Some(0.0625), Some(7), 0.375),
            ("density_pcf", 51.0, 64.0, Some(1.0), Some(14), 58.0),
            ("depth_in", 10.0, 16.0, Some(1.0), Some(7), 12.0),
        ]
    );
    assert_eq!(w.search_space[0].at_bound, Some(Bound::Min));
    assert_eq!(w.search_space[0].limit, Some("buildable floor"));
    assert!(w.search_space[1..].iter().all(|a| a.at_bound.is_none()));
    // 58 pcf is between the tested M2 A (56.408) and the 60 pcf mix: predicted, reach 1.6 pcf.
    let pv = &w.mix_provenance;
    assert_eq!(pv.status, jaguar::mix::Provenance::Predicted);
    assert_eq!(pv.nearest.len(), 2);
    assert!((pv.reach_pcf - 1.592).abs() < 1e-9, "{}", pv.reach_pcf);
    assert!(r.candidates.iter().all(|c| c.density_pcf >= 51.0 - 1e-9));
    assert!(r.candidates.iter().all(|c| c.thickness_in >= 0.375 - 1e-9));
}

/// Winner on a search bound that is not a real limit -> on_grid_edge, and the
/// axis says which bound; a density clipped by the mix data -> "mix data".
#[test]
fn search_space_flags_bounds() {
    use optimize::Bound;
    let (mut p, h) = example_gm0();
    p.search.depth_in = [12.0, 14.0, 1.0];
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert_eq!(w.candidate.depth_in, 12.0);
    assert_eq!((w.search_space[2].at_bound, w.search_space[2].limit), (Some(Bound::Min), None));
    assert!(w.on_grid_edge);
    // Thick wall: lightest density wins, and 51 pcf is as light as the lab data allows.
    let (mut p, h) = example_gm0();
    p.search.thickness_in = [0.625, 0.625, 0.0625];
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert_eq!(w.candidate.density_pcf, 51.0);
    assert_eq!((w.search_space[1].at_bound, w.search_space[1].limit), (Some(Bound::Min), Some("mix data")));
    assert!(w.at_mix_data_edge && !w.on_grid_edge);
    // One-value thickness axis: no bound.
    assert_eq!(w.search_space[0].at_bound, None);
}

/// search.depth_tol_in: the winner moves below its 12 in grid depth to the shallowest
/// 1/16 in step that passes; the next 1/16 in shallower was tried and fails.
#[test]
fn depth_tol_refines_winner_depth() {
    let (mut p, h) = example_gm0();
    let grid = optimize::run(&p, &h).unwrap().winner.unwrap().candidate;
    p.search.depth_tol_in = Some(0.0625);
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.unwrap();
    let c = &w.candidate;
    assert!(w.verified);
    assert!(c.depth_in < grid.depth_in && c.depth_in > grid.depth_in - 1.0, "{}", c.depth_in);
    assert_eq!(((grid.depth_in - c.depth_in) / 0.0625).fract(), 0.0);
    assert!(c.hull_weight_lb < grid.hull_weight_lb);
    assert_eq!((c.thickness_in, c.density_pcf), (grid.thickness_in, grid.density_pcf));
    let below = r.candidates.iter().find(|k| (k.depth_in - (c.depth_in - 0.0625)).abs() < 1e-12);
    assert!(below.is_some_and(|k| !k.passes), "next shallower depth must be tried and fail");
    assert_eq!(w.search_space[2].at_bound, None);
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
        c.weights_lb.fill(900.0);
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
    // Drag is reported whatever the objective, for every analysed candidate.
    assert!(c.drag_n.is_some_and(|d| d > 0.0), "weight objective still reports drag");
    assert!(r.candidates.iter().filter(|k| k.verdict.is_some()).all(|k| k.drag_n.is_some()));
    assert!(c.gm_in.unwrap() >= p.stability.gm_min_in);
    p.objective.kind = jaguar::params::ObjectiveKind::Drag;
    let d = optimize::run(&p, &h).unwrap().winner.unwrap().candidate;
    // The drag pick has no more drag than the weight pick.
    assert!(d.drag_n.unwrap() <= c.drag_n.unwrap() + 1e-9);
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

#[test]
fn winner_has_diagrams_for_every_load_case() {
    let (mut p, h) = example();
    // Even seats, nominal only: per-station D/C then equals the case verdict.
    (p.seats.auto, p.seats.variance_in) = (false, 0.0);
    let mut c2 = p.load_cases[0].clone();
    c2.name = "seated".into();
    c2.seats_in = Some(vec![90.0, 170.0]);
    p.load_cases.push(c2);
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.unwrap();
    assert_eq!(w.cases.len(), p.load_cases.len());
    let len = w.cases[0].diagrams.x_in[w.verify_stations - 1] - w.cases[0].diagrams.x_in[0];
    for (k, lc) in w.cases.iter().zip(&p.load_cases) {
        assert_eq!(k.name, lc.name);
        assert!(k.error.is_none());
        let n = lc.weights_lb.len();
        let want: Vec<f64> = match &lc.seats_in {
            Some(s) => s.clone(),
            None => (0..n).map(|i| k.diagrams.x_in[0] + len * (i + 1) as f64 / (n + 1) as f64).collect(),
        };
        assert_eq!(k.seats_in, want);
        assert_eq!(k.verdict_seats_in, want);
        let src = if lc.seats_in.is_some() { SeatSource::Given } else { SeatSource::Even };
        assert_eq!(k.seat_source, Some(src));
        let d = &k.diagrams;
        assert_eq!(d.x_in.len(), w.verify_stations);
        for v in [
            &k.compression_psi,
            &k.shear_psi,
            &k.dc_bending_tension,
            &k.dc_bending_compression,
            &k.dc_shear,
            &k.dc_biaxial,
        ] {
            assert_eq!(v.len(), w.verify_stations);
            assert!(v.iter().all(|x| x.is_finite() && *x >= 0.0));
        }
        // V and M close at both ends.
        let vmax = d.shear_lb.iter().fold(0.0f64, |a, b| a.max(b.abs()));
        let mmax = d.moment_lbin.iter().fold(0.0f64, |a, b| a.max(b.abs()));
        for e in [0, w.verify_stations - 1] {
            assert!(d.shear_lb[e].abs() < 0.02 * vmax, "{} V end {}", k.name, d.shear_lb[e]);
            assert!(d.moment_lbin[e].abs() < 0.02 * mmax, "{} M end {}", k.name, d.moment_lbin[e]);
        }
        // Per-station D/C max equals that case's verdict.
        let v = k.verdict.as_ref().unwrap();
        for (name, arr) in [
            ("bending_tension", &k.dc_bending_tension),
            ("bending_compression", &k.dc_bending_compression),
            ("shear", &k.dc_shear),
            ("biaxial", &k.dc_biaxial),
        ] {
            let c = v.checks.iter().find(|c| c.name == name).unwrap();
            let m = arr.iter().cloned().fold(0.0, f64::max);
            assert!((m - c.dc).abs() < 1e-9 * c.dc.max(1.0), "{name}: {m} vs {}", c.dc);
        }
    }
    // Governing-case diagrams kept and equal to that case's.
    let g = w.candidate.governing_case.as_deref().unwrap();
    let gk = w.cases.iter().find(|k| k.name == g).unwrap();
    assert_eq!(gk.diagrams.moment_lbin, w.diagrams.moment_lbin);
    // Biaxial is 0 at the masked tip stations, like the other per-station checks.
    for k in &w.cases {
        let masked: Vec<usize> = (0..k.dc_biaxial.len())
            .filter(|&i| k.diagrams.inertia_in4[i] <= 1e-3 * imax(&k.diagrams))
            .collect();
        assert!(masked.iter().all(|&i| k.dc_biaxial[i] == 0.0 && k.dc_shear[i] == 0.0));
    }
}

/// The bare hull (no crew) floats higher than under any crew, and its
/// freeboard is the gunwale minus the highest local waterline.
#[test]
fn winner_has_empty_float() {
    let (p, h) = example();
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    let e = &w.empty;
    assert!(e.error.is_none(), "{:?}", e.error);
    let (draft, fb) = (e.draft_in.unwrap(), e.freeboard_in.unwrap());
    assert_eq!(e.x_in, w.cases[0].diagrams.x_in);
    assert!(draft > 0.0);
    for k in &w.cases {
        assert!(draft < k.draft_in.unwrap(), "{}: {draft} vs {:?}", k.name, k.draft_in);
        assert!(fb > k.freeboard_in.unwrap(), "{}: {fb} vs {:?}", k.name, k.freeboard_in);
    }
    let top = e.waterline_in.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    assert!((w.candidate.depth_in - top - fb).abs() < 1e-9, "{top} {fb}");
}

/// The report's calculation trace recomputes the governing case: every
/// demand and capacity equals that case's verdict (verification resolution).
fn assert_calc_matches(p: &Params, h: &Hull) {
    let w = optimize::run(p, h).unwrap().winner.unwrap();
    assert!(w.calc.error.is_none(), "{:?}", w.calc.error);
    let c = w.calc.calc.as_ref().unwrap();
    let k = w.cases.iter().find(|k| k.name == c.case).unwrap();
    let v = k.verdict.as_ref().unwrap();
    let got = |n: &str| -> (f64, f64, Option<f64>) {
        match n {
            "bending_tension" | "bending_compression" => {
                let f = if n == "bending_tension" { &c.bending_tension } else { &c.bending_compression };
                let f = f.as_ref().unwrap();
                (f.stress_psi, f.capacity_psi, Some(f.x_in))
            }
            "shear" => {
                let s = c.shear.as_ref().unwrap();
                (s.stress_psi, s.capacity_psi, Some(s.x_in))
            }
            "punching" => (c.punching.calc.v_u, c.punching.capacity_psi, None),
            "biaxial" => {
                let b = c.biaxial.as_ref().unwrap();
                (b.dc, 1.0, Some(b.x_in))
            }
            _ => unreachable!(),
        }
    };
    for chk in &v.checks {
        let (d, cap, x) = got(chk.name);
        assert_eq!((d, cap, x), (chk.demand, chk.capacity, chk.x_in), "{}", chk.name);
    }
    // The hand-checkable formulas the report prints hold with these numbers.
    let bt = c.bending_tension.as_ref().unwrap();
    assert!((bt.moment_lbin.abs() * bt.c_in / bt.inertia_in4 - bt.stress_psi).abs() < 1e-9 * bt.stress_psi);
    let s = &c.service;
    assert!((s.kb_in + s.bm_in - s.kg_in - s.gm_in).abs() < 1e-12);
    assert!((c.depth_in - s.waterline_max_in - s.freeboard_in).abs() < 1e-9);
    assert!((c.wall_volume_in3 * c.density_pcf / 1728.0 - s.hull_weight_lb).abs() < 1e-9 * s.hull_weight_lb);
    assert!((s.hull_weight_lb + s.crew_lb - s.weight_lb).abs() < 1e-9 * s.weight_lb);
}

#[test]
fn calc_trace_matches_verdict_asd() {
    let (p, h) = example();
    assert_calc_matches(&p, &h);
}

#[test]
fn calc_trace_matches_verdict_lrfd_turning() {
    let (mut p, h) = example();
    p.material.method = jaguar::params::DesignMethod::Lrfd;
    p.turning = Some(jaguar::params::Turning { speed_mps: 2.0, radius_in: 600.0 });
    assert_calc_matches(&p, &h);
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    let c = w.calc.calc.unwrap();
    assert_eq!((c.dead_factor, c.live_factor), (1.2, 1.6));
    assert!(c.biaxial.unwrap().transverse_psi > 0.0);
}

/// The freeboard gate covers every load case, not only the one with the
/// highest D/C: a light crew kneeling forward (2F here) trims the bow down
/// while the heavier 2M crew still governs on punching.
#[test]
fn freeboard_gate_covers_non_governing_cases() {
    let (mut p, h) = example();
    p.load_cases[1].seats_in = Some(vec![80.0, 130.0]);
    let r = optimize::run(&p, &h).unwrap();
    let fit = optimize::strength(&p).unwrap();
    let lc = &p.load_cases[1];
    let mut checked = 0;
    for c in r.candidates.iter().filter(|c| c.passes) {
        let grid = jaguar::hull::Grid::build_flared(&h, c.depth_in, p.hull.stations, p.hull.flare_deg);
        let sec = jaguar::section::sections(&grid, c.thickness_in);
        let seats = lc.seats_in.clone().unwrap();
        let e = optimize::eval_case(&p, &fit, &grid, &sec, c.density_pcf, lc, &seats).unwrap();
        assert!(
            e.verdict.freeboard_in >= p.gates.freeboard_min_in,
            "t {} rho {} depth {}: 2F freeboard {}",
            c.thickness_in,
            c.density_pcf,
            c.depth_in,
            e.verdict.freeboard_in
        );
        checked += 1;
    }
    assert!(checked > 0);
    let missed = r.candidates.iter().filter(|c| {
        c.governing_case.as_deref() == Some("2M")
            && c.verdict.as_ref().is_some_and(|v| v.freeboard_in < p.gates.freeboard_min_in)
    });
    assert!(missed.count() > 0, "no candidate where only the non-governing case misses freeboard");
}

fn imax(d: &optimize::Diagrams) -> f64 {
    d.inertia_in4.iter().cloned().fold(0.0, f64::max)
}

/// B1: LRFD factors the loads (1.2D+1.6L, 0.9D+1.6L) and uses phi on capacity.
/// Punching (local, no equilibrium) scales exactly: demand x 1.6 and capacity
/// x 0.75 instead of / 1.5, so the D/C of the same wall is x 1.6 / (0.75 * 1.5).
#[test]
fn lrfd_punching_scales_and_is_heavier() {
    let (mut p, h) = example();
    let asd = optimize::run(&p, &h).unwrap().winner.unwrap();
    p.material.method = jaguar::params::DesignMethod::Lrfd;
    let grid = jaguar::hull::Grid::build(&h, asd.candidate.depth_in, p.hull.stations);
    let sec = jaguar::section::sections(&grid, asd.candidate.thickness_in);
    let fit = jaguar::mix::StrengthFit::load(&p.material.mix_data, p.material.flex_over_split).unwrap();
    let rho = asd.candidate.density_pcf;
    let pdc = |v: &jaguar::check::Verdict| v.checks.iter().find(|c| c.name == "punching").unwrap().dc;
    let asd_v = asd.candidate.verdict.as_ref().unwrap();
    for lc in &p.load_cases {
        let seats = optimize::resolved_seats(lc, &grid);
        let e = optimize::eval_case(&p, &fit, &grid, &sec, rho, lc, &seats).unwrap();
        // Service draft is unchanged by the method; freeboard uses service loads.
        assert!(e.service.draft_in > 0.0 && e.verdict.freeboard_in.is_finite());
        if Some(&lc.name) == asd.candidate.governing_case.as_ref() {
            let want = 1.6 / (0.75 * 1.5);
            assert!((pdc(&e.verdict) / pdc(asd_v) - want).abs() < 1e-9, "{}", pdc(&e.verdict));
        }
    }
    let lrfd = optimize::run(&p, &h).unwrap().winner;
    if let Some(w) = lrfd {
        assert!(w.candidate.hull_weight_lb > asd.candidate.hull_weight_lb);
    }
}

/// LRFD load factors are inputs: a larger paddler factor raises the same wall's D/C and never
/// lightens the winner; the default factors reproduce 1.2D+1.6L.
#[test]
fn lrfd_live_factor_moves_the_answer() {
    let (mut p, h) = example();
    p.material.method = jaguar::params::DesignMethod::Lrfd;
    p.search.thickness_in = [0.375, 0.5, 0.0625];
    p.search.depth_in = [12.0, 13.0, 1.0];
    let base = optimize::run(&p, &h).unwrap();
    p.material.lrfd_live = 2.0;
    let heavy = optimize::run(&p, &h).unwrap();
    let key = |c: &optimize::Candidate| (c.thickness_in, c.density_pcf, c.depth_in);
    let mut compared = 0;
    for (a, b) in base.candidates.iter().zip(&heavy.candidates) {
        assert_eq!(key(a), key(b));
        if let (Some(va), Some(vb)) = (&a.verdict, &b.verdict) {
            assert!(vb.dc > va.dc, "{:?}: {} vs {}", key(a), vb.dc, va.dc);
            compared += 1;
        }
    }
    assert!(compared > 10);
    let w = |r: &optimize::RunResult| r.winner.as_ref().map_or(f64::INFINITY, |w| w.candidate.hull_weight_lb);
    assert!(w(&heavy) >= w(&base));
}

/// Line-arc-line gunwale on a fixed hull: off1's full ends need a gunwale so wide that the
/// default 30 deg side-lean limit rejects every depth (each candidate says why, no winner);
/// with the limit lifted the same radius builds, the winner's sides never lean in and its
/// gunwale follows the outline.
#[test]
fn gunwale_outline_on_a_fixed_hull() {
    let (mut p, h) = example();
    p.search.thickness_in = [0.375, 0.5, 0.125];
    p.search.depth_in = [12.0, 13.0, 1.0];
    p.hull.gunwale_radius_in = Some(200.0);
    let r = optimize::run(&p, &h).unwrap();
    assert!(r.winner.is_none());
    assert!(
        r.candidates.iter().all(|c| !c.passes && c.error.as_deref().is_some_and(|e| e.contains("lean out")))
    );
    p.hull.gunwale_max_flare_deg = 89.0;
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.as_ref().expect("winner with the lean limit lifted");
    let g = jaguar::hull::Grid::build_topsides(&h, w.candidate.depth_in, 161, p.hull.topsides()).0;
    let gw = jaguar::hull::Gunwale::of_hull(&h, w.candidate.depth_in, p.hull.flare_deg, 200.0).unwrap();
    let (nw, nz) = (h.z.len(), g.nz());
    for i in 0..g.nx() {
        assert!(g.y(i, nz - 1) >= g.y(i, nw - 1) - 1e-9);
        assert!((g.y(i, nz - 1) - gw.half_breadth(g.x[i])).abs() < 1e-12);
    }
    // Wider topsides weigh more than the plain extrusion's lightest wall of the same t, rho, depth.
    p.hull.gunwale_radius_in = None;
    let plain = optimize::run(&p, &h).unwrap();
    let key = |c: &optimize::Candidate| (c.thickness_in, c.density_pcf, c.depth_in);
    let same = plain.candidates.iter().find(|c| key(c) == key(&w.candidate)).unwrap();
    assert!(w.candidate.hull_weight_lb > same.hull_weight_lb);
}

/// search.flare_deg: flare is a wall-search axis. More flare only adds wall, so on off1 (no
/// gate needs it) the lightest passing wall takes the demolding minimum; every candidate
/// records its flare and the order is (thickness, density, depth, flare).
#[test]
fn flare_axis_takes_the_demolding_minimum_unless_needed() {
    let (mut p, h) = example();
    p.search.thickness_in = [0.375, 0.5, 0.125];
    p.search.depth_in = [12.0, 13.0, 1.0];
    p.search.flare_deg = Some([3.0, 5.0, 1.0]);
    p.validate().unwrap();
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.as_ref().unwrap();
    assert_eq!(w.candidate.flare_deg, 3.0);
    let ax = w.search_space.iter().find(|a| a.name == "flare_deg").unwrap();
    assert_eq!((ax.count, ax.limit), (Some(3), Some("demolding draft")));
    let n = r.candidates.len();
    assert_eq!(n % 3, 0);
    for k in r.candidates.chunks(3) {
        assert_eq!(k.iter().map(|c| c.flare_deg).collect::<Vec<_>>(), vec![3.0, 4.0, 5.0]);
        // Same wall, more flare: more concrete.
        assert!(k[0].hull_weight_lb < k[1].hull_weight_lb && k[1].hull_weight_lb < k[2].hull_weight_lb);
    }
    // A fixed flare of 3 gives the same winner as the axis.
    p.search.flare_deg = None;
    p.hull.flare_deg = 3.0;
    let fixed = optimize::run(&p, &h).unwrap();
    assert_eq!(fixed.winner.unwrap().candidate.hull_weight_lb, w.candidate.hull_weight_lb);
    // Both at once is ambiguous.
    p.search.flare_deg = Some([3.0, 5.0, 1.0]);
    assert!(p.validate().unwrap_err().to_string().contains("not both"));
}

/// hull.stem_radius_in (default 4 in): the run analyses the hull with the side-view stem arcs
/// (stem tops kept, so the same length; concrete only removed). On off1 the 4 in arc runs above
/// the design waterline into the topsides. Weight, displacement, GM and D/C come from that
/// geometry, the winner hull carries it, and the STEP / STL export is a closed solid with it.
#[test]
fn stem_radius_keeps_length_and_reaches_the_export() {
    let (mut p, h) = example();
    p.search.thickness_in = [0.375, 0.5, 0.125];
    p.search.depth_in = [12.0, 13.0, 1.0];
    assert_eq!(p.hull.stem_radius_in, 4.0, "examples/canoe.toml uses the default");
    // Even seats in both runs, so the comparison sees the geometry alone (auto seats may pick
    // another layout for a slightly different hull).
    p.seats.auto = false;
    let r = jaguar::run(&p, &|_| {}).unwrap();
    p.hull.stem_radius_in = 0.0;
    p.validate().unwrap();
    let plain = jaguar::run(&p, &|_| {}).unwrap();
    let (a, b) = (plain.winner.as_ref().unwrap(), r.winner.as_ref().unwrap());
    let wh = r.winner_hull.as_ref().unwrap();
    assert!(wh.x == h.x && wh.name.ends_with("-f4"), "the table is kept; the arcs ride along");
    assert!(wh.stem.as_ref().is_some_and(|s| s.top_in() > h.draft_design), "the arc runs above the table");
    assert!(plain.winner_hull.as_ref().unwrap().stem.is_none());
    assert_eq!(a.candidate.length_in, b.candidate.length_in);
    let key = |c: &optimize::Candidate| (c.thickness_in, c.density_pcf, c.depth_in);
    let mut moved = 0;
    for (c0, c1) in plain.candidates.iter().zip(&r.candidates) {
        assert_eq!(key(c0), key(c1));
        assert!(c1.hull_weight_lb < c0.hull_weight_lb, "the stem arc only removes concrete");
        assert!(c1.hull_weight_lb > c0.hull_weight_lb * 0.97, "and only a little");
        // The float and the checks see the cut ends: freeboard and GM move (by little: the arc
        // takes about 0.05 lb of concrete and as little displaced volume off off1); the knee
        // punching D/C (local) stays, the hull-girder checks recompute.
        if let (Some(v0), Some(v1)) = (&c0.verdict, &c1.verdict) {
            assert!(v1.freeboard_in != v0.freeboard_in && (v1.freeboard_in - v0.freeboard_in).abs() < 0.05);
            assert!(c1.gm_in != c0.gm_in);
            let other = |v: &jaguar::check::Verdict| {
                serde_json::to_string(&v.checks.iter().filter(|c| c.name != "punching").collect::<Vec<_>>())
                    .unwrap()
            };
            moved += usize::from(other(v0) != other(v1));
        }
    }
    assert!(moved > 0, "GM and D/C come from the cut geometry");
    let text = serde_json::to_string(&r).unwrap();
    let (hull, spec) = jaguar::export::from_result(&text, 161, 41).unwrap();
    let nosed = jaguar::export::build_mesh(&hull, &spec).unwrap();
    let (hp, sp) = jaguar::export::from_result(&serde_json::to_string(&plain).unwrap(), 161, 41).unwrap();
    let smooth = jaguar::export::build_mesh(&hp, &sp).unwrap();
    assert!(nosed.closed && nosed.tris.len() > smooth.tris.len());
    let (lo, hi) = (nosed.bbox(), smooth.bbox());
    assert!((lo.1[0] - lo.0[0] - (hi.1[0] - hi.0[0])).abs() < 1e-6, "same exported length");
    for bad in [-1.0, 12.5, f64::NAN] {
        p.hull.stem_radius_in = bad;
        assert!(p.validate().is_err(), "{bad}");
    }
    // The old name still reads.
    let text = std::fs::read_to_string("examples/canoe.toml")
        .unwrap()
        .replace("stem_radius_in", "keel_fillet_radius_in");
    let old = Params::parse(&text, Path::new("examples")).unwrap();
    assert_eq!(old.hull.stem_radius_in, 4.0);
}

/// GM per load case and of the empty canoe: every candidate carries one GM per case (their
/// minimum is gm_in) and the empty canoe's; stability.gm_empty_min_in gates the empty one.
#[test]
fn gm_per_load_case_and_empty_canoe_gate() {
    let (mut p, h) = example();
    p.search.thickness_in = [0.375, 0.5, 0.125];
    p.search.depth_in = [12.0, 13.0, 1.0];
    p.stability.gm_empty_min_in = None;
    let free = optimize::run(&p, &h).unwrap();
    for c in free.candidates.iter().filter(|c| c.verdict.is_some()) {
        assert_eq!(c.gm_cases_in.len(), p.load_cases.len());
        let m = c.gm_cases_in.iter().cloned().fold(f64::INFINITY, f64::min);
        assert_eq!(Some(m), c.gm_in);
        // Empty, the canoe is far stiffer than with kneeling paddlers aboard.
        assert!(c.gm_empty_in.unwrap() > c.gm_in.unwrap());
    }
    let w = free.winner.as_ref().unwrap();
    let ge = w.candidate.gm_empty_in.unwrap();
    assert!((w.empty.gm_in.unwrap() - ge).abs() < 0.05 * ge, "verify-grid empty GM matches");
    // A gate under every design's empty GM changes nothing; one above all of them rejects all.
    let lo = free.candidates.iter().filter_map(|c| c.gm_empty_in).fold(f64::INFINITY, f64::min);
    p.stability.gm_empty_min_in = Some(lo - 0.01);
    let same = optimize::run(&p, &h).unwrap();
    assert_eq!(same.winner.unwrap().candidate.hull_weight_lb, w.candidate.hull_weight_lb);
    p.stability.gm_empty_min_in = Some(100.0);
    let none = optimize::run(&p, &h).unwrap();
    assert!(none.winner.is_none());
    let text = jaguar::report::summary_lines(&none).join("\n");
    assert!(text.contains("empty-canoe GM"), "{text}");
}

/// A fine depth_tol_in drives the depth onto the freeboard gate (raised here so it governs); the
/// refined winner must still verify (freeboard is re-checked at 2n-1 stations during the
/// bisection). Without that re-check every case here missed by 0.003-0.009 in.
#[test]
fn fine_depth_tol_winner_verifies() {
    let (mut p, h) = example_gm0();
    p.search.depth_tol_in = Some(0.001);
    let mut fails = 0;
    for fb in [7.0, 9.0] {
        for stations in [21, 81] {
            p.gates.freeboard_min_in = fb;
            p.hull.stations = stations;
            let w = optimize::run(&p, &h).unwrap().winner.expect("winner");
            if !w.verified {
                fails += 1;
                eprintln!("fb {fb} {stations} st depth {}: {:?}", w.candidate.depth_in, w.verify_failure);
            }
        }
    }
    assert_eq!(fails, 0);
}

/// A wide generated hull (27.77 in waterline beam) with the team's crew and gates
/// (tests/fixtures), on the edge of the loaded-GM gate: 3 deg flare misses it, 4.5 deg passes;
/// 27.76 in passes at no flare in 3-5 deg with the default search margins.
fn team_wide_hull() -> (Params, Hull) {
    use jaguar::hullgen::{ShapeParams, generate};
    let text = std::fs::read_to_string("tests/fixtures/team_canoe.toml").unwrap();
    let mut p = Params::parse(&text, Path::new("tests/fixtures")).unwrap();
    p.shape_search = None;
    p.objective.kind = jaguar::params::ObjectiveKind::Weight;
    (p.hull.stations, p.hull.flare_deg, p.gates.freeboard_margin_in) = (41, 0.0, 1.0);
    (p.search.flare_deg, p.search.depth_in) = (Some([3.0, 5.0, 0.5]), [10.375, 18.375, 0.5]);
    let shape = ShapeParams {
        length_in: 216.0,
        beam_in: 27.77,
        draft_in: 7.5,
        fore_wp: 0.74,
        section: 0.9,
        fore_rocker: 0.37,
        aft_wp: 0.74,
        aft_rocker: 0.37,
        fore_len_frac: 0.5,
        aft_len_frac: 0.5,
        gunwale_radius_in: None,
        metrics: None,
    };
    let h = generate(&shape, 41, 21, "wide").unwrap().prepared(&p.hull).unwrap();
    (p, h)
}

/// The binding-constraint table: every gate, sorted by relative slack. On the wide hull loaded
/// GM binds: it is first, at the verification stations, and (verified) not negative.
#[test]
fn gate_slack_table_sorts_the_binding_gate_first() {
    let (p, h) = team_wide_hull();
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert!(w.verified, "{:?}", w.verify_failure);
    let g = &w.gates;
    assert!(g.windows(2).all(|x| x[0].relative <= x[1].relative));
    assert_eq!((g[0].name.as_str(), g[0].stations), ("loaded GM", 2 * p.hull.stations - 1));
    assert!(g[0].slack >= 0.0 && g[0].at_least && (g[0].slack - (g[0].value - g[0].limit)).abs() < 1e-12);
    for name in ["D/C verified", "D/C punching", "freeboard", "empty GM", "air pocket"] {
        assert!(g.iter().any(|x| x.name == name), "{name} missing: {g:#?}");
    }
    assert!(g.iter().all(|x| x.slack >= 0.0), "{g:#?}");
}

/// The in-search re-check at 2n-1 (Task 7): without search margins the wide hull's best
/// candidate passes the search by a hair and fails at 2n-1, so it is failed with the reason and
/// the next best taken (verified); with the default margins no best candidate fails there.
#[test]
fn best_candidate_is_rechecked_at_2n_minus_1() {
    let (mut p, h) = team_wide_hull();
    let r = optimize::run(&p, &h).unwrap();
    assert_eq!((r.recheck.checked, r.recheck.failed), (1, 0));
    let with_margin = r.winner.unwrap().candidate;
    let g = &mut p.gates;
    (
        g.gm_search_margin_in,
        g.freeboard_search_margin_in,
        g.gm_empty_search_margin_in,
        g.air_pocket_search_margin_in,
    ) = (0.0, 0.0, 0.0, 0.0);
    let r = optimize::run(&p, &h).unwrap();
    assert_eq!((r.recheck.checked, r.recheck.failed), (2, 1));
    let failed = r
        .candidates
        .iter()
        .find(|c| c.error.as_deref().is_some_and(|e| e.starts_with("loaded GM at 81 stations")));
    assert!(failed.is_some_and(|c| !c.passes), "the re-checked best is failed with its reason");
    let w = r.winner.unwrap();
    assert!(w.verified);
    assert_eq!(w.candidate.hull_weight_lb, with_margin.hull_weight_lb);
}

/// search.flare_tol_deg: on the wide hull, where loaded GM binds, the grid winner is 4.5 deg;
/// refined, the flare lands between grid values (just above 4.0, which fails) with a shallower
/// mold, lighter and verified. Without flare_tol_deg the flare stays on the grid.
#[test]
fn flare_tol_refines_flare_between_grid_values() {
    let (mut p, h) = team_wide_hull();
    let grid = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert_eq!((grid.candidate.flare_deg, grid.verified), (4.5, true));
    p.search.depth_tol_in = Some(0.01);
    let depth_only = optimize::run(&p, &h).unwrap().winner.unwrap().candidate;
    assert_eq!(depth_only.flare_deg, 4.5);
    p.search.flare_tol_deg = Some(0.01);
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.as_ref().unwrap();
    let c = &w.candidate;
    assert!(w.verified, "{:?}", w.verify_failure);
    assert!(c.flare_deg > 4.0 && c.flare_deg < 4.5, "{}", c.flare_deg);
    assert!(c.depth_in < grid.candidate.depth_in);
    assert!(c.hull_weight_lb < depth_only.hull_weight_lb.min(grid.candidate.hull_weight_lb));
    // The 0.01 deg less flare was tried and failed.
    let less = r.candidates.iter().find(|k| (k.flare_deg - (c.flare_deg - 0.01)).abs() < 1e-9);
    assert!(less.is_some_and(|k| !k.passes));
}

/// A fit-mode winner outside the tested densities (here forced by searching only above them)
/// carries a WARNING saying how far its strength is extrapolated; one inside does not.
#[test]
fn extrapolated_winner_density_is_warned() {
    let (mut p, h) = example_gm0();
    let r = optimize::run(&p, &h).unwrap();
    assert!(
        r.data_warnings.iter().all(|w| !w.contains("outside the tested densities")),
        "{:?}",
        r.data_warnings
    );
    p.search.density_pcf = [61.0, 64.0, 1.0];
    let r = optimize::run(&p, &h).unwrap();
    let rho = r.winner.as_ref().expect("winner above the tested 54.9-60 pcf").candidate.density_pcf;
    assert!(rho > 60.0);
    assert!(
        r.data_warnings
            .iter()
            .any(|w| w.contains(&format!("density {rho:.1} pcf is {:.1} pcf outside", rho - 60.0))),
        "{:?}",
        r.data_warnings
    );
}

/// Seats are planned at the candidate's own density. The team fixture lists its mix (M4 A,
/// 65.35 pcf) and a heavy 80 pcf one: until 2026-10-20 every density used seats planned at the
/// middle of that list (80 pcf), as in run opt-3215, so the M4 A winner's seats differed from a
/// run with M4 A alone. Now they are the same seats and the same design.
#[test]
fn winner_seats_are_planned_at_its_own_density() {
    let (mut p, h) = team_wide_hull();
    let w = optimize::run(&p, &h).unwrap().winner.unwrap();
    assert_eq!(w.candidate.mix.as_deref(), Some("M4 A"));
    p.material.mixes = Some(vec!["M4 A".into()]);
    let alone = optimize::run(&p, &h).unwrap().winner.unwrap();
    let seats = |w: &optimize::Winner| w.cases.iter().map(|k| k.seats_in.clone()).collect::<Vec<_>>();
    assert_eq!(seats(&w), seats(&alone));
    assert_eq!(
        (w.candidate.depth_in, w.candidate.hull_weight_lb),
        (alone.candidate.depth_in, alone.candidate.hull_weight_lb)
    );
}

/// Crew-CG sensitivity (a diagnostic): GM is linear in the assumed crew CG, so the predicted GM
/// at crew_cg + 2 in equals a re-analysis of the same design (same wall and seats) at that crew
/// CG; at the threshold the setting case's GM is exactly the gate. On the wide hull loaded GM
/// binds, so the threshold is within 0.5 in of the assumption and data_warnings says so.
#[test]
fn crew_cg_sensitivity_matches_a_reanalysis() {
    let (mut p, h) = team_wide_hull();
    let r = optimize::run(&p, &h).unwrap();
    let w = r.winner.unwrap();
    let s = w.crew_cg.clone().expect("sensitivity of the winner");
    let k = s.cases.iter().find(|k| k.name == s.threshold_case).unwrap();
    let at_threshold = k.gm_in[1] + k.slope * (s.threshold_in - s.crew_cg_in);
    assert!((at_threshold - p.stability.gm_min_in).abs() < 1e-9);
    assert!(s.threshold_in - s.crew_cg_in <= optimize::CREW_CG_WARN_IN);
    assert!(r.data_warnings.iter().any(|d| d.starts_with("WARNING crew_cg_in")), "{:?}", r.data_warnings);
    // The same design, seats given, crew CG 2 in higher.
    let c = &w.candidate;
    p.material.mixes = Some(vec![c.mix.clone().unwrap()]);
    p.search.thickness_in = [c.thickness_in, c.thickness_in, 0.125];
    (p.search.depth_in, p.search.flare_deg) =
        ([c.depth_in, c.depth_in, 0.5], Some([c.flare_deg, c.flare_deg, 0.5]));
    for (lc, k) in p.load_cases.iter_mut().zip(&w.cases) {
        lc.seats_in = Some(k.seats_in.clone());
    }
    p.stability.crew_cg_in += 2.0;
    let r2 = optimize::run(&p, &h).unwrap();
    let w2 = r2.winner.as_ref().or(r2.closest.as_ref().map(|c| &c.design)).unwrap();
    let s2 = w2.crew_cg.as_ref().unwrap();
    for (a, b) in s.cases.iter().zip(&s2.cases) {
        assert!(
            (a.gm_in[2] - b.gm_in[1]).abs() < 1e-9,
            "{}: predicted {} analysed {}",
            a.name,
            a.gm_in[2],
            b.gm_in[1]
        );
    }
}
