use crate::hull::Grid;
use crate::section::Sections;
use crate::units::{IN3_PER_FT3, WATER_PCF};
use anyhow::{Result, bail};

#[derive(Debug, Clone, Copy)]
pub struct CrewLoad {
    pub x_in: f64,
    pub lb: f64,
}

#[derive(Debug, Clone)]
pub struct Loads {
    pub hull_weight_lb: f64,
    /// Total weight floated (hull + crew, as factored), lb = displacement.
    pub weight_lb: f64,
    pub draft_in: f64,
    pub trim_in: f64,
    pub shear: Vec<f64>,
    pub moment: Vec<f64>,
    /// Loaded transverse metacentric height GM_T = KB + BM - KG, in, at the
    /// equilibrium waterline (with trim). KG combines hull wall (section zbar)
    /// and crew (`crew_cg_in`).
    pub gm_in: f64,
    /// Parts of GM_T: centre of buoyancy, metacentric radius I_wp / V, and
    /// centre of gravity above the keel, in.
    pub kb_in: f64,
    pub bm_in: f64,
    pub kg_in: f64,
    /// Per station, lb per inch of length: buoyancy (empty when not floating) and self weight
    /// (as factored). Net distributed load q = buoyancy - self weight; V, M integrate it.
    pub buoyancy: Vec<f64>,
    pub self_weight: Vec<f64>,
}

fn trapz(f: &[f64], x: &[f64]) -> f64 {
    (1..x.len()).map(|i| 0.5 * (f[i] + f[i - 1]) * (x[i] - x[i - 1])).sum()
}

/// Root of `f` in [a, b] to `tol` (Brent 1973: inverse quadratic /
/// secant steps, bisection when they misbehave). Err if not bracketed.
fn brent(f: impl Fn(f64) -> Result<f64>, a: f64, b: f64, tol: f64) -> Result<f64> {
    let (mut a, mut b) = (a, b);
    let (mut fa, mut fb) = (f(a)?, f(b)?);
    if fa == 0.0 {
        return Ok(a);
    }
    if fa * fb > 0.0 {
        bail!("root not bracketed");
    }
    let (mut c, mut fc) = (b, fb);
    let (mut d, mut e) = (b - a, b - a);
    for _ in 0..200 {
        if (fb > 0.0) == (fc > 0.0) && fc != 0.0 {
            (c, fc) = (a, fa);
            d = b - a;
            e = d;
        }
        if fc.abs() < fb.abs() {
            (a, fa) = (b, fb);
            (b, fb) = (c, fc);
            (c, fc) = (a, fa);
        }
        let tol1 = 2.0 * f64::EPSILON * b.abs() + 0.5 * tol;
        let xm = 0.5 * (c - b);
        if xm.abs() <= tol1 || fb == 0.0 {
            return Ok(b);
        }
        if e.abs() >= tol1 && fa.abs() > fb.abs() {
            let s = fb / fa;
            let (mut p, mut q) = if a == c {
                (2.0 * xm * s, 1.0 - s)
            } else {
                let (qq, r) = (fa / fc, fb / fc);
                (s * (2.0 * xm * qq * (qq - r) - (b - a) * (r - 1.0)), (qq - 1.0) * (r - 1.0) * (s - 1.0))
            };
            if p > 0.0 {
                q = -q;
            }
            p = p.abs();
            if 2.0 * p < (3.0 * xm * q - (tol1 * q).abs()).min((e * q).abs()) {
                e = d;
                d = p / q;
            } else {
                d = xm;
                e = d;
            }
        } else {
            d = xm;
            e = d;
        }
        (a, fa) = (b, fb);
        b += if d.abs() > tol1 { d } else { tol1.copysign(xm) };
        fb = f(b)?;
    }
    Ok(b)
}

pub fn solve(
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    crew: &[CrewLoad],
    patch: f64,
    crew_cg_in: f64,
) -> Result<Loads> {
    let w_hull: Vec<f64> = sec.area.iter().map(|a| a * rho / IN3_PER_FT3).collect();
    solve_weights(grid, &w_hull, &sec.zbar, crew, patch, crew_cg_in)
}

