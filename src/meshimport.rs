//! A canoe made in CAD, read as a triangle mesh (STL binary/ASCII, OBJ, or a STEP solid / surface
//! meshed by `stepimport`), turned into a `jaguar-hull/1` table so it is analysed like any other
//! hull.
//!
//! Pure (bytes in, hull out); the CLI and web read the file.
//!
//! * Units: `in`, `mm`, `cm`, `m`, `ft`, the unit a STEP file declares, or auto from the length
//!   (a canoe is 12-30 ft).
//! * Axes: length = the longest bounding-box side, up = the shortest (beam > depth for a canoe),
//!   or given. Up points away from the keel: the end whose cross-sections are narrower is the
//!   bottom. The bow is at the low end of the length axis unless `bow_at_max`.
//! * Slicing: at each station plane the mesh's section is cut by each waterline; the largest
//!   crossing on each side is the outer (mold) surface. Half-breadth = the mean of the two
//!   sides (the table is symmetric); their difference is reported as asymmetry.
//! * Depth: the gunwale (sheer) is the top of each section; the table runs from the keel to
//!   the lowest sheer over the middle 80% of the length (a flat gunwale, as the analysis
//!   assumes). A sheer that rises at the ends is reported, the part above is not analysed.
//! * Wall (a solid mesh, both faces drawn): on each side the next crossing inside the outer one
//!   is the inner face; their horizontal gap times the cosine of the side's angle from vertical
//!   is the wall thickness there. Reported as the median over the middle 60% of the length and
//!   20-80% of the depth, with min / max, and cross-checked by 2 x volume / area (a thin closed
//!   shell). A surface mesh (one face) has no thickness: None.

use crate::hull::{FORMAT, Hull, linspace};
use anyhow::{Context, Result, bail, ensure};
use serde::Serialize;

type V3 = [f64; 3];

/// How to read the mesh. `None` = detect.
#[derive(Debug, Clone, Default)]
pub struct MeshOptions {
    /// Inches per model unit.
    pub units: Option<f64>,
    /// How `units` was decided, for the report (default: "given").
    pub units_from: Option<String>,
    /// Axis index (0 = x, 1 = y, 2 = z) of the length and of up.
    pub length_axis: Option<usize>,
    pub up_axis: Option<usize>,
    /// Up points the other way (keel at the high end).
    pub flip_up: bool,
    /// Bow at the high end of the length axis.
    pub bow_at_max: bool,
    pub stations: usize,
    pub waterlines: usize,
}

impl MeshOptions {
    pub fn new() -> Self {
        MeshOptions { stations: 81, waterlines: 41, ..Default::default() }
    }
}

/// Inches per unit for a unit name.
pub fn unit_scale(name: &str) -> Result<f64> {
    Ok(match name {
        "in" | "inch" | "inches" => 1.0,
        "mm" => 1.0 / 25.4,
        "cm" => 1.0 / 2.54,
        "m" => 1000.0 / 25.4,
        "ft" => 12.0,
        u => bail!("unknown unit '{u}' (in, mm, cm, m, ft)"),
    })
}

/// Axis index from a name.
pub fn axis_index(name: &str) -> Result<usize> {
    Ok(match name {
        "x" => 0,
        "y" => 1,
        "z" => 2,
        a => bail!("unknown axis '{a}' (x, y, z)"),
    })
}

/// What was measured on the mesh (inches).
#[derive(Debug, Clone, Serialize)]
pub struct Measured {
    pub units: String,
    pub length_axis: char,
    pub up_axis: char,
    pub triangles: usize,
    pub length_in: f64,
    pub beam_in: f64,
    /// Table top: the lowest sheer over the middle 80% of the length.
    pub depth_in: f64,
    /// Highest sheer anywhere (above `depth_in` = sheer rising toward the ends).
    pub sheer_max_in: f64,
    /// Wall thickness normal to the wall: median, min, max over the middle of the hull.
    /// None for a surface mesh (no inner face).
    pub thickness_in: Option<f64>,
    pub thickness_min_in: Option<f64>,
    pub thickness_max_in: Option<f64>,
    /// 2 x volume / area (thin closed shell); None when the mesh is not closed.
    pub thickness_volume_in: Option<f64>,
    /// Largest port / starboard half-breadth difference.
    pub asymmetry_in: f64,
    pub notes: Vec<String>,
}

