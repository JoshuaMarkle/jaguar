//! Drawing sheets (src/drawing.rs, src/sheets.rs, src/drafting.rs) on a small board-mold design
//! (tests/fixtures/drawing_canoe.toml, run once): determinism, dimensions read back against the
//! result rather than constants, scale fidelity measured in the PDF, the mold stack and nesting,
//! the BOM, view toggles, ANSI B, and a source scan for index slicing of station / seat vectors.

use jaguar::drafting::{to_dxf, to_pdf, to_svg};
use jaguar::params::{BomOverride, BomRow, Drawing, SheetSize};
use jaguar::sheets::{Drawings, render};
use serde_json::Value;

const STAMP: &str = "JAGUAR test";

/// The fixture's result.json text (one run for every test).
fn result() -> &'static str {
    static R: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    R.get_or_init(|| {
        let p =
            jaguar::params::Params::load(std::path::Path::new("tests/fixtures/drawing_canoe.toml")).unwrap();
        let r = jaguar::run(&p, &|_| {}).unwrap();
        assert!(r.winner.is_some(), "the fixture must have a winner");
        serde_json::to_string(&r).unwrap()
    })
}

fn v() -> Value {
    serde_json::from_str(result()).unwrap()
}

fn spec() -> Drawing {
    jaguar::drawing::spec_of(&v(), None).unwrap()
}

/// The fixture drawn with its own [drawing] inputs (rendered once).
fn sheets() -> &'static Drawings {
    static D: std::sync::OnceLock<Drawings> = std::sync::OnceLock::new();
    D.get_or_init(|| render(result(), spec(), STAMP).unwrap())
}

/// Every marked dimension in a PDF: (text, model value, scale, measured paper length in).
fn pdf_dims(pdf: &[u8]) -> Vec<(String, f64, f64, f64)> {
    let s = String::from_utf8_lossy(pdf);
    let mut out = vec![];
    for chunk in s.split("/Dim <</V (").skip(1) {
        let (text, rest) = chunk.split_once(") /M ").unwrap();
        let (m, rest) = rest.split_once(" /S ").unwrap();
        let (sc, rest) = rest.split_once(" /A ").unwrap();
        let (axis, rest) = rest.split_once(" /P [").unwrap();
        let axis: usize = axis.parse().unwrap();
        let p: Vec<f64> = rest.split_once(']').unwrap().0.split(' ').map(|x| x.parse().unwrap()).collect();
        let len = (p[2 + axis] - p[axis]).abs() / 72.0;
        out.push((text.to_string(), m.parse().unwrap(), sc.parse().unwrap(), len));
    }
    out
}

#[test]
fn sheets_are_byte_identical_across_runs() {
    let a = sheets();
    let b = render(result(), spec(), STAMP).unwrap();
    for (x, y) in [(&a.canoe, &b.canoe), (&a.mold, &b.mold)] {
        assert_eq!(to_pdf(x), to_pdf(y));
        assert_eq!(to_svg(x), to_svg(y));
        assert_eq!(to_dxf(x), to_dxf(y));
    }
    let pdf = String::from_utf8_lossy(&to_pdf(&a.canoe)).into_owned();
    assert!(!pdf.contains("CreationDate") && !pdf.contains("/ID"), "no timestamps or random IDs");
    assert!(pdf.contains("(JAGUAR test)"), "the version stamp is on the sheet");
}