/// [`solve`] for a wall given as self weight per station `w_hull` (lb/in) and the height of
/// its mass centroid `zg` (a two-mix zoned wall, `section::zoned_sections`).
pub fn solve_weights(
    grid: &Grid,
    w_hull: &[f64],
    zg: &[f64],
    crew: &[CrewLoad],
    patch: f64,
    crew_cg_in: f64,
) -> Result<Loads> {
    let (x, z, n, nz) = (&grid.x, &grid.z, grid.nx(), grid.nz());
    let l = x[n - 1];
    let xc = l / 2.0;
    let w_hull = w_hull.to_vec();
    let mut cum = vec![0.0; n * nz];
    // cumulative first moment of submerged section area about the keel
    let mut cumz = vec![0.0; n * nz];
    for i in 0..n {
        let y = grid.row(i);
        for k in 1..nz {
            let dz = z[k] - z[k - 1];
            cum[i * nz + k] = cum[i * nz + k - 1] + (y[k] + y[k - 1]) * dz;
            // exact for a linear y over the strip
            cumz[i * nz + k] = cumz[i * nz + k - 1]
                + dz * (y[k - 1] * (2.0 * z[k - 1] + z[k]) + y[k] * (z[k - 1] + 2.0 * z[k])) / 3.0;
        }
    }
    let wh = trapz(&w_hull, x);
    let wtot = wh + crew.iter().map(|c| c.lb).sum::<f64>();
    let mw = trapz(&w_hull.iter().zip(x).map(|(w, xi)| w * (xi - xc)).collect::<Vec<_>>(), x)
        + crew.iter().map(|c| c.lb * (c.x_in - xc)).sum::<f64>();
    let zt = z[nz - 1];
    let buoy = |d0: f64, th: f64| -> Vec<f64> {
        (0..n)
            .map(|i| {
                let d = (d0 + th * (x[i] - xc) / l).clamp(0.0, zt);
                let k = z.partition_point(|&v| v <= d).clamp(1, nz - 1);
                let t = (d - z[k - 1]) / (z[k] - z[k - 1]);
                let s = cum[i * nz + k - 1] * (1.0 - t) + cum[i * nz + k] * t;
                s * WATER_PCF / IN3_PER_FT3
            })
            .collect()
    };
    let draft_for = |th: f64| -> Result<f64> {
        let f = |d0: f64| Ok((trapz(&buoy(d0, th), x) - wtot) / wtot);
        if f(zt)? < 0.0 {
            bail!("hull sinks: total weight exceeds buoyancy at full depth");
        }
        brent(f, 0.0, zt, 1e-12)
    };
    let mres = |th: f64| -> Result<f64> {
        let d = draft_for(th)?;
        let b = buoy(d, th);
        Ok((trapz(&b.iter().zip(x).map(|(b, xi)| b * (xi - xc)).collect::<Vec<_>>(), x) - mw) / (wtot * l))
    };
    let thmax = 0.5 * zt;
    // Fast path: Newton on (draft, trim) with the Jacobian of the same
    // piecewise-linear buoyancy. Accepted only if it converges inside the
    // bracket box; otherwise the nested bracketing below decides (and gives
    // the "sinks" / "not bracketed" errors).
    let tw: Vec<f64> = (0..n).map(|i| 0.5 * (x[(i + 1).min(n - 1)] - x[i.saturating_sub(1)])).collect();
    let newton = || -> Option<(f64, f64)> {
        let c = WATER_PCF / IN3_PER_FT3;
        let (mut d0, mut th) = (0.5 * zt, 0.0);
        for _ in 0..50 {
            let [mut f1, mut f2, mut j11, mut j12, mut j21, mut j22] = [0.0; 6];
            for i in 0..n {
                let r = x[i] - xc;
                let d = d0 + th * r / l;
                let dc = d.clamp(0.0, zt);
                let k = z.partition_point(|&v| v <= dc).clamp(1, nz - 1);
                let dz = z[k] - z[k - 1];
                let t = (dc - z[k - 1]) / dz;
                let s = cum[i * nz + k - 1] * (1.0 - t) + cum[i * nz + k] * t;
                let g = if d > 0.0 && d < zt { (cum[i * nz + k] - cum[i * nz + k - 1]) / dz } else { 0.0 };
                let (b, gb) = (tw[i] * s * c, tw[i] * g * c);
                f1 += b;
                f2 += b * r;
                j11 += gb;
                j12 += gb * r / l;
                j21 += gb * r;
                j22 += gb * r * r / l;
            }
            let (r1, r2) = (f1 - wtot, f2 - mw);
            let det = j11 * j22 - j12 * j21;
            if !(det.is_finite() && det != 0.0) {
                return None;
            }
            let dd = (r1 * j22 - r2 * j12) / det;
            let dt = (j11 * r2 - j21 * r1) / det;
            // Halve a step that would leave the box (draft in (0, zt), |trim| < thmax).
            let mut lam = 1.0;
            while !(d0 - lam * dd > 0.0 && d0 - lam * dd < zt && (th - lam * dt).abs() < thmax) {
                lam *= 0.5;
                if lam < 1e-6 {
                    return None;
                }
            }
            d0 -= lam * dd;
            th -= lam * dt;
            if dd.abs() < 1e-12 && dt.abs() < 1e-10 {
                return Some((d0, th));
            }
        }
        None
    };
    let (d0, th) = match newton() {
        Some(r) => r,
        None => {
            if mres(-thmax)? > 0.0 || mres(thmax)? < 0.0 {
                bail!("trim balance not bracketed");
            }
            let th = brent(mres, -thmax, thmax, 1e-9)?;
            (draft_for(th)?, th)
        }
    };
    // Distributed loads (buoyancy - self weight) integrate by trapezoid; crew
    // patches are added in closed form so they are exact at any station spacing.
    let buoyancy = buoy(d0, th);
    let q: Vec<f64> = buoyancy.iter().zip(&w_hull).map(|(b, w)| b - w).collect();
    let mut v = vec![0.0; n];
    let mut m = vec![0.0; n];
    for i in 1..n {
        let dx = x[i] - x[i - 1];
        v[i] = v[i - 1] + 0.5 * (q[i] + q[i - 1]) * dx;
        m[i] = m[i - 1] + 0.5 * (v[i] + v[i - 1]) * dx;
    }
    for c in crew {
        for i in 0..n {
            let (fv, fm) = patch_vm(x[i], c.x_in, patch);
            v[i] -= c.lb * fv;
            m[i] -= c.lb * fm;
        }
    }
    if v[n - 1].abs() > 1e-3 * wtot || m[n - 1].abs() > 1e-3 * wtot * l {
        bail!("equilibrium broken: V_end={:.2} M_end={:.1}", v[n - 1], m[n - 1]);
    }
    let (kb_in, bm_in, kg_in) =
        metacentric_height(grid, &cum, &cumz, d0, th, zg, &w_hull, wh, crew, crew_cg_in)?;
    let gm_in = kb_in + bm_in - kg_in;
    Ok(Loads {
        hull_weight_lb: wh,
        weight_lb: wtot,
        draft_in: d0,
        trim_in: th,
        shear: v,
        moment: m,
        gm_in,
        kb_in,
        bm_in,
        kg_in,
        buoyancy,
        self_weight: w_hull,
    })
}

