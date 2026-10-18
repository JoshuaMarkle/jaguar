//! A STEP file (ISO 10303-21: AP203 / AP214 / AP242 B-rep) turned into triangles, so a canoe
//! drawn in CAD is read like an STL (`meshimport` slices it). Pure: text in, triangles out.
//!
//! * Faces: every face of every shell (`ADVANCED_FACE`, `FACE_SURFACE`). Surfaces: plane,
//!   B-spline (with knots, rational or not), cylinder, cone, sphere, torus. Edge curves: line,
//!   circle, ellipse, B-spline, polyline, and the 3D curve of surface / seam / intersection /
//!   trimmed curves. Anything else is an error naming the entity (export STL instead).
//! * Units: the length unit the file declares (`SI_UNIT` or a conversion-based unit like INCH).
//! * Meshing: each edge is sampled once (about every 1/4 in) and both faces on it use those
//!   points, so the mesh has no gaps along face boundaries. Each face's boundary is mapped onto
//!   its surface's (u, v) parameters, a grid of cells about 1/2 in square is laid over it, every
//!   cell inside the boundary becomes two triangles and every cell the boundary crosses is
//!   clipped to it (trimmed faces and holes work). Boundary chords that leave the surface by more
//!   than 0.005 in are split on the surface. Face orientation (`same_sense`, voids in a
//!   `BREP_WITH_VOIDS`) is kept, so volume / area of a solid is meaningful.
//! * Refused: assemblies that place parts with a transform (export the canoe as one part), a face
//!   that wraps all the way round a closed surface without a seam, an edge or boundary point more
//!   than 0.02 in off its surface.

use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;
use std::collections::{HashMap, HashSet};

type V3 = [f64; 3];
type V2 = [f64; 2];

/// What a STEP file holds, as triangles in the file's units.
#[derive(Debug, Clone)]
pub struct StepModel {
    pub tris: Vec<[V3; 3]>,
    /// Inches per model unit and the unit's name, when the file declares a length unit.
    pub units: Option<(f64, String)>,
    pub faces: usize,
}

// ---------------------------------------------------------------- Part 21 ----

#[derive(Debug, Clone)]
enum P {
    Ref(u64),
    Num(f64),
    Str(String),
    Enum(String),
    List(Vec<P>),
    Typed(Box<P>),
    Null,
}

impl P {
    fn r(&self) -> Result<u64> {
        match self {
            P::Ref(i) => Ok(*i),
            p => bail!("expected a reference, got {p:?}"),
        }
    }
    fn f(&self) -> Result<f64> {
        match self {
            P::Num(v) => Ok(*v),
            P::Typed(p) => p.f(),
            p => bail!("expected a number, got {p:?}"),
        }
    }
    fn list(&self) -> Result<&[P]> {
        match self {
            P::List(v) => Ok(v),
            p => bail!("expected a list, got {p:?}"),
        }
    }
    fn b(&self) -> Result<bool> {
        match self {
            P::Enum(e) if e == "T" => Ok(true),
            P::Enum(e) if e == "F" => Ok(false),
            p => bail!("expected .T. or .F., got {p:?}"),
        }
    }
    fn refs(&self) -> Result<Vec<u64>> {
        self.list()?.iter().map(P::r).collect()
    }
    fn nums(&self) -> Result<Vec<f64>> {
        self.list()?.iter().map(P::f).collect()
    }
}

/// One instance: a simple entity has one part, a complex one (`(A(..) B(..))`) several.
struct Ent {
    parts: Vec<(String, Vec<P>)>,
}

struct Lex<'a> {
    s: &'a [u8],
    i: usize,
}

impl Lex<'_> {
    fn ws(&mut self) {
        loop {
            while self.i < self.s.len() && self.s[self.i].is_ascii_whitespace() {
                self.i += 1;
            }
            if self.s[self.i..].starts_with(b"/*") {
                match self.s[self.i + 2..].windows(2).position(|w| w == b"*/") {
                    Some(k) => self.i += k + 4,
                    None => self.i = self.s.len(),
                }
            } else {
                return;
            }
        }
    }
    fn peek(&mut self) -> Option<u8> {
        self.ws();
        self.s.get(self.i).copied()
    }
    fn eat(&mut self, c: u8) -> Result<()> {
        ensure!(self.peek() == Some(c), "STEP syntax: expected '{}' at byte {}", c as char, self.i);
        self.i += 1;
        Ok(())
    }
    fn kw(&mut self) -> String {
        self.ws();
        let st = self.i;
        while self.i < self.s.len()
            && (self.s[self.i].is_ascii_alphanumeric() || b"_-".contains(&self.s[self.i]))
        {
            self.i += 1;
        }
        String::from_utf8_lossy(&self.s[st..self.i]).to_ascii_uppercase()
    }
    fn int(&mut self) -> Result<u64> {
        let st = self.i;
        while self.i < self.s.len() && self.s[self.i].is_ascii_digit() {
            self.i += 1;
        }
        std::str::from_utf8(&self.s[st..self.i])?
            .parse()
            .with_context(|| format!("STEP: bad id at byte {st}"))
    }
    fn list(&mut self) -> Result<Vec<P>> {
        self.eat(b'(')?;
        let mut v = Vec::new();
        if self.peek() == Some(b')') {
            self.i += 1;
            return Ok(v);
        }
        loop {
            v.push(self.param()?);
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b')') => {
                    self.i += 1;
                    return Ok(v);
                }
                _ => bail!("STEP syntax: expected ',' or ')' at byte {}", self.i),
            }
        }
    }
    fn param(&mut self) -> Result<P> {
        let c = self.peek().context("STEP: unexpected end of file")?;
        Ok(match c {
            b'#' => {
                self.i += 1;
                P::Ref(self.int()?)
            }
            b'\'' => {
                self.i += 1;
                let mut out = Vec::new();
                loop {
                    let ch = *self.s.get(self.i).context("STEP: unterminated string")?;
                    self.i += 1;
                    if ch == b'\'' {
                        if self.s.get(self.i) == Some(&b'\'') {
                            self.i += 1;
                        } else {
                            break;
                        }
                    }
                    out.push(ch);
                }
                P::Str(String::from_utf8_lossy(&out).into())
            }
            b'"' => {
                self.i += 1;
                while self.i < self.s.len() && self.s[self.i] != b'"' {
                    self.i += 1;
                }
                self.i += 1;
                P::Str(String::new())
            }
            b'.' if self.s.get(self.i + 1).is_some_and(|c| c.is_ascii_alphabetic() || *c == b'_') => {
                self.i += 1;
                let e = self.kw();
                self.eat(b'.')?;
                P::Enum(e)
            }
            b'$' | b'*' => {
                self.i += 1;
                P::Null
            }
            b'(' => P::List(self.list()?),
            c if c.is_ascii_digit() || b"+-.".contains(&c) => {
                let st = self.i;
                while self.i < self.s.len()
                    && (self.s[self.i].is_ascii_digit() || b"+-.eE".contains(&self.s[self.i]))
                {
                    self.i += 1;
                }
                let t = std::str::from_utf8(&self.s[st..self.i])?;
                let t = if t.ends_with('.') {
                    format!("{t}0")
                } else {
                    t.replace(".E", ".0E").replace(".e", ".0e")
                };
                P::Num(t.parse().with_context(|| format!("STEP: bad number '{t}'"))?)
            }
            c if c.is_ascii_alphabetic() => {
                self.kw();
                let mut v = self.list()?;
                P::Typed(Box::new(if v.len() == 1 { v.pop().unwrap() } else { P::List(v) }))
            }
            c => bail!("STEP syntax: unexpected '{}' at byte {}", c as char, self.i),
        })
    }
}

fn parse_p21(text: &str) -> Result<HashMap<u64, Ent>> {
    let mut lx = Lex { s: text.as_bytes(), i: 0 };
    ensure!(lx.kw() == "ISO-10303-21", "not a STEP file (no ISO-10303-21 header)");
    lx.eat(b';')?;
    let mut ents = HashMap::new();
    loop {
        let k = lx.kw();
        match k.as_str() {
            "HEADER" | "DATA" => {
                if lx.peek() == Some(b'(') {
                    lx.list()?;
                }
                lx.eat(b';')?;
                loop {
                    match lx.peek() {
                        Some(b'#') => {
                            lx.i += 1;
                            let id = lx.int()?;
                            lx.eat(b'=')?;
                            let mut parts = Vec::new();
                            if lx.peek() == Some(b'(') {
                                lx.i += 1;
                                while lx.peek() != Some(b')') {
                                    let name = lx.kw();
                                    ensure!(!name.is_empty(), "STEP syntax: bad complex entity #{id}");
                                    parts.push((name, lx.list()?));
                                }
                                lx.i += 1;
                            } else {
                                let name = lx.kw();
                                parts.push((name, lx.list()?));
                            }
                            lx.eat(b';')?;
                            ents.insert(id, Ent { parts });
                        }
                        Some(_) => {
                            let k = lx.kw();
                            if k == "ENDSEC" {
                                lx.eat(b';')?;
                                break;
                            }
                            ensure!(!k.is_empty(), "STEP syntax at byte {}", lx.i);
                            lx.list()?; // header entity
                            lx.eat(b';')?;
                        }
                        None => bail!("STEP: missing ENDSEC"),
                    }
                }
            }
            "END-ISO-10303-21" | "" => break,
            k => bail!("STEP: unexpected section {k}"),
        }
    }
    ensure!(!ents.is_empty(), "STEP file has no DATA");
    Ok(ents)
}

