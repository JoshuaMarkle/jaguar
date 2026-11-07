//! The two drawing sheets of a finished design (docs/SPEC.md "Drawing sheets"): sheet 1 the
//! canoe (the competition sheet), sheet 2 the male foam mold. Composed from `drawing::Model`
//! with `drafting` and written as vector PDF, SVG and DXF. Post-processing of a final result
//! only; never part of a search.
//!
//! Layout: a border with zone and centre marks, the BOM, notes, scale bar, revision table and
//! title block in the right-hand column, the views packed in rows on the rest. Every view has a
//! nominal scale; when the views do not fit the sheet every scale steps to the next standard
//! one together (and, last, the tables' text shrinks) until they do, so views never overlap.
//! Each view states the scale it was drawn at; the isometric is marked do-not-scale.

use crate::drafting::{DimTag, Doc, HAlign, Item, Logo, P, Pen, Style, VAlign, text_width};
use crate::drawing::{self, BomLine, Model};
use crate::params::{Drawing, Mold, Revision, SheetSize};
use anyhow::{Context, Result, bail};

/// Version stamp printed on every sheet: package version, commit, dirty flag, branch.
pub const STAMP: &str = concat!("JAGUAR ", env!("CARGO_PKG_VERSION"), " ", env!("JAGUAR_GIT"));

/// Standard scale denominators views snap to.
const STD: [f64; 36] = [
    1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 16.0, 20.0, 24.0, 25.0, 30.0, 32.0, 40.0, 48.0,
    50.0, 60.0, 64.0, 75.0, 80.0, 96.0, 100.0, 120.0, 125.0, 150.0, 160.0, 200.0, 240.0, 250.0, 300.0, 400.0,
    500.0, 1000.0,
];
/// Scale multipliers tried, in order, until the views fit.
const FACTORS: [f64; 14] = [1.0, 1.2, 1.25, 1.5, 1.6, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 16.0];
/// Station ticks and alignment holes along the mold layers, in (as `manufacture`'s 12 in stations).
const TICK_IN: f64 = 12.0;

/// The rendered sheets.
pub struct Drawings {
    pub model: Model,
    pub canoe: Doc,
    pub mold: Doc,
    pub warnings: Vec<String>,
}

impl Drawings {
    /// File name stem: the hull's name (as the STEP export), letters, digits, - and _ only.
    pub fn stem(&self) -> String {
        self.model
            .hull_name
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect()
    }
}

/// Both sheets of the result `text` with the drawing inputs `spec`, stamped `stamp`.
pub fn render(text: &str, spec: Drawing, stamp: &str) -> Result<Drawings> {
    let model = drawing::model(text, spec)?;
    let logo = match &model.spec.logo {
        Some(p) => Some(Logo::load(p)?),
        None => None,
    };
    let st = match model.spec.sheet_size {
        SheetSize::D => Style::d(),
        SheetSize::B => Style::b(),
    };
    let c = Ctx {
        m: &model,
        st,
        po: model.spec.precision_overall as usize,
        pl: model.spec.precision_laminate as usize,
    };
    let mut canoe = canoe_sheet(&c, stamp)?;
    let mut mold = mold_sheet(&c, stamp)?;
    for d in [&mut canoe, &mut mold] {
        d.logo = logo.clone();
        d.info.push(("JaguarDesign".into(), model.hull_name.clone()));
        d.info.push(("JaguarStamp".into(), stamp.to_string()));
        d.info.push((
            "JaguarBOMRecord".into(),
            if model.bom_record.is_empty() {
                "all quantities computed".into()
            } else {
                model.bom_record.join("\n")
            },
        ));
        if !model.warnings.is_empty() {
            d.info.push(("JaguarWarnings".into(), model.warnings.join("\n")));
        }
    }
    let warnings = model.warnings.clone();
    Ok(Drawings { model, canoe, mold, warnings })
}

struct Ctx<'a> {
    m: &'a Model,
    st: Style,
    po: usize,
    pl: usize,
}

impl Ctx<'_> {
    fn o(&self, v: f64) -> String {
        format!("{v:.p$}", p = self.po)
    }
    fn l(&self, v: f64) -> String {
        format!("{v:.p$}", p = self.pl)
    }
}

/// A view or table: its size on the paper and how to draw it with its lower left at a point.
struct Block<'a> {
    w: f64,
    h: f64,
    draw: Draw<'a>,
}

/// Draws a block with its lower left at a point.
type Draw<'a> = Box<dyn Fn(&mut Doc, P) + 'a>;

fn snap(d: f64) -> f64 {
    STD.iter().copied().find(|&s| s >= d - 1e-9).unwrap_or(d.ceil())
}

fn scale_txt(s: f64) -> String {
    if s >= 1.0 { format!("1:{}", (s * 1000.0).round() / 1000.0) } else { format!("{}:1", (1.0 / s).round()) }
}

fn title_h(st: &Style) -> f64 {
    st.title * 1.6 + st.text * 2.0
}

/// View title centred at `cx` in the strip from `y0` up (`title_h` tall): name, then scale.
fn view_title(d: &mut Doc, cx: f64, y0: f64, name: &str, scale: Option<f64>) {
    let st = d.style;
    match scale {
        Some(s) => {
            d.text([cx, y0 + st.text * 2.0], st.title, name, HAlign::Center, VAlign::Bottom);
            d.text(
                [cx, y0 + 0.4 * st.text],
                st.text,
                format!("SCALE {}", scale_txt(s)),
                HAlign::Center,
                VAlign::Bottom,
            );
        }
        None => d.text([cx, y0 + st.text * 1.2], st.title, name, HAlign::Center, VAlign::Bottom),
    }
}

/// Dimension with its line at the absolute coordinate `at` (paper) on the other axis.
#[allow(clippy::too_many_arguments)]
fn dim_to(d: &mut Doc, a: P, b: P, axis: usize, at: f64, text: &str, model: f64, s: f64) {
    let o = 1 - axis;
    let (lo, hi) = (a[o].min(b[o]), a[o].max(b[o]));
    if at >= hi {
        d.dim(a, b, axis, 1.0, at - hi, text, model, s);
    } else {
        d.dim(a, b, axis, -1.0, (lo - at).max(0.0), text, model, s);
    }
}

/// Ordinate dimension from a datum: value text (rotated) at the end of an extension line.
#[allow(clippy::too_many_arguments)]
fn ordinate(d: &mut Doc, datum: P, from: P, to_y: f64, text: &str, model: f64, s: f64) {
    let st = d.style;
    d.items.push(Item::DimBegin(DimTag {
        text: text.into(),
        model,
        scale: s,
        a: [datum[0], to_y],
        b: [from[0], to_y],
        axis: 0,
    }));
    d.line([from[0], from[1] - st.ext_gap], [from[0], to_y], Pen::DIM);
    d.text_rot([from[0] + 0.35 * st.small, to_y - 0.05], st.small, text, HAlign::Right, VAlign::Bottom, 90.0);
    d.items.push(Item::DimEnd);
}

/// y at x of a polyline sorted by x (clamped at its ends).
fn interp(pts: &[P], x: f64) -> f64 {
    let n = pts.len();
    if n == 0 {
        return 0.0;
    }
    let k = pts.partition_point(|p| p[0] < x);
    if k == 0 {
        return pts.first().map_or(0.0, |p| p[1]);
    }
    if k >= n {
        return pts.last().map_or(0.0, |p| p[1]);
    }
    let (a, b) = (pts[k - 1], pts[k]);
    if b[0] - a[0] < 1e-12 { b[1] } else { a[1] + (x - a[0]) / (b[0] - a[0]) * (b[1] - a[1]) }
}

/// The part of polygon `poly` with x <= `xc` (Sutherland-Hodgman, one plane).
fn clip_x(poly: &[P], xc: f64) -> Vec<P> {
    let n = poly.len();
    let mut out = vec![];
    for i in 0..n {
        let (a, b) = (poly[i], poly[(i + 1) % n]);
        let (ina, inb) = (a[0] <= xc, b[0] <= xc);
        if ina {
            out.push(a);
        }
        if ina != inb {
            let t = (xc - a[0]) / (b[0] - a[0]);
            out.push([xc, a[1] + t * (b[1] - a[1])]);
        }
    }
    out
}

/// Words of `s` in lines no wider than `w` at text height `h`.
fn wrap(s: &str, h: f64, w: f64) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for para in s.split('\n') {
        let mut cur = String::new();
        for word in para.split_whitespace() {
            let cand = if cur.is_empty() { word.to_string() } else { format!("{cur} {word}") };
            if !cur.is_empty() && text_width(&cand, h) > w {
                out.push(std::mem::take(&mut cur));
                cur = word.to_string();
            } else {
                cur = cand;
            }
        }
        out.push(cur);
    }
    out
}

/// A table: column widths, cells wrapped to them, the first `header` rows centred.
struct Table {
    cols: Vec<f64>,
    cells: Vec<Vec<Vec<String>>>,
    heights: Vec<f64>,
    h: f64,
    header: usize,
    title: Option<String>,
}