#[test]
fn key_dimensions_come_from_the_design() {
    let r = v();
    let w = &r["winner"];
    let c = &w["candidate"];
    let hull = &r["winner_hull"];
    let dims = jaguar::sheets::measured_dims(&sheets().canoe);
    let has = |model: f64, what: &str| {
        assert!(
            dims.iter().any(|d| (d.2 - model).abs() < 1e-9 && d.0 == format!("{model:.2}")),
            "{what} {model} not dimensioned: {dims:?}"
        )
    };
    has(hull["length"].as_f64().unwrap(), "length overall");
    has(w["stands"]["x_stand_in"].as_f64().unwrap(), "stand from the bow");
    has(w["stands"]["spacing_in"].as_f64().unwrap(), "stand spacing");
    let m = &sheets().model;
    has(m.beam, "max beam");
    // The beam is the analysis grid's gunwale: within its fine stations of the run's beam.
    assert!((m.beam - c["beam_in"].as_f64().unwrap()).abs() < 0.05, "{} vs {}", m.beam, c["beam_in"]);
    // Lettering placement from [lettering] (the freeboard gate's own spans).
    let p: jaguar::params::Params = serde_json::from_value(r["params"].clone()).unwrap();
    for s in p.lettering.as_ref().unwrap().spans(hull["length"].as_f64().unwrap()).unwrap() {
        has(s.x0_in, "lettering start");
        has(s.x1_in - s.x0_in, "lettering length");
    }
    // Laminate: total wall at laminate precision.
    let t = c["thickness_in"].as_f64().unwrap();
    assert!(dims.iter().any(|d| (d.2 - t).abs() < 1e-12 && d.0 == format!("{t:.3}")), "wall {t}");
    // Mold sheet: overall stack height = depth - wall.
    let md = jaguar::sheets::measured_dims(&sheets().mold);
    let h = c["depth_in"].as_f64().unwrap() - t;
    assert!(md.iter().any(|d| (d.2 - h).abs() < 1e-9), "mold height {h}: {md:?}");
}

#[test]
fn printed_distance_over_scale_is_the_model_dimension() {
    for doc in [&sheets().canoe, &sheets().mold] {
        let dims = pdf_dims(&to_pdf(doc));
        assert!(dims.len() > 15, "{}", dims.len());
        for (text, model, scale, len) in dims {
            // Points are written to 1/1000 pt: 1.4e-5 in on the paper.
            let tol = 2.0e-5 * scale + 1e-9 * model.abs();
            assert!(
                (len * scale - model).abs() <= tol,
                "{text}: {len} in x {scale} = {} vs {model}",
                len * scale
            );
        }
    }
    // The paper is the sheet size: 34 x 22 in.
    let pdf = String::from_utf8_lossy(&to_pdf(&sheets().canoe)).into_owned();
    assert!(pdf.contains("/MediaBox [0 0 2448 1584]"));
}

#[test]
fn mold_layers_are_whole_boards_and_nest_inside_the_margins() {
    let r = v();
    let c = &r["winner"]["candidate"];
    let m = &sheets().model;
    let mo = &m.mold;
    let board = r["params"]["search"]["mold_board_in"].as_f64().unwrap();
    assert_eq!(mo.board_in, board);
    let height = c["depth_in"].as_f64().unwrap() - c["thickness_in"].as_f64().unwrap();
    assert_eq!(mo.layers.len() as f64 * mo.board_in, mo.height_in);
    assert!((mo.height_in - height).abs() < 1e-9, "{} vs {height}", mo.height_in);
    for (k, l) in mo.layers.iter().enumerate() {
        assert_eq!(l.label, format!("L{}", k + 1), "numbered bottom (floor) to top");
        assert!(!l.pieces.is_empty() && l.pieces.iter().all(|p| p.len() >= 3), "{} empty", l.label);
        assert!(l.x1 > l.x0 && l.max_width > 0.0);
    }
    let pieces: usize = mo.layers.iter().map(|l| l.pieces.len()).sum();
    assert_eq!(mo.sheets.iter().map(|s| s.parts.len()).sum::<usize>(), pieces, "every piece nested once");
    let [sl, sw] = mo.sheet_in;
    for sh in &mo.sheets {
        for (label, o) in &sh.parts {
            for p in o {
                assert!(
                    p[0] >= mo.edge_in - 1e-9
                        && p[1] >= mo.edge_in - 1e-9
                        && p[0] <= sl - mo.edge_in + 1e-9
                        && p[1] <= sw - mo.edge_in + 1e-9,
                    "{label} at {p:?} is in the edge margin"
                );
            }
        }
    }
    // The canoe BOM's foam row is the nested sheet count, cross-referenced to sheet 2.
    let foam = m.bom.iter().find(|l| l.part == "F-01").unwrap();
    assert_eq!(foam.qty, mo.sheets.len() as f64);
    assert!(foam.desc.contains("SEE SHEET 2"));
}

