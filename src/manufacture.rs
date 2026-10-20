//! Mold boards for the shop (web Manufacture tab, `jaguar mold`). Below `export` in the layout:
//! reads the winner's geometry, never feeds physics.
//!
//! The mold is a MALE mold: its surface is the canoe's inner wall ([`export::Mesh::inner_rows`]:
//! the outer hull offset inward by the wall, the solid concrete ends left out, no air pocket
//! bulkheads), built upside down with the gunwale on the shop floor from foam sheets
//! (hand-cut and sanded, never water-jet cut). Only hardboard is water-jet cut, for one of two
//! uses ([`Boards`]):
//!
//! * [`Boards::Templates`] (default): the mold is foam only, layer on layer. Each foam layer gets
//!   a hardboard tracing template: laid on a foam sheet, traced, the foam cut and sanded to
//!   shape. The template is everything the layer covers in plan (the envelope of its waterlines
//!   from underside to top face, every 1/4 in), its tips tapered to a point ([`taper_tips`]), so
//!   the foam is cut oversize, never short; the top face's
//!   outline is etched on it as the sanding line. A layer longer than a foam sheet is several
//!   foam pieces, and each template piece is one foam piece: its ends are the foam joints.
//! * [`Boards::Mold`]: a hardboard sheet between each pair of foam layers, cut to the exact
//!   outline at its mid-plane; the foam is sanded to meet the board edges.
//!
//! Foam layers mix the thicknesses on hand ([`foam_order`]); foam pieces fit one of the foam sheet
//! sizes, hardboard pieces one of the hardboard sizes ([`nest`]).
//!
//! [`Orientation::Flat`]: layers lie flat, layer k (k = 0 on the floor) starting at the sum of
//! the layers (and boards, in the mold) below it, outlines are waterlines of the inner surface,
//! symmetric about the centreline, until the layer no longer reaches the keel; a layer is cut
//! across into equal lengths. [`Orientation::Vertical`]: layers stand upright across the canoe
//! like ribs, from the bow end xa of the hollow run, each a cross-section closed by the gunwale
//! line on the floor. [`Orientation::Hybrid`]: flat layers up to a switch height, then upright
//! keel slabs on them ([`keel_slabs`]); the switch defaults to the least sanding ([`sand_in3`]).
//!
//! [`Packing`]: `Blocks` (default) cuts every flat layer at the same joints (the fewest equal
//! lengths for which every layer's piece in every block fits), so the joints line up from floor to
//! keel and the mold comes apart in whole blocks; `Continuous` cuts each layer into the fewest pieces that fit (foam sheet and
//! hardboard sheet), then packs them; `Tight` also tries shorter pieces and keeps the fewest
//! hardboard sheets; `FewestShapes` makes each template half a layer (centreline to side), traced
//! on both sides by flipping it, and reuses a template for the mirror-image piece at the other
//! end when the hull is the same fore and aft (within 0.02 in).
//!
//! With `nest_inside`, pieces may be cut out of the middle of bigger ones (a tracing template only
//! needs its outer edge, so it becomes a ring at least `ring_in` wide), and layers narrow enough
//! to fit inside a wide layer's ring are also tried cut shorter (not with `Blocks`); the DXF cuts
//! the inner pieces first.
//!
//! Pieces are packed onto sheets ([`nest`]): every piece is rasterised to 1/4 in cells grown by
//! half the gap (so two pieces never come closer than `gap_in`, nor closer to a sheet edge), and
//! placed largest first at the lowest free spot along the sheet length (then width), on the
//! first sheet with room, turned in quarter turns or to any multiple of 5 deg (both packed,
//! the fewer sheets kept). The DXF ([`to_dxf`]) is R12, inches, one file per sheet: layer CUT
//! the piece outlines (closed polylines) and alignment holes, in cutting order; with `dxf_marks`
//! also layer LABEL (piece names, centreline, 12 in station ticks, sanding line) and SHEET (the
//! sheet edge). Off by default: OMAX Layout and the like cut every imported line.
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
/// Pieces are packed turned to any multiple of this angle, deg.
const FINE_STEP_DEG: u32 = 5;
/// Station ticks on the LABEL layer every this many inches from the bow.
const TICK_IN: f64 = 12.0;
/// A template's tip is bridged over inward dips within this distance of its point, in.
const TIP_ZONE_IN: f64 = 12.0;
/// Two template pieces closer than this everywhere are one shape (FewestShapes), in.
const SAME_SHAPE_IN: f64 = 0.02;

pub type Pt = [f64; 2];

/// Layers, their pieces and the sheets they are packed on.
type Built = (Vec<Layer>, Vec<Piece>, Vec<Sheet>);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Orientation {
    /// Layers laid flat (waterline outlines).
    Flat,
    /// Layers standing upright across the canoe (cross-section outlines).
    Vertical,
    /// Flat layers up to a switch height, then upright keel slabs across the canoe standing on
    /// them: flat layers follow the steep sides, upright slabs the flat bottom and keel.
    Hybrid,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Boards {
    /// Hardboard tracing templates for an all-foam mold.
    Templates,
    /// Hardboard sheets between the foam layers, part of the mold.
    Mold,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Packing {
    /// Every layer cut at the same places along the canoe: the joints line up from floor to
    /// keel, so the mold comes apart in whole blocks for demolding.
    Blocks,
    /// Fewest pieces per layer, then the fewest sheets.
    Continuous,
    /// Fewest hardboard sheets (pieces may be shorter).
    Tight,
    /// Half-layer templates, flipped for the other side, shared by mirror-image pieces.
    FewestShapes,
}

/// What to build the mold from. Inches.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct MoldSpec {
    pub boards: Boards,
    pub orientation: Orientation,
    pub packing: Packing,
    /// Foam sheet thicknesses on hand (one number or a list): the layers mix them so the stack
    /// covers the mold with the least left over ([`foam_order`]).
    #[serde(deserialize_with = "one_or_many")]
    pub foam_in: Vec<f64>,
    /// Foam sheet sizes on hand, [length, width]: every foam piece fits one of them.
    pub foam_sheets: Vec<[f64; 2]>,
    /// Hardboard thickness (between the layers with [`Boards::Mold`]).
    pub board_in: f64,
    /// Hardboard sheet sizes on hand, [length, width] ([`nest`]).
    pub sheets: Vec<[f64; 2]>,
    /// Least space between two pieces, and between a piece and the sheet edge.
    pub gap_in: f64,
    /// Pieces may be cut out of the middle of bigger ones: a tracing template only needs its
    /// outer edge, so it can be a ring at least `ring_in` wide around the pieces inside it.
    pub nest_inside: bool,
    pub ring_in: f64,
    /// Alignment holes `hole_in` across in every template at the 12 in stations (on the
    /// centreline; half templates 1 in off it; upright slabs on the centreline near the floor):
    /// the same places in every layer, to mark the foam or dowel the stack.
    pub holes: bool,
    pub hole_in: f64,
    /// Water-jet cut width to allow for in the DXF (outlines moved out, holes in, by half of it).
    /// 0: none (most water-jet software compensates itself).
    pub kerf_in: f64,
    /// Also write the reference lines in the DXF (layer LABEL: names, centreline, ticks, sanding
    /// line; layer SHEET: the sheet edge). Off: cut lines only, since water-jet software such as
    /// OMAX Layout imports every line and would cut these unless each is set not to.
    pub dxf_marks: bool,
    /// Double-symmetric canoe (the same fore and aft): cut templates for the bow half only; each
    /// also traces the matching stern piece, turned end for end. Flat tracing templates only.
    pub half_canoe: bool,
    /// Hybrid: height above the floor where the flat layers stop (rounded to a layer top);
    /// None = the one leaving the least foam to sand ([`sand_in3`]).
    pub switch_in: Option<f64>,
    /// Hybrid: keel slab thickness; None = the thickest foam on hand.
    pub keel_foam_in: Option<f64>,
    /// Shop rates for the labor estimate ([`Labor`]).
    pub labor: LaborRates,
}

/// How fast the shop works, for the labor estimate. Guesses until the team times a test piece.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct LaborRates {
    /// Tracing along a template, in/min, and placing the template for each trace, min.
    pub trace_in_per_min: f64,
    pub place_min: f64,
    /// Hand-cutting foam: cut face per minute (length x foam thickness), in^2/min.
    pub cut_in2_per_min: f64,
    /// A curve of radius R cuts as slowly as (1 + curve_radius_in / R) times its length straight.
    pub curve_radius_in: f64,
    /// A corner (a turn of over 30 deg at one point): stop and turn the cut, min.
    pub corner_min: f64,
    /// Sanding: foam removed, in^3/min, and the final pass over the whole surface, ft^2/min.
    pub sand_in3_per_min: f64,
    pub finish_ft2_per_min: f64,
    /// Gluing and aligning one foam piece in the stack, min.
    pub assemble_min: f64,
}

impl Default for LaborRates {
    fn default() -> Self {
        LaborRates {
            trace_in_per_min: 60.0,
            place_min: 0.5,
            cut_in2_per_min: 40.0,
            curve_radius_in: 12.0,
            corner_min: 0.25,
            sand_in3_per_min: 10.0,
            finish_ft2_per_min: 0.5,
            assemble_min: 2.0,
        }
    }
}

/// Estimated hand labor for the mold, hours, and what it is counted from.
#[derive(Debug, Clone, Serialize)]
pub struct Labor {
    pub trace_hr: f64,
    pub cut_hr: f64,
    pub sand_hr: f64,
    pub assemble_hr: f64,
    pub total_hr: f64,
    /// Template tracings, and the outline traced in all, in.
    pub traces: usize,
    pub trace_in: f64,
    /// Foam cut in all (outline length), in; the extra length its curves count as, in; corners.
    pub cut_in: f64,
    pub curve_in: f64,
    pub corners: usize,
    /// Foam to sand off, in^3, and the mold surface given the final pass, ft^2.
    pub sand_in3: f64,
    pub surface_ft2: f64,
    /// Foam pieces glued into the stack.
    pub foam_pieces: usize,
}

impl Default for MoldSpec {
    fn default() -> Self {
        MoldSpec {
            boards: Boards::Templates,
            orientation: Orientation::Flat,
            packing: Packing::Blocks,
            foam_in: vec![2.0],
            foam_sheets: vec![[96.0, 48.0]],
            board_in: 0.125,
            sheets: vec![[96.0, 48.0]],
            gap_in: 0.25,
            nest_inside: false,
            ring_in: 1.0,
            holes: true,
            hole_in: 0.25,
            kerf_in: 0.0,
            dxf_marks: false,
            half_canoe: false,
            switch_in: None,
            keel_foam_in: None,
            labor: LaborRates::default(),
        }
    }
}

impl MoldSpec {
    fn validate(&self) -> Result<()> {
        ensure!(!self.foam_in.is_empty(), "give at least one foam thickness");
        ensure!(self.foam_in.len() <= 4, "at most 4 foam thicknesses, got {}", self.foam_in.len());
        ensure!(!self.foam_sheets.is_empty(), "give at least one foam sheet size");
        ensure!(!self.sheets.is_empty(), "give at least one hardboard sheet size");
        let pos = |v: f64, what: &str| -> Result<()> {
            ensure!(v.is_finite() && v > 0.0, "{what} must be > 0, got {v}");
            Ok(())
        };
        for &t in &self.foam_in {
            pos(t, "foam thickness")?;
        }
        pos(self.board_in, "hardboard thickness")?;
        for &[l, w] in &self.foam_sheets {
            pos(l.min(w), "foam sheet size")?;
        }
        ensure!(self.gap_in.is_finite() && self.gap_in >= 0.0, "gap must be >= 0, got {}", self.gap_in);
        pos(self.ring_in, "ring width")?;
        pos(self.hole_in, "hole size")?;
        ensure!(
            !self.half_canoe || self.templates(),
            "half canoe needs tracing templates: boards in the mold are each part of the mold"
        );
        ensure!(
            !self.half_canoe || self.orientation != Orientation::Vertical,
            "half canoe is for flat layers (upright slabs do not mirror fore and aft)"
        );
        ensure!(
            self.orientation != Orientation::Hybrid || self.templates(),
            "flat + upright keel needs tracing templates"
        );
        let r = &self.labor;
        for (v, what) in [
            (r.trace_in_per_min, "tracing speed"),
            (r.cut_in2_per_min, "cutting rate"),
            (r.sand_in3_per_min, "sanding rate"),
            (r.finish_ft2_per_min, "finish sanding rate"),
        ] {
            pos(v, what)?;
        }
        for (v, what) in [
            (r.place_min, "template placing time"),
            (r.curve_radius_in, "curve radius"),
            (r.corner_min, "corner time"),
            (r.assemble_min, "assembly time"),
        ] {
            ensure!(v.is_finite() && v >= 0.0, "{what} must be >= 0, got {v}");
        }
        for (v, what) in [(self.switch_in, "switch height"), (self.keel_foam_in, "keel slab thickness")] {
            if let Some(v) = v {
                pos(v, what)?;
            }
        }
        ensure!(
            self.kerf_in.is_finite()
                && self.kerf_in >= 0.0
                && (self.kerf_in == 0.0 || self.kerf_in < self.gap_in),
            "cut width must be >= 0 and less than the gap ({} in), got {}",
            self.gap_in,
            self.kerf_in
        );
        for &[l, w] in &self.sheets {
            pos(l.min(w), "hardboard sheet size")?;
            ensure!(
                2.0 * self.gap_in + 1.0 < l.min(w),
                "a {} in gap leaves no room on a {l} x {w} in sheet",
                self.gap_in
            );
        }
        Ok(())
    }

    /// The hardboard sizes, largest area first (a new sheet is the largest that holds the piece).
    fn sheets_by_size(&self) -> Vec<[f64; 2]> {
        let mut v = self.sheets.clone();
        v.sort_by(|a, b| (b[0] * b[1]).total_cmp(&(a[0] * a[1])));
        v
    }

    fn sizes(v: &[[f64; 2]]) -> String {
        v.iter().map(|[l, w]| format!("{l} x {w}")).collect::<Vec<_>>().join(", ")
    }

    fn templates(&self) -> bool {
        self.boards == Boards::Templates
    }
}

/// A number or a list of numbers.
fn one_or_many<'de, D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Vec<f64>, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum OneOrMany {
        One(f64),
        Many(Vec<f64>),
    }
    Ok(match OneOrMany::deserialize(d)? {
        OneOrMany::One(v) => vec![v],
        OneOrMany::Many(v) => v,
    })
}

