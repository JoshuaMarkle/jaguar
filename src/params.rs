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
    /// Present = gate on swamped flotation with bow/stern air pockets (src/flotation.rs).
    #[serde(default)]
    pub flotation: Option<FlotationParams>,
    /// Present = add the transverse wall stress of a steady turn to the biaxial
    /// check (`check::hoop_psi`). Absent = no turning load.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turning: Option<Turning>,
    /// Seat placement for load cases without `seats_in` and the seat
    /// tolerance every case is checked over (src/seats.rs). Absent = defaults.
    #[serde(default)]
    pub seats: Seats,
    /// Present = race courses and events for `jaguar race` (src/race.rs).
    /// Never read by the wall or shape search.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub race: Option<RaceParams>,
    /// Present = also search two-mix walls (src/optimize.rs `zoned_search`): a dense mix in
    /// a bottom and a top band over the middle (an I-beam), the light mix everywhere else.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub zones: Option<Zones>,
}

/// Two-mix walls (docs/SPEC.md): the light mix is `search.density_pcf`; the dense mix is placed
/// where the checks need it, as side-view rectangles with edges on multiples of `increment_in`.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Zones {
    /// Dense mix densities [min, max, step], pcf (fit mode: clipped to the mix data like
    /// search.density_pcf; tested_mixes: the tested mixes inside [min, max]). Only ones heavier
    /// than the light mix are paired with it.
    pub dense_pcf: [f64; 3],
    /// Rectangle edges are multiples of this, in, so they can be measured on the mold.
    /// Default 1/8 in.
    #[serde(default = "default_increment")]
    pub increment_in: f64,
    /// Most (wall, light, dense) combinations placed, lightest first.
    #[serde(default = "default_max_checks")]
    pub max_checks: usize,
}

fn default_increment() -> f64 {
    0.125
}

fn default_max_checks() -> usize {
    400
}

/// Race simulator inputs (src/race.rs). Units: m, m/s, N, W (the rules give
/// courses in metres), crew weights lb.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RaceParams {
    /// Sustained power one paddler puts into the paddle, W (calibrate with
    /// `jaguar race --measured`).
    pub paddler_power_w: f64,
    /// Share of paddle power that becomes thrust (0..1].
    pub propulsive_efficiency: f64,
    /// Thrust one paddler can pull at low speed (start, slow turns), N.
    pub max_thrust_n: f64,
    /// Closest the canoe centreline may pass a buoy, m (half beam + margin).
    pub buoy_clearance_m: f64,
    /// Lane width, m; the centreline stays within +/- half of it of y = 0.
    pub lane_width_m: f64,
    /// Tightest turn the crew can steer, m (centreline radius). The turn drag
    /// already slows tight turns; this is only a floor.
    pub min_turn_radius_m: f64,
    #[serde(rename = "course")]
    pub courses: Vec<Course>,
    #[serde(rename = "event")]
    pub events: Vec<RaceEvent>,
}

/// A course: standing start on the line x = 0 heading +x (anywhere within the
/// lane), round each mark in order, finish crossing the line x = `finish_x_m`.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Course {
    pub name: String,
    pub finish_x_m: f64,
    pub marks: Vec<Mark>,
    /// This course's lane width, m, for the path (e.g. large for an open-water
    /// endurance loop). Absent = `race.lane_width_m`. The start is always within
    /// the race lane.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lane_width_m: Option<f64>,
}

impl Course {
    /// Half the lane width the path must stay within, m.
    pub fn lane_half_m(&self, rp: &RaceParams) -> f64 {
        0.5 * self.lane_width_m.unwrap_or(rp.lane_width_m)
    }
}

/// A buoy at (x, y) m (y = 0 is the lane centre, +y to port when heading +x),
/// kept on the canoe's `keep` side while rounding it.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Mark {
    pub x_m: f64,
    pub y_m: f64,
    pub keep: Side,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    /// Buoy on the canoe's left: the canoe turns left (counter-clockwise) around it.
    Port,
    /// Buoy on the canoe's right: the canoe turns right (clockwise) around it.
    Starboard,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RaceEvent {
    pub name: String,
    /// Name of a `[[race.course]]`.
    pub course: String,
    /// Paddler weights, lb (one per paddler).
    pub weights_lb: Vec<f64>,
    /// Share of the score this race carries (event weights sum to 1).
    pub weight: f64,
    /// Overrides `race.paddler_power_w` for this race (e.g. lower for the endurance race).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub paddler_power_w: Option<f64>,
}