/// Triangles of a binary or ASCII STL.
pub fn parse_stl(bytes: &[u8]) -> Result<Vec<[V3; 3]>> {
    let ascii = bytes.len() >= 5 && bytes[..5].eq_ignore_ascii_case(b"solid") && {
        // Binary STLs may also start with "solid": trust the size formula first.
        let n = if bytes.len() >= 84 {
            u32::from_le_bytes([bytes[80], bytes[81], bytes[82], bytes[83]]) as usize
        } else {
            0
        };
        bytes.len() != 84 + 50 * n
    };
    if ascii {
        let text = std::str::from_utf8(bytes).context("ASCII STL is not UTF-8")?;
        let mut v: Vec<V3> = Vec::new();
        for line in text.lines() {
            let mut w = line.split_whitespace();
            if w.next() == Some("vertex") {
                let c: Vec<f64> =
                    w.map(|s| s.parse::<f64>()).collect::<Result<_, _>>().context("STL vertex")?;
                ensure!(c.len() == 3, "STL vertex needs 3 numbers");
                v.push([c[0], c[1], c[2]]);
            }
        }
        ensure!(!v.is_empty() && v.len().is_multiple_of(3), "ASCII STL: no facets");
        return Ok(v.chunks(3).map(|c| [c[0], c[1], c[2]]).collect());
    }
    ensure!(bytes.len() >= 84, "STL too short");
    let n = u32::from_le_bytes([bytes[80], bytes[81], bytes[82], bytes[83]]) as usize;
    ensure!(bytes.len() >= 84 + 50 * n, "binary STL: {n} facets declared, file too short");
    let f = |o: usize| f32::from_le_bytes([bytes[o], bytes[o + 1], bytes[o + 2], bytes[o + 3]]) as f64;
    Ok((0..n)
        .map(|i| {
            let o = 84 + 50 * i + 12;
            let p = |k: usize| [f(o + 12 * k), f(o + 12 * k + 4), f(o + 12 * k + 8)];
            [p(0), p(1), p(2)]
        })
        .collect())
}

/// Triangles of an OBJ (`v` and `f` lines; polygons fanned; negative indices allowed).
pub fn parse_obj(text: &str) -> Result<Vec<[V3; 3]>> {
    let mut v: Vec<V3> = Vec::new();
    let mut tris = Vec::new();
    for line in text.lines() {
        let mut w = line.split_whitespace();
        match w.next() {
            Some("v") => {
                let c: Vec<f64> =
                    w.take(3).map(|s| s.parse::<f64>()).collect::<Result<_, _>>().context("OBJ vertex")?;
                ensure!(c.len() == 3, "OBJ vertex needs 3 numbers");
                v.push([c[0], c[1], c[2]]);
            }
            Some("f") => {
                let idx: Vec<usize> = w
                    .map(|s| -> Result<usize> {
                        let i: i64 = s.split('/').next().unwrap_or("").parse().context("OBJ face index")?;
                        let k = if i < 0 { v.len() as i64 + i } else { i - 1 };
                        ensure!(k >= 0 && (k as usize) < v.len(), "OBJ face index {i} out of range");
                        Ok(k as usize)
                    })
                    .collect::<Result<_>>()?;
                for k in 1..idx.len().saturating_sub(1) {
                    tris.push([v[idx[0]], v[idx[k]], v[idx[k + 1]]]);
                }
            }
            _ => {}
        }
    }
    ensure!(!tris.is_empty(), "OBJ: no faces");
    Ok(tris)
}