impl Table {
    fn new(cols: Vec<f64>, rows: Vec<Vec<String>>, h: f64, header: usize, title: Option<String>) -> Table {
        let pad = 0.05;
        let cells: Vec<Vec<Vec<String>>> = rows
            .iter()
            .map(|r| r.iter().zip(&cols).map(|(s, w)| wrap(s, h, w - 2.0 * pad)).collect())
            .collect();
        let heights = cells
            .iter()
            .map(|r| r.iter().map(Vec::len).max().unwrap_or(1).max(1) as f64 * 1.45 * h + 0.6 * h)
            .collect();
        Table { cols, cells, heights, h, header, title }
    }
    fn width(&self) -> f64 {
        self.cols.iter().sum()
    }
    fn title_h(&self) -> f64 {
        if self.title.is_some() { 2.0 * self.h } else { 0.0 }
    }
    fn height(&self) -> f64 {
        self.heights.iter().sum::<f64>() + self.title_h()
    }
    /// Draw with the top left corner at `tl`.
    fn draw(&self, d: &mut Doc, tl: P) {
        let (w, h) = (self.width(), self.h);
        let mut y = tl[1];
        if let Some(t) = &self.title {
            d.text([tl[0] + 0.5 * w, y - 0.5 * self.title_h()], 1.15 * h, t, HAlign::Center, VAlign::Middle);
            y -= self.title_h();
        }
        let body = self.heights.iter().sum::<f64>();
        d.rect([tl[0], y - body], [tl[0] + w, y], Pen::BORDER_THIN);
        let mut x = tl[0];
        for cw in self.cols.iter().take(self.cols.len().saturating_sub(1)) {
            x += cw;
            d.line([x, y], [x, y - body], Pen::BORDER_THIN);
        }
        for (r, (row, rh)) in self.cells.iter().zip(&self.heights).enumerate() {
            let mut x = tl[0];
            for (lines, cw) in row.iter().zip(&self.cols) {
                for (k, line) in lines.iter().enumerate() {
                    let ty = y - 0.4 * h - (k as f64 + 1.0) * 1.45 * h + 0.45 * h;
                    if r < self.header {
                        d.text([x + 0.5 * cw, ty], h, line.clone(), HAlign::Center, VAlign::Bottom);
                    } else {
                        d.text([x + 0.05, ty], h, line.clone(), HAlign::Left, VAlign::Bottom);
                    }
                }
                x += cw;
            }
            y -= rh;
            if r + 1 < self.cells.len() {
                d.line(
                    [tl[0], y],
                    [tl[0] + w, y],
                    if r + 1 == self.header { Pen::BORDER } else { Pen::BORDER_THIN },
                );
            }
        }
    }
}

fn table_block<'a>(t: Table) -> Block<'a> {
    let (w, h) = (t.width(), t.height());
    Block { w, h, draw: Box::new(move |d, o| t.draw(d, [o[0], o[1] + h])) }
}

/// Shelf packing of blocks (w, h) into an area w x h: rows from the top, in the given order,
/// else tallest first. Lower-left corners relative to the area's lower left.
fn pack(aw: f64, ah: f64, sizes: &[(f64, f64)], gap: f64) -> Option<Vec<P>> {
    let mut by_h: Vec<usize> = (0..sizes.len()).collect();
    by_h.sort_by(|&a, &b| sizes[b].1.total_cmp(&sizes[a].1).then(a.cmp(&b)));
    for order in [(0..sizes.len()).collect::<Vec<_>>(), by_h] {
        let mut pos = vec![[0.0, 0.0]; sizes.len()];
        let (mut x, mut top, mut row_h) = (0.0, ah, 0.0f64);
        let mut ok = true;
        for &i in &order {
            let (w, h) = sizes[i];
            if w > aw {
                ok = false;
                break;
            }
            if x > 0.0 && x + w > aw {
                top -= row_h + gap;
                x = 0.0;
                row_h = 0.0;
            }
            pos[i] = [x, top - h];
            x += w + gap;
            row_h = row_h.max(h);
        }
        if ok && top - row_h >= -1e-9 {
            return Some(pos);
        }
    }
    None
}

// ---------- sheet frame ----------

fn page(size: SheetSize) -> (f64, f64, f64, f64) {
    // (width, height, margin, right column width)
    match size {
        SheetSize::D => (34.0, 22.0, 0.5, 7.0),
        SheetSize::B => (17.0, 11.0, 0.375, 5.4),
    }
}

/// Border, zone marks and labels, centre marks on all four edges, the version stamp.
fn frame(d: &mut Doc, size: SheetSize, stamp: &str) {
    let (w, h, m, _) = page(size);
    let st = d.style;
    d.rect([m, m], [w - m, h - m], Pen::BORDER);
    let (nc, nr) = if size == SheetSize::D { (8, 4) } else { (4, 2) };
    for k in 0..=nc {
        let x = m + (w - 2.0 * m) * k as f64 / nc as f64;
        if k > 0 && k < nc {
            d.line([x, m], [x, m * 0.45], Pen::BORDER_THIN);
            d.line([x, h - m], [x, h - m * 0.45], Pen::BORDER_THIN);
        }
        if k < nc {
            let cx = x + 0.5 * (w - 2.0 * m) / nc as f64;
            let label = (nc - k).to_string();
            d.text([cx, 0.5 * m], st.small, label.clone(), HAlign::Center, VAlign::Middle);
            d.text([cx, h - 0.5 * m], st.small, label, HAlign::Center, VAlign::Middle);
        }
    }
    for k in 0..=nr {
        let y = m + (h - 2.0 * m) * k as f64 / nr as f64;
        if k > 0 && k < nr {
            d.line([m, y], [m * 0.45, y], Pen::BORDER_THIN);
            d.line([w - m, y], [w - m * 0.45, y], Pen::BORDER_THIN);
        }
        if k < nr {
            let cy = y + 0.5 * (h - 2.0 * m) / nr as f64;
            let label = ((b'A' + k as u8) as char).to_string();
            d.text([0.5 * m, cy], st.small, label.clone(), HAlign::Center, VAlign::Middle);
            d.text([w - 0.5 * m, cy], st.small, label, HAlign::Center, VAlign::Middle);
        }
    }
    // Centre marks: across the border at the middle of each edge.
    let c = 0.25;
    d.line([0.5 * w, m * 0.15], [0.5 * w, m + c], Pen::BORDER);
    d.line([0.5 * w, h - m * 0.15], [0.5 * w, h - m - c], Pen::BORDER);
    d.line([m * 0.15, 0.5 * h], [m + c, 0.5 * h], Pen::BORDER);
    d.line([w - m * 0.15, 0.5 * h], [w - m - c, 0.5 * h], Pen::BORDER);
    d.text([m + 0.6, 0.18 * m], 0.75 * st.small, stamp, HAlign::Left, VAlign::Bottom);
}

fn bom_table(c: &Ctx, bom: &[BomLine], width: f64, h: f64) -> Table {
    let (pw, qw) = (0.9 * width / 7.0 + 0.3, 1.25 * width / 7.0 + 0.2);
    let mut rows = vec![vec!["PART NO".to_string(), "QTY".into(), "DESCRIPTION".into()]];
    for l in bom {
        let tag = if l.overridden.is_some() {
            " (O)"
        } else if l.manual {
            " (H)"
        } else {
            ""
        };
        rows.push(vec![format!("{}{tag}", l.part), format!("{} {}", l.qty_text(), l.unit), l.desc.clone()]);
    }
    let _ = c;
    Table::new(vec![pw, qw, width - pw - qw], rows, h, 1, Some("BILL OF MATERIALS".into()))
}

fn revision_table(revs: &[Revision], width: f64, h: f64) -> Table {
    let mut rows = vec![vec!["REV".to_string(), "DATE".into(), "DESCRIPTION".into(), "BY".into()]];
    if revs.is_empty() {
        rows.push(vec!["-".into(), String::new(), String::new(), String::new()]);
    }
    for r in revs {
        rows.push(vec![r.rev.clone(), r.date.clone(), r.description.clone(), r.by.clone()]);
    }
    let (a, b, dd) = (0.45 + 0.1 * width / 7.0, 1.1 * width / 7.0 + 0.1, 0.7);
    Table::new(vec![a, b, width - a - b - dd, dd], rows, h, 1, Some("REVISIONS".into()))
}

/// Title block, `w` wide, its lower left at `o`; returns its height when `draw` is false.
fn title_block(d: &mut Doc, c: &Ctx, o: P, w: f64, title: &str, sheet: u32, draw: bool) -> f64 {
    let st = c.st;
    let s = &c.m.spec;
    let rh = 3.2 * st.text;
    let big_h = 2.6 * rh;
    type Row<'r> = (f64, Vec<(f64, &'r str, String)>);
    let rows: Vec<Row> = vec![
        (rh, vec![(1.0, "NOTE", s.note.clone())]),
        (rh, vec![(1.0, "UNITS", "ALL DIMENSIONS IN INCHES UNLESS OTHERWISE NOTED".into())]),
        (
            rh,
            vec![
                (0.2, "SIZE", format!("{:?}", s.sheet_size)),
                (0.4, "SCALE", "AS NOTED".into()),
                (0.4, "SHEET", format!("{sheet} OF 2")),
            ],
        ),
        (rh, vec![(0.65, "CHECKED BY", s.checked_by.clone()), (0.35, "DATE", s.checked_date.clone())]),
        (rh, vec![(0.65, "DRAWN BY", s.drawn_by.clone()), (0.35, "DATE", s.drawn_date.clone())]),
        (1.3 * rh, vec![(1.0, "DRAWING TITLE", title.to_string())]),
    ];
    let total = rows.iter().map(|r| r.0).sum::<f64>() + big_h;
    if !draw {
        return total;
    }
    let mut y = o[1];
    for (h, cells) in &rows {
        let mut x = o[0];
        for (frac, label, value) in cells {
            let cw = frac * w;
            d.rect([x, y], [x + cw, y + h], Pen::BORDER_THIN);
            d.text([x + 0.05, y + h - 0.06], 0.7 * st.small, *label, HAlign::Left, VAlign::Top);
            let vh = if *label == "DRAWING TITLE" { st.title } else { st.text };
            if let Some(line) = wrap(value, vh, cw - 0.1).first() {
                d.text([x + 0.08, y + 0.06], vh, line.clone(), HAlign::Left, VAlign::Bottom);
            }
            x += cw;
        }
        y += h;
    }
    // Top: logo, canoe name, school.
    let logo_w = if s.logo.is_some() { big_h } else { 0.0 };
    d.rect([o[0], y], [o[0] + w, y + big_h], Pen::BORDER_THIN);
    if s.logo.is_some() {
        d.rect([o[0], y], [o[0] + logo_w, y + big_h], Pen::BORDER_THIN);
        d.items.push(Item::Image { at: [o[0] + 0.05, y + 0.05], w: logo_w - 0.1, h: big_h - 0.1 });
    }
    let tx = o[0] + logo_w + 0.12;
    d.text([tx, y + big_h - 0.08], 0.7 * st.small, "CANOE", HAlign::Left, VAlign::Top);
    d.text([tx, y + 0.5 * big_h], st.big, s.canoe_name.trim(), HAlign::Left, VAlign::Bottom);
    d.text([tx, y + 0.18 * big_h], st.title, s.school.trim(), HAlign::Left, VAlign::Bottom);
    d.rect([o[0], o[1]], [o[0] + w, y + big_h], Pen::BORDER);
    total
}