impl RaceParams {
    pub fn validate(&self) -> Result<()> {
        let pos = |v: f64| v.is_finite() && v > 0.0;
        if !(pos(self.paddler_power_w)
            && pos(self.max_thrust_n)
            && pos(self.buoy_clearance_m)
            && pos(self.lane_width_m)
            && self.propulsive_efficiency > 0.0
            && self.propulsive_efficiency <= 1.0
            && pos(self.min_turn_radius_m))
        {
            bail!(
                "race: need paddler_power_w, max_thrust_n, buoy_clearance_m, lane_width_m, \
                 min_turn_radius_m > 0 and propulsive_efficiency in (0, 1]"
            );
        }
        if self.courses.is_empty() || self.events.is_empty() {
            bail!("race: need at least one [[race.course]] and one [[race.event]]");
        }
        for (i, c) in self.courses.iter().enumerate() {
            let half = c.lane_half_m(self);
            if c.lane_width_m.is_some_and(|w| !(w.is_finite() && w >= self.lane_width_m)) {
                bail!("race course '{}': lane_width_m must be >= race.lane_width_m", c.name);
            }
            if self.courses[..i].iter().any(|o| o.name == c.name) {
                bail!("race: course name '{}' used twice", c.name);
            }
            if c.marks.is_empty() || !c.finish_x_m.is_finite() {
                bail!("race course '{}': need at least one mark and a finite finish_x_m", c.name);
            }
            for m in &c.marks {
                if !(m.x_m.is_finite() && m.y_m.is_finite() && m.y_m.abs() < half) {
                    bail!("race course '{}': mark ({}, {}) m is outside the lane", c.name, m.x_m, m.y_m);
                }
            }
        }
        for e in &self.events {
            if !self.courses.iter().any(|c| c.name == e.course) {
                bail!("race event '{}': no course named '{}'", e.name, e.course);
            }
            if e.weights_lb.is_empty() || !e.weights_lb.iter().all(|w| pos(*w)) {
                bail!("race event '{}': need one weight > 0 per paddler", e.name);
            }
            if !(e.weight.is_finite() && e.weight >= 0.0) || e.paddler_power_w.is_some_and(|p| !pos(p)) {
                bail!("race event '{}': need weight >= 0 and paddler_power_w > 0", e.name);
            }
        }
        let sum: f64 = self.events.iter().map(|e| e.weight).sum();
        if (sum - 1.0).abs() > 1e-6 {
            bail!("race event weights must sum to 1, got {sum}");
        }
        Ok(())
    }
}

/// Steady turn for the biaxial check: lateral acceleration V^2 / R.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Turning {
    /// Boat speed through the turn, m/s.
    pub speed_mps: f64,
    /// Turn radius, in.
    pub radius_in: f64,
}

impl Turning {
    /// Lateral acceleration in g: k = V^2 / (g R). R -> infinity gives 0.
    pub fn lateral_g(&self) -> f64 {
        let r_m = self.radius_in / crate::units::M_TO_IN;
        self.speed_mps * self.speed_mps / (crate::units::G_MPS2 * r_m)
    }
}

/// Where paddlers kneel. Defaults: auto seats, 30 in apart, 36 in from
/// each end, checked at +/- 3 in.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct Seats {
    /// Cases without `seats_in`: true = choose seats and crew order with the
    /// most margin on the seat-dependent gates (src/seats.rs); false = evenly spaced.
    pub auto: bool,
    /// Auto seats: minimum knee-to-knee spacing between paddlers, in.
    pub spacing_min_in: f64,
    /// Auto seats: no knee closer than this to the bow / stern tip, in.
    pub bow_clear_in: f64,
    pub stern_clear_in: f64,
    /// Every case (auto, even or given seats) must pass with each paddler
    /// anywhere within +/- this of their seat: the nominal seats and all
    /// 2^k corners are checked. 0 = nominal seats only.
    pub variance_in: f64,
}

impl Default for Seats {
    fn default() -> Self {
        Seats { auto: true, spacing_min_in: 30.0, bow_clear_in: 36.0, stern_clear_in: 36.0, variance_in: 3.0 }
    }
}