/// A CAD file's triangles by its extension (stl, obj, step / stp), the units it declares (STEP:
/// inches per unit and the unit's name), and a one-line description of what was read.
#[allow(clippy::type_complexity)]
pub fn read_cad(ext: &str, bytes: &[u8]) -> Result<(Vec<[V3; 3]>, Option<(f64, String)>, String)> {
    Ok(match ext.to_ascii_lowercase().as_str() {
        "stl" => {
            let t = parse_stl(bytes)?;
            let d = format!("STL, {} triangles", t.len());
            (t, None, d)
        }
        "obj" => {
            let t = parse_obj(&String::from_utf8_lossy(bytes))?;
            let d = format!("OBJ, {} triangles", t.len());
            (t, None, d)
        }
        "step" | "stp" => {
            let m = crate::stepimport::parse_step(&String::from_utf8_lossy(bytes))?;
            let d = format!("STEP, {} faces meshed into {} triangles", m.faces, m.tris.len());
            (m.tris, m.units, d)
        }
        e => bail!("CAD file must be .step, .stp, .stl or .obj (got .{e})"),
    })
}

/// Section of the mesh at x = `x0`: segments (y, z) - (y, z).
fn section(tris: &[[V3; 3]], x0: f64) -> Vec<[[f64; 2]; 2]> {
    let mut out = Vec::new();
    for t in tris {
        let d = t.map(|p| p[0] - x0);
        let mut pts: Vec<[f64; 2]> = Vec::with_capacity(2);
        for (a, b) in [(0, 1), (1, 2), (2, 0)] {
            if (d[a] < 0.0) != (d[b] < 0.0) {
                let s = d[a] / (d[a] - d[b]);
                pts.push([t[a][1] + s * (t[b][1] - t[a][1]), t[a][2] + s * (t[b][2] - t[a][2])]);
            }
        }
        if pts.len() == 2 {
            out.push([pts[0], pts[1]]);
        }
    }
    out
}

/// Crossings y of the section segments with the line z = `z0`.
fn crossings(seg: &[[[f64; 2]; 2]], z0: f64) -> Vec<f64> {
    seg.iter()
        .filter(|[a, b]| (a[1] < z0) != (b[1] < z0))
        .map(|[a, b]| a[0] + (z0 - a[1]) / (b[1] - a[1]) * (b[0] - a[0]))
        .collect()
}

