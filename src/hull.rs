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
        let mut g = Self::build_flared(hull, depth, n, ts.flare_deg);
        let Some(r) = ts.gunwale_radius_in else { return (g, None) };
        let dd = hull.draft_design;
        if depth <= dd {
            let why =
                format!("gunwale outline: mold depth {depth} in must be above the design draft {dd:.2} in");
            return (g, Some(why));
        }
        let gw = match Gunwale::of_hull(hull, depth, ts.flare_deg, r) {
            Ok(gw) => gw,
            Err(e) => return (g, Some(e)),
        };
        let nw = hull.z.len();
        // Concave outline vs piecewise-linear waterline: the table stations decide.
        // `of_hull` already widened the outline to enclose; this guards it.
        let worst = gw.deficit(hull, 0.0);
        let (nz, nx) = (g.z.len(), g.x.len());
        for i in 0..nx {
            let (yw, yg) = (g.y[i * nz + nw - 1], gw.half_breadth(g.x[i]));
            for k in nw..nz {
                g.y[i * nz + k] = yw + (yg - yw) * (g.z[k] - dd) / (depth - dd);
            }
        }
        let (lean_x, lean) = gw.max_lean(hull, depth);
        let why = if worst.1 > 1e-6 {
            Some(format!(
                "gunwale R {r:.0} in sits {:.2} in inside the waterline at x = {:.1} in (sides would lean in)",
                worst.1, worst.0
            ))
        } else if lean > ts.gunwale_max_flare_deg {
            Some(format!(
                "gunwale R {r:.0} in needs the side to lean out {lean:.1} deg at x = {lean_x:.1} in \
                 (gunwale_max_flare_deg {}): the waterline ends are too full for a straight entry from the tips; \
                 try another radius or a deeper mold",
                ts.gunwale_max_flare_deg
            ))
        } else {
            None
        };
        (g, why)
    }

    /// Resample `hull` to `n` stations and extrude the top waterline up to
    /// `depth`. Matches `tests/golden/reference.py` `build()` (golden-tested).
    pub fn build(hull: &Hull, depth: f64, n: usize) -> Grid {
        Self::build_flared(hull, depth, n, 0.0)
    }

    /// Like [`Grid::build`], but the topsides above the design waterline lean
    /// OUTWARD by `flare_deg` from vertical: half-breadth grows by
    /// `(z - draft_design) * tan(flare)` at every station that has a waterline
    /// (stations with zero top half-breadth, the stem tips, stay closed).
    /// 0 = vertical sides (as in the golden reference). Flare adds wall area (weight), raises
    /// reserve buoyancy and widens the gunwale (stability, freeboard).
    pub fn build_flared(hull: &Hull, depth: f64, n: usize, flare_deg: f64) -> Grid {
        let flare = flare_deg.to_radians().tan();
        let x = linspace(0.0, hull.length, n);
        let mut z = hull.z.clone();
        let extrude = depth > hull.draft_design;
        if extrude {
            z.extend(linspace(hull.draft_design + 0.1, depth, TOPSIDE_LINES));
        }
        let nz = z.len();
        let nw = hull.z.len();
        let mut y = vec![0.0; n * nz];
        for (i, &xi) in x.iter().enumerate() {
            for k in 0..nw {
                y[i * nz + k] = interp_col(&hull.x, &hull.half_breadth, k, xi);
            }
            let top = y[i * nz + nw - 1];
            for k in nw..nz {
                let rise = z[k] - hull.draft_design;
                y[i * nz + k] = if top > 0.0 { top + rise * flare } else { top };
            }
        }
        Grid { x, z, y, depth }
    }
}

pub fn linspace(a: f64, b: f64, n: usize) -> Vec<f64> {
    if n == 1 {
        return vec![a];
    }
    (0..n).map(|i| a + (b - a) * i as f64 / (n - 1) as f64).collect()
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

    #[test]
    fn rejects_metres() {
        let mut h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        h.units = "m".into();
        assert!(h.validate().is_err());
    }
}
