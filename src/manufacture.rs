//! Mold boards for the shop (web Manufacture tab, `jaguar mold`). Below `export` in the layout:
//! reads the winner's geometry, never feeds physics.
//!
//! The mold is a MALE mold: its surface is the canoe's inner wall ([`export::Mesh::inner_rows`]:
//! the outer hull offset inward by the wall, the solid concrete ends left out, no air pocket
//! bulkheads), built upside down with the gunwale on the shop floor. It is a stack of foam
//! sheets (hand-shaped, never water-jet cut) with a hardboard sheet between each pair; the
//! hardboard is cut to the exact outline and the foam is sanded down to meet its edges.
//!
//! * [`Orientation::Flat`]: layers lie flat. Board k (k = 0 on the floor) sits at
//!   h = k (foam + board) above the floor and is the inner hull cut at its mid-plane, a
//!   waterline at z = depth - h - board / 2, symmetric about the centreline. The stack ends with
//!   the last board whose mid-plane still cuts the hull; foam on top covers the keel. A layer
//!   longer than the sheet is cut into the fewest equal lengths that fit (square joints).
//! * [`Orientation::Vertical`]: boards stand upright across the canoe like ribs, board k at
//!   x = xa + board / 2 + k (foam + board) from the bow end xa of the hollow run, each the inner
//!   cross-section there, closed by the gunwale line on the floor.
//!
//! Pieces are packed onto sheets ([`nest`]): every piece is rasterised to 1/4 in cells grown by
//! half the gap (so two pieces never come closer than `gap_in`, nor closer to a sheet edge), and
//! placed largest first at the lowest free spot along the sheet length (then width), trying 0,
//! 90, 180 and 270 deg, on the first sheet with room. The DXF ([`to_dxf`]) is R12, inches, one
//! file per sheet: layer CUT the piece outlines (closed polylines), layer LABEL the piece names,
//! a centreline and 12 in station ticks (to etch or ignore), layer SHEET the sheet edge.
//! Deterministic: same input, same files.

use crate::export::{self, ExportSpec, NetRow};
use crate::hull::Hull;
use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::fmt::Write as _;

pub const MOLD_FORMAT: &str = "jaguar-mold/1";
/// Nesting raster cell, in.
const CELL: f64 = 0.25;
/// Export resolution the mold is cut from: about 0.5 in between stations on a 20 ft canoe.
const STATIONS: usize = 481;
const GIRTH: usize = 61;
/// Station ticks on the LABEL layer every this many inches from the bow.
const TICK_IN: f64 = 12.0;

pub type Pt = [f64; 2];

/// Layers, their pieces and the sheets they are packed on.
type Built = (Vec<Layer>, Vec<Piece>, Vec<Sheet>);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Orientation {
    /// Layers laid flat (horizontal boards, waterline outlines).
    Flat,
    /// Boards standing upright across the canoe (cross-section outlines).
    Vertical,
}

/// What to build the mold from. Inches.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct MoldSpec {
    pub orientation: Orientation,
    /// Foam sheet thickness.
    pub foam_in: f64,
    /// Hardboard sheet thickness.
    pub board_in: f64,
    /// Hardboard stock sheet length and width.
    pub sheet_length_in: f64,
    pub sheet_width_in: f64,
    /// Least space between two pieces, and between a piece and the sheet edge.
    pub gap_in: f64,
}

impl Default for MoldSpec {
    fn default() -> Self {
        MoldSpec {
            orientation: Orientation::Flat,
            foam_in: 2.0,
            board_in: 0.125,
            sheet_length_in: 96.0,
            sheet_width_in: 48.0,
            gap_in: 0.25,
        }
    }
}

impl MoldSpec {
    fn validate(&self) -> Result<()> {
        for (v, what) in [
            (self.foam_in, "foam thickness"),
            (self.board_in, "hardboard thickness"),
            (self.sheet_length_in, "sheet length"),
            (self.sheet_width_in, "sheet width"),
        ] {
            ensure!(v.is_finite() && v > 0.0, "{what} must be > 0, got {v}");
        }
        ensure!(self.gap_in.is_finite() && self.gap_in >= 0.0, "gap must be >= 0, got {}", self.gap_in);
        ensure!(
            2.0 * self.gap_in + 1.0 < self.sheet_length_in.min(self.sheet_width_in),
            "a {} in gap leaves no room on a {} x {} in sheet",
            self.gap_in,
            self.sheet_length_in,
            self.sheet_width_in
        );
        Ok(())
    }
}

/// One hardboard piece, in its own frame: flat layers in hull plan coordinates (x from the bow,
/// y across, centreline y = 0); vertical ribs in section coordinates (y across, height above the
/// floor).
#[derive(Debug, Clone, Serialize)]
pub struct Piece {
    pub label: String,
    /// Index into [`Layout::layers`].
    pub layer: usize,
    /// Closed outline (last point not repeated), counter-clockwise.
    pub outline: Vec<Pt>,
    /// Marks to etch: centreline and station ticks.
    pub marks: Vec<[Pt; 2]>,
    pub text: String,
    pub text_at: Pt,
    pub text_deg: f64,
    pub text_h: f64,
    pub area_in2: f64,
}