#[test]
fn a_mold_off_the_board_pitch_fails_naming_the_board() {
    let s = Drawing { mold_board_in: Some(2.0), ..spec() };
    let e = format!("{:#}", jaguar::drawing::model(result(), s).unwrap_err());
    assert!(e.contains("not a whole number of 2 in boards"), "{e}");
}

#[test]
fn wall_buildup_is_the_punching_checks_schedule() {
    let r = v();
    let p: jaguar::params::Params = serde_json::from_value(r["params"].clone()).unwrap();
    let lam = &sheets().model.laminate;
    let t = r["winner"]["candidate"]["thickness_in"].as_f64().unwrap();
    assert_eq!(lam.lifts, p.punching.layers.unwrap());
    assert_eq!(lam.meshes.len(), 2);
    // The farthest mesh from the knee (inside) face is the punching check's d.
    let far = lam.meshes.iter().map(|m| m.1).fold(0.0, f64::max);
    assert!((far - p.punching.d_over_t().unwrap() * t).abs() < 1e-12);
    assert!((lam.d_in - far).abs() < 1e-12);
    for &(k, cin, cout) in &lam.meshes {
        assert!((cin + cout - t).abs() < 1e-12 && (cin - t * f64::from(k) / 3.0).abs() < 1e-12);
    }
    let texts = sheets().canoe.texts().join("\n");
    assert!(texts.contains("M-01 MESH AFTER LIFT 2") && texts.contains("LIFT 3 (0.125), C-01"), "{texts}");
}

#[test]
fn bom_quantities_are_computed_with_visible_factors() {
    let r = v();
    let c = &r["winner"]["candidate"];
    let m = &sheets().model;
    let s = &m.spec;
    let conc = &m.mixes[0];
    // The wall concrete is the hull weight's own sections: volume x density = weight.
    assert!(
        (conc.volume_in3 * conc.density_pcf / 1728.0 / c["hull_weight_lb"].as_f64().unwrap() - 1.0).abs()
            < 1e-9
    );
    let row = |p: &str| m.bom.iter().find(|l| l.part == p).unwrap();
    assert!((row("C-01").qty - conc.volume_in3 / 1728.0 * (1.0 + s.concrete_waste)).abs() < 1e-12);
    assert!((row("M-01").qty - m.developed_ft2 * 2.0 * (1.0 + s.mesh_overlap)).abs() < 1e-12);
    let sealer = m.developed_ft2 * f64::from(s.sealer_sides * s.sealer_coats) / s.sealer_coverage_ft2_per_gal;
    assert!((row("S-01").qty - sealer).abs() < 1e-12);
    let vinyl: f64 = m.graphics.iter().map(|g| (g.x1 - g.x0) * g.height * 2.0).sum::<f64>() / 144.0;
    assert!((row("V-01").qty - vinyl * (1.0 + s.vinyl_waste)).abs() < 1e-12);
    // Overrides and hand rows: drawn, flagged, and recorded in every file's metadata.
    let s2 = Drawing {
        bom_overrides: vec![BomOverride {
            part_no: "S-01".into(),
            qty: Some(2.0),
            description: None,
            reason: "two quarts on hand".into(),
        }],
        bom_rows: vec![BomRow {
            part_no: "X-01".into(),
            qty: 1.0,
            unit: "EA".into(),
            description: "Sponsor decal".into(),
        }],
        ..spec()
    };
    let d = render(result(), s2, STAMP).unwrap();
    let s1 = d.model.bom.iter().find(|l| l.part == "S-01").unwrap();
    assert_eq!(s1.qty, 2.0);
    assert!(s1.overridden.as_deref().unwrap().contains(&format!("computed {:.2} GAL", sealer)));
    let pdf = String::from_utf8_lossy(&to_pdf(&d.canoe)).into_owned();
    assert!(pdf.contains("/JaguarBOMRecord (X-01: entered by hand") && pdf.contains("S-01: computed"));
    assert!(to_svg(&d.canoe).contains("two quarts on hand") && to_dxf(&d.canoe).contains("S-01: computed"));
    assert!(d.canoe.texts().contains(&"S-01 (O)") && d.canoe.texts().contains(&"X-01 (H)"));
    let bad = Drawing {
        bom_overrides: vec![BomOverride {
            part_no: "Z-09".into(),
            qty: Some(1.0),
            description: None,
            reason: String::new(),
        }],
        ..spec()
    };
    assert!(
        format!("{:#}", jaguar::drawing::model(result(), bad).unwrap_err()).contains("no BOM row 'Z-09'")
    );
}

