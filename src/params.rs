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
    pub stability: Stability,
    pub objective: Objective,
    /// Knee punching-shear model (see docs/PUNCHING.md).
    pub punching: Punching,
    /// Present = optimize the hull SHAPE too (hull.file must then be absent).
    /// See docs/SHAPE_SEARCH.md.
    #[serde(default)]
    pub shape_search: Option<ShapeSearch>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HullParams {
    /// Path to a `jaguar-hull/1` JSON file, relative to the canoe.toml.
    /// Required unless `[shape_search]` is given (then the shape is generated).
    #[serde(default)]
    pub file: Option<PathBuf>,
    /// Longitudinal stations used for the search. The verify pass uses 2n-1.
    pub stations: usize,
    /// Outward lean of the hull sides above the design waterline, degrees from
    /// vertical (0 = straight up, the default). Applies to every hull, fixed or
    /// generated. Typical canoe flare 5-15 deg.
    #[serde(default)]
    pub flare_deg: f64,
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
    /// "fit": strength from the straight-line fit through all lab rows, any
    /// density in the trusted range can be chosen. "tested_mixes": only the
    /// mixes actually tested can be chosen, each with its own measured
    /// strength (rows grouped by `mix` name, mean final strength and density).
    pub strength_model: StrengthModel,
    /// Lab data CSV (see `src/mix.rs` and `data/mix_tests.csv`), relative to the
    /// canoe.toml. Final split tensile strength vs density is fitted from it and
    /// every strength (flexural, compressive) is derived from that fit.
    pub mix_data: PathBuf,
    /// Modulus of rupture / split tensile strength. 1.0 is conservative
    /// (ACI relations give ~1.12).
    pub flex_over_split: f64,
    /// How far (pcf) the search may go beyond the densities actually tested.
    /// The strength fit is a straight line through few points; far outside the
    /// data it is a guess, so densities outside
    /// [min tested - this, max tested + this] are not searched.
    pub density_extrapolation_pcf: f64,
    pub factor_of_safety: f64,
    /// Multiplier on crew weight for paddling impact.
    pub dynamic_load_factor: f64,
    /// Knee contact patch length, in. NEVER zero: a point load is a
    /// singularity and D/C stops converging.
    pub knee_patch_in: f64,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum StrengthModel {
    Fit,
    TestedMixes,
}

/// How a kneeling paddler loads the hull bottom (punching shear).
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Punching {
    /// "aci_22_6": ACI 318-19 Table 22.6.5.2 two-way shear (reinforced slab,
    /// critical section d/2 from the patch, v_u = V_u / (b0 d)).
    /// "plain_14_5": ACI 318-19 Table 14.5.5.1 plain-concrete two-way shear
    /// (critical section h/2 from the patch, on b0 h).
    pub method: PunchingMethod,
    /// Knees carrying one paddler: 1 (all weight on one knee, conservative) or 2.
    pub knees: u32,
    /// Share of the paddler's weight on the heavier knee when knees = 2 (0.5-1).
    pub knee_split: f64,
    /// Centre-to-centre knee spacing, in (used to check whether the two critical
    /// perimeters overlap; if so the full load acts on the combined perimeter).
    pub knee_spacing_in: f64,
    /// Knee pad thickness, in; spreads the contact (ASCE limit 0.5 in).
    pub pad_in: f64,
    /// Effective depth to the mesh as a fraction of wall thickness (aci_22_6), e.g. 0.5.
    pub d_over_t: f64,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum PunchingMethod {
    #[serde(rename = "aci_22_6")]
    Aci22_6,
    #[serde(rename = "plain_14_5")]
    Plain14_5,
}

/// Transverse stability of the loaded canoe.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Stability {
    /// Height of the crew's centre of gravity above the keel, in (kneeling
    /// paddlers ~12 in; an assumption, not a measurement).
    pub crew_cg_in: f64,
    /// Minimum metacentric height GM_T, in. GM > 0 = upright is stable.
    /// Without this gate a shape search makes the hull as narrow as possible.
    pub gm_min_in: f64,
}

/// What "best" means.
#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ObjectiveKind {
    /// Lightest hull (wall weight) that passes every gate.
    Weight,
    /// Lowest total water resistance (wave + friction) at `speed_mps`, loaded,
    /// among designs that pass every gate. Needs `[shape_search]` to matter.
    /// Default when `objective.kind` is omitted.
    #[default]
    Drag,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Objective {
    #[serde(default)]
    pub kind: ObjectiveKind,
    /// Race speed for the drag objective, m/s (2-person sprint ~2.5).
    pub speed_mps: f64,
    /// Drag objective only: displacement weighting. Absent = drag of the
    /// heaviest load case at its loaded draft. Present = sum of weight x
    /// resistance at the level draft each (hull weight + crew_lb) floats at.
    /// Weights must sum to 1.
    #[serde(default)]
    pub displacements: Option<Vec<Displacement>>,
    /// Drag objective with `[shape_search]`: share of straight-line drag in
    /// the shape score; the rest goes to the turning proxy
    /// (docs/SHAPE_SEARCH.md). Default 1.0 = drag only.
    #[serde(default = "one")]
    pub straight_weight: f64,
}

fn one() -> f64 {
    1.0
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Displacement {
    /// Crew (plus gear) weight for this race condition, lb.
    pub crew_lb: f64,
    /// Share of the race distance raced at this displacement (0..1).
    pub weight: f64,
}

/// Bounds for hull-shape optimization. Every field is `[min, max]`; set
/// min == max to hold a parameter fixed. Meanings: `hullgen::ShapeParams`.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShapeSearch {
    pub length_in: [f64; 2],
    pub beam_in: [f64; 2],
    pub draft_in: [f64; 2],
    pub fore_wp: [f64; 2],
    pub section: [f64; 2],
    pub fore_rocker: [f64; 2],
    pub aft_wp: [f64; 2],
    pub aft_rocker: [f64; 2],
    pub fore_len_frac: [f64; 2],
    pub aft_len_frac: [f64; 2],
    /// Snap sampled draft / length / beam to multiples of this, in (molds,
    /// strongbacks). Absent = continuous. Search depth already steps by
    /// `search.depth_in`.
    #[serde(default)]
    pub draft_step_in: Option<f64>,
    #[serde(default)]
    pub length_step_in: Option<f64>,
    #[serde(default)]
    pub beam_step_in: Option<f64>,
    /// Allowed keel rise [min, max], in, at 5% of length from the bow and from
    /// the stern, relative to the lowest keel point. Shapes outside are
    /// rejected (with a reason), not remapped.
    #[serde(default)]
    pub rocker_in: Option<[f64; 2]>,
    /// Minimum design-waterline entry half-angle, deg (chord over the first
    /// 5% of length). Finer shapes are rejected as unbuildable.
    #[serde(default)]
    pub min_entry_angle_deg: Option<f64>,
    /// true = fore and aft ends identical: aft_wp, aft_rocker, aft_len_frac
    /// follow the fore values (their bounds are ignored).
    #[serde(default)]
    pub symmetric: bool,
    /// Initial space-filling samples (deterministic, seeded).
    pub samples: usize,
    /// Pattern-search refinement rounds around the best shapes.
    pub refine_rounds: usize,
    pub seed: u64,
}

impl ShapeSearch {
    /// (name, [min, max]) for every shape parameter, in `ShapeParams` order.
    pub fn bounds(&self) -> [(&'static str, [f64; 2]); 10] {
        [
            ("length_in", self.length_in),
            ("beam_in", self.beam_in),
            ("draft_in", self.draft_in),
            ("fore_wp", self.fore_wp),
            ("section", self.section),
            ("fore_rocker", self.fore_rocker),
            ("aft_wp", self.aft_wp),
            ("aft_rocker", self.aft_rocker),
            ("fore_len_frac", self.fore_len_frac),
            ("aft_len_frac", self.aft_len_frac),
        ]
    }
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
    /// Thinnest wall the team can actually build, in. Candidates thinner than
    /// this are never generated, and a winner AT this floor is not flagged as
    /// a grid-edge result (it is a real constraint, not a too-narrow range).
    pub thickness_min_buildable_in: f64,
}

impl Params {
    /// Load and validate. Relative `hull.file` is resolved against the toml's dir.
    pub fn load(path: &Path) -> Result<Self> {
        let text =
            std::fs::read_to_string(path).map_err(|e| anyhow::anyhow!("reading {}: {e}", path.display()))?;
        let base = path.parent().unwrap_or(Path::new(""));
        Self::parse(&text, base).map_err(|e| anyhow::anyhow!("{}: {e}", path.display()))
    }

    /// Parse and validate TOML text (e.g. submitted from the web page).
    /// A relative `hull.file` is resolved against `base_dir`.
    pub fn parse(text: &str, base_dir: &Path) -> Result<Self> {
        let mut p: Params = toml::from_str(text).map_err(|e| anyhow::anyhow!("parsing: {e}"))?;
        if let Some(f) = &p.hull.file
            && f.is_relative()
        {
            p.hull.file = Some(base_dir.join(f));
        }
        if p.material.mix_data.is_relative() {
            p.material.mix_data = base_dir.join(&p.material.mix_data);
        }
        p.validate()?;
        Ok(p)
    }

    pub fn validate(&self) -> Result<()> {
        if self.hull.stations < 11 {
            bail!("hull.stations must be >= 11");
        }
        if !(0.0..=45.0).contains(&self.hull.flare_deg) {
            bail!("hull.flare_deg must be in [0, 45], got {}", self.hull.flare_deg);
        }
        let pu = &self.punching;
        if !(pu.knees == 1 || pu.knees == 2)
            || !(0.5..=1.0).contains(&pu.knee_split)
            || pu.knee_spacing_in.is_nan()
            || pu.knee_spacing_in < 0.0
            || !(0.0..=0.5).contains(&pu.pad_in)
            || !(pu.d_over_t > 0.0 && pu.d_over_t <= 1.0)
        {
            bail!(
                "punching: need knees 1|2, knee_split 0.5..1, knee_spacing_in >= 0, pad_in 0..0.5, 0 < d_over_t <= 1"
            );
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
        if self.gates.thickness_min_buildable_in > self.search.thickness_in[1] {
            bail!("gates.thickness_min_buildable_in is above search.thickness_in max");
        }
        match (&self.hull.file, &self.shape_search) {
            (None, None) => bail!("give either hull.file or a [shape_search] section"),
            (Some(_), Some(_)) => bail!("hull.file and [shape_search] are exclusive: remove one"),
            _ => {}
        }
        if let Some(ss) = &self.shape_search {
            for (name, [lo, hi]) in ss.bounds() {
                if !(lo.is_finite() && hi.is_finite() && lo >= 0.0 && hi >= lo) {
                    bail!("shape_search.{name} must be [min >= 0, max >= min], got [{lo}, {hi}]");
                }
            }
            for (name, [lo, _]) in [
                ("length_in", ss.length_in),
                ("beam_in", ss.beam_in),
                ("draft_in", ss.draft_in),
                ("section", ss.section),
            ] {
                if lo <= 0.0 {
                    bail!("shape_search.{name} min must be > 0");
                }
            }
            if ss.samples < 1 {
                bail!("shape_search.samples must be >= 1");
            }
            for (name, step, [lo, hi]) in [
                ("draft_step_in", ss.draft_step_in, ss.draft_in),
                ("length_step_in", ss.length_step_in, ss.length_in),
                ("beam_step_in", ss.beam_step_in, ss.beam_in),
            ] {
                if let Some(st) = step {
                    if !(st.is_finite() && st > 0.0) {
                        bail!("shape_search.{name} must be > 0");
                    }
                    if (lo / st - 1e-9).ceil() > (hi / st + 1e-9).floor() {
                        bail!("shape_search.{name}: no multiple of {st} inside [{lo}, {hi}]");
                    }
                }
            }
            if let Some([lo, hi]) = ss.rocker_in
                && !(lo.is_finite() && hi.is_finite() && lo >= 0.0 && hi >= lo)
            {
                bail!("shape_search.rocker_in must be [min >= 0, max >= min]");
            }
            if let Some(a) = ss.min_entry_angle_deg
                && !(a.is_finite() && (0.0..90.0).contains(&a))
            {
                bail!("shape_search.min_entry_angle_deg must be in [0, 90)");
            }
        }
        if self.material.flex_over_split.is_nan()
            || self.material.flex_over_split <= 0.0
            || self.material.density_extrapolation_pcf.is_nan()
            || self.material.density_extrapolation_pcf < 0.0
        {
            bail!("material: need flex_over_split > 0 and density_extrapolation_pcf >= 0");
        }
        if self.objective.speed_mps.is_nan() || self.objective.speed_mps <= 0.0 {
            bail!("objective.speed_mps must be > 0");
        }
        if let Some(ds) = &self.objective.displacements {
            if ds.is_empty() {
                bail!("objective.displacements is empty: remove it or add entries");
            }
            for d in ds {
                if !(d.crew_lb.is_finite() && d.crew_lb >= 0.0 && d.weight.is_finite() && d.weight >= 0.0) {
                    bail!("objective.displacements: need crew_lb >= 0 and weight >= 0");
                }
            }
            let sum: f64 = ds.iter().map(|d| d.weight).sum();
            if (sum - 1.0).abs() > 1e-6 {
                bail!("objective.displacements weights must sum to 1, got {sum}");
            }
        }
        let sw = self.objective.straight_weight;
        if !(0.0..=1.0).contains(&sw) {
            bail!("objective.straight_weight must be in [0, 1]");
        }
        if sw < 1.0 && (self.objective.kind != ObjectiveKind::Drag || self.shape_search.is_none()) {
            bail!("objective.straight_weight < 1 needs kind = \"drag\" and a [shape_search]");
        }
        if self.stability.crew_cg_in.is_nan() || self.stability.crew_cg_in < 0.0 {
            bail!("stability.crew_cg_in must be >= 0");
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
    fn objective_kind_defaults_to_drag() {
        let text = std::fs::read_to_string("examples/canoe.toml").unwrap().replace("kind = \"drag\"", "");
        let p = Params::parse(&text, Path::new("examples")).unwrap();
        assert_eq!(p.objective.kind, ObjectiveKind::Drag);
    }

    #[test]
    fn unknown_key_is_an_error() {
        let text = std::fs::read_to_string("examples/canoe.toml").unwrap() + "\nbogus = 1\n";
        assert!(toml::from_str::<Params>(&text).is_err());
    }

    #[test]
    fn displacement_weights_must_sum_to_one() {
        let mut p = Params::load(Path::new("examples/shape_opt.toml")).unwrap();
        p.objective.kind = ObjectiveKind::Drag;
        let d = |c, w| Displacement { crew_lb: c, weight: w };
        p.objective.displacements = Some(vec![d(350.0, 0.675), d(525.0, 0.10), d(700.0, 0.225)]);
        p.validate().unwrap();
        p.objective.displacements = Some(vec![d(350.0, 0.75), d(700.0, 0.20)]);
        assert!(p.validate().is_err());
        p.objective.displacements = Some(vec![]);
        assert!(p.validate().is_err());
    }

    #[test]
    fn straight_weight_needs_drag_shape_search() {
        let mut p = Params::load(Path::new("examples/shape_opt.toml")).unwrap();
        p.objective.straight_weight = 0.8;
        assert!(p.validate().is_err(), "weight objective");
        p.objective.kind = ObjectiveKind::Drag;
        p.validate().unwrap();
        p.objective.straight_weight = 1.5;
        assert!(p.validate().is_err());
    }

    #[test]
    fn step_without_multiple_in_bounds_is_rejected() {
        let mut p = Params::load(Path::new("examples/shape_opt.toml")).unwrap();
        let ss = p.shape_search.as_mut().unwrap();
        ss.draft_in = [4.1, 4.9];
        ss.draft_step_in = Some(1.0);
        assert!(p.validate().is_err());
        p.shape_search.as_mut().unwrap().draft_step_in = Some(0.5);
        p.validate().unwrap();
    }

    #[test]
    fn range_is_inclusive() {
        assert_eq!(range([60.0, 64.0, 2.0]), vec![60.0, 62.0, 64.0]);
        assert_eq!(range([0.375, 0.75, 0.125]).len(), 4);
    }
}
