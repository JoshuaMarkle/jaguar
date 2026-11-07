//! Drafting primitives and the sheet file writers (vector PDF, SVG, DXF R12). Knows nothing
//! about canoes: `sheets.rs` draws with it. Below `drawing` in the layout; no physics.
//!
//! A [`Doc`] is one sheet in PAPER inches, origin at the lower left, y up. Items carry an ASME
//! Y14.2 line convention ([`Pen`]: thick visible, thin hidden / centre / phantom / dimension) and
//! are written 1:1 by every writer, so a printed PDF measures true: a view drawn at 1:25 is
//! 1/25 of the model on the paper.
//!
//! * PDF 1.4, uncompressed content, Helvetica (one of the 14 standard fonts: nothing embedded),
//!   objects numbered in a fixed order, no creation date or ID: the same Doc is the same bytes.
//!   Each dimension is wrapped in a marked-content sequence `/Dim <</V (text) /M model /S scale /A axis
//!   /P [x0 y0 x1 y1]>> BDC .. EMC` (points), so a reader (and the tests) can measure it.
//! * SVG 1.1 in inches (viewBox = the sheet), dimensions as `<g class="dim" data-...>`.
//! * DXF R12 (AC1009), inches, paper scale, one layer per line convention with its linetype.
//!
//! Text heights are cap heights (drafting convention); widths come from the Helvetica AFM.

use anyhow::{Context, Result, bail, ensure};
use std::fmt::Write as _;
use std::path::Path;

pub type P = [f64; 2];

/// Line type (ASME Y14.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dash {
    Solid,
    /// Hidden: short dashes.
    Hidden,
    /// Centre line: long dash, short dash.
    Center,
    /// Phantom: long dash, two short dashes.
    Phantom,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Weight {
    /// 0.6 mm: visible outlines, cutting planes, borders.
    Thick,
    /// 0.3 mm: everything else.
    Thin,
}

/// Which DXF layer an item goes on (and which group a reader can filter in SVG).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layer {
    Visible,
    Hidden,
    Center,
    Phantom,
    Dim,
    Text,
    Hatch,
    Border,
    Shade,
}