#[test]
fn optional_views_off_and_ansi_b_still_compose() {
    let off = Drawing { body_plan: false, offsets_table: false, stands_diagram: false, ..spec() };
    let d = render(result(), off, STAMP).unwrap();
    let t = d.canoe.texts();
    assert!(!t.contains(&"BODY PLAN") && !t.contains(&"SUPPORT / STANDS DIAGRAM"));
    assert!(t.contains(&"ELEVATION") && t.iter().any(|x| x.starts_with("SECTION A-A")));
    let b = render(result(), Drawing { sheet_size: SheetSize::B, ..spec() }, STAMP).unwrap();
    let pdf = String::from_utf8_lossy(&to_pdf(&b.canoe)).into_owned();
    assert!(pdf.contains("/MediaBox [0 0 1224 792]"));
    for t in ["ELEVATION", "PLAN", "BODY PLAN", "ISOMETRIC VIEW, DO NOT SCALE", "SUPPORT / STANDS DIAGRAM"] {
        assert!(b.canoe.texts().contains(&t), "{t} on ANSI B");
    }
    // Every view states the scale it was drawn at; on B the lines plan is scaled down from 1:25.
    assert!(b.canoe.texts().iter().any(|x| x.starts_with("SCALE 1:") && *x != "SCALE 1:25"));
}

#[test]
fn unofficial_school_name_warns_and_missing_title_text_fails() {
    assert!(sheets().warnings.is_empty(), "{:?}", sheets().warnings);
    let d = render(result(), Drawing { school: "UVA".into(), ..spec() }, STAMP).unwrap();
    assert!(d.warnings.iter().any(|w| w.contains("\"UVA\" is not the official name")), "{:?}", d.warnings);
    let pdf = String::from_utf8_lossy(&to_pdf(&d.canoe)).into_owned();
    assert!(pdf.contains("/JaguarWarnings"));
    let e = render(result(), Drawing { drawn_by: String::new(), ..spec() }, STAMP).err().unwrap().to_string();
    assert!(e.contains("drawn_by"), "{e}");
}

/// Index-based slicing of station or seat vectors (`xs[1..n]`, `seats[..k]`) has bitten this
/// project before; the drawing code must not do it at all (iterate, or first()/last()/get()).
#[test]
fn drawing_code_never_slices_station_or_seat_vectors() {
    let suspect = |name: &str| {
        let n = name.to_ascii_lowercase();
        n.contains("station")
            || n.contains("seat")
            || n.contains("x_in")
            || matches!(n.as_str(), "x" | "xs" | "xo")
    };
    for file in ["src/drafting.rs", "src/drawing.rs", "src/sheets.rs"] {
        let src = std::fs::read_to_string(file).unwrap();
        for (ln, line) in src.lines().enumerate() {
            let b = line.as_bytes();
            for (i, &ch) in b.iter().enumerate() {
                if ch != b'[' || i == 0 {
                    continue;
                }
                // The identifier right before the bracket (a field path's last part).
                let start = (0..i).rev().take_while(|&j| b[j].is_ascii_alphanumeric() || b[j] == b'_').last();
                let Some(s) = start else { continue };
                let ident = &line[s..i];
                let inner: String = line[i + 1..].chars().take_while(|&c| c != ']').collect();
                assert!(
                    !(suspect(ident) && inner.contains("..")),
                    "{file}:{}: index slicing of `{ident}`: {}",
                    ln + 1,
                    line.trim()
                );
            }
            for pat in [".get(", ".get_mut("] {
                if let Some(k) = line.find(pat) {
                    let before: String = line[..k]
                        .chars()
                        .rev()
                        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
                        .collect();
                    let ident: String = before.chars().rev().collect();
                    let arg: String = line[k + pat.len()..].chars().take_while(|&c| c != ')').collect();
                    assert!(!(suspect(&ident) && arg.contains("..")), "{file}:{}: {}", ln + 1, line.trim());
                }
            }
        }
    }
}