/// Scale bar: four 1 ft segments at scale `s`, alternately filled, with labels.
fn scale_bar(d: &mut Doc, o: P, s: f64) -> f64 {
    let st = d.style;
    let seg = 12.0 / s;
    let hh = 0.08;
    for k in 0..4 {
        let x = o[0] + k as f64 * seg;
        if k % 2 == 0 {
            d.fill(
                vec![[x, o[1]], [x + seg, o[1]], [x + seg, o[1] + hh], [x, o[1] + hh]],
                [0.0; 3],
                crate::drafting::Layer::Dim,
            );
        }
        d.rect([x, o[1]], [x + seg, o[1] + hh], Pen::DIM);
        d.text([x, o[1] + hh + 0.05], st.small, format!("{k}"), HAlign::Center, VAlign::Bottom);
    }
    d.items.push(Item::DimBegin(DimTag {
        text: "4 FT".into(),
        model: 48.0,
        scale: s,
        a: o,
        b: [o[0] + 4.0 * seg, o[1]],
        axis: 0,
    }));
    d.text([o[0] + 4.0 * seg, o[1] + hh + 0.05], st.small, "4 FT", HAlign::Center, VAlign::Bottom);
    d.items.push(Item::DimEnd);
    d.text(
        [o[0], o[1] - 0.08],
        st.small,
        format!("SCALE BAR, {} VIEWS", scale_txt(s)),
        HAlign::Left,
        VAlign::Top,
    );
    hh + 0.2 + 2.0 * st.small
}

/// One sheet: the frame, the right-hand column, then the views packed in what is left.
#[allow(clippy::too_many_arguments)]
fn compose<'a>(
    c: &'a Ctx<'a>,
    stamp: &str,
    title: &str,
    sheet: u32,
    revs: &[Revision],
    bom: &[BomLine],
    notes: &[String],
    make: &dyn Fn(f64, f64) -> (Vec<Block<'a>>, f64),
) -> Result<Doc> {
    let size = c.m.spec.sheet_size;
    let (w, h, m, colw) = page(size);
    let st = c.st;
    let gap = 0.25;
    let col_x = w - m - colw - 0.1;
    let cw = colw;
    let doc_title = format!("{} - {title}", c.m.spec.canoe_name.trim());
    for g in [1.0, 0.85, 0.72] {
        let th = st.small * g;
        let bt = bom_table(c, bom, cw, th);
        let rt = revision_table(revs, cw, th);
        let mut probe = Doc::new(w, h, st, &doc_title);
        let tb_h = title_block(&mut probe, c, [0.0, 0.0], cw, title, sheet, false);
        let note_lines: Vec<(usize, String)> = notes
            .iter()
            .enumerate()
            .flat_map(|(i, n)| {
                wrap(&format!("{}. {n}", i + 1), th, cw - 0.3).into_iter().map(move |l| (i, l))
            })
            .collect();
        let notes_h = (note_lines.len() as f64 + 1.5) * 1.5 * th;
        let bar_h = 0.2 + 3.0 * st.small;
        let col_h = bt.height() + notes_h + bar_h + rt.height() + tb_h + 4.0 * 0.15;
        if col_h > h - 2.0 * m - 0.2 {
            continue;
        }
        let (ax, ay, aw, ah) = (m + gap, m + gap, col_x - m - 2.0 * gap, h - 2.0 * m - 2.0 * gap);
        for f in FACTORS {
            let (blocks, primary) = make(f, g);
            let sizes: Vec<(f64, f64)> = blocks.iter().map(|b| (b.w, b.h)).collect();
            let Some(pos) = pack(aw, ah, &sizes, gap) else { continue };
            let mut d = Doc::new(w, h, st, &doc_title);
            frame(&mut d, size, stamp);
            let x0 = col_x;
            let mut y = h - m - 0.1;
            bt.draw(&mut d, [x0, y]);
            y -= bt.height() + 0.25;
            d.text([x0, y], st.text, "NOTES:", HAlign::Left, VAlign::Top);
            y -= 1.6 * st.text;
            for (_, l) in &note_lines {
                d.text([x0 + 0.1, y], th, l.clone(), HAlign::Left, VAlign::Top);
                y -= 1.5 * th;
            }
            let bar_y = y - 0.15 - 2.0 * st.small;
            scale_bar(&mut d, [x0 + 0.1, bar_y], primary);
            let tb_y = m + 0.1;
            title_block(&mut d, c, [x0, tb_y], cw, title, sheet, true);
            rt.draw(&mut d, [x0, tb_y + tb_h + 0.1 + rt.height()]);
            for (b, p) in blocks.iter().zip(&pos) {
                (b.draw)(&mut d, [ax + p[0], ay + p[1]]);
            }
            return Ok(d);
        }
    }
    bail!(
        "the {title} does not fit an ANSI {:?} sheet even at the smallest scales: use ANSI D or turn off the \
         body plan / offsets table / stands diagram",
        size
    )
}

// ---------- sheet 1: the canoe ----------

fn canoe_sheet(c: &Ctx, stamp: &str) -> Result<Doc> {
    let m = c.m;
    let lam = &m.laminate;
    let mut notes = vec![
        "ALL DIMENSIONS IN INCHES UNLESS OTHERWISE NOTED.".to_string(),
        format!(
            "DECIMALS: OVERALL DIMENSIONS AND OFFSETS {} PLACES, LAMINATE {} PLACES.",
            c.po, c.pl
        ),
        "STATIONS ARE MEASURED FROM THE BOW DATUM (STA 0) AT THE BOW TIP; HEIGHTS FROM THE KEEL BASELINE (Z = 0).".into(),
        format!(
            "WALL {} THICK, {} EQUAL LIFTS ON A {} MOLD, {} MESH LAYER(S) (DETAIL B).",
            c.l(m.t),
            lam.lifts,
            match lam.mold {
                Mold::Male => "MALE",
                Mold::Female => "FEMALE",
            },
            lam.meshes.len()
        ),
        format!(
            "DESIGN HULL WEIGHT {:.1} LB. STATIC WATERLINE: LOAD CASE {} ({:.0} LB DISPLACEMENT), GATED FREEBOARD {}.",
            m.hull_weight_lb,
            m.waterline.case,
            m.waterline.displacement_lb,
            c.o(m.waterline.freeboard_gate_in)
        ),
        if m.verified {
            "DESIGN VERIFIED BY JAGUAR (ALL CHECKS AND GATES).".into()
        } else {
            format!("DESIGN NOT VERIFIED: {}.", m.verify_note.as_deref().unwrap_or("see the result"))
        },
    ];
    if m.bom.iter().any(|l| l.overridden.is_some() || l.manual) {
        notes.push(
            "BOM: (O) QUANTITY OVERRIDDEN, (H) ENTERED BY HAND; COMPUTED VALUES IN THE FILE METADATA.".into(),
        );
    }
    if !m.spec.note.trim().is_empty() {
        notes.push(m.spec.note.trim().to_string());
    }
    let make = |f: f64, g: f64| -> (Vec<Block>, f64) {
        let s_lines = snap(25.0 * f);
        let mut b = vec![lines_group(c, s_lines), iso_view(c, snap(30.0 * f)), section_aa(c, snap(10.0 * f))];
        b.push(detail_b(c, snap(4.0 * f)));
        b.push(detail_c(c, snap(10.0 * f)));
        b.push(end_view(c, snap(15.0 * f)));
        if m.spec.body_plan {
            b.push(body_plan(c, snap(15.0 * f)));
        }
        if m.spec.stands_diagram {
            b.push(stands_view(c, snap(50.0 * f)));
        }
        if m.spec.offsets_table {
            b.push(table_block(offsets_table(c, g)));
        }
        (b, s_lines)
    };
    compose(c, stamp, "CANOE DRAWING", 1, &m.spec.revisions, &m.bom, &notes, &make)
}

