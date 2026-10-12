use crate::hull::Grid;

#[derive(Debug, Clone)]
pub struct Sections {
    pub area: Vec<f64>,
    pub zbar: Vec<f64>,
    pub inertia: Vec<f64>,
}

pub fn sections(grid: &Grid, t: f64) -> Sections {
    let n = grid.nx();
    let z = &grid.z;
    let (mut area, mut zbar, mut inertia) = (vec![0.0; n], vec![0.0; n], vec![0.0; n]);
    for i in 0..n {
        let y = grid.row(i);
        let a0 = 2.0 * y[0] * t;
        let (mut a, mut az) = (a0, 0.0);
        for k in 0..z.len() - 1 {
            let (dz, dy) = (z[k + 1] - z[k], y[k + 1] - y[k]);
            let ai = 2.0 * t * dy.hypot(dz);
            a += ai;
            az += ai * 0.5 * (z[k] + z[k + 1]);
        }
        let zb = if a > 0.0 { az / a.max(1e-12) } else { 0.0 };
        let mut ii = a0 * zb * zb;
        for k in 0..z.len() - 1 {
            let (dz, dy) = (z[k + 1] - z[k], y[k + 1] - y[k]);
            let ai = 2.0 * t * dy.hypot(dz);
            let zm = 0.5 * (z[k] + z[k + 1]);
            ii += ai * ((zm - zb).powi(2) + dz * dz / 12.0);
        }
        area[i] = a;
        zbar[i] = zb;
        inertia[i] = ii;
    }
    Sections { area, zbar, inertia }
}