impl Layer {
    fn name(self) -> &'static str {
        match self {
            Layer::Visible => "VISIBLE",
            Layer::Hidden => "HIDDEN",
            Layer::Center => "CENTER",
            Layer::Phantom => "PHANTOM",
            Layer::Dim => "DIM",
            Layer::Text => "TEXT",
            Layer::Hatch => "HATCH",
            Layer::Border => "BORDER",
            Layer::Shade => "SHADE",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Pen {
    pub weight: Weight,
    pub dash: Dash,
    pub layer: Layer,
}

impl Pen {
    pub const VISIBLE: Pen = Pen { weight: Weight::Thick, dash: Dash::Solid, layer: Layer::Visible };
    pub const HIDDEN: Pen = Pen { weight: Weight::Thin, dash: Dash::Hidden, layer: Layer::Hidden };
    pub const CENTER: Pen = Pen { weight: Weight::Thin, dash: Dash::Center, layer: Layer::Center };
    pub const PHANTOM: Pen = Pen { weight: Weight::Thin, dash: Dash::Phantom, layer: Layer::Phantom };
    /// Dimension, extension and leader lines.
    pub const DIM: Pen = Pen { weight: Weight::Thin, dash: Dash::Solid, layer: Layer::Dim };
    /// Thin solid outline (tables, inner surfaces seen, body-plan stations).
    pub const THIN: Pen = Pen { weight: Weight::Thin, dash: Dash::Solid, layer: Layer::Visible };
    pub const HATCH: Pen = Pen { weight: Weight::Thin, dash: Dash::Solid, layer: Layer::Hatch };
    /// Cutting-plane line: thick phantom.
    pub const CUT: Pen = Pen { weight: Weight::Thick, dash: Dash::Phantom, layer: Layer::Visible };
    pub const BORDER: Pen = Pen { weight: Weight::Thick, dash: Dash::Solid, layer: Layer::Border };
    pub const BORDER_THIN: Pen = Pen { weight: Weight::Thin, dash: Dash::Solid, layer: Layer::Border };
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HAlign {
    Left,
    Center,
    Right,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VAlign {
    Bottom,
    Middle,
    Top,
}

/// A measured dimension's record: what it says and what it measures.
#[derive(Debug, Clone, PartialEq)]
pub struct DimTag {
    /// The text printed.
    pub text: String,
    /// The model value, in.
    pub model: f64,
    /// Scale denominator of the view (model in per paper in).
    pub scale: f64,
    /// The two measured feature points on the paper, in, measured along `axis` (0 x, 1 y).
    pub a: P,
    pub b: P,
    pub axis: usize,
}

impl DimTag {
    /// The paper distance the dimension measures, in.
    pub fn paper(&self) -> f64 {
        (self.b[self.axis] - self.a[self.axis]).abs()
    }
}

#[derive(Debug, Clone)]
pub enum Item {
    Path {
        pts: Vec<P>,
        closed: bool,
        pen: Pen,
    },
    /// Filled polygon (no outline), colour 0..1.
    Fill {
        pts: Vec<P>,
        rgb: [f64; 3],
        layer: Layer,
    },
    Circle {
        c: P,
        r: f64,
        pen: Pen,
        filled: bool,
    },
    /// `h` = cap height, in; rotation counter-clockwise, deg.
    Text {
        at: P,
        h: f64,
        text: String,
        ha: HAlign,
        va: VAlign,
        rot: f64,
    },
    /// The sheet's logo, lower-left corner `at`, `w` x `h` in.
    Image {
        at: P,
        w: f64,
        h: f64,
    },
    DimBegin(DimTag),
    DimEnd,
}

/// Paper sizes and drafting proportions of one sheet size.
#[derive(Debug, Clone, Copy)]
pub struct Style {
    pub text: f64,
    pub small: f64,
    pub title: f64,
    pub big: f64,
    pub arrow: f64,
    /// Extension line gap from the feature and overshoot past the dimension line.
    pub ext_gap: f64,
    pub ext_over: f64,
    /// Dash pattern scale (1 on D).
    pub dash: f64,
    /// Line widths, in.
    pub thick: f64,
    pub thin: f64,
}

impl Style {
    pub fn d() -> Style {
        Style {
            text: 0.12,
            small: 0.09,
            title: 0.18,
            big: 0.30,
            arrow: 0.125,
            ext_gap: 0.0625,
            ext_over: 0.125,
            dash: 1.0,
            thick: 0.6 / 25.4,
            thin: 0.3 / 25.4,
        }
    }
    pub fn b() -> Style {
        Style {
            text: 0.085,
            small: 0.065,
            title: 0.12,
            big: 0.19,
            arrow: 0.09,
            ext_gap: 0.045,
            ext_over: 0.09,
            dash: 0.7,
            thick: 0.5 / 25.4,
            thin: 0.25 / 25.4,
        }
    }
}

/// A raster logo for the title block (PNG passed through as Flate data, or JPEG).
#[derive(Debug, Clone, PartialEq)]
pub struct Logo {
    pub w_px: u32,
    pub h_px: u32,
    pub kind: LogoKind,
    /// The file as read (SVG embeds it as a data URI).
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum LogoKind {
    /// Concatenated IDAT data; colours per pixel 1 (gray / palette) or 3; bits per component;
    /// palette (RGB triples) for colour type 3.
    Png { colors: u8, bits: u8, palette: Option<Vec<u8>>, idat: Vec<u8> },
    /// JPEG: components (1 gray, 3 RGB, 4 CMYK).
    Jpeg { comps: u8 },
}

impl Logo {
    /// Read a PNG (8-bit or less gray, RGB or palette, not interlaced, no transparency) or a
    /// baseline / progressive JPEG. Anything else is an error saying how to fix the file.
    pub fn load(path: &Path) -> Result<Logo> {
        let bytes = std::fs::read(path).with_context(|| format!("reading logo {}", path.display()))?;
        Logo::parse(bytes).with_context(|| format!("logo {}", path.display()))
    }

    pub fn parse(bytes: Vec<u8>) -> Result<Logo> {
        if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
            let (mut pos, mut ihdr, mut idat, mut palette) = (8usize, None, Vec::new(), None);
            while pos + 8 <= bytes.len() {
                let len = u32::from_be_bytes(bytes[pos..pos + 4].try_into()?) as usize;
                let kind = &bytes[pos + 4..pos + 8];
                let data = bytes.get(pos + 8..pos + 8 + len).context("truncated PNG chunk")?;
                match kind {
                    b"IHDR" => ihdr = Some(data.to_vec()),
                    b"PLTE" => palette = Some(data.to_vec()),
                    b"IDAT" => idat.extend_from_slice(data),
                    b"tRNS" => bail!("the PNG has transparency: flatten it onto white and save again"),
                    b"IEND" => break,
                    _ => {}
                }
                pos += 12 + len;
            }
            let h = ihdr.context("PNG has no IHDR")?;
            ensure!(h.len() >= 13, "short PNG header");
            let w = u32::from_be_bytes(h[0..4].try_into()?);
            let hh = u32::from_be_bytes(h[4..8].try_into()?);
            let (bits, ctype, interlace) = (h[8], h[9], h[12]);
            ensure!(interlace == 0, "interlaced PNG: save it without interlacing");
            let colors = match ctype {
                0 => 1,
                2 => 3,
                3 => 1,
                4 | 6 => bail!("the PNG has an alpha channel: flatten it onto white and save as RGB"),
                c => bail!("unknown PNG colour type {c}"),
            };
            ensure!(bits <= 8, "16-bit PNG: save it as 8-bit");
            ensure!(ctype != 3 || palette.is_some(), "palette PNG without a palette");
            ensure!(!idat.is_empty() && w > 0 && hh > 0, "empty PNG");
            let palette = if ctype == 3 { palette } else { None };
            return Ok(Logo {
                w_px: w,
                h_px: hh,
                kind: LogoKind::Png { colors, bits, palette, idat },
                bytes,
            });
        }
        if bytes.starts_with(&[0xFF, 0xD8]) {
            let mut pos = 2usize;
            while pos + 4 <= bytes.len() {
                ensure!(bytes[pos] == 0xFF, "bad JPEG marker");
                let m = bytes[pos + 1];
                let len = u16::from_be_bytes([bytes[pos + 2], bytes[pos + 3]]) as usize;
                if matches!(m, 0xC0..=0xC2) {
                    let s = bytes.get(pos + 4..pos + 4 + 6).context("truncated JPEG")?;
                    let hh = u16::from_be_bytes([s[1], s[2]]) as u32;
                    let w = u16::from_be_bytes([s[3], s[4]]) as u32;
                    let comps = s[5];
                    ensure!(matches!(comps, 1 | 3 | 4), "JPEG with {comps} components");
                    return Ok(Logo { w_px: w, h_px: hh, kind: LogoKind::Jpeg { comps }, bytes });
                }
                pos += 2 + len;
            }
            bail!("JPEG without a frame header");
        }
        bail!("the logo must be a PNG or JPEG file")
    }

    fn mime(&self) -> &'static str {
        match self.kind {
            LogoKind::Png { .. } => "image/png",
            LogoKind::Jpeg { .. } => "image/jpeg",
        }
    }
}

/// One sheet.
#[derive(Debug, Clone)]
pub struct Doc {
    /// Paper width and height, in.
    pub w: f64,
    pub h: f64,
    pub style: Style,
    pub items: Vec<Item>,
    /// Document title and (key, value) metadata written into every format.
    pub title: String,
    pub info: Vec<(String, String)>,
    pub logo: Option<Logo>,
}

/// Helvetica advance widths, 1/1000 em, for ASCII 32..=126 (Adobe AFM).
const HELV: [u16; 95] = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
    556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
    500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
    556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
    278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];
/// Helvetica cap height, 1/1000 em: font size = cap height / this.
const CAP: f64 = 0.718;

/// Width of `s` set at cap height `h`, in.
pub fn text_width(s: &str, h: f64) -> f64 {
    let em: u32 = s
        .chars()
        .map(|c| {
            let k = c as u32;
            if (32..=126).contains(&k) { u32::from(HELV[(k - 32) as usize]) } else { 556 }
        })
        .sum();
    f64::from(em) / 1000.0 * h / CAP
}

fn add(a: P, b: P) -> P {
    [a[0] + b[0], a[1] + b[1]]
}
fn sub(a: P, b: P) -> P {
    [a[0] - b[0], a[1] - b[1]]
}
fn mul(a: P, k: f64) -> P {
    [a[0] * k, a[1] * k]
}
fn len(a: P) -> f64 {
    a[0].hypot(a[1])
}

impl Doc {
    pub fn new(w: f64, h: f64, style: Style, title: &str) -> Doc {
        Doc { w, h, style, items: Vec::new(), title: title.to_string(), info: Vec::new(), logo: None }
    }

    pub fn line(&mut self, a: P, b: P, pen: Pen) {
        self.items.push(Item::Path { pts: vec![a, b], closed: false, pen });
    }

    pub fn path(&mut self, pts: Vec<P>, pen: Pen) {
        if pts.len() >= 2 {
            self.items.push(Item::Path { pts, closed: false, pen });
        }
    }

    pub fn polygon(&mut self, pts: Vec<P>, pen: Pen) {
        if pts.len() >= 3 {
            self.items.push(Item::Path { pts, closed: true, pen });
        }
    }

    pub fn rect(&mut self, lo: P, hi: P, pen: Pen) {
        self.polygon(vec![lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]], pen);
    }

    pub fn fill(&mut self, pts: Vec<P>, rgb: [f64; 3], layer: Layer) {
        if pts.len() >= 3 {
            self.items.push(Item::Fill { pts, rgb, layer });
        }
    }

    pub fn circle(&mut self, c: P, r: f64, pen: Pen) {
        self.items.push(Item::Circle { c, r, pen, filled: false });
    }

    pub fn dot(&mut self, c: P, r: f64) {
        self.items.push(Item::Circle { c, r, pen: Pen::DIM, filled: true });
    }

    pub fn text(&mut self, at: P, h: f64, text: impl Into<String>, ha: HAlign, va: VAlign) {
        self.text_rot(at, h, text, ha, va, 0.0);
    }

    pub fn text_rot(&mut self, at: P, h: f64, text: impl Into<String>, ha: HAlign, va: VAlign, rot: f64) {
        let text = text.into();
        if !text.is_empty() {
            self.items.push(Item::Text { at, h, text, ha, va, rot });
        }
    }

    /// Filled arrowhead, tip at `tip`, pointing along `dir`.
    pub fn arrowhead(&mut self, tip: P, dir: P) {
        let l = len(dir).max(1e-12);
        let u = mul(dir, 1.0 / l);
        let n = [-u[1], u[0]];
        let a = self.style.arrow;
        let base = sub(tip, mul(u, a));
        self.fill(vec![tip, add(base, mul(n, a / 6.0)), sub(base, mul(n, a / 6.0))], [0.0; 3], Layer::Dim);
    }

    /// Linear dimension between feature points `a` and `b`, measured along `axis` (0 = x, 1 = y),
    /// the dimension line `off` paper inches from the farther feature on the side `side` (+1 /
    /// -1 along the other axis). Extension lines leave a gap at the feature and run past the
    /// dimension line; arrows go inside when there is room, else outside. The text is
    /// horizontal (unidirectional), above a horizontal dimension line or in a break of a
    /// vertical one.
    #[allow(clippy::too_many_arguments)]
    pub fn dim(&mut self, a: P, b: P, axis: usize, side: f64, off: f64, text: &str, model: f64, scale: f64) {
        let st = self.style;
        let o = 1 - axis;
        let at = if side > 0.0 { a[o].max(b[o]) + off } else { a[o].min(b[o]) - off };
        let foot = |p: P| -> (P, P) {
            let mut s = p;
            let mut e = p;
            let dir = if at >= p[o] { 1.0 } else { -1.0 };
            s[o] = p[o] + dir * st.ext_gap.min((at - p[o]).abs());
            e[o] = at + dir * st.ext_over;
            (s, e)
        };
        self.items.push(Item::DimBegin(DimTag { text: text.into(), model, scale, a, b, axis }));
        for p in [a, b] {
            if (at - p[o]).abs() > st.ext_gap + 1e-9 {
                let (s, e) = foot(p);
                self.line(s, e, Pen::DIM);
            }
        }
        let mut pa = a;
        let mut pb = b;
        pa[o] = at;
        pb[o] = at;
        if pa[axis] > pb[axis] {
            std::mem::swap(&mut pa, &mut pb);
        }
        let span = pb[axis] - pa[axis];
        let mut u = [0.0; 2];
        u[axis] = 1.0;
        let tw = text_width(text, st.text);
        let inside = span > 2.5 * st.arrow;
        if inside {
            self.arrowhead(pa, mul(u, -1.0));
            self.arrowhead(pb, u);
        } else {
            self.arrowhead(pa, u);
            self.arrowhead(pb, mul(u, -1.0));
            self.line(sub(pa, mul(u, 2.0 * st.arrow)), pa, Pen::DIM);
            self.line(pb, add(pb, mul(u, 2.0 * st.arrow)), Pen::DIM);
        }
        let mid = mul(add(pa, pb), 0.5);
        if axis == 0 {
            self.line(pa, pb, Pen::DIM);
            // Text above (or below, on the low side) the line; beside it when too long.
            let (ty, va) = if side > 0.0 {
                (mid[1] + 0.4 * st.text, VAlign::Bottom)
            } else {
                (mid[1] - 0.4 * st.text, VAlign::Top)
            };
            if tw + 0.1 < span || inside {
                self.text([mid[0], ty], st.text, text, HAlign::Center, va);
            } else {
                self.text([pb[0] + 2.2 * st.arrow, mid[1]], st.text, text, HAlign::Left, VAlign::Middle);
            }
        } else {
            let gap = 0.5 * st.text + 0.04;
            if inside && span > 2.0 * gap + 2.0 * st.arrow {
                self.line(pa, [pa[0], mid[1] - gap], Pen::DIM);
                self.line([pb[0], mid[1] + gap], pb, Pen::DIM);
                self.text(mid, st.text, text, HAlign::Center, VAlign::Middle);
            } else {
                self.line(pa, pb, Pen::DIM);
                let (x, ha) =
                    if side > 0.0 { (mid[0] + 0.08, HAlign::Left) } else { (mid[0] - 0.08, HAlign::Right) };
                self.text([x, mid[1]], st.text, text, ha, VAlign::Middle);
            }
        }
        self.items.push(Item::DimEnd);
    }

    /// Leader from the text at `from` to an arrowhead at `to`, with a short shoulder.
    pub fn leader(&mut self, from: P, to: P, text: &str) {
        let st = self.style;
        let right = from[0] >= to[0];
        let shoulder = if right { [from[0] - 0.12, from[1]] } else { [from[0] + 0.12, from[1]] };
        self.line(shoulder, to, Pen::DIM);
        self.line(from, shoulder, Pen::DIM);
        self.arrowhead(to, sub(to, shoulder));
        let (x, ha) = if right { (from[0] + 0.04, HAlign::Left) } else { (from[0] - 0.04, HAlign::Right) };
        for (k, line) in text.lines().enumerate() {
            self.text([x, from[1] - k as f64 * 1.5 * st.small], st.small, line, ha, VAlign::Middle);
        }
    }

    /// Detail bubble: a thin circle round the detail area and its letter in a small circle on a
    /// leader.
    pub fn bubble(&mut self, c: P, r: f64, letter: &str, label_at: P) {
        let st = self.style;
        self.circle(c, r, Pen::DIM);
        let d = sub(label_at, c);
        let l = len(d).max(1e-9);
        let edge = add(c, mul(d, r / l));
        let lr = 0.75 * st.title;
        let near = sub(label_at, mul(d, lr / l));
        self.line(edge, near, Pen::DIM);
        self.circle(label_at, lr, Pen::DIM);
        self.text(label_at, st.title, letter, HAlign::Center, VAlign::Middle);
    }

    /// Hatch lines (45 deg by default) inside polygon `poly`, `spacing` apart.
    pub fn hatch(&mut self, poly: &[P], spacing: f64, angle_deg: f64) {
        for (a, b) in hatch_lines(poly, spacing, angle_deg) {
            self.line(a, b, Pen::HATCH);
        }
    }

    /// The marked dimensions, in drawing order.
    pub fn dims(&self) -> Vec<&DimTag> {
        self.items.iter().filter_map(|i| if let Item::DimBegin(t) = i { Some(t) } else { None }).collect()
    }

    /// Every text string on the sheet, in drawing order.
    pub fn texts(&self) -> Vec<&str> {
        self.items
            .iter()
            .filter_map(|i| if let Item::Text { text, .. } = i { Some(text.as_str()) } else { None })
            .collect()
    }
}

/// Segments of parallel lines at `angle_deg`, `spacing` apart, clipped to the inside of `poly`
/// (even-odd), in a fixed order.
pub fn hatch_lines(poly: &[P], spacing: f64, angle_deg: f64) -> Vec<(P, P)> {
    let n = poly.len();
    if n < 3 || spacing <= 0.0 {
        return vec![];
    }
    let (s, c) = angle_deg.to_radians().sin_cos();
    // Rotate so the hatch lines are horizontal: v = -s x + c y.
    let v = |p: P| -s * p[0] + c * p[1];
    let u = |p: P| c * p[0] + s * p[1];
    let (lo, hi) =
        poly.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), &p| (a.min(v(p)), b.max(v(p))));
    let mut out = vec![];
    let mut k = (lo / spacing).ceil();
    while k * spacing <= hi {
        let y = k * spacing;
        let mut xs: Vec<f64> = (0..n)
            .filter_map(|i| {
                let (a, b) = (poly[i], poly[(i + 1) % n]);
                let (va, vb) = (v(a), v(b));
                ((va <= y) != (vb <= y)).then(|| {
                    let t = (y - va) / (vb - va);
                    u(a) + t * (u(b) - u(a))
                })
            })
            .collect();
        xs.sort_by(f64::total_cmp);
        for pair in xs.as_chunks::<2>().0 {
            let back = |uu: f64| [c * uu - s * y, s * uu + c * y];
            out.push((back(pair[0]), back(pair[1])));
        }
        k += 1.0;
    }
    out
}