/// Elevation (profile) above the plan, one scale, aligned on the stations.
fn lines_group<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let row = 3.0 * st.text;
    let (l, dp, hb) = (m.length, m.depth, 0.5 * m.beam);
    let (wl, he, hp) = (l / s, dp / s, m.beam / s);
    let th = title_h(&st);
    let pad_l = 1.0 + text_width("FREEBOARD", st.small);
    let pad_r = 0.5
        + m.graphics
            .iter()
            .map(|g| text_width(&format!("\"{}\" (H 00.00)", g.name), st.small))
            .fold(text_width("DWL 4_COED (0000 LB)", st.small), f64::max);
    let ord = if m.zones.is_some() { 2.0 * row + text_width("000.00", st.small) } else { 0.0 };
    let e_top = 1.8 * row;
    let e_bot = ord + 2.2 * st.text;
    let p_bot = (2 + m.graphics.len()) as f64 * row + 0.2;
    let p_top = 0.5;
    let gap = 0.35;
    let h = th + p_bot + hp + p_top + gap + th + e_bot + he + e_top;
    let w = pad_l + wl + pad_r;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let x0 = o[0] + pad_l;
            let yp = o[1] + th + p_bot + hb / s;
            let ye_title = o[1] + th + p_bot + hp + p_top + gap;
            let ye = ye_title + th + e_bot;
            let e = |x: f64, z: f64| [x0 + x / s, ye + z / s];
            let pl = |x: f64, y: f64| [x0 + x / s, yp + y / s];
            // ---- elevation ----
            d.polygon(m.profile.iter().map(|p| e(p[0], p[1])).collect(), Pen::VISIBLE);
            d.path(m.inner_keel.iter().map(|p| e(p[0], p[1])).collect(), Pen::HIDDEN);
            for x in m.hollow {
                d.line(e(x, interp(&m.inner_keel, x)), e(x, dp), Pen::HIDDEN);
            }
            if let Some(lp) = m.air_pocket_in {
                for x in [lp, l - lp] {
                    d.line(e(x, interp(&m.inner_keel, x)), e(x, dp), Pen::HIDDEN);
                }
                let at = e(l - lp, 0.5 * dp);
                d.leader(
                    [at[0] - 0.6, ye + he + 0.35 * row],
                    at,
                    &format!("AIR POCKET BULKHEADS AT {} FROM EACH END", c.o(lp)),
                );
            }
            if let Some(zl) = &m.zones {
                for r in &zl.dense {
                    d.rect(e(r.x0_in, r.z0_in), e(r.x1_in, r.z1_in.min(dp)), Pen::PHANTOM);
                    let mid = e(0.5 * (r.x0_in + r.x1_in), 0.5 * (r.z0_in + r.z1_in.min(dp)));
                    d.text(mid, 0.8 * st.small, "C-02", HAlign::Center, VAlign::Middle);
                }
                let mut xs: Vec<f64> = zl
                    .dense
                    .iter()
                    .flat_map(|r| [r.x0_in, r.x1_in])
                    .filter(|&x| x > 1e-6 && x < l - 1e-6)
                    .collect();
                xs.sort_by(f64::total_cmp);
                xs.dedup_by(|a, b| (*a - *b).abs() < 1e-6);
                let mut last = f64::NEG_INFINITY;
                let mut lvl = 0;
                for x in xs {
                    let k = interp(&m.keel, x);
                    d.line(e(x, k), e(x, dp), Pen::PHANTOM);
                    let px = e(x, 0.0)[0];
                    lvl = if px - last < 1.6 * st.small { 1 - lvl } else { 0 };
                    last = px;
                    let to_y = ye - 0.25 - lvl as f64 * row;
                    ordinate(d, e(0.0, 0.0), e(x, k), to_y, &c.o(x), x, s);
                }
                d.text(
                    [x0 - 0.1, ye - 0.25 - 0.5 * row],
                    st.small,
                    "MIX BOUNDARY STA",
                    HAlign::Right,
                    VAlign::Middle,
                );
            }
            for g in &m.graphics {
                let (z1, z0) = (dp - g.below_gunwale, dp - g.below_gunwale - g.height);
                d.rect(e(g.x0, z0), e(g.x1, z1), Pen::PHANTOM);
                d.text(
                    e(0.5 * (g.x0 + g.x1), 0.5 * (z0 + z1)),
                    0.8 * st.small,
                    g.name.clone(),
                    HAlign::Center,
                    VAlign::Middle,
                );
            }
            // Static waterline and freeboard.
            let w = &m.waterline;
            let (xa, xb) = (m.keel.first().map_or(0.0, |p| p[0]), m.keel.last().map_or(l, |p| p[0]));
            let (pa, pb) = (e(xa, w.z(xa, l)), e(xb, w.z(xb, l)));
            d.line([pa[0] - 0.2, pa[1]], [pb[0] + 0.2, pb[1]], Pen::PHANTOM);
            d.text(
                [pb[0] + 0.25, pb[1]],
                st.small,
                format!("DWL {} ({:.0} LB)", w.case, w.displacement_lb),
                HAlign::Left,
                VAlign::Middle,
            );
            let (fa, fb) = (e(w.x_min, w.z(w.x_min, l)), e(w.x_min, dp));
            let line = if w.x_min < 0.5 * l { x0 - 0.35 } else { x0 + wl + 0.35 };
            dim_to(d, fa, fb, 1, line, &c.o(w.freeboard_min_in), w.freeboard_min_in, s);
            let (fx, ha) =
                if w.x_min < 0.5 * l { (line - 0.1, HAlign::Right) } else { (line + 0.1, HAlign::Left) };
            d.text([fx, fb[1] + 0.12], st.small, "FREEBOARD", ha, VAlign::Bottom);
            // Length overall, bow datum.
            dim_to(d, e(0.0, dp), e(l, dp), 0, ye + he + row, &c.o(l), l, s);
            d.line(e(0.0, -0.15 * s), e(0.0, dp + 0.2 * s), Pen::CENTER);
            d.text([x0, ye - 0.2], st.small, "STA 0 BOW DATUM", HAlign::Center, VAlign::Top);
            view_title(d, x0 + 0.5 * wl, ye_title, "ELEVATION", Some(s));
            // ---- plan ----
            let mut outline: Vec<P> = m.sheer.iter().map(|p| pl(p[0], p[1])).collect();
            outline.extend(m.sheer.iter().rev().map(|p| pl(p[0], -p[1])));
            outline.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-9 && (a[1] - b[1]).abs() < 1e-9);
            d.polygon(outline, Pen::VISIBLE);
            let mut inner: Vec<P> = m.inner_sheer.iter().map(|p| pl(p[0], p[1])).collect();
            inner.extend(m.inner_sheer.iter().rev().map(|p| pl(p[0], -p[1])));
            d.polygon(inner, Pen::THIN);
            d.line([x0 - 0.25, yp], [x0 + wl + 0.25, yp], Pen::CENTER);
            // Section A-A cutting plane, looking forward.
            let xm = m.mid_x;
            let (top, bot) = (pl(xm, hb), pl(xm, -hb));
            let (t1, b1) = ([top[0], top[1] + 0.3], [bot[0], bot[1] - 0.3]);
            d.line(t1, b1, Pen::CUT);
            for p in [t1, b1] {
                d.line(p, [p[0] - 0.3, p[1]], Pen::VISIBLE);
                d.arrowhead([p[0] - 0.3, p[1]], [-1.0, 0.0]);
                d.text([p[0] - 0.45, p[1]], st.title, "A", HAlign::Right, VAlign::Middle);
            }
            // Detail C bubble on the bow.
            let xc = detail_c_len(m);
            let r = 0.5 * xc / s + 0.1;
            let cc = pl(0.5 * xc, 0.0);
            d.bubble(cc, r, "C", [cc[0] - r - 0.3, cc[1] + r + 0.2]);
            // Beam, station of max beam, tapers.
            let ys = |x: f64| interp(&m.sheer, x);
            dim_to(d, pl(m.x_beam, -hb), pl(m.x_beam, hb), 1, x0 + m.x_beam / s, &c.o(m.beam), m.beam, s);
            let r1 = yp - hb / s - row;
            dim_to(d, pl(0.0, 0.0), pl(m.x_beam, -hb), 0, r1, &c.o(m.x_beam), m.x_beam, s);
            d.text([x0 + wl + 0.3, r1], st.small, "MAX BEAM STA", HAlign::Left, VAlign::Middle);
            let r2 = r1 - row;
            dim_to(
                d,
                pl(0.0, 0.0),
                pl(m.taper_bow, -ys(m.taper_bow)),
                0,
                r2,
                &c.o(m.taper_bow),
                m.taper_bow,
                s,
            );
            let xs0 = l - m.taper_stern;
            dim_to(d, pl(xs0, -ys(xs0)), pl(l, 0.0), 0, r2, &c.o(m.taper_stern), m.taper_stern, s);
            d.text(
                [x0 + wl + 0.3, r2],
                st.small,
                format!("TAPERS (TO {:.0}% BEAM)", 100.0 * m.spec.taper_fraction),
                HAlign::Left,
                VAlign::Middle,
            );
            for (k, g) in m.graphics.iter().enumerate() {
                let rk = r2 - (k + 1) as f64 * row;
                let (a, b) = (pl(g.x0, -ys(g.x0)), pl(g.x1, -ys(g.x1)));
                d.path(
                    (0..=20)
                        .map(|j| {
                            let x = g.x0 + (g.x1 - g.x0) * j as f64 / 20.0;
                            let p = pl(x, -ys(x));
                            [p[0], p[1] - 0.06]
                        })
                        .collect(),
                    Pen::PHANTOM,
                );
                dim_to(d, pl(0.0, 0.0), a, 0, rk, &c.o(g.x0), g.x0, s);
                dim_to(d, a, b, 0, rk, &c.o(g.x1 - g.x0), g.x1 - g.x0, s);
                d.text(
                    [x0 + wl + 0.3, rk],
                    st.small,
                    format!("\"{}\" (H {})", g.name, c.o(g.height)),
                    HAlign::Left,
                    VAlign::Middle,
                );
            }
            view_title(d, x0 + 0.5 * wl, o[1], "PLAN", Some(s));
        }),
    }
}

/// Model length shown in Detail C (bow end).
fn detail_c_len(m: &Model) -> f64 {
    let mut xc = (m.hollow[0] + 6.0).max(24.0);
    if let Some(g) = m.graphics.iter().map(|g| g.x0).fold(None::<f64>, |a, x| Some(a.map_or(x, |a| a.min(x))))
        && g < 48.0
    {
        xc = xc.max(g + 12.0);
    }
    xc.min(0.5 * m.length)
}

fn end_view<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let row = 3.0 * st.text;
    let ymax = m.end_view.iter().map(|p| p[0]).fold(0.0, f64::max);
    let th = title_h(&st);
    let w = 2.0 * ymax / s + 2.0 * row + 0.8;
    let h = th + 0.3 + m.depth / s + 1.5 * row;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let cx = o[0] + 0.4 + ymax / s;
            let y0 = o[1] + th + 0.3;
            let v = |y: f64, z: f64| [cx + y / s, y0 + z / s];
            let mut pts: Vec<P> = m.end_view.iter().map(|p| v(p[0], p[1])).collect();
            pts.extend(m.end_view.iter().rev().filter(|p| p[0] > 1e-9).map(|p| v(-p[0], p[1])));
            d.polygon(pts, Pen::VISIBLE);
            d.line([cx, y0 - 0.15], [cx, y0 + m.depth / s + 0.2], Pen::CENTER);
            let zt = m.end_view.iter().find(|p| p[0] >= ymax - 1e-9).map_or(m.depth, |p| p[1]);
            dim_to(d, v(-ymax, zt), v(ymax, zt), 0, y0 + m.depth / s + row, &c.o(2.0 * ymax), 2.0 * ymax, s);
            let k0 = m.end_view.first().map_or(0.0, |p| p[1]);
            dim_to(
                d,
                v(0.0, k0),
                v(ymax, m.depth),
                1,
                cx + ymax / s + row,
                &c.o(m.depth - k0),
                m.depth - k0,
                s,
            );
            view_title(d, cx, o[1], "END VIEW (FROM BOW)", Some(s));
        }),
    }
}

