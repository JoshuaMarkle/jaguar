//! Hull geometry: the `jaguar-hull/1` file format and the analysis [`Grid`].
//!
//! A hull is a table of half-breadths `half_breadth[i][k]` at longitudinal
//! station `x[i]` (bow = 0) and height above keel `z[k]` (keel = 0), inches.
//! Only the underwater body up to `draft_design` needs to be given. Above the
//! last waterline the topsides are extruded straight up to the mold depth,
//! which is a search variable, not part of the hull file.
//!
//! The format is deliberately a plain dense table so that the optimizer can
//! resample it with nothing but linear interpolation (fast, allocation-light),
//! and the web viewer can draw it without any geometry library.

use crate::units::M_TO_IN;
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::path::Path;

pub const FORMAT: &str = "jaguar-hull/1";

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Hull {
    /// Must equal [`FORMAT`].
    pub format: String,
    /// Must be "in". Present so a metre file can never be read as inches.
    pub units: String,
    pub name: String,
    /// Free text: where the offsets came from.
    #[serde(default)]
    pub source: String,
    /// Overall length, in.
    pub length: f64,
    /// Height of the top waterline in the table, in.
    pub draft_design: f64,
    /// Station positions, strictly increasing, `x[0] = 0`, `x[last] = length`.
    pub x: Vec<f64>,
    /// Waterline heights, strictly increasing, `z[0] = 0`, `z[last] = draft_design`.
    pub z: Vec<f64>,
    /// `half_breadth[station][waterline]`, in, all finite and >= 0.
    pub half_breadth: Vec<Vec<f64>>,
    /// Side-view stem radius arcs ([`Hull::filleted`]): the keel line over each arc and the
    /// sections stretched onto it, applied when the grid is built. Absent on hull files.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stem: Option<StemArcs>,
}

/// The side-view stem radius of a [`Hull`] (see [`Hull::filleted`]).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct StemArcs {
    /// Arc radius, in.
    pub radius_in: f64,
    /// Bow arc: [stem x, arc centre height, x where the arc meets the keel line], x from the
    /// bow tip, in. None = no arc there.
    pub bow: Option<[f64; 3]>,
    /// Stern arc, x measured in from the stern tip.
    pub stern: Option<[f64; 3]>,
}

impl StemArcs {
    /// Highest point of the arcs (their centre height, on the stem), in.
    pub fn top_in(&self) -> f64 {
        [self.bow, self.stern].iter().flatten().map(|e| e[1]).fold(f64::NEG_INFINITY, f64::max)
    }

    /// Lowest point of the arcs (where they meet the keel), in.
    pub fn bottom_in(&self) -> f64 {
        [self.bow, self.stern]
            .iter()
            .flatten()
            .map(|e| e[1] - (self.radius_in.powi(2) - (e[2] - e[0] - self.radius_in).powi(2)).max(0.0).sqrt())
            .fold(f64::INFINITY, f64::min)
    }
}

impl Hull {
    pub fn load(path: &Path) -> Result<Self> {
        let text =
            std::fs::read_to_string(path).with_context(|| format!("reading hull {}", path.display()))?;
        let h: Hull =
            serde_json::from_str(&text).with_context(|| format!("parsing hull {}", path.display()))?;
        h.validate().with_context(|| format!("invalid hull {}", path.display()))?;
        Ok(h)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        self.validate()?;
        std::fs::write(path, serde_json::to_string_pretty(self)?)
            .with_context(|| format!("writing {}", path.display()))
    }

    /// The hull as the run builds it: `hull.doubly_symmetric`, then `hull.templates`, then
    /// `hull.stem_radius_in` (both treat the two ends alike, so symmetry is kept).
    pub fn prepared(self, hp: &crate::params::HullParams) -> Result<Hull> {
        let h = if hp.doubly_symmetric { self.symmetrized() } else { self };
        let h = match hp.templates {
            Some(n) => h.templated(n),
            None => h,
        };
        if hp.stem_radius_in > 0.0 { h.filleted(hp.stem_radius_in) } else { Ok(h) }
    }

    /// The hull with a side-view radius `r`, in, at the forefoot of the bow and of the stern (the
    /// corner where the stem meets the keel line), so the bottom has no point. The profile is
    /// rounded on an arc tangent to the stem and to the keel line ([`forefoot`]); from the stem to
    /// where the arc meets the keel, the keel line is the arc and every section, from the keel up
    /// to the mold depth, is stretched onto it ([`Grid::build_topsides`]): sections keep their
    /// shape and pointed bottom, waterlines stay pointed, the top of the stem, the gunwale and
    /// everything aft of the arc are unchanged, concrete is only removed. The table itself is
    /// kept; the arcs are recorded in `stem`. An arc may reach above the design waterline; a mold
    /// shallower than its top is unbuildable.
    pub fn filleted(&self, r: f64) -> Result<Hull> {
        ensure!(r.is_finite() && r > 0.0, "stem radius must be > 0, got {r}");
        ensure!(self.stem.is_none(), "hull {} already has a stem radius", self.name);
        let loft = self.loft();
        let bow = forefoot(&loft, r, true);
        // A mirror table (doubly symmetric, to 1e-9 in) gets the bow's arc at the stern too, so
        // float noise in the stern's own fit cannot make the ends differ.
        let n = self.x.len();
        let mirror = (0..n).all(|i| {
            (self.x[i] + self.x[n - 1 - i] - self.length).abs() < 1e-9
                && self.half_breadth[i]
                    .iter()
                    .zip(&self.half_breadth[n - 1 - i])
                    .all(|(a, b)| (a - b).abs() < 1e-9)
        });
        let ends = [bow, if mirror { bow } else { forefoot(&loft, r, false) }];
        let h = Hull {
            name: format!("{}-f{r}", self.name),
            source: format!("{}, stem radius r = {r} in (side view)", self.source),
            stem: ends.iter().any(Option::is_some).then_some(StemArcs {
                radius_in: r,
                bow: ends[0],
                stern: ends[1],
            }),
            ..self.clone()
        };
        h.validate()?;
        Ok(h)
    }

