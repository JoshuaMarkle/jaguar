//! Mold boards of the example winner: stack, pieces, packing and DXF.

use jaguar::manufacture::{Layout, MoldSpec, Orientation, Pt, dxf_name, from_result, to_dxf};

fn example() -> String {
    std::fs::read_to_string("examples/result.json").unwrap()
}

fn bbox(p: &[Pt]) -> (Pt, Pt) {
    p.iter().fold(([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]), |(lo, hi), q| {
        ([lo[0].min(q[0]), lo[1].min(q[1])], [hi[0].max(q[0]), hi[1].max(q[1])])
    })
}

/// Every placed piece on its sheet, inside the gap margin, and every piece placed once.
fn check_sheets(l: &Layout) {
    let s = &l.spec;
    let mut seen = vec![0; l.pieces.len()];
    for sh in &l.sheets {
        for p in &sh.parts {
            seen[p.piece] += 1;
            let (lo, hi) = bbox(&p.outline);
            assert!(lo[0] >= s.gap_in - 1e-9 && lo[1] >= s.gap_in - 1e-9, "{} off the sheet", p.piece);
            assert!(
                hi[0] <= s.sheet_length_in - s.gap_in + 1e-9 && hi[1] <= s.sheet_width_in - s.gap_in + 1e-9
            );
            let a = l.pieces[p.piece].area_in2;
            let n = p.outline.len();
            let placed: f64 = 0.5
                * (0..n)
                    .map(|i| {
                        p.outline[i][0] * p.outline[(i + 1) % n][1]
                            - p.outline[(i + 1) % n][0] * p.outline[i][1]
                    })
                    .sum::<f64>();
            assert!((placed - a).abs() < 1e-6 * a.max(1.0), "a quarter turn keeps the area");
        }
    }
    assert!(seen.iter().all(|&k| k == 1), "each piece placed exactly once");
}

#[test]
fn flat_layers_stack_from_the_gunwale_and_fit_the_sheets() {
    let spec = MoldSpec::default();
    let l = from_result(&example(), &spec).unwrap();
    let st = &l.stack;
    // Board k sits k (foam + board) above the floor; boards until the keel.
    let pitch = spec.foam_in + spec.board_in;
    for (k, ly) in l.layers.iter().enumerate() {
        assert!((ly.pos_in - k as f64 * pitch).abs() < 1e-9);
        assert!(!ly.pieces.is_empty());
    }
    assert_eq!(st.boards, l.layers.len());
    assert!((st.boards - 1) as f64 * pitch < st.height_in && st.height_in <= st.boards as f64 * pitch + 1e-9);
    assert!(st.foam_layers >= st.boards - 1);
    // The floor board is the inner gunwale: as wide as the inner beam, symmetric, pieces end to end.
    let l1: Vec<&_> = l.layers[0].pieces.iter().map(|&i| &l.pieces[i]).collect();
    let w = l1.iter().map(|p| 2.0 * bbox(&p.outline).1[1]).fold(0.0, f64::max);
    assert!((w - st.beam_in).abs() < 0.05, "floor board {w} vs inner beam {}", st.beam_in);
    assert!(st.beam_in < 30.0 && st.beam_in > 20.0);
    for p in &l1 {
        let (lo, hi) = bbox(&p.outline);
        assert!((lo[1] + hi[1]).abs() < 1e-9, "{} symmetric about the centreline", p.label);
    }
    for w in l1.windows(2) {
        assert!((bbox(&w[0].outline).1[0] - bbox(&w[1].outline).0[0]).abs() < 1e-9, "joint");
    }
    let span = bbox(&l1[l1.len() - 1].outline).1[0] - bbox(&l1[0].outline).0[0];
    assert!((span - st.length_in).abs() < 0.5, "floor board spans the hollow run");
    // Higher boards are smaller (the hull narrows toward the keel).
    let area = |k: usize| l.layers[k].pieces.iter().map(|&i| l.pieces[i].area_in2).sum::<f64>();
    for k in 1..l.layers.len() {
        assert!(area(k) < area(k - 1) + 1e-6, "layer {} bigger than the one below", k + 1);
    }
    check_sheets(&l);
    assert!(l.utilization > 0.5 && l.utilization <= 1.0);
}

#[test]
fn upright_ribs_every_pitch_along_the_hollow_run() {
    let spec = MoldSpec { orientation: Orientation::Vertical, ..MoldSpec::default() };
    let l = from_result(&example(), &spec).unwrap();
    let pitch = spec.foam_in + spec.board_in;
    let n = ((l.stack.length_in - spec.board_in) / pitch + 1e-9).floor() as usize + 1;
    assert_eq!(l.layers.len(), n);
    assert_eq!(l.pieces.len(), n);
    for w in l.layers.windows(2) {
        assert!((w[1].pos_in - w[0].pos_in - pitch).abs() < 1e-9);
    }
    // Each rib stands on the floor (y from -half gunwale to +half) and reaches the inner keel.
    let mid = &l.pieces[n / 2];
    let (lo, hi) = bbox(&mid.outline);
    assert!(lo[1].abs() < 1e-9 && (lo[0] + hi[0]).abs() < 1e-9);
    assert!(
        hi[1] > 10.0 && hi[1] < l.stack.depth_in - l.stack.wall_in + 1e-6,
        "midships rib {} in high",
        hi[1]
    );
    check_sheets(&l);
}

#[test]
fn dxf_is_r12_with_one_outline_per_piece_and_deterministic() {
    let l = from_result(&example(), &MoldSpec::default()).unwrap();
    let a = to_dxf(&l, 0).unwrap();
    assert_eq!(a, to_dxf(&from_result(&example(), &MoldSpec::default()).unwrap(), 0).unwrap());
    assert!(a.contains("AC1009") && a.ends_with("EOF\n"));
    let polylines = a.matches("\nPOLYLINE\n").count() + usize::from(a.starts_with("0\nPOLYLINE"));
    assert_eq!(polylines, l.sheets[0].parts.len() + 1, "pieces plus the sheet edge");
    let cut = a.matches("POLYLINE\n8\nCUT\n").count();
    assert_eq!(cut, l.sheets[0].parts.len());
    assert!(a.is_ascii());
    assert!(to_dxf(&l, l.sheets.len()).is_err());
    assert_eq!(dxf_name(&l, 0), "opt-6240-sym-f2_sheet_01.dxf");
}

#[test]
fn bad_settings_and_tiny_sheets_fail() {
    let ex = example();
    assert!(from_result(&ex, &MoldSpec { foam_in: 0.0, ..MoldSpec::default() }).is_err());
    assert!(from_result(&ex, &MoldSpec { gap_in: -1.0, ..MoldSpec::default() }).is_err());
    let tiny = MoldSpec { sheet_length_in: 12.0, sheet_width_in: 12.0, ..MoldSpec::default() };
    let e = from_result(&ex, &tiny).unwrap_err().to_string();
    assert!(e.contains("does not fit"), "{e}");
}