// ---------------------------------------------------------------- vectors ----

fn sub(a: V3, b: V3) -> V3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn add(a: V3, b: V3) -> V3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}
fn mul(a: V3, s: f64) -> V3 {
    [a[0] * s, a[1] * s, a[2] * s]
}
fn dot(a: V3, b: V3) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn cross(a: V3, b: V3) -> V3 {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}
fn norm(a: V3) -> f64 {
    dot(a, a).sqrt()
}
fn dist(a: V3, b: V3) -> f64 {
    norm(sub(a, b))
}
fn lerp(a: V3, b: V3, t: f64) -> V3 {
    add(a, mul(sub(b, a), t))
}
fn unit(a: V3) -> Result<V3> {
    let n = norm(a);
    ensure!(n > 0.0 && n.is_finite(), "zero-length direction");
    Ok(mul(a, 1.0 / n))
}

/// A right-handed frame (AXIS2_PLACEMENT_3D).
#[derive(Debug, Clone, Copy)]
struct Frame {
    o: V3,
    x: V3,
    y: V3,
    z: V3,
}

impl Frame {
    fn at(&self, a: f64, b: f64, c: f64) -> V3 {
        add(self.o, add(mul(self.x, a), add(mul(self.y, b), mul(self.z, c))))
    }
    fn local(&self, p: V3) -> V3 {
        let q = sub(p, self.o);
        [dot(q, self.x), dot(q, self.y), dot(q, self.z)]
    }
    fn same(&self, o: &Frame) -> bool {
        [dist(self.o, o.o), dist(self.x, o.x), dist(self.z, o.z)].iter().all(|d| *d < 1e-9)
    }
}

// ---------------------------------------------------------------- B-splines ----

/// Non-uniform rational B-spline basis on one parameter.
#[derive(Debug, Clone)]
struct Basis {
    p: usize,
    n: usize,
    k: Vec<f64>,
}

impl Basis {
    fn new(p: usize, n: usize, mults: &[f64], knots: &[f64]) -> Result<Basis> {
        ensure!((1..=15).contains(&p), "B-spline degree {p} not supported");
        ensure!(mults.len() == knots.len(), "B-spline knot multiplicities and knots differ in length");
        let mut k = Vec::new();
        for (m, v) in mults.iter().zip(knots) {
            ensure!(*m >= 1.0 && m.fract() == 0.0, "bad knot multiplicity {m}");
            k.extend(std::iter::repeat_n(*v, *m as usize));
        }
        ensure!(k.len() == n + p + 1, "B-spline: {} knots for {n} control points of degree {p}", k.len());
        ensure!(k.windows(2).all(|w| w[0] <= w[1]) && k[p] < k[n], "B-spline knots not increasing");
        Ok(Basis { p, n, k })
    }
    fn dom(&self) -> (f64, f64) {
        (self.k[self.p], self.k[self.n])
    }
    /// First control index and the p + 1 basis values at t.
    fn eval(&self, t: f64) -> (usize, [f64; 16]) {
        let (p, k) = (self.p, &self.k);
        let t = t.clamp(k[p], k[self.n]);
        let s = if t >= k[self.n] {
            // last non-empty span
            let mut s = self.n - 1;
            while k[s] >= k[s + 1] {
                s -= 1;
            }
            s
        } else {
            let (mut lo, mut hi) = (p, self.n);
            while hi - lo > 1 {
                let mid = (lo + hi) / 2;
                if t < k[mid] { hi = mid } else { lo = mid }
            }
            lo
        };
        let mut nb = [0.0; 16];
        let (mut left, mut right) = ([0.0; 16], [0.0; 16]);
        nb[0] = 1.0;
        for j in 1..=p {
            left[j] = t - k[s + 1 - j];
            right[j] = k[s + j] - t;
            let mut saved = 0.0;
            for r in 0..j {
                let den = right[r + 1] + left[j - r];
                let tmp = if den != 0.0 { nb[r] / den } else { 0.0 };
                nb[r] = saved + right[r + 1] * tmp;
                saved = left[j - r] * tmp;
            }
            nb[j] = saved;
        }
        (s - p, nb)
    }
}

#[derive(Debug, Clone)]
struct BCurve {
    b: Basis,
    /// Homogeneous control points (x w, y w, z w, w).
    c: Vec<[f64; 4]>,
}

impl BCurve {
    fn eval(&self, t: f64) -> V3 {
        let (i0, nb) = self.b.eval(t);
        let mut h = [0.0; 4];
        for (r, w) in nb.iter().take(self.b.p + 1).enumerate() {
            for (hk, ck) in h.iter_mut().zip(&self.c[i0 + r]) {
                *hk += w * ck;
            }
        }
        [h[0] / h[3], h[1] / h[3], h[2] / h[3]]
    }
}

#[derive(Debug, Clone)]
struct BSurf {
    bu: Basis,
    bv: Basis,
    /// Homogeneous control points, index u * nv + v.
    c: Vec<[f64; 4]>,
}

impl BSurf {
    fn eval(&self, u: f64, v: f64) -> V3 {
        let (i0, nu) = self.bu.eval(u);
        let (j0, nv) = self.bv.eval(v);
        let mut h = [0.0; 4];
        for (a, wu) in nu.iter().take(self.bu.p + 1).enumerate() {
            let row = (i0 + a) * self.bv.n;
            for (b, wv) in nv.iter().take(self.bv.p + 1).enumerate() {
                let w = wu * wv;
                for (hk, ck) in h.iter_mut().zip(&self.c[row + j0 + b]) {
                    *hk += w * ck;
                }
            }
        }
        [h[0] / h[3], h[1] / h[3], h[2] / h[3]]
    }
}

// ---------------------------------------------------------------- geometry ----

#[derive(Debug, Clone)]
enum Curve {
    Line,
    Conic { f: Frame, a: f64, b: f64 },
    BSpline(BCurve),
    Poly(Vec<V3>),
}

#[derive(Debug, Clone)]
enum Surf {
    Plane(Frame),
    Cyl(Frame, f64),
    Cone(Frame, f64, f64),
    Sphere(Frame, f64),
    Torus(Frame, f64, f64),
    BSpline(BSurf),
}

const TAU: f64 = std::f64::consts::TAU;

impl Surf {
    fn eval(&self, uv: V2) -> V3 {
        let [u, v] = uv;
        match self {
            Surf::Plane(f) => f.at(u, v, 0.0),
            Surf::Cyl(f, r) => f.at(r * u.cos(), r * u.sin(), v),
            Surf::Cone(f, r, t) => {
                let rr = r + v * t;
                f.at(rr * u.cos(), rr * u.sin(), v)
            }
            Surf::Sphere(f, r) => f.at(r * v.cos() * u.cos(), r * v.cos() * u.sin(), r * v.sin()),
            Surf::Torus(f, big, r) => {
                let rr = big + r * v.cos();
                f.at(rr * u.cos(), rr * u.sin(), r * v.sin())
            }
            Surf::BSpline(s) => s.eval(u, v),
        }
    }
    fn period(&self) -> [Option<f64>; 2] {
        match self {
            Surf::Plane(_) | Surf::BSpline(_) => [None, None],
            Surf::Cyl(..) | Surf::Cone(..) | Surf::Sphere(..) => [Some(TAU), None],
            Surf::Torus(..) => [Some(TAU), Some(TAU)],
        }
    }
    fn dom(&self) -> Option<[(f64, f64); 2]> {
        match self {
            Surf::BSpline(s) => Some([s.bu.dom(), s.bv.dom()]),
            _ => None,
        }
    }
    /// Closed-form parameters of a point on an analytic surface.
    fn project(&self, p: V3) -> Option<V2> {
        Some(match self {
            Surf::Plane(f) => {
                let l = f.local(p);
                [l[0], l[1]]
            }
            Surf::Cyl(f, _) | Surf::Cone(f, ..) => {
                let l = f.local(p);
                [l[1].atan2(l[0]), l[2]]
            }
            Surf::Sphere(f, _) => {
                let l = f.local(p);
                [l[1].atan2(l[0]), l[2].atan2(l[0].hypot(l[1]))]
            }
            Surf::Torus(f, big, _) => {
                let l = f.local(p);
                [l[1].atan2(l[0]), l[2].atan2(l[0].hypot(l[1]) - big)]
            }
            Surf::BSpline(_) => return None,
        })
    }
    /// Partial derivatives by central differences (one-sided at a B-spline's domain edge).
    fn derivs(&self, uv: V2) -> (V3, V3) {
        let dom = self.dom();
        let d = |k: usize| -> V3 {
            let (lo, hi) = dom.map_or((uv[k] - 1.0, uv[k] + 1.0), |d| d[k]);
            let h = 1e-6 * dom.map_or(1.0, |d| d[k].1 - d[k].0);
            let (a, b) = ((uv[k] - h).max(lo), (uv[k] + h).min(hi));
            let (mut pa, mut pb) = (uv, uv);
            pa[k] = a;
            pb[k] = b;
            mul(sub(self.eval(pb), self.eval(pa)), 1.0 / (b - a))
        };
        (d(0), d(1))
    }
    /// Gauss-Newton closest point from `seed` (B-spline): parameters and distance.
    fn newton(&self, p: V3, seed: V2) -> (V2, f64) {
        let dom = self.dom().expect("newton is for B-splines");
        let clamp = |uv: V2| [uv[0].clamp(dom[0].0, dom[0].1), uv[1].clamp(dom[1].0, dom[1].1)];
        let mut uv = clamp(seed);
        let mut d = dist(self.eval(uv), p);
        for _ in 0..40 {
            let (su, sv) = self.derivs(uv);
            let r = sub(p, self.eval(uv));
            let (a, b, c) = (dot(su, su), dot(su, sv), dot(sv, sv));
            let det = a * c - b * b;
            if det <= 1e-300 {
                break;
            }
            let (ru, rv) = (dot(su, r), dot(sv, r));
            let step = [(c * ru - b * rv) / det, (a * rv - b * ru) / det];
            let mut s = 1.0;
            let mut improved = false;
            for _ in 0..12 {
                let cand = clamp([uv[0] + s * step[0], uv[1] + s * step[1]]);
                let dc = dist(self.eval(cand), p);
                if dc < d {
                    let moved = (cand[0] - uv[0]).abs() / (dom[0].1 - dom[0].0)
                        + (cand[1] - uv[1]).abs() / (dom[1].1 - dom[1].0);
                    uv = cand;
                    d = dc;
                    improved = moved > 1e-14;
                    break;
                }
                s *= 0.5;
            }
            if !improved {
                break;
            }
        }
        (uv, d)
    }
}