/// Swamped flotation (ASCE: the canoe must float when swamped).
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FlotationParams {
    /// Max length of each sealed air pocket, measured in from the bow and from the stern, in.
    pub air_pocket_max_in: f64,
    /// Wall + pocket volume must displace >= swamp_factor x canoe weight (fresh water).
    pub swamp_factor: f64,
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
    /// generated. Typical canoe flare 5-15 deg. With `gunwale_radius_in` it is
    /// the lean at the widest station only.
    #[serde(default)]
    pub flare_deg: f64,
    /// Present = constructable gunwale: in top view straight from the bow,
    /// one arc of this radius (in) through the middle, straight to the stern;
    /// sides ruled from the design waterline up to it (`hull::Gunwale`).
    /// Designs whose sides would lean in are rejected. In a shape search,
    /// `shape_search.gunwale_radius_in` searches the radius instead.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gunwale_radius_in: Option<f64>,
    /// Present = build the hull from this many cross-section templates with a
    /// straight loft between neighbours (`Hull::templated`; hot-wire foam or
    /// frames + straight battens). The analysed, exported hull is that loft; in
    /// a shape search every generated shape is reduced before it is analysed,
    /// so the search tunes the buildable hull. >= 3.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub templates: Option<usize>,
    /// Present = side-view radius, in, at the forefoot of the bow and stern (where the stem
    /// meets the keel line; `Hull::filleted`), so the bottom has no point (stress
    /// concentration). Plan-view entry stays sharp. Applied after `templates`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keel_fillet_radius_in: Option<f64>,
    /// With a gunwale outline: steepest outward side lean allowed anywhere
    /// along the hull, deg from vertical (default 30). Steeper = rejected.
    #[serde(default = "gunwale_max_flare")]
    pub gunwale_max_flare_deg: f64,
    /// Doubly symmetric canoe (manufacturability: one end mold makes both ends): bow and stern
    /// identical as well as port and starboard. A hull file is made fore-aft symmetric
    /// (`Hull::symmetrized`); a shape search ties the stern factors to the bow's (same as
    /// `shape_search.symmetric`). Default off.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub doubly_symmetric: bool,
}

fn gunwale_max_flare() -> f64 {
    crate::hull::GUNWALE_MAX_FLARE_DEG
}

impl Params {
    /// Flares the wall search tries: `search.flare_deg`, else `hull.flare_deg`.
    pub fn flares(&self) -> Vec<f64> {
        self.search.flare_deg.map_or_else(|| vec![self.hull.flare_deg], range)
    }
}

impl HullParams {
    pub fn topsides(&self) -> crate::hull::Topsides {
        crate::hull::Topsides {
            flare_deg: self.flare_deg,
            gunwale_radius_in: self.gunwale_radius_in,
            gunwale_max_flare_deg: self.gunwale_max_flare_deg,
        }
    }
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
    /// "asd" (default): service loads (crew x dynamic factor), every capacity /
    /// `factor_of_safety`.
    /// "lrfd": the ASCE 7-22 2.3.1 gravity combinations lrfd_dead D + lrfd_live L
    /// (1.2D + 1.6L) and lrfd_dead_only D (1.4D, no crew), L = crew x dynamic factor
    /// (impact is part of the live load before factoring, ASCE 7 4.6); buoyancy is a
    /// fluid load factored like D (it balances the factored weight); capacity x phi
    /// (ACI 318-19 21.2.1: 0.60 plain concrete, 0.75 for `aci_22_6` punching);
    /// `factor_of_safety` unused.
    /// Freeboard, GM and drag always use service loads.
    #[serde(default)]
    pub method: DesignMethod,
    /// LRFD factor on self weight; the buoyancy is solved to balance the
    /// factored weight, so it carries the same factor. Default 1.2.
    #[serde(default = "lrfd_dead")]
    pub lrfd_dead: f64,
    /// LRFD factor on self weight in the dead-load-only combination (default 1.4).
    #[serde(default = "lrfd_dead_only")]
    pub lrfd_dead_only: f64,
    /// Removed (the 0.9D + 1.6L combination is not an ASCE 7 gravity combination); kept
    /// only to refuse old files with a message instead of "unknown field".
    #[serde(default, skip_serializing)]
    pub lrfd_dead_min: Option<f64>,
    /// LRFD factor on the paddler loads (default 1.6).
    #[serde(default = "lrfd_live")]
    pub lrfd_live: f64,
    pub factor_of_safety: f64,
    /// Multiplier on crew weight for paddling impact.
    pub dynamic_load_factor: f64,
    /// Knee contact patch length, in. NEVER zero: a point load is a
    /// singularity and D/C stops converging.
    pub knee_patch_in: f64,
}

#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum DesignMethod {
    #[default]
    Asd,
    Lrfd,
}

fn lrfd_dead() -> f64 {
    1.2
}
fn lrfd_dead_only() -> f64 {
    1.4
}
fn lrfd_live() -> f64 {
    1.6
}

impl Material {
    /// (dead, live) load factors of every strength combination.
    pub fn combos(&self) -> Vec<(f64, f64)> {
        match self.method {
            DesignMethod::Asd => vec![(1.0, 1.0)],
            DesignMethod::Lrfd => {
                vec![(self.lrfd_dead, self.lrfd_live), (self.lrfd_dead_only, 0.0)]
            }
        }
    }
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
    /// Explicit override: effective depth to the mesh as a fraction of wall
    /// thickness (aci_22_6). Mutually exclusive with the layer fields below.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub d_over_t: Option<f64>,
    /// Equal-thickness casting passes (>= 2 so a mesh fits between two).
    /// `layers`, `mesh_after_layer` and `mold` are given together or not at all;
    /// all absent (and no `d_over_t`) means 2 layers, mesh after pass 1 (d = t/2).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub layers: Option<u32>,
    /// A mesh is laid after pass k, 1 <= k < layers: one pass (`2`) or several
    /// meshes (`[1, 2]`). Punching uses the mesh farthest from the knee face.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mesh_after_layer: Option<MeshAfter>,
    /// Which mold face pass 1 is cast against (decides which face the mesh is measured from).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mold: Option<Mold>,
}

