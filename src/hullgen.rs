//! Parametric hull generator (Michlet Series 7 "kayak" shape function). Produces a `jaguar-hull/1`
//! [`Hull`] in inches, bow at x = 0, keel at z = 0, half-breadths.
//!
//! Station shape: in the fore and aft taper zones a longitudinal factor
//! `X = 4u(1-u)`, `u = 0.5 * (distance from end / zone length)`, goes from 0
//! at the end to 1 where the zone meets the parallel midbody (X = 1).
//! Half-breadth at depth fraction `zn = (draft - z)/draft` is
//! `0.5 * beam * X^wp * (1 - zn^2 / X^(2 rocker))^section`, and zero below
//! the keel line where `X^rocker < zn`.

use crate::hull::{FORMAT, Hull, linspace};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ShapeParams {
    /// Overall length, in.
    pub length_in: f64,
    /// Maximum beam at the design waterline, in (half-breadth max = beam/2).
    pub beam_in: f64,
    /// Design draft (top waterline of the table), in.
    pub draft_in: f64,
    /// Fore waterplane exponent (>= 0). Larger = finer, more hollow bow
    /// waterlines; 0 = full width all the way to the stem (blunt).
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
    let y = 0.5 * p.beam_in * xx.powf(pw);
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
    let x = linspace(0.0, l, n_stations);
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
                    0.5 * xx.powf(pw) * g.powf(p.section) * b
                })
                .collect()
        })
        .collect();
    let h = Hull {
        format: FORMAT.into(),
        units: "in".into(),
        name: name.into(),
        source: format!(
            "jaguar gen-hull (Michlet Series 7): L={l} B={b} T={t} wp={}/{} section={} rocker={}/{} zones={}/{}",
            p.fore_wp, p.aft_wp, p.section, p.fore_rocker, p.aft_rocker, p.fore_len_frac, p.aft_len_frac
        ),
        length: l,
        draft_design: t,
        x,
        z,
        half_breadth,
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
            metrics: None,
        }
    }

    #[test]
    fn metrics_match_the_generated_table() {
        let s = shape();
        let m = metrics(&s);
        // Keel rise from the table: lowest wetted waterline at the 5% station.
        let h = generate(&s, 241, 601, "t").unwrap();
        let i = 12; // x = 0.05 L
        assert!((h.x[i] - END_FRAC * s.length_in).abs() < 1e-9);
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

    #[test]
    fn midbody_has_full_beam_and_ends_are_zero() {
        let h = generate(&shape(), 41, 21, "t").unwrap();
        let mid = &h.half_breadth[20];
        let max = mid.iter().cloned().fold(0.0, f64::max);
        assert!((max - 12.0).abs() < 1e-12);
        assert!((mid[20] - 12.0).abs() < 1e-12);
        assert!(h.half_breadth[0].iter().all(|v| *v == 0.0));
        assert!(h.half_breadth[40].iter().all(|v| *v == 0.0));
        h.validate().unwrap();
    }

    #[test]
    fn rejects_bad_input() {
        let mut s = shape();
        s.beam_in = -1.0;
        assert!(generate(&s, 41, 21, "t").is_err());
    }
}
