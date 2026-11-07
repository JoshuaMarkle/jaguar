//! The model behind the engineering drawing sheets (`sheets.rs`): every outline, dimension and
//! quantity of a finished design, computed from its jaguar-result/1 (the winner's hull, wall,
//! depth, flare, mix and zones, casting schedule, load cases, stands) and the `[drawing]`
//! inputs, never typed in. Below `manufacture` in the layout: reads the geometry and mold
//! modules, never feeds physics.
//!
//! Sources, so the drawing and the analysis cannot disagree:
//! * outlines: the analysis surface (`Grid::build_topsides_at`: table loft, topsides to the mold
//!   depth, stem radius) on a fine even grid plus the table stations; the inner wall and the
//!   midship section from the export's mitred offset (`export::build_mesh`, as STEP and the
//!   mold use);
//! * wall buildup: `Punching::schedule` (the passes and meshes the punching d comes from);
//! * concrete and mesh: the section areas the hull weight is integrated from (`section`), at the
//!   search stations, so volume x density / 1728 is the candidate's `hull_weight_lb`;
//! * mold: `manufacture::stack_only` (flat foam layers of `mold_board_in`), nested on the foam
//!   sheets with `manufacture::nest`.
//!
//! A value that cannot be derived is an error naming the missing input, never a guess.

use crate::export::{self, ExportSpec, NetRow};
use crate::hull::{Grid, Hull, Topsides, linspace};
use crate::manufacture::{self, MoldSpec, Packing};
use crate::params::{BomRow, Drawing, Mold, Params};
use crate::section::{self, ZoneLayout};
use anyhow::{Context, Result, bail, ensure};
use serde_json::Value;

pub type P = [f64; 2];

/// Rocker of a hull without its generating shape factors (a fixed hull file) is measured on
/// the lofted keel, which sits up to a few hundredths off an analytic keel: whole boards within
/// this, in.
pub const ROCKER_TOL_IN: f64 = 0.03125;

/// One station of the body plan and offsets table.
#[derive(Debug, Clone)]
pub struct Station {
    /// From the bow datum, in.
    pub x: f64,
    /// Keel height, in (None: no hull at this station, a stem tip).
    pub keel: Option<f64>,
    /// Half section (y, z), keel on the centreline first, up to the gunwale.
    pub half: Vec<P>,
}

impl Station {
    /// Half-breadth at height z (None below the keel or with no hull here).
    pub fn breadth(&self, z: f64) -> Option<f64> {
        let k = self.keel?;
        if z < k - 1e-9 || self.half.len() < 2 {
            return None;
        }
        let i = self.half.iter().position(|p| p[1] >= z - 1e-12)?;
        if i == 0 {
            return Some(self.half.first()?[0]);
        }
        let (a, b) = (self.half.get(i - 1)?, self.half.get(i)?);
        let t = if b[1] - a[1] > 1e-12 { (z - a[1]) / (b[1] - a[1]) } else { 1.0 };
        Some(a[0] + t * (b[0] - a[0]))
    }
}

/// Static waterline of one load case.
#[derive(Debug, Clone)]
pub struct Waterline {
    pub case: String,
    pub draft_in: f64,
    pub trim_in: f64,
    /// Hull + crew, lb (real crew weight: no dynamic factor).
    pub displacement_lb: f64,
    /// The freeboard the gate sees (at the lettering with `[lettering]`), in.
    pub freeboard_gate_in: f64,
    /// Lowest gunwale-to-waterline height on the hull and where.
    pub x_min: f64,
    pub freeboard_min_in: f64,
}

impl Waterline {
    /// Waterline height above the keel at x on a hull `length` long (draft at midships, trim
    /// stern minus bow).
    pub fn z(&self, x: f64, length: f64) -> f64 {
        self.draft_in + self.trim_in * (x - 0.5 * length) / length
    }
}

/// One concrete mix of the wall.
#[derive(Debug, Clone)]
pub struct MixPart {
    pub part: String,
    pub density_pcf: f64,
    /// Tested mix name, or None (fit-mode density on the strength line).
    pub name: Option<String>,
    /// Wall concrete of this mix before waste, in^3.
    pub volume_in3: f64,
}

impl MixPart {
    pub fn label(&self) -> String {
        match &self.name {
            Some(n) => format!("MIX {n}, {:.1} PCF", self.density_pcf),
            None => format!("MIX AT {:.1} PCF (STRENGTH LINE)", self.density_pcf),
        }
    }
}

/// The wall buildup the punching check's effective depth comes from.
#[derive(Debug, Clone)]
pub struct Laminate {
    pub t: f64,
    /// Equal casting lifts, numbered from the mold face.
    pub lifts: u32,
    pub mold: Mold,
    /// Meshes, each laid after a lift: (lift k, cover to the inside face, cover to the outside).
    pub meshes: Vec<(u32, f64, f64)>,
    /// Punching effective depth (knee face to the farthest mesh), in = `d_over_t()` x t.
    pub d_in: f64,
}

impl Laminate {
    pub fn lift_in(&self) -> f64 {
        self.t / f64::from(self.lifts)
    }
}

/// A lettered or graphic stretch of the hull side.
#[derive(Debug, Clone)]
pub struct Placed {
    pub name: String,
    pub x0: f64,
    pub x1: f64,
    pub height: f64,
    pub below_gunwale: f64,
    pub sides: u32,
    /// From `[lettering]` (true) or `[[drawing.graphic]]`.
    pub lettering: bool,
}

#[derive(Debug, Clone)]
pub struct StandsAt {
    /// Pad centre in from each end, in, and centre-to-centre spacing.
    pub x_in: f64,
    pub spacing_in: f64,
    pub pad_in: f64,
    pub inverted: bool,
}

/// One foam layer of the male mold (L1 on the floor, which is the gunwale).
#[derive(Debug, Clone)]
pub struct MoldLayer {
    pub label: String,
    /// Underside and top above the floor, in.
    pub h0: f64,
    pub h1: f64,
    /// Station range it covers, in from the bow datum.
    pub x0: f64,
    pub x1: f64,
    /// Foam pieces (closed outlines in plan: x from the bow, y across) and their names.
    pub pieces: Vec<Vec<P>>,
    pub piece_labels: Vec<String>,
    /// Sanding line etched on the template (the layer's top face), plan segments.
    pub sanding: Vec<[P; 2]>,
    /// Dowel / alignment holes in plan.
    pub holes: Vec<P>,
    pub max_width: f64,
}

