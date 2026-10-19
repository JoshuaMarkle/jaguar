//! Parametric hull generator (Michlet Series 7 "kayak" shape function). Produces a `jaguar-hull/1`
//! [`Hull`] in inches, bow at x = 0, keel at z = 0, half-breadths.
//!
//! Station shape: in the fore and aft taper zones a longitudinal factor
//! `X = 4u(1-u)`, `u = 0.5 * (distance from end / zone length)`, goes from 0
//! at the end to 1 where the zone meets the parallel midbody (X = 1).
//! Half-breadth at depth fraction `zn = (draft - z)/draft` is
//! `0.5 * beam * W(X) * (1 - zn^2 / X^(2 rocker))^section`, and zero below
//! the keel line where `X^rocker < zn`. W(X) = X^wp, except near the tips of a full end
//! ([`waterplane`]: pointed, no corner). The table has extra stations near each tip
//! ([`stations`]), so its straight lines between stations follow the curved ends.

use crate::hull::{FORMAT, Hull, column_keel, linspace};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ShapeParams {
    /// Overall length, in.
    pub length_in: f64,
    /// Maximum beam at the design waterline, in (half-breadth max = beam/2).
    pub beam_in: f64,
    /// Design draft of the shape function, in. The table's top waterline is this less the
    /// keel lift [`generate`] removes (0 to ~0.13 in).
    pub draft_in: f64,
    /// Fore waterplane exponent (>= 0). Larger = finer, more hollow bow
    /// waterlines; below 1 = fuller, still pointed at the stem ([`waterplane`]).
    pub fore_wp: f64,
    /// Section exponent (> 0). Small (~0.1) = boxy, near-vertical sides and
    /// a flat bottom; 0.5 = elliptic; 1 = parabolic V-ish sections.
    pub section: f64,
    /// Fore rocker exponent (>= 0). Larger = keel rises sooner toward the bow.
    pub fore_rocker: f64,
    /// Aft waterplane exponent (>= 0), like `fore_wp` for the stern.
    pub aft_wp: f64,
    /// Aft rocker exponent (>= 0), like `fore_rocker` for the stern.
    pub aft_rocker: f64,
    /// Fraction of length in the fore taper zone (0..1).
    pub fore_len_frac: f64,
    /// Fraction of length in the aft taper zone (0..1). If fore + aft > 1
    /// both are scaled down to sum to 1 (no parallel midbody).
    pub aft_len_frac: f64,
    /// Gunwale arc radius, in, when the topsides use the line-arc-line gunwale
    /// (`hull::Gunwale`; set by the shape search, not part of the generated
    /// offsets, which stop at the design waterline).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gunwale_radius_in: Option<f64>,
    /// Measured from the generated shape by the shape search (output only;
    /// not accepted in shape.toml).
    #[serde(default, skip_deserializing, skip_serializing_if = "Option::is_none")]
    pub metrics: Option<ShapeMetrics>,
}

/// Buildability and turning measurements of one shape (docs/SHAPE_SEARCH.md).
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ShapeMetrics {
    /// Design-waterline entry half-angle at the bow, deg: chord of the
    /// waterline half-breadth over the first `END_FRAC` of length.
    pub entry_angle_deg: f64,
    /// Keel rise above the lowest keel point at `END_FRAC` of length from the
    /// bow / stern, in.
    pub rocker_bow_in: f64,
    pub rocker_stern_in: f64,
    /// Second moment of the underwater lateral profile about midships at
    /// design draft, integral of depth(x) (x - L/2)^2 dx, in^4 (the old turning proxy).
    pub lateral_moment_in4: f64,
    /// Steady turn at `objective.turn_radius_in` and design draft
    /// (`resistance::turn`); filled by the shape search.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn: Option<Turn>,
    /// Why the shape search rejected this shape (a [shape_search] constraint), if it did.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rejected: Option<String>,
}