fn body_plan<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let ymax = m.stations.iter().flat_map(|q| q.half.iter().map(|p| p[0])).fold(0.0, f64::max);
    let th = title_h(&st);
    let lab = text_width("WL 00.00", st.small) + 0.2;
    let w = 2.0 * ymax / s + 2.0 * lab + 0.4;
    let h = th + 0.3 + m.depth / s + 0.6 + 2.0 * st.small;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let cx = o[0] + lab + 0.2 + ymax / s;
            let y0 = o[1] + th + 0.3;
            let v = |y: f64, z: f64| [cx + y / s, y0 + z / s];
            let (yl, yr) = (cx - ymax / s - 0.1, cx + ymax / s + 0.1);
            let pitch = m.waterlines.windows(2).map(|w| w[1] - w[0]).fold(f64::INFINITY, f64::min) / s;
            let every = ((1.3 * 0.8 * st.small / pitch).ceil() as usize).max(1);
            for (k, &z) in m.waterlines.iter().enumerate() {
                d.line([yl, y0 + z / s], [yr, y0 + z / s], Pen::DIM);
                if (k + 1).is_multiple_of(every) {
                    d.text(
                        [yr + 0.05, y0 + z / s],
                        0.8 * st.small,
                        format!("WL {}", c.o(z)),
                        HAlign::Left,
                        VAlign::Middle,
                    );
                }
            }
            let step = m.spec.buttock_step_in;
            let bl_every =
                ((1.2 * text_width("BL 00.00", 0.8 * st.small) / (step / s)).ceil() as usize).max(1);
            let (mut b, mut k) = (step, 1usize);
            while b < ymax - 1e-9 {
                for sg in [-1.0, 1.0] {
                    d.line(v(sg * b, 0.0), v(sg * b, m.depth), Pen::DIM);
                }
                if k.is_multiple_of(bl_every) {
                    d.text(
                        v(b, m.depth + 0.1 * s),
                        0.8 * st.small,
                        format!("BL {}", c.o(b)),
                        HAlign::Center,
                        VAlign::Bottom,
                    );
                }
                b += step;
                k += 1;
            }
            d.line([cx, y0 - 0.15], [cx, y0 + m.depth / s + 0.3], Pen::CENTER);
            let n = m.stations.len();
            for q in &m.stations {
                if q.keel.is_none() {
                    continue;
                }
                let fwd = q.x <= 0.5 * m.length + 1e-9;
                let aft = q.x >= 0.5 * m.length - 1e-9;
                for (on, sg) in [(fwd, 1.0), (aft, -1.0)] {
                    if !on {
                        continue;
                    }
                    d.path(q.half.iter().map(|p| v(sg * p[0], p[1])).collect(), Pen::THIN);
                }
            }
            d.text(
                [cx, o[1] + th + 0.05],
                0.8 * st.small,
                format!(
                    "STATIONS 0-{} EVERY {} (OFFSETS TABLE); FWD RIGHT, AFT LEFT",
                    n.saturating_sub(1),
                    c.o(m.length / (n.max(2) - 1) as f64)
                ),
                HAlign::Center,
                VAlign::Bottom,
            );
            view_title(d, cx, o[1], "BODY PLAN", Some(s));
        }),
    }
}

/// Isometric projection from bow-starboard-above: screen right (1, 1, 0)/sqrt 2, up
/// (-1, 1, 2)/sqrt 6, toward the viewer (1, -1, 1)/sqrt 3.
fn iso_project(p: [f64; 3]) -> (P, f64) {
    let (r2, r6, r3) = (2f64.sqrt(), 6f64.sqrt(), 3f64.sqrt());
    ([(p[0] + p[1]) / r2, (-p[0] + p[1] + 2.0 * p[2]) / r6], (p[0] - p[1] + p[2]) / r3)
}

fn iso_view<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let pts: Vec<P> = m.iso.iter().flat_map(|(t, _)| t.iter().map(|&p| iso_project(p).0)).collect();
    let (lo, hi) = pts.iter().fold(([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]), |(lo, hi), p| {
        ([lo[0].min(p[0]), lo[1].min(p[1])], [hi[0].max(p[0]), hi[1].max(p[1])])
    });
    let th = title_h(&st);
    let legend = (m.mixes.len() as f64) * 1.8 * st.small + 0.2;
    let w = (hi[0] - lo[0]) / s + 0.4;
    let h = th + legend + (hi[1] - lo[1]) / s + 0.3;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let base = [o[0] + 0.2, o[1] + th + legend + 0.1];
            let q = |p: P| [base[0] + (p[0] - lo[0]) / s, base[1] + (p[1] - lo[1]) / s];
            let light = {
                let v: [f64; 3] = [0.2, -0.5, 1.0];
                let l = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
                [v[0] / l, v[1] / l, v[2] / l]
            };
            let view = [1.0 / 3f64.sqrt(), -1.0 / 3f64.sqrt(), 1.0 / 3f64.sqrt()];
            let mut tris: Vec<(f64, usize)> = m
                .iso
                .iter()
                .enumerate()
                .map(|(i, (t, _))| {
                    ((iso_project(t[0]).1 + iso_project(t[1]).1 + iso_project(t[2]).1) / 3.0, i)
                })
                .collect();
            tris.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
            for (_, i) in tris {
                let (t, dense) = &m.iso[i];
                let (u, v) = (
                    [t[1][0] - t[0][0], t[1][1] - t[0][1], t[1][2] - t[0][2]],
                    [t[2][0] - t[0][0], t[2][1] - t[0][1], t[2][2] - t[0][2]],
                );
                let mut n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
                let ln = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
                if ln < 1e-12 {
                    continue;
                }
                n = [n[0] / ln, n[1] / ln, n[2] / ln];
                if n[0] * view[0] + n[1] * view[1] + n[2] * view[2] < 0.0 {
                    n = [-n[0], -n[1], -n[2]];
                }
                let k = 0.35 + 0.65 * (n[0] * light[0] + n[1] * light[1] + n[2] * light[2]).max(0.0);
                let col = if *dense { [0.55, 0.70, 0.95] } else { [0.88, 0.88, 0.86] };
                d.fill(
                    t.iter().map(|&p| q(iso_project(p).0)).collect(),
                    [col[0] * k, col[1] * k, col[2] * k],
                    crate::drafting::Layer::Shade,
                );
            }
            for (k, mx) in m.mixes.iter().enumerate() {
                let y = o[1] + th + 0.1 + k as f64 * 1.8 * st.small;
                let col = if k == 0 { [0.88, 0.88, 0.86] } else { [0.55, 0.70, 0.95] };
                let sw = 1.2 * st.small;
                d.fill(
                    vec![
                        [o[0] + 0.2, y],
                        [o[0] + 0.2 + sw, y],
                        [o[0] + 0.2 + sw, y + sw],
                        [o[0] + 0.2, y + sw],
                    ],
                    col,
                    crate::drafting::Layer::Shade,
                );
                d.rect([o[0] + 0.2, y], [o[0] + 0.2 + sw, y + sw], Pen::DIM);
                d.text(
                    [o[0] + 0.3 + sw, y],
                    st.small,
                    format!("{} {}", mx.part, mx.label()),
                    HAlign::Left,
                    VAlign::Bottom,
                );
            }
            view_title(d, o[0] + 0.5 * w, o[1], "ISOMETRIC VIEW, DO NOT SCALE", None);
        }),
    }
}

/// The wall section polygon of half girths `outer` and `inner` (y, z), sheer to keel.
fn wall_polygon(outer: &[P], inner: &[P]) -> Vec<P> {
    let mut p: Vec<P> = outer.to_vec();
    p.extend(outer.iter().rev().skip(1).map(|q| [-q[0], q[1]]));
    p.extend(inner.iter().map(|q| [-q[0], q[1]]));
    p.extend(inner.iter().rev().skip(1).copied());
    p
}

fn section_aa<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let row = 3.0 * st.text;
    let ymax = m.mid_outer.iter().map(|p| p[0]).fold(0.0, f64::max);
    let th = title_h(&st);
    let lab = text_width("INSIDE SURFACE (MOLD FACE)", st.small) + 0.3;
    let w = 2.0 * ymax / s + lab + row + 1.0;
    let h = th + 0.5 + m.depth / s + 1.6 * row;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let cx = o[0] + lab + ymax / s;
            let y0 = o[1] + th + 0.5;
            let v = |p: P| [cx + p[0] / s, y0 + p[1] / s];
            let poly: Vec<P> = wall_polygon(&m.mid_outer, &m.mid_inner).iter().map(|&p| v(p)).collect();
            let wall_paper = m.t / s;
            d.hatch(&poly, (wall_paper / 2.5).clamp(0.01, 0.08), 45.0);
            let full = |half: &[P]| {
                let mut f: Vec<P> = half.iter().map(|&p| v(p)).collect();
                f.extend(half.iter().rev().skip(1).map(|p| v([-p[0], p[1]])));
                f
            };
            d.path(full(&m.mid_outer), Pen::THIN);
            d.path(full(&m.mid_inner), Pen::THIN);
            d.line([cx, y0 - 0.2], [cx, y0 + m.depth / s + 0.25], Pen::CENTER);
            let at = |half: &[P], z: f64| {
                half.iter()
                    .min_by(|a, b| (a[1] - z).abs().total_cmp(&(b[1] - z).abs()))
                    .copied()
                    .unwrap_or([0.0, z])
            };
            let pw = at(&m.mid_outer, 0.55 * m.depth);
            d.leader(
                [cx + ymax / s + 0.45, y0 + 0.75 * m.depth / s],
                v(pw),
                &format!("WALL {} TYP", c.l(m.t)),
            );
            let pi = at(&m.mid_inner, 0.45 * m.depth);
            let face = match m.laminate.mold {
                Mold::Male => "INSIDE SURFACE (MOLD FACE)",
                Mold::Female => "INSIDE SURFACE",
            };
            d.leader(
                [o[0] + 0.1 + text_width(face, st.small), y0 + 0.2 * m.depth / s],
                v([-pi[0], pi[1]]),
                face,
            );
            let pb = at(&m.mid_outer, 0.3 * m.depth);
            let cb = v(pb);
            d.bubble(cb, 0.18, "B", [cb[0] + 0.55, cb[1] - 0.35]);
            let mix_txt: Vec<String> = match &m.zones {
                Some(zl) => {
                    let dense: Vec<&crate::section::Rect> =
                        zl.dense.iter().filter(|r| r.has_x(m.mid_x)).collect();
                    for r in &dense {
                        d.line(
                            v([ymax + 0.15 * s, r.z0_in]),
                            v([ymax + 0.15 * s, r.z1_in.min(m.depth)]),
                            Pen::PHANTOM,
                        );
                    }
                    vec![if dense.is_empty() {
                        "C-01".into()
                    } else {
                        "C-01, C-02 AT PHANTOM BRACKETS".into()
                    }]
                }
                None => vec!["C-01".into()],
            };
            d.text(
                [cx, y0 - 0.25],
                st.small,
                format!("CONCRETE {}", mix_txt.join(" ")),
                HAlign::Center,
                VAlign::Top,
            );
            let top = m.mid_outer.first().copied().unwrap_or([ymax, m.depth]);
            dim_to(
                d,
                v([-top[0], top[1]]),
                v(top),
                0,
                y0 + m.depth / s + row,
                &c.o(2.0 * top[0]),
                2.0 * top[0],
                s,
            );
            let k0 = m.mid_outer.last().map_or(0.0, |p| p[1]);
            dim_to(d, v([0.0, k0]), v(top), 1, cx + ymax / s + row, &c.o(top[1] - k0), top[1] - k0, s);
            view_title(d, cx, o[1], &format!("SECTION A-A (STA {})", c.o(m.mid_x)), Some(s));
        }),
    }
}