/// The hull table of a canoe mesh, and what was measured.
pub fn hull_from_mesh(raw: &[[V3; 3]], name: &str, o: &MeshOptions) -> Result<(Hull, Measured)> {
    ensure!(raw.len() >= 4, "mesh has {} triangles", raw.len());
    ensure!(raw.iter().flatten().flatten().all(|c| c.is_finite()), "mesh has non-finite coordinates");
    ensure!(o.stations >= 11 && o.waterlines >= 5, "need >= 11 stations and >= 5 waterlines");
    let (mut lo, mut hi) = ([f64::INFINITY; 3], [f64::NEG_INFINITY; 3]);
    for p in raw.iter().flatten() {
        for a in 0..3 {
            lo[a] = lo[a].min(p[a]);
            hi[a] = hi[a].max(p[a]);
        }
    }
    let ext: Vec<f64> = (0..3).map(|a| hi[a] - lo[a]).collect();
    let mut order: Vec<usize> = (0..3).collect();
    order.sort_by(|&a, &b| ext[b].total_cmp(&ext[a]));
    let la = o.length_axis.unwrap_or(order[0]);
    let ua = o.up_axis.unwrap_or(if order[2] == la { order[1] } else { order[2] });
    ensure!(la != ua && la < 3 && ua < 3, "length and up must be different axes");
    let ba = 3 - la - ua;
    let mut notes = Vec::new();
    // Units: a canoe is 12-30 ft long.
    let (scale, units) = match o.units {
        Some(s) => (s, o.units_from.clone().unwrap_or_else(|| format!("{s} in/unit (given)"))),
        None => {
            let l = ext[la];
            let cand =
                [("in", 1.0), ("mm", 1.0 / 25.4), ("cm", 1.0 / 2.54), ("m", 1000.0 / 25.4), ("ft", 12.0)];
            let fits: Vec<_> = cand.iter().filter(|(_, s)| (144.0..=360.0).contains(&(l * s))).collect();
            match fits.as_slice() {
                [(u, s)] => (*s, format!("{u} (detected from the length)")),
                [] => bail!(
                    "cannot tell the units: length {l:.3} is not 12-30 ft in in/mm/cm/m/ft; give --units"
                ),
                many => bail!(
                    "units are ambiguous ({}): give --units",
                    many.iter().map(|(u, _)| *u).collect::<Vec<_>>().join(", ")
                ),
            }
        }
    };
    // Canoe frame: x from the bow, y across from the beam centre, z up from the keel.
    let yc = 0.5 * (lo[ba] + hi[ba]);
    let to_canoe = |p: &V3, up_sign: f64, bow_max: bool| -> V3 {
        let x = if bow_max { hi[la] - p[la] } else { p[la] - lo[la] };
        let z = if up_sign > 0.0 { p[ua] - lo[ua] } else { hi[ua] - p[ua] };
        [x * scale, (p[ba] - yc) * scale, z * scale]
    };
    // Up sign: the keel end is the narrower one (beam of the vertices in the bottom / top 15%).
    let up_sign = {
        let h = ext[ua];
        let width = |near_low: bool| {
            raw.iter()
                .flatten()
                .filter(|p| if near_low { p[ua] - lo[ua] < 0.15 * h } else { hi[ua] - p[ua] < 0.15 * h })
                .map(|p| (p[ba] - yc).abs())
                .fold(0.0, f64::max)
        };
        let s = if width(true) <= width(false) { 1.0 } else { -1.0 };
        if o.flip_up { -s } else { s }
    };
    let tris: Vec<[V3; 3]> = raw.iter().map(|t| t.map(|p| to_canoe(&p, up_sign, o.bow_at_max))).collect();
    let length = ext[la] * scale;
    let beam = ext[ba] * scale;
    // Stations (the tips closed), sections, sheer per station.
    let x = linspace(0.0, length, o.stations);
    let secs: Vec<_> = x.iter().map(|&xi| section(&tris, xi)).collect();
    let sheer: Vec<Option<f64>> = secs
        .iter()
        .map(|s| {
            s.iter().flatten().map(|p| p[1]).fold(None, |m: Option<f64>, z| Some(m.map_or(z, |m| m.max(z))))
        })
        .collect();
    let mid = |xi: f64, f: f64| (xi - 0.5 * length).abs() <= 0.5 * f * length;
    let depth = x
        .iter()
        .zip(&sheer)
        .filter(|(xi, _)| mid(**xi, 0.8))
        .filter_map(|(_, s)| *s)
        .fold(f64::INFINITY, f64::min);
    ensure!(depth.is_finite() && depth > 0.0, "no sections through the middle of the mesh");
    let sheer_max = sheer.iter().flatten().fold(0.0f64, |a, b| a.max(*b));
    if sheer_max > depth + 0.5 {
        notes.push(format!(
            "the sheer rises {:.1} in above the midship gunwale toward the ends; the analysis uses a flat gunwale at {depth:.2} in (that part is dropped)",
            sheer_max - depth
        ));
    }
    // Waterlines: keel to the gunwale, the top one just below it (a cut exactly at the rim is degenerate).
    let z: Vec<f64> =
        linspace(0.0, depth, o.waterlines).into_iter().map(|zk| zk.min(depth - 1e-6 * depth)).collect();
    let mut half_breadth = vec![vec![0.0; z.len()]; x.len()];
    let (mut asym, mut thick): (f64, Vec<f64>) = (0.0, Vec::new());
    for (i, seg) in secs.iter().enumerate() {
        if i == 0 || i + 1 == x.len() {
            continue; // closed tips
        }
        let mut sides_k: Vec<(f64, f64, Option<f64>, Option<f64>)> = Vec::with_capacity(z.len());
        for &zk in &z {
            let c = crossings(seg, zk);
            let side = |s: f64| -> (f64, Option<f64>) {
                let mut v: Vec<f64> = c.iter().map(|y| s * y).filter(|y| *y > 0.0).collect();
                v.sort_by(|a, b| b.total_cmp(a));
                (v.first().copied().unwrap_or(0.0), v.get(1).copied())
            };
            let ((so, si), (po, pi)) = (side(1.0), side(-1.0));
            sides_k.push((so, po, si, pi));
        }
        for (k, &(so, po, si, pi)) in sides_k.iter().enumerate() {
            half_breadth[i][k] = 0.5 * (so + po);
            if so > 0.0 && po > 0.0 {
                asym = asym.max((so - po).abs());
            }
            // Wall thickness: horizontal gap to the inner face, times cos(side angle).
            if mid(x[i], 0.6) && z[k] >= 0.2 * depth && z[k] <= 0.8 * depth && k > 0 && k + 1 < z.len() {
                let dy =
                    0.5 * ((sides_k[k + 1].0 + sides_k[k + 1].1) - (sides_k[k - 1].0 + sides_k[k - 1].1));
                let cosa = (z[k + 1] - z[k - 1]) / dy.hypot(z[k + 1] - z[k - 1]);
                for (out, inn) in [(so, si), (po, pi)] {
                    if let Some(inn) = inn {
                        thick.push((out - inn) * cosa);
                    }
                }
            }
        }
    }
    let solid_rows = thick.len();
    thick.sort_by(f64::total_cmp);
    let (t_med, t_min, t_max) = if solid_rows >= 10 {
        (
            Some(thick[thick.len() / 2]),
            Some(thick[thick.len() / 20]),
            Some(thick[thick.len() - 1 - thick.len() / 20]),
        )
    } else {
        notes.push("one face only (a surface mesh): no wall thickness measured".into());
        (None, None, None)
    };
    // 2 V / A, meaningful for a closed solid.
    let (vol, area) = tris.iter().fold((0.0, 0.0), |(v, a), [p, q, r]| {
        let (u, w) = ([q[0] - p[0], q[1] - p[1], q[2] - p[2]], [r[0] - p[0], r[1] - p[1], r[2] - p[2]]);
        let n = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]];
        let det = p[0] * (q[1] * r[2] - q[2] * r[1]) - p[1] * (q[0] * r[2] - q[2] * r[0])
            + p[2] * (q[0] * r[1] - q[1] * r[0]);
        (v + det / 6.0, a + 0.5 * (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt())
    });
    let t_vol = (t_med.is_some() && vol.abs() > 0.0).then(|| 2.0 * vol.abs() / area);
    if asym > 0.25 {
        notes.push(format!("port and starboard differ by up to {asym:.2} in; the table uses their mean"));
    }
    let hull = Hull {
        format: FORMAT.into(),
        units: "in".into(),
        name: name.into(),
        source: format!("mesh import ({} triangles, {units})", raw.len()),
        length,
        draft_design: z[z.len() - 1],
        x,
        z,
        half_breadth,
        stem: None,
    };
    hull.validate().context("imported table")?;
    let axis = |a: usize| ['x', 'y', 'z'][a];
    Ok((
        hull,
        Measured {
            units,
            length_axis: axis(la),
            up_axis: axis(ua),
            triangles: raw.len(),
            length_in: length,
            beam_in: beam,
            depth_in: depth,
            sheer_max_in: sheer_max,
            thickness_in: t_med,
            thickness_min_in: t_min,
            thickness_max_in: t_max,
            thickness_volume_in: t_vol,
            asymmetry_in: asym,
            notes,
        },
    ))
}