struct Model {
    e: HashMap<u64, Ent>,
}

/// A B-spline's base attributes (degree(s), control net, ...), knot attributes, and weights.
type BsplineAttrs = (Vec<P>, Vec<P>, Option<Vec<P>>);

impl Model {
    fn ent(&self, id: u64) -> Result<&Ent> {
        self.e.get(&id).with_context(|| format!("STEP: #{id} is referenced but not defined"))
    }
    fn kind(&self, id: u64) -> String {
        self.e
            .get(&id)
            .map_or("?".into(), |e| e.parts.iter().map(|p| p.0.as_str()).collect::<Vec<_>>().join("+"))
    }
    fn part(&self, id: u64, name: &str) -> Option<&[P]> {
        self.e.get(&id)?.parts.iter().find(|p| p.0 == name).map(|p| p.1.as_slice())
    }
    fn need(&self, id: u64, name: &str) -> Result<&[P]> {
        self.part(id, name).with_context(|| format!("STEP: #{id} is {}, expected {name}", self.kind(id)))
    }
    fn arg(a: &[P], i: usize, id: u64) -> Result<&P> {
        a.get(i).with_context(|| format!("STEP: #{id} has too few attributes"))
    }
    fn point(&self, id: u64) -> Result<V3> {
        let a = self.need(id, "CARTESIAN_POINT")?;
        let c = Self::arg(a, 1, id)?.nums()?;
        ensure!(c.len() == 3, "STEP: #{id} is a {}D point, expected 3D", c.len());
        Ok([c[0], c[1], c[2]])
    }
    fn dir(&self, id: u64) -> Result<V3> {
        let a = self.need(id, "DIRECTION")?;
        let c = Self::arg(a, 1, id)?.nums()?;
        ensure!(c.len() == 3, "STEP: #{id} is a {}D direction", c.len());
        unit([c[0], c[1], c[2]]).with_context(|| format!("STEP: #{id}"))
    }
    fn frame(&self, id: u64) -> Result<Frame> {
        let a = self.need(id, "AXIS2_PLACEMENT_3D")?;
        let o = self.point(Self::arg(a, 1, id)?.r()?)?;
        let z = match Self::arg(a, 2, id)? {
            P::Null => [0.0, 0.0, 1.0],
            p => self.dir(p.r()?)?,
        };
        let r = match a.get(3) {
            Some(P::Ref(r)) => self.dir(*r)?,
            _ => [1.0, 0.0, 0.0],
        };
        let mut x = sub(r, mul(z, dot(r, z)));
        if norm(x) < 1e-9 {
            x = if z[0].abs() < 0.9 { [1.0, 0.0, 0.0] } else { [0.0, 1.0, 0.0] };
            x = sub(x, mul(z, dot(x, z)));
        }
        let x = unit(x)?;
        Ok(Frame { o, x, y: cross(z, x), z })
    }
    fn vertex(&self, id: u64) -> Result<V3> {
        let a = self.need(id, "VERTEX_POINT")?;
        self.point(Self::arg(a, 1, id)?.r()?)
    }

    /// Control net + knots of a B-spline curve or surface, simple or complex (rational) form.
    fn bspline_parts(&self, id: u64, surf: bool) -> Result<BsplineAttrs> {
        let (base, knots, rat) = if surf {
            ("B_SPLINE_SURFACE", "B_SPLINE_SURFACE_WITH_KNOTS", "RATIONAL_B_SPLINE_SURFACE")
        } else {
            ("B_SPLINE_CURVE", "B_SPLINE_CURVE_WITH_KNOTS", "RATIONAL_B_SPLINE_CURVE")
        };
        let nbase = if surf { 7 } else { 5 };
        let k = self.part(id, knots).with_context(|| {
            format!("STEP: #{id} is {} (only B-splines with knots are read: export STL)", self.kind(id))
        })?;
        if let Some(b) = self.part(id, base) {
            // complex instance: attributes split across parts, name in REPRESENTATION_ITEM
            let w = self.part(id, rat).map(|r| r.to_vec());
            Ok((b.to_vec(), k.to_vec(), w))
        } else {
            ensure!(k.len() > nbase, "STEP: #{id} {knots} has too few attributes");
            Ok((k[1..=nbase].to_vec(), k[nbase + 1..].to_vec(), None))
        }
    }

    fn curve(&self, id: u64) -> Result<Curve> {
        let e = self.ent(id)?;
        let name = e.parts[0].0.as_str();
        if let Some(a) = self.part(id, "LINE") {
            let _ = a;
            return Ok(Curve::Line);
        }
        for k in ["SURFACE_CURVE", "SEAM_CURVE", "INTERSECTION_CURVE", "BOUNDED_SURFACE_CURVE"] {
            if let Some(a) = self.part(id, k) {
                return self.curve(Self::arg(a, 1, id)?.r()?);
            }
        }
        if let Some(a) = self.part(id, "TRIMMED_CURVE") {
            return self.curve(Self::arg(a, 1, id)?.r()?);
        }
        if let Some(a) = self.part(id, "CIRCLE") {
            let r = Self::arg(a, 2, id)?.f()?;
            return Ok(Curve::Conic { f: self.frame(Self::arg(a, 1, id)?.r()?)?, a: r, b: r });
        }
        if let Some(a) = self.part(id, "ELLIPSE") {
            return Ok(Curve::Conic {
                f: self.frame(Self::arg(a, 1, id)?.r()?)?,
                a: Self::arg(a, 2, id)?.f()?,
                b: Self::arg(a, 3, id)?.f()?,
            });
        }
        if let Some(a) = self.part(id, "POLYLINE") {
            let pts =
                Self::arg(a, 1, id)?.refs()?.into_iter().map(|r| self.point(r)).collect::<Result<_>>()?;
            return Ok(Curve::Poly(pts));
        }
        if name.starts_with("B_SPLINE") || self.part(id, "B_SPLINE_CURVE").is_some() {
            let (b, k, w) = self.bspline_parts(id, false)?;
            let p = Self::arg(&b, 0, id)?.f()? as usize;
            let pts: Vec<V3> =
                Self::arg(&b, 1, id)?.refs()?.into_iter().map(|r| self.point(r)).collect::<Result<_>>()?;
            let ws = match &w {
                Some(w) => Self::arg(w, 0, id)?.nums()?,
                None => vec![1.0; pts.len()],
            };
            ensure!(ws.len() == pts.len(), "STEP: #{id} weights and control points differ");
            let basis =
                Basis::new(p, pts.len(), &Self::arg(&k, 0, id)?.nums()?, &Self::arg(&k, 1, id)?.nums()?)
                    .with_context(|| format!("STEP: #{id}"))?;
            let c = pts.iter().zip(&ws).map(|(q, w)| [q[0] * w, q[1] * w, q[2] * w, *w]).collect();
            return Ok(Curve::BSpline(BCurve { b: basis, c }));
        }
        bail!("STEP: edge curve #{id} is {} (not read: export STL from your CAD)", self.kind(id))
    }