/// Steady turn of a slender hull (computed by `resistance::turn`).
#[derive(Debug, Clone, Copy, Serialize, PartialEq)]
pub struct Turn {
    /// Extra resistance of holding the turn, N: cross-flow power / speed.
    pub drag_n: f64,
    /// Hydrodynamic yaw moment about midships resisting the turn, N m
    /// (positive = the paddlers must steer into the turn).
    pub moment_nm: f64,
    /// Drift angle at midships (stern skids out), deg.
    pub drift_deg: f64,
}

/// Where entry angle and rocker are measured, as a fraction of length from each end.
pub const END_FRAC: f64 = 0.05;

/// Taper zones after scaling (fore, aft lengths, in).
fn zones(p: &ShapeParams) -> (f64, f64) {
    let (mut f5, mut f6) = (p.fore_len_frac, p.aft_len_frac);
    if f5 + f6 > 1.0 {
        let s = f5 + f6;
        f5 /= s;
        f6 /= s;
    }
    (p.length_in * f5, p.length_in * f6)
}

/// (X, waterplane exponent, rocker exponent) at x from the bow.
fn station(p: &ShapeParams, xg: f64) -> (f64, f64, f64) {
    let l = p.length_in;
    let (l_fore, l_aft) = zones(p);
    let taper = |d: f64, zone: f64| {
        let u = 0.5 * if zone > 0.0 { d / zone } else { 1.0 };
        (4.0 * u * (1.0 - u)).max(0.0)
    };
    if xg < l_fore {
        (taper(xg, l_fore), p.fore_wp, p.fore_rocker)
    } else if xg > l - l_aft {
        (taper(l - xg, l_aft), p.aft_wp, p.aft_rocker)
    } else {
        (1.0, 0.0, 0.0)
    }
}

/// Taper factor X below which a full end (wp < 1) is eased to a point (about the last 13% of
/// the taper zone; X = 4u(1 - u), u = 0.5 d / zone).
const TIP_X: f64 = 0.25;

/// Waterplane factor W(X) of a station: X^wp. For wp < 1, X^wp has a vertical tangent at the
/// tip (a round, bulbous nose) that a table with stations 5.4 in apart turned into a straight
/// wedge with a corner at its first station (37 -> 21 deg at wp 0.62). Below [`TIP_X`] it is
/// instead the quadratic in X with the same value and slope there: 0 at the tip, a finite entry
/// angle, no corner. wp >= 1 (straight or hollow ends) is unchanged.
pub fn waterplane(xx: f64, wp: f64) -> f64 {
    if wp >= 1.0 || xx >= TIP_X {
        return xx.powf(wp);
    }
    let s = xx / TIP_X;
    TIP_X.powf(wp) * s * ((2.0 - wp) + (wp - 1.0) * s)
}

/// Even intervals at each end split into [`TIP_SPLIT`] (the curved ends, about 20 in of a 216 in
/// hull at 41 stations).
const TIP_INTERVALS: usize = 4;
const TIP_SPLIT: usize = 4;

/// Table stations: `n` even over the length, plus the first and last [`TIP_INTERVALS`] intervals
/// split in [`TIP_SPLIT`], so the table (linear between stations) follows the curved ends
/// instead of one chord from the tip to the first even station.
pub fn stations(l: f64, n: usize) -> Vec<f64> {
    let mut x = linspace(0.0, l, n);
    if n < 2 {
        return x;
    }
    let h = l / (n - 1) as f64;
    for i in 0..TIP_INTERVALS.min((n - 1) / 2) {
        for k in 1..TIP_SPLIT {
            let d = h * (i as f64 + k as f64 / TIP_SPLIT as f64);
            x.extend([d, l - d]);
        }
    }
    x.sort_by(f64::total_cmp);
    x.dedup_by(|a, b| (*a - *b).abs() < 1e-9);
    x
}

/// Keel height above the lowest keel point at x, in (analytic: X^rocker = zn).
pub fn keel_z(p: &ShapeParams, xg: f64) -> f64 {
    let (xx, _, pr) = station(p, xg);
    // 0^0 = 1: with no rocker the keel runs flat to the stem.
    p.draft_in * (1.0 - xx.powf(pr))
}