/// (KB, BM, KG) of GM_T = KB + BM - KG at the waterline `d0 + th (x - L/2)/L`.
#[allow(clippy::too_many_arguments)]
fn metacentric_height(
    grid: &Grid,
    cum: &[f64],
    cumz: &[f64],
    d0: f64,
    th: f64,
    zg: &[f64],
    w_hull: &[f64],
    wh: f64,
    crew: &[CrewLoad],
    crew_cg_in: f64,
) -> Result<(f64, f64, f64)> {
    let (x, z, n, nz) = (&grid.x, &grid.z, grid.nx(), grid.nz());
    let l = x[n - 1];
    let zt = z[nz - 1];
    let (mut a, mut az, mut iy) = (vec![0.0; n], vec![0.0; n], vec![0.0; n]);
    for i in 0..n {
        let d = (d0 + th * (x[i] - l / 2.0) / l).clamp(0.0, zt);
        let k = z.partition_point(|&v| v <= d).clamp(1, nz - 1);
        let (z0, z1) = (z[k - 1], z[k]);
        let t = (d - z0) / (z1 - z0);
        let (y0, y1) = (grid.y(i, k - 1), grid.y(i, k));
        let yd = y0 * (1.0 - t) + y1 * t;
        let dz = d - z0;
        a[i] = cum[i * nz + k - 1] + (y0 + yd) * dz;
        az[i] = cumz[i * nz + k - 1] + dz * (y0 * (2.0 * z0 + d) + yd * (z0 + 2.0 * d)) / 3.0;
        iy[i] = 2.0 / 3.0 * yd.powi(3);
    }
    let vol = trapz(&a, x);
    if vol.is_nan() || vol <= 0.0 {
        bail!("no displaced volume for GM");
    }
    let kb = trapz(&az, x) / vol;
    let bm = trapz(&iy, x) / vol;
    let mz = trapz(&w_hull.iter().zip(zg).map(|(w, zb)| w * zb).collect::<Vec<_>>(), x);
    let crew_lb: f64 = crew.iter().map(|c| c.lb).sum();
    let kg = (mz + crew_lb * crew_cg_in) / (wh + crew_lb);
    let gm = kb + bm - kg;
    if !gm.is_finite() {
        bail!("non-finite GM (KB {kb}, BM {bm}, KG {kg})");
    }
    Ok((kb, bm, kg))
}