    fn surface(&self, id: u64) -> Result<Surf> {
        if let Some(a) = self.part(id, "PLANE") {
            return Ok(Surf::Plane(self.frame(Self::arg(a, 1, id)?.r()?)?));
        }
        if let Some(a) = self.part(id, "CYLINDRICAL_SURFACE") {
            return Ok(Surf::Cyl(self.frame(Self::arg(a, 1, id)?.r()?)?, Self::arg(a, 2, id)?.f()?));
        }
        if let Some(a) = self.part(id, "CONICAL_SURFACE") {
            let ang = Self::arg(a, 3, id)?.f()? * self.angle_unit()?;
            return Ok(Surf::Cone(
                self.frame(Self::arg(a, 1, id)?.r()?)?,
                Self::arg(a, 2, id)?.f()?,
                ang.tan(),
            ));
        }
        if let Some(a) = self.part(id, "SPHERICAL_SURFACE") {
            return Ok(Surf::Sphere(self.frame(Self::arg(a, 1, id)?.r()?)?, Self::arg(a, 2, id)?.f()?));
        }
        if let Some(a) = self.part(id, "TOROIDAL_SURFACE") {
            return Ok(Surf::Torus(
                self.frame(Self::arg(a, 1, id)?.r()?)?,
                Self::arg(a, 2, id)?.f()?,
                Self::arg(a, 3, id)?.f()?,
            ));
        }
        if self.part(id, "B_SPLINE_SURFACE_WITH_KNOTS").is_some()
            || self.part(id, "B_SPLINE_SURFACE").is_some()
        {
            let (b, k, w) = self.bspline_parts(id, true)?;
            let (pu, pv) = (Self::arg(&b, 0, id)?.f()? as usize, Self::arg(&b, 1, id)?.f()? as usize);
            let rows = Self::arg(&b, 2, id)?.list()?;
            let nu = rows.len();
            let nv = Self::arg(rows, 0, id)?.list()?.len();
            let wrows: Option<Vec<Vec<f64>>> = match &w {
                Some(w) => Some(Self::arg(w, 0, id)?.list()?.iter().map(P::nums).collect::<Result<_>>()?),
                None => None,
            };
            let mut c = Vec::with_capacity(nu * nv);
            for (i, row) in rows.iter().enumerate() {
                let r = row.refs()?;
                ensure!(r.len() == nv, "STEP: #{id} control net is not rectangular");
                for (j, pid) in r.into_iter().enumerate() {
                    let q = self.point(pid)?;
                    let wt = wrows.as_ref().map_or(Ok(1.0), |w| {
                        w.get(i).and_then(|r| r.get(j)).copied().context("weights do not match the net")
                    })?;
                    c.push([q[0] * wt, q[1] * wt, q[2] * wt, wt]);
                }
            }
            let bu = Basis::new(pu, nu, &Self::arg(&k, 0, id)?.nums()?, &Self::arg(&k, 2, id)?.nums()?)
                .with_context(|| format!("STEP: #{id} (u)"))?;
            let bv = Basis::new(pv, nv, &Self::arg(&k, 1, id)?.nums()?, &Self::arg(&k, 3, id)?.nums()?)
                .with_context(|| format!("STEP: #{id} (v)"))?;
            return Ok(Surf::BSpline(BSurf { bu, bv, c }));
        }
        bail!("STEP: face surface #{id} is {} (not read: export STL from your CAD)", self.kind(id))
    }

    /// Value of a unit in base units (metres for length, radians for angle).
    fn unit_value(&self, id: u64, kind: &str) -> Result<f64> {
        if let Some(a) = self.part(id, "SI_UNIT") {
            let pre = match Self::arg(a, 0, id)? {
                P::Enum(e) => match e.as_str() {
                    "MILLI" => 1e-3,
                    "CENTI" => 1e-2,
                    "DECI" => 1e-1,
                    "KILO" => 1e3,
                    "MICRO" => 1e-6,
                    e => bail!("STEP: unit prefix {e} not read"),
                },
                _ => 1.0,
            };
            return Ok(pre);
        }
        if let Some(a) = self.part(id, "CONVERSION_BASED_UNIT") {
            let m = Self::arg(a, 1, id)?.r()?;
            let ma = self.ent(m)?.parts.iter().find(|p| p.0.ends_with("MEASURE_WITH_UNIT")).map(|p| &p.1);
            let ma = ma.with_context(|| format!("STEP: unit #{id}: #{m} is not a measure with unit"))?;
            return Ok(Self::arg(ma, 0, m)?.f()? * self.unit_value(Self::arg(ma, 1, m)?.r()?, kind)?);
        }
        bail!("STEP: {kind} unit #{id} is {} (not read)", self.kind(id))
    }

    fn units_of(&self, kind: &str) -> Result<Vec<(u64, f64)>> {
        let mut out = Vec::new();
        for e in self.e.values() {
            if let Some(p) = e.parts.iter().find(|p| p.0 == "GLOBAL_UNIT_ASSIGNED_CONTEXT") {
                for r in Self::arg(&p.1, 0, 0)?.refs()? {
                    if self.part(r, kind).is_some() {
                        out.push((r, self.unit_value(r, kind)?));
                    }
                }
            }
        }
        Ok(out)
    }

    fn angle_unit(&self) -> Result<f64> {
        let u = self.units_of("PLANE_ANGLE_UNIT")?;
        Ok(u.first().map_or(1.0, |x| x.1))
    }

    /// Points along an edge from its start vertex to its end vertex, about `h` apart.
    fn edge_points(&self, id: u64, h: f64, tol: f64) -> Result<Vec<V3>> {
        let a = self.need(id, "EDGE_CURVE")?;
        let (va, vb) = (self.vertex(Self::arg(a, 1, id)?.r()?)?, self.vertex(Self::arg(a, 2, id)?.r()?)?);
        let cid = Self::arg(a, 3, id)?.r()?;
        let sense = Self::arg(a, 4, id)?.b()?;
        let closed = dist(va, vb) <= tol;
        let sample = |f: &dyn Fn(f64) -> V3, t0: f64, t1: f64| -> Vec<V3> {
            let probe: Vec<V3> = (0..=64).map(|i| f(t0 + (t1 - t0) * i as f64 / 64.0)).collect();
            let len: f64 = probe.windows(2).map(|w| dist(w[0], w[1])).sum();
            let n = ((len / h).ceil() as usize).clamp(8, 20000);
            let mut v: Vec<V3> = (0..=n).map(|i| f(t0 + (t1 - t0) * i as f64 / n as f64)).collect();
            v[0] = va;
            v[n] = vb;
            v
        };
        let on = |p: V3, q: V3, what: &str| -> Result<()> {
            ensure!(
                dist(p, q) <= tol,
                "STEP: edge #{id}: its {what} vertex is {:.4} off the curve (more than {tol:.4})",
                dist(p, q)
            );
            Ok(())
        };
        Ok(match self.curve(cid)? {
            Curve::Line => vec![va, vb],
            Curve::Poly(pts) => {
                let mut pts = pts;
                if dist(pts[0], va) > tol {
                    pts.reverse();
                }
                on(pts[0], va, "start")?;
                on(pts[pts.len() - 1], vb, "end")?;
                let n = pts.len();
                pts[0] = va;
                pts[n - 1] = vb;
                pts
            }
            Curve::Conic { f, a: ra, b: rb } => {
                let ang = |p: V3| {
                    let l = f.local(p);
                    (l[1] / rb).atan2(l[0] / ra)
                };
                let (t0, mut t1) = (ang(va), ang(vb));
                if sense {
                    if t1 <= t0 + 1e-12 {
                        t1 += TAU;
                    }
                } else if t1 >= t0 - 1e-12 {
                    t1 -= TAU;
                }
                let g = |t: f64| f.at(ra * t.cos(), rb * t.sin(), 0.0);
                on(g(t0), va, "start")?;
                on(g(t1), vb, "end")?;
                sample(&g, t0, t1)
            }
            Curve::BSpline(c) => {
                let (d0, d1) = c.b.dom();
                let proj = |p: V3| -> (f64, f64) {
                    let n = 400;
                    let (mut bi, mut bd) = (0usize, f64::INFINITY);
                    for i in 0..=n {
                        let d = dist(c.eval(d0 + (d1 - d0) * i as f64 / n as f64), p);
                        if d < bd {
                            (bi, bd) = (i, d);
                        }
                    }
                    let (mut lo, mut hi) = (
                        d0 + (d1 - d0) * bi.saturating_sub(1) as f64 / n as f64,
                        d0 + (d1 - d0) * (bi + 1).min(n) as f64 / n as f64,
                    );
                    for _ in 0..80 {
                        let (m1, m2) = (lo + (hi - lo) / 3.0, hi - (hi - lo) / 3.0);
                        if dist(c.eval(m1), p) <= dist(c.eval(m2), p) { hi = m2 } else { lo = m1 }
                    }
                    let t = 0.5 * (lo + hi);
                    (t, dist(c.eval(t), p))
                };
                let (t0, t1) = if closed {
                    if sense { (d0, d1) } else { (d1, d0) }
                } else {
                    let ((t0, e0), (t1, e1)) = (proj(va), proj(vb));
                    ensure!(
                        e0 <= tol && e1 <= tol,
                        "STEP: edge #{id}: a vertex is {:.4} off its curve",
                        e0.max(e1)
                    );
                    (t0, t1)
                };
                sample(&|t| c.eval(t), t0, t1)
            }
        })
    }
}

// ---------------------------------------------------------------- 2D helpers ----

fn cross2(a: V2, b: V2, c: V2) -> f64 {
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
}