/// Measure entry angle, rocker and the lateral-profile moment (no rejection).
pub fn metrics(p: &ShapeParams) -> ShapeMetrics {
    let (l, t) = (p.length_in, p.draft_in);
    let xe = END_FRAC * l;
    let (xx, pw, _) = station(p, xe);
    let y = 0.5 * p.beam_in * waterplane(xx, pw);
    let n = 400;
    let dx = l / n as f64;
    let mut m = 0.0;
    for i in 0..=n {
        let x = (i as f64 * dx).min(l);
        let w = if i == 0 || i == n { 0.5 } else { 1.0 };
        m += w * (t - keel_z(p, x)) * (x - 0.5 * l).powi(2) * dx;
    }
    ShapeMetrics {
        entry_angle_deg: y.atan2(xe).to_degrees(),
        rocker_bow_in: keel_z(p, xe),
        rocker_stern_in: keel_z(p, l - xe),
        lateral_moment_in4: m,
        turn: None,
        rejected: None,
    }
}

/// The table lowered so its lofted keel's lowest point is z = 0 (the mold depth is measured from
/// z = 0). Below the lowest offsets the loft takes a parabola through the two lowest
/// ([`column_keel`]); a section of (1 - zn^2)^section with section > 0.5 is blunter than that at
/// the keel, so the lofted keel floats up to ~0.13 in above the analytic one at z = 0. The z = 0
/// row is all zero (zn = 1), so it stays; the waterlines above move down by that gap and the loft
/// is the same surface, shifted. The top waterline (the design draft) moves down with them.
fn keel_on_zero(z: Vec<f64>, rows: Vec<Vec<f64>>) -> (Vec<f64>, Vec<Vec<f64>>) {
    let kmin = rows.iter().filter_map(|r| column_keel(r, &z)).fold(f64::INFINITY, f64::min);
    if kmin.is_nan() || kmin <= 1e-9 || rows.iter().any(|r| r[0] != 0.0) {
        return (z, rows);
    }
    // Waterlines that would land on or below z = 0 go (only z[1], when kmin reached it).
    let keep: Vec<usize> = std::iter::once(0).chain((1..z.len()).filter(|&k| z[k] - kmin > 1e-9)).collect();
    let z2 = keep.iter().map(|&k| if k == 0 { 0.0 } else { z[k] - kmin }).collect();
    let rows2 = rows.iter().map(|r| keep.iter().map(|&k| r[k]).collect()).collect();
    (z2, rows2)
}

