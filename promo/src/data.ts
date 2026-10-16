// Typed access to src/generated/promo-data.json (made by scripts/extract.mjs from promo/data).
// Nothing in the video may show a number that does not come through here.
import raw from './generated/promo-data.json';
import type { Hull } from './hull/loft';

export type Check = { name: string; dc: number; demand: number; capacity: number };
export type Winner = {
  hull_weight_lb: number;
  thickness_in: number;
  density_pcf: number;
  depth_in: number;
  flare_deg: number;
  length_in: number;
  beam_in: number;
  gm_in: number;
  gm_empty_in: number;
  drag_n: number | null;
  race_s: number | null;
  dc: number;
  governing_check: string;
  freeboard_in: number;
  checks: Check[];
  verified: boolean;
  verify_stations: number;
  verify_dc: number;
  shape: ShapeFactors | null;
  /** Verify resolutions (search n, 2n-1, 4n-3) and the governing D/C at each. */
  convergence: { stations: number[]; dc: number[] } | null;
  search_space: Array<{ name: string; min: number; max: number; count: number | null; winner: number }>;
};
export type ShapeFactors = {
  length_in: number;
  beam_in: number;
  draft_in: number;
  fore_wp: number;
  aft_wp: number;
  section: number;
  fore_rocker: number;
  aft_rocker: number;
  fore_len_frac: number;
  aft_len_frac: number;
  metrics?: { entry_angle_deg: number; rocker_bow_in: number; rocker_stern_in: number };
};
export type Gates = { dc_search: number; dc_verify: number; freeboard_min_in: number; gm_min_in: number };
export type Case = {
  name: string;
  seats_in: number[];
  weights_lb: number[];
  freeboard_in: number;
  gm_in: number;
  x_in: number[];
  shear_lb: number[];
  moment_lbin: number[];
  tension_psi: number[];
  tension_allow_psi: number;
  shear_psi: number[];
  shear_allow_psi: number;
  waterline_in: number[];
  dc_bending_tension: number[];
  dc_bending_compression: number[];
  dc_shear: number[];
  dc_biaxial: number[];
};
export type PathShape = {
  index: number;
  phase: string;
  passes?: boolean;
  hull_weight_lb: number;
  depth_in: number;
  flare_deg: number;
  dc?: number | null;
  shape: ShapeFactors;
  hull: Hull;
};
export type RaceSummary = {
  hull: string;
  hull_lb: number;
  weighted_time_s: number;
  events: Array<{ name: string; course: string; time_s: number; distance_m: number; crew_lb: number; paddlers: number; weight: number }>;
};
export type RaceSegment = { mark: number | null; radius_m: number | null; length_m: number; angle_deg: number | null; drift_deg: number | null };
export type RaceEvent = {
  name: string;
  hull: string;
  hull_lb: number;
  paddlers: number;
  crew_lb: number;
  course: { name: string; finish_x_m: number; marks: Array<{ x_m: number; y_m: number; keep: 'port' | 'starboard' }> };
  start_y_m: number;
  time_s: number;
  distance_m: number;
  segments: RaceSegment[];
  /** Planned path, m: start on x = 0 heading +x, y = lane centre (+y = port). */
  path_m: Array<[number, number]>;
  /** [distance m, speed m/s, time s] about every metre. */
  speed_m: Array<[number, number, number]>;
};
export type PromoData = {
  provenance: string;
  off1: {
    hull: Hull;
    winner: Winner;
    gates: Gates;
    /** [weight lb, D/C | null, passes 0/1] per wall candidate. */
    candidates: Array<[number, number | null, number]>;
    cases: Case[];
    empty: { x_in: number[]; waterline_in: number[]; freeboard_in: number };
    terminal: string;
    command: string;
  };
  shapeOpt: {
    winner: Winner;
    gates: Gates;
    winner_hull: Hull;
    /** [weight lb, D/C | null, passes 0/1, length in, beam in] per shape, evaluation order. */
    candidates: Array<[number, number | null, number, number, number]>;
    /** The winner hull's own wall grid: [weight lb, D/C | null, passes 0/1]. */
    winner_walls: Array<[number, number | null, number]>;
    shapes_evaluated: number;
    latin_hypercube_samples: number;
    cases: Case[];
    empty: { x_in: number[]; waterline_in: number[]; freeboard_in: number };
    incumbents: PathShape[];
    spread: PathShape[];
    shape_search_toml: string[];
  };
  shapeRace: { winner: Winner; winner_hull: Hull };
  race: {
    illustrative: boolean;
    race_pick: RaceSummary;
    weight_pick: RaceSummary;
    off1: RaceSummary;
    lane_width_m: number;
    buoy_clearance_m: number;
    slalom: RaceEvent;
  };
  mix: {
    points: Array<{ mix: string; density_pcf: number; final_psi: number; compressive_psi?: number }>;
    split_slope_psi_per_pcf: number;
    split_intercept_psi: number;
    rms_residual_psi: number;
    density_min_pcf: number;
    density_max_pcf: number;
  };
};

export const data = raw as unknown as PromoData;