fn area2(p: &[V2]) -> f64 {
    (0..p.len())
        .map(|i| {
            let (a, b) = (p[i], p[(i + 1) % p.len()]);
            a[0] * b[1] - b[0] * a[1]
        })
        .sum::<f64>()
        * 0.5
}

/// Ear clipping of a simple polygon (CCW): triangles as index triples. None if it is not simple.
fn ear_clip(p: &[V2]) -> Option<Vec<[usize; 3]>> {
    let n = p.len();
    if n < 3 {
        return Some(vec![]);
    }
    let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
    for q in p {
        for k in 0..2 {
            lo[k] = lo[k].min(q[k]);
            hi[k] = hi[k].max(q[k]);
        }
    }
    let eps = 1e-13 * (hi[0] - lo[0]) * (hi[1] - lo[1]);
    let mut idx: Vec<usize> = (0..n).collect();
    let mut out = Vec::with_capacity(n);
    while idx.len() > 3 {
        let m = idx.len();
        let mut cut = None;
        for k in 0..m {
            let (a, b, c) = (idx[(k + m - 1) % m], idx[k], idx[(k + 1) % m]);
            if cross2(p[a], p[b], p[c]) <= eps {
                continue;
            }
            let blocked = idx.iter().any(|&q| {
                let pq = p[q];
                q != a
                    && q != b
                    && q != c
                    && pq != p[a]
                    && pq != p[b]
                    && pq != p[c]
                    && cross2(p[a], p[b], pq) >= -eps
                    && cross2(p[b], p[c], pq) >= -eps
                    && cross2(p[c], p[a], pq) >= -eps
            });
            if !blocked {
                cut = Some(k);
                out.push([a, b, c]);
                break;
            }
        }
        match cut {
            Some(k) => {
                idx.remove(k);
            }
            None => {
                // only flat (collinear or doubled-back) corners left to remove
                let k = (0..m).find(|&k| {
                    cross2(p[idx[(k + m - 1) % m]], p[idx[k]], p[idx[(k + 1) % m]]).abs() <= eps
                })?;
                idx.remove(k);
            }
        }
    }
    if idx.len() == 3 && cross2(p[idx[0]], p[idx[1]], p[idx[2]]) > eps {
        out.push([idx[0], idx[1], idx[2]]);
    }
    Some(out)
}

// ---------------------------------------------------------------- faces ----

/// A boundary point: parameters, position, and whether it lies on a grid line.
#[derive(Clone, Copy, Debug)]
struct Bp {
    uv: V2,
    p: V3,
    on: bool,
}

struct Tess {
    /// Cell size, edge spacing, chord sag and distance tolerances (model units).
    cell: f64,
    tol: f64,
    sag: f64,
}