/// Foam layer thicknesses, in stack order, to cover `total` (flat: floor to the inner keel;
/// upright: bow to stern) from the thicknesses on hand (`board` added to each with boards in
/// the mold): the count of each that leaves the least over the top, then the fewest layers.
/// Flat stacks go thickest on the floor, thinnest at the keel where the hull curves most;
/// upright stacks put the thin slabs at the two ends. Returns foam thicknesses.
fn foam_order(thick: &[f64], board: f64, total: f64, flat: bool) -> Vec<f64> {
    let mut t: Vec<f64> = thick.to_vec();
    t.sort_by(|a, b| b.total_cmp(a));
    t.dedup();
    let pitch: Vec<f64> = t.iter().map(|v| v + board).collect();
    let max_n = (total / pitch.iter().cloned().fold(f64::INFINITY, f64::min)).ceil() as usize + 1;
    // (overshoot, layers, counts)
    let mut best: Option<(f64, usize, Vec<usize>)> = None;
    #[allow(clippy::too_many_arguments)]
    fn walk(
        i: usize,
        sum: f64,
        n: usize,
        counts: &mut Vec<usize>,
        pitch: &[f64],
        total: f64,
        max_n: usize,
        best: &mut Option<(f64, usize, Vec<usize>)>,
    ) {
        if i == pitch.len() {
            if sum >= total - 1e-9 {
                let over = sum - total;
                let better =
                    best.as_ref().is_none_or(|b| over < b.0 - 1e-9 || (over < b.0 + 1e-9 && n < b.1));
                if better {
                    *best = Some((over, n, counts.clone()));
                }
            }
            return;
        }
        let mut c = 0;
        loop {
            counts.push(c);
            walk(i + 1, sum + c as f64 * pitch[i], n + c, counts, pitch, total, max_n, best);
            counts.pop();
            c += 1;
            if n + c > max_n || sum + c as f64 * pitch[i] > total + pitch[i] + 1e-9 {
                break;
            }
        }
    }
    walk(0, 0.0, 0, &mut vec![], &pitch, total, max_n, &mut best);
    let counts = best.map(|b| b.2).unwrap_or_else(|| vec![max_n]);
    let thick_first: Vec<f64> =
        t.iter().zip(&counts).flat_map(|(&v, &c)| std::iter::repeat_n(v, c)).collect();
    if flat {
        return thick_first;
    }
    // Upright: thinnest first, alternately to the bow end and the stern end.
    let (mut front, mut back) = (vec![], vec![]);
    for (k, v) in thick_first.iter().rev().enumerate() {
        if k % 2 == 0 { front.push(*v) } else { back.push(*v) }
    }
    front.extend(back.iter().rev());
    front
}

/// One hardboard piece, in its own frame: flat layers in hull plan coordinates (x from the bow,
/// y across, centreline y = 0); upright layers in section coordinates (y across, height above
/// the floor).
#[derive(Debug, Clone, Serialize)]
pub struct Piece {
    pub label: String,
    /// Index into [`Layout::layers`].
    pub layer: usize,
    /// Closed outline (last point not repeated), counter-clockwise.
    pub outline: Vec<Pt>,
    /// Marks to etch: centreline, station ticks, sanding line.
    pub marks: Vec<[Pt; 2]>,
    /// Alignment hole centres (cut, `hole_in` across).
    pub holes: Vec<Pt>,
    pub text: String,
    pub text_at: Pt,
    pub text_deg: f64,
    pub text_h: f64,
    pub area_in2: f64,
    /// The foam pieces this template traces ("L3-1 S", "L3-1 P (flip)", ...); with
    /// [`Boards::Mold`], the board itself.
    pub traces: Vec<String>,
}

/// One layer of the stack.
#[derive(Debug, Clone, Serialize)]
pub struct Layer {
    pub label: String,
    /// Flat: height of the layer's underside above the floor. Upright: x of its bow face.
    pub pos_in: f64,
    /// Templates: the layer's foam thickness; mold: the board's.
    pub thick_in: f64,
    /// Thickness of the foam layer it belongs to (templates) or that sits on it (mold).
    pub foam_in: f64,
    /// Stands upright across the canoe (pos_in is then the x of its bow face).
    pub upright: bool,
    /// Foam pieces (templates) or boards (mold) of the layer, bow to stern, in the layer frame.
    pub foam: Vec<Vec<Pt>>,
    /// Names of those pieces (`L3-2`, `R7`).
    pub foam_labels: Vec<String>,
    /// Hardboard pieces the layer uses (a FewestShapes template may serve several).
    pub pieces: Vec<usize>,
}