    /// The surface between the stations ([`Loft`]: smooth keel).
    pub fn loft(&self) -> Loft<'_> {
        Loft::new(self)
    }

    /// The hull made fore-aft symmetric (doubly symmetric with port/starboard): each station
    /// is the mean of the hull at x and at L - x, on the stations of both (the table
    /// interpolates linearly, so that is exact). Already symmetric hulls come back unchanged.
    pub fn symmetrized(&self) -> Hull {
        let (x0, x1) = (self.x[0], self.x[self.x.len() - 1]);
        let mut x: Vec<f64> = self.x.iter().flat_map(|&v| [v, x0 + x1 - v]).collect();
        x.sort_by(f64::total_cmp);
        x.dedup_by(|a, b| (*a - *b).abs() < 1e-6);
        // Pair the stations exactly (mirror of each is in the list), so the table is symmetric bit for bit.
        let n = x.len();
        for i in 0..n / 2 {
            x[n - 1 - i] = x0 + x1 - x[i];
        }
        if n % 2 == 1 {
            x[n / 2] = 0.5 * (x0 + x1);
        }
        let loft = self.loft();
        let half: Vec<Vec<f64>> = (0..n)
            .map(|i| {
                self.z
                    .iter()
                    .map(|&zk| 0.5 * (loft.breadth(x[i], zk) + loft.breadth(x[n - 1 - i], zk)))
                    .collect()
            })
            .collect();
        Hull {
            name: format!("{}-sym", self.name),
            source: format!("{}, made doubly symmetric (bow and stern averaged)", self.source),
            x,
            half_breadth: half,
            ..self.clone()
        }
    }

    /// The hull as `n` cross-section templates lofted between neighbours: the exact
    /// sections of this hull at `n` stations, cosine-spaced (closer at the ends, where
    /// the shape changes fastest), tips included. Between templates the table's [`Loft`]
    /// applies: waterlines straight where the keel is level, the keel a smooth batten
    /// through the templates' keels where it rises.
    pub fn templated(&self, n: usize) -> Hull {
        let (x0, x1) = (self.x[0], self.x[self.x.len() - 1]);
        let x: Vec<f64> = (0..n)
            .map(|i| x0 + (x1 - x0) * 0.5 * (1.0 - (std::f64::consts::PI * i as f64 / (n - 1) as f64).cos()))
            .collect();
        let loft = self.loft();
        let half_breadth =
            x.iter().map(|&xi| self.z.iter().map(|&zk| loft.breadth(xi, zk)).collect()).collect();
        Hull {
            name: format!("{}-t{n}", self.name),
            source: format!("{} as {n} cross-section templates, straight between", self.source),
            x,
            half_breadth,
            ..self.clone()
        }
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(self.format == FORMAT, "format must be '{FORMAT}', got '{}'", self.format);
        ensure!(self.units == "in", "units must be 'in', got '{}'", self.units);
        ensure!(self.x.len() >= 3 && self.z.len() >= 2, "need >= 3 stations and >= 2 waterlines");
        ensure!(self.half_breadth.len() == self.x.len(), "half_breadth rows != x.len()");
        for (i, row) in self.half_breadth.iter().enumerate() {
            ensure!(row.len() == self.z.len(), "half_breadth[{i}] len != z.len()");
            ensure!(
                row.iter().all(|v| v.is_finite() && *v >= 0.0),
                "half_breadth[{i}] has a negative or non-finite value"
            );
        }
        strictly_increasing(&self.x, "x")?;
        strictly_increasing(&self.z, "z")?;
        ensure!(
            close(self.x[0], 0.0) && close(*self.x.last().unwrap(), self.length),
            "x must span [0, length]"
        );
        ensure!(
            close(self.z[0], 0.0) && close(*self.z.last().unwrap(), self.draft_design),
            "z must span [0, draft_design]"
        );
        if let Some(st) = &self.stem {
            ensure!(st.radius_in.is_finite() && st.radius_in > 0.0, "stem.radius_in must be > 0");
            ensure!(
                [st.bow, st.stern].iter().flatten().all(|e| e.iter().all(|v| v.is_finite()) && e[2] >= e[0]),
                "stem arc has a non-finite value or ends before the stem"
            );
        }
        Ok(())
    }

    /// Import a Godzilla/Michlet offset table (`off1.mlt`, metres, rows =
    /// stations bow->stern, columns = waterlines keel->DWL, evenly spaced)
    /// plus the `LPP (m)` and `T (m)` lines from its `out.mlt`.
    pub fn from_michlet(off1: &Path, out: &Path) -> Result<Self> {
        let rows: Vec<Vec<f64>> = std::fs::read_to_string(off1)
            .with_context(|| format!("reading {}", off1.display()))?
            .lines()
            .map(|l| l.split(['#', '!']).next().unwrap_or(""))
            .filter(|l| !l.trim().is_empty())
            .map(|l| {
                l.replace(',', " ")
                    .split_whitespace()
                    .map(|v| v.parse::<f64>().map(|m| m * M_TO_IN))
                    .collect::<std::result::Result<Vec<_>, _>>()
            })
            .collect::<std::result::Result<_, _>>()
            .with_context(|| format!("parsing numbers in {}", off1.display()))?;
        let out_text = std::fs::read_to_string(out).with_context(|| format!("reading {}", out.display()))?;
        let field = |name: &str| -> Result<f64> {
            for line in out_text.lines() {
                if let Some((k, v)) = line.split_once(',')
                    && k.trim() == name
                {
                    return Ok(v.trim().parse::<f64>()? * M_TO_IN);
                }
            }
            bail!("{}: no '{name}' line, refusing to guess a hull size", out.display())
        };
        let (length, draft) = (field("LPP (m)")?, field("T (m)")?);
        let (ns, nw) = (rows.len(), rows.first().map_or(0, Vec::len));
        let h = Hull {
            format: FORMAT.into(),
            units: "in".into(),
            name: off1.file_stem().unwrap_or_default().to_string_lossy().into(),
            source: format!("{} + {}", off1.display(), out.display()),
            length,
            draft_design: draft,
            x: linspace(0.0, length, ns),
            z: linspace(0.0, draft, nw),
            half_breadth: rows,
            stem: None,
        };
        h.validate()?;
        Ok(h)
    }
}

/// The hull resampled for analysis: `n` evenly spaced stations, the given
/// waterlines plus topsides extruded to `depth`. Row-major `y[i * nz + k]`.
#[derive(Debug, Clone)]
pub struct Grid {
    pub x: Vec<f64>,
    pub z: Vec<f64>,
    pub y: Vec<f64>,
    pub depth: f64,
}

/// Number of extra waterlines used for the extruded topsides.
pub const TOPSIDE_LINES: usize = 10;

/// How the topsides above `draft_design` are built ([`Grid::build_topsides`]).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Topsides {
    /// Outward lean of the sides, deg from vertical (demolding draft). With a
    /// gunwale outline it is the minimum lean at every station with waterline
    /// breadth; the outline may lean more where it must.
    pub flare_deg: f64,
    /// Some(R): the gunwale in top view is straight from the bow tip, one
    /// circular arc of radius R in, straight to the stern tip (constructable
    /// planform), and each side is ruled (straight) from the design waterline
    /// up to it. None: the top waterline extruded with `flare_deg`.
    pub gunwale_radius_in: Option<f64>,
    /// With a gunwale outline: steepest outward lean the ruled sides may have
    /// anywhere along the hull, deg from vertical (full-ended hulls otherwise
    /// get absurdly wide gunwales near the tips).
    pub gunwale_max_flare_deg: f64,
}

/// Default `gunwale_max_flare_deg`.
pub const GUNWALE_MAX_FLARE_DEG: f64 = 30.0;

/// Stations (evenly spaced, independent of the analysis grid) at which the side lean is checked.
const LEAN_STATIONS: usize = 401;

/// Line-arc-line gunwale (top view, half-breadth y >= 0 against x): straight
/// from the bow tip (x0, 0) tangent to a circle of radius `r` whose top is at
/// (xc, b), the arc, then straight tangent to the stern tip (x1, 0). The
/// outline is concave, so it encloses a piecewise-linear waterline wherever it
/// does at the waterline's table stations.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Gunwale {
    pub x0: f64,
    pub x1: f64,
    pub xc: f64,
    pub b: f64,
    pub r: f64,
    /// Tangent points (x, y) of the bow and stern lines.
    pub bow: [f64; 2],
    pub stern: [f64; 2],
}

impl Default for Topsides {
    fn default() -> Self {
        Topsides { flare_deg: 0.0, gunwale_radius_in: None, gunwale_max_flare_deg: GUNWALE_MAX_FLARE_DEG }
    }
}

impl Gunwale {
    /// Steepest outward lean of the ruled sides of `hull` built to `depth`, deg
    /// from vertical, and where (x, in). Checked at `LEAN_STATIONS` even stations.
    pub fn max_lean(&self, hull: &Hull, depth: f64) -> (f64, f64) {
        let (nw, h) = (hull.z.len(), depth - hull.draft_design);
        linspace(hull.x[0], hull.x[hull.x.len() - 1], LEAN_STATIONS)
            .into_iter()
            .map(|x| {
                (
                    x,
                    ((self.half_breadth(x) - interp_col(&hull.x, &hull.half_breadth, nw - 1, x)) / h)
                        .atan()
                        .to_degrees(),
                )
            })
            .fold((0.0, f64::NEG_INFINITY), |a, p| if p.1 > a.1 { p } else { a })
    }

    /// Err (the reason) when the arc would reach past a tip: r must stay below
    /// ((xc - tip)^2 + b^2) / (2 b) at both ends.
    pub fn new(x0: f64, x1: f64, xc: f64, b: f64, r: f64) -> std::result::Result<Gunwale, String> {
        if !(r.is_finite() && r > 0.0 && b > 0.0 && x0 < xc && xc < x1) {
            return Err(format!("gunwale: need radius > 0 and a beam peak inside the hull (r {r}, b {b})"));
        }
        let cy = b - r;
        let tangent = |px: f64| -> std::result::Result<[f64; 2], String> {
            let (dx, dy) = (px - xc, -cy);
            let d = dx.hypot(dy);
            let r_max = (dx * dx + b * b) / (2.0 * b);
            if d <= r {
                return Err(format!(
                    "gunwale_radius_in {r:.0} in is too large for this hull: the arc reaches the {} tip \
                     (max {r_max:.0} in at a {b:.1} in gunwale half-beam)",
                    if px < xc { "bow" } else { "stern" }
                ));
            }
            let (phi, alpha) = (dy.atan2(dx), (r / d).acos());
            let p = |a: f64| [xc + r * a.cos(), cy + r * a.sin()];
            let (u, v) = (p(phi + alpha), p(phi - alpha));
            Ok(if u[1] >= v[1] { u } else { v })
        };
        let (bow, stern) = (tangent(x0)?, tangent(x1)?);
        Ok(Gunwale { x0, x1, xc, b, r, bow, stern })
    }

