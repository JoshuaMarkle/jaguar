//! Parametric hull generator: a port of `old/hull_generator.py::generate_hull`
//! (Michlet Series 7 "kayak" shape function). Produces a `jaguar-hull/1`
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
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
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
    let (mut f5, mut f6) = (p.fore_len_frac, p.aft_len_frac);
    if f5 + f6 > 1.0 {
        let s = f5 + f6;
        f5 /= s;
        f6 /= s;
    }
    let (l_fore, l_aft) = (l * f5, l * f6);
    let x = linspace(0.0, l, n_stations);
    let z = linspace(0.0, t, n_waterlines);
    let taper = |d: f64, zone: f64| {
        let u = 0.5 * if zone > 0.0 { d / zone } else { 1.0 };
        4.0 * u * (1.0 - u)
    };
    let half_breadth = x
        .iter()
        .map(|&xg| {
            let (xx, pw, pr) = if xg < l_fore {
                (taper(xg, l_fore), p.fore_wp, p.fore_rocker)
            } else if xg > l - l_aft {
                (taper(l - xg, l_aft), p.aft_wp, p.aft_rocker)
            } else {
                (1.0, 0.0, 0.0)
            };
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
        }
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