fn face_triangles(
    m: &Model,
    fid: u64,
    flip: bool,
    edges: &HashMap<u64, Vec<V3>>,
    t: &Tess,
) -> Result<Vec<[V3; 3]>> {
    let e = m.ent(fid)?;
    let (_, a) = e
        .parts
        .iter()
        .find(|p| p.0 == "ADVANCED_FACE" || p.0 == "FACE_SURFACE")
        .with_context(|| format!("STEP: #{fid} is not a face"))?;
    let bounds = Model::arg(a, 1, fid)?.refs()?;
    let surf = m.surface(Model::arg(a, 2, fid)?.r()?).with_context(|| format!("face #{fid}"))?;
    let same = Model::arg(a, 3, fid)?.b()?;
    // Boundary loops in 3D, with where each edge starts.
    let mut loops3: Vec<Vec<Vec<V3>>> = Vec::new();
    let mut seen = HashSet::new();
    for b in bounds {
        let ba = m
            .part(b, "FACE_OUTER_BOUND")
            .or_else(|| m.part(b, "FACE_BOUND"))
            .with_context(|| format!("STEP: face #{fid}: bound #{b} is {}", m.kind(b)))?;
        let lid = Model::arg(ba, 1, b)?.r()?;
        let border = Model::arg(ba, 2, b)?.b()?;
        let mut runs: Vec<Vec<V3>> = Vec::new();
        if let Some(la) = m.part(lid, "EDGE_LOOP") {
            for oe in Model::arg(la, 1, lid)?.refs()? {
                let oa = m.need(oe, "ORIENTED_EDGE")?;
                let ec = Model::arg(oa, 3, oe)?.r()?;
                ensure!(
                    seen.insert(ec),
                    "STEP: face #{fid} uses edge #{ec} twice (a seam on a closed surface): not read, export STL"
                );
                let mut pts = edges.get(&ec).with_context(|| format!("edge #{ec} not sampled"))?.clone();
                if !Model::arg(oa, 4, oe)?.b()? {
                    pts.reverse();
                }
                runs.push(pts);
            }
        } else if let Some(la) = m.part(lid, "POLY_LOOP") {
            let mut pts: Vec<V3> =
                Model::arg(la, 1, lid)?.refs()?.into_iter().map(|r| m.point(r)).collect::<Result<_>>()?;
            pts.push(pts[0]);
            for w in pts.windows(2) {
                runs.push(w.to_vec());
            }
        } else if m.part(lid, "VERTEX_LOOP").is_some() {
            continue; // a degenerate point bound (cone apex): nothing to trim
        } else {
            bail!("STEP: face #{fid}: loop #{lid} is {}", m.kind(lid));
        }
        if !border {
            runs.reverse();
            for r in &mut runs {
                r.reverse();
            }
        }
        loops3.push(runs);
    }
    ensure!(!loops3.is_empty(), "STEP: face #{fid} has no boundary");
    // Parameters of every boundary point.
    let period = surf.period();
    let seeds: Vec<(V2, V3)> = match surf.dom() {
        Some(d) => {
            let n = 32;
            (0..=n)
                .flat_map(|i| (0..=n).map(move |j| (i, j)))
                .map(|(i, j)| {
                    let uv = [
                        d[0].0 + (d[0].1 - d[0].0) * i as f64 / n as f64,
                        d[1].0 + (d[1].1 - d[1].0) * j as f64 / n as f64,
                    ];
                    (uv, surf.eval(uv))
                })
                .collect()
        }
        None => Vec::new(),
    };
    let unwrap = |uv: V2, near: V2| -> V2 {
        let mut o = uv;
        for k in 0..2 {
            if let Some(tp) = period[k] {
                o[k] += ((near[k] - o[k]) / tp).round() * tp;
            }
        }
        o
    };
    let solve = |p: V3, pred: Option<V2>| -> Result<V2> {
        if let Some(uv) = surf.project(p) {
            let uv = pred.map_or(uv, |n| unwrap(uv, n));
            let d = dist(surf.eval(uv), p);
            ensure!(d <= t.tol, "STEP: face #{fid}: a boundary point is {d:.4} off its surface");
            return Ok(uv);
        }
        let mut best = pred.map(|s| surf.newton(p, s));
        if best.is_none_or(|b| b.1 > 0.1 * t.tol) {
            let mut near: Vec<(f64, V2)> = seeds.iter().map(|(uv, q)| (dist(*q, p), *uv)).collect();
            near.sort_by(|a, b| a.0.total_cmp(&b.0));
            for (_, s) in near.iter().take(4) {
                let c = surf.newton(p, *s);
                if best.is_none_or(|b| c.1 < b.1) {
                    best = Some(c);
                }
            }
        }
        let (uv, d) = best.expect("seeded");
        ensure!(
            d <= t.tol,
            "STEP: face #{fid}: a boundary point is {d:.4} off its surface (more than {:.4})",
            t.tol
        );
        Ok(uv)
    };
    let mut loops: Vec<Vec<Bp>> = Vec::new();
    for runs in &loops3 {
        let mut lp: Vec<Bp> = Vec::new();
        for run in runs {
            let n = run.len();
            let mut uv = vec![[0.0; 2]; n];
            let mid = n / 2;
            uv[mid] = solve(run[mid], lp.last().map(|b| b.uv))?;
            for i in mid + 1..n {
                let pred = if i >= mid + 2 {
                    [2.0 * uv[i - 1][0] - uv[i - 2][0], 2.0 * uv[i - 1][1] - uv[i - 2][1]]
                } else {
                    uv[i - 1]
                };
                uv[i] = solve(run[i], Some(pred))?;
            }
            for i in (0..mid).rev() {
                let pred = if i + 2 <= mid {
                    [2.0 * uv[i + 1][0] - uv[i + 2][0], 2.0 * uv[i + 1][1] - uv[i + 2][1]]
                } else {
                    uv[i + 1]
                };
                uv[i] = solve(run[i], Some(pred))?;
            }
            if let Some(last) = lp.last() {
                let s = unwrap(uv[0], last.uv);
                let sh = [s[0] - uv[0][0], s[1] - uv[0][1]];
                for q in &mut uv {
                    *q = [q[0] + sh[0], q[1] + sh[1]];
                }
            }
            for (k, (q, p)) in uv.iter().zip(run).enumerate() {
                let dup = k == 0
                    && lp
                        .last()
                        .is_some_and(|b| b.p == *p && (b.uv[0] - q[0]).abs() + (b.uv[1] - q[1]).abs() < 1e-9);
                if !dup {
                    lp.push(Bp { uv: *q, p: *p, on: false });
                }
            }
        }
        if lp.len() > 1 {
            let (f, l) = (lp[0], lp[lp.len() - 1]);
            for (k, per) in period.iter().enumerate() {
                if let Some(tp) = per {
                    ensure!(
                        (f.uv[k] - l.uv[k]).abs() < 0.5 * tp,
                        "STEP: face #{fid} wraps all the way round its surface (no seam edge): not read, export STL"
                    );
                }
            }
            if f.p == l.p && (f.uv[0] - l.uv[0]).abs() + (f.uv[1] - l.uv[1]).abs() < 1e-9 {
                lp.pop();
            }
        }
        // Split chords that leave the surface.
        let mut out: Vec<Bp> = Vec::with_capacity(lp.len());
        for i in 0..lp.len() {
            let (a, b) = (lp[i], lp[(i + 1) % lp.len()]);
            out.push(a);
            let mut stack = vec![(a, b, 0)];
            let mut ins = Vec::new();
            while let Some((a, b, depth)) = stack.pop() {
                let uv = [0.5 * (a.uv[0] + b.uv[0]), 0.5 * (a.uv[1] + b.uv[1])];
                let s = surf.eval(uv);
                if depth < 14 && dist(s, lerp(a.p, b.p, 0.5)) > t.sag {
                    let mp = Bp { uv, p: s, on: false };
                    stack.push((mp, b, depth + 1));
                    stack.push((a, mp, depth + 1));
                } else {
                    ins.push(b);
                }
            }
            ins.pop(); // b itself
            out.extend(ins);
        }
        if out.len() >= 3 {
            loops.push(out);
        }
    }
    ensure!(!loops.is_empty(), "STEP: face #{fid} has an empty boundary");
    // Outer loop counter-clockwise in (u, v), holes clockwise.
    let areas: Vec<f64> = loops.iter().map(|l| area2(&l.iter().map(|b| b.uv).collect::<Vec<_>>())).collect();
    let outer = (0..loops.len()).max_by(|&a, &b| areas[a].abs().total_cmp(&areas[b].abs())).unwrap();
    for (k, l) in loops.iter_mut().enumerate() {
        if (k == outer) != (areas[k] > 0.0) {
            l.reverse();
        }
    }
    // Facing: the (u, v) normal of every surface here is du x dv; flip for a reversed face.
    let reverse = !same ^ flip;
    let orient = |tri: [V3; 3]| if reverse { [tri[0], tri[2], tri[1]] } else { tri };
    let mut tris = Vec::new();
    // A plane with one short boundary: clip it directly.
    if matches!(surf, Surf::Plane(_)) && loops.len() == 1 && loops[0].len() <= 64 {
        let l = &loops[0];
        let uv: Vec<V2> = l.iter().map(|b| b.uv).collect();
        let ix =
            ear_clip(&uv).with_context(|| format!("STEP: face #{fid}: boundary is not a simple polygon"))?;
        return Ok(ix.into_iter().map(|[a, b, c]| orient([l[a].p, l[b].p, l[c].p])).collect());
    }
    // Grid of cells about `cell` across in 3D, offset so no boundary point sits on a grid line.
    let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
    for b in loops.iter().flatten() {
        for k in 0..2 {
            lo[k] = lo[k].min(b.uv[k]);
            hi[k] = hi[k].max(b.uv[k]);
        }
    }
    ensure!(hi[0] > lo[0] && hi[1] > lo[1], "STEP: face #{fid} has no area");
    let mut speed = [1e-300f64; 2];
    for i in 0..=8 {
        for j in 0..=8 {
            let uv = [lo[0] + (hi[0] - lo[0]) * i as f64 / 8.0, lo[1] + (hi[1] - lo[1]) * j as f64 / 8.0];
            let (su, sv) = surf.derivs(uv);
            speed[0] = speed[0].max(norm(su));
            speed[1] = speed[1].max(norm(sv));
        }
    }
    let mut du = [t.cell / speed[0], t.cell / speed[1]];
    for l in &loops {
        for (k, d) in du.iter_mut().enumerate() {
            let (a, b) = l
                .iter()
                .fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), p| (a.min(p.uv[k]), b.max(p.uv[k])));
            if b > a {
                *d = d.min(0.4 * (b - a));
            }
        }
    }
    let n = [((hi[0] - lo[0]) / du[0]).ceil() as usize + 2, ((hi[1] - lo[1]) / du[1]).ceil() as usize + 2];
    ensure!(
        n[0].saturating_mul(n[1]) <= 6_000_000,
        "STEP: face #{fid} needs a {} x {} grid (a tiny trim loop on a large face): export STL",
        n[0],
        n[1]
    );
    let mut g0 = [0.0; 2];
    let mut ok = false;
    for frac in [0.381966, 0.618034, 0.27183, 0.71828, 0.14159, 0.5532] {
        g0 = [lo[0] - frac * du[0], lo[1] - frac * du[1]];
        ok = loops.iter().flatten().all(|b| {
            (0..2).all(|k| {
                let f = ((b.uv[k] - g0[k]) / du[k]).fract();
                f > 1e-9 && f < 1.0 - 1e-9
            })
        });
        if ok {
            break;
        }
    }
    ensure!(ok, "STEP: face #{fid}: could not lay a grid clear of its boundary points");
    let gl = |k: usize, i: i64| g0[k] + i as f64 * du[k];
    let ci = |k: usize, x: f64| ((x - g0[k]) / du[k]).floor() as i64;
    // Insert the boundary's crossings with the grid lines.
    let mut split: Vec<Vec<Bp>> = Vec::new();
    for l in &loops {
        let mut out = Vec::with_capacity(l.len() * 2);
        for i in 0..l.len() {
            let (a, b) = (l[i], l[(i + 1) % l.len()]);
            out.push(a);
            let mut cr: Vec<(f64, usize, i64)> = Vec::new();
            for k in 0..2 {
                let (ia, ib) = (ci(k, a.uv[k]), ci(k, b.uv[k]));
                let lines: Vec<i64> =
                    if ib > ia { (ia + 1..=ib).collect() } else { (ib + 1..=ia).rev().collect() };
                for li in lines {
                    cr.push(((gl(k, li) - a.uv[k]) / (b.uv[k] - a.uv[k]), k, li));
                }
            }
            cr.sort_by(|x, y| x.0.total_cmp(&y.0));
            let mut j = 0;
            while j < cr.len() {
                let (s, k, li) = cr[j];
                let mut uv = [a.uv[0] + s * (b.uv[0] - a.uv[0]), a.uv[1] + s * (b.uv[1] - a.uv[1])];
                uv[k] = gl(k, li);
                // a crossing at a grid corner: both lines at once
                if j + 1 < cr.len() && cr[j + 1].0 - s < 1e-9 && cr[j + 1].1 != k {
                    uv[cr[j + 1].1] = gl(cr[j + 1].1, cr[j + 1].2);
                    j += 1;
                }
                out.push(Bp { uv, p: lerp(a.p, b.p, s), on: true });
                j += 1;
            }
        }
        split.push(out);
    }
    // Chains: runs of the boundary inside one cell, from a crossing to the next.
    struct Chain {
        pts: Vec<Bp>,
    }
    let mut cells: HashMap<(i64, i64), Vec<Chain>> = HashMap::new();
    for l in &split {
        let s = l
            .iter()
            .position(|b| b.on)
            .with_context(|| format!("STEP: face #{fid}: a boundary loop lies inside one grid cell"))?;
        let mut cur = vec![l[s]];
        for k in 1..=l.len() {
            let b = l[(s + k) % l.len()];
            cur.push(b);
            if b.on {
                let (p0, p1) = (cur[0].uv, cur[1].uv);
                let mid = [0.5 * (p0[0] + p1[0]), 0.5 * (p0[1] + p1[1])];
                if cur.len() > 2 || p0 != p1 {
                    cells
                        .entry((ci(0, mid[0]), ci(1, mid[1])))
                        .or_default()
                        .push(Chain { pts: std::mem::take(&mut cur) });
                }
                cur = vec![b];
            }
        }
    }
    // Inside test for untouched cells: scan each row's centre line.
    let segs: Vec<(V2, V2)> =
        loops.iter().flat_map(|l| (0..l.len()).map(move |i| (l[i].uv, l[(i + 1) % l.len()].uv))).collect();
    let (i_lo, i_hi, j_lo, j_hi) = (ci(0, lo[0]), ci(0, hi[0]), ci(1, lo[1]), ci(1, hi[1]));
    let mut corner: HashMap<(i64, i64), V3> = HashMap::new();
    let mut at =
        |i: i64, j: i64| -> V3 { *corner.entry((i, j)).or_insert_with(|| surf.eval([gl(0, i), gl(1, j)])) };
    for j in j_lo..=j_hi {
        let vc = gl(1, j) + 0.5 * du[1];
        let mut xs: Vec<f64> = segs
            .iter()
            .filter(|(a, b)| (a[1] < vc) != (b[1] < vc))
            .map(|(a, b)| a[0] + (vc - a[1]) / (b[1] - a[1]) * (b[0] - a[0]))
            .collect();
        xs.sort_by(f64::total_cmp);
        let mut c = 0;
        for i in i_lo..=i_hi {
            if cells.contains_key(&(i, j)) {
                continue;
            }
            let uc = gl(0, i) + 0.5 * du[0];
            while c < xs.len() && xs[c] < uc {
                c += 1;
            }
            if c % 2 == 1 {
                let (p00, p10, p11, p01) = (at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1));
                tris.push(orient([p00, p10, p11]));
                tris.push(orient([p00, p11, p01]));
            }
        }
    }
    // Cells the boundary crosses: walk boundary chains and the cell's sides (Weiler-Atherton).
    let mut keys: Vec<(i64, i64)> = cells.keys().copied().collect();
    keys.sort();
    for key in keys {
        let ch = &cells[&key];
        let (u0, u1, v0, v1) = (gl(0, key.0), gl(0, key.0 + 1), gl(1, key.1), gl(1, key.1 + 1));
        let (w, h) = (u1 - u0, v1 - v0);
        let perim = |p: V2| -> Result<f64> {
            Ok(if p[1] == v0 {
                (p[0] - u0) / w
            } else if p[0] == u1 {
                1.0 + (p[1] - v0) / h
            } else if p[1] == v1 {
                2.0 + (u1 - p[0]) / w
            } else if p[0] == u0 {
                3.0 + (v1 - p[1]) / h
            } else {
                bail!("STEP: face #{fid}: boundary crossing off its cell side (internal)")
            })
        };
        let ends: Vec<(f64, f64)> = ch
            .iter()
            .map(|c| Ok((perim(c.pts[0].uv)?, perim(c.pts[c.pts.len() - 1].uv)?)))
            .collect::<Result<_>>()?;
        let (i, j) = key;
        let corners = [
            (1.0, [u1, v0], (i + 1, j)),
            (2.0, [u1, v1], (i + 1, j + 1)),
            (3.0, [u0, v1], (i, j + 1)),
            (4.0, [u0, v0], (i, j)),
        ];
        let mut used = vec![false; ch.len()];
        for st in 0..ch.len() {
            if used[st] {
                continue;
            }
            let mut poly: Vec<Bp> = Vec::new();
            let mut c = st;
            let mut steps = 0;
            loop {
                used[c] = true;
                poly.extend(ch[c].pts.iter().copied());
                let te = ends[c].1;
                let nxt = (0..ch.len())
                    .filter(|&k| k == st || !used[k])
                    .min_by(|&a, &b| {
                        (ends[a].0 - te).rem_euclid(4.0).total_cmp(&(ends[b].0 - te).rem_euclid(4.0))
                    })
                    .unwrap();
                let span = (ends[nxt].0 - te).rem_euclid(4.0);
                let mut cs: Vec<(f64, V2, (i64, i64))> = corners
                    .iter()
                    .map(|(t, uv, g)| ((t - te).rem_euclid(4.0), *uv, *g))
                    .filter(|(d, ..)| *d > 0.0 && *d < span)
                    .collect();
                cs.sort_by(|a, b| a.0.total_cmp(&b.0));
                for (_, uv, g) in cs {
                    poly.push(Bp { uv, p: at(g.0, g.1), on: true });
                }
                steps += 1;
                ensure!(
                    steps <= ch.len(),
                    "STEP: face #{fid}: boundary does not close in a cell (self-crossing trim?)"
                );
                if nxt == st {
                    break;
                }
                c = nxt;
            }
            poly.dedup_by(|a, b| a.uv == b.uv);
            while poly.len() > 1 && poly[0].uv == poly[poly.len() - 1].uv {
                poly.pop();
            }
            let uv: Vec<V2> = poly.iter().map(|b| b.uv).collect();
            let ix = ear_clip(&uv)
                .with_context(|| format!("STEP: face #{fid}: trimmed cell is not a simple polygon"))?;
            tris.extend(ix.into_iter().map(|[a, b, c]| orient([poly[a].p, poly[b].p, poly[c].p])));
        }
    }
    Ok(tris)
}