    pub fn half_breadth(&self, x: f64) -> f64 {
        if x <= self.x0 || x >= self.x1 {
            0.0
        } else if x < self.bow[0] {
            self.bow[1] * (x - self.x0) / (self.bow[0] - self.x0)
        } else if x > self.stern[0] {
            self.stern[1] * (self.x1 - x) / (self.x1 - self.stern[0])
        } else {
            let dx = x - self.xc;
            (self.b - self.r) + (self.r * self.r - dx * dx).max(0.0).sqrt()
        }
    }

    /// Excess of the waterline, widened by `offset` where it has breadth, over
    /// this outline at the worst table station of `hull` (x, inches; <= 0 = the
    /// outline clears it). offset = (depth - draft) tan(flare): every side then
    /// leans out at least the flare.
    pub fn deficit(&self, hull: &Hull, offset: f64) -> (f64, f64) {
        let nw = hull.z.len();
        hull.x
            .iter()
            .zip(&hull.half_breadth)
            .map(|(&x, row)| {
                let yw = row[nw - 1];
                (x, if yw > 0.0 { yw + offset } else { yw } - self.half_breadth(x))
            })
            .fold((0.0, f64::NEG_INFINITY), |a, p| if p.1 > a.1 { p } else { a })
    }

    /// The gunwale of `hull` built to `depth` with arc radius `r`: the
    /// narrowest such outline that clears the design waterline widened by
    /// (depth - draft) tan(`flare_deg`) wherever the waterline has breadth, so
    /// every side leans out at least the flare (demolding). The arc peaks at the middle of the widest waterline stations.
    /// The outline rises everywhere with its half-beam b, so b is bisected; Err
    /// when no b works (a larger b only shrinks the largest radius that fits).
    pub fn of_hull(hull: &Hull, depth: f64, flare_deg: f64, r: f64) -> std::result::Result<Gunwale, String> {
        let nw = hull.z.len();
        let top = hull.half_breadth.iter().map(|row| row[nw - 1]).fold(f64::NEG_INFINITY, f64::max);
        let widest: Vec<f64> = hull
            .x
            .iter()
            .zip(&hull.half_breadth)
            .filter(|(_, row)| row[nw - 1] >= top - 1e-9)
            .map(|(&x, _)| x)
            .collect();
        let xc = 0.5 * (widest[0] + widest[widest.len() - 1]);
        let (x0, x1) = (hull.x[0], hull.x[hull.x.len() - 1]);
        let offset = (depth - hull.draft_design).max(0.0) * flare_deg.to_radians().tan();
        let b0 = top + offset;
        let at = |b: f64| Gunwale::new(x0, x1, xc, b, r);
        let g0 = at(b0)?;
        if g0.deficit(hull, offset).1 <= 1e-9 {
            return Ok(g0);
        }
        // Largest b with the arc clear of both tips: r < (e^2 + b^2) / (2 b), e the
        // shorter end, i.e. b < r - sqrt(r^2 - e^2) when r > e (any b otherwise).
        let e = (xc - x0).min(x1 - xc);
        let b_cap = if r > e { r - (r * r - e * e).sqrt() } else { f64::INFINITY };
        let b_hi = (b_cap * (1.0 - 1e-9)).min(b0 + 4.0 * top);
        let enclosing = b_hi > b0 && at(b_hi).is_ok_and(|g| g.deficit(hull, offset).1 <= 1e-9);
        if !enclosing {
            let (x, d) = at(b_hi.max(b0)).map_or((f64::NAN, f64::NAN), |g| g.deficit(hull, offset));
            return Err(format!(
                "no gunwale of radius {r:.0} in encloses the waterline: at the widest that fits \
                 ({:.1} in half-beam) it is still {d:.2} in inside at x = {x:.1} in; use a smaller \
                 gunwale_radius_in or a hull with straighter / hollower ends",
                b_hi.max(b0)
            ));
        }
        let (mut lo, mut hi) = (b0, b_hi);
        for _ in 0..60 {
            let mid = 0.5 * (lo + hi);
            if at(mid).is_ok_and(|g| g.deficit(hull, offset).1 <= 1e-9) { hi = mid } else { lo = mid }
        }
        at(hi)
    }
}

impl Grid {
    pub fn nx(&self) -> usize {
        self.x.len()
    }
    pub fn nz(&self) -> usize {
        self.z.len()
    }
    pub fn y(&self, i: usize, k: usize) -> f64 {
        self.y[i * self.z.len() + k]
    }
    pub fn row(&self, i: usize) -> &[f64] {
        let nz = self.z.len();
        &self.y[i * nz..(i + 1) * nz]
    }

    /// [`Grid::build_flared`], or with `ts.gunwale_radius_in` the line-arc-line
    /// gunwale with ruled sides. The second value is why that design cannot be
    /// built (sides would lean in, arc past a tip, depth not above the design
    /// draft); the grid is then still the real geometry, so weights stay real.
    pub fn build_topsides(hull: &Hull, depth: f64, n: usize, ts: Topsides) -> (Grid, Option<String>) {
        Self::build_topsides_at(hull, depth, &linspace(0.0, hull.length, n), ts)
    }

    /// [`Grid::build_topsides`] at the given stations `xs` (increasing) instead of `n` even ones.
    pub fn build_topsides_at(hull: &Hull, depth: f64, xs: &[f64], ts: Topsides) -> (Grid, Option<String>) {
        let mut g = Self::build_flared_at(hull, depth, xs, ts.flare_deg);
        let dd = hull.draft_design;
        let mut why = hull.stem.as_ref().filter(|st| st.top_in() > depth + 1e-9).map(|st| {
            format!(
                "stem radius {} in: the arc reaches z = {:.2} in, above the mold depth {depth} in; \
                 use a smaller stem_radius_in or a deeper mold",
                st.radius_in,
                st.top_in()
            )
        });
        let loft = hull.loft();
        let mut gunwale = None;
        if let Some(r) = ts.gunwale_radius_in {
            if depth <= dd {
                why.get_or_insert(format!(
                    "gunwale outline: mold depth {depth} in must be above the design draft {dd:.2} in"
                ));
            } else {
                match Gunwale::of_hull(hull, depth, ts.flare_deg, r) {
                    Err(e) => {
                        why.get_or_insert(e);
                    }
                    Ok(gw) => {
                        // Concave outline vs piecewise-linear waterline: the table stations decide.
                        // `of_hull` already widened the outline to enclose; this guards it.
                        let worst = gw.deficit(hull, 0.0);
                        let nz = g.z.len();
                        for i in 0..g.x.len() {
                            let (yw, yg) = (loft.breadth(g.x[i], dd), gw.half_breadth(g.x[i]));
                            for k in (0..nz).filter(|&k| g.z[k] > dd) {
                                g.y[i * nz + k] = yw + (yg - yw) * (g.z[k] - dd) / (depth - dd);
                            }
                        }
                        let (lean_x, lean) = gw.max_lean(hull, depth);
                        if worst.1 > 1e-6 {
                            why.get_or_insert(format!(
                                "gunwale R {r:.0} in sits {:.2} in inside the waterline at x = {:.1} in (sides would lean in)",
                                worst.1, worst.0
                            ));
                        } else if lean > ts.gunwale_max_flare_deg {
                            why.get_or_insert(format!(
                                "gunwale R {r:.0} in needs the side to lean out {lean:.1} deg at x = {lean_x:.1} in \
                                 (gunwale_max_flare_deg {}): the waterline ends are too full for a straight entry from the tips; \
                                 try another radius or a deeper mold",
                                ts.gunwale_max_flare_deg
                            ));
                        }
                        gunwale = Some(gw);
                    }
                }
            }
        }
        // The surface before the stem radius, at any height: the loft, then these topsides.
        let flare = ts.flare_deg.to_radians().tan();
        let uncut = |x: f64, z: f64| -> f64 {
            if z <= dd {
                return loft.breadth(x, z);
            }
            let t = loft.breadth(x, dd);
            match &gunwale {
                Some(gw) => t + (gw.half_breadth(x) - t) * (z - dd) / (depth - dd),
                None => flared(t, z - dd, depth - dd, flare),
            }
        };
        g.cut_stem(&loft, uncut);
        (g, why)
    }