/// One board of the stack.
#[derive(Debug, Clone, Serialize)]
pub struct Layer {
    pub label: String,
    /// Flat: height of the board's underside above the floor. Vertical: x of its bow face.
    pub pos_in: f64,
    /// Where the outline is cut: flat, the canoe height z of the mid-plane (keel = 0);
    /// vertical, the x of the mid-plane.
    pub cut_in: f64,
    pub pieces: Vec<usize>,
}

/// A piece on a sheet: its outline, marks and text moved there (rotation about the origin, then
/// the shift).
#[derive(Debug, Clone, Serialize)]
pub struct Placed {
    pub piece: usize,
    pub rot_deg: u32,
    pub shift: Pt,
    pub outline: Vec<Pt>,
    pub marks: Vec<[Pt; 2]>,
    pub text_at: Pt,
    pub text_deg: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Sheet {
    pub parts: Vec<Placed>,
    /// Area of the pieces on it, in^2.
    pub used_in2: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stack {
    pub boards: usize,
    pub foam_layers: usize,
    /// Flat: floor to the top of the inner keel. Vertical: bow to stern of the hollow run.
    pub height_in: f64,
    /// Hollow run length and inner beam at the gunwale.
    pub length_in: f64,
    pub beam_in: f64,
    /// Mold depth (gunwale to keel bottom, outer) and wall the inner surface is offset by.
    pub depth_in: f64,
    pub wall_in: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Layout {
    pub format: String,
    pub hull: String,
    pub spec: MoldSpec,
    pub stack: Stack,
    pub layers: Vec<Layer>,
    pub pieces: Vec<Piece>,
    pub sheets: Vec<Sheet>,
    /// Pieces area over the sheets' area.
    pub utilization: f64,
}

/// Mold boards for a `jaguar-result/1` winner. Err if it has no winner, or a piece fits no sheet.
pub fn from_result(text: &str, spec: &MoldSpec) -> Result<Layout> {
    let (hull, mut es) = export::from_result(text, STATIONS, GIRTH)?;
    es.pocket_in = None;
    layout(&hull, &es, spec)
}

/// Mold boards for `hull` with the wall and depth of `es` (`es.thickness_in` required).
pub fn layout(hull: &Hull, es: &ExportSpec, spec: &MoldSpec) -> Result<Layout> {
    spec.validate()?;
    let wall = es.thickness_in.context("the mold needs the wall thickness (the inner surface)")?;
    let es = ExportSpec { pocket_in: None, filled: false, ..es.clone() };
    let mesh = export::build_mesh(hull, &es)?;
    let rows = mesh.inner_rows.context("this hull has no single hollow inner surface to build a mold on")?;
    let z_top = rows[0].1[0].1;
    let (xa, xb) = (rows[0].0, rows[rows.len() - 1].0);
    let keel_min = rows.iter().map(|r| r.1.last().unwrap().1).fold(f64::INFINITY, f64::min);
    let beam = 2.0 * rows.iter().map(|r| r.1[0].0).fold(0.0, f64::max);
    let pitch = spec.foam_in + spec.board_in;
    let (mut layers, mut pieces, mut nested): (Vec<Layer>, Vec<Piece>, Option<Vec<Sheet>>) =
        (vec![], vec![], None);
    let stack = match spec.orientation {
        Orientation::Flat => {
            let height = z_top - keel_min;
            let mut cuts: Vec<(Layer, Vec<Vec<Pt>>)> = vec![];
            loop {
                let pos = cuts.len() as f64 * pitch;
                let z = z_top - pos - 0.5 * spec.board_in;
                if z <= keel_min + 1e-6 {
                    break;
                }
                let label = format!("L{}", cuts.len() + 1);
                cuts.push((Layer { label, pos_in: pos, cut_in: z, pieces: vec![] }, waterline(&rows, z)));
            }
            // Fewest pieces that fit is not always fewest sheets (a 70 in piece on a 96 in sheet
            // wastes the rest of its length): try shorter pieces too, keep the fewest sheets
            // (then fewest pieces, then the longest pieces).
            let usable = spec.sheet_length_in.max(spec.sheet_width_in) - 2.0 * spec.gap_in;
            let limits: Vec<Option<f64>> =
                std::iter::once(None).chain((5..20).rev().map(|k| Some(usable * k as f64 / 20.0))).collect();
            let tries: Vec<Result<Built>> = limits
                .par_iter()
                .map(|&max_len| {
                    let (mut ls, mut ps) = (vec![], vec![]);
                    for (li, (layer, runs)) in cuts.iter().enumerate() {
                        let mut layer = layer.clone();
                        for (ri, run) in runs.iter().enumerate() {
                            let tag = if runs.len() > 1 {
                                format!("{}{}", layer.label, (b'a' + ri as u8) as char)
                            } else {
                                layer.label.clone()
                            };
                            for p in split_run(run, &tag, li, max_len, spec)? {
                                layer.pieces.push(ps.len());
                                ps.push(p);
                            }
                        }
                        ls.push(layer);
                    }
                    let sh = nest(&ps, spec)?;
                    Ok((ls, ps, sh))
                })
                .collect();
            let mut best: Option<Built> = None;
            let mut first_err = None;
            for t in tries {
                match t {
                    Ok(t) => {
                        if best.as_ref().is_none_or(|b| (t.2.len(), t.1.len()) < (b.2.len(), b.1.len())) {
                            best = Some(t);
                        }
                    }
                    Err(e) => {
                        first_err.get_or_insert(e);
                    }
                }
            }
            let Some((ls, ps, sh)) = best else {
                return Err(first_err
                    .unwrap_or_else(|| anyhow::anyhow!("the mold is thinner than one hardboard sheet")));
            };
            (layers, pieces, nested) = (ls, ps, Some(sh));
            ensure!(!layers.is_empty(), "the mold is thinner than one hardboard sheet");
            let top = (layers.len() - 1) as f64 * pitch + spec.board_in;
            let above = ((height - top) / spec.foam_in - 1e-9).ceil().max(0.0) as usize;
            Stack {
                boards: layers.len(),
                foam_layers: layers.len() - 1 + above,
                height_in: height,
                length_in: xb - xa,
                beam_in: beam,
                depth_in: es.depth_in,
                wall_in: wall,
            }
        }
        Orientation::Vertical => {
            let mut k = 0;
            loop {
                let pos = xa + k as f64 * pitch;
                if pos + spec.board_in > xb + 1e-9 {
                    break;
                }
                let x = pos + 0.5 * spec.board_in;
                let label = format!("R{}", k + 1);
                let p = rib(&rows, x, z_top, &label, layers.len());
                layers.push(Layer { label, pos_in: pos, cut_in: x, pieces: vec![pieces.len()] });
                pieces.push(p);
                k += 1;
            }
            ensure!(!layers.is_empty(), "the mold is shorter than one hardboard sheet");
            let end = (layers.len() - 1) as f64 * pitch + spec.board_in;
            let rest = xb - xa - end;
            let above = (rest / spec.foam_in - 1e-9).ceil().max(0.0) as usize;
            Stack {
                boards: layers.len(),
                foam_layers: layers.len() - 1 + above,
                height_in: xb - xa,
                length_in: xb - xa,
                beam_in: beam,
                depth_in: es.depth_in,
                wall_in: wall,
            }
        }
    };
    let sheets = match nested {
        Some(s) => s,
        None => nest(&pieces, spec)?,
    };
    let used: f64 = sheets.iter().map(|s| s.used_in2).sum();
    let utilization = used / (sheets.len() as f64 * spec.sheet_length_in * spec.sheet_width_in);
    Ok(Layout {
        format: MOLD_FORMAT.into(),
        hull: hull.name.clone(),
        spec: spec.clone(),
        stack,
        layers,
        pieces,
        sheets,
        utilization,
    })
}

/// Outermost half-breadth of one half girth at height z. None if its keel is above z.
fn half_at(half: &[(f64, f64)], z: f64) -> Option<f64> {
    if half.last()?.1 > z {
        return None;
    }
    let mut y: Option<f64> = None;
    for w in half.windows(2) {
        let (a, b) = (w[0], w[1]);
        if z < a.1.min(b.1) || z > a.1.max(b.1) {
            continue;
        }
        let v = if (a.1 - b.1).abs() < 1e-12 {
            a.0.max(b.0)
        } else {
            a.0 + (z - a.1) / (b.1 - a.1) * (b.0 - a.0)
        };
        y = Some(y.map_or(v, |u| u.max(v)));
    }
    y
}

/// The inner surface cut at height z: each run of stations it cuts, as the starboard chain
/// (x increasing, y >= 0) from the centreline back to the centreline. Where the keel rises
/// through z the run ends on the centreline between stations; at the end of the hollow run it
/// ends square (the bulkhead to the solid end).
fn waterline(rows: &[NetRow], z: f64) -> Vec<Vec<Pt>> {
    let keel = |i: usize| rows[i].1.last().unwrap().1;
    let at = |i: usize| {
        let (ka, kb) = (keel(i - 1), keel(i));
        let s = if (kb - ka).abs() < 1e-12 { 0.5 } else { ((z - ka) / (kb - ka)).clamp(0.0, 1.0) };
        rows[i - 1].0 + s * (rows[i].0 - rows[i - 1].0)
    };
    let (mut runs, mut cur): (Vec<Vec<Pt>>, Vec<Pt>) = (vec![], vec![]);
    for (i, (x, half)) in rows.iter().enumerate() {
        match half_at(half, z) {
            Some(y) => {
                if cur.is_empty() {
                    cur.push([if i > 0 { at(i) } else { *x }, 0.0]);
                }
                cur.push([*x, y]);
            }
            None if !cur.is_empty() => {
                cur.push([at(i), 0.0]);
                runs.push(std::mem::take(&mut cur));
            }
            None => {}
        }
    }
    if !cur.is_empty() {
        cur.push([rows[rows.len() - 1].0, 0.0]);
        runs.push(cur);
    }
    for r in &mut runs {
        r.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
    }
    runs.retain(|r| r.len() >= 3 && r[r.len() - 1][0] - r[0][0] > 1e-6);
    runs
}

/// y of a chain (x non-decreasing) at x, inside it.
fn chain_y(chain: &[Pt], x: f64) -> f64 {
    let k = chain.partition_point(|p| p[0] < x).clamp(1, chain.len() - 1);
    let (a, b) = (chain[k - 1], chain[k]);
    if b[0] - a[0] < 1e-12 { a[1].max(b[1]) } else { a[1] + (x - a[0]) / (b[0] - a[0]) * (b[1] - a[1]) }
}

/// Starboard chain -> closed outline mirrored about y = 0, counter-clockwise (port side bow to
/// stern, then starboard back).
fn mirrored(chain: &[Pt]) -> Vec<Pt> {
    let mut out: Vec<Pt> = chain.iter().map(|p| [p[0], -p[1]]).collect();
    out.extend(chain.iter().rev().filter(|p| p[1] > 1e-9).copied());
    if out.first().is_some_and(|f| {
        out.len() > 1
            && (f[0] - out[out.len() - 1][0]).abs() < 1e-9
            && (f[1] - out[out.len() - 1][1]).abs() < 1e-9
    }) {
        out.pop();
    }
    out
}

fn area(p: &[Pt]) -> f64 {
    let n = p.len();
    0.5 * (0..n).map(|i| p[i][0] * p[(i + 1) % n][1] - p[(i + 1) % n][0] * p[i][1]).sum::<f64>()
}

fn bbox(p: &[Pt]) -> (Pt, Pt) {
    p.iter().fold(([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]), |(lo, hi), q| {
        ([lo[0].min(q[0]), lo[1].min(q[1])], [hi[0].max(q[0]), hi[1].max(q[1])])
    })
}

/// Does a w x h box fit the sheet's usable area in some quarter turn?
fn fits(w: f64, h: f64, spec: &MoldSpec) -> bool {
    let r = raster_pad(spec);
    let cells = |v: f64| ((v + 2.0 * r) / CELL).ceil() as usize + 1;
    let (nx, ny) = sheet_cells(spec);
    let (cw, ch) = (cells(w), cells(h));
    (cw <= nx && ch <= ny) || (ch <= nx && cw <= ny)
}

/// A flat layer's run cut into the fewest equal lengths that each fit a sheet (and are no longer
/// than `max_len`): pieces bow to stern, labelled `<tag>-1`, `<tag>-2`, ...
fn split_run(
    run: &[Pt],
    tag: &str,
    layer: usize,
    max_len: Option<f64>,
    spec: &MoldSpec,
) -> Result<Vec<Piece>> {
    let (x0, x1) = (run[0][0], run[run.len() - 1][0]);
    let span = x1 - x0;
    let n_min = max_len.map_or(1, |m| ((span / m).ceil() as usize).max(1));
    let n = (n_min..=200)
        .find(|&n| {
            (0..n).all(|k| {
                let (c0, c1) = (x0 + span * k as f64 / n as f64, x0 + span * (k + 1) as f64 / n as f64);
                let w = run
                    .iter()
                    .filter(|p| p[0] >= c0 && p[0] <= c1)
                    .map(|p| p[1])
                    .fold(chain_y(run, c0).max(chain_y(run, c1)), f64::max);
                fits(c1 - c0, 2.0 * w, spec)
            })
        })
        .with_context(|| {
            format!(
                "layer {tag} ({:.1} in wide) does not fit a {} x {} in sheet in any number of pieces",
                2.0 * run.iter().map(|p| p[1]).fold(0.0, f64::max),
                spec.sheet_length_in,
                spec.sheet_width_in
            )
        })?;
    let mut out = vec![];
    for k in 0..n {
        let c0 = x0 + span * k as f64 / n as f64;
        let c1 = if k + 1 == n { x1 } else { x0 + span * (k + 1) as f64 / n as f64 };
        let mut chain: Vec<Pt> = vec![];
        if k == 0 {
            chain.extend(run.iter().take_while(|p| p[0] <= c0 + 1e-12));
        } else {
            chain.push([c0, chain_y(run, c0)]);
        }
        chain.extend(run.iter().filter(|p| p[0] > c0 + 1e-12 && p[0] < c1 - 1e-12));
        if k + 1 == n {
            chain.extend(run.iter().skip_while(|p| p[0] < c1 - 1e-12));
        } else {
            chain.push([c1, chain_y(run, c1)]);
        }
        chain.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
        let outline = mirrored(&chain);
        let label = if n > 1 { format!("{tag}-{}", k + 1) } else { tag.to_string() };
        let xm = 0.5 * (c0 + c1);
        let wm = 2.0 * chain_y(&chain, xm);
        let text_h = (0.3 * wm).clamp(0.2, 1.0);
        let mut marks = vec![];
        if c1 - c0 > 2.0 {
            marks.push([[c0 + 0.5, 0.0], [c1 - 0.5, 0.0]]);
        }
        let mut t = (c0 / TICK_IN).floor() * TICK_IN + TICK_IN;
        while t < c1 - 1e-9 {
            let hw = (0.5f64).min(0.4 * chain_y(&chain, t));
            if hw > 0.05 {
                marks.push([[t, -hw], [t, hw]]);
            }
            t += TICK_IN;
        }
        out.push(Piece {
            text: format!("{label} <BOW"),
            label,
            layer,
            area_in2: area(&outline).abs(),
            outline,
            marks,
            text_at: [xm, 0.6 * text_h],
            text_deg: 0.0,
            text_h,
        });
    }
    Ok(out)
}

/// The rib at x: the inner section there (linear between the neighbouring stations, point by
/// point, as the export's bulkhead rows), in (y, height above the floor), closed by the floor.
fn rib(rows: &[NetRow], x: f64, z_top: f64, label: &str, layer: usize) -> Piece {
    let k = rows.partition_point(|r| r.0 < x).clamp(1, rows.len() - 1);
    let (a, b) = (&rows[k - 1], &rows[k]);
    let s = if b.0 - a.0 < 1e-12 { 0.0 } else { ((x - a.0) / (b.0 - a.0)).clamp(0.0, 1.0) };
    let half: Vec<Pt> =
        a.1.iter().zip(&b.1).map(|(p, q)| [p.0 + s * (q.0 - p.0), z_top - (p.1 + s * (q.1 - p.1))]).collect();
    // Starboard sheer (on the floor) -> keel; then the port side back up.
    let mut outline: Vec<Pt> = half.iter().map(|p| [p[0], p[1]]).collect();
    outline.extend(half.iter().rev().filter(|p| p[0] > 1e-9).map(|p| [-p[0], p[1]]));
    outline.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
    if area(&outline) < 0.0 {
        outline.reverse();
    }
    let hk = half.last().map_or(0.0, |p| p[1]);
    let text_h = (0.15 * hk).clamp(0.2, 0.75);
    let marks = if hk > 1.5 { vec![[[0.0, 0.5], [0.0, hk - 0.5]]] } else { vec![] };
    Piece {
        label: label.to_string(),
        layer,
        area_in2: area(&outline).abs(),
        outline,
        marks,
        text: format!("{label} x={x:.2}"),
        text_at: [0.0, (0.3 * hk).max(0.6 * text_h)],
        text_deg: 0.0,
        text_h,
    }
}

// ---------- nesting ----------

/// Raster growth: half the gap plus half a cell diagonal, so that two pieces whose rasters do
/// not overlap are at least `gap_in` apart (and a piece inside the sheet raster is inside the
/// sheet edge margin).
fn raster_pad(spec: &MoldSpec) -> f64 {
    0.5 * spec.gap_in + CELL * std::f64::consts::FRAC_1_SQRT_2
}

/// Usable sheet raster: the sheet less `gap_in` at every edge.
fn sheet_cells(spec: &MoldSpec) -> (usize, usize) {
    let c = |v: f64| ((v - 2.0 * spec.gap_in) / CELL).floor().max(0.0) as usize;
    (c(spec.sheet_length_in), c(spec.sheet_width_in))
}

/// A piece rasterised: cell (i, j) covers origin + [i, i+1] x [j, j+1] cells and is occupied
/// when its centre is within `r` of the piece (inside, or near its edge).
struct Raster {
    w: usize,
    h: usize,
    origin: Pt,
    /// Occupied cells per row, as inclusive [a, b] runs.
    rows: Vec<Vec<(usize, usize)>>,
}

fn raster(poly: &[Pt], r: f64) -> Raster {
    let (lo, hi) = bbox(poly);
    let origin = [lo[0] - r - 0.5 * CELL, lo[1] - r - 0.5 * CELL];
    let w = ((hi[0] - lo[0] + 2.0 * r + CELL) / CELL).ceil() as usize + 1;
    let h = ((hi[1] - lo[1] + 2.0 * r + CELL) / CELL).ceil() as usize + 1;
    let mut occ = vec![false; w * h];
    let cx = |i: usize| origin[0] + (i as f64 + 0.5) * CELL;
    let cy = |j: usize| origin[1] + (j as f64 + 0.5) * CELL;
    let n = poly.len();
    for j in 0..h {
        let y = cy(j);
        let mut xs: Vec<f64> = (0..n)
            .filter_map(|e| {
                let (a, b) = (poly[e], poly[(e + 1) % n]);
                ((a[1] <= y) != (b[1] <= y)).then(|| a[0] + (y - a[1]) / (b[1] - a[1]) * (b[0] - a[0]))
            })
            .collect();
        xs.sort_by(f64::total_cmp);
        for p in xs.as_chunks::<2>().0 {
            for i in 0..w {
                let x = cx(i);
                if x >= p[0] && x <= p[1] {
                    occ[j * w + i] = true;
                }
            }
        }
    }
    let cell = |v: f64, o: f64| ((v - o) / CELL).floor();
    for e in 0..n {
        let (a, b) = (poly[e], poly[(e + 1) % n]);
        let i0 = cell(a[0].min(b[0]) - r, origin[0]).max(0.0) as usize;
        let i1 = (cell(a[0].max(b[0]) + r, origin[0]) as usize).min(w - 1);
        let j0 = cell(a[1].min(b[1]) - r, origin[1]).max(0.0) as usize;
        let j1 = (cell(a[1].max(b[1]) + r, origin[1]) as usize).min(h - 1);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let ll = dx * dx + dy * dy;
        for j in j0..=j1 {
            for i in i0..=i1 {
                let (px, py) = (cx(i) - a[0], cy(j) - a[1]);
                let t = if ll > 0.0 { ((px * dx + py * dy) / ll).clamp(0.0, 1.0) } else { 0.0 };
                if (px - t * dx).hypot(py - t * dy) <= r {
                    occ[j * w + i] = true;
                }
            }
        }
    }
    // Trim to the occupied cells, so the raster's size is what the piece needs on the sheet.
    let used = |i: usize, j: usize| occ[j * w + i];
    let (i0, i1) = (
        (0..w).find(|&i| (0..h).any(|j| used(i, j))).unwrap_or(0),
        (0..w).rfind(|&i| (0..h).any(|j| used(i, j))).unwrap_or(0),
    );
    let (j0, j1) = (
        (0..h).find(|&j| (0..w).any(|i| used(i, j))).unwrap_or(0),
        (0..h).rfind(|&j| (0..w).any(|i| used(i, j))).unwrap_or(0),
    );
    let origin = [origin[0] + i0 as f64 * CELL, origin[1] + j0 as f64 * CELL];
    let (occ, w0, w, h) = (occ, w, i1 - i0 + 1, j1 - j0 + 1);
    let occ: Vec<bool> =
        (0..h).flat_map(|j| (0..w).map(move |i| (i, j))).map(|(i, j)| occ[(j + j0) * w0 + i + i0]).collect();
    let rows = (0..h)
        .map(|j| {
            let mut runs = vec![];
            let mut i = 0;
            while i < w {
                if occ[j * w + i] {
                    let s = i;
                    while i < w && occ[j * w + i] {
                        i += 1;
                    }
                    runs.push((s, i - 1));
                } else {
                    i += 1;
                }
            }
            runs
        })
        .collect();
    Raster { w, h, origin, rows }
}

/// Sheet occupancy as per-row prefix counts (pre[j][i] = occupied cells left of i).
struct Grid {
    nx: usize,
    ny: usize,
    pre: Vec<Vec<u32>>,
}

impl Grid {
    fn new(nx: usize, ny: usize) -> Grid {
        Grid { nx, ny, pre: vec![vec![0; nx + 1]; ny] }
    }
    fn free(&self, r: &Raster, x: usize, y: usize) -> bool {
        r.rows.iter().enumerate().all(|(j, runs)| {
            let p = &self.pre[y + j];
            runs.iter().all(|&(a, b)| p[x + b + 1] == p[x + a])
        })
    }
    /// Lowest free spot: least x (along the sheet length), then least y. A tie between quarter
    /// turns goes to the first (0 deg: a long flat-layer piece along the sheet length).
    fn spot(&self, r: &Raster) -> Option<(usize, usize)> {
        if r.w > self.nx || r.h > self.ny {
            return None;
        }
        (0..=self.nx - r.w).find_map(|x| (0..=self.ny - r.h).find(|&y| self.free(r, x, y)).map(|y| (x, y)))
    }
    fn fill(&mut self, r: &Raster, x: usize, y: usize) {
        for (j, runs) in r.rows.iter().enumerate() {
            let p = &mut self.pre[y + j];
            let mut occ: Vec<bool> = (0..self.nx).map(|i| p[i + 1] > p[i]).collect();
            for &(a, b) in runs {
                occ[x + a..=x + b].iter_mut().for_each(|c| *c = true);
            }
            for i in 0..self.nx {
                p[i + 1] = p[i] + u32::from(occ[i]);
            }
        }
    }
}

fn rot(p: Pt, deg: u32) -> Pt {
    match deg {
        90 => [-p[1], p[0]],
        180 => [-p[0], -p[1]],
        270 => [p[1], -p[0]],
        _ => p,
    }
}

/// Pack the pieces onto as few sheets as the greedy placement finds (module doc), trying the
/// pieces largest box first, longest first and widest first; the fewest sheets wins (ties: that
/// order).
pub fn nest(pieces: &[Piece], spec: &MoldSpec) -> Result<Vec<Sheet>> {
    let dims = |p: &Piece| {
        let (lo, hi) = bbox(&p.outline);
        let (a, b) = (hi[0] - lo[0], hi[1] - lo[1]);
        (a.max(b), a.min(b))
    };
    let keys: [fn((f64, f64)) -> f64; 3] = [|(l, w)| l * w, |(l, _)| l, |(_, w)| w];
    let mut best: Option<Vec<Sheet>> = None;
    for key in keys {
        let mut order: Vec<usize> = (0..pieces.len()).collect();
        order.sort_by(|&a, &b| key(dims(&pieces[b])).total_cmp(&key(dims(&pieces[a]))).then(a.cmp(&b)));
        let s = nest_order(pieces, &order, spec)?;
        if best.as_ref().is_none_or(|b| s.len() < b.len()) {
            best = Some(s);
        }
    }
    Ok(best.unwrap_or_default())
}

fn nest_order(pieces: &[Piece], order: &[usize], spec: &MoldSpec) -> Result<Vec<Sheet>> {
    let r = raster_pad(spec);
    let (nx, ny) = sheet_cells(spec);
    let m = spec.gap_in;
    let mut grids: Vec<Grid> = vec![];
    let mut sheets: Vec<Sheet> = vec![];
    for &pi in order {
        let p = &pieces[pi];
        let rasters: Vec<(u32, Vec<Pt>, Raster)> = [0, 90, 180, 270]
            .into_iter()
            .map(|d| {
                let q: Vec<Pt> = p.outline.iter().map(|&v| rot(v, d)).collect();
                let ras = raster(&q, r);
                (d, q, ras)
            })
            .collect();
        let best = |g: &Grid| {
            rasters
                .iter()
                .enumerate()
                .filter_map(|(k, (_, _, ras))| g.spot(ras).map(|(x, y)| ((x, y), k, x, y)))
                .min_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)))
        };
        let mut hit = grids.iter().enumerate().find_map(|(s, g)| best(g).map(|b| (s, b)));
        if hit.is_none() {
            grids.push(Grid::new(nx, ny));
            sheets.push(Sheet { parts: vec![], used_in2: 0.0 });
            let s = grids.len() - 1;
            match best(&grids[s]) {
                Some(b) => hit = Some((s, b)),
                None => bail!(
                    "piece {} does not fit a {} x {} in sheet with a {} in gap",
                    p.label,
                    spec.sheet_length_in,
                    spec.sheet_width_in,
                    spec.gap_in
                ),
            }
        }
        let (s, (_, k, x, y)) = hit.unwrap();
        let (d, q, ras) = &rasters[k];
        grids[s].fill(ras, x, y);
        let shift = [m + x as f64 * CELL - ras.origin[0], m + y as f64 * CELL - ras.origin[1]];
        let mv = |v: Pt| {
            let w = rot(v, *d);
            [w[0] + shift[0], w[1] + shift[1]]
        };
        sheets[s].used_in2 += p.area_in2;
        sheets[s].parts.push(Placed {
            piece: pi,
            rot_deg: *d,
            shift,
            outline: q.iter().map(|v| [v[0] + shift[0], v[1] + shift[1]]).collect(),
            marks: p.marks.iter().map(|[a, b]| [mv(*a), mv(*b)]).collect(),
            text_at: mv(p.text_at),
            text_deg: (p.text_deg + f64::from(*d)) % 360.0,
        });
    }
    Ok(sheets)
}