// ---------- number formatting ----------

/// A number with up to `dp` decimals, trailing zeros dropped, never "-0".
fn num(v: f64, dp: usize) -> String {
    let s = format!("{v:.dp$}");
    let s = if s.contains('.') { s.trim_end_matches('0').trim_end_matches('.').to_string() } else { s };
    if s == "-0" { "0".into() } else { s }
}

fn pt(v: f64) -> String {
    num(v * 72.0, 3)
}

// ---------- PDF ----------

/// PDF string literal: WinAnsi (Latin-1 range kept as octal escapes, anything else '?').
fn pdf_str(s: &str) -> String {
    let mut o = String::from("(");
    for c in s.chars() {
        match c {
            '(' | ')' | '\\' => {
                o.push('\\');
                o.push(c);
            }
            ' '..='~' => o.push(c),
            c if (0xA0..=0xFF).contains(&(c as u32)) => {
                let _ = write!(o, "\\{:03o}", c as u32);
            }
            _ => o.push('?'),
        }
    }
    o.push(')');
    o
}

fn dash_pattern(d: Dash, k: f64) -> Vec<f64> {
    let v: &[f64] = match d {
        Dash::Solid => &[],
        Dash::Hidden => &[0.125, 0.0625],
        Dash::Center => &[0.75, 0.0625, 0.125, 0.0625],
        Dash::Phantom => &[0.75, 0.0625, 0.125, 0.0625, 0.125, 0.0625],
    };
    v.iter().map(|x| x * k).collect()
}