    /// The stem radius ([`Hull::filleted`]): where the keel line is the arc, each column is the
    /// uncut surface stretched from its keel K(x) .. depth onto the arc K'(x) .. depth, z ->
    /// K + (z - K') (depth - K) / (depth - K'), and nothing below K'. Columns aft of the arcs
    /// (K' = K) are untouched.
    fn cut_stem(&mut self, loft: &Loft, uncut: impl Fn(f64, f64) -> f64) {
        if loft.hull.stem.is_none() {
            return;
        }
        let (nz, top) = (self.z.len(), self.depth);
        for i in 0..self.x.len() {
            let x = self.x[i];
            let (Some(k), Some(kc)) = (loft.table_keel(x), loft.keel(x)) else { continue };
            // Where the arc meets the keel the two agree to rounding: no cut (decided the same
            // at both ends of a symmetric hull).
            if kc <= k + 1e-9 {
                continue;
            }
            for j in 0..nz {
                let z = self.z[j];
                self.y[i * nz + j] = if z < kc || top - kc <= 1e-9 {
                    0.0
                } else {
                    uncut(x, k + (z - kc) * (top - k) / (top - kc))
                };
            }
        }
    }

    /// Resample `hull` to `n` stations and extrude the top waterline up to
    /// `depth`. Matches `tests/golden/reference.py` `build()` (golden-tested).
    pub fn build(hull: &Hull, depth: f64, n: usize) -> Grid {
        Self::build_flared(hull, depth, n, 0.0)
    }

    /// Like [`Grid::build`], but the topsides above the design waterline lean
    /// OUTWARD by `flare_deg` from vertical: half-breadth grows by
    /// `(z - draft_design) * tan(flare)` at every station that has a waterline, less near the
    /// tips ([`flared`]: the stem stays a sharp line up to the gunwale).
    /// 0 = vertical sides (as in the golden reference). Flare adds wall area (weight), raises
    /// reserve buoyancy and widens the gunwale (stability, freeboard).
    pub fn build_flared(hull: &Hull, depth: f64, n: usize, flare_deg: f64) -> Grid {
        Self::build_flared_at(hull, depth, &linspace(0.0, hull.length, n), flare_deg)
    }

    fn build_flared_at(hull: &Hull, depth: f64, xs: &[f64], flare_deg: f64) -> Grid {
        let flare = flare_deg.to_radians().tan();
        let x = xs.to_vec();
        let n = x.len();
        let dd = hull.draft_design;
        let mut z = hull.z.clone();
        if depth > dd {
            // From 0.1 in above the table top, or halfway when the gunwale is closer than 0.2 in
            // (a CAD import's table ends just under its depth): the rows must rise to the depth.
            z.extend(linspace((dd + 0.1).min(0.5 * (dd + depth)), depth, TOPSIDE_LINES));
        }
        // A stem radius above the table: rows every r/8 up its arcs there, so they are resolved
        // (the table's own waterlines resolve them below).
        if let Some(st) = &hull.stem {
            let (h, lo, hi) = (0.125 * st.radius_in, st.bottom_in().max(dd), st.top_in().min(depth.max(dd)));
            if hi > lo + h {
                z.extend(linspace(lo, hi, ((hi - lo) / h).ceil() as usize + 1));
            }
            z.sort_by(f64::total_cmp);
            // Near-duplicates go, but the table top stays a row (the topsides start from it).
            z.dedup_by(|a, b| (*a - *b).abs() < 1e-3 && (*a != dd || *b == dd));
        }
        let nz = z.len();
        let loft = hull.loft();
        let mut y = vec![0.0; n * nz];
        for (i, &xi) in x.iter().enumerate() {
            let c = loft.col(xi);
            let top = loft.at(&c, dd);
            for k in 0..nz {
                y[i * nz + k] =
                    if z[k] <= dd { loft.at(&c, z[k]) } else { flared(top, z[k] - dd, depth - dd, flare) };
            }
        }
        Grid { x, z, y, depth }
    }
}

/// Flared topside half-breadth `dz` above the table top, whose half-breadth is `top`, on a
/// topside `rise` tall: leaning out by `flare` (tan), eased in near the tips so a tip with a
/// sliver of waterline stays a sharp stem instead of a flat face as wide as the flare (0.74 in at
/// 3 deg over 7 in). With F = rise * flare (the full gunwale offset), w = max([`FLARE_EASE_IN`],
/// 2 F) and v = top / w, the offset is F v (2 - v) up to v = 1, then F: C1 in `top`, at most `top`,
/// and the gunwale leaves the tip at (1 + 2 F / w) times the waterline's slope, so in top view it
/// turns only a few degrees (team winner, 3 deg: 19.6 -> 16.8 deg over the first 15 in; easing
/// over 2 F instead turned 31 -> 16.8 deg in 2.7 in, a visible hook). Unchanged wherever
/// top >= w; 0 at a closed tip.
fn flared(top: f64, dz: f64, rise: f64, flare: f64) -> f64 {
    let f = rise * flare;
    if f <= 0.0 {
        return top;
    }
    let v = top / FLARE_EASE_IN.max(2.0 * f);
    top + dz * flare * if v >= 1.0 { 1.0 } else { v * (2.0 - v) }
}

/// Waterline half-breadth, in, below which the flare eases in toward the tips ([`flared`]).
/// Larger = straighter gunwale near the tips, less side lean there.
const FLARE_EASE_IN: f64 = 4.0;

pub fn linspace(a: f64, b: f64, n: usize) -> Vec<f64> {
    if n == 1 {
        return vec![a];
    }
    (0..n).map(|i| a + (b - a) * i as f64 / (n - 1) as f64).collect()
}

/// Keel height of one station column (half-breadths `y` at waterlines `z`), not snapped to a
/// waterline: below the lowest non-zero offset the section is taken as `z = zk + c y^2` through
/// the two lowest non-zero offsets, clamped between the last zero waterline and the first non-zero
/// one. A flat bottom (offset at the first waterline) keeps `z[0]`. `None` if all zero.
pub fn column_keel(y: &[f64], z: &[f64]) -> Option<f64> {
    let first = y.iter().position(|&v| v > 1e-9)?;
    if first == 0 {
        return Some(z[0]);
    }
    let (y1, z1) = (y[first], z[first]);
    let fit = y.get(first + 1).filter(|&&y2| y2 > y1 + 1e-9).map(|&y2| {
        let z2 = z[first + 1];
        (z1 * y2 * y2 - z2 * y1 * y1) / (y2 * y2 - y1 * y1)
    });
    Some(fit.unwrap_or(z[first - 1]).clamp(z[first - 1], z1))
}

/// Slopes of the monotone cubic (Fritsch-Carlson PCHIP) through `(xs, ys)`: interior slopes the
/// weighted harmonic mean of the neighbouring secants (0 at a local extremum), end slopes the
/// end secants. No overshoot, C1.
fn pchip_slopes(xs: &[f64], ys: &[f64]) -> Vec<f64> {
    let n = xs.len();
    if n < 2 {
        return vec![0.0; n];
    }
    let h: Vec<f64> = xs.windows(2).map(|w| w[1] - w[0]).collect();
    let d: Vec<f64> = (0..n - 1).map(|i| (ys[i + 1] - ys[i]) / h[i]).collect();
    let mut m = vec![0.0; n];
    m[0] = d[0];
    m[n - 1] = d[n - 2];
    for i in 1..n - 1 {
        if d[i - 1] * d[i] > 0.0 {
            let (w1, w2) = (2.0 * h[i] + h[i - 1], h[i] + 2.0 * h[i - 1]);
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
        }
    }
    m
}

