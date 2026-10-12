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
    pub draft_in: f64,
    pub trim_in: f64,
    pub shear: Vec<f64>,
    pub moment: Vec<f64>,
}

fn trapz(f: &[f64], x: &[f64]) -> f64 {
    (1..x.len()).map(|i| 0.5 * (f[i] + f[i - 1]) * (x[i] - x[i - 1])).sum()
}

fn brent(f: impl Fn(f64) -> Result<f64>, mut a: f64, mut b: f64, tol: f64) -> Result<f64> {
    let (mut fa, mut fb) = (f(a)?, f(b)?);
    if fa * fb > 0.0 {
        bail!("root not bracketed");
    }
    for _ in 0..200 {
        let m = 0.5 * (a + b);
        let s = if fb != fa { b - fb * (b - a) / (fb - fa) } else { m };
        let x = if (s - a) * (s - b) < 0.0 { s } else { m };
        let fx = f(x)?;
        if fx == 0.0 || (b - a).abs() < tol {
            return Ok(x);
        }
        // keep bracket; fall back to bisection to guarantee shrink
        let fm = f(m)?;
        if fa * fx < 0.0 {
            b = x;
            fb = fx;
        } else {
            a = x;
            fa = fx;
        }
        if fa * fm < 0.0 && m < b && m > a {
            b = m;
            fb = fm;
        } else if m > a && m < b {
            a = m;
            fa = fm;
        }
    }
    Ok(0.5 * (a + b))
}

pub fn solve(grid: &Grid, sec: &Sections, rho: f64, crew: &[CrewLoad], patch: f64) -> Result<Loads> {
    let (x, z, n, nz) = (&grid.x, &grid.z, grid.nx(), grid.nz());
    let l = x[n - 1];
    let xc = l / 2.0;
    let w_hull: Vec<f64> = sec.area.iter().map(|a| a * rho / IN3_PER_FT3).collect();
    let mut cum = vec![0.0; n * nz];
    for i in 0..n {
        let y = grid.row(i);
        for k in 1..nz {
            cum[i * nz + k] = cum[i * nz + k - 1] + (y[k] + y[k - 1]) * (z[k] - z[k - 1]);
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
    if mres(-thmax)? > 0.0 || mres(thmax)? < 0.0 {
        bail!("trim balance not bracketed");
    }
    let th = brent(mres, -thmax, thmax, 1e-9)?;
    let d0 = draft_for(th)?;
    // Distributed loads (buoyancy - self weight) integrate by trapezoid; crew
    // patches are added in closed form so they are exact at any station spacing.
    let q: Vec<f64> = buoy(d0, th).iter().zip(&w_hull).map(|(b, w)| b - w).collect();
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
    Ok(Loads { hull_weight_lb: wh, draft_in: d0, trim_in: th, shear: v, moment: m })
}

/// Shear and moment at `x` from a unit load spread uniformly over
/// `[xc - w/2, xc + w/2]`, integrated from the bow: returns (V, M) per lb.
/// Closed form, so the crew load is exact at any station spacing or seat.
/// (The prototype sampled the patch at nodes, which caught 2 or 3 nodes
/// depending on seat position, losing up to a third of a paddler and breaking
/// free-free closure for 3+ crew.)
fn patch_vm(x: f64, xc: f64, w: f64) -> (f64, f64) {
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
                solve(&g, &s, 66.0, &crew, 3.0)
                    .unwrap_or_else(|e| panic!("{n} crew at {stations} stations: {e}"));
            }
        }
    }
}
