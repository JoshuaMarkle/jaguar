# JAGUAR spec

## Question answered
Given a hull, a crew, and the team's concrete lab data: **what is the lightest uniform wall
(thickness t, mix density rho, mold depth D) that passes every check for every load case?**
Optionally the hull shape is searched too (`[shape_search]`).

## Interface
```
jaguar check  [canoe.toml]                     validate inputs, print hull summary
jaguar run    [canoe.toml] [-o result.json]    optimize; exit 0 verified, 2 no verified design, 1 error
jaguar mix    [data/mix_tests.csv]             print lab points and the strength fit (docs/MIX_DATA.md)
jaguar gen-hull shape.toml [-o hull.json]      parametric hull (Michlet Series 7), see docs/HULLS.md
jaguar import-michlet off1.mlt out.mlt [-o hull.json]
jaguar export result.json|hull.json [-o canoe.step|canoe.stl] [--thickness in] [--depth in] [--flare deg] [--no-pockets]
jaguar race   [result.json] [--params canoe.toml] [-o race.json] [--measured "EVENT=SECONDS"]
jaguar serve  [canoe.toml] [-p 8000] [--prefs <path>]  web UI: 3D canoe, edit params, run + watch live
```
`serve`: the form is the only editor. Each edit is autosaved (validated, absolute paths) to
`--prefs`, default `$XDG_CONFIG_HOME/jaguar/prefs.toml` else `~/.config/jaguar/prefs.toml`, and
restored on reopen; "Reset to canoe.toml" deletes it. The mix panel charts and edits the lab CSV
(saved over `material.mix_data` after it parses and fits, previous kept as `.bak`). HTTP API: `src/serve.rs`.

## Inputs

### `canoe.toml`
`examples/canoe.toml` is the canonical, commented example; the schema is `src/params.rs`.
Sections: `[hull]`, one or more `[[load_case]]`, `[material]`, `[search]`, `[gates]`, `[punching]`,
`[stability]`, `[objective]`, optional `[shape_search]`, optional `[flotation]`, optional `[turning]`, `[seats]`,
optional `[race]` (read only by `jaguar race`). Paths are relative to the toml.
Unknown keys are errors, so a typo never becomes a silent default.

### `jaguar-hull/1` (hull geometry, JSON)
```json
{
  "format": "jaguar-hull/1",
  "units": "in",
  "name": "off1",
  "source": "where the offsets came from",
  "length": 240.12,
  "draft_design": 5.91,
  "x": [0.0, ..., 240.12],              // stations, bow = 0, strictly increasing
  "z": [0.0, ..., 5.91],                // waterlines, keel = 0, strictly increasing
  "half_breadth": [[...], ...]          // [station][waterline], in, >= 0
}
```
A dense rectangular offset table is what every hull tool (Michlet, CAD section cuts, spreadsheets)
can produce, needs only linear interpolation, and the browser can mesh it directly.

## Physics
1. **Grid**: resample to `hull.stations` evenly spaced x. Topsides above `draft_design` are
   extended to the mold depth D (10 extra waterlines), vertical or leaning out by `hull.flare_deg`.
2. **Section** per station: thin shell of thickness t on both sides + keel strip: A, zbar, I. The
   girth follows the hull: waterline segments with zero half-breadth at both ends (below the
   rockered keel, the closed stem) are not wall. Same rule for the wetted surface in drag.