/// A foam sheet with the pieces nested on it (sheet frame, in).
#[derive(Debug, Clone)]
pub struct FoamSheet {
    pub parts: Vec<(String, Vec<P>)>,
    pub used_in2: f64,
}

#[derive(Debug, Clone)]
pub struct MoldModel {
    pub board_in: f64,
    /// Floor (gunwale) to the inner keel: depth - t = layers x board.
    pub height_in: f64,
    pub layers: Vec<MoldLayer>,
    /// Hollow run the mold covers and the mold's top (inner keel) above the floor along it.
    pub top: Vec<P>,
    pub sheet_in: [f64; 2],
    pub kerf_in: f64,
    pub edge_in: f64,
    pub sheets: Vec<FoamSheet>,
    pub hole_in: f64,
    /// Glued area between layers and mold surface, ft^2.
    pub glue_ft2: f64,
    pub surface_ft2: f64,
    /// Block joints shared by every layer (x), bow to stern.
    pub joints: Vec<f64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BomLine {
    pub part: String,
    pub qty: f64,
    pub unit: String,
    pub desc: String,
    /// Hand-entered row (not computed).
    pub manual: bool,
    /// The computed value an override replaced, for the metadata.
    pub overridden: Option<String>,
}

impl BomLine {
    /// Quantity as printed: whole counts for sheets and units, else 2 decimals (1 for ft^2).
    pub fn qty_text(&self) -> String {
        match self.unit.as_str() {
            "FT2" => format!("{:.1}", self.qty),
            "FT3" | "GAL" => format!("{:.2}", self.qty),
            _ if self.qty.fract() == 0.0 => format!("{:.0}", self.qty),
            _ => format!("{:.2}", self.qty),
        }
    }
}

/// Everything the two sheets draw.
#[derive(Debug, Clone)]
pub struct Model {
    pub spec: Drawing,
    pub hull_name: String,
    pub t: f64,
    pub depth: f64,
    pub flare_deg: f64,
    /// Length overall, bow datum (x = 0) to the stern tip, in.
    pub length: f64,
    /// Gunwale (overall) beam and its station from the bow datum.
    pub beam: f64,
    pub x_beam: f64,
    pub taper_bow: f64,
    pub taper_stern: f64,
    /// Side view silhouette (x, z), closed.
    pub profile: Vec<P>,
    /// Outer and inner keel lines (x, z) and the mold's length [xa, xb] (solid concrete tips outside).
    pub keel: Vec<P>,
    pub inner_keel: Vec<P>,
    pub hollow: [f64; 2],
    /// Gunwale half-breadth (x, y): outside, and inside over the hollow run.
    pub sheer: Vec<P>,
    pub inner_sheer: Vec<P>,
    /// End view: half outline (y, z) enveloping every section, keel point first.
    pub end_view: Vec<P>,
    pub stations: Vec<Station>,
    pub waterlines: Vec<f64>,
    /// Midship section A-A: x and half girths (y, z), sheer to keel, outside and inside.
    pub mid_x: f64,
    pub mid_outer: Vec<P>,
    pub mid_inner: Vec<P>,
    pub waterline: Waterline,
    pub mixes: Vec<MixPart>,
    pub zones: Option<ZoneLayout>,
    pub laminate: Laminate,
    pub stands: Option<StandsAt>,
    pub graphics: Vec<Placed>,
    pub stem_radius_in: Option<f64>,
    pub air_pocket_in: Option<f64>,
    /// Isometric: triangles of the shelled canoe (x, y, z) and whether each is the dense mix.
    pub iso: Vec<([[f64; 3]; 3], bool)>,
    pub hull_weight_lb: f64,
    pub developed_ft2: f64,
    pub bom: Vec<BomLine>,
    pub mold_bom: Vec<BomLine>,
    /// "C-01: computed 2.31 FT3, drawn 2.50 (reason)" per override; manual rows listed too.
    pub bom_record: Vec<String>,
    pub mold: MoldModel,
    pub verified: bool,
    pub verify_note: Option<String>,
    pub warnings: Vec<String>,
}

/// The drawing inputs of a result: its saved `drawing` (written by an export that edited them),
/// else its params' `[drawing]`, else `fallback` (the canoe.toml's), else the defaults.
pub fn spec_of(v: &Value, fallback: Option<&Drawing>) -> Result<Drawing> {
    if v["drawing"].is_object() {
        return serde_json::from_value(v["drawing"].clone()).context("result: drawing");
    }
    if v["params"]["drawing"].is_object() {
        return serde_json::from_value(v["params"]["drawing"].clone()).context("result: params.drawing");
    }
    Ok(fallback.cloned().unwrap_or_default())
}

/// The `[drawing]` table of a canoe.toml (its logo made absolute). None: the file has none.
pub fn spec_from_toml(path: &std::path::Path) -> Result<Option<Drawing>> {
    let text = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let table: toml::Table = toml::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
    let Some(t) = table.get("drawing") else { return Ok(None) };
    let mut d: Drawing = t.clone().try_into().with_context(|| format!("{}: [drawing]", path.display()))?;
    if let Some(l) = d.logo.as_mut()
        && l.is_relative()
    {
        *l = path.parent().unwrap_or(std::path::Path::new("")).join(&*l);
    }
    d.validate()?;
    Ok(Some(d))
}

/// The result text with `spec` saved as its `drawing` (so a re-export reproduces the sheets).
/// The rest of the file is kept byte for byte, key order included (`format` stays first: the
/// web UI recognizes results by it); a previous `drawing` entry is replaced, a new one appended,
/// pretty-printed when the file is (result.json), compact otherwise (runs/).
pub fn with_spec(text: &str, spec: &Drawing) -> Result<String> {
    let v: Value = serde_json::from_str(text).context("parsing result JSON")?;
    ensure!(v["format"] == crate::optimize::RESULT_FORMAT, "not a jaguar-result/1 file");
    let spans = top_level(text).context("result JSON: not an object")?;
    let pretty = text.trim_start().starts_with("{\n");
    let mut parts: Vec<String> =
        spans.iter().filter(|(k, _, _)| k != "drawing").map(|(_, a, b)| text[*a..*b].to_string()).collect();
    let d = if pretty {
        format!("\"drawing\": {}", serde_json::to_string_pretty(spec)?.replace('\n', "\n  "))
    } else {
        format!("\"drawing\":{}", serde_json::to_string(spec)?)
    };
    parts.push(d);
    Ok(if pretty { format!("{{\n  {}\n}}", parts.join(",\n  ")) } else { format!("{{{}}}", parts.join(",")) })
}

/// The top-level members of a JSON object text: (key, start, end) byte spans of `"key": value`.
fn top_level(text: &str) -> Option<Vec<(String, usize, usize)>> {
    let b = text.as_bytes();
    let open = text.find('{')?;
    let (mut i, mut out) = (open + 1, vec![]);
    let skip_ws = |mut i: usize| {
        while i < b.len() && (b[i] as char).is_whitespace() {
            i += 1;
        }
        i
    };
    // End of the string starting at the quote at i (index after the closing quote).
    let string_end = |mut i: usize| {
        i += 1;
        while i < b.len() && b[i] != b'"' {
            i += if b[i] == b'\\' { 2 } else { 1 };
        }
        i + 1
    };
    loop {
        i = skip_ws(i);
        match b.get(i)? {
            b'}' => return Some(out),
            b',' => {
                i += 1;
                continue;
            }
            b'"' => {}
            _ => return None,
        }
        let start = i;
        let key_end = string_end(i);
        let key: String = serde_json::from_str(&text[i..key_end]).ok()?;
        i = skip_ws(key_end);
        if b.get(i)? != &b':' {
            return None;
        }
        i += 1;
        let mut depth = 0i32;
        while i < b.len() {
            match b[i] {
                b'"' => {
                    i = string_end(i);
                    continue;
                }
                b'{' | b'[' => depth += 1,
                b'}' | b']' if depth == 0 => break,
                b'}' | b']' => depth -= 1,
                b',' if depth == 0 => break,
                _ => {}
            }
            i += 1;
        }
        let mut end = i;
        while end > start && (b[end - 1] as char).is_whitespace() {
            end -= 1;
        }
        out.push((key, start, end));
    }
}

/// The title block text the sheets cannot do without; Err names every missing field.
pub fn require(d: &Drawing) -> Result<()> {
    let missing: Vec<&str> = [
        ("canoe_name", &d.canoe_name),
        ("school", &d.school),
        ("official_school_name", &d.official_school_name),
        ("drawn_by", &d.drawn_by),
        ("drawn_date", &d.drawn_date),
    ]
    .iter()
    .filter(|(_, v)| v.trim().is_empty())
    .map(|(k, _)| *k)
    .collect();
    ensure!(
        missing.is_empty(),
        "drawing: the title block needs {} (set them in [drawing] or the Drawings panel)",
        missing.join(", ")
    );
    Ok(())
}

fn norm_name(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// WARNINGs when a school name on the drawing or on the hull lettering is not the official one.
pub fn school_warnings(d: &Drawing, lettering_school: Option<&str>) -> Vec<String> {
    let official = norm_name(&d.official_school_name);
    let mut out = vec![];
    if norm_name(&d.school) != official {
        out.push(format!(
            "WARNING the title block school name \"{}\" is not the official name \"{}\"",
            d.school.trim(),
            official
        ));
    }
    if let Some(s) = lettering_school.filter(|s| !s.trim().is_empty())
        && norm_name(s) != official
    {
        out.push(format!(
            "WARNING the hull lettering school name \"{}\" ([lettering] school) is not the official name \"{}\"",
            s.trim(),
            official
        ));
    }
    out
}

fn trapz(x: &[f64], f: &[f64]) -> f64 {
    x.windows(2).zip(f.windows(2)).map(|(x, f)| 0.5 * (f[0] + f[1]) * (x[1] - x[0])).sum()
}

fn num(v: &Value, what: &str) -> Result<f64> {
    v.as_f64().with_context(|| format!("result: {what} is missing"))
}

/// Half section (y, z) of grid column `i`: the keel point on the centreline, then every row
/// above the keel. Empty when the column has no hull.
fn half_section(grid: &Grid, i: usize, keel: f64) -> Vec<P> {
    let row = grid.row(i);
    if !row.iter().any(|&y| y > 1e-9) {
        return vec![];
    }
    let mut out = vec![[0.0, keel]];
    out.extend(grid.z.iter().zip(row).filter(|(z, _)| **z > keel + 1e-9).map(|(&z, &y)| [y, z]));
    out
}

/// Everything the sheets draw, from a jaguar-result/1 text and the drawing inputs `spec`.
pub fn model(text: &str, spec: Drawing) -> Result<Model> {
    let v: Value = serde_json::from_str(text).context("parsing result JSON")?;
    ensure!(v["format"] == crate::optimize::RESULT_FORMAT, "not a jaguar-result/1 file");
    ensure!(
        v["winner"]["candidate"].is_object(),
        "the result has no winner: a design that fails its checks is not drawn"
    );
    spec.validate()?;
    require(&spec)?;
    let params: Params = serde_json::from_value(v["params"].clone()).context("result: params")?;
    let w = &v["winner"];
    let c = &w["candidate"];
    let (hull, es) = export::from_result(text, 481, 61)?;
    let (t, depth) = (es.thickness_in.context("result: winner has no wall thickness")?, es.depth_in);
    let ts: Topsides = crate::report::result_topsides(&v);
    let length = hull.length;

    // ---- board mold: the mold height and the rocker are whole boards ----
    let board = spec.mold_board_in.or(params.search.mold_board_in).context(
        "drawing: the mold sheet needs the foam board thickness: search.mold_board_in in the run, or drawing.mold_board_in",
    )?;
    let k = (depth - t) / board;
    ensure!(
        (k - k.round()).abs() < 1e-6 && k.round() >= 1.0,
        "mold: depth {depth} in less the wall {t} in is {:.4} in, not a whole number of {board} in boards ({k:.4}); \
         run with search.mold_board_in = {board}",
        depth - t
    );
    let boards = k.round() as usize;
    let rocker = rockers(&hull, c)?;
    for (end, r) in [("bow", rocker[0]), ("stern", rocker[1])] {
        let j = r.0 / board;
        ensure!(
            (j - j.round()).abs() * board <= r.1,
            "mold: the {end} rocker {:.3} in is not a whole number of {board} in boards ({j:.3}); \
             run the shape search with search.mold_board_in = {board}",
            r.0
        );
    }

    // ---- outlines on a fine grid ----
    let nf = ((length / 0.25).ceil() as usize + 1).clamp(201, 2401);
    let mut xs = linspace(0.0, length, nf);
    xs.extend(hull.x.iter().copied());
    xs.sort_by(f64::total_cmp);
    xs.dedup_by(|a, b| (*a - *b).abs() < 1e-6);
    let (grid, why) = Grid::build_topsides_at(&hull, depth, &xs, ts);
    if let Some(why) = why {
        bail!("drawing: {why}");
    }
    let loft = hull.loft();
    let (nx, nz) = (grid.nx(), grid.nz());
    let has = |i: usize| grid.row(i).iter().any(|&y| y > 1e-9);
    let mut keel: Vec<P> = vec![];
    for i in (0..nx).filter(|&i| has(i)) {
        keel.push([grid.x[i], loft.keel(grid.x[i]).context("hull has no offsets")?]);
    }
    ensure!(keel.len() >= 2, "hull has offsets at fewer than two stations");
    let gr = &grid;
    let rows_with = |k: usize| (0..nx).filter(move |&i| gr.y(i, k) > 1e-9);
    let mut front: Vec<P> = vec![];
    let mut back: Vec<P> = vec![];
    for k in 0..nz {
        let (Some(i0), Some(i1)) = (rows_with(k).next(), rows_with(k).next_back()) else { continue };
        let xf = if i0 > 0 { grid.x[i0 - 1] } else { grid.x[i0] };
        let xb = if i1 + 1 < nx { grid.x[i1 + 1] } else { grid.x[i1] };
        front.push([xf, grid.z[k]]);
        back.push([xb, grid.z[k]]);
    }
    let mut profile: Vec<P> = front.iter().rev().copied().collect();
    profile.extend(keel.iter().copied());
    profile.extend(back.iter().copied());
    profile.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
    let top = nz - 1;
    let sheer: Vec<P> = (0..nx).map(|i| [grid.x[i], grid.y(i, top)]).collect();
    let ymax = sheer.iter().map(|p| p[1]).fold(0.0, f64::max);
    ensure!(ymax > 0.0, "hull has no beam at the gunwale");
    let plateau: Vec<f64> = sheer.iter().filter(|p| p[1] >= ymax - 1e-4).map(|p| p[0]).collect();
    let x_beam = 0.5 * (plateau.first().copied().unwrap_or(0.0) + plateau.last().copied().unwrap_or(0.0));
    let frac = spec.taper_fraction * ymax;
    let taper_bow = sheer.iter().find(|p| p[1] >= frac - 1e-12).map_or(0.0, |p| p[0]);
    let taper_stern = length - sheer.iter().rev().find(|p| p[1] >= frac - 1e-12).map_or(length, |p| p[0]);
    let kmin = keel.iter().map(|p| p[1]).fold(f64::INFINITY, f64::min);
    let mut end_view: Vec<P> = vec![[0.0, kmin]];
    for k in 0..nz {
        let y = (0..nx).map(|i| grid.y(i, k)).fold(0.0, f64::max);
        if y > 1e-9 && grid.z[k] > kmin + 1e-9 {
            end_view.push([y, grid.z[k]]);
        }
    }

    // ---- inner wall and midship section: the mold's surface (`export::mold_rows`, the wall's
    // offset inner face tapering into the ends), so the canoe is drawn as cast on that mold ----
    let es_wall = ExportSpec { pocket_in: None, filled: false, ..es.clone() };
    let mesh = export::build_mesh(&hull, &es_wall)?;
    let inner: Vec<NetRow> = export::mold_rows(&hull, &es_wall)?;
    let outer: &Vec<NetRow> =
        &mesh.brep.as_ref().context("this hull's ends are not stem lines: no structured surface")?.outer;
    let (Some(first_in), Some(last_in)) = (inner.first(), inner.last()) else { bail!("no mold surface") };
    let hollow = [first_in.0, last_in.0];
    let inner_keel: Vec<P> = inner.iter().filter_map(|r| r.1.last().map(|p| [r.0, p.1])).collect();
    let inner_sheer: Vec<P> = inner.iter().filter_map(|r| r.1.first().map(|p| [r.0, p.0])).collect();
    let near = |rows: &[NetRow], x: f64| {
        rows.iter().min_by(|a, b| (a.0 - x).abs().total_cmp(&(b.0 - x).abs())).cloned()
    };
    let mid = near(outer, 0.5 * length).context("no midship station")?;
    let mid_in = near(&inner, mid.0).context("no hollow midship station")?;
    ensure!((mid_in.0 - mid.0).abs() < 1e-6, "midship is in the solid end");
    let as_pts = |r: &NetRow| r.1.iter().map(|&(y, z)| [y, z]).collect::<Vec<P>>();

    // ---- body plan and offsets stations ----
    let xo = linspace(0.0, length, spec.offset_stations);
    let (go, _) = Grid::build_topsides_at(&hull, depth, &xo, ts);
    let mut stations = vec![];
    for (i, &x) in xo.iter().enumerate() {
        let k = if go.row(i).iter().any(|&y| y > 1e-9) { loft.keel(x) } else { None };
        let half = k.map_or(vec![], |kk| half_section(&go, i, kk));
        stations.push(Station { x, keel: k.filter(|_| half.len() >= 2), half });
    }
    let step = spec.offset_waterline_step_in;
    let mut waterlines: Vec<f64> = vec![];
    if stations.iter().any(|s| s.keel.is_some_and(|k| k <= 1e-9) && s.breadth(0.0).is_some_and(|y| y > 1e-9))
    {
        waterlines.push(0.0);
    }
    let mut z = step;
    while z < depth - 1e-6 {
        waterlines.push(z);
        z += step;
    }

    // ---- static waterline of the race displacement ----
    let crews: Vec<(String, f64)> =
        params.load_cases.iter().map(|lc| (lc.name.clone(), lc.weights_lb.iter().sum())).collect();
    let case_name = match &spec.waterline_case {
        Some(n) => {
            ensure!(
                crews.iter().any(|c| &c.0 == n),
                "drawing.waterline_case: no load case named '{n}' (load cases: {})",
                crews.iter().map(|c| c.0.as_str()).collect::<Vec<_>>().join(", ")
            );
            n.clone()
        }
        None => crews
            .iter()
            .fold(None::<&(String, f64)>, |a, c| if a.is_none_or(|a| c.1 > a.1) { Some(c) } else { a })
            .map(|c| c.0.clone())
            .context("the result has no load case")?,
    };
    let case = w["cases"]
        .as_array()
        .and_then(|cs| cs.iter().find(|cc| cc["name"].as_str() == Some(case_name.as_str())))
        .with_context(|| format!("result: no load case '{case_name}' in the winner"))?;
    if let Some(e) = case["error"].as_str() {
        bail!("load case '{case_name}' has no equilibrium ({e}): no waterline to draw");
    }
    let hull_weight_lb = num(&c["hull_weight_lb"], "winner hull_weight_lb")?;
    let crew = crews.iter().find(|c| c.0 == case_name).map_or(0.0, |c| c.1);
    let mut wl = Waterline {
        case: case_name.clone(),
        draft_in: num(&case["draft_in"], "load case draft")?,
        trim_in: num(&case["trim_in"], "load case trim")?,
        displacement_lb: hull_weight_lb + crew,
        freeboard_gate_in: num(&case["freeboard_in"], "load case freeboard")?,
        x_min: 0.0,
        freeboard_min_in: f64::INFINITY,
    };
    for p in &keel {
        let f = depth - wl.z(p[0], length);
        if f < wl.freeboard_min_in {
            (wl.x_min, wl.freeboard_min_in) = (p[0], f);
        }
    }

    // ---- mixes and their concrete (the hull weight's own sections) ----
    let zones: Option<ZoneLayout> = if c["zones"].is_object() {
        Some(serde_json::from_value(c["zones"].clone()).context("winner zones")?)
    } else {
        None
    };
    let gs = Grid::build_topsides(&hull, depth, params.hull.stations, ts).0;
    let sec = section::sections(&gs, t);
    let total_in3 = trapz(&gs.x, &sec.area);
    let developed_in2 = total_in3 / t;
    let rho = num(&c["density_pcf"], "winner density_pcf")?;
    let mix_name = c["mix"].as_str().map(str::to_string);
    let mixes = match &zones {
        None => {
            vec![MixPart { part: "C-01".into(), density_pcf: rho, name: mix_name, volume_in3: total_in3 }]
        }
        Some(zl) => {
            ensure!(zl.dense_pcf > zl.light_pcf, "zones: dense mix not heavier than the light one");
            let zs = section::zoned_sections(&gs, t, zl, 1.0);
            let dense: Vec<f64> = zs
                .weight
                .iter()
                .zip(&sec.area)
                .map(|(wt, a)| ((1728.0 * wt - a * zl.light_pcf) / (zl.dense_pcf - zl.light_pcf)).max(0.0))
                .collect();
            let dense_in3 = trapz(&gs.x, &dense);
            vec![
                MixPart {
                    part: "C-01".into(),
                    density_pcf: zl.light_pcf,
                    name: mix_name,
                    volume_in3: total_in3 - dense_in3,
                },
                MixPart {
                    part: "C-02".into(),
                    density_pcf: zl.dense_pcf,
                    name: c["dense_mix"].as_str().map(str::to_string),
                    volume_in3: dense_in3,
                },
            ]
        }
    };

    // ---- wall buildup: the punching check's casting schedule ----
    let (lifts, after, mold_kind) = params.punching.schedule().context("drawing: wall buildup (Detail B)")?;
    let d_in = params.punching.d_over_t()? * t;
    let meshes: Vec<(u32, f64, f64)> = after
        .iter()
        .map(|&k| {
            let from_mold = t * f64::from(k) / f64::from(lifts);
            match mold_kind {
                Mold::Male => (k, from_mold, t - from_mold),
                Mold::Female => (k, t - from_mold, from_mold),
            }
        })
        .collect();
    let laminate = Laminate { t, lifts, mold: mold_kind, meshes, d_in };

    // ---- stands, graphics, stem, pockets ----
    let stands = if params.stands.is_some() && w["stands"].is_object() {
        if let Some(e) = w["stands"]["error"].as_str() {
            bail!("the stands check has no result ({e}): no stand positions to draw");
        }
        Some(StandsAt {
            x_in: num(&w["stands"]["x_stand_in"], "stands x_stand_in")?,
            spacing_in: num(&w["stands"]["spacing_in"], "stands spacing_in")?,
            pad_in: num(&w["stands"]["pad_in"], "stands pad_in")?,
            inverted: w["stands"]["orientation"] == "inverted",
        })
    } else {
        None
    };
    ensure!(
        !spec.stands_diagram || stands.is_some(),
        "drawing: the support/stands diagram needs [stands] in the run (the stand positions are the stands \
         check's); add [stands] and run again, or turn stands_diagram off"
    );
    let mut graphics = vec![];
    if let Some(l) = &params.lettering {
        for s in l.spans(length)? {
            graphics.push(Placed {
                name: s.text,
                x0: s.x0_in,
                x1: s.x1_in,
                height: l.height_in,
                below_gunwale: l.below_gunwale_in,
                sides: spec.lettering_sides,
                lettering: true,
            });
        }
    }
    for g in &spec.graphics {
        ensure!(
            g.x_in + g.length_in <= length + 1e-9,
            "drawing.graphic '{}' runs off the {length:.1} in hull",
            g.name
        );
        ensure!(
            g.below_gunwale_in + g.height_in <= depth,
            "drawing.graphic '{}' is taller than the {depth} in hull side",
            g.name
        );
        graphics.push(Placed {
            name: g.name.clone(),
            x0: g.x_in,
            x1: g.x_in + g.length_in,
            height: g.height_in,
            below_gunwale: g.below_gunwale_in,
            sides: g.sides,
            lettering: false,
        });
    }

    // ---- isometric shading mesh ----
    let iso_mesh = export::build_mesh(&hull, &ExportSpec { stations: 97, girth: 25, ..es_wall.clone() })?;
    let iso = iso_mesh
        .tris
        .iter()
        .map(|tr| {
            let p = tr.map(|i| iso_mesh.verts[i as usize]);
            let (cx, cz) = ((p[0][0] + p[1][0] + p[2][0]) / 3.0, (p[0][2] + p[1][2] + p[2][2]) / 3.0);
            (p, zones.as_ref().is_some_and(|zl| zl.dense_at(cx, cz)))
        })
        .collect();

    // ---- mold ----
    let mold = mold_model(&hull, &es, &spec, board, boards, depth, t, &inner)?;

    // ---- BOM ----
    let developed_ft2 = developed_in2 / 144.0;
    let (bom, mold_bom, bom_record) = boms(&spec, &mixes, &laminate, developed_ft2, &graphics, &mold, board)?;

    let verified = w["verified"].as_bool().unwrap_or(false);
    let lettering_school = params.lettering.as_ref().map(|l| l.school.as_str());
    let mut warnings = school_warnings(&spec, lettering_school);
    if let Some(n) = params.lettering.as_ref().map(|l| l.canoe_name.trim()).filter(|n| !n.is_empty())
        && norm_name(n) != norm_name(&spec.canoe_name)
    {
        warnings.push(format!(
            "WARNING the title block canoe name \"{}\" differs from the hull lettering \"{n}\"",
            spec.canoe_name.trim()
        ));
    }
    if !verified {
        warnings.push(format!(
            "WARNING the design is NOT VERIFIED: {}",
            w["verify_failure"].as_str().unwrap_or("see the result")
        ));
    }
    Ok(Model {
        hull_name: hull.name.clone(),
        t,
        depth,
        flare_deg: ts.flare_deg,
        length,
        beam: 2.0 * ymax,
        x_beam,
        taper_bow,
        taper_stern,
        profile,
        keel,
        inner_keel,
        hollow,
        sheer,
        inner_sheer,
        end_view,
        stations,
        waterlines,
        mid_x: mid.0,
        mid_outer: as_pts(&mid),
        mid_inner: as_pts(&mid_in),
        waterline: wl,
        mixes,
        zones,
        laminate,
        stands,
        graphics,
        stem_radius_in: hull.stem.as_ref().map(|s| s.radius_in),
        air_pocket_in: c["air_pocket_in"].as_f64().filter(|l| *l > 0.0),
        iso,
        hull_weight_lb,
        developed_ft2,
        bom,
        mold_bom,
        bom_record,
        mold,
        verified,
        verify_note: w["verify_failure"].as_str().map(str::to_string),
        warnings,
        spec,
    })
}

/// (bow, stern) rocker, in, with the tolerance it must be whole boards within: from the shape
/// factors when the winner was generated (the shape search's own measure), else the lofted keel
/// at the same place (`hullgen::END_FRAC` of length from each end, above the lowest keel point).
fn rockers(hull: &Hull, c: &Value) -> Result<[(f64, f64); 2]> {
    if c["shape"].is_object() {
        let mut shape = c["shape"].clone();
        let saved = shape.as_object_mut().and_then(|o| o.remove("metrics"));
        // The search's own measurement when the result carries it, else measured again.
        if let Some(m) = saved
            && let (Some(b), Some(s)) = (m["rocker_bow_in"].as_f64(), m["rocker_stern_in"].as_f64())
        {
            return Ok([(b, 1e-6), (s, 1e-6)]);
        }
        let s: crate::hullgen::ShapeParams = serde_json::from_value(shape).context("winner shape")?;
        let m = crate::hullgen::metrics(&s);
        return Ok([(m.rocker_bow_in, 1e-6), (m.rocker_stern_in, 1e-6)]);
    }
    let bare = Hull { stem: None, ..hull.clone() };
    let loft = bare.loft();
    let l = hull.length;
    let low = linspace(0.0, l, 2001).iter().filter_map(|&x| loft.keel(x)).fold(f64::INFINITY, f64::min);
    let at = |x: f64| loft.keel(x).map(|k| k - low).context("hull has no keel");
    let e = crate::hullgen::END_FRAC * l;
    Ok([(at(e)?, ROCKER_TOL_IN), (at(l - e)?, ROCKER_TOL_IN)])
}

fn shoelace(p: &[P]) -> f64 {
    let n = p.len();
    0.5 * (0..n).map(|i| p[i][0] * p[(i + 1) % n][1] - p[(i + 1) % n][0] * p[i][1]).sum::<f64>()
}

/// The male mold: `boards` flat foam layers of `board` (tracing templates, blocks packing so
/// the joints line up), and the layer outlines nested on the foam sheets with the kerf between
/// pieces and the edge margin clear.
#[allow(clippy::too_many_arguments)]
fn mold_model(
    hull: &Hull,
    es: &ExportSpec,
    spec: &Drawing,
    board: f64,
    boards: usize,
    depth: f64,
    t: f64,
    rows: &[NetRow],
) -> Result<MoldModel> {
    let [sl, sw] = spec.foam_sheet_in;
    let (e, kerf) = (spec.edge_margin_in, spec.kerf_in);
    let ms = MoldSpec {
        foam_in: vec![board],
        // Every piece fits inside the margins of a foam sheet (the template is the foam piece).
        foam_sheets: vec![[sl - 2.0 * e, sw - 2.0 * e]],
        sheets: vec![[sl - 2.0 * (e - kerf), sw - 2.0 * (e - kerf)]],
        gap_in: kerf,
        packing: Packing::Blocks,
        ..MoldSpec::default()
    };
    let es = ExportSpec { pocket_in: None, filled: false, ..es.clone() };
    let lay = manufacture::stack_only(hull, &es, &ms).context("mold layers")?;
    ensure!(
        lay.layers.len() == boards && lay.layers.iter().all(|l| !l.upright),
        "mold: {boards} boards of {board} in make the {:.3} in mold, but the inner surface gives {} layers",
        depth - t,
        lay.layers.len()
    );
    let mut layers = vec![];
    for (i, l) in lay.layers.iter().enumerate() {
        ensure!(
            (l.pos_in - i as f64 * board).abs() < 1e-9 && (l.thick_in - board).abs() < 1e-12,
            "mold: layer {} is not one {board} in board",
            l.label
        );
        ensure!(
            !l.foam.is_empty() && l.foam.iter().all(|p| p.len() >= 3),
            "mold: layer {} has no outline",
            l.label
        );
        let xs = l.foam.iter().flatten().map(|p| p[0]);
        let (x0, x1) = xs.fold((f64::INFINITY, f64::NEG_INFINITY), |a, x| (a.0.min(x), a.1.max(x)));
        let ys = l.foam.iter().flatten().map(|p| p[1].abs()).fold(0.0, f64::max);
        let mine: Vec<&manufacture::Piece> = l.pieces.iter().filter_map(|&k| lay.pieces.get(k)).collect();
        let holes: Vec<P> = mine.iter().flat_map(|p| p.holes.iter().copied()).collect();
        // The template marks: centreline and station ticks are redrawn by the sheet; the rest
        // (both sides of the top face's outline) is the sanding line.
        let sanding: Vec<[P; 2]> = mine
            .iter()
            .flat_map(|p| p.marks.iter().copied())
            .filter(|[a, b]| !(a[1].abs() < 1e-9 && b[1].abs() < 1e-9) && (a[0] - b[0]).abs() > 1e-9)
            .collect();
        layers.push(MoldLayer {
            label: l.label.clone(),
            h0: l.pos_in,
            h1: l.pos_in + board,
            x0,
            x1,
            pieces: l.foam.clone(),
            piece_labels: l.foam_labels.clone(),
            sanding,
            holes,
            max_width: 2.0 * ys,
        });
    }
    // Nest the foam pieces on the sheets, quarter turns.
    let pieces: Vec<manufacture::Piece> = lay
        .layers
        .iter()
        .enumerate()
        .flat_map(|(li, l)| {
            l.foam.iter().zip(&l.foam_labels).map(move |(o, lab)| manufacture::Piece {
                label: lab.clone(),
                layer: li,
                outline: o.clone(),
                marks: vec![],
                holes: vec![],
                text: lab.clone(),
                text_at: [0.0, 0.0],
                text_deg: 0.0,
                text_h: 1.0,
                area_in2: shoelace(o).abs(),
                traces: vec![lab.clone()],
            })
        })
        .collect();
    let nested = manufacture::nest(&pieces, &ms, 90).context("nesting the foam layers on the foam sheets")?;
    let shift = e - kerf;
    let sheets: Vec<FoamSheet> = nested
        .iter()
        .map(|s| FoamSheet {
            parts: s
                .parts
                .iter()
                .map(|p| {
                    (
                        pieces[p.piece].label.clone(),
                        p.outline.iter().map(|q| [q[0] + shift, q[1] + shift]).collect(),
                    )
                })
                .collect(),
            used_in2: s.used_in2,
        })
        .collect();
    let glue_in2: f64 =
        lay.layers.iter().skip(1).flat_map(|l| l.foam.iter()).map(|o| shoelace(o).abs()).sum();
    let z_top = rows.first().and_then(|r| r.1.first()).map_or(depth, |p| p.1);
    let top: Vec<P> = rows.iter().filter_map(|r| r.1.last().map(|p| [r.0, z_top - p.1])).collect();
    Ok(MoldModel {
        board_in: board,
        height_in: boards as f64 * board,
        layers,
        top,
        sheet_in: [sl, sw],
        kerf_in: kerf,
        edge_in: e,
        sheets,
        hole_in: ms.hole_in,
        glue_ft2: glue_in2 / 144.0,
        surface_ft2: lay.labor.surface_ft2,
        joints: lay.stack.joints_in.clone(),
    })
}

type Boms = (Vec<BomLine>, Vec<BomLine>, Vec<String>);

/// The canoe BOM and the mold BOM, computed rows first, then the hand-entered rows; overrides
/// applied and recorded.
fn boms(
    spec: &Drawing,
    mixes: &[MixPart],
    lam: &Laminate,
    developed_ft2: f64,
    graphics: &[Placed],
    mold: &MoldModel,
    board: f64,
) -> Result<Boms> {
    let pct = |f: f64| format!("{:.0}%", 100.0 * f);
    let line = |part: &str, qty: f64, unit: &str, desc: String| BomLine {
        part: part.into(),
        qty,
        unit: unit.into(),
        desc,
        manual: false,
        overridden: None,
    };
    let mut bom = vec![];
    for m in mixes {
        bom.push(line(
            &m.part,
            m.volume_in3 / 1728.0 * (1.0 + spec.concrete_waste),
            "FT3",
            format!(
                "CONCRETE, {}; {:.3} FT3 IN THE WALL + {} WASTE",
                m.label(),
                m.volume_in3 / 1728.0,
                pct(spec.concrete_waste)
            ),
        ));
    }
    let nmesh = lam.meshes.len() as f64;
    if nmesh > 0.0 {
        bom.push(line(
            "M-01",
            developed_ft2 * nmesh * (1.0 + spec.mesh_overlap),
            "FT2",
            format!(
                "{}, {} LAYER(S) x {:.1} FT2 DEVELOPED + {} OVERLAP",
                spec.mesh_description.trim(),
                lam.meshes.len(),
                developed_ft2,
                pct(spec.mesh_overlap)
            ),
        ));
    }
    let sealed = developed_ft2 * f64::from(spec.sealer_sides) * f64::from(spec.sealer_coats);
    bom.push(line(
        "S-01",
        sealed / spec.sealer_coverage_ft2_per_gal,
        "GAL",
        format!(
            "{}, {} COAT(S) {} SIDE(S), {:.1} FT2 AT {} FT2/GAL",
            spec.sealer_description.trim(),
            spec.sealer_coats,
            spec.sealer_sides,
            sealed,
            spec.sealer_coverage_ft2_per_gal
        ),
    ));
    let vinyl_ft2: f64 =
        graphics.iter().map(|g| (g.x1 - g.x0) * g.height * f64::from(g.sides)).sum::<f64>() / 144.0;
    if vinyl_ft2 > 0.0 {
        bom.push(line(
            "V-01",
            vinyl_ft2 * (1.0 + spec.vinyl_waste),
            "FT2",
            format!(
                "{}, {:.2} FT2 PLACED ({} ITEM(S)) + {} WASTE",
                spec.vinyl_description.trim(),
                vinyl_ft2,
                graphics.len(),
                pct(spec.vinyl_waste)
            ),
        ));
    }
    let foam = |sheet: &str| {
        line(
            "F-01",
            mold.sheets.len() as f64,
            "SHEETS",
            format!(
                "{} {board} IN, {} x {} IN; {} LAYERS, SEE {sheet}",
                spec.foam_description.trim(),
                mold.sheet_in[0],
                mold.sheet_in[1],
                mold.layers.len()
            ),
        )
    };
    bom.push(foam("SHEET 2"));
    let mut mold_bom = vec![foam("NESTING ON THIS SHEET")];
    mold_bom.push(line(
        "A-01",
        (mold.glue_ft2 / spec.adhesive_coverage_ft2_per_unit).ceil(),
        &spec.adhesive_unit.trim().to_uppercase(),
        format!(
            "{}, {:.1} FT2 GLUED BETWEEN LAYERS AT {} FT2/{}",
            spec.adhesive_description.trim(),
            mold.glue_ft2,
            spec.adhesive_coverage_ft2_per_unit,
            spec.adhesive_unit.trim().to_uppercase()
        ),
    ));
    let treated = mold.surface_ft2 * f64::from(spec.surface_coats);
    mold_bom.push(line(
        "T-01",
        treated / spec.surface_coverage_ft2_per_gal,
        "GAL",
        format!(
            "{}, {} COAT(S) ON {:.1} FT2 MOLD SURFACE AT {} FT2/GAL",
            spec.surface_description.trim(),
            spec.surface_coats,
            mold.surface_ft2,
            spec.surface_coverage_ft2_per_gal
        ),
    ));
    let mut record = vec![];
    let manual = |r: &BomRow| BomLine {
        part: r.part_no.trim().into(),
        qty: r.qty,
        unit: r.unit.trim().to_uppercase(),
        desc: r.description.clone(),
        manual: true,
        overridden: None,
    };
    for (list, rows) in [(&mut bom, &spec.bom_rows), (&mut mold_bom, &spec.mold_bom_rows)] {
        for r in rows {
            ensure!(
                !list.iter().any(|l| l.part == r.part_no.trim()),
                "drawing BOM row '{}' uses a computed part number",
                r.part_no
            );
            record.push(format!("{}: entered by hand, {} {}", r.part_no.trim(), r.qty, r.unit.trim()));
            list.push(manual(r));
        }
    }
    for o in &spec.bom_overrides {
        let mut hit = false;
        for l in bom.iter_mut().chain(mold_bom.iter_mut()).filter(|l| l.part == o.part_no.trim()) {
            hit = true;
            let was = format!("{} {}", l.qty_text(), l.unit);
            if let Some(q) = o.qty {
                l.qty = q;
            }
            if let Some(d) = &o.description {
                l.desc = d.clone();
            }
            let why =
                if o.reason.trim().is_empty() { String::new() } else { format!(" ({})", o.reason.trim()) };
            l.overridden =
                Some(format!("{}: computed {was}, drawn {} {}{why}", l.part, l.qty_text(), l.unit));
        }
        ensure!(hit, "drawing.bom_overrides: no BOM row '{}'", o.part_no);
        if let Some(r) = bom
            .iter()
            .chain(&mold_bom)
            .find(|l| l.part == o.part_no.trim())
            .and_then(|l| l.overridden.clone())
            && !record.contains(&r)
        {
            record.push(r);
        }
    }
    Ok((bom, mold_bom, record))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn school_name_must_be_the_official_one() {
        let d = Drawing {
            school: "UVA".into(),
            official_school_name: "University  of Virginia".into(),
            ..Drawing::default()
        };
        let w = school_warnings(&d, Some("University of Virginia"));
        assert_eq!(w.len(), 1, "{w:?}");
        assert!(w[0].contains("\"UVA\"") && w[0].contains("official name \"University of Virginia\""));
        let ok = Drawing { school: "University of Virginia".into(), ..d.clone() };
        assert!(school_warnings(&ok, Some("University of Virginia ")).is_empty(), "spacing does not count");
        assert_eq!(school_warnings(&ok, Some("U. Virginia")).len(), 1, "the lettering is checked too");
    }

    #[test]
    fn saving_the_inputs_keeps_the_result_text_and_key_order() {
        let d = Drawing { canoe_name: "J \"x\"".into(), ..Drawing::default() };
        for text in [
            "{\"format\":\"jaguar-result/1\",\"b\":[1,{\"c\":\"}\\\"\"}],\"a\":2}",
            "{
  \"format\": \"jaguar-result/1\",
  \"b\": [
    1
  ],
  \"a\": 2
}",
        ] {
            let once = with_spec(text, &d).unwrap();
            assert!(once.starts_with(&text[..text.find(",").unwrap()]), "format stays first: {once}");
            let v: Value = serde_json::from_str(&once).unwrap();
            assert_eq!(v["drawing"]["canoe_name"], "J \"x\"");
            assert_eq!(v["a"], 2);
            // Saving again replaces it (one drawing entry), the rest unchanged.
            let twice = with_spec(&once, &Drawing { canoe_name: "K".into(), ..d.clone() }).unwrap();
            assert_eq!(twice.matches("\"drawing\"").count(), 1);
            assert_eq!(serde_json::from_str::<Value>(&twice).unwrap()["drawing"]["canoe_name"], "K");
            assert!(twice.find("\"b\"").unwrap() < twice.find("\"a\"").unwrap());
        }
    }

    #[test]
    fn missing_title_block_text_is_named() {
        let e =
            require(&Drawing { canoe_name: "Jaguar".into(), ..Drawing::default() }).unwrap_err().to_string();
        assert!(e.contains("school, official_school_name, drawn_by, drawn_date"), "{e}");
    }

    #[test]
    fn station_breadth_interpolates_above_the_keel_only() {
        let s = Station { x: 0.0, keel: Some(1.0), half: vec![[0.0, 1.0], [2.0, 2.0], [4.0, 4.0]] };
        assert_eq!(s.breadth(0.5), None);
        assert_eq!(s.breadth(1.5), Some(1.0));
        assert_eq!(s.breadth(3.0), Some(3.0));
        assert_eq!(s.breadth(5.0), None);
    }
}