/// A canoe.toml that analyses the imported canoe as drawn: `base` (a canoe.toml's text, its
/// relative paths resolved against `base_dir`) with `hull.file` = `hull_path`, the hull's
/// own shape (no flare, templates, stem radius, gunwale outline or shape search: the CAD
/// model is the shape), and the search pinned to the drawn wall `thickness_in` (the measured
/// one, or given) and the measured depth, and to `density_pcf` when given (else the base's
/// mix range: the lightest passing mix for that wall). The buildable floor is lowered to the
/// drawn wall when it is thinner (noted). Returns the text and notes.
pub fn analysis_toml(
    base: &str,
    base_dir: &std::path::Path,
    hull_path: &std::path::Path,
    m: &Measured,
    thickness_in: Option<f64>,
    density_pcf: Option<f64>,
) -> Result<(String, Vec<String>)> {
    let mut t: toml::Table = toml::from_str(base).context("base canoe.toml")?;
    let mut notes = Vec::new();
    let abs = |p: &str| -> String {
        let pb = std::path::Path::new(p);
        let full = if pb.is_relative() { base_dir.join(pb) } else { pb.to_path_buf() };
        std::path::absolute(&full).unwrap_or(full).to_string_lossy().replace('\\', "/")
    };
    let table = |t: &mut toml::Table, k: &str| -> Result<toml::Table> {
        t.get(k).and_then(|v| v.as_table()).cloned().with_context(|| format!("base canoe.toml has no [{k}]"))
    };
    let mut hull = table(&mut t, "hull")?;
    for k in [
        "templates",
        "keel_fillet_radius_in",
        "stem_radius_in",
        "gunwale_radius_in",
        "gunwale_max_flare_deg",
        "doubly_symmetric",
    ] {
        hull.remove(k);
    }
    hull.insert("file".into(), abs(&hull_path.to_string_lossy()).into());
    hull.insert("flare_deg".into(), 0.0.into());
    // The CAD stem is analysed as drawn (the default stem radius would cut it).
    hull.insert("stem_radius_in".into(), 0.0.into());
    t.insert("hull".into(), hull.into());
    t.remove("shape_search");
    let wall = thickness_in.or(m.thickness_in);
    let mut search = table(&mut t, "search")?;
    search.remove("flare_deg");
    search.insert(
        "depth_in".into(),
        toml::Value::Array(vec![m.depth_in.into(), m.depth_in.into(), 1.0.into()]),
    );
    match wall {
        Some(w) => {
            search.insert("thickness_in".into(), toml::Value::Array(vec![w.into(), w.into(), 0.0625.into()]));
            let mut gates = table(&mut t, "gates")?;
            if gates.get("thickness_min_buildable_in").and_then(|v| v.as_float()).is_some_and(|f| f > w) {
                notes.push(format!(
                    "the drawn wall ({w:.3} in) is thinner than your buildable floor: analysed anyway"
                ));
                gates.insert("thickness_min_buildable_in".into(), w.into());
            }
            t.insert("gates".into(), gates.into());
        }
        None => notes.push(
            "no wall thickness (surface mesh, no --thickness): the base thickness range is searched".into(),
        ),
    }
    if let Some(d) = density_pcf {
        search.insert("density_pcf".into(), toml::Value::Array(vec![d.into(), d.into(), 1.0.into()]));
    }
    t.insert("search".into(), search.into());
    let mut material = table(&mut t, "material")?;
    if let Some(p) = material.get("mix_data").and_then(|v| v.as_str()).map(|s| s.to_string()) {
        material.insert("mix_data".into(), abs(&p).into());
    }
    t.insert("material".into(), material.into());
    // The race objective needs a shape search; a single drawn hull ranks walls by weight.
    if let Some(obj) = t.get_mut("objective").and_then(|v| v.as_table_mut())
        && obj.get("kind").and_then(|v| v.as_str()) == Some("race")
    {
        obj.insert("kind".into(), "weight".into());
        obj.remove("straight_weight");
        notes.push("objective race needs a shape search: weight used (race it with jaguar race)".into());
    }
    let head = format!(
        "# Analysis of an imported CAD canoe as drawn (jaguar import-mesh): hull {}, wall {}, depth {:.3} in.\n",
        hull_path.display(),
        wall.map_or("searched".to_string(), |w| format!("{w:.3} in")),
        m.depth_in
    );
    Ok((head + &toml::to_string(&t)?, notes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::export::{ExportSpec, build_mesh, to_stl};
    use crate::hull::Grid;
    use std::path::Path;

    fn off1() -> Hull {
        Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap()
    }

    fn solid(t: Option<f64>) -> Vec<[V3; 3]> {
        let spec = ExportSpec {
            depth_in: 12.0,
            thickness_in: t,
            flare_deg: 0.0,
            gunwale_radius_in: None,
            stations: 161,
            girth: 41,
            pocket_in: None,
            layers: None,
        };
        parse_stl(&to_stl(&build_mesh(&off1(), &spec).unwrap(), "off1")).unwrap()
    }

    #[test]
    fn round_trip_of_an_exported_solid() {
        let (h, m) = hull_from_mesh(&solid(Some(0.375)), "rt", &MeshOptions::new()).unwrap();
        assert!(m.units.starts_with("in"), "{}", m.units);
        assert!((m.length_in - 240.1).abs() < 0.5 && (m.depth_in - 12.0).abs() < 0.05, "{m:?}");
        let t = m.thickness_in.unwrap();
        assert!((t - 0.375).abs() < 0.02, "wall {t}");
        assert!((m.thickness_volume_in.unwrap() - 0.375).abs() < 0.05, "{:?}", m.thickness_volume_in);
        assert!(m.asymmetry_in < 0.01);
        // Offsets match the source hull's analysis grid at the same heights.
        let src = Grid::build(&off1(), 12.0, 81);
        for i in [20, 40, 60] {
            for &zz in &[2.0, 4.0, 5.5] {
                let k = h.z.partition_point(|&v| v <= zz).clamp(1, h.z.len() - 1);
                let f = (zz - h.z[k - 1]) / (h.z[k] - h.z[k - 1]);
                let got = h.half_breadth[i][k - 1] * (1.0 - f) + h.half_breadth[i][k] * f;
                let j = src.z.partition_point(|&v| v <= zz).clamp(1, src.nz() - 1);
                let g = (zz - src.z[j - 1]) / (src.z[j] - src.z[j - 1]);
                let want = src.y(i, j - 1) * (1.0 - g) + src.y(i, j) * g;
                assert!((got - want).abs() < 0.15, "x {} z {zz}: {got} vs {want}", h.x[i]);
            }
        }
    }

    #[test]
    fn detects_units_and_axes_and_flips() {
        // Millimetres, length along z, up along -x (keel at +x), beam along y.
        let mm: Vec<[V3; 3]> =
            solid(Some(0.375)).iter().map(|t| t.map(|p| [-p[2] * 25.4, p[1] * 25.4, p[0] * 25.4])).collect();
        let (h, m) = hull_from_mesh(&mm, "mm", &MeshOptions::new()).unwrap();
        assert!(m.units.starts_with("mm") && m.length_axis == 'z' && m.up_axis == 'x', "{m:?}");
        assert!((m.depth_in - 12.0).abs() < 0.05 && (m.thickness_in.unwrap() - 0.375).abs() < 0.02, "{m:?}");
        let (g, _) = hull_from_mesh(&solid(Some(0.375)), "in", &MeshOptions::new()).unwrap();
        let mid = h.x.len() / 2;
        assert!((h.half_breadth[mid][20] - g.half_breadth[mid][20]).abs() < 1e-3);
        // A canoe-length object in metres vs inches is unambiguous; an odd size is an error.
        let tiny: Vec<[V3; 3]> = mm.iter().map(|t| t.map(|p| p.map(|c| c * 1e-5))).collect();
        assert!(hull_from_mesh(&tiny, "x", &MeshOptions::new()).is_err());
        let o = MeshOptions { units: Some(1.0 / 25.4), ..MeshOptions::new() };
        assert!(hull_from_mesh(&mm, "mm", &o).is_ok());
    }

    #[test]
    fn surface_mesh_has_no_thickness() {
        let (_, m) = hull_from_mesh(&solid(None), "s", &MeshOptions::new()).unwrap();
        assert!(m.thickness_in.is_none() && m.notes.iter().any(|n| n.contains("surface")), "{m:?}");
    }

    #[test]
    fn analysis_toml_pins_the_drawn_wall_and_runs() {
        let dir = std::env::temp_dir().join(format!("jaguar_mesh_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (h, m) = hull_from_mesh(&solid(Some(0.375)), "cad", &MeshOptions::new()).unwrap();
        let hp = dir.join("cad_hull.json");
        h.save(&hp).unwrap();
        let base = std::fs::read_to_string("examples/canoe.toml").unwrap();
        let (text, notes) = analysis_toml(&base, Path::new("examples"), &hp, &m, None, Some(58.0)).unwrap();
        assert!(notes.is_empty(), "{notes:?}");
        let p = crate::params::Params::parse(&text, &dir).unwrap();
        assert_eq!(p.search.depth_in[0], m.depth_in);
        assert_eq!(p.search.thickness_in[..2], [m.thickness_in.unwrap(); 2]);
        assert_eq!((p.hull.flare_deg, p.hull.templates), (0.0, None));
        // Run it: one wall, the CAD hull, every check. It is the off1 winner it was drawn from (0.375 in,
        // 12 in, 58 pcf: 98.24 lb, punching 0.904, GM 1.07 with the real crew) up to the mesh's sampling. off1 sits on
        // the GM gate and GM is a small difference of large terms, so it is compared, not gated.
        let mut p = p;
        p.stability.gm_min_in = 0.0;
        let r = crate::run(&p, &|_| {}).unwrap();
        assert_eq!(r.candidates.len(), 1);
        let w = r.winner.expect("the drawn design passes");
        let c = &w.candidate;
        assert!((c.hull_weight_lb / 98.24 - 1.0).abs() < 0.02, "{}", c.hull_weight_lb);
        assert!((c.verdict.as_ref().unwrap().dc - 0.904).abs() < 0.005);
        assert!((c.gm_in.unwrap() / 1.06 - 1.0).abs() < 0.08, "{:?}", c.gm_in);
        // A drawn wall under the buildable floor is still analysed, with a note.
        let (_, notes) = analysis_toml(&base, Path::new("examples"), &hp, &m, Some(0.25), None).unwrap();
        assert!(notes.iter().any(|n| n.contains("thinner than your buildable floor")));
    }

    #[test]
    fn step_solid_reproduces_the_design_it_was_exported_from() {
        // off1's winner (0.375 in, 12 in, 58 pcf: 98.24 lb, punching 0.904) as a B-spline STEP
        // solid with air pockets, read back through read_cad and run as drawn.
        let spec = ExportSpec {
            depth_in: 12.0,
            thickness_in: Some(0.375),
            flare_deg: 0.0,
            gunwale_radius_in: None,
            stations: 161,
            girth: 41,
            pocket_in: Some(30.0),
            layers: Some(2),
        };
        let step = crate::export::to_step(&build_mesh(&off1(), &spec).unwrap(), "off1");
        let (tris, units, what) = read_cad("STEP", step.as_bytes()).unwrap();
        assert!(what.starts_with("STEP, 17 faces"), "{what}");
        let (ipu, name) = units.unwrap();
        let o = MeshOptions { units: Some(ipu), units_from: Some(name), ..MeshOptions::new() };
        let (h, m) = hull_from_mesh(&tris, "step", &o).unwrap();
        assert_eq!(m.units, "inch");
        assert!((m.length_in - 240.1).abs() < 0.5 && (m.depth_in - 12.0).abs() < 0.05, "{m:?}");
        assert!((m.thickness_in.unwrap() - 0.375).abs() < 0.01, "{m:?}");
        let dir = std::env::temp_dir().join(format!("jaguar_step_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let hp = dir.join("step_hull.json");
        h.save(&hp).unwrap();
        let base = std::fs::read_to_string("examples/canoe.toml").unwrap();
        let (text, _) =
            analysis_toml(&base, Path::new("examples"), &hp, &m, Some(0.375), Some(58.0)).unwrap();
        let mut p = crate::params::Params::parse(&text, &dir).unwrap();
        p.stability.gm_min_in = 0.0;
        let r = crate::run(&p, &|_| {}).unwrap();
        let c = &r.winner.expect("the drawn design passes").candidate;
        assert!((c.hull_weight_lb / 98.24 - 1.0).abs() < 0.02, "{}", c.hull_weight_lb);
        assert!((c.verdict.as_ref().unwrap().dc - 0.904).abs() < 0.005, "{:?}", c.verdict);
        assert!(read_cad("dwg", b"").is_err());
    }

    #[test]
    fn obj_and_ascii_stl_parse() {
        let obj = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\nf -4 -2 -1\n";
        let t = parse_obj(obj).unwrap();
        assert_eq!(t.len(), 3);
        assert_eq!(t[0], [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]]);
        let stl = "solid a\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid a\n";
        assert_eq!(parse_stl(stl.as_bytes()).unwrap().len(), 1);
        assert!(parse_obj("f 1 2 3").is_err());
    }
}