/// The PCHIP of [`pchip_slopes`] at `x`; outside `xs` the straight line on with the end slope
/// (so the curve stays C1 through the end points).
fn pchip_eval(xs: &[f64], ys: &[f64], m: &[f64], x: f64) -> f64 {
    let n = xs.len();
    if n == 1 {
        return ys[0];
    }
    if x < xs[0] {
        return ys[0] + m[0] * (x - xs[0]);
    }
    if x > xs[n - 1] {
        return ys[n - 1] + m[n - 1] * (x - xs[n - 1]);
    }
    let i = xs.partition_point(|&v| v <= x).clamp(1, n - 1) - 1;
    let h = xs[i + 1] - xs[i];
    let t = (x - xs[i]) / h;
    let (t2, t3) = (t * t, t * t * t);
    ys[i] * (2.0 * t3 - 3.0 * t2 + 1.0)
        + h * m[i] * (t3 - 2.0 * t2 + t)
        + ys[i + 1] * (-2.0 * t3 + 3.0 * t2)
        + h * m[i + 1] * (t3 - t2)
}

/// The hull surface between its stations: a loft with a smooth keel.
///
/// Every station with breadth has a keel height ([`column_keel`]); the keel line K(x) is the
/// monotone cubic through them (C1, no overshoot), continued straight with its end slope to the
/// stems, kept within the table (0 .. T). A station's
/// section is its offsets, linear between waterlines, and below its lowest offset a straight line
/// down to its keel. Between stations a and b (fraction t) the half-breadth at height z is
///
///   s = (z - K(x)) / (T - K(x)),  y = (1 - t) Y_a(K_a + s (T - K_a)) + t Y_b(K_b + s (T - K_b)),
///
/// T the table top, K_a = K(x_a): each section is stretched between its keel and the top, so
/// the keel runs along K(x) instead of stepping from one waterline to the next, and the table
/// top (s = 1) is the plain linear blend. At the stations it is the table. 0 below K(x).
pub struct Loft<'a> {
    hull: &'a Hull,
    /// Per station: keel height (None = no breadth) and its lowest non-zero waterline.
    keel: Vec<Option<(f64, usize)>>,
    kx: Vec<f64>,
    kz: Vec<f64>,
    km: Vec<f64>,
    /// K at each station (the keel line there, also at stations without breadth).
    kst: Vec<f64>,
}

/// One column of a [`Loft`] at some x: its station interval, blend and keel heights, so many
/// heights are read without redoing them ([`Loft::at`]).
pub struct LoftCol {
    j: usize,
    t: f64,
    k: Option<f64>,
    ka: f64,
    kb: f64,
}

