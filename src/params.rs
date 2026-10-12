//! `canoe.toml` -> [`Params`]. The ONLY place run parameters come from.
//!
//! Rules:
//! * Every field is required unless it has a `#[serde(default)]` with a
//!   documented reason. Unknown keys are an error (`deny_unknown_fields`),
//!   so a typo cannot silently fall back to a default.
//! * Validation happens here, once. Downstream code may assume `validate()`
//!   passed.

use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Params {
    pub hull: HullParams,
    /// One or more load cases. A design must pass ALL of them.
    #[serde(rename = "load_case")]
    pub load_cases: Vec<LoadCase>,
    pub material: Material,
    pub search: Search,
    pub gates: Gates,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HullParams {
    /// Path to a `jaguar-hull/1` JSON file, relative to the canoe.toml.
    pub file: PathBuf,
    /// Longitudinal stations used for the search. The verify pass uses 2n-1.
    pub stations: usize,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LoadCase {
    pub name: String,
    /// Paddler weights, lb, bow to stern.
    pub weights_lb: Vec<f64>,
    /// Knee positions, inches from the bow. Omitted = evenly spaced.
    #[serde(default)]
    pub seats_in: Option<Vec<f64>>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Material {
    /// Tensile strength fit: f_t [psi] = slope * density [pcf] + intercept.
    pub ft_slope_psi_per_pcf: f64,
    pub ft_intercept_psi: f64,
    pub factor_of_safety: f64,
    /// Multiplier on crew weight for paddling impact.
    pub dynamic_load_factor: f64,
    /// Knee contact patch length, in. NEVER zero: a point load is a
    /// singularity and D/C stops converging (see old/CLAUDE.md).
    pub knee_patch_in: f64,
}

/// Search axes. Each is an inclusive `[min, max, step]` range.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Search {
    pub thickness_in: [f64; 3],
    pub density_pcf: [f64; 3],
    pub depth_in: [f64; 3],
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Gates {
    /// D/C a candidate must stay under on the search grid.
    pub dc_search: f64,
    /// D/C the winner must stay under when re-run at 2n-1 stations.
    pub dc_verify: f64,
    /// Minimum loaded freeboard at the worst station, in.
    pub freeboard_min_in: f64,
}

impl Params {
    /// Load and validate. Relative `hull.file` is resolved against the toml's dir.
    pub fn load(path: &Path) -> Result<Self> {
        let text =
            std::fs::read_to_string(path).map_err(|e| anyhow::anyhow!("reading {}: {e}", path.display()))?;
        let mut p: Params =
            toml::from_str(&text).map_err(|e| anyhow::anyhow!("parsing {}: {e}", path.display()))?;
        if p.hull.file.is_relative()
            && let Some(dir) = path.parent()
        {
            p.hull.file = dir.join(&p.hull.file);
        }
        p.validate()?;
        Ok(p)
    }

    pub fn validate(&self) -> Result<()> {
        if self.hull.stations < 11 {
            bail!("hull.stations must be >= 11");
        }
        if self.load_cases.is_empty() {
            bail!("at least one [[load_case]] is required");
        }
        for lc in &self.load_cases {
            if lc.weights_lb.is_empty() {
                bail!("load_case '{}': weights_lb is empty", lc.name);
            }
            if let Some(s) = &lc.seats_in
                && s.len() != lc.weights_lb.len()
            {
                bail!("load_case '{}': seats_in and weights_lb differ in length", lc.name);
            }
        }
        if self.material.knee_patch_in <= 0.0 {
            bail!("material.knee_patch_in must be > 0 (a point load does not converge)");
        }
        if self.material.factor_of_safety < 1.0 {
            bail!("material.factor_of_safety must be >= 1");
        }
        for (name, r) in [
            ("thickness_in", self.search.thickness_in),
            ("density_pcf", self.search.density_pcf),
            ("depth_in", self.search.depth_in),
        ] {
            let [lo, hi, step] = r;
            if !(lo > 0.0 && hi >= lo && step > 0.0) {
                bail!("search.{name} must be [min > 0, max >= min, step > 0], got {r:?}");
            }
        }
        if !(self.gates.dc_search <= self.gates.dc_verify && self.gates.dc_verify <= 1.0) {
            bail!("gates: need dc_search <= dc_verify <= 1.0");
        }
        Ok(())
    }
}

/// Expand an inclusive `[min, max, step]` range. Robust to float drift.
pub fn range(r: [f64; 3]) -> Vec<f64> {
    let [lo, hi, step] = r;
    let n = ((hi - lo) / step + 1e-9).floor() as usize;
    (0..=n).map(|i| lo + i as f64 * step).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn example_toml_loads() {
        let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
        assert_eq!(p.load_cases.len(), 1);
    }

    #[test]
    fn unknown_key_is_an_error() {
        let text = std::fs::read_to_string("examples/canoe.toml").unwrap() + "\nbogus = 1\n";
        assert!(toml::from_str::<Params>(&text).is_err());
    }

    #[test]
    fn range_is_inclusive() {
        assert_eq!(range([60.0, 64.0, 2.0]), vec![60.0, 62.0, 64.0]);
        assert_eq!(range([0.375, 0.75, 0.125]).len(), 4);
    }
}