/// Passes a mesh is laid after: a single pass or a list (one entry per mesh).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(untagged)]
pub enum MeshAfter {
    One(u32),
    Many(Vec<u32>),
}

impl MeshAfter {
    pub fn passes(&self) -> &[u32] {
        match self {
            MeshAfter::One(k) => std::slice::from_ref(k),
            MeshAfter::Many(ks) => ks,
        }
    }
}

/// Mold type. Pass 1 is cast against the mold, so on a male mold pass 1 is the
/// INSIDE face (the one the knee bears on), on a female mold the OUTSIDE.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Mold {
    Male,
    Female,
}

impl Punching {
    /// Effective depth d / t for `aci_22_6`: distance from the loaded (inside,
    /// knee) face to the mesh, over the wall thickness. Mesh after pass k of n
    /// equal passes sits k t/n from the pass-1 face, so
    /// male (pass 1 inside): d/t = k/n; female (pass 1 outside): d/t = (n-k)/n.
    /// t = 0.375, n = 3, k = 2: male d = 0.25 in, female d = 0.125 in.
    /// Several meshes: the one farthest from the knee face (the tension face under
    /// the knee) sets d, assuming it is strong in both directions. 3 passes,
    /// meshes after 1 and 2: d = 2t/3 on either mold.
    /// Casting passes the wall is built in: `layers`, or 2 with no layer fields (the documented
    /// default); None with an explicit `d_over_t` (no pass count given).
    pub fn layer_count(&self) -> Option<u32> {
        match (self.d_over_t, self.layers) {
            (Some(_), _) => None,
            (None, Some(n)) => Some(n),
            (None, None) => Some(2),
        }
    }

    pub fn d_over_t(&self) -> Result<f64> {
        let layer_fields = self.layers.is_some() || self.mesh_after_layer.is_some() || self.mold.is_some();
        if let Some(r) = self.d_over_t {
            if layer_fields {
                bail!("punching: give either d_over_t or layers/mesh_after_layer/mold, not both");
            }
            if !(r > 0.0 && r <= 1.0) {
                bail!("punching: need 0 < d_over_t <= 1, got {r}");
            }
            return Ok(r);
        }
        let (n, ks, mold) = match (self.layers, &self.mesh_after_layer, self.mold) {
            (None, None, None) => (2, &[1][..], Mold::Male),
            (Some(n), Some(k), Some(m)) => (n, k.passes(), m),
            _ => bail!("punching: layers, mesh_after_layer and mold must be given together"),
        };
        if ks.is_empty() {
            bail!("punching: mesh_after_layer needs at least one pass");
        }
        let mut sorted = ks.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        if sorted.len() != ks.len() {
            bail!("punching: mesh_after_layer lists a pass twice: {ks:?}");
        }
        if n < 2 || ks.iter().any(|&k| k < 1 || k >= n) {
            bail!(
                "punching: need layers >= 2 and 1 <= mesh_after_layer < layers, got layers = {n}, mesh_after_layer = {ks:?}"
            );
        }
        let depth = |k: u32| {
            let from_pass1 = f64::from(k) / f64::from(n);
            match mold {
                Mold::Male => from_pass1,
                Mold::Female => 1.0 - from_pass1,
            }
        };
        Ok(ks.iter().map(|&k| depth(k)).fold(f64::NEG_INFINITY, f64::max))
    }
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
    /// Minimum metacentric height GM_T, in, of EVERY load case (and seat
    /// sample). GM > 0 = upright is stable. Without this gate a shape search
    /// makes the hull as narrow as possible.
    pub gm_min_in: f64,
    /// Minimum GM_T of the empty canoe (no crew), in. Absent = not gated (it is
    /// still computed and reported).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gm_empty_min_in: Option<f64>,
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
    /// Shortest weighted race time over the `[race]` events (src/race.rs):
    /// wall weight, displacement, straight and turning drag all enter through
    /// time. Needs `[race]` and `[shape_search]` (on a fixed hull the lightest
    /// wall is also the fastest, so use "weight").
    Race,
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
    /// the shape score; the rest goes to the steady-turn drag at
    /// `turn_radius_in` (docs/SHAPE_SEARCH.md). Default 1.0 = drag only.
    #[serde(default = "one")]
    pub straight_weight: f64,
    /// Radius of the buoy turn the turning score is computed at, in
    /// (default 600 = 50 ft, about 2.5 hull lengths).
    #[serde(default = "turn_radius")]
    pub turn_radius_in: f64,
}