// ---------- DXF ----------

fn dxf_num(v: f64) -> String {
    let s = format!("{v:.4}");
    if s == "-0.0000" { "0.0000".into() } else { s }
}

/// DXF text: ASCII only (R12 has no Unicode).
fn dxf_text(s: &str) -> String {
    s.chars().map(|c| if c.is_ascii() && !c.is_ascii_control() { c } else { '_' }).collect()
}

/// Sheet `k` of the layout as an R12 DXF, inches (module doc).
pub fn to_dxf(l: &Layout, k: usize) -> Result<String> {
    let sheet = l.sheets.get(k).with_context(|| format!("no sheet {}", k + 1))?;
    let mut o = String::new();
    let mut g = |code: i32, v: &str| {
        let _ = writeln!(o, "{code}\n{v}");
    };
    g(0, "SECTION");
    g(2, "HEADER");
    g(9, "$ACADVER");
    g(1, "AC1009");
    g(9, "$INSUNITS");
    g(70, "1");
    g(9, "$EXTMIN");
    g(10, "0.0");
    g(20, "0.0");
    g(9, "$EXTMAX");
    g(10, &dxf_num(l.spec.sheet_length_in));
    g(20, &dxf_num(l.spec.sheet_width_in));
    g(0, "ENDSEC");
    g(0, "SECTION");
    g(2, "TABLES");
    g(0, "TABLE");
    g(2, "LTYPE");
    g(70, "1");
    g(0, "LTYPE");
    g(2, "CONTINUOUS");
    g(70, "0");
    g(3, "Solid line");
    g(72, "65");
    g(73, "0");
    g(40, "0.0");
    g(0, "ENDTAB");
    g(0, "TABLE");
    g(2, "LAYER");
    g(70, "3");
    for (name, color) in [("CUT", "7"), ("LABEL", "1"), ("SHEET", "8")] {
        g(0, "LAYER");
        g(2, name);
        g(70, "0");
        g(62, color);
        g(6, "CONTINUOUS");
    }
    g(0, "ENDTAB");
    g(0, "ENDSEC");
    g(0, "SECTION");
    g(2, "ENTITIES");
    let poly = |g: &mut dyn FnMut(i32, &str), layer: &str, pts: &[Pt]| {
        g(0, "POLYLINE");
        g(8, layer);
        g(66, "1");
        g(10, "0.0");
        g(20, "0.0");
        g(30, "0.0");
        g(70, "1");
        for p in pts {
            g(0, "VERTEX");
            g(8, layer);
            g(10, &dxf_num(p[0]));
            g(20, &dxf_num(p[1]));
            g(30, "0.0");
        }
        g(0, "SEQEND");
        g(8, layer);
    };
    let (sl, sw) = (l.spec.sheet_length_in, l.spec.sheet_width_in);
    poly(&mut g, "SHEET", &[[0.0, 0.0], [sl, 0.0], [sl, sw], [0.0, sw]]);
    for part in &sheet.parts {
        poly(&mut g, "CUT", &part.outline);
    }
    let text = |g: &mut dyn FnMut(i32, &str), at: Pt, h: f64, deg: f64, s: &str| {
        g(0, "TEXT");
        g(8, "LABEL");
        g(10, &dxf_num(at[0]));
        g(20, &dxf_num(at[1]));
        g(30, "0.0");
        g(40, &dxf_num(h));
        g(1, &dxf_text(s));
        g(50, &dxf_num(deg));
        g(72, "1");
        g(73, "2");
        g(11, &dxf_num(at[0]));
        g(21, &dxf_num(at[1]));
        g(31, "0.0");
    };
    for part in &sheet.parts {
        let p = &l.pieces[part.piece];
        for [a, b] in &part.marks {
            g(0, "LINE");
            g(8, "LABEL");
            g(10, &dxf_num(a[0]));
            g(20, &dxf_num(a[1]));
            g(30, "0.0");
            g(11, &dxf_num(b[0]));
            g(21, &dxf_num(b[1]));
            g(31, "0.0");
        }
        text(&mut g, part.text_at, p.text_h, part.text_deg, &p.text);
    }
    let title = format!(
        "{} sheet {} of {}: {} hardboard {} in, {} x {} in",
        l.hull,
        k + 1,
        l.sheets.len(),
        match l.spec.orientation {
            Orientation::Flat => "flat layers,",
            Orientation::Vertical => "upright ribs,",
        },
        l.spec.board_in,
        sl,
        sw
    );
    text(&mut g, [0.5 * sl, -1.0], 0.5, 0.0, &title);
    g(0, "ENDSEC");
    g(0, "EOF");
    Ok(o)
}