/// A piece on a sheet: its outline, marks and text moved there (rotation about the origin, then
/// the shift).
#[derive(Debug, Clone, Serialize)]
pub struct Placed {
    pub piece: usize,
    /// Nested: the part (index in this sheet) it is cut out of.
    pub inside: Option<usize>,
    pub rot_deg: u32,
    pub shift: Pt,
    pub outline: Vec<Pt>,
    pub marks: Vec<[Pt; 2]>,
    pub holes: Vec<Pt>,
    pub text_at: Pt,
    pub text_deg: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Sheet {
    /// This sheet's size (one of [`MoldSpec::sheets`]).
    pub length_in: f64,
    pub width_in: f64,
    pub parts: Vec<Placed>,
    /// Area of the pieces on it, in^2.
    pub used_in2: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stack {
    /// Foam layers with an outline (templates), or hardboard layers (mold).
    pub layers: usize,
    /// Foam layers in all (the mold: between and above the boards).
    pub foam_layers: usize,
    /// Foam pieces to cut (templates); boards (mold).
    pub foam_pieces: usize,
    /// Blocks packing: x of the joints between blocks (the same in every layer), bow to stern.
    pub joints_in: Vec<f64>,
    /// Foam thickness of each foam layer in stack order (floor up, or bow to stern), and how
    /// far the stack runs past the mold.
    pub foam_order_in: Vec<f64>,
    pub over_in: f64,
    /// Hybrid: height above the floor where the flat layers end and the keel slabs stand.
    pub switch_in: Option<f64>,
    /// Foam left to sand off after cutting, estimated ([`sand_in3`]), ft^3, and the deepest step
    /// the cut foam leaves (how far it strays from the hull before sanding), in.
    pub sand_ft3: f64,
    pub max_step_in: f64,
    /// Flat: floor to the top of the inner keel. Upright: bow to stern of the hollow run.
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
    /// Estimated hand labor.
    pub labor: Labor,
    /// The other ways to lay the foam with the same stock, packing and rates ([`alternatives`]).
    pub alternatives: Vec<Alternative>,
}

/// Another way to lay the foam, for comparison: its labor and accuracy, not packed on sheets.
#[derive(Debug, Clone, Serialize)]
pub struct Alternative {
    pub name: String,
    pub orientation: Orientation,
    pub keel_foam_in: Option<f64>,
    /// The layout this comparison belongs to.
    pub current: bool,
    /// Why it cannot be built with this stock; None when `result` is there.
    pub error: Option<String>,
    pub result: Option<AltResult>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AltResult {
    pub labor: Labor,
    pub sand_ft3: f64,
    pub max_step_in: f64,
    pub switch_in: Option<f64>,
    pub foam_pieces: usize,
    /// Hardboard templates (boards) and their area, ft^2.
    pub templates: usize,
    pub hardboard_ft2: f64,
}

/// Mold boards for a `jaguar-result/1` winner. Err if it has no winner, or a piece fits no sheet.
pub fn from_result(text: &str, spec: &MoldSpec) -> Result<Layout> {
    let (hull, mut es) = export::from_result(text, STATIONS, GIRTH)?;
    es.pocket_in = None;
    layout(&hull, &es, spec)
}

/// A layer before it is cut into pieces: flat, its waterline runs (starboard chains) and the
/// sanding line; upright, its half section (y, height) and the sanding half section.
enum Shape {
    Flat { runs: Vec<Vec<Pt>>, guide: Vec<Vec<Pt>> },
    Upright { half: Vec<Pt>, guide: Option<Vec<Pt>>, x: f64, grid: Option<i64> },
}

/// Mold boards for `hull` with the wall and depth of `es` (`es.thickness_in` required), with the
/// other ways to lay the foam for comparison.
pub fn layout(hull: &Hull, es: &ExportSpec, spec: &MoldSpec) -> Result<Layout> {
    let mut l = plan(hull, es, spec, true)?;
    l.alternatives = alternatives(hull, es, spec);
    Ok(l)
}

/// All flat, all upright, and flat + upright keel with slabs of each foam thickness on hand, of
/// two of the thickest glued, and of the chosen thickness, each with this spec's stock, packing and rates, laid out but not packed on
/// sheets (the slow part). Hybrid only for tracing templates.
fn alternatives(hull: &Hull, es: &ExportSpec, spec: &MoldSpec) -> Vec<Alternative> {
    let thickest = spec.foam_in.iter().copied().fold(0.0, f64::max);
    let mut keels: Vec<f64> = spec.foam_in.clone();
    keels.push(2.0 * thickest);
    keels.extend(spec.keel_foam_in);
    keels.sort_by(f64::total_cmp);
    keels.dedup();
    let own_keel = spec.keel_foam_in.unwrap_or(thickest);
    let mut alts: Vec<(String, Orientation, Option<f64>)> = vec![
        ("All flat".into(), Orientation::Flat, None),
        ("All upright".into(), Orientation::Vertical, None),
    ];
    if spec.templates() {
        alts.extend(keels.iter().map(|&t| {
            let note = if spec.foam_in.contains(&t) {
                String::new()
            } else if (t - 2.0 * thickest).abs() < 1e-9 {
                format!(" (two {thickest} in sheets glued)")
            } else {
                " (not a stock thickness)".into()
            };
            (format!("Flat + {t} in keel slabs{note}"), Orientation::Hybrid, Some(t))
        }));
    }
    alts.into_par_iter()
        .map(|(name, orientation, keel)| {
            let current = orientation == spec.orientation
                && (orientation != Orientation::Hybrid || keel.is_some_and(|t| (t - own_keel).abs() < 1e-9));
            // Half canoe is for flat layers; upright alone drops it.
            let half_canoe = spec.half_canoe && orientation != Orientation::Vertical;
            let s = MoldSpec { orientation, keel_foam_in: keel, half_canoe, ..spec.clone() };
            let (error, result) = match plan(hull, es, &s, false) {
                Ok(l) => (
                    None,
                    Some(AltResult {
                        sand_ft3: l.stack.sand_ft3,
                        max_step_in: l.stack.max_step_in,
                        switch_in: l.stack.switch_in,
                        foam_pieces: l.stack.foam_pieces,
                        templates: l.pieces.len(),
                        hardboard_ft2: l.pieces.iter().map(|p| p.area_in2).sum::<f64>() / 144.0,
                        labor: l.labor,
                    }),
                ),
                Err(e) => (Some(format!("{e:#}")), None),
            };
            Alternative { name, orientation, keel_foam_in: keel, current, error, result }
        })
        .collect()
}

/// The layout itself; `nest` false leaves the pieces off the sheets (and Tight's split search).
fn plan(hull: &Hull, es: &ExportSpec, spec: &MoldSpec, nest_sheets: bool) -> Result<Layout> {
    spec.validate()?;
    let wall = es.thickness_in.context("the mold needs the wall thickness (the inner surface)")?;
    let es = ExportSpec { pocket_in: None, filled: false, ..es.clone() };
    let mesh = export::build_mesh(hull, &es)?;
    let rows = mesh.inner_rows.context("this hull has no single hollow inner surface to build a mold on")?;
    let z_top = rows[0].1[0].1;
    let (xa, xb) = (rows[0].0, rows[rows.len() - 1].0);
    let keel_min = rows.iter().map(|r| r.1.last().unwrap().1).fold(f64::INFINITY, f64::min);
    let beam = 2.0 * rows.iter().map(|r| r.1[0].0).fold(0.0, f64::max);
    let tpl = spec.templates();
    let hybrid = spec.orientation == Orientation::Hybrid;
    // Flat layers (all of them, or below the switch with Hybrid).
    let flat = spec.orientation != Orientation::Vertical;
    let total = if flat { z_top - keel_min } else { xb - xa };
    let board = if tpl { 0.0 } else { spec.board_in };
    let facets = facets(&rows, z_top);
    let keel_t = spec.keel_foam_in.unwrap_or_else(|| spec.foam_in.iter().copied().fold(0.0, f64::max));
    let hybrid_sand = |order: &[f64], switch: f64| {
        sand_in3(&facets, |f| if f.h > switch { keel_t * f.nx } else { layer_at(order, f.h) * f.nz })
    };
    // Hybrid: the flat layers stop at a layer top, the given switch or the one leaving the least
    // foam to sand (the whole height counts as all flat).
    let (order, switch) = if hybrid {
        match spec.switch_in {
            Some(h) => {
                let o = foam_order(&spec.foam_in, 0.0, h.min(total), true);
                let top = o.iter().sum::<f64>();
                (o, top)
            }
            None => {
                let full = foam_order(&spec.foam_in, 0.0, total, true);
                let k = (0..=full.len())
                    .min_by(|&a, &b| {
                        let (sa, sb) = (full[..a].iter().sum::<f64>(), full[..b].iter().sum::<f64>());
                        hybrid_sand(&full, sa).total_cmp(&hybrid_sand(&full, sb)).then(b.cmp(&a))
                    })
                    .unwrap();
                (full[..k].to_vec(), full[..k].iter().sum::<f64>())
            }
        }
    } else {
        (foam_order(&spec.foam_in, board, total, flat), f64::INFINITY)
    };
    // Layer k starts at the sum of the layers (and boards) below / ahead of it.
    let start = |k: usize| order[..k.min(order.len())].iter().map(|t| t + board).sum::<f64>();
    let wl =
        |z: f64| -> Vec<Vec<Pt>> { waterline(&rows, z).iter().map(|r| smooth_run(r, &hull.x)).collect() };
    // Each layer: (label, pos, shape).
    let mut shapes: Vec<(String, f64, f64, Shape)> = vec![];
    if flat {
        while shapes.len() < order.len() {
            let (pos, ft) = (start(shapes.len()), order[shapes.len()]);
            let label = format!("L{}", shapes.len() + 1);
            let shape = if tpl {
                // Underside (nearer the gunwale) and top face; the template is their envelope.
                let (z0, z1) = (z_top - pos - 1e-9, z_top - pos - ft);
                if z0 <= keel_min + 1e-6 {
                    break;
                }
                // In plan the layer covers every cut of the hull between its two faces, not just
                // the faces: near a tip the widest-of-two-faces outline has a notch where the
                // longer, narrow underside meets the top face's round nose. Slices every 1/4 in
                // fill it.
                let n = ((ft / 0.25).ceil() as usize).max(2);
                let slices: Vec<Vec<Pt>> = (0..=n)
                    .map(|j| z0 - (z0 - z1) * j as f64 / n as f64)
                    .filter(|&z| z > keel_min + 1e-6)
                    .flat_map(&wl)
                    .collect();
                let w1 = if z1 > keel_min + 1e-6 { wl(z1) } else { vec![] };
                let runs = envelope(&slices, &[]).iter().map(|r| taper_tips(r)).collect();
                Shape::Flat { runs, guide: w1 }
            } else {
                let z = z_top - pos - 0.5 * spec.board_in;
                if z <= keel_min + 1e-6 {
                    break;
                }
                Shape::Flat { runs: wl(z), guide: vec![] }
            };
            shapes.push((label, pos, ft, shape));
        }
        if hybrid && switch < total - 1e-6 {
            shapes.extend(keel_slabs(&rows, z_top, switch, keel_t));
        }
    } else {
        while shapes.len() < order.len() {
            let (pos, ft) = (xa + start(shapes.len()), order[shapes.len()]);
            let label = format!("R{}", shapes.len() + 1);
            let shape = if tpl {
                if pos >= xb - 1e-6 {
                    break;
                }
                // Both faces of the slab; the template is the bigger section, the other etched.
                let (s0, s1) = (section(&rows, pos, z_top), section(&rows, (pos + ft).min(xb), z_top));
                let (big, small) = if half_area(&s0) >= half_area(&s1) { (s0, s1) } else { (s1, s0) };
                Shape::Upright { half: big, guide: Some(small), x: pos, grid: None }
            } else {
                if pos + spec.board_in > xb + 1e-9 {
                    break;
                }
                let x = pos + 0.5 * spec.board_in;
                Shape::Upright { half: section(&rows, x, z_top), guide: None, x, grid: None }
            };
            shapes.push((label, pos, ft, shape));
        }
    }
    ensure!(!shapes.is_empty(), "the mold is thinner than one layer");
    // Blocks: one set of joints for every flat layer.
    let edges: Option<Vec<f64>> = if spec.packing == Packing::Blocks && flat {
        let runs: Vec<&Vec<Pt>> = shapes
            .iter()
            .flat_map(|(_, _, _, s)| match s {
                Shape::Flat { runs, .. } => runs.iter().collect::<Vec<_>>(),
                Shape::Upright { .. } => vec![],
            })
            .collect();
        Some(block_edges(&runs, spec)?)
    } else {
        None
    };
    // Nested (not Blocks, whose joints are shared): layers narrow enough to sit inside a wide
    // layer's ring may be cut shorter so their pieces fit in the rings; wide layers keep their
    // fewest pieces (Tight shortens every layer).
    let width = |s: &Shape| match s {
        Shape::Flat { runs, .. } => 2.0 * runs.iter().flatten().map(|p| p[1]).fold(0.0, f64::max),
        Shape::Upright { .. } => 0.0,
    };
    let widest = shapes.iter().map(|(_, _, _, s)| width(s)).fold(0.0, f64::max);
    let nests = spec.nest_inside && spec.packing != Packing::Blocks;
    let shortened = |s: &Shape| {
        spec.packing == Packing::Tight || (nests && width(s) < widest - 2.0 * (spec.ring_in + spec.gap_in))
    };
    // Half canoe: pieces wholly aft of midships reuse the bow pieces' templates.
    let mid = spec.half_canoe.then_some(0.5 * (xa + xb));
    let build = |max_len: Option<f64>| -> Result<(Vec<Layer>, Vec<Piece>)> {
        let (mut ls, mut ps) = (vec![], vec![]);
        // Half canoe: keel slab grid index -> (its template, its section).
        let mut slab_tpl: std::collections::BTreeMap<i64, (usize, Vec<Pt>)> = Default::default();
        for (li, (label, pos, ft, shape)) in shapes.iter().enumerate() {
            let thick_in = if tpl { *ft } else { spec.board_in };
            let mut layer = Layer {
                label: label.clone(),
                pos_in: *pos,
                thick_in,
                foam_in: *ft,
                upright: matches!(shape, Shape::Upright { .. }),
                foam: vec![],
                foam_labels: vec![],
                pieces: vec![],
            };
            match shape {
                Shape::Flat { runs, guide } => {
                    let mut layer_cuts: Vec<Cut> = vec![];
                    for (ri, run) in runs.iter().enumerate() {
                        let tag = if runs.len() > 1 {
                            format!("{label}{}", (b'a' + ri as u8) as char)
                        } else {
                            label.clone()
                        };
                        let cuts = match &edges {
                            Some(e) => cuts_at(run, guide, &tag, e),
                            None => split_run(run, guide, &tag, max_len.filter(|_| shortened(shape)), spec)?,
                        };
                        layer.foam.extend(cuts.iter().map(|c| mirrored(&c.chain)));
                        layer.foam_labels.extend(cuts.iter().map(|c| c.label.clone()));
                        layer_cuts.extend(cuts);
                    }
                    flat_pieces(&layer_cuts, li, spec, mid, &mut layer.pieces, &mut ps)?;
                }
                Shape::Upright { half, guide, x, grid } => {
                    let full = close_section(half);
                    ensure!(
                        !tpl || fits_foam(&full, spec),
                        "{label} ({:.1} x {:.1} in) does not fit a foam sheet ({} in)",
                        2.0 * half[0][0],
                        half.last().map_or(0.0, |p| p[1]),
                        MoldSpec::sizes(&spec.foam_sheets)
                    );
                    layer.foam.push(full);
                    layer.foam_labels.push(label.clone());
                    // Half canoe: a keel slab aft of midships (grid k >= 0) is the mirror image of
                    // slab -k-1 at the bow and is traced with its template.
                    match grid.filter(|&g| mid.is_some() && g >= 0) {
                        Some(g) => {
                            let (pi, bow) = slab_tpl.get(&(-g - 1)).with_context(|| {
                                format!("half canoe: {label} has no matching keel slab at the bow (the canoe is not the same fore and aft); turn half canoe off")
                            })?;
                            ensure!(
                                bow.len() == half.len()
                                    && bow
                                        .iter()
                                        .zip(half)
                                        .all(|(a, b)| (a[0] - b[0]).abs().max((a[1] - b[1]).abs())
                                            <= SAME_SHAPE_IN),
                                "half canoe: {label} differs from its bow match by more than {SAME_SHAPE_IN} in (the canoe is not the same fore and aft); turn half canoe off"
                            );
                            let p = &mut ps[*pi];
                            if spec.packing == Packing::FewestShapes {
                                p.traces.extend([format!("{label} P"), format!("{label} S (flip)")]);
                            } else {
                                p.traces.push(format!("{label} (other face up)"));
                            }
                            p.text = format!("{} x{}", p.label, p.traces.len());
                            layer.pieces.push(*pi);
                        }
                        None => {
                            if let Some(g) = grid {
                                slab_tpl.insert(*g, (ps.len(), half.clone()));
                            }
                            rib_pieces(
                                half,
                                guide.as_deref(),
                                *x,
                                label,
                                li,
                                spec,
                                &mut layer.pieces,
                                &mut ps,
                            )?;
                        }
                    }
                }
            }
            ls.push(layer);
        }
        Ok((ls, ps))
    };
    let (layers, pieces, sheets) = if !nest_sheets {
        let (ls, ps) = build(None)?;
        (ls, ps, vec![])
    } else if (spec.packing == Packing::Tight || nests) && flat {
        // Fewest pieces that fit is not always fewest sheets (a 70 in piece on a 96 in sheet
        // wastes the rest of its length): try shorter pieces too, packed in quarter turns, then
        // the three best again turned to any angle; fewest sheets, then fewest pieces.
        let usable = spec.sheets.iter().map(|s| s[0].max(s[1])).fold(0.0, f64::max) - 2.0 * spec.gap_in;
        let limits: Vec<Option<f64>> =
            std::iter::once(None).chain((5..20).rev().map(|k| Some(usable * k as f64 / 20.0))).collect();
        let tries: Vec<Result<Built>> = limits
            .par_iter()
            .map(|&max_len| {
                let (ls, ps) = build(max_len)?;
                let sh = nest(&ps, spec, 90)?;
                Ok((ls, ps, sh))
            })
            .collect();
        let mut ok: Vec<(usize, Built)> = vec![];
        let mut first_err = None;
        for (i, t) in tries.into_iter().enumerate() {
            match t {
                Ok(t) => ok.push((i, t)),
                Err(e) => {
                    first_err.get_or_insert(e);
                }
            }
        }
        if ok.is_empty() {
            return Err(first_err.unwrap_or_else(|| anyhow::anyhow!("the mold is thinner than one layer")));
        }
        ok.sort_by_key(|(i, t)| (t.2.len(), t.1.len(), *i));
        ok.truncate(3);
        let fine: Vec<Result<Built>> = ok
            .into_par_iter()
            .map(|(_, (ls, ps, coarse))| {
                let sh = nest(&ps, spec, FINE_STEP_DEG)?;
                Ok((ls, ps, if sh.len() < coarse.len() { sh } else { coarse }))
            })
            .collect();
        let mut best: Option<Built> = None;
        for t in fine {
            let t = t?;
            if best.as_ref().is_none_or(|b| (t.2.len(), t.1.len()) < (b.2.len(), b.1.len())) {
                best = Some(t);
            }
        }
        best.expect("at least one split")
    } else {
        let (ls, ps) = build(None)?;
        let sh = nest(&ps, spec, FINE_STEP_DEG)?;
        (ls, ps, sh)
    };
    let foam_pieces = layers.iter().map(|l| l.foam.len()).sum();
    let joints: Vec<f64> = edges.as_ref().map_or(vec![], |e| e[1..e.len() - 1].to_vec());
    let n = layers.len();
    let keel_n = layers.iter().filter(|l| l.upright && hybrid).count();
    // With keel slabs the stack reaches the keel through them, not past it.
    let over = if keel_n > 0 { 0.0 } else { order.iter().map(|t| t + board).sum::<f64>() - total };
    let pitch_at = |o: &[f64], d: f64| layer_at(o, d) + board;
    let sw = if keel_n > 0 { switch } else { f64::INFINITY };
    let step = |f: &Facet| match spec.orientation {
        Orientation::Flat => pitch_at(&order, f.h) * f.nz,
        Orientation::Vertical => pitch_at(&order, f.x - xa) * f.nx,
        Orientation::Hybrid if f.h > sw => keel_t * f.nx,
        Orientation::Hybrid => layer_at(&order, f.h) * f.nz,
    };
    let sand_vol = sand_in3(&facets, step);
    let sand = sand_vol / 1728.0;
    let max_step = facets.iter().map(step).fold(0.0, f64::max);
    let surface_ft2 = facets.iter().map(|f| f.area).sum::<f64>() / 144.0;
    let stack = if flat {
        let mut foam_order_in = order.clone();
        foam_order_in.extend(std::iter::repeat_n(keel_t, keel_n));
        Stack {
            layers: n,
            foam_layers: foam_order_in.len(),
            foam_pieces,
            joints_in: joints.clone(),
            foam_order_in,
            over_in: over,
            switch_in: (keel_n > 0).then_some(switch),
            sand_ft3: sand,
            max_step_in: max_step,
            height_in: total,
            length_in: xb - xa,
            beam_in: beam,
            depth_in: es.depth_in,
            wall_in: wall,
        }
    } else {
        Stack {
            layers: n,
            foam_layers: order.len(),
            foam_pieces,
            joints_in: joints.clone(),
            foam_order_in: order.clone(),
            over_in: over,
            switch_in: None,
            sand_ft3: sand,
            max_step_in: max_step,
            height_in: xb - xa,
            length_in: xb - xa,
            beam_in: beam,
            depth_in: es.depth_in,
            wall_in: wall,
        }
    };
    let used: f64 = sheets.iter().map(|s| s.used_in2).sum();
    let sheet_area = sheets.iter().map(|s| s.length_in * s.width_in).sum::<f64>();
    let utilization = if sheet_area > 0.0 { used / sheet_area } else { 0.0 };
    let labor = labor(&layers, &pieces, sand_vol, surface_ft2, spec);
    Ok(Layout {
        format: MOLD_FORMAT.into(),
        hull: hull.name.clone(),
        spec: spec.clone(),
        stack,
        layers,
        pieces,
        sheets,
        utilization,
        labor,
        alternatives: vec![],
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

/// A template chain's tips (an end on the centreline) bridged over any inward dip within
/// TIP_ZONE_IN of the point, by the tightest outward curve over it (the upper hull of the chain
/// there): the outline tapers to its point without a neck or a spike. It only adds material, so
/// the foam is still never cut short.
fn taper_tips(chain: &[Pt]) -> Vec<Pt> {
    if chain.len() < 4 {
        return chain.to_vec();
    }
    let is_tip = |c: &[Pt]| c[0][1] <= 1e-9 && c[1][0] > c[0][0] + 1e-9;
    let mut out = chain.to_vec();
    if is_tip(&out) {
        out = taper_start(&out);
    }
    // The stern end: the same, on the chain mirrored end for end.
    let mut rev: Vec<Pt> = out.iter().rev().map(|p| [-p[0], p[1]]).collect();
    if is_tip(&rev) {
        rev = taper_start(&rev);
    }
    rev.iter().rev().map(|p| [-p[0], p[1]]).collect()
}

/// [`taper_tips`] at the start of a chain: the upper hull of its points from the tip to
/// TIP_ZONE_IN on (not past its widest point), then the rest of the chain.
fn taper_start(c: &[Pt]) -> Vec<Pt> {
    let imax = (0..c.len()).max_by(|&a, &b| c[a][1].total_cmp(&c[b][1]).then(b.cmp(&a))).unwrap();
    let j = (0..=imax).take_while(|&i| c[i][0] <= c[0][0] + TIP_ZONE_IN).last().unwrap_or(0);
    if j < 2 {
        return c.to_vec();
    }
    let mut h: Vec<Pt> = vec![];
    for &p in &c[..=j] {
        while h.len() >= 2 {
            let (o, a) = (h[h.len() - 2], h[h.len() - 1]);
            if (a[0] - o[0]) * (p[1] - o[1]) - (a[1] - o[1]) * (p[0] - o[0]) >= 0.0 {
                h.pop();
            } else {
                break;
            }
        }
        h.push(p);
    }
    h.extend_from_slice(&c[j + 1..]);
    h
}

/// The outline enclosing sets of waterline runs (the widest of any at every x): one
/// chain per stretch of x covered by any run, from the centreline back to it.
fn envelope(a: &[Vec<Pt>], b: &[Vec<Pt>]) -> Vec<Vec<Pt>> {
    let all: Vec<&Vec<Pt>> = a.iter().chain(b).collect();
    let mut spans: Vec<(f64, f64)> = all.iter().map(|r| (r[0][0], r[r.len() - 1][0])).collect();
    spans.sort_by(|p, q| p.0.total_cmp(&q.0));
    let mut merged: Vec<(f64, f64)> = vec![];
    for (s, e) in spans {
        match merged.last_mut() {
            Some(m) if s <= m.1 + 1e-9 => m.1 = m.1.max(e),
            _ => merged.push((s, e)),
        }
    }
    let y_at = |x: f64| {
        all.iter()
            .filter(|r| x >= r[0][0] - 1e-9 && x <= r[r.len() - 1][0] + 1e-9)
            .map(|r| chain_y(r, x))
            .fold(0.0, f64::max)
    };
    merged
        .into_iter()
        .map(|(s, e)| {
            let mut xs: Vec<f64> = all
                .iter()
                .flat_map(|r| r.iter().map(|p| p[0]))
                .filter(|&x| x >= s - 1e-9 && x <= e + 1e-9)
                .collect();
            xs.sort_by(f64::total_cmp);
            xs.dedup_by(|p, q| (*p - *q).abs() < 1e-9);
            let mut c: Vec<Pt> = vec![[s, 0.0]];
            c.extend(xs.iter().map(|&x| [x, y_at(x)]));
            c.push([e, 0.0]);
            c.dedup_by(|p, q| (p[0] - q[0]).abs() < 1e-9 && (p[1] - q[1]).abs() < 1e-9);
            c
        })
        .filter(|c| c.len() >= 3)
        .collect()
}

/// A waterline run through a smooth curve. The analysis loft blends offsets linearly between
/// table stations, which near the keel at the ends kinks the waterline at every station; the
/// board follows a centripetal Catmull-Rom spline (C1, no cusps or loops) through the run's
/// values at the table stations instead, sampled every 0.2 in or so. A tip on the centreline is
/// a knot (the loft rows between it and the first station are left out) and the curve tapers
/// smoothly into it, to a point; a square bulkhead end keeps its corner. Runs spanning fewer
/// than two table stations are kept as is.
fn smooth_run(run: &[Pt], table_x: &[f64]) -> Vec<Pt> {
    let n = run.len();
    if n < 4 {
        return run.to_vec();
    }
    // Data points: the run less its centreline end points (tips or bulkhead feet).
    let (a, b) = (usize::from(run[0][1] <= 1e-9), n - usize::from(run[n - 1][1] <= 1e-9));
    if b < a + 2 {
        return run.to_vec();
    }
    let data = &run[a..b];
    let (xa, xb) = (data[0][0], data[data.len() - 1][0]);
    let stations: Vec<Pt> =
        table_x.iter().filter(|&&x| x > xa + 1e-6 && x < xb - 1e-6).map(|&x| [x, chain_y(data, x)]).collect();
    if stations.len() < 2 {
        return run.to_vec();
    }
    let tip0 = a == 1 && run[0][0] < xa - 1e-9;
    let tip1 = b == n - 1 && run[n - 1][0] > xb + 1e-9;
    let mut k: Vec<Pt> = vec![];
    // At a tip the loft's rows between the tip and the first table station are blending
    // artifacts too: the curve runs from the station straight into the tip.
    k.push(if tip0 { run[0] } else { data[0] });
    k.extend(stations);
    k.push(if tip1 { run[n - 1] } else { data[data.len() - 1] });
    let m = k.len();
    let reflect = |p: Pt, q: Pt| [2.0 * p[0] - q[0], 2.0 * p[1] - q[1]];
    // Ends extend straight on (a tip then tapers to its point along the curve's own direction;
    // forcing it square to the centreline rounded the nose into a bulb with a neck behind it).
    let (first, last) = (reflect(k[0], k[1]), reflect(k[m - 1], k[m - 2]));
    let at = |j: isize| -> Pt {
        if j < 0 {
            first
        } else if j as usize >= m {
            last
        } else {
            k[j as usize]
        }
    };
    let mut out: Vec<Pt> = vec![];
    if !tip0 && a == 1 {
        out.push(run[0]);
    }
    out.push(k[0]);
    for j in 0..m - 1 {
        let ps = [at(j as isize - 1), k[j], k[j + 1], at(j as isize + 2)];
        let len = (k[j + 1][0] - k[j][0]).hypot(k[j + 1][1] - k[j][1]);
        let steps = ((len / 0.2).ceil() as usize).max(2);
        for s in 1..=steps {
            out.push(if s == steps { k[j + 1] } else { catmull_rom(&ps, s as f64 / steps as f64) });
        }
    }
    if !tip1 && b == n - 1 {
        out.push(run[n - 1]);
    }
    // Keep the chain a function of x (clipping and cutting rely on it) and on the starboard side.
    for i in 1..out.len() {
        out[i][0] = out[i][0].max(out[i - 1][0]);
        out[i][1] = out[i][1].max(0.0);
    }
    out
}

/// Centripetal (alpha = 1/2) Catmull-Rom between p[1] and p[2] at u in [0, 1] (Barry-Goldman).
fn catmull_rom(p: &[Pt; 4], u: f64) -> Pt {
    let knot = |a: Pt, b: Pt| (a[0] - b[0]).hypot(a[1] - b[1]).sqrt().max(1e-9);
    let t1 = knot(p[0], p[1]);
    let t2 = t1 + knot(p[1], p[2]);
    let t3 = t2 + knot(p[2], p[3]);
    let t = t1 + u * (t2 - t1);
    let lerp = |a: Pt, b: Pt, ta: f64, tb: f64| {
        let w = (t - ta) / (tb - ta);
        [a[0] + w * (b[0] - a[0]), a[1] + w * (b[1] - a[1])]
    };
    let a1 = lerp(p[0], p[1], 0.0, t1);
    let a2 = lerp(p[1], p[2], t1, t2);
    let a3 = lerp(p[2], p[3], t2, t3);
    let b1 = lerp(a1, a2, 0.0, t2);
    let b2 = lerp(a2, a3, t1, t3);
    lerp(b1, b2, t1, t2)
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

/// Does the polygon fit one of the foam sheets (quarter turns, no margin)?
fn fits_foam(poly: &[Pt], spec: &MoldSpec) -> bool {
    let (lo, hi) = bbox(poly);
    let (w, h) = (hi[0] - lo[0], hi[1] - lo[1]);
    spec.foam_sheets.iter().any(|&[l, b]| {
        let (l, b) = (l + 1e-9, b + 1e-9);
        (w <= l && h <= b) || (h <= l && w <= b)
    })
}

/// Does the polygon fit one hardboard sheet's usable raster, turned to some whole degree?
fn fits_board(poly: &[Pt], spec: &MoldSpec) -> bool {
    let r = raster_pad(spec);
    let cells = |v: f64| ((v + 2.0 * r) / CELL).ceil() as usize + 1;
    (0..180).any(|d| {
        let q: Vec<Pt> = poly.iter().map(|&p| rot(p, d)).collect();
        let (lo, hi) = bbox(&q);
        let (cw, ch) = (cells(hi[0] - lo[0]), cells(hi[1] - lo[1]));
        spec.sheets.iter().any(|&sz| {
            let (nx, ny) = sheet_cells(sz, spec.gap_in);
            cw <= nx && ch <= ny
        })
    })
}

/// One length of a flat layer's run: its starboard chain over [c0, c1] and the sanding line
/// over it.
struct Cut {
    label: String,
    chain: Vec<Pt>,
    c0: f64,
    c1: f64,
    guide: Vec<Vec<Pt>>,
}

/// The part of a chain over [c0, c1], ends interpolated (or the chain's own ends, square or tip,
/// when they fall inside).
fn clip(chain: &[Pt], c0: f64, c1: f64) -> Option<Vec<Pt>> {
    let (s, e) = (chain[0][0], chain[chain.len() - 1][0]);
    if e <= c0 + 1e-9 || s >= c1 - 1e-9 {
        return None;
    }
    let mut out: Vec<Pt> = vec![];
    if c0 <= s + 1e-12 {
        out.extend(chain.iter().take_while(|p| p[0] <= s + 1e-12));
    } else {
        out.push([c0, chain_y(chain, c0)]);
    }
    out.extend(chain.iter().filter(|p| p[0] > c0.max(s) + 1e-12 && p[0] < c1.min(e) - 1e-12));
    if c1 >= e - 1e-12 {
        out.extend(chain.iter().skip_while(|p| p[0] < e - 1e-12));
    } else {
        out.push([c1, chain_y(chain, c1)]);
    }
    out.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
    (out.len() >= 2).then_some(out)
}

/// Half-layer template: a starboard chain closed along the centreline, counter-clockwise.
fn half_poly(chain: &[Pt]) -> Vec<Pt> {
    let (c0, c1) = (chain[0][0], chain[chain.len() - 1][0]);
    let mut p: Vec<Pt> = vec![[c0, 0.0]];
    p.extend_from_slice(chain);
    p.push([c1, 0.0]);
    p.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
    if p.len() > 2 && (p[0][0] - p[p.len() - 1][0]).abs() < 1e-9 && (p[0][1] - p[p.len() - 1][1]).abs() < 1e-9
    {
        p.pop();
    }
    if area(&p) < 0.0 {
        p.reverse();
    }
    p
}

/// A flat layer's run cut into the fewest equal lengths whose foam pieces fit a foam sheet
/// (templates) and whose hardboard pieces fit a hardboard sheet (and no longer than `max_len`):
/// bow to stern, labelled `<tag>-1`, `<tag>-2`, ...
/// The sheets pieces must fit, for errors.
fn fit_what(spec: &MoldSpec) -> String {
    if spec.templates() {
        format!(
            "the foam sheets ({} in) and hardboard ({} in)",
            MoldSpec::sizes(&spec.foam_sheets),
            MoldSpec::sizes(&spec.sheets)
        )
    } else {
        format!("the hardboard ({} in)", MoldSpec::sizes(&spec.sheets))
    }
}

/// Do a cut's foam piece (templates) and hardboard piece fit their sheets?
fn piece_fits(chain: &[Pt], spec: &MoldSpec) -> bool {
    let full = mirrored(chain);
    (!spec.templates() || fits_foam(&full, spec))
        && fits_board(&if spec.packing == Packing::FewestShapes { half_poly(chain) } else { full }, spec)
}

/// Block joints (Blocks packing): the fewest equal lengths over all the layers' runs for which
/// every layer's piece in every block fits.
fn block_edges(runs: &[&Vec<Pt>], spec: &MoldSpec) -> Result<Vec<f64>> {
    let x0 = runs.iter().map(|r| r[0][0]).fold(f64::INFINITY, f64::min);
    let x1 = runs.iter().map(|r| r[r.len() - 1][0]).fold(f64::NEG_INFINITY, f64::max);
    let edges = |n: usize| -> Vec<f64> {
        (0..=n).map(|k| if k == n { x1 } else { x0 + (x1 - x0) * k as f64 / n as f64 }).collect()
    };
    (1..=200)
        .map(edges)
        .find(|e| {
            runs.iter()
                .all(|r| e.windows(2).all(|w| clip(r, w[0], w[1]).is_none_or(|c| piece_fits(&c, spec))))
        })
        .with_context(|| {
            format!(
                "the mold ({:.1} in long) does not split into blocks that fit {}",
                x1 - x0,
                fit_what(spec)
            )
        })
}

/// A run cut at the block joints: one cut per block it reaches, labelled `<tag>-<block>`.
fn cuts_at(run: &[Pt], guide: &[Vec<Pt>], tag: &str, edges: &[f64]) -> Vec<Cut> {
    edges
        .windows(2)
        .enumerate()
        .filter_map(|(k, w)| {
            let chain = clip(run, w[0], w[1])?;
            // A sliver under a hundredth of an inch is the run just touching the joint.
            (chain[chain.len() - 1][0] - chain[0][0] > 0.01).then(|| Cut {
                label: format!("{tag}-{}", k + 1),
                c0: w[0].max(run[0][0]),
                c1: w[1].min(run[run.len() - 1][0]),
                guide: guide.iter().filter_map(|g| clip(g, w[0], w[1])).collect(),
                chain,
            })
        })
        .collect()
}

fn split_run(
    run: &[Pt],
    guide: &[Vec<Pt>],
    tag: &str,
    max_len: Option<f64>,
    spec: &MoldSpec,
) -> Result<Vec<Cut>> {
    let (x0, x1) = (run[0][0], run[run.len() - 1][0]);
    let span = x1 - x0;
    let edge = |n: usize, k: usize| if k == n { x1 } else { x0 + span * k as f64 / n as f64 };
    let n_min = max_len.map_or(1, |m| ((span / m).ceil() as usize).max(1));
    let n = (n_min..=200)
        .find(|&n| {
            (0..n).all(|k| clip(run, edge(n, k), edge(n, k + 1)).is_some_and(|c| piece_fits(&c, spec)))
        })
        .with_context(|| {
            format!(
                "layer {tag} ({:.1} in wide) does not fit {} in any number of pieces",
                2.0 * run.iter().map(|p| p[1]).fold(0.0, f64::max),
                fit_what(spec)
            )
        })?;
    Ok((0..n)
        .map(|k| {
            let (c0, c1) = (edge(n, k), edge(n, k + 1));
            Cut {
                label: if n > 1 { format!("{tag}-{}", k + 1) } else { tag.to_string() },
                chain: clip(run, c0, c1).expect("checked above"),
                c0,
                c1,
                guide: guide.iter().filter_map(|g| clip(g, c0, c1)).collect(),
            }
        })
        .collect())
}

/// Is chain b chain a turned end for end (within SAME_SHAPE_IN)?
fn mirror_shape(a: &Cut, b: &Cut) -> bool {
    let (la, lb) = (a.c1 - a.c0, b.c1 - b.c0);
    if (la - lb).abs() > SAME_SHAPE_IN {
        return false;
    }
    (0..=100).all(|i| {
        let s = la * i as f64 / 100.0;
        (chain_y(&a.chain, a.c0 + s) - chain_y(&b.chain, b.c1 - s)).abs() <= SAME_SHAPE_IN
    })
}

/// The run's cuts as hardboard pieces: one full-width piece each, or (FewestShapes) a half
/// template each, shared with the mirror-image cut at the other end when there is one.
/// With `mid` (half canoe) every cut wholly aft of it must share the template of its mirror
/// image at the bow, or Err: the canoe is not the same fore and aft there.
fn flat_pieces(
    cuts: &[Cut],
    layer: usize,
    spec: &MoldSpec,
    mid: Option<f64>,
    used: &mut Vec<usize>,
    ps: &mut Vec<Piece>,
) -> Result<()> {
    let half = spec.packing == Packing::FewestShapes;
    let mut made: Vec<(usize, usize)> = vec![]; // (cut, piece)
    for (k, c) in cuts.iter().enumerate() {
        let aft = mid.is_some_and(|m| c.c0 >= m - 0.01);
        if half || aft {
            if let Some(&(_, pi)) = made.iter().find(|&&(j, _)| mirror_shape(&cuts[j], c)) {
                // Turned end for end: a half template traces this cut's port side as it is and
                // its starboard side flipped over; a full one traces it whole.
                if half {
                    ps[pi].traces.extend([format!("{} P", c.label), format!("{} S (flip)", c.label)]);
                } else {
                    ps[pi].traces.push(format!("{} (turned end for end)", c.label));
                }
                let n = ps[pi].traces.len();
                ps[pi].text = format!("{} <BOW x{n}", ps[pi].label);
                continue;
            }
            ensure!(
                !aft,
                "half canoe: {} has no matching piece at the bow (the canoe is not the same fore and aft \
                 within {SAME_SHAPE_IN} in); turn half canoe off",
                c.label
            );
        }
        let traces = if half {
            vec![format!("{} S", c.label), format!("{} P (flip)", c.label)]
        } else {
            vec![c.label.clone()]
        };
        made.push((k, ps.len()));
        used.push(ps.len());
        ps.push(flat_piece(c, layer, half, traces, spec));
    }
    Ok(())
}

fn flat_piece(c: &Cut, layer: usize, half: bool, traces: Vec<String>, spec: &MoldSpec) -> Piece {
    let outline = if half { half_poly(&c.chain) } else { mirrored(&c.chain) };
    let (c0, c1) = (c.c0, c.c1);
    let xm = 0.5 * (c0 + c1);
    let ym = chain_y(&c.chain, xm);
    let mut marks = vec![];
    if !half && c1 - c0 > 2.0 {
        marks.push([[c0 + 0.5, 0.0], [c1 - 0.5, 0.0]]);
    }
    let mut t = (c0 / TICK_IN).floor() * TICK_IN + TICK_IN;
    while t < c1 - 1e-9 {
        let hw = (0.5f64).min(0.4 * chain_y(&c.chain, t));
        if hw > 0.05 {
            marks.push([[t, if half { 0.0 } else { -hw }], [t, hw]]);
        }
        t += TICK_IN;
    }
    for g in &c.guide {
        for w in g.windows(2) {
            marks.push([w[0], w[1]]);
            if !half {
                marks.push([[w[0][0], -w[0][1]], [w[1][0], -w[1][1]]]);
            }
        }
    }
    let (text_h, text_at) = if half {
        let h = (0.25 * ym).clamp(0.2, 1.0);
        (h, [xm, (0.45 * ym).max(0.6 * h)])
    } else {
        let h = (0.3 * 2.0 * ym).clamp(0.2, 1.0);
        (h, [xm, 0.6 * h])
    };
    let n = traces.len();
    // Alignment holes at the stations: on the centreline, or 1 in off it on a half template.
    let mut stations = vec![];
    let mut t = (c0 / TICK_IN).floor() * TICK_IN + TICK_IN;
    while t < c1 - 1e-9 {
        stations.push([t, if half { 1.0 } else { 0.0 }]);
        t += TICK_IN;
    }
    let holes = hole_spots(&outline, &stations, spec);
    Piece {
        text: if n > 1 { format!("{} <BOW x{n}", c.label) } else { format!("{} <BOW", c.label) },
        label: c.label.clone(),
        layer,
        area_in2: area(&outline).abs(),
        holes,
        outline,
        marks,
        text_at,
        text_deg: 0.0,
        text_h,
        traces,
    }
}

/// The candidate hole centres that sit well inside the outline: at least the hole radius plus
/// 3/8 in from its edge. None without `spec.holes`.
fn hole_spots(outline: &[Pt], candidates: &[Pt], spec: &MoldSpec) -> Vec<Pt> {
    if !spec.holes {
        return vec![];
    }
    let clear = 0.5 * spec.hole_in + 0.375;
    candidates
        .iter()
        .copied()
        .filter(|&p| inside_poly(outline, p) && edge_dist(outline, p) >= clear)
        .collect()
}

/// Distance from p to the polygon's edge.
fn edge_dist(poly: &[Pt], p: Pt) -> f64 {
    let n = poly.len();
    (0..n)
        .map(|i| {
            let (a, b) = (poly[i], poly[(i + 1) % n]);
            let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
            let ll = dx * dx + dy * dy;
            let t =
                if ll > 0.0 { (((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ll).clamp(0.0, 1.0) } else { 0.0 };
            (p[0] - a[0] - t * dx).hypot(p[1] - a[1] - t * dy)
        })
        .fold(f64::INFINITY, f64::min)
}

/// A facet of the inner surface (both sides counted): area, |normal| along x and z, centre x and
/// height above the floor.
struct Facet {
    area: f64,
    nx: f64,
    nz: f64,
    x: f64,
    h: f64,
}

fn facets(rows: &[NetRow], z_top: f64) -> Vec<Facet> {
    let mut out = vec![];
    for w in rows.windows(2) {
        let (r0, r1) = (&w[0], &w[1]);
        for j in 0..r0.1.len().min(r1.1.len()).saturating_sub(1) {
            let p = |x: f64, q: (f64, f64)| [x, q.0, q.1];
            let (a, b, c, d) =
                (p(r0.0, r0.1[j]), p(r1.0, r1.1[j]), p(r0.0, r0.1[j + 1]), p(r1.0, r1.1[j + 1]));
            let sub = |u: [f64; 3], v: [f64; 3]| [u[0] - v[0], u[1] - v[1], u[2] - v[2]];
            let cross = |u: [f64; 3], v: [f64; 3]| {
                [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
            };
            let (n1, n2) = (cross(sub(b, a), sub(c, a)), cross(sub(d, c), sub(d, b)));
            let n = [n1[0] + n2[0], n1[1] + n2[1], n1[2] + n2[2]];
            let len = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
            if len < 1e-12 {
                continue;
            }
            out.push(Facet {
                area: len, // half the quad's doubled area, times the two sides
                nx: (n[0] / len).abs(),
                nz: (n[2] / len).abs(),
                x: 0.25 * (a[0] + b[0] + c[0] + d[0]),
                h: z_top - 0.25 * (a[2] + b[2] + c[2] + d[2]),
            });
        }
    }
    out
}

/// A closed outline's length, the turning along its smooth stretches (radians), and its corners
/// (turns of over 30 deg at one point).
fn outline_shape(o: &[Pt]) -> (f64, f64, usize) {
    let n = o.len();
    let (mut len, mut smooth, mut corners) = (0.0, 0.0, 0);
    for i in 0..n {
        let (a, b, c) = (o[(i + n - 1) % n], o[i], o[(i + 1) % n]);
        len += (c[0] - b[0]).hypot(c[1] - b[1]);
        let (u, v) = ([b[0] - a[0], b[1] - a[1]], [c[0] - b[0], c[1] - b[1]]);
        let (lu, lv) = (u[0].hypot(u[1]), v[0].hypot(v[1]));
        if lu < 1e-9 || lv < 1e-9 {
            continue;
        }
        let turn = ((u[0] * v[0] + u[1] * v[1]) / (lu * lv)).clamp(-1.0, 1.0).acos();
        if turn > 30f64.to_radians() {
            corners += 1;
        } else {
            smooth += turn;
        }
    }
    (len, smooth, corners)
}

/// The hand labor estimate ([`LaborRates`]): tracing every template trace (its outline, plus
/// placing it), cutting every foam piece (outline x thickness, curves counted longer by
/// curve_radius_in per radian turned, corners), sanding (the volume, plus a final pass over the
/// surface) and gluing each foam piece in. Boards in the mold are cut to shape, not traced.
fn labor(layers: &[Layer], pieces: &[Piece], sand_in3: f64, surface_ft2: f64, spec: &MoldSpec) -> Labor {
    let r = &spec.labor;
    let (mut traces, mut trace_in) = (0, 0.0);
    if spec.templates() {
        for p in pieces {
            let per = outline_shape(&p.outline).0;
            traces += p.traces.len();
            trace_in += per * p.traces.len() as f64;
        }
    }
    let (mut cut_in, mut curve_in, mut corners, mut cut_min) = (0.0, 0.0, 0, 0.0);
    for ly in layers {
        for f in &ly.foam {
            let (len, smooth, c) = outline_shape(f);
            let curve = r.curve_radius_in * smooth;
            cut_in += len;
            curve_in += curve;
            corners += c;
            cut_min += (len + curve) * ly.foam_in / r.cut_in2_per_min + c as f64 * r.corner_min;
        }
    }
    let foam_pieces = layers.iter().map(|l| l.foam.len()).sum::<usize>();
    let trace_hr = (trace_in / r.trace_in_per_min + traces as f64 * r.place_min) / 60.0;
    let cut_hr = cut_min / 60.0;
    let sand_hr = (sand_in3 / r.sand_in3_per_min + surface_ft2 / r.finish_ft2_per_min) / 60.0;
    let assemble_hr = foam_pieces as f64 * r.assemble_min / 60.0;
    Labor {
        trace_hr,
        cut_hr,
        sand_hr,
        assemble_hr,
        total_hr: trace_hr + cut_hr + sand_hr + assemble_hr,
        traces,
        trace_in,
        cut_in,
        curve_in,
        corners,
        sand_in3,
        surface_ft2,
        foam_pieces,
    }
}

/// Foam left to sand off, in^3, estimated: layers stacked across a facet leave steps as deep as
/// the layer thickness times the facet normal's component along the stacking (`step`), half of it
/// on average. A flat bottom under flat layers steps a full layer; a steep side, almost nothing.
fn sand_in3(facets: &[Facet], step: impl Fn(&Facet) -> f64) -> f64 {
    facets.iter().map(|f| 0.5 * f.area * step(f)).sum()
}

/// Thickness of the layer at distance d along a stack (the last layer past its end).
fn layer_at(order: &[f64], d: f64) -> f64 {
    let mut acc = 0.0;
    for &t in order {
        acc += t;
        if d < acc {
            return t;
        }
    }
    order.last().copied().unwrap_or(0.0)
}

/// The part of a half section above height `base` (the flat stack the keel slabs stand on): from
/// where it crosses `base` to the keel. None if it hardly reaches above it.
fn clip_section(half: &[Pt], base: f64) -> Option<Vec<Pt>> {
    let i = half.iter().position(|p| p[1] > base + 1e-6)?;
    let foot = if i == 0 {
        [half[0][0], base]
    } else {
        let (a, b) = (half[i - 1], half[i]);
        let s = (base - a[1]) / (b[1] - a[1]);
        [a[0] + s * (b[0] - a[0]), base]
    };
    let mut out = vec![foot];
    out.extend(half[i..].iter().filter(|p| p[1] > base));
    (out.len() >= 2 && half_area(&out) > 0.05).then_some(out)
}

/// Hybrid keel slabs `t` thick standing on the flat stack at height `base`, across the canoe
/// wherever the hull rises above it, on a grid with a face at midships (so with a canoe the same
/// fore and aft, slab k aft is the mirror of slab -k-1 at the bow). Each template is the biggest of
/// its two faces' and middle sections above `base`, the smaller face etched: (label, x of the bow
/// face, thickness, shape), bow to stern.
fn keel_slabs(rows: &[NetRow], z_top: f64, base: f64, t: f64) -> Vec<(String, f64, f64, Shape)> {
    let (xa, xb) = (rows[0].0, rows[rows.len() - 1].0);
    let mid = 0.5 * (xa + xb);
    let above: Vec<f64> =
        rows.iter().filter(|r| z_top - r.1.last().unwrap().1 > base + 1e-6).map(|r| r.0).collect();
    let (Some(&x0), Some(&x1)) = (above.first(), above.last()) else { return vec![] };
    // One row spacing beyond the first and last rows above, so the slab grid covers the tips.
    let dx = (xb - xa) / (rows.len() - 1) as f64;
    let (x0, x1) = ((x0 - dx).max(xa), (x1 + dx).min(xb));
    let (k0, k1) = (((x0 - mid) / t).floor() as i64, ((x1 - mid) / t).ceil() as i64);
    let cut = |x: f64| clip_section(&section(rows, x.clamp(xa, xb), z_top), base);
    let mut out = vec![];
    for k in k0..k1 {
        let f0 = mid + k as f64 * t;
        let faces = [cut(f0), cut(f0 + t)];
        let Some(big) = [cut(f0), cut(f0 + t), cut(f0 + 0.5 * t)]
            .into_iter()
            .flatten()
            .max_by(|a, b| half_area(a).total_cmp(&half_area(b)))
        else {
            continue;
        };
        let guide = faces
            .into_iter()
            .flatten()
            .min_by(|a, b| half_area(a).total_cmp(&half_area(b)))
            .filter(|g| (half_area(g) - half_area(&big)).abs() > 1e-6);
        let label = format!("K{}", out.len() + 1);
        out.push((label, f0, t, Shape::Upright { half: big, guide, x: f0, grid: Some(k) }));
    }
    out
}

/// The inner section at x (linear between the neighbouring stations, point by point, as the
/// export's bulkhead rows), as a half: (y, height above the floor) from the starboard sheer on
/// the floor to the keel on the centreline.
fn section(rows: &[NetRow], x: f64, z_top: f64) -> Vec<Pt> {
    let k = rows.partition_point(|r| r.0 < x).clamp(1, rows.len() - 1);
    let (a, b) = (&rows[k - 1], &rows[k]);
    let s = if b.0 - a.0 < 1e-12 { 0.0 } else { ((x - a.0) / (b.0 - a.0)).clamp(0.0, 1.0) };
    a.1.iter().zip(&b.1).map(|(p, q)| [p.0 + s * (q.0 - p.0), z_top - (p.1 + s * (q.1 - p.1))]).collect()
}

/// A half section closed into the full section (both sides, the floor line), counter-clockwise.
fn close_section(half: &[Pt]) -> Vec<Pt> {
    let mut outline: Vec<Pt> = half.to_vec();
    outline.extend(half.iter().rev().filter(|p| p[0] > 1e-9).map(|p| [-p[0], p[1]]));
    outline.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
    if area(&outline) < 0.0 {
        outline.reverse();
    }
    outline
}

fn half_area(half: &[Pt]) -> f64 {
    area(&close_section(half)).abs()
}

/// An upright layer's hardboard piece: the full section, or (FewestShapes) its starboard half
/// closed along the centreline, traced on both sides.
#[allow(clippy::too_many_arguments)]
fn rib_pieces(
    half: &[Pt],
    guide: Option<&[Pt]>,
    x: f64,
    label: &str,
    layer: usize,
    spec: &MoldSpec,
    used: &mut Vec<usize>,
    ps: &mut Vec<Piece>,
) -> Result<()> {
    let halved = spec.packing == Packing::FewestShapes;
    // The height its foot stands at: the floor, or the flat stack under a keel slab.
    let base = half[0][1];
    let outline = if halved {
        let mut p: Vec<Pt> = half.to_vec();
        p.push([0.0, base]);
        p.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
        if area(&p) < 0.0 {
            p.reverse();
        }
        p
    } else {
        close_section(half)
    };
    ensure!(
        fits_board(&outline, spec),
        "{label} does not fit the hardboard ({} in) with a {} in gap",
        MoldSpec::sizes(&spec.sheets),
        spec.gap_in
    );
    let top = half.last().map_or(base, |p| p[1]);
    let hk = top - base;
    let text_h = (0.15 * hk).clamp(0.2, 0.75);
    let mut marks = if hk > 1.5 && !halved { vec![[[0.0, base + 0.5], [0.0, top - 0.5]]] } else { vec![] };
    if let Some(g) = guide {
        for w in g.windows(2) {
            marks.push([w[0], w[1]]);
            if !halved {
                marks.push([[-w[0][0], w[0][1]], [-w[1][0], w[1][1]]]);
            }
        }
    }
    let traces = if halved {
        vec![format!("{label} S"), format!("{label} P (flip)")]
    } else {
        vec![label.to_string()]
    };
    let y_mid = half.iter().map(|p| p[0]).fold(0.0, f64::max);
    // Alignment holes 1 and 4 in above its foot, on the centreline (half slabs: 1 in off it):
    // in line from slab to slab.
    let u = if halved { 1.0 } else { 0.0 };
    let holes = hole_spots(&outline, &[[u, base + 1.0], [u, base + 4.0]], spec);
    used.push(ps.len());
    ps.push(Piece {
        label: label.to_string(),
        layer,
        holes,
        area_in2: area(&outline).abs(),
        outline,
        marks,
        text: if halved { format!("{label} x={x:.2} x2") } else { format!("{label} x={x:.2}") },
        text_at: if halved {
            [0.45 * y_mid, base + (0.3 * hk).max(0.6 * text_h)]
        } else {
            [0.0, base + (0.3 * hk).max(0.6 * text_h)]
        },
        text_deg: 0.0,
        text_h,
        traces,
    });
    Ok(())
}

// ---------- nesting ----------

/// Raster growth: half the gap plus half a cell diagonal, so that two pieces whose rasters do
/// not overlap are at least `gap_in` apart (and a piece inside the sheet raster is inside the
/// sheet edge margin).
fn raster_pad(spec: &MoldSpec) -> f64 {
    0.5 * spec.gap_in + CELL * std::f64::consts::FRAC_1_SQRT_2
}

/// Usable raster of a [length, width] sheet: less `gap` at every edge.
fn sheet_cells(size: [f64; 2], gap: f64) -> (usize, usize) {
    let c = |v: f64| ((v - 2.0 * gap) / CELL).floor().max(0.0) as usize;
    (c(size[0]), c(size[1]))
}

/// A piece rasterised: cell (i, j) covers origin + [i, i+1] x [j, j+1] cells and is occupied
/// when its centre is within `r` of the piece (inside, or near its edge).
struct Raster {
    w: usize,
    h: usize,
    origin: Pt,
    /// Occupied cells.
    cells: usize,
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
    let rows: Vec<Vec<(usize, usize)>> = (0..h)
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
    let cells = rows.iter().flatten().map(|&(a, b): &(usize, usize)| b - a + 1).sum();
    Raster { w, h, origin, cells, rows }
}

/// Sheet occupancy as per-row prefix counts (pre[j][i] = occupied cells left of i).
struct Grid {
    nx: usize,
    ny: usize,
    /// Free cells left.
    free_cells: usize,
    pre: Vec<Vec<u32>>,
}

impl Grid {
    fn new(nx: usize, ny: usize) -> Grid {
        Grid { nx, ny, free_cells: nx * ny, pre: vec![vec![0; nx + 1]; ny] }
    }
    fn free(&self, r: &Raster, x: usize, y: usize) -> bool {
        let row = |j: usize| {
            let p = &self.pre[y + j];
            r.rows[j].iter().all(|&(a, b)| p[x + b + 1] == p[x + a])
        };
        // Every 8th row first: most collisions show there, so most spots are rejected early.
        (0..r.h).step_by(8).all(row) && (0..r.h).filter(|j| j % 8 != 0).all(row)
    }
    /// Lowest free spot: least x (along the sheet length), then least y. A tie between quarter
    /// turns goes to the first (0 deg: a long flat-layer piece along the sheet length).
    fn spot(&self, r: &Raster) -> Option<(usize, usize)> {
        if r.w > self.nx || r.h > self.ny || r.cells > self.free_cells {
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
            self.free_cells = self.free_cells.saturating_sub(runs.iter().map(|&(a, b)| b - a + 1).sum());
        }
    }
}

/// p turned counter-clockwise by deg about the origin (quarter turns exact).
fn rot(p: Pt, deg: u32) -> Pt {
    match deg % 360 {
        0 => p,
        90 => [-p[1], p[0]],
        180 => [-p[0], -p[1]],
        270 => [p[1], -p[0]],
        d => {
            let (s, c) = f64::from(d).to_radians().sin_cos();
            [c * p[0] - s * p[1], s * p[0] + c * p[1]]
        }
    }
}

/// Pack the pieces onto as few sheets as the greedy placement finds (module doc). Pieces turn
/// in quarter turns, and, with `step_deg` < 90, also to every multiple of `step_deg`: placed
/// lowest-spot-first, free angles can tilt a piece into a spot that blocks its neighbours, so
/// both are packed and the fewer sheets kept (ties: quarter turns). Each is tried with the
/// pieces largest box first, longest first and widest first (ties: that order). With several
/// sheet sizes a new sheet is the largest that holds the piece; then each sheet's pieces move to
/// the smallest size that still holds them all.
pub fn nest(pieces: &[Piece], spec: &MoldSpec, step_deg: u32) -> Result<Vec<Sheet>> {
    let dims = |p: &Piece| {
        let (lo, hi) = bbox(&p.outline);
        let (a, b) = (hi[0] - lo[0], hi[1] - lo[1]);
        (a.max(b), a.min(b))
    };
    let keys: [fn((f64, f64)) -> f64; 3] = [|(l, w)| l * w, |(l, _)| l, |(_, w)| w];
    let steps: Vec<u32> = if step_deg.clamp(1, 90) < 90 { vec![90, step_deg.max(1)] } else { vec![90] };
    let r = raster_pad(spec);
    let runs: Vec<(usize, usize, Result<Vec<Sheet>>)> = steps
        .par_iter()
        .enumerate()
        .flat_map(|(si, &step)| {
            // Every piece at every angle, rasterised once for all orders.
            let turned: Vec<Vec<Turned>> = pieces
                .par_iter()
                .map(|p| {
                    (0..360)
                        .step_by(step as usize)
                        .map(|d| {
                            let q: Vec<Pt> = p.outline.iter().map(|&v| rot(v, d)).collect();
                            let ras = raster(&q, r);
                            (d, q, ras)
                        })
                        .collect()
                })
                .collect();
            keys.par_iter()
                .enumerate()
                .map(|(ki, key)| {
                    let mut order: Vec<usize> = (0..pieces.len()).collect();
                    order.sort_by(|&a, &b| {
                        key(dims(&pieces[b])).total_cmp(&key(dims(&pieces[a]))).then(a.cmp(&b))
                    });
                    let sizes = spec.sheets_by_size();
                    let packed = nest_order(pieces, &turned, &order, spec, &sizes).map(|sheets| {
                        // Smallest size that takes each sheet's pieces on one sheet.
                        sheets
                            .into_iter()
                            .map(|sh| {
                                let mine: Vec<usize> = sh.parts.iter().map(|p| p.piece).collect();
                                sizes
                                    .iter()
                                    .rev()
                                    .filter(|z| z[0] * z[1] < sh.length_in * sh.width_in - 1e-9)
                                    .find_map(|&z| {
                                        nest_order(pieces, &turned, &mine, spec, &[z])
                                            .ok()
                                            .filter(|v| v.len() == 1)
                                    })
                                    .map_or(sh, |mut v| v.remove(0))
                            })
                            .collect::<Vec<_>>()
                    });
                    (si, ki, packed)
                })
                .collect::<Vec<_>>()
        })
        .collect();
    // Err only when every packing fails (a piece may fit only when turned off the square).
    let (mut best, mut err): (Option<Vec<Sheet>>, Option<anyhow::Error>) = (None, None);
    for (_, _, s) in runs {
        let area = |v: &Vec<Sheet>| v.iter().map(|s| s.length_in * s.width_in).sum::<f64>();
        match s {
            Ok(s)
                if best.as_ref().is_none_or(|b| {
                    s.len() < b.len() || (s.len() == b.len() && area(&s) < area(b) - 1e-9)
                }) =>
            {
                best = Some(s)
            }
            Ok(_) => {}
            Err(e) => {
                err.get_or_insert(e);
            }
        }
    }
    match (best, err) {
        (Some(b), _) => Ok(b),
        (None, Some(e)) => Err(e),
        (None, None) => Ok(vec![]),
    }
}

/// A piece turned by an angle: (deg, outline, raster).
type Turned = (u32, Vec<Pt>, Raster);

/// Pack `order` lowest-spot-first; a new sheet is the first of `sizes` that holds the piece.
fn nest_order(
    pieces: &[Piece],
    turned: &[Vec<Turned>],
    order: &[usize],
    spec: &MoldSpec,
    sizes: &[[f64; 2]],
) -> Result<Vec<Sheet>> {
    let m = spec.gap_in;
    let mut grids: Vec<Grid> = vec![];
    let mut sheets: Vec<Sheet> = vec![];
    for &pi in order {
        let p = &pieces[pi];
        let rasters = &turned[pi];
        let best = |g: &Grid| {
            rasters
                .iter()
                .enumerate()
                .filter_map(|(k, (_, _, ras))| g.spot(ras).map(|(x, y)| ((x, y), k, x, y)))
                .min_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)))
        };
        let mut hit = grids.iter().enumerate().find_map(|(s, g)| best(g).map(|b| (s, b)));
        if hit.is_none() {
            let Some((z, g, b)) = sizes.iter().find_map(|&z| {
                let (nx, ny) = sheet_cells(z, m);
                let g = Grid::new(nx, ny);
                best(&g).map(|b| (z, g, b))
            }) else {
                bail!(
                    "piece {} does not fit the hardboard ({} in) with a {} in gap",
                    p.label,
                    MoldSpec::sizes(sizes),
                    spec.gap_in
                );
            };
            grids.push(g);
            sheets.push(Sheet { length_in: z[0], width_in: z[1], parts: vec![], used_in2: 0.0 });
            hit = Some((grids.len() - 1, b));
        }
        let (s, (_, k, x, y)) = hit.unwrap();
        let (d, q, ras) = &rasters[k];
        // Nested: only a ring along the edge (and the gap outside it) is taken; the middle stays
        // free for smaller pieces.
        if spec.nest_inside {
            grids[s].fill(&ring_raster(q, raster_pad(spec), spec.ring_in, ras), x, y);
        } else {
            grids[s].fill(ras, x, y);
        }
        let shift = [m + x as f64 * CELL - ras.origin[0], m + y as f64 * CELL - ras.origin[1]];
        let mv = |v: Pt| {
            let w = rot(v, *d);
            [w[0] + shift[0], w[1] + shift[1]]
        };
        sheets[s].used_in2 += p.area_in2;
        sheets[s].parts.push(Placed {
            piece: pi,
            inside: None,
            rot_deg: *d,
            shift,
            outline: q.iter().map(|v| [v[0] + shift[0], v[1] + shift[1]]).collect(),
            marks: p.marks.iter().map(|[a, b]| [mv(*a), mv(*b)]).collect(),
            holes: p.holes.iter().map(|h| mv(*h)).collect(),
            text_at: mv(p.text_at),
            text_deg: (p.text_deg + f64::from(*d)) % 360.0,
        });
    }
    if spec.nest_inside {
        for sh in &mut sheets {
            hosts(sh, pieces, spec.ring_in);
        }
    }
    Ok(sheets)
}

/// Even-odd point in polygon.
fn inside_poly(poly: &[Pt], p: Pt) -> bool {
    let n = poly.len();
    let mut c = false;
    for i in 0..n {
        let (a, b) = (poly[i], poly[(i + 1) % n]);
        if (a[1] > p[1]) != (b[1] > p[1]) && p[0] < a[0] + (p[1] - a[1]) / (b[1] - a[1]) * (b[0] - a[0]) {
            c = !c;
        }
    }
    c
}

/// Nested parts: which part each is cut out of (the smallest that contains it), the host's
/// marks over its children dropped and its label moved onto the ring if it falls on one.
fn hosts(sh: &mut Sheet, pieces: &[Piece], ring: f64) {
    let n = sh.parts.len();
    for i in 0..n {
        let probe = sh.parts[i].outline[0];
        sh.parts[i].inside =
            (0..n).filter(|&j| j != i && inside_poly(&sh.parts[j].outline, probe)).min_by(|&a, &b| {
                pieces[sh.parts[a].piece].area_in2.total_cmp(&pieces[sh.parts[b].piece].area_in2)
            });
    }
    for h in 0..n {
        let kids: Vec<Vec<Pt>> =
            (0..n).filter(|&i| sh.parts[i].inside == Some(h)).map(|i| sh.parts[i].outline.clone()).collect();
        if kids.is_empty() {
            continue;
        }
        let on_kid = |p: Pt| kids.iter().any(|k| inside_poly(k, p));
        let part = &mut sh.parts[h];
        part.marks.retain(|[a, b]| {
            !on_kid([(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]) && !on_kid(*a) && !on_kid(*b)
        });
        part.holes.retain(|h| !on_kid(*h));
        if on_kid(part.text_at) {
            // Half a ring in from the outline, at the first vertex where that is clear of the kids.
            let o = &part.outline;
            let c = o.iter().fold([0.0, 0.0], |s, p| [s[0] + p[0], s[1] + p[1]]);
            let c = [c[0] / o.len() as f64, c[1] / o.len() as f64];
            let clear = o
                .iter()
                .map(|v| {
                    let (dx, dy) = (c[0] - v[0], c[1] - v[1]);
                    let l = dx.hypot(dy).max(1e-9);
                    [v[0] + 0.5 * ring * dx / l, v[1] + 0.5 * ring * dy / l]
                })
                .find(|&p| !on_kid(p));
            if let Some(at) = clear {
                part.text_at = at;
            }
        }
    }
}

/// The cells a nested piece takes: those of its raster `like` that lie outside it or within
/// `ring` + `r` of its edge (same origin and size as `like`).
fn ring_raster(poly: &[Pt], r: f64, ring: f64, like: &Raster) -> Raster {
    let (w, h, origin) = (like.w, like.h, like.origin);
    let mut occ = vec![false; w * h];
    for (j, runs) in like.rows.iter().enumerate() {
        for &(a, b) in runs {
            occ[j * w + a..=j * w + b].iter_mut().for_each(|c| *c = true);
        }
    }
    let cx = |i: usize| origin[0] + (i as f64 + 0.5) * CELL;
    let cy = |j: usize| origin[1] + (j as f64 + 0.5) * CELL;
    // Outside the piece: kept (the gap around it).
    let mut keep: Vec<bool> = (0..w * h).map(|k| !inside_poly(poly, [cx(k % w), cy(k / w)])).collect();
    // Within ring + r of the edge: kept.
    let reach = ring + r;
    let cell = |v: f64, o: f64| ((v - o) / CELL).floor();
    let n = poly.len();
    for e in 0..n {
        let (a, b) = (poly[e], poly[(e + 1) % n]);
        let i0 = cell(a[0].min(b[0]) - reach, origin[0]).max(0.0) as usize;
        let i1 = (cell(a[0].max(b[0]) + reach, origin[0]).max(0.0) as usize).min(w - 1);
        let j0 = cell(a[1].min(b[1]) - reach, origin[1]).max(0.0) as usize;
        let j1 = (cell(a[1].max(b[1]) + reach, origin[1]).max(0.0) as usize).min(h - 1);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let ll = dx * dx + dy * dy;
        for j in j0..=j1 {
            for i in i0..=i1 {
                let (px, py) = (cx(i) - a[0], cy(j) - a[1]);
                let t = if ll > 0.0 { ((px * dx + py * dy) / ll).clamp(0.0, 1.0) } else { 0.0 };
                if (px - t * dx).hypot(py - t * dy) <= reach {
                    keep[j * w + i] = true;
                }
            }
        }
    }
    let rows: Vec<Vec<(usize, usize)>> = (0..h)
        .map(|j| {
            let mut runs = vec![];
            let mut i = 0;
            while i < w {
                if occ[j * w + i] && keep[j * w + i] {
                    let s = i;
                    while i < w && occ[j * w + i] && keep[j * w + i] {
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
    let cells = rows.iter().flatten().map(|&(a, b)| b - a + 1).sum();
    Raster { w, h, origin, cells, rows }
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
    g(10, &dxf_num(sheet.length_in));
    g(20, &dxf_num(sheet.width_in));
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
    let (sl, sw) = (sheet.length_in, sheet.width_in);
    let marks = l.spec.dxf_marks;
    if marks {
        poly(&mut g, "SHEET", &[[0.0, 0.0], [sl, 0.0], [sl, sw], [0.0, sw]]);
    }
    // Each piece: its holes, then its outline from its sharpest corner, with the cut width
    // allowed for ([`cut_order`]).
    let half_kerf = 0.5 * l.spec.kerf_in;
    for i in cut_order(sheet) {
        let part = &sheet.parts[i];
        for h in &part.holes {
            g(0, "CIRCLE");
            g(8, "CUT");
            g(10, &dxf_num(h[0]));
            g(20, &dxf_num(h[1]));
            g(30, "0.0");
            g(40, &dxf_num(0.5 * l.spec.hole_in - half_kerf));
        }
        let outline =
            if half_kerf > 0.0 { offset_out(&part.outline, half_kerf) } else { part.outline.clone() };
        poly(&mut g, "CUT", &from_corner(&outline));
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
    for part in sheet.parts.iter().filter(|_| marks) {
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
            Orientation::Hybrid => "flat layers and keel slabs,",
        },
        l.spec.board_in,
        sl,
        sw
    );
    if marks {
        text(&mut g, [0.5 * sl, -1.0], 0.5, 0.0, &title);
    }
    g(0, "ENDSEC");
    g(0, "EOF");
    Ok(o)
}

/// The order the water jet cuts a sheet's parts: pieces cut out of others first (deepest first,
/// so a host is cut free after its middle), and within that nearest first from the sheet corner
/// (each from where the last one started).
pub fn cut_order(sheet: &Sheet) -> Vec<usize> {
    let parts = &sheet.parts;
    let depth = |mut i: usize| {
        let mut d = 0;
        while let Some(h) = parts[i].inside {
            d += 1;
            i = h;
        }
        d
    };
    let start = |i: usize| from_corner(&parts[i].outline)[0];
    let mut left: Vec<usize> = (0..parts.len()).collect();
    let (mut out, mut at) = (vec![], [0.0, 0.0]);
    while !left.is_empty() {
        let deepest = left.iter().map(|&i| depth(i)).max().unwrap();
        let k = left
            .iter()
            .enumerate()
            .filter(|&(_, &i)| depth(i) == deepest)
            .min_by(|a, b| {
                let d = |i: usize| (start(i)[0] - at[0]).hypot(start(i)[1] - at[1]);
                d(*a.1).total_cmp(&d(*b.1)).then(a.1.cmp(b.1))
            })
            .map(|(k, _)| k)
            .unwrap();
        let i = left.remove(k);
        at = start(i);
        out.push(i);
    }
    out
}

/// The outline starting at its sharpest corner (a square joint end, a tip): the water jet's
/// lead-in then marks a corner, not a traced curve.
fn from_corner(o: &[Pt]) -> Vec<Pt> {
    let n = o.len();
    let turn = |i: usize| {
        let (a, b, c) = (o[(i + n - 1) % n], o[i], o[(i + 1) % n]);
        let (u, v) = ([b[0] - a[0], b[1] - a[1]], [c[0] - b[0], c[1] - b[1]]);
        let (lu, lv) = (u[0].hypot(u[1]), v[0].hypot(v[1]));
        if lu < 1e-12 || lv < 1e-12 {
            return 0.0;
        }
        ((u[0] * v[0] + u[1] * v[1]) / (lu * lv)).clamp(-1.0, 1.0).acos()
    };
    let k = (0..n).max_by(|&a, &b| turn(a).total_cmp(&turn(b)).then(b.cmp(&a))).unwrap_or(0);
    o[k..].iter().chain(&o[..k]).copied().collect()
}

/// A counter-clockwise outline moved out by d (mitred, the mitre capped at 2 d).
fn offset_out(o: &[Pt], d: f64) -> Vec<Pt> {
    let n = o.len();
    let normal = |a: Pt, b: Pt| {
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let l = dx.hypot(dy).max(1e-12);
        [dy / l, -dx / l]
    };
    let sign = if area(o) >= 0.0 { 1.0 } else { -1.0 };
    (0..n)
        .map(|i| {
            let (a, b, c) = (o[(i + n - 1) % n], o[i], o[(i + 1) % n]);
            let (n1, n2) = (normal(a, b), normal(b, c));
            let (sx, sy) = (n1[0] + n2[0], n1[1] + n2[1]);
            let l = sx.hypot(sy);
            if l < 1e-9 {
                return [b[0] + sign * d * n1[0], b[1] + sign * d * n1[1]];
            }
            let (ux, uy) = (sx / l, sy / l);
            let m = (d / (ux * n1[0] + uy * n1[1]).max(0.5)).min(2.0 * d);
            [b[0] + sign * m * ux, b[1] + sign * m * uy]
        })
        .collect()
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
        let spec = MoldSpec { sheets: vec![[40.0, 20.0]], gap_in: 0.25, ..MoldSpec::default() };
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
                traces: vec![],
                holes: vec![],
            })
            .collect();
        let sheets = nest(&pieces, &spec, 90).unwrap();
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
    fn a_bar_longer_than_the_sheet_fits_on_the_diagonal() {
        let spec = MoldSpec { sheets: vec![[48.0, 48.0]], ..MoldSpec::default() };
        let bar = Piece {
            label: "bar".into(),
            layer: 0,
            outline: rect(55.0, 3.0),
            marks: vec![],
            text: String::new(),
            text_at: [27.5, 1.5],
            text_deg: 0.0,
            text_h: 0.5,
            area_in2: 165.0,
            traces: vec![],
            holes: vec![],
        };
        assert!(nest(std::slice::from_ref(&bar), &spec, 90).is_err(), "too long square to the edges");
        let sh = nest(&[bar], &spec, 5).unwrap();
        let p = &sh[0].parts[0];
        assert!(!p.rot_deg.is_multiple_of(90), "turned to {} deg", p.rot_deg);
        let (lo, hi) = bbox(&p.outline);
        assert!(
            lo[0] >= 0.25 - 1e-9 && lo[1] >= 0.25 - 1e-9 && hi[0] <= 47.75 + 1e-9 && hi[1] <= 47.75 + 1e-9
        );
    }

    #[test]
    fn too_big_piece_errs() {
        let spec = MoldSpec { sheets: vec![[20.0, 10.0]], ..MoldSpec::default() };
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
            traces: vec![],
            holes: vec![],
        };
        assert!(nest(&[p], &spec, 5).unwrap_err().to_string().contains("does not fit"));
    }

    /// Largest change of direction between consecutive segments, deg.
    fn max_turn(c: &[Pt]) -> f64 {
        c.windows(3)
            .map(|w| {
                let a = (w[1][1] - w[0][1]).atan2(w[1][0] - w[0][0]);
                let b = (w[2][1] - w[1][1]).atan2(w[2][0] - w[1][0]);
                (b - a).abs().to_degrees()
            })
            .fold(0.0, f64::max)
    }

    #[test]
    fn smoothing_removes_station_kinks_and_rounds_the_tip() {
        // A loft-like waterline: linear between table stations (every 2 in) of a sqrt nose, with
        // rows every 0.5 in, starting at a centreline tip at x = 0 and ending square at x = 20.
        let table: Vec<f64> = (1..=10).map(|k| 2.0 * k as f64).collect();
        let g = |x: f64| 3.0 * x.sqrt();
        let mut run: Vec<Pt> = vec![[0.0, 0.0]];
        for i in 1..=40 {
            let x = 0.5 * i as f64;
            let k = ((x / 2.0).ceil() * 2.0).max(2.0);
            let (xa, xb) = (k - 2.0, k);
            run.push([x, g(xa) + (x - xa) / 2.0 * (g(xb) - g(xa))]);
        }
        run.push([20.0, 0.0]);
        let sm = smooth_run(&run, &table);
        // Station values and both ends kept.
        for &x in &table[..9] {
            assert!((chain_y(&sm, x) - g(x)).abs() < 1e-9, "station {x}");
        }
        assert_eq!(sm[0], [0.0, 0.0]);
        assert_eq!(sm[sm.len() - 1], [20.0, 0.0]);
        assert!(sm[sm.len() - 2][0] == 20.0, "square end kept");
        // Kinks gone: the smoothed chain (less its square end) turns far less per step.
        let inner = &sm[1..sm.len() - 1];
        assert!(
            max_turn(inner) < 0.5 * max_turn(&run[2..run.len() - 1]),
            "{} vs {}",
            max_turn(inner),
            max_turn(&run[2..run.len() - 1])
        );
        // Pointed nose: the curve leaves the tip heading for the first station (no bulb).
        let (a, b) = (sm[0], sm[1]);
        let chord = g(2.0).atan2(2.0).to_degrees();
        assert!(((b[1] - a[1]).atan2(b[0] - a[0]).to_degrees() - chord).abs() < 15.0);
        // x never runs backwards.
        assert!(sm.windows(2).all(|w| w[1][0] >= w[0][0]));
    }

    /// A 200 in lens, 24 in wide (symmetric fore and aft).
    fn lens() -> Vec<Pt> {
        (0..=100)
            .map(|i| {
                let x = 2.0 * i as f64;
                [x, 12.0 * (1.0 - ((x - 100.0) / 100.0).powi(2))]
            })
            .collect()
    }

    #[test]
    fn split_run_makes_pieces_that_fit_the_foam_and_the_hardboard() {
        // 96 in foam sheets: 3 foam pieces of 66.7 in, one template each.
        let spec = MoldSpec::default();
        let cuts = split_run(&lens(), &[], "L1", None, &spec).unwrap();
        assert_eq!(cuts.len(), 3);
        assert_eq!(cuts[1].label, "L1-2");
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&cuts, 0, &spec, None, &mut used, &mut ps).unwrap();
        assert_eq!(ps.len(), 3);
        let total: f64 = ps.iter().map(|p| p.area_in2).sum();
        let exact = 2.0 * 12.0 * 200.0 * 2.0 / 3.0;
        assert!((total - exact).abs() / exact < 0.01, "{total} vs {exact}");
        for p in &ps {
            assert!(area(&p.outline) > 0.0, "counter-clockwise");
            assert_eq!(p.traces, vec![p.label.clone()]);
        }
        // Shorter foam sheets: more, shorter foam pieces.
        let short = MoldSpec { foam_sheets: vec![[45.0, 48.0]], ..spec };
        assert_eq!(split_run(&lens(), &[], "L1", None, &short).unwrap().len(), 5);
    }

    #[test]
    fn fewest_shapes_halves_and_shares_mirror_image_ends() {
        let spec = MoldSpec { packing: Packing::FewestShapes, ..MoldSpec::default() };
        let cuts = split_run(&lens(), &[], "L1", None, &spec).unwrap();
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&cuts, 0, &spec, None, &mut used, &mut ps).unwrap();
        // Bow and stern thirds are one template (four traces), the middle its own (two).
        assert_eq!(ps.len(), 2);
        assert_eq!(ps[0].traces, ["L1-1 S", "L1-1 P (flip)", "L1-3 P", "L1-3 S (flip)"]);
        assert_eq!(ps[1].traces, ["L1-2 S", "L1-2 P (flip)"]);
        assert!(ps[0].text.ends_with("x4"));
        // Half templates: on the starboard side of the centreline, half the full area.
        let (lo, _) = bbox(&ps[1].outline);
        assert!(lo[1].abs() < 1e-9);
        let full = area(&mirrored(&cuts[1].chain));
        assert!((2.0 * ps[1].area_in2 - full).abs() < 1e-6 * full);
        // A lopsided run shares nothing.
        let skew: Vec<Pt> = lens().iter().map(|p| [p[0], p[1] * (1.0 + p[0] / 400.0)]).collect();
        let cuts = split_run(&skew, &[], "L2", None, &spec).unwrap();
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&cuts, 0, &spec, None, &mut used, &mut ps).unwrap();
        assert_eq!(ps.len(), 3);
    }

    #[test]
    fn envelope_takes_the_wider_outline_everywhere() {
        // Two lenses crossing: one longer, one wider in the middle.
        let a: Vec<Pt> = vec![[0.0, 0.0], [5.0, 3.0], [10.0, 0.0]];
        let b: Vec<Pt> = vec![[2.0, 0.0], [5.0, 4.0], [8.0, 0.0]];
        let e = envelope(std::slice::from_ref(&a), std::slice::from_ref(&b));
        assert_eq!(e.len(), 1);
        for x in [1.0, 3.0, 5.0, 7.0, 9.0] {
            let want = chain_y(&a, x).max(if (2.0..=8.0).contains(&x) { chain_y(&b, x) } else { 0.0 });
            assert!(chain_y(&e[0], x) >= want - 1e-9, "x = {x}");
        }
        assert_eq!((e[0][0][0], e[0][e[0].len() - 1][0]), (0.0, 10.0));
    }

    #[test]
    fn foam_order_mixes_thicknesses_to_fit() {
        // 7 in from 1.5 and 2 in foam: exactly, in 4 layers, thick on the floor.
        assert_eq!(foam_order(&[1.5, 2.0], 0.0, 7.0, true), [2.0, 2.0, 1.5, 1.5]);
        // One thickness: enough layers to cover, the least over.
        assert_eq!(foam_order(&[2.0], 0.0, 13.12, true), [2.0; 7]);
        // Least over first (13.5 in), then fewest layers (6 x 2 + 1.5, not 3 x 2 + 5 x 1.5).
        assert_eq!(foam_order(&[1.5, 2.0], 0.0, 13.12, true), [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 1.5]);
        // Boards in the mold count toward the stack.
        assert_eq!(foam_order(&[2.0], 0.125, 6.3, true), [2.0; 3]);
        // Upright: the thin slabs go to the two ends.
        let o = foam_order(&[1.5, 2.0], 0.0, 11.0, false);
        assert_eq!(o.iter().sum::<f64>(), 11.0);
        assert_eq!((o[0], o[o.len() - 1]), (1.5, 1.5), "{o:?}");
    }

    #[test]
    fn small_last_sheet_moves_to_a_smaller_size() {
        // Ten 12 x 5 in pieces: nine fill a 40 x 20 in sheet, the tenth goes on a 20 x 10.
        let spec = MoldSpec { sheets: vec![[20.0, 10.0], [40.0, 20.0]], gap_in: 0.25, ..MoldSpec::default() };
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
                traces: vec![],
                holes: vec![],
            })
            .collect();
        let sheets = nest(&pieces, &spec, 90).unwrap();
        assert_eq!(sheets.len(), 2);
        assert_eq!((sheets[0].length_in, sheets[0].width_in), (40.0, 20.0));
        assert_eq!((sheets[1].length_in, sheets[1].width_in), (20.0, 10.0));
        for p in &sheets[1].parts {
            let (_, hi) = bbox(&p.outline);
            assert!(hi[0] <= 19.75 + 1e-9 && hi[1] <= 9.75 + 1e-9);
        }
    }

    #[test]
    fn foam_in_reads_one_number_or_a_list() {
        let one: MoldSpec = serde_json::from_str(r#"{"foam_in": 1.5}"#).unwrap();
        assert_eq!(one.foam_in, [1.5]);
        let many: MoldSpec =
            serde_json::from_str(r#"{"foam_in": [1.5, 2], "sheets": [[96, 48], [48, 48]]}"#).unwrap();
        assert_eq!(many.foam_in, [1.5, 2.0]);
        assert_eq!(many.sheets.len(), 2);
        assert!(
            serde_json::from_str::<MoldSpec>(r#"{"sheet_length_in": 96}"#).is_err(),
            "old field names refused"
        );
    }

    fn plain(outline: Vec<Pt>, label: &str) -> Piece {
        Piece {
            label: label.into(),
            layer: 0,
            area_in2: area(&outline).abs(),
            text_at: [1.0, 1.0],
            marks: vec![[[1.0, 1.0], [2.0, 1.0]]],
            outline,
            text: label.into(),
            text_deg: 0.0,
            text_h: 0.5,
            traces: vec![label.into()],
            holes: vec![],
        }
    }

    #[test]
    fn nest_inside_cuts_a_small_piece_from_a_big_one_and_cuts_it_first() {
        // A 44 x 20 in template and a 10 x 5 in one on a 48 x 24 in sheet: side by side they
        // need two sheets; nested, the small one comes out of the big one's middle.
        let ps = vec![plain(rect(44.0, 20.0), "big"), plain(rect(10.0, 5.0), "small")];
        let flat = MoldSpec { sheets: vec![[48.0, 24.0]], ..MoldSpec::default() };
        assert_eq!(nest(&ps, &flat, 90).unwrap().len(), 2);
        let spec = MoldSpec { nest_inside: true, ring_in: 1.0, ..flat };
        let sh = nest(&ps, &spec, 90).unwrap();
        assert_eq!(sh.len(), 1);
        let (big, small) = (&sh[0].parts[0], &sh[0].parts[1]);
        assert_eq!((big.piece, big.inside, small.inside), (0, None, Some(0)));
        // The ring is at least ring_in wide all round.
        let ((bl, bh), (sl, shi)) = (bbox(&big.outline), bbox(&small.outline));
        for d in [sl[0] - bl[0], sl[1] - bl[1], bh[0] - shi[0], bh[1] - shi[1]] {
            assert!(d >= 1.0 - 1e-9, "ring {d}");
        }
        // The DXF cuts the small piece before the big one.
        let (bx, sx) = (big.outline[0][0], small.outline[0][0]);
        let l = Layout {
            format: MOLD_FORMAT.into(),
            hull: "t".into(),
            spec,
            stack: Stack {
                layers: 1,
                foam_layers: 1,
                foam_pieces: 2,
                joints_in: vec![],
                foam_order_in: vec![2.0],
                over_in: 0.0,
                switch_in: None,
                sand_ft3: 0.0,
                max_step_in: 0.0,
                height_in: 2.0,
                length_in: 40.0,
                beam_in: 20.0,
                depth_in: 2.0,
                wall_in: 0.0,
            },
            layers: vec![],
            labor: labor(&[], &ps, 0.0, 0.0, &MoldSpec::default()),
            pieces: ps,
            utilization: 0.0,
            alternatives: vec![],
            sheets: sh,
        };
        let dxf = to_dxf(&l, 0).unwrap();
        let first = |x: f64| dxf.find(&format!("CUT\n10\n{}\n", dxf_num(x))).unwrap();
        assert!(first(sx) < first(bx), "inner piece cut first");
    }

    #[test]
    fn a_host_loses_marks_over_its_children_and_its_label_moves_to_the_ring() {
        let ps = vec![plain(rect(40.0, 20.0), "big"), plain(rect(10.0, 5.0), "small")];
        let spec =
            MoldSpec { sheets: vec![[48.0, 24.0]], nest_inside: true, ring_in: 1.0, ..MoldSpec::default() };
        let mut ps = ps;
        // The big piece's label and a mark sit right where the small piece will go.
        ps[0].text_at = [4.0, 3.0];
        ps[0].marks = vec![[[3.5, 3.0], [4.5, 3.0]], [[39.0, 19.0], [39.5, 19.0]]];
        let sh = nest(&ps, &spec, 90).unwrap();
        let (big, small) = (&sh[0].parts[0], &sh[0].parts[1]);
        assert_eq!(big.marks.len(), 1, "the mark over the child is gone");
        assert!(!inside_poly(&small.outline, big.text_at), "label moved off the child");
        assert!(inside_poly(&big.outline, big.text_at), "label still on the host");
    }

    #[test]
    fn outlines_start_at_their_sharpest_corner_and_offset_outward() {
        // A lens closed by a square end: the start is one of the two square corners.
        let lens: Vec<Pt> =
            (0..=20).map(|i| [i as f64, 3.0 * (1.0 - ((i as f64 - 10.0) / 10.0).powi(2))]).collect();
        let mut o: Vec<Pt> = lens.iter().map(|p| [p[0], -p[1]]).collect();
        o.extend(lens.iter().rev().skip(1).take(10).copied());
        o.push([10.0, -3.0]); // closes square at x = 10 (half lens)
        let c = from_corner(&o);
        assert_eq!(c.len(), o.len());
        assert!((c[0][0] - 10.0).abs() < 1e-9 || c[0] == [0.0, 0.0], "{:?}", c[0]);
        // A 10 x 5 rectangle moved out by 0.1 is 10.2 x 5.2.
        let (lo, hi) = bbox(&offset_out(&rect(10.0, 5.0), 0.1));
        assert!((hi[0] - lo[0] - 10.2).abs() < 1e-9 && (hi[1] - lo[1] - 5.2).abs() < 1e-9);
    }

    #[test]
    fn holes_sit_on_the_stations_clear_of_the_edge() {
        let spec = MoldSpec::default();
        let cuts = split_run(&lens(), &[], "L1", None, &spec).unwrap();
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&cuts, 0, &spec, None, &mut used, &mut ps).unwrap();
        for p in &ps {
            assert!(!p.holes.is_empty());
            for h in &p.holes {
                assert!((h[0] / TICK_IN - (h[0] / TICK_IN).round()).abs() < 1e-9 && h[1] == 0.0);
                assert!(edge_dist(&p.outline, *h) >= 0.5 * spec.hole_in + 0.375);
            }
        }
        // Half templates: 1 in off the centreline; none at all without holes.
        let half = MoldSpec { packing: Packing::FewestShapes, ..MoldSpec::default() };
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&split_run(&lens(), &[], "L1", None, &half).unwrap(), 0, &half, None, &mut used, &mut ps)
            .unwrap();
        assert!(ps.iter().flat_map(|p| &p.holes).all(|h| h[1] == 1.0));
        let off = MoldSpec { holes: false, ..MoldSpec::default() };
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&split_run(&lens(), &[], "L1", None, &off).unwrap(), 0, &off, None, &mut used, &mut ps)
            .unwrap();
        assert!(ps.iter().all(|p| p.holes.is_empty()));
    }

    #[test]
    fn cut_order_is_nested_first_then_nearest() {
        let mk = |o: Vec<Pt>, inside: Option<usize>| Placed {
            piece: 0,
            inside,
            rot_deg: 0,
            shift: [0.0, 0.0],
            outline: o,
            marks: vec![],
            holes: vec![],
            text_at: [0.0, 0.0],
            text_deg: 0.0,
        };
        let at = |x: f64, y: f64, w: f64| vec![[x, y], [x + w, y], [x + w, y + w], [x, y + w]];
        let sh = Sheet {
            length_in: 96.0,
            width_in: 48.0,
            used_in2: 0.0,
            // 0: far, 1: host, 2: inside 1, 3: near the corner
            parts: vec![
                mk(at(80.0, 30.0, 5.0), None),
                mk(at(20.0, 2.0, 20.0), None),
                mk(at(25.0, 7.0, 5.0), Some(1)),
                mk(at(1.0, 1.0, 5.0), None),
            ],
        };
        // The inner piece first; then from its corner the host is nearest, then the corner piece.
        assert_eq!(cut_order(&sh), [2, 1, 3, 0]);
    }

    #[test]
    fn taper_tips_bridges_a_neck_and_a_spike() {
        // A blunt round nose with a thin spike ahead of it (the underside reaching further than
        // the layer's other slices), mirrored at the stern.
        let mut c: Vec<Pt> = vec![[0.0, 0.0], [1.0, 0.1], [2.0, 0.15]];
        c.extend((1..=40).map(|i| {
            let x = 2.0 + 0.25 * i as f64;
            [x, 0.15 + 4.0 * ((x - 2.0) / 10.0).sqrt()]
        }));
        let tail: Vec<Pt> = c.iter().rev().map(|p| [40.0 - p[0], p[1]]).collect();
        c.extend(tail.into_iter().skip(1));
        let t = taper_tips(&c);
        // Never inside the original, and convex from each tip.
        for p in &c {
            assert!(chain_y(&t, p[0]) >= p[1] - 1e-9, "inside at x = {}", p[0]);
        }
        let slopes: Vec<f64> = t
            .windows(2)
            .filter(|w| w[0][0] < 12.0 && w[1][0] > w[0][0])
            .map(|w| (w[1][1] - w[0][1]) / (w[1][0] - w[0][0]))
            .collect();
        assert!(slopes.windows(2).all(|s| s[1] <= s[0] + 1e-9), "{slopes:?}");
        assert_eq!((t[0], t[t.len() - 1]), (c[0], c[c.len() - 1]), "tips kept");
        // A square end is left alone.
        let sq: Vec<Pt> = vec![[0.0, 0.0], [0.0, 3.0], [5.0, 4.0], [10.0, 0.0]];
        assert_eq!(taper_tips(&sq)[..2], sq[..2]);
    }

    #[test]
    fn half_canoe_cuts_the_bow_half_and_refuses_a_lopsided_canoe() {
        // The 200 in lens in three 66.7 in pieces: the stern piece reuses the bow template turned
        // end for end, the middle one (across midships) is cut whole.
        let spec = MoldSpec { half_canoe: true, packing: Packing::Continuous, ..MoldSpec::default() };
        let cuts = split_run(&lens(), &[], "L1", None, &spec).unwrap();
        let (mut used, mut ps) = (vec![], vec![]);
        flat_pieces(&cuts, 0, &spec, Some(100.0), &mut used, &mut ps).unwrap();
        assert_eq!(ps.len(), 2);
        assert_eq!(ps[0].traces, ["L1-1", "L1-3 (turned end for end)"]);
        assert!(ps[0].text.ends_with("x2"));
        assert_eq!(ps[1].traces, ["L1-2"]);
        // Fuller at the stern than the bow: no bow piece matches the stern one.
        let skew: Vec<Pt> = lens().iter().map(|p| [p[0], p[1] * (1.0 + p[0] / 400.0)]).collect();
        let cuts = split_run(&skew, &[], "L1", None, &spec).unwrap();
        let (mut used, mut ps) = (vec![], vec![]);
        let e = flat_pieces(&cuts, 0, &spec, Some(100.0), &mut used, &mut ps).unwrap_err().to_string();
        assert!(e.contains("L1-3") && e.contains("not the same fore and aft"), "{e}");
    }

    #[test]
    fn clip_section_keeps_the_part_above_the_platform() {
        // A half section from the sheer (y 10, h 0) to the keel (y 0, h 10), straight.
        let half: Vec<Pt> = (0..=10).map(|i| [10.0 - i as f64, i as f64]).collect();
        let c = clip_section(&half, 6.5).unwrap();
        assert_eq!(c[0], [3.5, 6.5], "foot where it crosses the platform");
        assert_eq!(c[c.len() - 1], [0.0, 10.0]);
        assert!(c.iter().all(|p| p[1] >= 6.5));
        assert!(clip_section(&half, 10.0).is_none(), "nothing above the keel");
        // Layer thickness along a stack, the last one past its end.
        assert_eq!(
            [layer_at(&[2.0, 2.0, 1.5], 0.5), layer_at(&[2.0, 2.0, 1.5], 4.2), layer_at(&[2.0, 1.5], 9.0)],
            [2.0, 1.5, 1.5]
        );
    }

    #[test]
    fn outline_shape_counts_length_curves_and_corners() {
        // A 10 x 5 rectangle: 30 in, four corners, no curve.
        let (len, smooth, corners) = outline_shape(&rect(10.0, 5.0));
        assert!((len - 30.0).abs() < 1e-9 && smooth.abs() < 1e-9 && corners == 4);
        // A finely drawn circle of radius 10: one full turn, all of it smooth.
        let circle: Vec<Pt> = (0..400)
            .map(|i| {
                let a = i as f64 / 400.0 * std::f64::consts::TAU;
                [10.0 * a.cos(), 10.0 * a.sin()]
            })
            .collect();
        let (len, smooth, corners) = outline_shape(&circle);
        assert!((len - 20.0 * std::f64::consts::PI).abs() < 0.01);
        assert!((smooth - std::f64::consts::TAU).abs() < 1e-6 && corners == 0);
        // Rates: a partial set fills in the rest; bad rates are refused.
        let r: LaborRates = serde_json::from_str(r#"{"sand_in3_per_min": 3}"#).unwrap();
        assert_eq!((r.sand_in3_per_min, r.trace_in_per_min), (3.0, 60.0));
        let bad = MoldSpec {
            labor: LaborRates { cut_in2_per_min: 0.0, ..LaborRates::default() },
            ..MoldSpec::default()
        };
        assert!(bad.validate().is_err());
    }
}
