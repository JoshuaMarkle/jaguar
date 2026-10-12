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

    /// Resample `hull` to `n` stations and extrude the top waterline up to
    /// `depth`. Matches the prototype `build()` (golden-tested).
    pub fn build(hull: &Hull, depth: f64, n: usize) -> Grid {
        Self::build_flared(hull, depth, n, 0.0)
    }

    /// Like [`Grid::build`], but the topsides above the design waterline lean
    /// OUTWARD by `flare_deg` from vertical: half-breadth grows by
    /// `(z - draft_design) * tan(flare)` at every station that has a waterline
    /// (stations with zero top half-breadth, the stem tips, stay closed).
    /// 0 = vertical sides (the prototype). Flare adds wall area (weight), raises
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
        let imported = Hull::from_michlet(Path::new("old/off1.mlt"), Path::new("old/out.mlt")).unwrap();
        let golden = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        assert!((imported.length - golden.length).abs() < 1e-3);
        assert!((imported.draft_design - golden.draft_design).abs() < 1e-4);
        let (a, b) = (&imported.half_breadth, &golden.half_breadth);
        let max = a.iter().flatten().zip(b.iter().flatten()).map(|(p, q)| (p - q).abs()).fold(0.0, f64::max);
        // 1e-4 in: the golden file used the legacy 39.3701 in/m, we use the exact factor.
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
    fn rejects_metres() {
        let mut h = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        h.units = "m".into();
        assert!(h.validate().is_err());
    }
}