/// The hull table: `n_stations` even stations plus the tip stations ([`stations`]), keel at z = 0
/// ([`keel_on_zero`]).
pub fn generate(p: &ShapeParams, n_stations: usize, n_waterlines: usize, name: &str) -> Result<Hull> {
    let (l, b, t) = (p.length_in, p.beam_in, p.draft_in);
    ensure!(l > 0.0 && b > 0.0 && t > 0.0, "length, beam, draft must be > 0");
    for (v, n) in [
        (p.fore_wp, "fore_wp"),
        (p.section, "section"),
        (p.fore_rocker, "fore_rocker"),
        (p.aft_wp, "aft_wp"),
        (p.aft_rocker, "aft_rocker"),
        (p.fore_len_frac, "fore_len_frac"),
        (p.aft_len_frac, "aft_len_frac"),
    ] {
        ensure!(v.is_finite() && v >= 0.0, "{n} must be finite and >= 0, got {v}");
    }
    ensure!(p.section > 0.0, "section must be > 0");
    let x = stations(l, n_stations);
    let z = linspace(0.0, t, n_waterlines);
    let half_breadth = x
        .iter()
        .map(|&xg| {
            let (xx, pw, pr) = station(p, xg);
            z.iter()
                .map(|&zh| {
                    if xx <= 1e-9 {
                        return 0.0;
                    }
                    let zn = (t - zh) / t;
                    if xx.powf(pr) < zn {
                        return 0.0;
                    }
                    let g = (1.0 - zn * zn / xx.powf(2.0 * pr)).max(0.0);
                    0.5 * waterplane(xx, pw) * g.powf(p.section) * b
                })
                .collect()
        })
        .collect();
    let (z, half_breadth) = keel_on_zero(z, half_breadth);
    let h = Hull {
        format: FORMAT.into(),
        units: "in".into(),
        name: name.into(),
        source: format!(
            "jaguar gen-hull (Michlet Series 7): L={l} B={b} T={t} wp={}/{} section={} rocker={}/{} zones={}/{}",
            p.fore_wp, p.aft_wp, p.section, p.fore_rocker, p.aft_rocker, p.fore_len_frac, p.aft_len_frac
        ),
        length: l,
        draft_design: *z.last().unwrap(),
        x,
        z,
        half_breadth,
        stem: None,
    };
    h.validate()?;
    Ok(h)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shape() -> ShapeParams {
        ShapeParams {
            length_in: 240.0,
            beam_in: 24.0,
            draft_in: 6.0,
            fore_wp: 0.8,
            section: 0.4,
            fore_rocker: 0.3,
            aft_wp: 0.8,
            aft_rocker: 0.3,
            fore_len_frac: 0.4,
            aft_len_frac: 0.4,
            gunwale_radius_in: None,
            metrics: None,
        }
    }

    #[test]
    fn metrics_match_the_generated_table() {
        let s = shape();
        let m = metrics(&s);
        // Keel rise from the table: lowest wetted waterline at the 5% station.
        let h = generate(&s, 241, 601, "t").unwrap();
        let i = h.x.iter().position(|&x| (x - END_FRAC * s.length_in).abs() < 1e-9).unwrap();
        let k = h.half_breadth[i].iter().position(|&v| v > 0.0).unwrap();
        assert!((h.z[k] - m.rocker_bow_in).abs() < 0.02, "{} vs {}", h.z[k], m.rocker_bow_in);
        let y = h.half_breadth[i][600];
        assert!((y.atan2(h.x[i]).to_degrees() - m.entry_angle_deg).abs() < 1e-9);
        assert_eq!(m.rocker_bow_in, m.rocker_stern_in);
        // No rocker: full-depth lateral profile, moment = T L^3 / 12.
        let mut f = s.clone();
        f.fore_rocker = 0.0;
        f.aft_rocker = 0.0;
        let m0 = metrics(&f);
        assert!(m0.rocker_bow_in.abs() < 1e-12);
        let exact = 6.0 * 240f64.powi(3) / 12.0;
        assert!((m0.lateral_moment_in4 - exact).abs() / exact < 1e-4);
        assert!(m.lateral_moment_in4 < m0.lateral_moment_in4, "rocker turns easier");
    }

    /// A section blunter than a parabola at the keel (0.706, a shape-search winner): the loft's
    /// keel would float ~0.13 in above z = 0, and the canoe would be that much shallower than its
    /// mold depth.
    fn blunt() -> ShapeParams {
        ShapeParams {
            length_in: 216.0,
            beam_in: 28.64,
            draft_in: 7.0,
            fore_wp: 0.818,
            section: 0.706,
            fore_rocker: 0.221,
            aft_wp: 0.818,
            aft_rocker: 0.221,
            fore_len_frac: 0.5,
            aft_len_frac: 0.5,
            gunwale_radius_in: None,
            metrics: None,
        }
    }

    #[test]
    fn lofted_keel_sits_on_z_zero() {
        let s = blunt();
        let h = generate(&s, 41, 21, "t").unwrap();
        let loft = h.loft();
        let kmin = (0..=2160).map(|i| loft.keel(0.1 * i as f64).unwrap()).fold(f64::INFINITY, f64::min);
        assert!(kmin.abs() < 1e-9, "lofted keel at {kmin}");
        // The same surface, lowered: each waterline has the analytic offsets of its old height.
        let lift = s.draft_in - h.draft_design;
        assert!(lift > 0.1 && lift < 0.15, "lift {lift}");
        let mid = h.x.iter().position(|&x| x == 108.0).unwrap();
        for (k, &z) in h.z.iter().enumerate().skip(1) {
            let zn = (s.draft_in - (z + lift)) / s.draft_in;
            let y = 0.5 * s.beam_in * (1.0 - zn * zn).powf(s.section);
            assert!((h.half_breadth[mid][k] - y).abs() < 1e-9, "z {z}");
        }
        // A parabola-like section needs no lift.
        let h = generate(&shape(), 41, 21, "t").unwrap();
        assert_eq!(h.draft_design, 6.0);
    }

    #[test]
    fn midbody_has_full_beam_and_ends_are_zero() {
        let h = generate(&shape(), 41, 21, "t").unwrap();
        let mid = &h.half_breadth[h.x.iter().position(|&x| x == 120.0).unwrap()];
        let max = mid.iter().cloned().fold(0.0, f64::max);
        assert!((max - 12.0).abs() < 1e-12);
        assert!((mid[20] - 12.0).abs() < 1e-12);
        assert!(h.half_breadth[0].iter().all(|v| *v == 0.0));
        assert!(h.half_breadth.last().unwrap().iter().all(|v| *v == 0.0));
        h.validate().unwrap();
    }

    #[test]
    fn tip_stations_split_the_end_intervals() {
        let x = stations(240.0, 41);
        assert_eq!(x.len(), 41 + 2 * TIP_INTERVALS * (TIP_SPLIT - 1));
        assert_eq!((x[0], x[1], *x.last().unwrap()), (0.0, 1.5, 240.0));
        assert!(linspace(0.0, 240.0, 41).iter().all(|e| x.contains(e)), "even stations kept");
        assert!(x.windows(2).all(|w| w[1] > w[0]));
        assert_eq!(stations(240.0, 3).len(), 3 + 2 * (TIP_SPLIT - 1));
    }

    #[test]
    fn full_ends_are_pointed_without_a_corner() {
        // wp < 1: X^wp is vertical at the tip (round nose); W is 0 there with a finite slope,
        // and value and slope match X^wp at TIP_X. wp >= 1 is untouched.
        for wp in [0.0, 0.1, 0.3, 0.62, 0.95] {
            assert_eq!(waterplane(0.0, wp), 0.0);
            let tip = waterplane(1e-6, wp) / 1e-6;
            assert!(tip.is_finite() && tip > 0.0 && tip <= (2.0 - wp) * TIP_X.powf(wp - 1.0) + 1e-9);
            let d = 1e-7;
            let (lo, hi) = (TIP_X - d, TIP_X + d);
            assert!((waterplane(lo, wp) - TIP_X.powf(wp)).abs() < 1e-6);
            let (sl, sr) = (
                (waterplane(TIP_X, wp) - waterplane(lo, wp)) / d,
                (waterplane(hi, wp) - waterplane(TIP_X, wp)) / d,
            );
            assert!((sl - sr).abs() < 1e-4, "wp {wp}: slope {sl} vs {sr} at TIP_X");
            let mut prev = 0.0;
            for k in 1..=100 {
                let w = waterplane(TIP_X * k as f64 / 100.0, wp);
                assert!(w > prev, "monotone");
                prev = w;
            }
        }
        for wp in [1.0, 1.6] {
            assert_eq!(waterplane(0.1, wp), 0.1f64.powf(wp));
        }
        // The generated table: at wp 0.62 the bow waterline turns smoothly (no 16 deg corner).
        let s = ShapeParams { length_in: 216.0, beam_in: 30.0, fore_wp: 0.62, aft_wp: 0.62, ..shape() };
        let h = generate(&s, 41, 21, "t").unwrap();
        let top = h.z.len() - 1;
        let ang: Vec<f64> =
            h.x.windows(2)
                .zip(h.half_breadth.windows(2))
                .take(20)
                .map(|(x, y)| ((y[1][top] - y[0][top]) / (x[1] - x[0])).atan().to_degrees())
                .collect();
        let worst = ang.windows(2).map(|a| (a[1] - a[0]).abs()).fold(0.0, f64::max);
        assert!(worst < 3.0, "waterline corner {worst} deg: {ang:?}");
    }

    #[test]
    fn rejects_bad_input() {
        let mut s = shape();
        s.beam_in = -1.0;
        assert!(generate(&s, 41, 21, "t").is_err());
    }
}
