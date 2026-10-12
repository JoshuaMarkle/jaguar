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
    let mut q: Vec<f64> = buoy(d0, th).iter().zip(&w_hull).map(|(b, w)| b - w).collect();
    for c in crew {
        for i in 0..n {
            if (x[i] - c.x_in).abs() <= patch / 2.0 {
                q[i] -= c.lb / patch;
            }
        }
    }
    let mut v = vec![0.0; n];
    let mut m = vec![0.0; n];
    for i in 1..n {
        let dx = x[i] - x[i - 1];
        v[i] = v[i - 1] + 0.5 * (q[i] + q[i - 1]) * dx;
        m[i] = m[i - 1] + 0.5 * (v[i] + v[i - 1]) * dx;
    }
    if v[n - 1].abs() > 1e-3 * wtot || m[n - 1].abs() > 1e-3 * wtot * l {
        bail!("equilibrium broken: V_end={:.2} M_end={:.1}", v[n - 1], m[n - 1]);
    }
    Ok(Loads { hull_weight_lb: wh, draft_in: d0, trim_in: th, shear: v, moment: m })
}