fn detail_b<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let lam = &m.laminate;
    let strip = 6.0;
    let (lp, tp) = (strip / s, lam.t / s);
    let mix = if m.zones.is_some() { "C-01 OR C-02 PER ZONE".to_string() } else { "C-01".to_string() };
    let mut labels: Vec<(f64, String)> = vec![];
    for i in 0..lam.lifts {
        let z0 = f64::from(i) * lam.lift_in();
        let from_inside = match lam.mold {
            Mold::Male => z0 + 0.5 * lam.lift_in(),
            Mold::Female => lam.t - z0 - 0.5 * lam.lift_in(),
        };
        labels.push((from_inside, format!("LIFT {} ({}), {mix}", i + 1, c.l(lam.lift_in()))));
    }
    for &(k, cin, cout) in &lam.meshes {
        labels.push((cin, format!("M-01 MESH AFTER LIFT {k}: COVER {} IN / {} OUT", c.l(cin), c.l(cout))));
    }
    labels.sort_by(|a, b| a.0.total_cmp(&b.0));
    let lw = labels.iter().map(|l| text_width(&l.1, st.small)).fold(0.0, f64::max);
    let th = title_h(&st);
    let pitch = 1.7 * st.small;
    let stack_h = (labels.len() as f64 * pitch).max(tp) + 0.2;
    let dims = 1.2;
    let w = dims + lp + 0.6 + lw + 0.3;
    let h = th + 0.5 + stack_h + 0.5 + 2.0 * st.small;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let x0 = o[0] + dims;
            let ymid = o[1] + th + 0.5 + 0.5 * stack_h;
            let yb = ymid - 0.5 * tp;
            let z = |zi: f64| yb + zi / s;
            for i in 0..lam.lifts {
                let z0 = f64::from(i) * lam.lift_in();
                let (a, b) = (z(z0), z(z0 + lam.lift_in()));
                let r = [[x0, a], [x0 + lp, a], [x0 + lp, b], [x0, b]];
                d.hatch(
                    &r,
                    (lam.lift_in() / s / 2.5).clamp(0.008, 0.05),
                    if i % 2 == 0 { 45.0 } else { 135.0 },
                );
                if i > 0 {
                    d.line([x0, a], [x0 + lp, a], Pen::THIN);
                }
            }
            d.line([x0, z(0.0)], [x0 + lp, z(0.0)], Pen::VISIBLE);
            d.line([x0, z(lam.t)], [x0 + lp, z(lam.t)], Pen::VISIBLE);
            d.line([x0, z(0.0)], [x0, z(lam.t)], Pen::THIN);
            d.line([x0 + lp, z(0.0)], [x0 + lp, z(lam.t)], Pen::THIN);
            for &(_, cin, _) in &lam.meshes {
                let y = z(cin);
                d.line([x0, y], [x0 + lp, y], Pen::HIDDEN);
                let mut x = x0 + 0.04;
                while x < x0 + lp {
                    d.dot([x, y], 0.012);
                    x += 0.08;
                }
            }
            let ly0 = ymid - 0.5 * (labels.len() as f64 - 1.0) * pitch;
            for (k, (zi, t)) in labels.iter().enumerate() {
                d.leader(
                    [x0 + lp + 0.6, ly0 + k as f64 * pitch],
                    [x0 + 0.85 * lp - 0.05 * k as f64, z(*zi)],
                    t,
                );
            }
            d.text(
                [x0 + 0.5 * lp, z(lam.t) + 0.12],
                st.small,
                "OUTSIDE FACE",
                HAlign::Center,
                VAlign::Bottom,
            );
            let inside = match lam.mold {
                Mold::Male => "INSIDE FACE (CAST ON THE MALE MOLD)",
                Mold::Female => "INSIDE FACE (LAST LIFT)",
            };
            d.text([x0 + 0.5 * lp, z(0.0) - 0.12], st.small, inside, HAlign::Center, VAlign::Top);
            dim_to(d, [x0, z(0.0)], [x0, z(lam.t)], 1, x0 - 0.3, &c.l(lam.t), lam.t, s);
            d.text(
                [o[0] + 0.5 * w, o[1] + th + 0.1],
                0.85 * st.small,
                format!("PUNCHING d = {} (KNEE FACE TO FARTHEST MESH)", c.l(lam.d_in)),
                HAlign::Center,
                VAlign::Bottom,
            );
            view_title(d, o[0] + 0.5 * w, o[1], "DETAIL B (WALL BUILDUP)", Some(s));
        }),
    }
}

fn detail_c<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let row = 3.0 * st.text;
    let xc = detail_c_len(m);
    let th = title_h(&st);
    let lab = 0.4 + text_width("R0.00 STEM RADIUS", st.small);
    let w = lab + xc / s + 1.2;
    let h = th + 0.6 + m.depth / s + 2.4 * row;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let x0 = o[0] + lab;
            let y0 = o[1] + th + 0.6;
            let e = |x: f64, z: f64| [x0 + x / s, y0 + z / s];
            let poly = clip_x(&m.profile, xc);
            d.polygon(poly.iter().map(|p| e(p[0], p[1])).collect(), Pen::VISIBLE);
            let ik: Vec<P> = m.inner_keel.iter().filter(|p| p[0] <= xc).map(|p| e(p[0], p[1])).collect();
            d.path(ik, Pen::HIDDEN);
            let xa = m.hollow[0];
            if xa < xc {
                d.line(e(xa, interp(&m.inner_keel, xa)), e(xa, m.depth), Pen::HIDDEN);
                dim_to(d, e(0.0, m.depth), e(xa, m.depth), 0, y0 + m.depth / s + row, &c.o(xa), xa, s);
                d.text(
                    e(0.5 * xa, m.depth + 1.6 * row * s),
                    st.small,
                    "SOLID END",
                    HAlign::Center,
                    VAlign::Bottom,
                );
            }
            d.text(
                [x0 + xc / s + 0.1, y0 + 0.5 * m.depth / s],
                st.small,
                "BREAK",
                HAlign::Left,
                VAlign::Middle,
            );
            let tip = m.keel.first().copied().unwrap_or([0.0, 0.0]);
            let txt = match m.stem_radius_in {
                Some(r) => format!("R{} STEM RADIUS", c.o(r)),
                None => "SHARP STEM".into(),
            };
            d.leader([o[0] + 0.1 + text_width(&txt, st.small), y0 - 0.25], e(tip[0], tip[1]), &txt);
            let kx = (xa + 2.0).min(xc - 1.0).max(1.0);
            d.leader(
                [x0 + kx / s + 0.6, y0 - 0.3],
                e(kx, interp(&m.keel, kx)),
                &format!("WALL {}", c.l(m.t)),
            );
            for g in m.graphics.iter().filter(|g| g.x0 < xc) {
                let (z1, z0) = (m.depth - g.below_gunwale, m.depth - g.below_gunwale - g.height);
                let x1 = g.x1.min(xc);
                d.rect(e(g.x0, z0), e(x1, z1), Pen::PHANTOM);
                dim_to(d, e(0.0, m.depth), e(g.x0, z1), 0, y0 + m.depth / s + 2.0 * row, &c.o(g.x0), g.x0, s);
                dim_to(d, e(g.x0, z0), e(g.x0, z1), 1, x0 + g.x0 / s - 0.3, &c.o(g.height), g.height, s);
                if g.below_gunwale > 1e-9 {
                    dim_to(
                        d,
                        e(x1, z1),
                        e(x1, m.depth),
                        1,
                        x0 + x1 / s + 0.3,
                        &c.o(g.below_gunwale),
                        g.below_gunwale,
                        s,
                    );
                }
                d.text(
                    e(0.5 * (g.x0 + x1), 0.5 * (z0 + z1)),
                    0.8 * st.small,
                    g.name.clone(),
                    HAlign::Center,
                    VAlign::Middle,
                );
            }
            d.line(e(0.0, -0.1 * s), e(0.0, m.depth + 0.1 * s), Pen::CENTER);
            view_title(d, o[0] + 0.5 * w, o[1], "DETAIL C (BOW END)", Some(s));
        }),
    }
}