fn width_of(st: &Style, w: Weight) -> f64 {
    match w {
        Weight::Thick => st.thick,
        Weight::Thin => st.thin,
    }
}

/// Where a text's baseline starts, given its anchor and alignment (unrotated frame).
fn text_origin(text: &str, h: f64, ha: HAlign, va: VAlign) -> P {
    let w = text_width(text, h);
    let dx = match ha {
        HAlign::Left => 0.0,
        HAlign::Center => -0.5 * w,
        HAlign::Right => -w,
    };
    let dy = match va {
        VAlign::Bottom => 0.0,
        VAlign::Middle => -0.5 * h,
        VAlign::Top => -h,
    };
    [dx, dy]
}

/// Four cubic Beziers approximating a circle.
fn circle_path(c: P, r: f64) -> String {
    let k = 0.552_284_75 * r;
    let mut s = String::new();
    let p = |x: f64, y: f64| format!("{} {}", pt(x), pt(y));
    let _ = write!(s, "{} m ", p(c[0] + r, c[1]));
    let _ = write!(s, "{} {} {} c ", p(c[0] + r, c[1] + k), p(c[0] + k, c[1] + r), p(c[0], c[1] + r));
    let _ = write!(s, "{} {} {} c ", p(c[0] - k, c[1] + r), p(c[0] - r, c[1] + k), p(c[0] - r, c[1]));
    let _ = write!(s, "{} {} {} c ", p(c[0] - r, c[1] - k), p(c[0] - k, c[1] - r), p(c[0], c[1] - r));
    let _ = write!(s, "{} {} {} c h", p(c[0] + k, c[1] - r), p(c[0] + r, c[1] - k), p(c[0] + r, c[1]));
    s
}