fn one() -> f64 {
    1.0
}

fn turn_radius() -> f64 {
    600.0
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
    /// [min, max]: the waterline beam is sampled in it, and a design whose overall beam
    /// (flared topsides / gunwale included) exceeds max fails.
    pub beam_in: [f64; 2],
    pub draft_in: [f64; 2],
    pub fore_wp: [f64; 2],
    pub section: [f64; 2],
    pub fore_rocker: [f64; 2],
    pub aft_wp: [f64; 2],
    pub aft_rocker: [f64; 2],
    pub fore_len_frac: [f64; 2],
    pub aft_len_frac: [f64; 2],
    /// Present = line-arc-line gunwale (see `hull.gunwale_radius_in`) with the
    /// arc radius searched in [min, max], in. Absent = `hull.gunwale_radius_in`
    /// (fixed) or no gunwale outline.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gunwale_radius_in: Option<[f64; 2]>,
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
    /// The last, `gunwale_radius_in`, is [0, 0] (off, never searched) when absent.
    pub fn bounds(&self) -> [(&'static str, [f64; 2]); SHAPE_DIMS] {
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
            ("gunwale_radius_in", self.gunwale_radius_in.unwrap_or([0.0, 0.0])),
        ]
    }
}

/// Searched shape variables: the 10 Series 7 factors and the gunwale radius.
pub const SHAPE_DIMS: usize = 11;