/// File name for sheet k: `<hull>_sheet_01.dxf`.
pub fn dxf_name(l: &Layout, k: usize) -> String {
    let stem: String = l
        .hull
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect();
    format!("{stem}_sheet_{:02}.dxf", k + 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(w: f64, h: f64) -> Vec<Pt> {
        vec![[0.0, 0.0], [w, 0.0], [w, h], [0.0, h]]
    }

    #[test]
    fn half_at_takes_outermost_crossing_and_none_below_keel() {
        let half = [(5.0, 10.0), (5.0, 2.0), (0.0, 1.0)];
        assert_eq!(half_at(&half, 6.0), Some(5.0));
        assert!((half_at(&half, 1.5).unwrap() - 2.5).abs() < 1e-12);
        assert_eq!(half_at(&half, 0.5), None);
    }

    #[test]
    fn rasters_of_touching_rects_overlap_when_closer_than_gap() {
        let spec = MoldSpec { gap_in: 0.25, ..MoldSpec::default() };
        let r = raster_pad(&spec);
        let a = raster(&rect(10.0, 4.0), r);
        // Every point of the rectangle lies in an occupied cell.
        let covered = |x: f64, y: f64| {
            let (i, j) = (((x - a.origin[0]) / CELL) as usize, ((y - a.origin[1]) / CELL) as usize);
            a.rows[j].iter().any(|&(s, e)| i >= s && i <= e)
        };
        for (x, y) in [(0.0, 0.0), (10.0, 4.0), (5.0, 2.0), (0.0, 4.0), (10.0, 0.0)] {
            assert!(covered(x, y), "({x}, {y})");
        }
    }

    #[test]
    fn nest_keeps_gap_and_stays_on_sheet() {
        let spec =
            MoldSpec { sheet_length_in: 40.0, sheet_width_in: 20.0, gap_in: 0.25, ..MoldSpec::default() };
        let pieces: Vec<Piece> = (0..10)
            .map(|k| Piece {
                label: format!("P{k}"),
                layer: 0,
                outline: rect(12.0, 5.0),
                marks: vec![],
                text: String::new(),
                text_at: [6.0, 2.5],
                text_deg: 0.0,
                text_h: 0.5,
                area_in2: 60.0,
            })
            .collect();
        let sheets = nest(&pieces, &spec).unwrap();
        // 3 x 3 fit a 40 x 20 in sheet (36.5 x 15.5 in with the gaps); the tenth needs a second.
        assert_eq!(sheets.len(), 2);
        assert_eq!(sheets[0].parts.len(), 9);
        let all: Vec<(usize, &Placed)> =
            sheets.iter().enumerate().flat_map(|(s, sh)| sh.parts.iter().map(move |p| (s, p))).collect();
        assert_eq!(all.len(), 10);
        for (_, p) in &all {
            let (lo, hi) = bbox(&p.outline);
            assert!(lo[0] >= 0.25 - 1e-9 && lo[1] >= 0.25 - 1e-9, "{lo:?}");
            assert!(hi[0] <= 40.0 - 0.25 + 1e-9 && hi[1] <= 20.0 - 0.25 + 1e-9, "{hi:?}");
        }
        for (i, (sa, a)) in all.iter().enumerate() {
            for (sb, b) in &all[i + 1..] {
                if sa != sb {
                    continue;
                }
                let (la, ha) = bbox(&a.outline);
                let (lb, hb) = bbox(&b.outline);
                let dx = (lb[0] - ha[0]).max(la[0] - hb[0]);
                let dy = (lb[1] - ha[1]).max(la[1] - hb[1]);
                assert!(dx.max(dy) >= 0.25 - 1e-9, "pieces {} and {} closer than the gap", a.piece, b.piece);
            }
        }
    }

    #[test]
    fn too_big_piece_errs() {
        let spec = MoldSpec { sheet_length_in: 20.0, sheet_width_in: 10.0, ..MoldSpec::default() };
        let p = Piece {
            label: "big".into(),
            layer: 0,
            outline: rect(30.0, 30.0),
            marks: vec![],
            text: String::new(),
            text_at: [0.0, 0.0],
            text_deg: 0.0,
            text_h: 0.5,
            area_in2: 900.0,
        };
        assert!(nest(&[p], &spec).unwrap_err().to_string().contains("does not fit"));
    }

    #[test]
    fn split_run_makes_pieces_that_fit() {
        // A 200 in lens, 24 in wide, onto 96 x 48 in sheets: 3 pieces of 66.7 in.
        let run: Vec<Pt> = (0..=100)
            .map(|i| {
                let x = 2.0 * i as f64;
                [x, 12.0 * (1.0 - ((x - 100.0) / 100.0).powi(2))]
            })
            .collect();
        let ps = split_run(&run, "L1", 0, None, &MoldSpec::default()).unwrap();
        assert_eq!(ps.len(), 3);
        assert_eq!(ps[1].label, "L1-2");
        let total: f64 = ps.iter().map(|p| p.area_in2).sum();
        let exact = 2.0 * 12.0 * 200.0 * 2.0 / 3.0;
        assert!((total - exact).abs() / exact < 0.01, "{total} vs {exact}");
        for p in &ps {
            assert!(area(&p.outline) > 0.0, "counter-clockwise");
        }
    }
}