/// The sheet as a PDF (see the module doc).
pub fn to_pdf(doc: &Doc) -> Vec<u8> {
    let st = &doc.style;
    let mut c = String::new();
    c.push_str("1 J 1 j 0 G 0 g\n");
    let (mut cur_w, mut cur_d): (Option<String>, Option<String>) = (None, None);
    let mut set_pen = |c: &mut String, pen: &Pen| {
        let w = pt(width_of(st, pen.weight));
        if cur_w.as_deref() != Some(w.as_str()) {
            let _ = writeln!(c, "{w} w");
            cur_w = Some(w);
        }
        let d = format!(
            "[{}] 0 d",
            dash_pattern(pen.dash, st.dash).iter().map(|v| pt(*v)).collect::<Vec<_>>().join(" ")
        );
        if cur_d.as_ref() != Some(&d) {
            let _ = writeln!(c, "{d}");
            cur_d = Some(d);
        }
    };
    for it in &doc.items {
        match it {
            Item::Path { pts, closed, pen } => {
                set_pen(&mut c, pen);
                for (k, p) in pts.iter().enumerate() {
                    let _ = write!(c, "{} {} {} ", pt(p[0]), pt(p[1]), if k == 0 { "m" } else { "l" });
                }
                c.push_str(if *closed { "h S\n" } else { "S\n" });
            }
            Item::Fill { pts, rgb, .. } => {
                // Filled and stroked hairline in the same colour, so neighbours leave no seam.
                let col = format!("{} {} {}", num(rgb[0], 4), num(rgb[1], 4), num(rgb[2], 4));
                let _ = write!(c, "q {col} rg {col} RG 0.3 w [] 0 d ");
                for (k, p) in pts.iter().enumerate() {
                    let _ = write!(c, "{} {} {} ", pt(p[0]), pt(p[1]), if k == 0 { "m" } else { "l" });
                }
                c.push_str("h B Q\n");
            }
            Item::Circle { c: cc, r, pen, filled } => {
                set_pen(&mut c, pen);
                let _ = writeln!(c, "{} {}", circle_path(*cc, *r), if *filled { "f" } else { "S" });
            }
            Item::Text { at, h, text, ha, va, rot } => {
                let o = text_origin(text, *h, *ha, *va);
                let (s, co) = rot.to_radians().sin_cos();
                let x = at[0] + co * o[0] - s * o[1];
                let y = at[1] + s * o[0] + co * o[1];
                let _ = writeln!(
                    c,
                    "BT /F1 {} Tf {} {} {} {} {} {} Tm {} Tj ET",
                    num(h / CAP * 72.0, 3),
                    num(co, 6),
                    num(s, 6),
                    num(-s, 6),
                    num(co, 6),
                    pt(x),
                    pt(y),
                    pdf_str(text)
                );
            }
            Item::Image { at, w, h } => {
                if doc.logo.is_some() {
                    let _ =
                        writeln!(c, "q {} 0 0 {} {} {} cm /Im1 Do Q", pt(*w), pt(*h), pt(at[0]), pt(at[1]));
                }
            }
            Item::DimBegin(t) => {
                let _ = writeln!(
                    c,
                    "/Dim <</V {} /M {} /S {} /A {} /P [{} {} {} {}]>> BDC",
                    pdf_str(&t.text),
                    num(t.model, 6),
                    num(t.scale, 6),
                    t.axis,
                    pt(t.a[0]),
                    pt(t.a[1]),
                    pt(t.b[0]),
                    pt(t.b[1])
                );
            }
            Item::DimEnd => c.push_str("EMC\n"),
        }
    }
    // Objects: 1 catalog, 2 pages, 3 page, 4 font, 5 content, 6 info, 7 logo image.
    let mut objs: Vec<Vec<u8>> = vec![];
    objs.push(b"<< /Type /Catalog /Pages 2 0 R >>".to_vec());
    objs.push(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_vec());
    let xobj = if doc.logo.is_some() { " /XObject << /Im1 7 0 R >>" } else { "" };
    objs.push(
        format!(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {} {}] /Resources << /Font << /F1 4 0 R >>{xobj} >> /Contents 5 0 R >>",
            pt(doc.w),
            pt(doc.h)
        )
        .into_bytes(),
    );
    objs.push(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>".to_vec());
    let mut content = format!("<< /Length {} >>\nstream\n", c.len()).into_bytes();
    content.extend_from_slice(c.as_bytes());
    content.extend_from_slice(b"\nendstream");
    objs.push(content);
    let mut info = format!("<< /Title {} /Producer (JAGUAR)", pdf_str(&doc.title));
    for (k, v) in &doc.info {
        let key: String = k.chars().filter(|c| c.is_ascii_alphanumeric()).collect();
        let _ = write!(info, " /{key} {}", pdf_str(v));
    }
    info.push_str(" >>");
    objs.push(info.into_bytes());
    if let Some(l) = &doc.logo {
        let mut o = match &l.kind {
            LogoKind::Png { colors, bits, palette, idat } => {
                let cs = match palette {
                    Some(p) => {
                        let hex: String = p.iter().map(|b| format!("{b:02X}")).collect();
                        format!("[/Indexed /DeviceRGB {} <{hex}>]", p.len() / 3 - 1)
                    }
                    None if *colors == 1 => "/DeviceGray".into(),
                    None => "/DeviceRGB".into(),
                };
                let mut o = format!(
                    "<< /Type /XObject /Subtype /Image /Width {} /Height {} /ColorSpace {cs} /BitsPerComponent {bits} /Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors {colors} /BitsPerComponent {bits} /Columns {} >> /Length {} >>\nstream\n",
                    l.w_px,
                    l.h_px,
                    l.w_px,
                    idat.len()
                )
                .into_bytes();
                o.extend_from_slice(idat);
                o
            }
            LogoKind::Jpeg { comps } => {
                let cs = match comps {
                    1 => "/DeviceGray",
                    4 => "/DeviceCMYK",
                    _ => "/DeviceRGB",
                };
                let mut o = format!(
                    "<< /Type /XObject /Subtype /Image /Width {} /Height {} /ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode /Length {} >>\nstream\n",
                    l.w_px,
                    l.h_px,
                    l.bytes.len()
                )
                .into_bytes();
                o.extend_from_slice(&l.bytes);
                o
            }
        };
        o.extend_from_slice(b"\nendstream");
        objs.push(o);
    }
    let mut out = b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n".to_vec();
    let mut offsets = vec![];
    for (i, o) in objs.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n", i + 1).as_bytes());
        out.extend_from_slice(o);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    let mut x = format!("xref\n0 {}\n0000000000 65535 f \n", objs.len() + 1);
    for off in offsets {
        let _ = writeln!(x, "{off:010} 00000 n ");
    }
    let _ = write!(
        x,
        "trailer\n<< /Size {} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n",
        objs.len() + 1
    );
    out.extend_from_slice(x.as_bytes());
    out
}

