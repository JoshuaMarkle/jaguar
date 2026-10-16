//! Performance curves of the winner (docs/SPEC.md, "Curves"): pictures of how the solved canoe
//! behaves, computed once after the search (`optimize::winner_curves`), never used by it.
//!
//! * Power: effective (towing) power P = R(v) v, R = Michell wave + ITTC friction
//!   (`resistance`), at the level draft of hull + crew for each load case (real crew weight, no
//!   load factors), 3 to 11 ft/s. The paddlers must deliver P / propulsive efficiency.
//! * Righting lever GZ(phi), 0 to 180 deg of heel, for each load case and the empty canoe.
//!   Each station's section is CLOSED at the gunwale (a deck at the mold depth) and cut by the
//!   heeled waterline -y sin(phi) + z cos(phi) = h (starboard down for phi > 0); h is solved
//!   so the displaced volume floats the weight. GZ = Y_B - Y_G, Y = y cos(phi) + z sin(phi),
//!   G on the centreline at KG (wall at its section centroids, crew at `crew_cg_in`, as in the
//!   GM check). Small angles: GZ ~ GM phi. Level trim (heel only), crew fixed to the hull.
//!   An open canoe takes water once the gunwale goes under: `gunwale_immersion_deg` is that
//!   angle; beyond it the closed-hull curve shows the hull shape, not a canoe that stays dry.

use crate::hull::{Grid, Hull};
use crate::resistance::{Resistance, level_draft_in, resistance};
use crate::section::Sections;
use crate::units::{IN3_PER_FT3, WATER_PCF};
use anyhow::{Result, bail};
use serde::Serialize;

/// ft/s -> m/s.
const MPS_PER_FPS: f64 = 0.3048;

/// Speeds of the power curve, ft/s: 3 to 11 in 0.25 steps.
pub fn speeds_fps() -> Vec<f64> {
    (0..=32).map(|k| 3.0 + 0.25 * k as f64).collect()
}

/// Heel angles of the GZ curve, deg: 0 to 180 in 2 deg steps.
pub fn angles_deg() -> Vec<f64> {
    (0..=90).map(|k| 2.0 * k as f64).collect()
}

/// Effective power of one loading over `speeds_fps`.
#[derive(Debug, Clone, Serialize)]
pub struct PowerCurve {
    pub name: String,
    pub crew_lb: f64,
    /// Hull + crew, lb.
    pub displacement_lb: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub draft_in: Option<f64>,
    /// Total resistance, N, and effective power R v, W, per speed.
    pub drag_n: Vec<f64>,
    pub power_w: Vec<f64>,
}