/// Triangles of every face in a STEP file (its own units).
pub fn parse_step(text: &str) -> Result<StepModel> {
    let m = Model { e: parse_p21(text)? };
    // Placed parts would need their transforms: refuse rather than misplace them.
    for (id, e) in &m.e {
        if let Some(p) = e.parts.iter().find(|p| p.0 == "ITEM_DEFINED_TRANSFORMATION") {
            let (a, b) = (m.frame(Model::arg(&p.1, 2, *id)?.r()?)?, m.frame(Model::arg(&p.1, 3, *id)?.r()?)?);
            ensure!(
                a.same(&b),
                "STEP: an assembly that places parts (#{id}): export the canoe as a single part, or STL"
            );
        }
        ensure!(
            e.parts[0].0 != "MAPPED_ITEM",
            "STEP: mapped (instanced) geometry #{id}: export the canoe as a single part, or STL"
        );
    }
    // Length unit.
    let lens = m.units_of("LENGTH_UNIT")?;
    let units = match lens.first() {
        None => None,
        Some(&(id, metres)) => {
            ensure!(
                lens.iter().all(|(_, v)| (v / metres - 1.0).abs() < 1e-9),
                "STEP: the file uses more than one length unit"
            );
            let name = match m.part(id, "CONVERSION_BASED_UNIT") {
                Some(a) => match &a[0] {
                    P::Str(s) => s.to_lowercase(),
                    _ => "unit".into(),
                },
                None => match metres {
                    x if (x - 1e-3).abs() < 1e-12 => "mm".into(),
                    x if (x - 1e-2).abs() < 1e-12 => "cm".into(),
                    x if (x - 1.0).abs() < 1e-12 => "m".into(),
                    x => format!("{x} m"),
                },
            };
            Some((metres / 0.0254, name))
        }
    };
    // Faces: those of every shell (flipped inside a reversed oriented shell), else every face.
    let mut faces: Vec<(u64, bool)> = Vec::new();
    let mut flipped: HashSet<u64> = HashSet::new();
    for e in m.e.values() {
        for p in &e.parts {
            if (p.0 == "ORIENTED_CLOSED_SHELL" || p.0 == "ORIENTED_OPEN_SHELL")
                && !Model::arg(&p.1, 3, 0)?.b()?
            {
                flipped.insert(Model::arg(&p.1, 2, 0)?.r()?);
            }
        }
    }
    let mut shells: Vec<u64> =
        m.e.iter()
            .filter(|(_, e)| ["CLOSED_SHELL", "OPEN_SHELL"].contains(&e.parts[0].0.as_str()))
            .map(|(i, _)| *i)
            .collect();
    shells.sort();
    let mut seen = HashSet::new();
    for s in &shells {
        let a = &m.ent(*s)?.parts[0].1;
        for f in Model::arg(a, 1, *s)?.refs()? {
            if seen.insert(f) {
                faces.push((f, flipped.contains(s)));
            }
        }
    }
    if faces.is_empty() {
        let mut ids: Vec<u64> =
            m.e.iter()
                .filter(|(_, e)| ["ADVANCED_FACE", "FACE_SURFACE"].contains(&e.parts[0].0.as_str()))
                .map(|(i, _)| *i)
                .collect();
        ids.sort();
        faces = ids.into_iter().map(|f| (f, false)).collect();
    }
    if faces.is_empty() {
        let tess = m.e.values().any(|e| e.parts.iter().any(|p| p.0.contains("TRIANGULATED")));
        bail!(
            "STEP file has no B-rep faces{}: export STL from your CAD",
            if tess { " (only tessellated geometry)" } else { "" }
        );
    }
    // Sizes in model units: from inches when the unit is known, else from the model's extent.
    let per_in = match &units {
        Some((ipu, _)) => 1.0 / ipu,
        None => {
            let (mut lo, mut hi) = ([f64::INFINITY; 3], [f64::NEG_INFINITY; 3]);
            for (id, e) in &m.e {
                if e.parts[0].0 == "VERTEX_POINT" {
                    let p = m.vertex(*id)?;
                    for k in 0..3 {
                        lo[k] = lo[k].min(p[k]);
                        hi[k] = hi[k].max(p[k]);
                    }
                }
            }
            let diag = norm(sub(hi, lo));
            ensure!(diag.is_finite() && diag > 0.0, "STEP file has no vertices");
            diag / 240.0
        }
    };
    let t = Tess { cell: 0.5 * per_in, tol: 0.02 * per_in, sag: 0.005 * per_in };
    // Edges once each, shared by the faces on either side.
    let mut eids: Vec<u64> = Vec::new();
    let mut eseen = HashSet::new();
    for (f, _) in &faces {
        let a = &m
            .ent(*f)?
            .parts
            .iter()
            .find(|p| p.0 == "ADVANCED_FACE" || p.0 == "FACE_SURFACE")
            .map(|p| p.1.clone());
        let Some(a) = a else { bail!("STEP: #{f} is {}, expected a face", m.kind(*f)) };
        for b in Model::arg(a, 1, *f)?.refs()? {
            let ba = m.part(b, "FACE_OUTER_BOUND").or_else(|| m.part(b, "FACE_BOUND"));
            let Some(ba) = ba else { continue };
            let lid = Model::arg(ba, 1, b)?.r()?;
            if let Some(la) = m.part(lid, "EDGE_LOOP") {
                for oe in Model::arg(la, 1, lid)?.refs()? {
                    let ec = Model::arg(m.need(oe, "ORIENTED_EDGE")?, 3, oe)?.r()?;
                    if eseen.insert(ec) {
                        eids.push(ec);
                    }
                }
            }
        }
    }
    let edges: HashMap<u64, Vec<V3>> = eids
        .par_iter()
        .map(|&id| Ok((id, m.edge_points(id, 0.25 * per_in, t.tol)?)))
        .collect::<Result<_>>()?;
    let parts: Vec<Vec<[V3; 3]>> =
        faces.par_iter().map(|&(f, flip)| face_triangles(&m, f, flip, &edges, &t)).collect::<Result<_>>()?;
    let tris: Vec<[V3; 3]> = parts.into_iter().flatten().collect();
    ensure!(!tris.is_empty(), "STEP: no triangles");
    Ok(StepModel { tris, units, faces: faces.len() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::export::{ExportSpec, build_mesh, to_step};
    use crate::hull::Hull;
    use std::path::Path;

    fn spec(t: Option<f64>, pocket: Option<f64>) -> ExportSpec {
        ExportSpec {
            depth_in: 12.0,
            thickness_in: t,
            flare_deg: 0.0,
            gunwale_radius_in: None,
            stations: 161,
            girth: 41,
            pocket_in: pocket,
            layers: Some(2),
            filled: false,
        }
    }

    fn vol_area(t: &[[V3; 3]]) -> (f64, f64) {
        t.iter().fold((0.0, 0.0), |(v, a), [p, q, r]| {
            (v + dot(*p, cross(*q, *r)) / 6.0, a + 0.5 * norm(cross(sub(*q, *p), sub(*r, *p))))
        })
    }

    #[test]
    fn jaguar_step_solid_reads_back_with_its_volume() {
        let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        for pocket in [None, Some(30.0)] {
            let mesh = build_mesh(&hull, &spec(Some(0.375), pocket)).unwrap();
            let m = parse_step(&to_step(&mesh, "off1")).unwrap();
            assert_eq!(m.units.as_ref().unwrap().1, "inch");
            assert!((m.units.as_ref().unwrap().0 - 1.0).abs() < 1e-12);
            let (v, a) = vol_area(&m.tris);
            let facets: Vec<[V3; 3]> = mesh.tris.iter().map(|t| t.map(|i| mesh.verts[i as usize])).collect();
            let (vm, am) = vol_area(&facets);
            // The STEP B-spline is smoothed through its control net: within 0.5% of the facets.
            assert!((v / vm - 1.0).abs() < 0.005, "volume {v} vs mesh {vm} (pocket {pocket:?})");
            assert!((a / am - 1.0).abs() < 0.005, "area {a} vs mesh {am}");
        }
    }

    #[test]
    fn units_and_analytic_faces() {
        // A 10 mm cube with a cylindrical hole would need seams; a box is planes only. Here: one
        // planar square face in millimetres.
        let s = "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('x'),'2;1');\nENDSEC;\nDATA;\n\
#1=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));\n\
#2=(GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNIT_ASSIGNED_CONTEXT((#1)) REPRESENTATION_CONTEXT('',''));\n\
#10=CARTESIAN_POINT('',(0.,0.,0.));#11=CARTESIAN_POINT('',(10.,0.,0.));#12=CARTESIAN_POINT('',(10.,10.,0.));#13=CARTESIAN_POINT('',(0.,10.,0.));\n\
#20=VERTEX_POINT('',#10);#21=VERTEX_POINT('',#11);#22=VERTEX_POINT('',#12);#23=VERTEX_POINT('',#13);\n\
#30=DIRECTION('',(1.,0.,0.));#31=VECTOR('',#30,1.);#32=LINE('',#10,#31);\n\
#40=EDGE_CURVE('',#20,#21,#32,.T.);#41=EDGE_CURVE('',#21,#22,#32,.T.);#42=EDGE_CURVE('',#22,#23,#32,.T.);#43=EDGE_CURVE('',#23,#20,#32,.T.);\n\
#50=ORIENTED_EDGE('',*,*,#40,.T.);#51=ORIENTED_EDGE('',*,*,#41,.T.);#52=ORIENTED_EDGE('',*,*,#42,.T.);#53=ORIENTED_EDGE('',*,*,#43,.T.);\n\
#60=EDGE_LOOP('',(#50,#51,#52,#53));#61=FACE_OUTER_BOUND('',#60,.T.);\n\
#70=DIRECTION('',(0.,0.,1.));#71=AXIS2_PLACEMENT_3D('',#10,#70,#30);#72=PLANE('',#71);\n\
#80=ADVANCED_FACE('',(#61),#72,.T.);#81=OPEN_SHELL('',(#80));\nENDSEC;\nEND-ISO-10303-21;\n";
        let m = parse_step(s).unwrap();
        assert_eq!(m.units.as_ref().unwrap().1, "mm");
        assert!((m.units.as_ref().unwrap().0 - 1.0 / 25.4).abs() < 1e-12);
        assert_eq!(m.tris.len(), 2);
        let n: V3 = m.tris.iter().map(|[p, q, r]| cross(sub(*q, *p), sub(*r, *p))).fold([0.0; 3], add);
        assert!(n[2] > 0.0 && (0.5 * n[2] - 100.0).abs() < 1e-9, "{n:?}");
        // Reversed face: normal flips. A cylinder face is meshed on the cylinder.
        let rev = s.replace("#80=ADVANCED_FACE('',(#61),#72,.T.)", "#80=ADVANCED_FACE('',(#61),#72,.F.)");
        let m = parse_step(&rev).unwrap();
        let n: V3 = m.tris.iter().map(|[p, q, r]| cross(sub(*q, *p), sub(*r, *p))).fold([0.0; 3], add);
        assert!(n[2] < 0.0);
        assert!(parse_step("not step").is_err());
    }

    #[test]
    fn trimmed_cylinder_patch_is_on_the_cylinder() {
        // Quarter of a radius-20 in cylinder, 30 in tall, bounded by two arcs and two lines.
        let s = "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n\
#1=(CONVERSION_BASED_UNIT('INCH',#3) LENGTH_UNIT() NAMED_UNIT(*));\n\
#2=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));#3=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#2);\n\
#4=(GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNIT_ASSIGNED_CONTEXT((#1)) REPRESENTATION_CONTEXT('',''));\n\
#10=CARTESIAN_POINT('',(0.,0.,0.));#11=DIRECTION('',(0.,0.,1.));#12=DIRECTION('',(1.,0.,0.));\n\
#13=AXIS2_PLACEMENT_3D('',#10,#11,#12);#14=CYLINDRICAL_SURFACE('',#13,20.);\n\
#15=CARTESIAN_POINT('',(0.,0.,30.));#16=AXIS2_PLACEMENT_3D('',#15,#11,#12);\n\
#17=CIRCLE('',#13,20.);#18=CIRCLE('',#16,20.);\n\
#20=CARTESIAN_POINT('',(20.,0.,0.));#21=CARTESIAN_POINT('',(0.,20.,0.));#22=CARTESIAN_POINT('',(0.,20.,30.));#23=CARTESIAN_POINT('',(20.,0.,30.));\n\
#30=VERTEX_POINT('',#20);#31=VERTEX_POINT('',#21);#32=VERTEX_POINT('',#22);#33=VERTEX_POINT('',#23);\n\
#34=VECTOR('',#11,1.);#35=LINE('',#20,#34);\n\
#40=EDGE_CURVE('',#30,#31,#17,.T.);#41=EDGE_CURVE('',#31,#32,#35,.T.);#42=EDGE_CURVE('',#33,#32,#18,.T.);#43=EDGE_CURVE('',#30,#33,#35,.T.);\n\
#50=ORIENTED_EDGE('',*,*,#40,.T.);#51=ORIENTED_EDGE('',*,*,#41,.T.);#52=ORIENTED_EDGE('',*,*,#42,.F.);#53=ORIENTED_EDGE('',*,*,#43,.F.);\n\
#60=EDGE_LOOP('',(#50,#51,#52,#53));#61=FACE_OUTER_BOUND('',#60,.T.);\n\
#80=ADVANCED_FACE('',(#61),#14,.T.);#81=OPEN_SHELL('',(#80));\nENDSEC;\nEND-ISO-10303-21;\n";
        let m = parse_step(s).unwrap();
        assert_eq!(m.units.as_ref().unwrap().1, "inch");
        assert!((m.units.as_ref().unwrap().0 - 1.0).abs() < 1e-12);
        for p in m.tris.iter().flatten() {
            assert!((p[0].hypot(p[1]) - 20.0).abs() < 0.01 && p[0] >= -1e-9 && p[1] >= -1e-9, "{p:?}");
        }
        let (_, a) = vol_area(&m.tris);
        let want = 0.25 * TAU * 20.0 * 30.0;
        assert!((a / want - 1.0).abs() < 0.002, "area {a} vs {want}");
        // Outward (away from the axis).
        let [p, q, r] = m.tris[m.tris.len() / 2];
        let n = cross(sub(q, p), sub(r, p));
        assert!(dot(n, [p[0], p[1], 0.0]) > 0.0);
    }
}