// ---------- SVG ----------

fn xml(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
}

fn base64(b: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut o = String::with_capacity(b.len().div_ceil(3) * 4);
    for ch in b.chunks(3) {
        let n = (u32::from(ch[0]) << 16)
            | (u32::from(*ch.get(1).unwrap_or(&0)) << 8)
            | u32::from(*ch.get(2).unwrap_or(&0));
        for k in 0..4 {
            if k <= ch.len() {
                o.push(T[((n >> (18 - 6 * k)) & 63) as usize] as char);
            } else {
                o.push('=');
            }
        }
    }
    o
}

/// The sheet as SVG (inches; y flipped to SVG's downward axis).
pub fn to_svg(doc: &Doc) -> String {
    let st = &doc.style;
    let n = |v: f64| num(v, 4);
    let y = |v: f64| num(doc.h - v, 4);
    let mut s = String::new();
    let _ = writeln!(
        s,
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{}in\" height=\"{}in\" viewBox=\"0 0 {} {}\">",
        n(doc.w),
        n(doc.h),
        n(doc.w),
        n(doc.h)
    );
    let _ = writeln!(s, "<title>{}</title>", xml(&doc.title));
    s.push_str("<metadata>");
    for (k, v) in &doc.info {
        let _ = write!(s, "<jaguar key=\"{}\">{}</jaguar>", xml(k), xml(v));
    }
    s.push_str("</metadata>\n");
    let _ =
        writeln!(s, "<rect x=\"0\" y=\"0\" width=\"{}\" height=\"{}\" fill=\"#fff\"/>", n(doc.w), n(doc.h));
    let stroke = |pen: &Pen| {
        let d = dash_pattern(pen.dash, st.dash);
        let da = if d.is_empty() {
            String::new()
        } else {
            format!(" stroke-dasharray=\"{}\"", d.iter().map(|v| n(*v)).collect::<Vec<_>>().join(" "))
        };
        format!(
            "fill=\"none\" stroke=\"#000\" stroke-width=\"{}\" stroke-linecap=\"round\" stroke-linejoin=\"round\"{da}",
            n(width_of(st, pen.weight))
        )
    };
    let pts = |p: &[P]| p.iter().map(|q| format!("{},{}", n(q[0]), y(q[1]))).collect::<Vec<_>>().join(" ");
    let rgb = |c: &[f64; 3]| {
        format!(
            "#{:02x}{:02x}{:02x}",
            (c[0] * 255.0).round() as u8,
            (c[1] * 255.0).round() as u8,
            (c[2] * 255.0).round() as u8
        )
    };
    for it in &doc.items {
        match it {
            Item::Path { pts: p, closed, pen } => {
                let tag = if *closed { "polygon" } else { "polyline" };
                let _ = writeln!(s, "<{tag} points=\"{}\" {}/>", pts(p), stroke(pen));
            }
            Item::Fill { pts: p, rgb: c, .. } => {
                let col = rgb(c);
                let _ = writeln!(
                    s,
                    "<polygon points=\"{}\" fill=\"{col}\" stroke=\"{col}\" stroke-width=\"0.004\"/>",
                    pts(p)
                );
            }
            Item::Circle { c, r, pen, filled } => {
                let style = if *filled { "fill=\"#000\" stroke=\"none\"".to_string() } else { stroke(pen) };
                let _ =
                    writeln!(s, "<circle cx=\"{}\" cy=\"{}\" r=\"{}\" {style}/>", n(c[0]), y(c[1]), n(*r));
            }
            Item::Text { at, h, text, ha, va, rot } => {
                let o = text_origin(text, *h, *ha, *va);
                let (sn, co) = rot.to_radians().sin_cos();
                let x = at[0] + co * o[0] - sn * o[1];
                let yy = at[1] + sn * o[0] + co * o[1];
                let tr = if rot.abs() > 1e-9 {
                    format!(" transform=\"rotate({} {} {})\"", n(-rot), n(x), y(yy))
                } else {
                    String::new()
                };
                let _ = writeln!(
                    s,
                    "<text x=\"{}\" y=\"{}\" font-family=\"Helvetica, Arial, sans-serif\" font-size=\"{}\"{tr}>{}</text>",
                    n(x),
                    y(yy),
                    n(h / CAP),
                    xml(text)
                );
            }
            Item::Image { at, w, h } => {
                if let Some(l) = &doc.logo {
                    let _ = writeln!(
                        s,
                        "<image x=\"{}\" y=\"{}\" width=\"{}\" height=\"{}\" href=\"data:{};base64,{}\"/>",
                        n(at[0]),
                        y(at[1] + h),
                        n(*w),
                        n(*h),
                        l.mime(),
                        base64(&l.bytes)
                    );
                }
            }
            Item::DimBegin(t) => {
                let _ = writeln!(
                    s,
                    "<g class=\"dim\" data-text=\"{}\" data-model=\"{}\" data-scale=\"{}\" data-axis=\"{}\" data-p=\"{} {} {} {}\">",
                    xml(&t.text),
                    num(t.model, 6),
                    num(t.scale, 6),
                    t.axis,
                    n(t.a[0]),
                    n(t.a[1]),
                    n(t.b[0]),
                    n(t.b[1])
                );
            }
            Item::DimEnd => s.push_str("</g>\n"),
        }
    }
    s.push_str("</svg>\n");
    s
}