fn offsets_table(c: &Ctx, g: f64) -> Table {
    let m = c.m;
    let h = c.st.small * g;
    let mut head = vec!["STA".to_string(), "X".into(), "KEEL Z".into()];
    head.extend(m.waterlines.iter().map(|z| format!("WL {}", c.o(*z))));
    head.push(format!("GUNWALE {}", c.o(m.depth)));
    let mut rows = vec![head];
    for (i, q) in m.stations.iter().enumerate() {
        let f = |v: Option<f64>| v.map_or("-".to_string(), |v| c.o(v));
        let mut r = vec![i.to_string(), c.o(q.x), f(q.keel)];
        r.extend(m.waterlines.iter().map(|&z| f(q.breadth(z))));
        r.push(f(q.breadth(m.depth)));
        rows.push(r);
    }
    let wcol =
        |k: usize| rows.iter().map(|r| r.get(k).map_or(0.0, |s| text_width(s, h))).fold(0.0, f64::max) + 0.12;
    let cols: Vec<f64> = (0..rows.first().map_or(0, Vec::len)).map(wcol).collect();
    let n = m.stations.len().max(2) - 1;
    Table::new(
        cols,
        rows,
        h,
        1,
        Some(format!(
            "OFFSETS: HALF-BREADTHS FROM CL AT WATERLINE HEIGHTS ABOVE THE KEEL; STATION SPACING {}",
            c.o(m.length / n as f64)
        )),
    )
}

fn stands_view<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let row = 3.0 * st.text;
    let th = title_h(&st);
    let w = m.length / s + 1.2;
    let h = th + 2.0 * row + 0.45 + m.depth / s + 0.3 + 2.0 * st.small;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let Some(sd) = &m.stands else { return };
            let x0 = o[0] + 0.6;
            let y0 = o[1] + th + 2.0 * row + 0.45;
            let flip = |z: f64| if sd.inverted { m.depth - z } else { z };
            let e = |x: f64, z: f64| [x0 + x / s, y0 + flip(z) / s];
            d.polygon(m.profile.iter().map(|p| e(p[0], p[1])).collect(), Pen::VISIBLE);
            let xs = [sd.x_in, m.length - sd.x_in];
            for &x in &xs {
                let z = if sd.inverted { m.depth } else { interp(&m.keel, x) };
                let apex = e(x, z);
                let tri = vec![apex, [apex[0] - 0.12, y0 - 0.3], [apex[0] + 0.12, y0 - 0.3]];
                d.polygon(tri, Pen::VISIBLE);
                d.line(
                    [apex[0] - 0.5 * sd.pad_in / s, apex[1]],
                    [apex[0] + 0.5 * sd.pad_in / s, apex[1]],
                    Pen::VISIBLE,
                );
            }
            d.line([x0 - 0.3, y0 - 0.3], [x0 + m.length / s + 0.3, y0 - 0.3], Pen::THIN);
            let at = y0 - 0.3 - row;
            let ground = |x: f64| [x0 + x / s, y0 - 0.3];
            dim_to(d, e(0.0, m.depth), ground(xs[0]), 0, at, &c.o(xs[0]), xs[0], s);
            dim_to(d, ground(xs[0]), ground(xs[1]), 0, at - row, &c.o(sd.spacing_in), sd.spacing_in, s);
            d.text(
                [x0 + 0.5 * m.length / s, y0 + m.depth / s + 0.1],
                0.85 * st.small,
                format!(
                    "TWO STANDS, {} PADS, {}; STANDS {} FROM EACH END",
                    c.o(sd.pad_in),
                    if sd.inverted { "INVERTED ON THE GUNWALES" } else { "UPRIGHT UNDER THE KEEL" },
                    c.o(sd.x_in)
                ),
                HAlign::Center,
                VAlign::Bottom,
            );
            view_title(d, o[0] + 0.5 * w, o[1], "SUPPORT / STANDS DIAGRAM", Some(s));
        }),
    }
}

// ---------- sheet 2: the mold ----------

fn mold_sheet(c: &Ctx, stamp: &str) -> Result<Doc> {
    let m = c.m;
    let mo = &m.mold;
    let notes = vec![
        "ALL DIMENSIONS IN INCHES UNLESS OTHERWISE NOTED.".to_string(),
        "MALE MOLD BUILT UPSIDE DOWN: GUNWALE PLANE ON THE SHOP FLOOR (DATUM A), LAYER L1 ON THE FLOOR, NUMBERED UP TO THE KEEL.".into(),
        format!(
            "{} BOARDS x {} = {} = MOLD DEPTH {} LESS WALL {}; THE MOLD TOP IS THE INSIDE OF THE KEEL.",
            mo.layers.len(),
            c.l(mo.board_in),
            c.l(mo.height_in),
            c.l(m.depth),
            c.l(m.t)
        ),
        "LAYER OUTLINES ARE THE TRACING TEMPLATES (WHAT THE LAYER COVERS IN PLAN): CUT TO THE OUTLINE, SAND TO THE SANDING LINE (PHANTOM, THE LAYER'S TOP FACE).".into(),
        format!(
            "ALIGNMENT: CENTERLINE (DATUM B) AND STATION TICKS FROM THE BOW DATUM (DATUM C) ON EVERY LAYER; DIA {} DOWEL HOLES ON CL AT THE {} IN STATIONS.",
            c.l(mo.hole_in),
            TICK_IN
        ),
        format!(
            "NESTING: {} x {} SHEETS, KERF {} BETWEEN PIECES, EDGE MARGIN {}.",
            c.o(mo.sheet_in[0]),
            c.o(mo.sheet_in[1]),
            c.l(mo.kerf_in),
            c.l(mo.edge_in)
        ),
    ];
    let make = |f: f64, g: f64| -> (Vec<Block>, f64) {
        let s = snap(25.0 * f);
        let b = vec![
            stack_view(c, snap(20.0 * f)),
            layer_profiles(c, s),
            nesting_view(c, snap(24.0 * f)),
            table_block(layer_table(c, g)),
            alignment_detail(c, snap(4.0 * f)),
        ];
        (b, s)
    };
    compose(c, stamp, "MOLD DRAWING", 2, &m.spec.mold_revisions, &m.mold_bom, &notes, &make)
}

fn stack_view<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let mo = &m.mold;
    let row = 3.0 * st.text;
    let [xa, xb] = m.hollow;
    let th = title_h(&st);
    let lab = text_width("L00", st.text) + 0.4;
    let right = row + text_width("00.000", st.small) + text_width("00.000", st.text) + 0.8;
    let w = lab + (xb - xa) / s + right;
    let h = th + 0.6 + mo.height_in / s + 1.5 * row;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let x0 = o[0] + lab;
            let y0 = o[1] + th + 0.6;
            let e = |x: f64, hh: f64| [x0 + (x - xa) / s, y0 + hh / s];
            let every = ((1.3 * st.small / (mo.board_in / s)).ceil() as usize).max(1);
            let n = mo.layers.len();
            let shown = |k: usize| (n - 1 - k).is_multiple_of(every);
            for (k, l) in mo.layers.iter().enumerate() {
                d.rect(e(l.x0, l.h0), e(l.x1, l.h1), Pen::THIN);
                if shown(k) {
                    d.text(
                        [x0 - 0.15, e(xa, 0.5 * (l.h0 + l.h1))[1]],
                        st.small,
                        l.label.clone(),
                        HAlign::Right,
                        VAlign::Middle,
                    );
                }
            }
            let mut prof: Vec<P> = vec![e(xa, 0.0), e(xb, 0.0)];
            prof.extend(mo.top.iter().rev().map(|p| e(p[0], p[1])));
            d.polygon(prof, Pen::VISIBLE);
            for &j in &mo.joints {
                d.line(e(j, 0.0), e(j, mo.height_in), Pen::PHANTOM);
            }
            d.line([x0 - 0.3, y0], [x0 + (xb - xa) / s + 0.3, y0], Pen::VISIBLE);
            d.text(
                [x0 + 0.5 * (xb - xa) / s, y0 - 0.12],
                st.small,
                "DATUM A: SHOP FLOOR = GUNWALE PLANE",
                HAlign::Center,
                VAlign::Top,
            );
            let xr = x0 + (xb - xa) / s;
            for (k, l) in mo.layers.iter().enumerate() {
                if shown(k) {
                    ordinate_v(d, [xr, y0], e(xb, l.h1), xr + row, &c.l(l.h1), l.h1, s);
                }
            }
            dim_to(
                d,
                e(xb, 0.0),
                e(xb, mo.height_in),
                1,
                xr + row + text_width("00.000", st.small) + 0.35,
                &c.l(mo.height_in),
                mo.height_in,
                s,
            );
            dim_to(
                d,
                e(xa, mo.height_in),
                e(xb, mo.height_in),
                0,
                y0 + mo.height_in / s + row,
                &c.o(xb - xa),
                xb - xa,
                s,
            );
            view_title(d, o[0] + 0.5 * w, o[1], "LAYER STACK ELEVATION", Some(s));
        }),
    }
}

/// Vertical ordinate: from a height on the view out to a column of cumulative heights.
fn ordinate_v(d: &mut Doc, datum: P, from: P, to_x: f64, text: &str, model: f64, s: f64) {
    let st = d.style;
    d.items.push(Item::DimBegin(DimTag {
        text: text.into(),
        model,
        scale: s,
        a: [to_x, datum[1]],
        b: [to_x, from[1]],
        axis: 1,
    }));
    d.line([from[0] + st.ext_gap, from[1]], [to_x, from[1]], Pen::DIM);
    d.text([to_x + 0.05, from[1]], st.small, text, HAlign::Left, VAlign::Middle);
    d.items.push(Item::DimEnd);
}