3. **Equilibrium**: self weight w = A*rho/1728 lb/in. Crew = weight * `dynamic_load_factor`, each
   paddler on a `knee_patch_in` patch at their seat. Draft and trim
   are solved (Newton, bracketing fallback) so fresh-water buoyancy (62.4 pcf) balances force and moment, then
   q -> V -> M by integration. Free-free closure: |V(L)| <= 1e-3 W, |M(L)| <= 1e-3 W L.
   Also gives loaded freeboard (D minus local draft, min over stations) and transverse GM_T
   (crew CG at `stability.crew_cg_in`).
   **Seats** (`src/seats.rs`): `seats_in` if given; else `[seats] auto = true` (default) picks knee
   positions and crew order (every distinct order, <= 4 paddlers) minimizing the highest
   seat-dependent utilization max(D/C without punching / dc_search, 1 - (fb - fb_min)/s,
   1 - (GM - gm_min)/s), s = max(min, 1 in); knees >= `spacing_min_in` apart within
   [`bow_clear_in`, L - `stern_clear_in`]. Solved once per (t, depth) at the middle density of the
   axis and reused for every density and for verify. `auto = false`: evenly spaced L(i+1)/(k+1).
   Every case is then checked at the nominal seats and all 2^k corners with each knee moved
   +/- `variance_in` (default 3 in); the case verdict is the worst sample, freeboard and GM the
   minimum. Search candidates stop sampling at the first sample that misses a gate.
4. **Material** (`src/mix.rs`, docs/MIX_DATA.md): least-squares line of FINAL split tensile strength
   vs density from the lab CSV. Flexural and f'c use their own least-squares lines when the CSV's
   optional `flexural_psi` / `compressive_psi` columns have data at >= 2 densities (per-mix means
   under tested_mixes), else flexural = `flex_over_split` * f_sp, f'c = (f_sp/6.7)^2.
   Densities are searched only within `density_extrapolation_pcf` of the tested range of every
   fitted line (split and each measured one; none below the lightest test of a line with a
   negative slope), or only at
   tested mixes with `strength_model = "tested_mixes"`. `material.method = "asd"` (default): service
   loads, every capacity divided by `factor_of_safety`. `"lrfd"`: each load case is re-solved for
   1.2D+1.6L and 0.9D+1.6L (D = self weight, L = crew x dynamic factor, buoyancy balancing the
   factored weight) and the worse combination governs; capacities x phi (0.60 plain concrete, 0.75
   for `aci_22_6` punching), never both. Freeboard, GM and drag always use service loads.