/// Righting lever of one loading over `angles_deg`.
#[derive(Debug, Clone, Serialize)]
pub struct GzCurve {
    pub name: String,
    pub weight_lb: f64,
    pub kg_in: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub gz_in: Vec<f64>,
    /// Largest GZ and its angle.
    pub max_gz_in: Option<f64>,
    pub max_gz_deg: Option<f64>,
    /// First angle past which GZ is no longer positive (the range of stability ends), deg.
    pub vanishing_deg: Option<f64>,
    /// First angle at which the gunwale is under water at some station (an open canoe
    /// starts to take water there); None if it stays dry to 180 deg.
    pub gunwale_immersion_deg: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct Curves {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub speeds_fps: Vec<f64>,
    pub power: Vec<PowerCurve>,
    pub angles_deg: Vec<f64>,
    pub gz: Vec<GzCurve>,
    /// Drag broken down (wave, friction, Re, Cf, wetted surface) at `objective.speed_mps` for
    /// each load case (hull + real crew, level draft), for the report's drag derivation.
    pub drag: Vec<DragCase>,
    /// With `objective.displacements`: the same for each weighted displacement; the objective
    /// drag is the sum of weight x total over these.
    pub objective_drag: Vec<DragCase>,
    pub drag_speed_mps: f64,
    pub roughness_um: f64,
}

/// One loading's resistance at one speed, every intermediate value.
#[derive(Debug, Clone, Serialize)]
pub struct DragCase {
    pub name: String,
    pub crew_lb: f64,
    /// Hull + crew, lb.
    pub displacement_lb: f64,
    /// Share in the objective (`objective.displacements` only).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub weight: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub draft_in: Option<f64>,
    pub resistance: Option<Resistance>,
}

/// Resistance of `hull` floating level with `weight_lb` at `speed_mps`, every term kept.
pub fn drag_case(
    hull: &Hull,
    name: &str,
    crew_lb: f64,
    weight_lb: f64,
    speed_mps: f64,
    roughness_um: f64,
    weight: Option<f64>,
) -> DragCase {
    let mut c = DragCase {
        name: name.into(),
        crew_lb,
        displacement_lb: weight_lb,
        weight,
        error: None,
        draft_in: None,
        resistance: None,
    };
    match level_draft_in(hull, weight_lb).and_then(|d| Ok((d, resistance(hull, d, speed_mps, roughness_um)?)))
    {
        Ok((d, r)) => {
            c.draft_in = Some(d);
            c.resistance = Some(r);
        }
        Err(e) => c.error = Some(format!("{e:#}")),
    }
    c
}

/// Effective power at `speeds_fps` of `hull` (a hull table up to the gunwale) floating level
/// with `weight_lb`. Errors (sinks, no resistance) are per curve, never zeros.
pub fn power_curve(
    hull: &Hull,
    name: &str,
    crew_lb: f64,
    weight_lb: f64,
    speeds_fps: &[f64],
    roughness_um: f64,
) -> PowerCurve {
    let run = || -> Result<(f64, Vec<f64>)> {
        let d = level_draft_in(hull, weight_lb)?;
        let r = speeds_fps
            .iter()
            .map(|v| resistance(hull, d, v * MPS_PER_FPS, roughness_um).map(|r| r.total_n))
            .collect::<Result<Vec<_>>>()?;
        Ok((d, r))
    };
    let mut c = PowerCurve {
        name: name.into(),
        crew_lb,
        displacement_lb: weight_lb,
        error: None,
        draft_in: None,
        drag_n: vec![],
        power_w: vec![],
    };
    match run() {
        Ok((d, r)) => {
            c.power_w = r.iter().zip(speeds_fps).map(|(r, v)| r * v * MPS_PER_FPS).collect();
            (c.draft_in, c.drag_n) = (Some(d), r);
        }
        Err(e) => c.error = Some(format!("{e:#}")),
    }
    c
}

/// Closed section of station `i`: up the starboard side, across the deck, down the port side.
fn section_polygon(grid: &Grid, i: usize) -> Vec<(f64, f64)> {
    let y = grid.row(i);
    let mut p: Vec<(f64, f64)> = y.iter().zip(&grid.z).map(|(&y, &z)| (y, z)).collect();
    p.extend(y.iter().zip(&grid.z).rev().map(|(&y, &z)| (-y, z)));
    p
}

/// Area and first moments (A, A ybar, A zbar) of polygon `p` below the line
/// -y s + z c = h (Sutherland-Hodgman on one half-plane, then the shoelace formulas).
fn clipped(p: &[(f64, f64)], s: f64, c: f64, h: f64) -> (f64, f64, f64) {
    let f = |q: (f64, f64)| -q.0 * s + q.1 * c - h;
    let mut out: Vec<(f64, f64)> = Vec::with_capacity(p.len() + 2);
    for k in 0..p.len() {
        let (a, b) = (p[k], p[(k + 1) % p.len()]);
        let (fa, fb) = (f(a), f(b));
        if fa <= 0.0 {
            out.push(a);
        }
        if (fa <= 0.0) != (fb <= 0.0) {
            let t = fa / (fa - fb);
            out.push((a.0 + t * (b.0 - a.0), a.1 + t * (b.1 - a.1)));
        }
    }
    let (mut a2, mut my, mut mz) = (0.0, 0.0, 0.0);
    for k in 0..out.len() {
        let (a, b) = (out[k], out[(k + 1) % out.len()]);
        let cr = a.0 * b.1 - b.0 * a.1;
        a2 += cr;
        my += (a.0 + b.0) * cr;
        mz += (a.1 + b.1) * cr;
    }
    // Orientation-free: A from |2A|, moments with the same sign as A.
    let sg = if a2 < 0.0 { -1.0 } else { 1.0 };
    (0.5 * a2.abs(), sg * my / 6.0, sg * mz / 6.0)
}

fn trapz(f: &[f64], x: &[f64]) -> f64 {
    x.windows(2).zip(f.windows(2)).map(|(x, f)| 0.5 * (f[0] + f[1]) * (x[1] - x[0])).sum()
}

/// Weight (lb) and KG (in) of the wall (`sec`, density `rho`) plus `crew_lb` at `crew_cg_in`.
pub fn weight_and_kg(grid: &Grid, sec: &Sections, rho: f64, crew_lb: f64, crew_cg_in: f64) -> (f64, f64) {
    let w: Vec<f64> = sec.area.iter().map(|a| a * rho / IN3_PER_FT3).collect();
    let wh = trapz(&w, &grid.x);
    let mz = trapz(&w.iter().zip(&sec.zbar).map(|(w, z)| w * z).collect::<Vec<_>>(), &grid.x);
    (wh + crew_lb, (mz + crew_lb * crew_cg_in) / (wh + crew_lb))
}

/// GZ at each of `angles_deg` for `weight_lb` with G at height `kg_in` on the centreline.
pub fn gz_curve(grid: &Grid, name: &str, weight_lb: f64, kg_in: f64, angles_deg: &[f64]) -> GzCurve {
    let mut c = GzCurve {
        name: name.into(),
        weight_lb,
        kg_in,
        error: None,
        gz_in: vec![],
        max_gz_in: None,
        max_gz_deg: None,
        vanishing_deg: None,
        gunwale_immersion_deg: None,
    };
    let polys: Vec<Vec<(f64, f64)>> = (0..grid.nx()).map(|i| section_polygon(grid, i)).collect();
    let need = weight_lb * IN3_PER_FT3 / WATER_PCF;
    let x = &grid.x;
    let solve = |deg: f64| -> Result<(f64, bool)> {
        let ph = deg.to_radians();
        let (s, co) = ph.sin_cos();
        let props = |h: f64| {
            let r: Vec<(f64, f64, f64)> = polys.iter().map(|p| clipped(p, s, co, h)).collect();
            let v = trapz(&r.iter().map(|q| q.0).collect::<Vec<_>>(), x);
            (v, r)
        };
        let f = |q: &(f64, f64)| -q.0 * s + q.1 * co;
        let all = polys.iter().flatten();
        let (mut lo, mut hi) =
            all.clone().fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), q| (a.min(f(q)), b.max(f(q))));
        if props(hi).0 < need {
            bail!("{weight_lb:.0} lb sinks the closed hull at {deg} deg");
        }
        for _ in 0..50 {
            let mid = 0.5 * (lo + hi);
            if props(mid).0 < need { lo = mid } else { hi = mid }
        }
        let h = 0.5 * (lo + hi);
        let (v, r) = props(h);
        let yb = trapz(&r.iter().map(|q| q.1).collect::<Vec<_>>(), x) / v;
        let zb = trapz(&r.iter().map(|q| q.2).collect::<Vec<_>>(), x) / v;
        // Starboard gunwale (the low side) of any station under the waterline.
        let nz = grid.nz();
        let wet = (0..grid.nx()).any(|i| {
            let yt = grid.y(i, nz - 1);
            yt > 0.0 && -yt * s + grid.depth * co < h
        });
        Ok(((yb * co + zb * s) - kg_in * s, wet))
    };
    let mut gz = Vec::with_capacity(angles_deg.len());
    for &a in angles_deg {
        match solve(a) {
            Ok((g, wet)) => {
                gz.push(g);
                if wet && c.gunwale_immersion_deg.is_none() {
                    c.gunwale_immersion_deg = Some(a);
                }
            }
            Err(e) => {
                c.error = Some(format!("{e:#}"));
                return c;
            }
        }
    }
    if let Some((k, &g)) = gz.iter().enumerate().max_by(|a, b| a.1.total_cmp(b.1)) {
        (c.max_gz_in, c.max_gz_deg) = (Some(g), Some(angles_deg[k]));
        c.vanishing_deg =
            gz.iter().enumerate().skip(k).find(|(_, g)| **g <= 1e-9).map(|(j, _)| angles_deg[j]);
    }
    c.gz_in = gz;
    c
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hull::linspace;
    use crate::section;

    fn box_grid(l: f64, b: f64, depth: f64) -> Grid {
        let x = linspace(0.0, l, 41);
        let z = linspace(0.0, depth, 49);
        let y = vec![b / 2.0; x.len() * z.len()];
        Grid { x, z, y, depth }
    }

    /// Closed box: the wall-sided formula GZ = sin(phi) (GM + BM tan^2(phi) / 2) holds until the
    /// deck edge goes under or the bilge comes out; 0 upright and upside down.
    #[test]
    fn box_matches_the_wall_sided_formula() {
        let (l, b, depth, t, kg) = (200.0, 30.0, 12.0, 4.0, 5.0);
        let g = box_grid(l, b, depth);
        let w = l * b * t * WATER_PCF / IN3_PER_FT3;
        let c = gz_curve(&g, "box", w, kg, &[0.0, 5.0, 10.0, 14.0, 90.0, 180.0]);
        assert!(c.error.is_none(), "{:?}", c.error);
        let bm = b * b / (12.0 * t);
        let gm = t / 2.0 + bm - kg;
        for (k, deg) in [(1, 5.0f64), (2, 10.0), (3, 14.0)] {
            let ph = deg.to_radians();
            let want = ph.sin() * (gm + 0.5 * bm * ph.tan().powi(2));
            assert!((c.gz_in[k] - want).abs() < 1e-3 * want, "{deg}: {} vs {want}", c.gz_in[k]);
        }
        assert!(c.gz_in[0].abs() < 1e-9 && c.gz_in[5].abs() < 1e-6);
        // At 90 deg the box lies on its side: the horizontal is the hull's z, so GZ = zB - KG
        // with B at mid-depth.
        assert!((c.gz_in[4] - (depth / 2.0 - kg)).abs() < 1e-6, "{}", c.gz_in[4]);
        // Deck edge goes under first at tan(phi) = (D - T) / (B/2) = 28 deg, past the last
        // wall-sided point; it is reported between 14 and 90 deg.
        assert_eq!(c.gunwale_immersion_deg, Some(90.0));
    }

    #[test]
    fn gz_curve_summary_and_errors() {
        let g = box_grid(200.0, 30.0, 12.0);
        let w = 200.0 * 30.0 * 4.0 * WATER_PCF / IN3_PER_FT3;
        let c = gz_curve(&g, "box", w, 5.0, &angles_deg());
        let (m, a) = (c.max_gz_in.unwrap(), c.max_gz_deg.unwrap());
        assert!(c.gz_in.iter().all(|&g| g <= m) && a > 0.0 && a < 180.0);
        // Upside down a closed box with KG below mid-depth is stable, so GZ turns negative
        // before 180 deg: the range of stability ends there.
        let v = c.vanishing_deg.unwrap();
        assert!(v > a && v < 180.0, "{v}");
        assert!(c.gz_in[(v / 2.0) as usize] <= 0.0);
        // Past bilge emergence (15 deg) the immersed section is a triangle under the deck corner:
        // area 1/2 D (D / tan) = B T gives tan = 0.6, 30.96 deg, so the first wet 2 deg step is 32.
        assert_eq!(c.gunwale_immersion_deg, Some(32.0));
        // Heavier than the closed hull can float: an error, never a curve of zeros.
        let sunk = gz_curve(&g, "lead", 1e6, 5.0, &angles_deg());
        assert!(sunk.error.is_some() && sunk.gz_in.is_empty());
    }

    #[test]
    fn kg_and_power() {
        let g = box_grid(200.0, 30.0, 12.0);
        let sec = section::sections(&g, 0.375);
        let (w, kg) = weight_and_kg(&g, &sec, 60.0, 300.0, 12.0);
        let wh = w - 300.0;
        assert!((kg - (wh * sec.zbar[0] + 300.0 * 12.0) / w).abs() < 1e-9);
        let hull = Hull {
            format: crate::hull::FORMAT.into(),
            units: "in".into(),
            name: "box".into(),
            source: String::new(),
            length: 200.0,
            draft_design: 12.0,
            x: g.x.clone(),
            z: g.z.clone(),
            half_breadth: (0..g.nx()).map(|i| g.row(i).to_vec()).collect(),
            stem: None,
        };
        let sp = speeds_fps();
        let p = power_curve(&hull, "case", 300.0, w, &sp, 0.0);
        assert!(p.error.is_none(), "{:?}", p.error);
        // P = R v, rising with speed.
        assert!((p.power_w[4] - p.drag_n[4] * sp[4] * MPS_PER_FPS).abs() < 1e-9);
        assert!(p.power_w.windows(2).all(|w| w[1] > w[0]));
        let sunk = power_curve(&hull, "lead", 0.0, 1e6, &sp, 0.0);
        assert!(sunk.error.is_some() && sunk.power_w.is_empty());
    }
}