// ---------- DXF ----------

fn dxf_text(s: &str) -> String {
    s.chars().map(|c| if c.is_ascii() && !c.is_ascii_control() { c } else { '?' }).collect()
}

/// Nearest AutoCAD colour index for a fill: greys 250-255, else blue (5) or cyan (4).
fn aci(c: &[f64; 3]) -> u8 {
    let (mx, mn) = (c[0].max(c[1]).max(c[2]), c[0].min(c[1]).min(c[2]));
    if mx - mn < 0.08 {
        if mx > 0.97 {
            return 7;
        }
        return 250 + ((mx * 5.0).round() as u8).min(5);
    }
    if c[2] >= c[0] && c[1] >= 0.6 { 4 } else { 5 }
}

/// The sheet as DXF R12 at paper scale, inches (see the module doc). Fills of 3 or 4 points
/// become SOLIDs, larger fills their outline; the logo is left out (raster images need R13+).
pub fn to_dxf(doc: &Doc) -> String {
    let mut o = String::new();
    let mut g = |code: i32, v: &str| {
        let _ = writeln!(o, "{code}\n{v}");
    };
    let n = |v: f64| num(v, 4);
    g(999, &dxf_text(&doc.title));
    for (k, v) in &doc.info {
        for line in v.lines() {
            g(999, &dxf_text(&format!("{k}: {line}")));
        }
    }
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
    g(10, &n(doc.w));
    g(20, &n(doc.h));
    g(9, "$LTSCALE");
    g(40, &n(doc.style.dash));
    g(0, "ENDSEC");
    g(0, "SECTION");
    g(2, "TABLES");
    g(0, "TABLE");
    g(2, "LTYPE");
    g(70, "4");
    for (name, desc, pat) in [
        ("CONTINUOUS", "Solid line", vec![]),
        ("HIDDEN", "Hidden __ __ __", vec![0.125, -0.0625]),
        ("CENTER", "Center ____ _ ____", vec![0.75, -0.0625, 0.125, -0.0625]),
        ("PHANTOM", "Phantom ____ _ _ ____", vec![0.75, -0.0625, 0.125, -0.0625, 0.125, -0.0625]),
    ] {
        g(0, "LTYPE");
        g(2, name);
        g(70, "0");
        g(3, desc);
        g(72, "65");
        g(73, &pat.len().to_string());
        g(40, &n(pat.iter().map(|v: &f64| v.abs()).sum::<f64>()));
        for v in pat {
            g(49, &n(v));
        }
    }
    g(0, "ENDTAB");
    g(0, "TABLE");
    g(2, "LAYER");
    let layers = [
        (Layer::Visible, "CONTINUOUS", "7"),
        (Layer::Hidden, "HIDDEN", "7"),
        (Layer::Center, "CENTER", "7"),
        (Layer::Phantom, "PHANTOM", "7"),
        (Layer::Dim, "CONTINUOUS", "7"),
        (Layer::Text, "CONTINUOUS", "7"),
        (Layer::Hatch, "CONTINUOUS", "8"),
        (Layer::Border, "CONTINUOUS", "7"),
        (Layer::Shade, "CONTINUOUS", "9"),
    ];
    g(70, &layers.len().to_string());
    for (l, lt, col) in layers {
        g(0, "LAYER");
        g(2, l.name());
        g(70, "0");
        g(62, col);
        g(6, lt);
    }
    g(0, "ENDTAB");
    g(0, "ENDSEC");
    g(0, "SECTION");
    g(2, "ENTITIES");
    let ltype = |d: Dash| match d {
        Dash::Solid => "CONTINUOUS",
        Dash::Hidden => "HIDDEN",
        Dash::Center => "CENTER",
        Dash::Phantom => "PHANTOM",
    };
    for it in &doc.items {
        match it {
            Item::Path { pts, closed, pen } => {
                if pts.len() == 2 && !closed {
                    g(0, "LINE");
                    g(8, pen.layer.name());
                    g(6, ltype(pen.dash));
                    g(10, &n(pts[0][0]));
                    g(20, &n(pts[0][1]));
                    g(30, "0.0");
                    g(11, &n(pts[1][0]));
                    g(21, &n(pts[1][1]));
                    g(31, "0.0");
                    continue;
                }
                g(0, "POLYLINE");
                g(8, pen.layer.name());
                g(6, ltype(pen.dash));
                g(66, "1");
                g(10, "0.0");
                g(20, "0.0");
                g(30, "0.0");
                g(70, if *closed { "1" } else { "0" });
                for p in pts {
                    g(0, "VERTEX");
                    g(8, pen.layer.name());
                    g(10, &n(p[0]));
                    g(20, &n(p[1]));
                    g(30, "0.0");
                }
                g(0, "SEQEND");
                g(8, pen.layer.name());
            }
            Item::Fill { pts, rgb, layer } => {
                if pts.len() <= 4 {
                    g(0, "SOLID");
                    g(8, layer.name());
                    g(62, &aci(rgb).to_string());
                    // SOLID corners go 1, 2, 4, 3 (a bow tie otherwise); a triangle repeats its last.
                    let q = [pts[0], pts[1], *pts.get(3).unwrap_or(&pts[2]), pts[2]];
                    for (k, p) in q.iter().enumerate() {
                        g(10 + k as i32, &n(p[0]));
                        g(20 + k as i32, &n(p[1]));
                        g(30 + k as i32, "0.0");
                    }
                } else {
                    g(0, "POLYLINE");
                    g(8, layer.name());
                    g(66, "1");
                    g(10, "0.0");
                    g(20, "0.0");
                    g(30, "0.0");
                    g(70, "1");
                    for p in pts {
                        g(0, "VERTEX");
                        g(8, layer.name());
                        g(10, &n(p[0]));
                        g(20, &n(p[1]));
                        g(30, "0.0");
                    }
                    g(0, "SEQEND");
                    g(8, layer.name());
                }
            }
            Item::Circle { c, r, pen, .. } => {
                g(0, "CIRCLE");
                g(8, pen.layer.name());
                g(10, &n(c[0]));
                g(20, &n(c[1]));
                g(30, "0.0");
                g(40, &n(*r));
            }
            Item::Text { at, h, text, ha, va, rot } => {
                g(0, "TEXT");
                g(8, Layer::Text.name());
                g(10, &n(at[0]));
                g(20, &n(at[1]));
                g(30, "0.0");
                g(40, &n(*h));
                g(1, &dxf_text(text));
                g(50, &n(*rot));
                let h72 = match ha {
                    HAlign::Left => "0",
                    HAlign::Center => "1",
                    HAlign::Right => "2",
                };
                let v73 = match va {
                    VAlign::Bottom => "1",
                    VAlign::Middle => "2",
                    VAlign::Top => "3",
                };
                g(72, h72);
                g(73, v73);
                g(11, &n(at[0]));
                g(21, &n(at[1]));
                g(31, "0.0");
            }
            Item::Image { .. } | Item::DimBegin(_) | Item::DimEnd => {}
        }
    }
    g(0, "ENDSEC");
    g(0, "EOF");
    o
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn helvetica_widths_cover_ascii() {
        // "A" is 667/1000 em; cap height 718: a 0.718 in cap-height "A" is 0.667 in wide.
        assert!((text_width("A", CAP) - 0.667).abs() < 1e-12);
        assert!((text_width("~", CAP) - 0.584).abs() < 1e-12);
    }

    #[test]
    fn hatch_fills_a_square_inside_only() {
        let sq = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]];
        let l = hatch_lines(&sq, 0.1, 45.0);
        assert!(l.len() >= 13, "{}", l.len());
        for (a, b) in l {
            for p in [a, b] {
                assert!(p[0] > -1e-9 && p[0] < 1.0 + 1e-9 && p[1] > -1e-9 && p[1] < 1.0 + 1e-9, "{p:?}");
            }
        }
    }

    #[test]
    fn base64_matches_rfc4648() {
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64(b"foob"), "Zm9vYg==");
    }

    #[test]
    fn pdf_is_deterministic_and_well_formed() {
        let mut d = Doc::new(17.0, 11.0, Style::b(), "t");
        d.line([1.0, 1.0], [2.0, 1.0], Pen::VISIBLE);
        d.dim([1.0, 1.0], [3.0, 1.0], 0, 1.0, 0.5, "50.00", 50.0, 25.0);
        d.text([5.0, 5.0], 0.1, "Univ (A) é", HAlign::Center, VAlign::Middle);
        let (a, b) = (to_pdf(&d), to_pdf(&d));
        assert_eq!(a, b);
        let s = String::from_utf8_lossy(&a);
        assert!(s.starts_with("%PDF-1.4") && s.ends_with("%%EOF\n"));
        assert!(s.contains("(Univ \\(A\\) \\351) Tj"), "escaped WinAnsi text");
        assert!(s.contains("/Dim <</V (50.00)"), "dimension marked");
        assert!(!s.contains("CreationDate"));
        // xref offsets point at the objects.
        let xref = s.find("xref").unwrap();
        let first = s[xref..].lines().nth(3).unwrap();
        let off: usize = first[..10].parse().unwrap();
        // Byte offsets: the raw file, not its lossy UTF-8 copy (the binary comment line).
        assert!(a[off..].starts_with(b"1 0 obj"));
    }

    #[test]
    fn logo_rejects_alpha_png() {
        // 1x1 RGBA PNG header only (colour type 6).
        let mut b = b"\x89PNG\r\n\x1a\n".to_vec();
        b.extend_from_slice(&13u32.to_be_bytes());
        b.extend_from_slice(b"IHDR");
        b.extend_from_slice(&[0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0, 0, 0]);
        b.extend_from_slice(&[0; 4]);
        let e = Logo::parse(b).unwrap_err().to_string();
        assert!(e.contains("alpha"), "{e}");
        assert!(Logo::parse(b"GIF89a".to_vec()).is_err());
    }
}