fn layer_profiles<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let mo = &m.mold;
    let row = 3.0 * st.text;
    let th = title_h(&st);
    let xmax = mo.layers.iter().map(|l| l.x1).fold(0.0, f64::max);
    let lab = text_width("L00", st.text) + 0.4;
    let right = row + text_width("00.00", st.text) + 0.4;
    let band = move |l: &crate::drawing::MoldLayer| l.max_width / s + row + 2.0 * st.small + 0.15;
    let w = lab + xmax / s + right;
    let h = th + 0.2 + mo.layers.iter().map(band).sum::<f64>();
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let x0 = o[0] + lab;
            let mut y = o[1] + th + 0.2 + 1.2 * st.small;
            for l in &mo.layers {
                let yc = y + 0.5 * l.max_width / s;
                let p = |q: P| [x0 + q[0] / s, yc + q[1] / s];
                for piece in &l.pieces {
                    d.polygon(piece.iter().map(|&q| p(q)).collect(), Pen::VISIBLE);
                }
                for [a, b] in &l.sanding {
                    d.line(p(*a), p(*b), Pen::PHANTOM);
                }
                for &hpos in &l.holes {
                    d.circle(p(hpos), (0.5 * mo.hole_in / s).max(0.012), Pen::THIN);
                }
                d.line([x0 - 0.1, yc], [x0 + l.x1 / s + 0.2, yc], Pen::CENTER);
                d.line([x0, yc - 0.12], [x0, yc + 0.12], Pen::CENTER);
                let mut t = TICK_IN;
                while t < l.x1 {
                    if t > l.x0 {
                        d.line([x0 + t / s, yc - 0.05], [x0 + t / s, yc + 0.05], Pen::THIN);
                    }
                    t += TICK_IN;
                }
                d.text([x0 - 0.15, yc], st.text, l.label.clone(), HAlign::Right, VAlign::Middle);
                let top = yc + 0.5 * l.max_width / s;
                dim_to(
                    d,
                    p([l.x0, 0.0]),
                    p([l.x1, 0.0]),
                    0,
                    top + 0.55 * row,
                    &c.o(l.x1 - l.x0),
                    l.x1 - l.x0,
                    s,
                );
                d.text(
                    [x0 + l.x0 / s - 0.1, top + 0.55 * row],
                    0.8 * st.small,
                    format!("STA {}", c.o(l.x0)),
                    HAlign::Right,
                    VAlign::Middle,
                );
                let xw = x0 + l.x1 / s;
                dim_to(
                    d,
                    [xw, yc - 0.5 * l.max_width / s],
                    [xw, yc + 0.5 * l.max_width / s],
                    1,
                    xw + 0.5 * row,
                    &c.o(l.max_width),
                    l.max_width,
                    s,
                );
                y += band(l);
            }
            d.text(
                [x0, o[1] + th + 0.05],
                0.8 * st.small,
                "STA 0 = BOW DATUM (DATUM C), TICKS EVERY 12 IN, CL = DATUM B",
                HAlign::Left,
                VAlign::Bottom,
            );
            view_title(d, o[0] + 0.5 * w, o[1], "LAYER PROFILES (TRACING TEMPLATES)", Some(s));
        }),
    }
}

fn nesting_view<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let mo = &m.mold;
    let [sl, sw] = mo.sheet_in;
    let n = mo.sheets.len().max(1);
    let cols = ((n as f64 * 2.0).sqrt().ceil() as usize).clamp(1, n);
    let rows = n.div_ceil(cols);
    let (cw, ch) = (sl / s + 0.3, sw / s + 2.5 * st.small + 0.2);
    let th = title_h(&st);
    let w = cols as f64 * cw + 0.1;
    let h = th + rows as f64 * ch + 0.1;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            for (k, sh) in mo.sheets.iter().enumerate() {
                let (col, r) = (k % cols, k / cols);
                let x0 = o[0] + 0.1 + col as f64 * cw;
                let y0 = o[1] + th + (rows - 1 - r) as f64 * ch + 2.2 * st.small;
                let p = |q: P| [x0 + q[0] / s, y0 + q[1] / s];
                d.rect(p([0.0, 0.0]), p([sl, sw]), Pen::VISIBLE);
                d.rect(p([mo.edge_in, mo.edge_in]), p([sl - mo.edge_in, sw - mo.edge_in]), Pen::PHANTOM);
                for (label, outline) in &sh.parts {
                    d.polygon(outline.iter().map(|&q| p(q)).collect(), Pen::THIN);
                    let (lo, hi) =
                        outline.iter().fold(([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]), |(lo, hi), q| {
                            ([lo[0].min(q[0]), lo[1].min(q[1])], [hi[0].max(q[0]), hi[1].max(q[1])])
                        });
                    d.text(
                        p([0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1])]),
                        0.7 * st.small,
                        label.clone(),
                        HAlign::Center,
                        VAlign::Middle,
                    );
                }
                d.text(
                    [x0, y0 - 0.06],
                    0.8 * st.small,
                    format!(
                        "SHEET {} OF {}, UTILIZATION {:.1}%",
                        k + 1,
                        mo.sheets.len(),
                        100.0 * sh.used_in2 / (sl * sw)
                    ),
                    HAlign::Left,
                    VAlign::Top,
                );
            }
            view_title(
                d,
                o[0] + 0.5 * w,
                o[1],
                &format!("NESTING / CUT SHEETS ({} SHEETS {} x {})", mo.sheets.len(), c.o(sl), c.o(sw)),
                Some(s),
            );
        }),
    }
}

fn layer_table(c: &Ctx, g: f64) -> Table {
    let mo = &c.m.mold;
    let h = c.st.small * g;
    let mut rows = vec![vec![
        "LAYER".to_string(),
        "BOARD".into(),
        "CUM. HEIGHT".into(),
        "STATIONS".into(),
        "MAX WIDTH".into(),
        "PIECES".into(),
    ]];
    for l in &mo.layers {
        rows.push(vec![
            l.label.clone(),
            c.l(l.h1 - l.h0),
            c.l(l.h1),
            format!("{} - {}", c.o(l.x0), c.o(l.x1)),
            c.o(l.max_width),
            l.piece_labels.join(" "),
        ]);
    }
    let wcol =
        |k: usize| rows.iter().map(|r| r.get(k).map_or(0.0, |s| text_width(s, h))).fold(0.0, f64::max) + 0.12;
    let cols: Vec<f64> = (0..6).map(wcol).map(|w| w.min(3.0)).collect();
    Table::new(cols, rows, h, 1, Some("LAYER SCHEDULE (L1 ON THE FLOOR)".into()))
}

fn alignment_detail<'a>(c: &'a Ctx<'a>, s: f64) -> Block<'a> {
    let (m, st) = (c.m, c.st);
    let mo = &m.mold;
    let b = mo.board_in;
    let n = mo.layers.len().min(3);
    let span = 2.0 * TICK_IN;
    let th = title_h(&st);
    let lab = text_width("DIA 0.000 DOWEL HOLES ON CL AT EVERY 12 IN STATION, ALL LAYERS", st.small) + 0.4;
    let w = span / s + 1.6 + lab.min(4.0);
    let h = th + 0.7 + n as f64 * b / s + 1.4;
    Block {
        w,
        h,
        draw: Box::new(move |d, o| {
            let x0 = o[0] + 0.9;
            let y0 = o[1] + th + 0.7;
            let e = |x: f64, z: f64| [x0 + x / s, y0 + z / s];
            for k in 0..n {
                let (z0, z1) = (k as f64 * b, (k + 1) as f64 * b);
                d.rect(e(0.0, z0), e(span, z1), Pen::VISIBLE);
                d.text(
                    [x0 - 0.1, e(0.0, 0.5 * (z0 + z1))[1]],
                    st.small,
                    format!("L{}", k + 1),
                    HAlign::Right,
                    VAlign::Middle,
                );
            }
            let top = n as f64 * b;
            let r = 0.5 * mo.hole_in;
            for x in [0.5 * TICK_IN, 1.5 * TICK_IN] {
                d.line(e(x - r, 0.0), e(x - r, top), Pen::THIN);
                d.line(e(x + r, 0.0), e(x + r, top), Pen::THIN);
                d.line(e(x, -0.3 * s), e(x, top + 0.3 * s), Pen::CENTER);
            }
            d.line([x0 - 0.4, y0], [x0 + span / s + 0.4, y0], Pen::VISIBLE);
            d.text(
                [x0 + 0.5 * span / s, y0 - 0.1],
                st.small,
                "DATUM A: SHOP FLOOR (GUNWALE PLANE)",
                HAlign::Center,
                VAlign::Top,
            );
            dim_to(
                d,
                e(0.5 * TICK_IN, top),
                e(1.5 * TICK_IN, top),
                0,
                y0 + top / s + 0.5,
                &c.o(TICK_IN),
                TICK_IN,
                s,
            );
            dim_to(d, e(span, 0.0), e(span, b), 1, x0 + span / s + 0.35, &c.l(b), b, s);
            d.leader(
                [x0 + span / s + 0.6, y0 + top / s + 0.35],
                e(1.5 * TICK_IN + r, 0.5 * top),
                &format!(
                    "DIA {} DOWEL HOLES ON CL\nAT EVERY {} IN STATION, ALL LAYERS",
                    c.l(mo.hole_in),
                    TICK_IN
                ),
            );
            d.text(
                [x0, o[1] + th + 0.1],
                0.8 * st.small,
                "SECTION ON CL (DATUM B); STATIONS FROM THE BOW DATUM (DATUM C). STACK EACH LAYER ON THE DOWELS, CL AND TICKS LINED UP.",
                HAlign::Left,
                VAlign::Bottom,
            );
            view_title(d, o[0] + 0.5 * w, o[1], "ALIGNMENT DETAIL", Some(s));
        }),
    }
}

/// Every dimension of a rendered sheet with its measured paper length (along its axis) times
/// its scale, and its model value: (text, measured, model).
pub fn measured_dims(doc: &Doc) -> Vec<(String, f64, f64)> {
    doc.dims().into_iter().map(|t| (t.text.clone(), t.paper() * t.scale, t.model)).collect()
}

/// Write both sheets (PDF, SVG, DXF) into `dir`; returns the file names.
pub fn write(dw: &Drawings, dir: &std::path::Path) -> Result<Vec<String>> {
    std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    let stem = dw.stem();
    let mut out = vec![];
    for (doc, tag) in [(&dw.canoe, "sheet1_canoe"), (&dw.mold, "sheet2_mold")] {
        for (ext, bytes) in [
            ("pdf", crate::drafting::to_pdf(doc)),
            ("svg", crate::drafting::to_svg(doc).into_bytes()),
            ("dxf", crate::drafting::to_dxf(doc).into_bytes()),
        ] {
            let name = format!("{stem}_{tag}.{ext}");
            std::fs::write(dir.join(&name), bytes).with_context(|| format!("writing {name}"))?;
            out.push(name);
        }
    }
    Ok(out)
}