impl<'a> Loft<'a> {
    pub fn new(hull: &'a Hull) -> Loft<'a> {
        let keel: Vec<Option<(f64, usize)>> = hull
            .half_breadth
            .iter()
            .map(|row| {
                let k = column_keel(row, &hull.z)?;
                Some((k, row.iter().position(|&v| v > 1e-9).unwrap_or(0)))
            })
            .collect();
        let (kx, kz): (Vec<f64>, Vec<f64>) =
            hull.x.iter().zip(&keel).filter_map(|(&x, k)| Some((x, (*k)?.0))).unzip();
        let km = pchip_slopes(&kx, &kz);
        let mut loft = Loft { hull, keel, kx, kz, km, kst: vec![] };
        loft.kst = hull.x.iter().map(|&x| loft.table_keel(x).unwrap_or(0.0)).collect();
        loft
    }

    /// K(x), the table's keel line (through the station keels; beyond the end ones on straight
    /// to the stem with the end slope), kept within the table. None when no station has breadth.
    fn table_keel(&self, x: f64) -> Option<f64> {
        let (lo, hi) = (self.hull.z[0], self.hull.z[self.hull.z.len() - 1]);
        (!self.kx.is_empty()).then(|| pchip_eval(&self.kx, &self.kz, &self.km, x).clamp(lo, hi))
    }

    /// Keel height at `x`: K(x), and over a stem radius ([`Hull::filleted`]) its arc, from the
    /// stem to where the arc meets K (tangent there and vertical at the stem; it may run above
    /// the table, where the table itself has no breadth).
    pub fn keel(&self, x: f64) -> Option<f64> {
        let k = self.table_keel(x)?;
        let Some(st) = &self.hull.stem else { return Some(k) };
        let (xa, xb) = (self.hull.x[0], self.hull.x[self.hull.x.len() - 1]);
        let bow = x - xa <= xb - x;
        let (end, d) = if bow { (st.bow, x - xa) } else { (st.stern, xb - x) };
        let r = st.radius_in;
        Some(match end {
            Some([xe, zc, xt]) if d >= xe - 1e-12 && d <= xt => {
                let u = d - xe - r;
                k.max(zc - (r * r - u * u).max(0.0).sqrt())
            }
            _ => k,
        })
    }

    /// Section of station `j` at height `z` (0 below its keel).
    fn station(&self, j: usize, z: f64) -> f64 {
        let Some((k, f)) = self.keel[j] else { return 0.0 };
        let (row, zt) = (&self.hull.half_breadth[j], &self.hull.z);
        if f > 0 {
            if z <= k {
                return 0.0;
            }
            if z < zt[f] {
                return if zt[f] > k { row[f] * (z - k) / (zt[f] - k) } else { row[f] };
            }
        }
        let n = zt.len();
        let i = zt.partition_point(|&v| v <= z).clamp(1, n - 1);
        let t = ((z - zt[i - 1]) / (zt[i] - zt[i - 1])).clamp(0.0, 1.0);
        row[i - 1] * (1.0 - t) + row[i] * t
    }

    /// Half-breadth at (x, z), z up to the table top (see [`Loft`]).
    pub fn breadth(&self, x: f64, z: f64) -> f64 {
        self.at(&self.col(x), z)
    }

    /// The column at `x`, for [`Loft::at`].
    pub fn col(&self, x: f64) -> LoftCol {
        let (xs, n) = (&self.hull.x, self.hull.x.len());
        let j = xs.partition_point(|&v| v <= x).clamp(1, n - 1);
        let t = ((x - xs[j - 1]) / (xs[j] - xs[j - 1])).clamp(0.0, 1.0);
        LoftCol { j, t, k: self.table_keel(x), ka: self.kst[j - 1], kb: self.kst[j] }
    }

    /// Half-breadth of column `c` at height `z` (same as [`Loft::breadth`]).
    pub fn at(&self, c: &LoftCol, z: f64) -> f64 {
        let (j, t, ka, kb) = (c.j, c.t, c.ka, c.kb);
        let Some(k) = c.k else { return 0.0 };
        if z < k {
            return 0.0;
        }
        let top = self.hull.z[self.hull.z.len() - 1];
        let (za, zb) = if top - k > 1e-9 {
            let s = ((z - k) / (top - k)).clamp(0.0, 1.0);
            (ka + s * (top - ka), kb + s * (top - kb))
        } else {
            (z, z)
        };
        let (ya, yb) = (self.station(j - 1, za), self.station(j, zb));
        if t <= 0.0 {
            ya
        } else if t >= 1.0 {
            yb
        } else {
            ya * (1.0 - t) + yb * t
        }
    }
}

/// Forefoot arc of an end (distances from that end's tip): [stem x, arc centre height, x where it
/// meets the keel]. The stem is the last station with no breadth before the first one with
/// some; the keel line is the loft's K(x). The arc is the radius-`r` circle tangent to the stem
/// (centre r aft of it) sitting on the keel within 2 r: centre height = max over the keel there of
/// K + sqrt(r^2 - dx^2), met where that maximum is (sampled every r/256 and at the stations).
/// None when the end has breadth at its tip station (no stem crease) or no breadth at all.
fn forefoot(loft: &Loft, r: f64, bow: bool) -> Option<[f64; 3]> {
    let (xs, n) = (&loft.hull.x, loft.hull.x.len());
    let order: Vec<usize> = if bow { (0..n).collect() } else { (0..n).rev().collect() };
    let p = order.iter().position(|&j| loft.keel[j].is_some())?;
    if p == 0 {
        return None;
    }
    let d = |x: f64| if bow { x - xs[0] } else { xs[n - 1] - x };
    let at = |dd: f64| if bow { xs[0] + dd } else { xs[n - 1] - dd };
    let xe = d(xs[order[p - 1]]);
    let c = xe + r;
    let mut probe: Vec<f64> = (0..=512).map(|i| xe + 2.0 * r * i as f64 / 512.0).collect();
    probe.extend(xs.iter().map(|&x| d(x)).filter(|&v| v >= xe && v <= xe + 2.0 * r));
    probe.sort_by(f64::total_cmp);
    let (xt, zc) = probe
        .iter()
        .filter_map(|&dd| {
            let k = loft.table_keel(at(dd))?;
            Some((dd, k + (r * r - (dd - c) * (dd - c)).max(0.0).sqrt()))
        })
        .fold((xe, f64::NEG_INFINITY), |a, b| if b.1 > a.1 { b } else { a });
    zc.is_finite().then_some([xe, zc, xt])
}

/// Linear interpolation of column `k` of `table` (rows at `xs`) at `x`, clamped.
fn interp_col(xs: &[f64], table: &[Vec<f64>], k: usize, x: f64) -> f64 {
    let j = xs.partition_point(|&v| v <= x).clamp(1, xs.len() - 1);
    let (x0, x1) = (xs[j - 1], xs[j]);
    let t = ((x - x0) / (x1 - x0)).clamp(0.0, 1.0);
    table[j - 1][k] * (1.0 - t) + table[j][k] * t
}

fn strictly_increasing(v: &[f64], name: &str) -> Result<()> {
    ensure!(v.iter().all(|a| a.is_finite()), "{name} has non-finite values");
    ensure!(v.windows(2).all(|w| w[1] > w[0]), "{name} must be strictly increasing");
    Ok(())
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-6 * (1.0 + b.abs())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn michlet_import_matches_golden_hull() {
        let imported = Hull::from_michlet(
            Path::new("tests/golden/michlet/off1.mlt"),
            Path::new("tests/golden/michlet/out.mlt"),
        )
        .unwrap();
        let golden = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        assert!((imported.length - golden.length).abs() < 1e-3);
        assert!((imported.draft_design - golden.draft_design).abs() < 1e-4);
        let (a, b) = (&imported.half_breadth, &golden.half_breadth);
        let max = a.iter().flatten().zip(b.iter().flatten()).map(|(p, q)| (p - q).abs()).fold(0.0, f64::max);
        // 1e-4 in: the golden file used 39.3701 in/m, we use the exact factor.
        assert!(max < 1e-4, "max offset diff {max}");
    }

    #[test]
    fn topsides_rise_to_a_depth_just_above_the_table() {
        // A CAD import's table ends a hair under its gunwale: the topside rows must still rise
        // monotonically to the depth (they used to start 0.1 in up and fold back down).
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        for d in [h.draft_design + 1e-5, h.draft_design + 0.05, h.draft_design + 0.15] {
            let g = Grid::build(&h, d, 41);
            assert!(g.z.windows(2).all(|w| w[1] > w[0]), "depth {d}: {:?}", &g.z[g.z.len() - 12..]);
            assert_eq!(*g.z.last().unwrap(), d);
        }
        // Wall area per station is continuous across the table top.
        let a = |d: f64| crate::section::sections(&Grid::build(&h, d, 41), 0.375).area[20];
        let (lo, hi) = (a(h.draft_design), a(h.draft_design + 1e-5));
        assert!((hi / lo - 1.0).abs() < 1e-4, "{lo} vs {hi}");
    }

    #[test]
    fn grid_extrudes_topsides() {
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let g = Grid::build(&h, 14.0, 161);
        assert_eq!(g.nx(), 161);
        assert_eq!(g.nz(), h.z.len() + TOPSIDE_LINES);
        assert_eq!(*g.z.last().unwrap(), 14.0);
        let mid = 80;
        assert_eq!(g.y(mid, g.nz() - 1), g.y(mid, h.z.len() - 1));
    }

    #[test]
    fn flare_widens_topsides_only() {
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let v = Grid::build(&h, 14.0, 161);
        let f = Grid::build_flared(&h, 14.0, 161, 10.0);
        let nw = h.z.len();
        assert_eq!(&v.y[..], &Grid::build_flared(&h, 14.0, 161, 0.0).y[..]);
        let mid = 80;
        for k in 0..nw {
            assert_eq!(v.y(mid, k), f.y(mid, k), "below the waterline is unchanged");
        }
        let gunwale = f.y(mid, f.nz() - 1) - v.y(mid, v.nz() - 1);
        let want = (14.0 - h.draft_design) * 10f64.to_radians().tan();
        assert!((gunwale - want).abs() < 1e-9, "{gunwale} vs {want}");
        assert_eq!(f.y(0, f.nz() - 1), 0.0, "closed stem stays closed");
    }

    #[test]
    fn flare_keeps_the_stem_sharp_above_the_waterline() {
        // Stations a hair from the tips have a sliver of waterline: the full flare there would
        // make the stem a flat face (2 (D - draft) tan(flare) wide) from the waterline up.
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let nw = h.z.len();
        let xs = [1e-3, 0.05 * h.length, 0.5 * h.length, h.length - 1e-3];
        let ts = Topsides { flare_deg: 10.0, ..Topsides::default() };
        let (g, why) = Grid::build_topsides_at(&h, 14.0, &xs, ts);
        assert!(why.is_none());
        let full = (14.0 - h.draft_design) * 10f64.to_radians().tan();
        for (i, x) in xs.iter().enumerate() {
            let (wl, sheer) = (g.y(i, nw - 1), g.y(i, g.nz() - 1));
            assert!(sheer <= 2.0 * wl + 1e-12, "x {x}: sheer {sheer} vs waterline {wl}");
            if wl >= FLARE_EASE_IN.max(2.0 * full) {
                assert!((sheer - wl - full).abs() < 1e-9, "full flare where the waterline is wide");
            }
        }
        assert!(g.y(0, g.nz() - 1) < 0.05, "tip sheer {} in", g.y(0, g.nz() - 1));
    }

    #[test]
    fn flare_eases_in_without_a_corner() {
        // The gunwale offset is C1 in the waterline half-breadth: no kink in top view where the
        // full flare takes over (a hard cap min(top, F) turned 31 -> 17 deg there), and it
        // leaves the tip close to the waterline's angle (no hook at the tip).
        let (rise, flare) = (7.0, 3f64.to_radians().tan());
        let f = rise * flare;
        let sheer = |top: f64| flared(top, rise, rise, flare);
        let slope = |top: f64| (sheer(top + 1e-7) - sheer(top - 1e-7)) / 2e-7;
        assert_eq!(sheer(0.0), 0.0);
        let tip = 1.0 + 2.0 * f / FLARE_EASE_IN;
        assert!(
            tip < 1.2 && (slope(1e-6) - tip).abs() < 1e-3,
            "sheer leaves the tip near the waterline's angle"
        );
        for k in 1..400 {
            let top = 1.2 * FLARE_EASE_IN * k as f64 / 400.0;
            assert!(sheer(top) <= 2.0 * top + 1e-12);
            assert!((slope(top) - slope(top + 1e-5)).abs() < 1e-3, "corner at top = {top}");
        }
        assert_eq!(sheer(FLARE_EASE_IN), FLARE_EASE_IN + f);
        // A big flare eases over 2 F: still C1 and never wider than 2x the waterline.
        let big = |top: f64| flared(top, 7.0, 7.0, 1.0);
        assert!(big(3.0) <= 6.0 && (big(14.0) - 21.0).abs() < 1e-12);
        assert_eq!(flared(1.0, 7.0, 7.0, 0.0), 1.0, "no flare: unchanged");
    }

    #[test]
    fn gunwale_is_line_arc_line() {
        let g = Gunwale::new(0.0, 240.0, 120.0, 12.0, 400.0).unwrap();
        let n = 2001;
        let xs: Vec<f64> = (0..n).map(|i| 240.0 * i as f64 / (n - 1) as f64).collect();
        let ys: Vec<f64> = xs.iter().map(|&x| g.half_breadth(x)).collect();
        assert_eq!((ys[0], ys[n - 1]), (0.0, 0.0));
        assert!((g.half_breadth(120.0) - 12.0).abs() < 1e-12, "beam at the peak");
        // Straight up to the tangent point, then the circle; tangent: slopes match there.
        let slope_line = g.bow[1] / g.bow[0];
        let h = 1e-6;
        let slope_arc = (g.half_breadth(g.bow[0] + h) - g.half_breadth(g.bow[0])) / h;
        assert!((slope_line - slope_arc).abs() < 1e-4, "{slope_line} vs {slope_arc}");
        let mid = 0.5 * g.bow[0];
        assert!((g.half_breadth(mid) - slope_line * mid).abs() < 1e-12);
        let (dx, dy) = (g.bow[0] - 120.0, g.bow[1] - (12.0 - 400.0));
        assert!((dx.hypot(dy) - 400.0).abs() < 1e-9, "tangent point on the circle");
        // Concave everywhere (second differences <= 0).
        assert!(ys.windows(3).all(|w| w[0] + w[2] - 2.0 * w[1] <= 1e-9));
        // Too large a radius reaches the tip: an error that names the limit.
        let e = Gunwale::new(0.0, 240.0, 120.0, 12.0, 700.0).unwrap_err();
        assert!(e.contains("too large") && e.contains("max 606"), "{e}");
    }

    #[test]
    fn gunwale_widens_until_it_encloses_the_waterline() {
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let nw = h.z.len();
        let top = h.half_breadth.iter().map(|r| r[nw - 1]).fold(0.0, f64::max);
        let g = Gunwale::of_hull(&h, 12.0, 0.0, 200.0).unwrap();
        assert!(g.deficit(&h, 0.0).1 <= 1e-9, "encloses");
        assert!(g.b > top, "off1's full ends need a gunwale wider than the waterline");
        // Narrowest: a hair narrower no longer encloses.
        let tighter = Gunwale::new(g.x0, g.x1, g.xc, g.b - 1e-3, 200.0).unwrap();
        assert!(tighter.deficit(&h, 0.0).1 > 0.0);
        // Flare = demolding draft: every station with waterline breadth leans out at least it.
        let off = (12.0 - h.draft_design) * 4f64.to_radians().tan();
        let drafted = Gunwale::of_hull(&h, 12.0, 4.0, 200.0).unwrap();
        assert!(drafted.b > g.b && drafted.deficit(&h, off).1 <= 1e-9);
        for (&x, row) in h.x.iter().zip(&h.half_breadth) {
            if row[nw - 1] > 0.0 {
                let lean =
                    ((drafted.half_breadth(x) - row[nw - 1]) / (12.0 - h.draft_design)).atan().to_degrees();
                assert!(lean >= 4.0 - 1e-6, "x {x}: lean {lean}");
            }
        }
        let _ = top;
        // Too large a radius: no outline fits, with the reason.
        let e = Gunwale::of_hull(&h, 12.0, 0.0, 5000.0).unwrap_err();
        assert!(e.contains("no gunwale of radius 5000") || e.contains("too large"), "{e}");
    }

    #[test]
    fn gunwale_topsides_are_ruled_and_reject_tumblehome() {
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let plain = Topsides { flare_deg: 10.0, ..Topsides::default() };
        assert_eq!(Grid::build_topsides(&h, 14.0, 161, plain).0.y, Grid::build_flared(&h, 14.0, 161, 10.0).y);
        // off1's full bow needs a very wide gunwale: allow any lean to test the geometry.
        let ts = Topsides { flare_deg: 3.0, gunwale_radius_in: Some(200.0), gunwale_max_flare_deg: 89.0 };
        let (g, why) = Grid::build_topsides(&h, 14.0, 161, ts);
        let gw = Gunwale::of_hull(&h, 14.0, 3.0, 200.0).unwrap();
        let (nw, nz) = (h.z.len(), g.nz());
        let vertical = Grid::build(&h, 14.0, 161);
        for i in [10, 40, 80, 120, 150] {
            let top = g.y(i, nz - 1);
            assert!((top - gw.half_breadth(g.x[i])).abs() < 1e-12, "gunwale at the outline");
            // Ruled: half-breadth linear in z between the waterline and the gunwale.
            let (z0, y0) = (h.draft_design, g.y(i, nw - 1));
            for k in nw..nz {
                let want = y0 + (top - y0) * (g.z[k] - z0) / (14.0 - z0);
                assert!((g.y(i, k) - want).abs() < 1e-12);
            }
            for k in 0..nw {
                assert_eq!(g.y(i, k), vertical.y(i, k), "underwater unchanged");
            }
        }
        assert!(why.is_none(), "{why:?}");
        assert!((0..g.nx()).all(|i| g.y(i, nz - 1) >= g.y(i, nw - 1) - 1e-9), "sides never lean in");
        // With the default lean limit the same design is rejected, naming the lean.
        let (_, why) = Grid::build_topsides(
            &h,
            14.0,
            161,
            Topsides { gunwale_max_flare_deg: GUNWALE_MAX_FLARE_DEG, ..ts },
        );
        assert!(why.as_deref().is_some_and(|w| w.contains("lean out") && w.contains("too full")), "{why:?}");
        // The bow forces a wide gunwale everywhere: about 73 deg of lean at midships.
        let (lx, lean) = gw.max_lean(&h, 14.0);
        assert!(lean > 60.0 && lx > 0.0 && lx < h.length, "{lean} at {lx}");
        let (_, why) = Grid::build_topsides(&h, h.draft_design, 161, ts);
        assert!(why.is_some_and(|w| w.contains("above the design draft")));
    }

    /// Table hull from a half-breadth function, x over [0, l], z over [0, d].
    fn table(l: f64, d: f64, nx: usize, nz: usize, f: impl Fn(f64, f64) -> f64) -> Hull {
        let x = linspace(0.0, l, nx);
        let z = linspace(0.0, d, nz);
        let half_breadth = x.iter().map(|&xi| z.iter().map(|&zk| f(xi, zk).max(0.0)).collect()).collect();
        Hull {
            format: FORMAT.into(),
            units: "in".into(),
            name: "t".into(),
            source: "test".into(),
            length: l,
            draft_design: d,
            x,
            z,
            half_breadth,
            stem: None,
        }
    }

    /// Grid columns and their first row with breadth.
    fn col(g: &Grid, i: usize) -> &[f64] {
        g.row(i)
    }

    /// Checks of a stem radius on grid `g` against the uncut grid `p` (same stations and rows):
    /// nothing below the keel line, sections never narrower higher up (no fold), concrete only
    /// removed, columns aft of the arcs and the gunwale untouched. Returns the arc columns seen.
    fn stem_checks(h: &Hull, g: &Grid, p: &Grid) -> usize {
        let loft = h.loft();
        let st = h.stem.as_ref().unwrap();
        let nz = g.nz();
        assert_eq!((g.z.clone(), g.x.clone()), (p.z.clone(), p.x.clone()));
        let mut cut = 0;
        for i in 0..g.nx() {
            let x = g.x[i];
            let (y, y0) = (col(g, i), col(p, i));
            let k = loft.keel(x).unwrap();
            for j in 0..nz {
                assert!(y[j] <= y0[j] + 1e-9, "concrete only removed: x {x} z {}", g.z[j]);
                if g.z[j] < k - 1e-9 {
                    assert_eq!(y[j], 0.0, "nothing below the keel: x {x} z {}", g.z[j]);
                }
                if j > 0 && y[j - 1] > 0.0 {
                    assert!(
                        y[j] >= y[j - 1] - 1e-9,
                        "section folds at x {x} z {}: {} < {}",
                        g.z[j],
                        y[j],
                        y[j - 1]
                    );
                }
            }
            assert_eq!(y[nz - 1], y0[nz - 1], "gunwale unchanged at x {x}");
            let d = x.min(h.length - x);
            let xt = [st.bow, st.stern].iter().flatten().map(|e| e[2]).fold(0.0, f64::max);
            if d > xt + 1e-9 {
                assert_eq!(y, y0, "untouched aft of the arc at x {x}");
            } else if y != y0 {
                cut += 1;
            }
        }
        cut
    }

    #[test]
    fn forefoot_gets_a_side_view_arc_and_keeps_sections_and_entry_pointed() {
        // V sections (half-breadth = 2 (z - keel), capped at 5) under a keel that rises toward
        // a plumb stem at x = 0 (keel 2 at x = 6, 1 amidships): a pointed forefoot corner.
        let keel = |x: f64| 1.0 + (1.0 - x.min(60.0 - x) / 12.0).max(0.0);
        let h = table(60.0, 6.0, 31, 49, |x, z| {
            if x <= 0.0 || x >= 60.0 { 0.0 } else { (2.0 * (z - keel(x))).min(5.0) }
        });
        let r = 1.0;
        let f = h.filleted(r).unwrap();
        assert_eq!(f.half_breadth, h.half_breadth, "the table itself is kept");
        let [xe, zc, xt] = f.stem.as_ref().unwrap().bow.unwrap();
        assert_eq!(f.stem.as_ref().unwrap().stern.unwrap(), [xe, zc, xt], "stern mirrors the bow");
        let loft = f.loft();
        // The arc: vertical at the stem (its top on the stem at zc), tangent to the keel line where
        // it meets it (same height and slope), the keel line beyond.
        assert!((loft.keel(xe).unwrap() - zc).abs() < 1e-9 && xt > xe && xt < xe + 2.0 * r);
        let (k, kc) = (|x: f64| loft.table_keel(x).unwrap(), |x: f64| loft.keel(x).unwrap());
        assert!((kc(xt) - k(xt)).abs() < 1e-3, "{} vs {}", kc(xt), k(xt));
        let e = 1e-4;
        let slope = |f: &dyn Fn(f64) -> f64, x: f64| (f(x + e) - f(x - e)) / (2.0 * e);
        assert!((slope(&kc, xt - 2.0 * e) - slope(&k, xt)).abs() < 0.05, "tangent where it meets the keel");
        assert!(kc(xe + 0.01) < zc && zc - kc(xe + 0.01) < 0.2, "steep at the stem");
        let ts = Topsides { flare_deg: 0.0, gunwale_radius_in: None, gunwale_max_flare_deg: 30.0 };
        let xs = linspace(0.0, 60.0, 1201); // 0.05 in
        let (g, why) = Grid::build_topsides_at(&f, 8.0, &xs, ts);
        assert!(why.is_none(), "{why:?}");
        // The same rows uncut (the flared extrusion before the stem radius).
        let p = Grid::build_flared_at(&f, 8.0, &xs, 0.0);
        assert!(stem_checks(&f, &g, &p) > 10);
        // The profile is the arc: at every column the first row with breadth is at the keel line.
        let nz = g.nz();
        for i in (1..g.nx()).filter(|&i| g.x[i] <= xt) {
            let c = col(&g, i);
            let j = c.iter().position(|&v| v > 0.0).unwrap();
            assert!(
                g.z[j] >= kc(g.x[i]) - 1e-9 && (j == 0 || g.z[j - 1] < kc(g.x[i]) + 1e-9),
                "x {}",
                g.x[i]
            );
        }
        // Waterlines stay pointed: just aft of where a waterline starts it is still V-thin.
        for j in (0..nz).filter(|&j| g.z[j] > zc - r && g.z[j] < zc) {
            let i = (0..g.nx()).position(|i| g.y(i, j) > 0.0).unwrap();
            assert!(g.y(i, j) < 0.2, "waterline z {} starts blunt: {}", g.z[j], g.y(i, j));
        }
    }

    #[test]
    fn forefoot_on_off1_keeps_the_table_and_length() {
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let f = h.filleted(1.0).unwrap();
        assert_eq!((f.x.clone(), f.half_breadth.clone()), (h.x.clone(), h.half_breadth.clone()));
        let st = f.stem.as_ref().unwrap();
        assert!(st.top_in() <= h.draft_design, "a 1 in arc ends inside the table");
        let g = Grid::build_topsides(
            &f,
            12.0,
            161,
            Topsides { flare_deg: 0.0, gunwale_radius_in: None, gunwale_max_flare_deg: 30.0 },
        )
        .0;
        // Same length overall: the stems' tops do not move.
        let nz = g.nz();
        assert!(g.y(1, nz - 1) > 0.0 && g.y(g.nx() - 2, nz - 1) > 0.0);
        assert!(h.filleted(0.0).is_err() && f.filleted(1.0).is_err());
    }

    #[test]
    fn stem_radius_taller_than_the_table_runs_on_into_the_topsides() {
        // off1's stem foot is near z 4.1 and its table stops at 5.91 in: a 4 in arc goes on above
        // the design waterline, to about z = 8, in the topsides.
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let r = 4.0;
        let f = h.filleted(r).unwrap();
        let st = f.stem.as_ref().unwrap();
        let [xe, zc, xt] = st.bow.unwrap();
        let top = h.draft_design;
        assert!(zc > top + 1.0 && zc < 9.0, "{zc}");
        for flare in [0.0, 3.0] {
            let ts = Topsides { flare_deg: flare, gunwale_radius_in: None, gunwale_max_flare_deg: 30.0 };
            let xs = linspace(0.0, h.length, 2401); // 0.1 in
            let (g, why) = Grid::build_topsides_at(&f, 12.0, &xs, ts);
            assert!(why.is_none(), "{why:?}");
            let p = Grid::build_flared_at(&f, 12.0, &xs, flare);
            let nz = g.nz();
            // Rows every r/8 up the arc (the uncut grid has the same rows without a cut).
            let on_arc = g.z.iter().filter(|&&z| z > top && z <= zc).count();
            assert!(on_arc >= ((zc - top) / (0.125 * r)) as usize, "{on_arc}");
            assert!(stem_checks(&f, &g, &p) > 10);
            // Every topsides row on the arc starts on it, bow and stern alike.
            let front = |k: usize| g.x[(0..g.nx()).position(|i| g.y(i, k) > 0.0).unwrap().saturating_sub(1)];
            let back =
                |k: usize| g.x[((0..g.nx()).rposition(|i| g.y(i, k) > 0.0).unwrap() + 1).min(g.nx() - 1)];
            for k in (0..nz).filter(|&k| g.z[k] > top && g.z[k] < zc) {
                let z = g.z[k];
                let want = xe + r - (r * r - (z - zc) * (z - zc)).max(0.0).sqrt();
                assert!((front(k) - want).abs() <= 0.1 + 1e-9, "z {z}: {} vs {want}", front(k));
                assert!((h.length - back(k) - want).abs() <= 0.1 + 1e-9, "stern z {z}");
            }
            assert!(xt > xe && (front(nz - 1) - xe).abs() < 1e-9, "the top of the stem stays");
        }
        // A mold too shallow for the arc is unbuildable, with why; the grid stays real.
        let ts = Topsides { flare_deg: 0.0, gunwale_radius_in: None, gunwale_max_flare_deg: 30.0 };
        let (_, why) = Grid::build_topsides(&f, zc - 0.5, 161, ts);
        assert!(why.as_deref().is_some_and(|w| w.contains("stem radius 4 in")), "{why:?}");
        // The arcs travel with the hull (result.json winner_hull): a JSON round trip keeps them.
        let back: Hull = serde_json::from_str(&serde_json::to_string(&f).unwrap()).unwrap();
        let bs = back.stem.as_ref().unwrap();
        assert!((bs.top_in() - st.top_in()).abs() < 1e-9);
        assert!(!serde_json::to_string(&h).unwrap().contains("stem"), "hull files carry no stem field");
    }

    #[test]
    fn templates_are_exact_sections_lofted_straight() {
        let h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        let t = h.templated(9);
        t.validate().unwrap();
        assert_eq!(t.x.len(), 9);
        assert_eq!((t.x[0], t.x[8]), (h.x[0], h.x[h.x.len() - 1]), "tips kept");
        // Cosine spacing: end gaps are the shortest, the middle gap the longest.
        let gaps: Vec<f64> = t.x.windows(2).map(|w| w[1] - w[0]).collect();
        assert!(gaps[0] < gaps[3] && (gaps[0] - gaps[7]).abs() < 1e-9);
        // Each template is the source section at its station (the loft there).
        let (nz, loft) = (h.z.len(), h.loft());
        for (i, &x) in t.x.iter().enumerate() {
            for k in 0..nz {
                assert!((t.half_breadth[i][k] - loft.breadth(x, h.z[k])).abs() < 1e-12);
            }
        }
        // Between templates the loft is straight: the analysis grid interpolates linearly.
        let g = Grid::build(&t, h.draft_design, 401);
        let (a, b) = (t.x[3], t.x[4]);
        for i in 0..g.nx() {
            if g.x[i] > a && g.x[i] < b {
                let f = (g.x[i] - a) / (b - a);
                let want = t.half_breadth[3][nz - 1] * (1.0 - f) + t.half_breadth[4][nz - 1] * f;
                assert!((g.y(i, nz - 1) - want).abs() < 1e-9);
            }
        }
    }

    #[test]
    fn rejects_metres() {
        let mut h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        h.units = "m".into();
        assert!(h.validate().is_err());
    }
}