5. **Checks** (`src/check.rs`; governing D/C = max):
   * bending tension: |M| c / I at the tension fibre (bottom, c = zbar, when sagging M > 0; top,
     c = D - zbar, when hogging), vs flexural strength
     (stations with I > 1e-3 max I)
   * bending compression vs 0.85 f'c
   * shell shear VQ/(I*2t) vs 2 sqrt(f'c)
   * knee punching for the heaviest paddler, ACI 318-19 Table 22.6.5.2 or plain Table 14.5.5.1,
     one or two knees (docs/PUNCHING.md). Effective depth d (knee/inside face to mesh) comes from
     `[punching] layers`, `mesh_after_layer` (k) and `mold`: male d = k/layers t, female
     d = (layers-k)/layers t. `mesh_after_layer` may list several meshes (`[1, 2]`); the one
     farthest from the knee face sets d. `d_over_t` is an explicit override and an error together with them;
     with neither, 2 layers and mesh after 1 (d = t/2).
   * biaxial (Kupfer-Gerstle): per station, principal stresses of the bending fibre stress (both
     fibres) with that station's shell shear and, with `[turning]` (`speed_mps`, `radius_in`), the
     transverse turning stress at both signs; sigma_1 <= f_t/FS (1 - 0.8 |min(sigma_2, 0)| /
     (f'c/FS)), f_t = flexural strength. Turning: k = V^2/(gR), the floated weight W (hull + crew,
     as factored) is reacted by a uniform lateral pressure p = k W / A_lat on one side's underwater
     profile (h = local waterline - keel), each side a cantilever from the keel:
     sigma_y = 3 p h^2 / t^2 (`check::hoop_psi`). Absent = sigma_y 0
6. **Gates**: a candidate passes iff D/C <= `dc_search`, freeboard >= `freeboard_min_in` and
   GM_T >= `gm_min_in` for every load case (and seat sample), and, with `[flotation]`, the canoe floats swamped
   (`src/flotation.rs`): wall volume + sealed bow and stern air pockets (equal length l from each
   tip, full hull interior up to D = enclosed area minus wall area) displace >= `swamp_factor` x self weight at 62.4 pcf. l is found
   by bisection and reported as `air_pocket_in`; l > `air_pocket_max_in` fails with a reason. Thicknesses below `thickness_min_buildable_in` are
   never tried.
7. **Objective** (`objective.kind`, default `drag` when omitted): `weight` = lightest passing; `drag` = lowest Michell wave +
   ITTC-57 friction resistance at `speed_mps` (`src/resistance.rs`; use it to rank shapes, not as a
   race prediction). Drag is taken at the heaviest load case's loaded draft, or, with
   `objective.displacements = [{crew_lb, weight}, ...]` (weights sum to 1), as the weighted sum of
   resistance at the level draft each (hull weight + crew_lb) floats at. `objective.straight_weight`
   (default 1, needs drag + `[shape_search]`) blends in the steady-turn drag at `objective.turn_radius_in`
   (default 600), see docs/SHAPE_SEARCH.md. `race` (needs `[race]` and `[shape_search]`) = shortest
   weighted race time over the `[race]` events on paths planned for the midpoint shape
   (`candidate.race_s`); the winner is re-raced on its own paths (`result.race`, a
   `jaguar-race/1` record). The inner wall search ranks by weight (on one shape the lightest
   wall is also the fastest).
8. **Verify**: the winner is re-run at 2n-1 and 4n-3 stations (spacing h/2, h/4; same seats).
   Each (load case, check) D/C series, max over the seat samples, gets Richardson extrapolation
   (`optimize::richardson`): converged (steps < 1e-6 relative) = the finest value; monotone and
   contracting with observed order p = log2(d1/d2) in [0.5, 4] = f3 + d2/(2^p - 1); else
   irregular. `dc_extrapolated` = max over the regular series. It is used when every irregular
   series stays below it even at max + 2 x its largest step; then VERIFIED iff
   max(dc_extrapolated, D/C at 4n-3) <= `dc_verify` (never below a computed value). Otherwise
   `dc_extrapolated` is null, `convergence.note` says which series, and VERIFIED iff the 2n-1 D/C
   <= `dc_verify`. The search gate `dc_search` < `dc_verify` keeps its margin.
9. **Shape search** (optional, `src/shapeopt.rs`, docs/SHAPE_SEARCH.md): Latin hypercube + pattern
   search over Michlet Series 7 shape factors within bounds. Each shape runs the wall search above,
   and the best objective wins. Optional manufacturability rules: `draft_step_in`, `length_step_in`,
   `beam_step_in` (snap), `rocker_in = [min, max]` and `min_entry_angle_deg` (reject, with the reason
   in the candidate), `symmetric` (aft factors = fore). Each shape's `shape.metrics` (entry angle,
   rocker in, lateral moment, rejection reason) is in result.json.

## Output: `jaguar-result/1` (JSON)
`{format, params, strength (mix fit + points; `flexural_fit` / `compressive_fit` only when
measured), winner_hull (shape runs) | null, candidates: [...],
winner: {candidate, verify_stations, verify_dc, verified, dc_extrapolated, dc_order, convergence
{stations, dc, extrapolated, order, governing, note}, on_grid_edge, at_mix_data_edge,
mix_provenance, search_space, diagrams, cases, empty, calc} | null}`. `mix_provenance`: the winner density is
`tested` (a tested mix within 0.05 pcf, or the tested mix itself) or `predicted` (fit line only),
the nearest tested mixes below/above it with their signed distance, and `reach_pcf` (distance to the
nearest). `search_space`: per searched variable (thickness, density, depth, then each free
`[shape_search]` factor) the min, max, step and count actually tried, the winner value, the bound it
sits on (`min`/`max`) and `limit` when that bound is real (`buildable floor`, `mix data`,
`tested mixes`). `on_grid_edge` = a wall variable on a bound with no limit; shape bounds are listed,
not flagged. `diagrams` is the governing case at verification resolution. `cases` has one entry
per `[[load_case]]` (input order): name, nominal `seats_in` (`weights_lb` order), `crew_order`
(paddler indices bow to stern), `seat_source` (given/even/auto), `seat_variance_in`,
`verdict_seats_in` (the sample the verdict comes from), weights, draft, trim (nominal seats), GM,
freeboard (worst sample), x, V, M, tension/compression/shear stress, waterline, and
per-station D/C for bending tension, bending compression, shear and biaxial (Kupfer-Gerstle; 0 at
masked tip stations, same formulas as the checks; nominal seats), plus that case's verdict. A case whose equilibrium fails carries `error`
and no numbers. `empty` floats the bare hull (no crew): draft, trim, min freeboard, x and
waterline, or `error` and no numbers. `calc` is the worked calculation of the governing case at
its worst seat sample and governing combination: factors, weights, both equilibria (with KB, BM,
KG), strength fit values, and per check the station, section values (M, V, A, zbar, I, c, Q, b)
or punching intermediates (d_load, depth, b0, P, v_u, v_c) with capacity and D/C; recomputed by
the checks' own code so each demand and capacity equals that case's verdict (or `error`). The terminal prints one line per case under the winner (seats bow to stern as
knee@lb, source, variance, D/C), the mix provenance
line and the search-space table (shape factors inside their bounds are counted, not listed). Each candidate has t, rho, mix, depth, length, beam, shape, weight, GM, drag, race_s and the
per-case verdict (every check's demand, capacity, D/C). Shape runs list one candidate per shape.
Schema: `src/optimize.rs`. Ordering is deterministic (thickness, density, depth ascending), and the
same input gives byte-identical output. The terminal prints a short summary; the JSON is the record.

## Export (`src/export.rs`)
`jaguar export` writes the canoe for CAD and the mold shop, in inches. From a result.json it takes
the winner's hull (generated or `hull.file`), wall thickness, mold depth and flare; from a hull.json
the outer mold surface to `--depth` (add `--thickness` for the solid wall). Same surface as the
analysis grid, both sides mirrored, each station's girth resampled to `--girth` points.
* **STEP** (AP214): a closed `MANIFOLD_SOLID_BREP` of planar triangular faces for a wall, or an
  `OPEN_SHELL` surface model for the mold surface. Verified: opens as one valid solid in
  OpenCASCADE (gmsh 4.15, the kernel FreeCAD uses) with matching volume and bounding box, also
  with air pocket bulkheads (off1 and barge winners).
* **STL** (binary): the same triangles, watertight for a wall.
* The keel line is smooth along x: its height at each table station is fitted below the lowest
  non-zero waterline (parabolic section through the two lowest offsets), then interpolated with a
  monotone cubic, not snapped to waterlines.
* The inner face is the mold line offset inward by t, point for point (mitred), with the offset
  stretched where the hull tapers along x so the wall is t thick normal to the surface (off1 at
  t = 0.375: within 1% over the middle 80% of the length). At the bow and stern, where that offset
  closes, the ends are solid concrete closed by a bulkhead. Its volume matches the analysis wall
  within 0.1% on off1 at t = 0.375 (the analysis thin shell is about 1.6% heavy amidships, the
  solid ends make up the difference).
* **Air pockets**: when the winner has `air_pocket_in` (`[flotation]`), the wall gets a solid
  bulkhead t thick inboard of each sealed pocket, from x0 + l to x0 + l + t and from xn - l - t to
  xn - l (x0, xn the hull's ends, l = `air_pocket_in`), fused into the same single solid so the mold
  shop sees where the foam/sealed volume goes. The bulkheads are not in the analysis weight
  (2 t x interior area, 17 in^3 on the examples/canoe.toml winner). `--no-pockets` leaves them out. A
  pocket shorter than the solid end at the export's station spacing is an error (use more
  `--stations`, or `--no-pockets`).
* A flat bottom (lowest offset at the keel height) keeps its chine corner.
* `GET /api/export?format=step|stl` in the web UI exports the last run's winner (with pockets; stations refined below the pocket length).
* **PDF report** (web UI only, `Export PDF` beside STEP/STL): built in the browser from the last
  result and printed via the print dialog's "Save as PDF" (no server endpoint, no dependency).
  Page 1: snapshot of the 3D canoe top left, weight and verification, winner stats, checks, load
  cases. Then search space, station convergence, mix fit and lab points, every input and every
  chart (all cases shown), then a calculations appendix: each governing equation and the same
  equation with `winner.calc`'s numbers. No per-candidate table: result.json is the full record.
  Each page carries a small "Generated by JAGUAR" footer.

## Race simulator (`src/race.rs`)
`jaguar race` races the winner of a result.json (its hull, `hull_weight_lb`, depth and flare;
`[race]` from the result's params or `--params`) over each `[[race.event]]` and writes
`jaguar-race/1` JSON. Courses are data (`[[race.course]]`: marks at (x, y) m with the side
each is kept on, finish line x = `finish_x_m`); `examples/canoe.toml` has the ASCE 2026 sprint
200/400, slalom 200 and endurance 600 courses and the Symposium and Finals events, with the
geometry the rules leave to the site marked as assumptions.
* **Path**: standing start on x = 0 heading +x, a circular arc around each mark (buoy inside
  the circle, `buoy_clearance_m` in from the arc), common tangents between arcs, straight to the
  finish once heading at it. Free: start y, each arc's radius (>= `min_turn_radius_m`) and centre
  direction; compass search with penalty continuation from deterministic starts. The path keeps
  the lane and every buoy's clearance (worst overshoot <= 1 cm) or the event is an error.
* **Speed**: m (1 + 0.05 added mass) dv/dt = T - D along the path. T = min(n `max_thrust_n`,
  n `propulsive_efficiency` P / v), P = `paddler_power_w` (per event override). D = Michell + ITTC
  resistance at the level draft of hull + crew, plus on arcs the steady-turn cross-flow drag
  (`resistance::turn`), both tabulated. Integrated in distance on v^2 (RK4, 0.25 m).
  The drag table runs to 6 m/s; a faster canoe is an error, not an extrapolation.
* **Output** per event: time, distance, straight and turning distance and time, each segment
  (radius, turn angle, entry / min / exit speed, drift), the path, and the time sensitivities
  S = d ln t / d ln D for straight and turning drag. `weighted_time_s` = sum of event weight x
  time. `recommended`: `straight_weight` = sum w t S_straight / sum w t (S_straight + S_turn),
  `displacements` (same shares grouped by crew), `turn_radius_in` (turn-time weighted radius):
  values to put in `[objective]`.
* `--measured "EVENT=SECONDS"` first fits `paddler_power_w` (bisection) so that event takes the
  measured time. Calibrate before trusting absolute times; the weightings depend on it less.
* Neglected: wind, current, yaw acceleration, stroke pulsation, heel. Drag is the rank-only
  model of the drag objective, so absolute times need the calibration.

## Performance
`examples/canoe.toml` (686 candidates, 161 stations) runs in about 0.2 s on a laptop (release; about 2 CPU-s, mostly drag and the seat corners).
Keep it under 1 s. `examples/shape_opt.toml` must stay under 60 s. `jaguar race` on the
examples/canoe.toml winner (9 events) runs in about 5 s; `examples/shape_race.toml` in about 44 s
(22 threads; mostly the per-shape straight-resistance tables, 24 Michell calls per crew).

## Out of scope (do not build)
Running Michlet/Godzilla, desktop GUI, mix database, multizone pours (declined 2026-10-13),
torsion. Anything else new needs the user's approval first (ideas: docs/PLAN.md).