/// Search axes. Each is an inclusive `[min, max, step]` range.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Search {
    pub thickness_in: [f64; 3],
    pub density_pcf: [f64; 3],
    pub depth_in: [f64; 3],
    /// Optional flare axis [min, max, step], deg: the flare (demolding draft)
    /// becomes a wall-search variable, so more flare (more wall) is chosen only
    /// when it buys a gate (freeboard, flotation, ...). Absent = `hull.flare_deg`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flare_deg: Option<[f64; 3]>,
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
        // shape_search.symmetric is the shape-search form of hull.doubly_symmetric: either sets both.
        if let Some(ss) = &mut p.shape_search {
            let sym = ss.symmetric || p.hull.doubly_symmetric;
            (ss.symmetric, p.hull.doubly_symmetric) = (sym, sym);
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
        if let Some([lo, hi, st]) = self.search.flare_deg {
            if !(lo >= 0.0 && hi <= 45.0 && lo <= hi && st > 0.0) {
                bail!("search.flare_deg must be [min >= 0, max <= 45, step > 0], got [{lo}, {hi}, {st}]");
            }
            if self.hull.flare_deg != 0.0 {
                bail!("give hull.flare_deg (fixed) or search.flare_deg (searched), not both");
            }
        }
        let pu = &self.punching;
        if !(pu.knees == 1 || pu.knees == 2)
            || !(0.5..=1.0).contains(&pu.knee_split)
            || pu.knee_spacing_in.is_nan()
            || pu.knee_spacing_in < 0.0
            || !(0.0..=0.5).contains(&pu.pad_in)
        {
            bail!("punching: need knees 1|2, knee_split 0.5..1, knee_spacing_in >= 0, pad_in 0..0.5");
        }
        pu.d_over_t()?;
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
        let st = &self.seats;
        if ![st.spacing_min_in, st.bow_clear_in, st.stern_clear_in, st.variance_in]
            .iter()
            .all(|v| v.is_finite() && *v >= 0.0)
        {
            bail!("seats: spacing_min_in, bow_clear_in, stern_clear_in and variance_in must be >= 0");
        }
        for lc in &self.load_cases {
            let k = lc.weights_lb.len();
            if st.auto && lc.seats_in.is_none() && k > 4 {
                bail!(
                    "load_case '{}': auto seats try every crew order, so at most 4 paddlers; give seats_in",
                    lc.name
                );
            }
            if st.variance_in > 0.0 && k > 6 {
                bail!(
                    "load_case '{}': seats.variance_in checks 2^k seat corners, so at most 6 paddlers",
                    lc.name
                );
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
        let m = &self.material;
        if m.lrfd_dead_min.is_some() {
            bail!(
                "material.lrfd_dead_min was removed: 0.9D + 1.6L is not an ASCE 7 gravity combination; \
                 LRFD now checks 1.2D + 1.6L and 1.4D (material.lrfd_dead_only); delete the line"
            );
        }
        if ![m.lrfd_dead, m.lrfd_dead_only, m.lrfd_live].iter().all(|v| v.is_finite() && *v > 0.0) {
            bail!("material: need lrfd_dead, lrfd_dead_only, lrfd_live > 0");
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
        if !(self.objective.turn_radius_in.is_finite() && self.objective.turn_radius_in > 0.0) {
            bail!("objective.turn_radius_in must be > 0");
        }
        let sw = self.objective.straight_weight;
        if !(0.0..=1.0).contains(&sw) {
            bail!("objective.straight_weight must be in [0, 1]");
        }
        if sw < 1.0 && (self.objective.kind != ObjectiveKind::Drag || self.shape_search.is_none()) {
            bail!("objective.straight_weight < 1 needs kind = \"drag\" and a [shape_search]");
        }
        if self.objective.kind == ObjectiveKind::Race && (self.race.is_none() || self.shape_search.is_none())
        {
            bail!(
                "objective.kind = \"race\" needs [race] and [shape_search] \
                 (on a fixed hull the lightest wall is also the fastest: use \"weight\")"
            );
        }
        if self.stability.crew_cg_in.is_nan() || self.stability.crew_cg_in < 0.0 {
            bail!("stability.crew_cg_in must be >= 0");
        }
        if self.stability.gm_empty_min_in.is_some_and(|g| !g.is_finite()) {
            bail!("stability.gm_empty_min_in must be a number");
        }
        if let Some(f) = &self.flotation
            && !(f.air_pocket_max_in >= 0.0 && f.swamp_factor > 0.0 && f.swamp_factor.is_finite())
        {
            bail!("flotation: need air_pocket_max_in >= 0 and swamp_factor > 0");
        }
        if let Some(t) = &self.turning
            && !(t.speed_mps.is_finite()
                && t.speed_mps >= 0.0
                && t.radius_in.is_finite()
                && t.radius_in > 0.0)
        {
            bail!("turning: need speed_mps >= 0 and radius_in > 0");
        }
        if !(self.gates.dc_search <= self.gates.dc_verify && self.gates.dc_verify <= 1.0) {
            bail!("gates: need dc_search <= dc_verify <= 1.0");
        }
        if let Some(r) = &self.race {
            r.validate()?;
        }
        if let Some(z) = &self.zones {
            let [lo, hi, st] = z.dense_pcf;
            if !(lo.is_finite() && hi.is_finite() && lo > 0.0 && hi >= lo && st > 0.0) {
                bail!("zones.dense_pcf must be [min > 0, max >= min, step > 0], got [{lo}, {hi}, {st}]");
            }
            if !(z.increment_in.is_finite() && z.increment_in > 0.0 && z.increment_in <= 6.0) {
                bail!("zones.increment_in must be in (0, 6] in, got {}", z.increment_in);
            }
            if z.max_checks < 1 {
                bail!("zones.max_checks must be >= 1");
            }
        }
        if let Some(r) = self.hull.gunwale_radius_in
            && !(r.is_finite() && r > 0.0)
        {
            bail!("hull.gunwale_radius_in must be > 0");
        }
        if self.hull.keel_fillet_radius_in.is_some_and(|r| !(r.is_finite() && r > 0.0 && r <= 6.0)) {
            bail!("hull.keel_fillet_radius_in must be in (0, 6] in");
        }
        if self.hull.templates.is_some_and(|n| n < 3) {
            bail!("hull.templates must be >= 3 (bow tip, one section, stern tip)");
        }
        if !(self.hull.gunwale_max_flare_deg > 0.0 && self.hull.gunwale_max_flare_deg < 90.0) {
            bail!("hull.gunwale_max_flare_deg must be in (0, 90)");
        }
        if let Some(ss) = &self.shape_search
            && let Some([lo, _]) = ss.gunwale_radius_in
        {
            if lo <= 0.0 {
                bail!("shape_search.gunwale_radius_in min must be > 0");
            }
            if self.hull.gunwale_radius_in.is_some() {
                bail!(
                    "give hull.gunwale_radius_in (fixed) or shape_search.gunwale_radius_in (searched), not both"
                );
            }
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
        assert_eq!(p.load_cases.len(), 2);
    }

    #[test]
    fn example_race_section_loads_and_is_checked() {
        let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
        let r = p.race.as_ref().unwrap();
        assert_eq!((r.courses.len(), r.events.len()), (4, 9));
        let text = std::fs::read_to_string("examples/canoe.toml").unwrap();
        let bad = |from: &str, to: &str| {
            assert!(text.contains(from), "{from}");
            Params::parse(&text.replacen(from, to, 1), Path::new("examples")).unwrap_err().to_string()
        };
        assert!(bad("course = \"slalom_200\"", "course = \"nope\"").contains("no course named 'nope'"));
        assert!(bad("weight = 0.125", "weight = 0.5").contains("sum to 1"));
        assert!(bad("{ x_m = 10.0, y_m = -5.0,", "{ x_m = 10.0, y_m = -9.0,").contains("outside the lane"));
        // A course may widen its own lane (open-water loop), never narrow it below the race lane.
        assert!(bad("lane_width_m = 400.0", "lane_width_m = 10.0").contains("lane_width_m must be >="));
        assert!(bad("lane_width_m = 400.0", "lane_width_m = 20.0").contains("outside the lane"));
        assert!(bad("lane_width_m = 15.0", "lane_width = 15.0").contains("unknown field"));
    }

    #[test]
    fn race_objective_needs_race_and_shape_search() {
        let text = std::fs::read_to_string("examples/canoe.toml")
            .unwrap()
            .replace("kind = \"drag\"", "kind = \"race\"");
        let e = Params::parse(&text, Path::new("examples")).unwrap_err().to_string();
        assert!(e.contains("needs [race] and [shape_search]"), "{e}");
        let p = Params::load(Path::new("examples/shape_race.toml")).unwrap();
        assert_eq!(p.objective.kind, ObjectiveKind::Race);
    }

    #[test]
    fn lrfd_factors_are_inputs() {
        let text = std::fs::read_to_string("examples/canoe.toml").unwrap();
        let p = Params::parse(&text, Path::new("examples")).unwrap();
        assert_eq!((p.material.lrfd_dead, p.material.lrfd_dead_only, p.material.lrfd_live), (1.2, 1.4, 1.6));
        assert_eq!(p.material.combos(), vec![(1.0, 1.0)], "asd: service loads");
        // ASCE 7 gravity combinations: 1.2D + 1.6L and 1.4D alone (no crew).
        let lrfd = text.replace("method = \"asd\"", "method = \"lrfd\"\nlrfd_dead = 1.25\nlrfd_live = 1.5");
        let p = Params::parse(&lrfd, Path::new("examples")).unwrap();
        assert_eq!(p.material.combos(), vec![(1.25, 1.5), (1.4, 0.0)]);
        // The removed 0.9D + 1.6L factor is refused with a message, not silently ignored.
        let old = text.replace("method = \"asd\"", "method = \"lrfd\"\nlrfd_dead_min = 0.9");
        let e = Params::parse(&old, Path::new("examples")).unwrap_err().to_string();
        assert!(e.contains("lrfd_dead_min was removed") && e.contains("1.4D"), "{e}");
        let bad = text.replace("method = \"asd\"", "method = \"lrfd\"\nlrfd_dead_only = 0.0");
        assert!(Params::parse(&bad, Path::new("examples")).is_err());
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
        p.objective.straight_weight = 0.8;
        assert_eq!(p.objective.turn_radius_in, 600.0, "default");
        p.objective.turn_radius_in = 0.0;
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

    fn layered(n: u32, k: u32, mold: Mold) -> Punching {
        meshes(n, MeshAfter::One(k), mold)
    }

    fn meshes(n: u32, ks: MeshAfter, mold: Mold) -> Punching {
        let mut pu = Params::load(Path::new("examples/canoe.toml")).unwrap().punching;
        (pu.d_over_t, pu.layers, pu.mesh_after_layer, pu.mold) = (None, Some(n), Some(ks), Some(mold));
        pu
    }

    #[test]
    fn two_meshes_use_the_one_farthest_from_the_knee() {
        // 3 equal passes, a mesh after pass 1 and after pass 2: one mesh sits 2t/3
        // from the knee face on either mold.
        for mold in [Mold::Male, Mold::Female] {
            let r = meshes(3, MeshAfter::Many(vec![1, 2]), mold).d_over_t().unwrap();
            assert!((r - 2.0 / 3.0).abs() < 1e-12, "{mold:?}: {r}");
        }
        // Order does not matter; a one-entry list equals the single form.
        let r = meshes(4, MeshAfter::Many(vec![3, 1]), Mold::Female).d_over_t().unwrap();
        assert!((r - 0.75).abs() < 1e-12);
        assert_eq!(
            meshes(3, MeshAfter::Many(vec![2]), Mold::Male).d_over_t().unwrap(),
            layered(3, 2, Mold::Male).d_over_t().unwrap()
        );
        // TOML: integer or list.
        let base = std::fs::read_to_string("examples/canoe.toml").unwrap();
        let with = |v: &str| {
            base.replace("layers = 2 ", "layers = 3 ")
                .replace("mesh_after_layer = 1 ", &format!("mesh_after_layer = {v} "))
        };
        let p = Params::parse(&with("[1, 2]"), Path::new("examples")).unwrap();
        assert_eq!(p.punching.mesh_after_layer, Some(MeshAfter::Many(vec![1, 2])));
        let p = Params::parse(&with("2"), Path::new("examples")).unwrap();
        assert_eq!(p.punching.mesh_after_layer, Some(MeshAfter::One(2)));
        assert!(Params::parse(&with("[1, 3]"), Path::new("examples")).is_err(), "mesh after the last pass");
        assert!(Params::parse(&with("[2, 2]"), Path::new("examples")).is_err(), "same pass twice");
        assert!(Params::parse(&with("[]"), Path::new("examples")).is_err(), "no mesh");
        assert!(Params::parse(&with("\"2\""), Path::new("examples")).is_err(), "not a number");
    }

    #[test]
    fn mesh_depth_from_layers_and_mold() {
        // data/review_asce_report_and_tasks.md item 5: t = 0.375, mesh after pass 2 of 3.
        let t = 0.375;
        assert!((layered(3, 2, Mold::Male).d_over_t().unwrap() * t - 0.25).abs() < 1e-12);
        assert!((layered(3, 2, Mold::Female).d_over_t().unwrap() * t - 0.125).abs() < 1e-12);
        assert!((layered(3, 1, Mold::Male).d_over_t().unwrap() - 1.0 / 3.0).abs() < 1e-12);
        // 2 layers, mesh between: mid-wall either way; also the default when nothing is given.
        assert_eq!(layered(2, 1, Mold::Female).d_over_t().unwrap(), 0.5);
        let mut pu = layered(2, 1, Mold::Male);
        (pu.layers, pu.mesh_after_layer, pu.mold) = (None, None, None);
        assert_eq!(pu.d_over_t().unwrap(), 0.5);
        pu.d_over_t = Some(0.7);
        assert_eq!(pu.d_over_t().unwrap(), 0.7);
    }

    #[test]
    fn mesh_depth_inputs_are_strict() {
        let mut p = Params::load(Path::new("examples/canoe.toml")).unwrap();
        let ok = layered(3, 2, Mold::Male);
        p.punching = ok.clone();
        p.validate().unwrap();
        let bad = |f: &dyn Fn(&mut Punching)| {
            let mut q = p.clone();
            f(&mut q.punching);
            q.validate().is_err()
        };
        assert!(bad(&|pu| pu.d_over_t = Some(0.5)), "d_over_t together with layers");
        assert!(bad(&|pu| pu.mold = None), "layers without mold");
        assert!(bad(&|pu| pu.mesh_after_layer = None), "layers without mesh_after_layer");
        assert!(bad(&|pu| pu.mesh_after_layer = Some(MeshAfter::One(3))), "mesh after the last pass");
        assert!(bad(&|pu| pu.mesh_after_layer = Some(MeshAfter::One(0))), "mesh before the first pass");
        assert!(
            bad(&|pu| (pu.layers, pu.mesh_after_layer) = (Some(1), Some(MeshAfter::One(1)))),
            "one layer"
        );
        assert!(bad(
            &|pu| (pu.layers, pu.mesh_after_layer, pu.mold, pu.d_over_t) = (None, None, None, Some(0.0))
        ));
        assert!(bad(
            &|pu| (pu.layers, pu.mesh_after_layer, pu.mold, pu.d_over_t) = (None, None, None, Some(1.5))
        ));
        let text = std::fs::read_to_string("examples/canoe.toml")
            .unwrap()
            .replace("mold = \"male\"", "mold = \"wood\"");
        assert!(Params::parse(&text, Path::new("examples")).is_err(), "unknown mold");
    }

    #[test]
    fn turning_is_optional_and_strict() {
        let base = std::fs::read_to_string("examples/canoe.toml").unwrap();
        assert!(Params::parse(&base, Path::new("examples")).unwrap().turning.is_none());
        let t = |extra: &str| Params::parse(&format!("{base}\n[turning]\n{extra}\n"), Path::new("examples"));
        let p = t("speed_mps = 2.5\nradius_in = 393.7007874015748").unwrap();
        // 2.5^2 / (9.80665 x 10 m)
        assert!((p.turning.unwrap().lateral_g() - 6.25 / 98.0665).abs() < 1e-12);
        assert!(t("speed_mps = 2.5\nradius_in = 0.0").is_err());
        assert!(t("speed_mps = -1.0\nradius_in = 400.0").is_err());
        assert!(t("speed_mps = 2.5\nradius_in = 400.0\nbogus = 1").is_err());
        assert!(t("speed_mps = 2.5").is_err(), "radius required");
    }

    #[test]
    fn range_is_inclusive() {
        assert_eq!(range([60.0, 64.0, 2.0]), vec![60.0, 62.0, 64.0]);
        assert_eq!(range([0.375, 0.75, 0.125]).len(), 4);
    }
}