/// Shear and moment at `x` from a unit load spread uniformly over
/// `[xc - w/2, xc + w/2]`, integrated from the bow: returns (V, M) per lb.
/// Closed form, so the crew load is exact at any station spacing or seat.
/// (Sampling the patch at nodes, as the golden reference script does, catches 2 or 3 nodes
/// depending on seat position, losing up to a third of a paddler and breaking
/// free-free closure for 3+ crew.)
pub fn patch_vm(x: f64, xc: f64, w: f64) -> (f64, f64) {
    let a = xc - w / 2.0;
    if x <= a {
        (0.0, 0.0)
    } else if x >= a + w {
        (1.0, x - xc)
    } else {
        let u = x - a;
        (u / w, u * u / (2.0 * w))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hull::{Grid, Hull};
    use crate::section;
    use std::path::Path;

    #[test]
    fn patch_vm_is_continuous_and_exact() {
        let (xc, w) = (50.0, 3.0);
        assert_eq!(patch_vm(48.0, xc, w), (0.0, 0.0));
        let (v, m) = patch_vm(60.0, xc, w);
        assert!((v - 1.0).abs() < 1e-12 && (m - 10.0).abs() < 1e-12);
        let (v, m) = patch_vm(51.5, xc, w);
        assert!((v - 1.0).abs() < 1e-12 && (m - 1.5).abs() < 1e-12, "{v} {m}");
    }

    /// Regression: 3 and 4 paddlers evenly spaced used to fail with
    /// "equilibrium broken" at every candidate.
    #[test]
    fn any_crew_size_closes() {
        let hull = Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap();
        for stations in [81, 161, 321] {
            let g = Grid::build(&hull, 14.0, stations);
            let s = section::sections(&g, 0.375);
            for n in 1..=5 {
                let crew: Vec<CrewLoad> = (0..n)
                    .map(|i| CrewLoad {
                        x_in: hull.length * (i + 1) as f64 / (n + 1) as f64,
                        lb: 160.0 * 1.2,
                    })
                    .collect();
                solve(&g, &s, 66.0, &crew, 3.0, 12.0)
                    .unwrap_or_else(|e| panic!("{n} crew at {stations} stations: {e}"));
            }
        }
    }

    /// Rectangular box barge: KB = T/2, BM = B^2/(12 T), KG from crew + walls.
    #[test]
    fn box_barge_gm_matches_hand_formula() {
        let (l, b, depth, n) = (200.0, 30.0, 12.0, 41);
        let x = crate::hull::linspace(0.0, l, n);
        let z = crate::hull::linspace(0.0, depth, 25);
        let y = vec![b / 2.0; n * z.len()];
        let g = Grid { x, z, y, depth };
        let s = section::sections(&g, 0.5);
        let crew = [CrewLoad { x_in: l / 2.0, lb: 400.0 }];
        let ld = solve(&g, &s, 60.0, &crew, 3.0, 12.0).unwrap();
        let wh = ld.hull_weight_lb;
        let t = (wh + 400.0) / (l * b * WATER_PCF / IN3_PER_FT3);
        assert!((ld.draft_in - t).abs() < 1e-6, "{} {t}", ld.draft_in);
        assert!(ld.trim_in.abs() < 1e-6);
        // wall zbar is uniform along a prismatic hull
        let kg = (wh * s.zbar[0] + 400.0 * 12.0) / (wh + 400.0);
        let gm = t / 2.0 + b * b / (12.0 * t) - kg;
        assert!((ld.gm_in - gm).abs() < 1e-6, "{} {gm}", ld.gm_in);
    }
}
