//! Water resistance of a floating hull. Owner: T12 (physics agent).
//!
//! Used only for the `drag` objective (pick the fastest hull that is strong
//! enough). Pure function, no I/O. Units at this boundary: hull geometry in
//! inches, speed in m/s, result in newtons (convert inside, see `units`).
//!
//! Model (port intent from old/wave_resistance.py; do not port its numba code):
//! * wave resistance: Michell thin-ship integral over the SUBMERGED part of the
//!   hull at the loaded draft (half-breadths clipped at `draft_in`), fresh water
//!   998 kg/m^3, g = 9.80665, ntheta = 128 (or fewer if a test shows <1%
//!   change), theta excluding pi/2.
//! * friction: ITTC-1957, Cf = 0.075 / (log10 Re - 2)^2, Re = V L / nu,
//!   nu = 1.139e-6 m^2/s, times 0.5 rho V^2 S with S the wetted surface
//!   (girth below the draft integrated along the length).
//!   No form factor (k = 0); say so in the result doc.
//!
//! Errors (Err, never 0.0): non-finite inputs, speed <= 0, draft <= 0, or a
//! hull with no submerged volume at `draft_in`.

use crate::hull::Hull;
use anyhow::Result;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Resistance {
    pub wave_n: f64,
    pub friction_n: f64,
    pub total_n: f64,
    /// Wetted surface used for friction, m^2.
    pub wetted_m2: f64,
}

/// Resistance of `hull` floating level at `draft_in` (inches above keel) at
/// `speed_mps`.
pub fn resistance(hull: &Hull, draft_in: f64, speed_mps: f64) -> Result<Resistance> {
    let _ = (hull, draft_in, speed_mps);
    anyhow::bail!("resistance not implemented yet (T12)")
}
