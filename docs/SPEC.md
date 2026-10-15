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
jaguar import-mesh canoe.stl|canoe.obj [-o hull.json] [--units ..] [--canoe canoe.toml [--analysis a.toml] [--thickness in] [--density pcf]]
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
`[stability]`, `[objective]`, optional `[shape_search]`, optional `[flotation]`, optional `[turning]`,
optional `[zones]`, `[seats]`,
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
Optional `"stem"`: written by JAGUAR on a prepared hull whose stem radius runs above the table
(radius, each end's [stem x, arc top], the uncut top waterline; Physics 0). Hull files leave it out.
A dense rectangular offset table is what every hull tool (Michlet, CAD section cuts, spreadsheets)
can produce, needs only linear interpolation, and the browser can mesh it directly.

## Physics
0. **Templates** (optional `hull.templates = N`, the constructability simplification): the hull
   is replaced by N cross-section templates, the exact sections at cosine-spaced stations (tips
   included, closer at the ends), with a straight (ruled) loft between neighbours, i.e. the mold a
   hot-wire foam cut or frames with straight battens produces (`Hull::templated`). Fixed hull: the
   table is reduced before the search. Shape search: every generated shape is reduced before it is
   analysed, so the search tunes the buildable loft (not an approximation of a smooth optimum,
   which sits on its gates and falls off them when simplified). The winner hull, export and web
   view are the loft. Topsides follow it with the constant flare (draft everywhere).
   **Doubly symmetric** (optional `hull.doubly_symmetric = true`, applied first): bow and stern
   identical as well as port and starboard, so one end mold makes both ends. A hull file becomes
   the mean of the hull at x and at L - x, on the stations of both (`Hull::symmetrized`; an
   already symmetric hull is unchanged); a shape search ties the stern factors to the bow's
   (`shape_search.symmetric`, the same switch). Templates, stem radius, flare, the gunwale
   outline and the export treat both ends alike, so the built canoe stays symmetric.
   **Stem radius** (`hull.stem_radius_in = r`, default 4 in, 0 = sharp, applied after templates;
   old name `keel_fillet_radius_in` still reads): in side view, the corner where the stem meets
   the keel line at the bow and at the stern (the forefoot) is cut on an arc of radius r tangent to
   the stem (the last station without breadth) and resting on the keel line within 2 r
   (`Hull::filleted`). Each waterline the arc shortens keeps its plan shape at the tip: it is
   shifted aft to the arc, the shift blended out over 4 r with zero slope at the tip, so the entry
   angle is unchanged. The top of the stem, length overall, draft and everything beyond 5 r from the
   ends are unchanged; concrete is only removed; plan view and sections are not rounded. An arc
   taller than the hull table (its top above the design draft; off1's stem foot is at z 4.0, so
   r 4 reaches z 8.0) goes on into the topsides: the prepared hull carries the arcs and the uncut
   top waterline (`stem` in the hull JSON, kept in result.json's `winner_hull`), every topsides
   row up to the arc top is built from the uncut waterline (flared or ruled to the gunwale
   outline, which is fitted to the uncut waterline) and then cut the same way, and the grid gets
   a row every r/8 up the arc. A mold shallower than the arc top fails that candidate with the
   reason (a shape search goes on). Weight, displacement, freeboard, GM, D/C, flotation, drag
   and the export all use the cut hull; the export takes the keel along the arc from the grid
   where the table has no breadth. The table is refined (waterlines every r/8 over each arc,
   stations every r/8 within 5 r of each end) and the STEP/STL export keeps those stations.
   off1 at r 4 vs sharp: 98.32 vs 98.39 lb; the cut takes more displaced volume than concrete off
   the ends, so it floats about 0.01 in lower (freeboard 6.66 vs 6.67 in, GM 1.061 vs 1.063 in),
   drag 32.2 vs 32.0 N, punching D/C unchanged; run 0.34 vs 0.30 s. Shape search
   (shape_opt.toml): 86.8 vs 86.3 lb, 33 s vs 27 s.
1. **Grid**: resample to `hull.stations` evenly spaced x. Topsides above `draft_design` are
   extended to the mold depth D (10 extra waterlines), vertical or leaning out by `hull.flare_deg`.
   Optional constructable gunwale (`hull.gunwale_radius_in` = R, or `shape_search.gunwale_radius_in`
   = [min, max] searched as a shape variable): in top view the gunwale runs straight from the bow
   tip, tangent onto one arc of radius R, then straight to the stern tip (`hull::Gunwale`); each
   side is ruled from the design waterline up to it. The arc peaks at the middle of the widest
   waterline stations; its half-beam is the smallest that clears the design waterline widened by
   (D - draft) tan(flare) wherever it has breadth, so every side leans out at least the flare (the
   demolding draft; checked at the table stations, exact since the outline is concave). Every candidate of a depth fails, with the reason, when no outline of
   radius R fits or the sides would lean out more than `hull.gunwale_max_flare_deg` (default 30)
   anywhere (401 even stations). Off = byte-identical to before.
   **Flare as a search axis** (optional `search.flare_deg = [min, max, step]`, then `hull.flare_deg`
   must be 0): each (depth, flare) is its own topsides; candidates record `flare_deg`, ordered
   (thickness, density, depth, flare). More flare only adds wall, so it is chosen only when a gate
   needs it; the low end is reported as the "demolding draft" limit. Each value multiplies the
   wall grid (a shape search with 6 flares took 120 s). Export and the web view build the same surface.
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
   optional `flexural_psi` / `compressive_psi` columns have data at >= 2 densities (under
   tested_mixes each lab row is its own mix with its own values, never averaged), else flexural = `flex_over_split` * f_sp, f'c = (f_sp/6.7)^2.
   Densities are searched only within `density_extrapolation_pcf` of the tested range of every
   fitted line (split and each measured one; none below the lightest test of a line with a
   negative slope), or only at
   tested mixes with `strength_model = "tested_mixes"`. `material.method = "asd"` (default): service
   loads (crew x `dynamic_load_factor`), every capacity divided by `factor_of_safety`. `"lrfd"`:
   the ASCE 7-22 2.3.1 gravity combinations, each load case re-solved for `lrfd_dead` D +
   `lrfd_live` L and `lrfd_dead_only` D alone (defaults 1.2D + 1.6L and 1.4D; D = self weight,
   L = crew x dynamic factor, impact being part of the live load before factoring, ASCE 7 4.6;
   buoyancy is a fluid load factored like D, balancing the factored weight) and the worse
   combination governs (the removed `lrfd_dead_min`, 0.9D + 1.6L, is refused); capacities x phi (0.60 plain concrete, 0.75
   for `aci_22_6` punching), never both. Freeboard, GM, draft and drag always use the real crew
   weight (no dynamic factor, no load factor): the freeboard gate already holds its margin.
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
   GM_T >= `gm_min_in` for every load case (and seat sample), the empty canoe (no crew) has GM_T >=
   `stability.gm_empty_min_in` when that is given (optional; always computed: candidate `gm_empty_in`,
   per-case `gm_cases_in`, winner `empty.gm_in`), and, with `[flotation]`, the canoe floats swamped
   (`src/flotation.rs`): wall volume + sealed bow and stern air pockets (equal length l from each
   tip, full hull interior up to D = enclosed area minus wall area) displace >= `swamp_factor` x self weight at 62.4 pcf. l is found
   by bisection and reported as `air_pocket_in`; l > `air_pocket_max_in` fails with a reason. Thicknesses below `thickness_min_buildable_in` are
   never tried.
7. **Objective** (`objective.kind`, default `drag` when omitted; drag is computed and reported for every candidate whatever the objective): `weight` = lightest passing; `drag` = lowest Michell wave +
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
   and the best objective wins. `beam_in = [min, max]`: the waterline beam is sampled in it, and a
   wall candidate whose overall beam (flared topsides and gunwale at its depth included) exceeds
   max fails with the reason. Optional manufacturability rules: `draft_step_in`, `length_step_in`,
   `beam_step_in` (snap), `rocker_in = [min, max]` and `min_entry_angle_deg` (reject, with the reason
   in the candidate), `symmetric` (aft factors = fore; same switch as `hull.doubly_symmetric`). Each shape's `shape.metrics` (entry angle,
   rocker in, lateral moment, rejection reason) is in result.json.
10. **Two-mix zones** (optional `[zones]`, `optimize::zoned_search`): the wall search also tries
   walls of two mixes: the light mix (`search.density_pcf`) everywhere except side-view rectangles
   of the dense mix (`dense_pcf`, heavier than the light one), x from the bow and z above the keel,
   each covering the whole girth between its heights. Every edge is a multiple of `increment_in`
   (default 1/8 in; a rectangle may also end at a hull tip). Sections are transformed (light =
   reference, n = E_dense / E_light, E = 33 w^1.5 sqrt(f'c), ACI 318-19 19.2.2.1); self weight and
   its centroid use each mix's density. Bending is checked at each mix's extreme fibres,
   sigma = n M (z - zbar) / I, against that mix's strengths; shell shear at the neutral axis against
   the mix there; Kupfer per mix. Punching uses the dense mix only when every knee's footprint is
   dense over its whole seat range: x = seat -/+ (seat variance + (patch + d)/2), z from the keel up
   to where the hull is (knee spacing / 2 + (patch + d)/2) wide each side; else the light mix.
   Placement (`place_zones`) per (t, depth, flare, light, dense), seats from that wall's single-mix
   plan: start all light; if the light mix fails punching, make every knee footprint dense (top
   rounded up to the increment, plus one increment of margin); then repeat: while a D/C check over
   `dc_search` fails in the light mix, make that fibre dense at that station (0.5 in in from a
   bending fibre, around the neutral axis for shear; a check failing in the dense mix stops it);
   while only GM is short, add a dense keel strip z 0..h centred on midships, 24 in longer per
   round, h up one 0.5 in step once it spans the hull. Freeboard failures stop it (dense only adds
   weight). An all-light wall that passes is the single-mix candidate and is not repeated.
   Combinations go lightest all-light weight first, in batches of 32; one whose all-light weight is
   not below the lightest pass so far cannot win and is not placed; at most `max_checks`
   (default 400) are placed. The winner is the best passing wall of either kind by the objective,
   verified as above; its `calc` is an `error` (the worked calculation covers single-mix walls).
   The result reports the saving against the single-mix pick of the same run. A fixed-mold
   re-sweep with new mixes is `run` on a fixed hull with `[zones]` after updating the mix data.

## Output: `jaguar-result/1` (JSON)
`{format, params, strength (mix fit + points; `flexural_fit` / `compressive_fit` only when
measured), winner_hull (shape runs) | null, candidates: [...],
winner: {candidate, verify_stations, verify_dc, verified, dc_extrapolated, dc_order, convergence
{stations, dc, extrapolated, order, governing, note}, on_grid_edge, at_mix_data_edge,
mix_provenance, dense_mix_provenance, search_space, diagrams, cases, empty, calc} | null, zones}`.
With `[zones]`: a zoned candidate has `zones` {light_pcf, dense_pcf, dense: [{x0_in, x1_in,
z0_in, z1_in}]} (disjoint rectangles; its `density_pcf` is the light mix) and `dense_mix` (tested
mixes); the winner has `dense_mix_provenance`; `search_space` adds `zones.dense_pcf`; the result's
`zones` has combinations (`walls`; with none, `note` says why, e.g. no dense mix heavier than a light one), skipped (`heavier`, `punching`), `checked` (placed), `freeboard` (walls that miss freeboard, which dense concrete cannot fix: skipped when their single-mix wall already misses it, or placed and ending short), `capped`,
`single_mix_lb` (the single-mix pick, null if none passes) and `saved_lb` (single_mix_lb - winner
weight; 0 when the winner is single-mix). Zoned candidates follow the single-mix ones. Zoned D/C(x)
and stresses are the governing mix's; `inertia_in4` is transformed. The terminal lists the
rectangles as mold marks (`60 1/8`) and the saving. `mix_provenance`: the winner density is
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
line and the search-space table (shape factors inside their bounds are counted, not listed). Each candidate has t, rho, mix, depth, flare, length, beam (overall, gunwale included; a shape run's `shape.beam_in` is its waterline beam), shape, weight, GM, drag, race_s and the
per-case verdict (every check's demand, capacity, D/C). Shape runs list one candidate per shape.
Schema: `src/optimize.rs`. Ordering is deterministic (thickness, density, depth ascending), and the
same input gives byte-identical output. The terminal prints a short summary; the JSON is the record.

## CAD import (`src/meshimport.rs`)
`jaguar import-mesh` (and the web form's "Import CAD mesh", `POST /api/import-mesh`) reads a canoe
drawn in CAD, exported as an STL (binary or ASCII) or OBJ mesh, and writes a jaguar-hull/1 table:
units (in/mm/cm/m/ft, detected from a 12-30 ft length or given), axes (length = longest side, up =
shortest, keel at the narrower end; or given), bow at the low end of the length axis (or
`--bow-at-max`). Each of `--stations` x `--waterlines` cuts takes the outer crossing on each side
(their mean; the difference is reported as asymmetry). The table runs from the keel to the lowest
gunwale over the middle 80% (a sheer rising toward the ends is reported, not analysed). A solid
mesh's wall is measured from the inner crossing (normal to the wall; median, min, max over the
middle) and cross-checked by 2 x volume / area; a surface mesh has none. With `--canoe`, it also
writes a canoe.toml that analyses the design as drawn: that hull with no flare, templates, stem
radius, gunwale outline or shape search (the CAD shape is the shape), search pinned to the drawn
wall (or `--thickness`) and depth, and to `--density` if given (else the mix range: the lightest
passing mix). `jaguar run` then gives every check, drag, stability and flotation; a single pinned
design that fails is shown with all its checks (it is the design asked about, not a fallback), and
`jaguar race` races it (with a note). Verified on JAGUAR's own exported solids: wall within 0.02 in,
offsets within 0.15 in, the off1 winner reproduced (weight within 2%, punching D/C within 0.005;
GM within 0.05 in, which is enough to cross off1's GM gate at 0.25).

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
  bulkhead one casting layer thick inboard of each sealed pocket: b = t / `punching.layers` (2 when
  no layer fields are given; t with an explicit `d_over_t`, which gives no pass count), so a 3/8 in
  wall cast in 3 layers gets 1/8 in bulkheads. They run from x0 + l to x0 + l + b and from
  xn - l - b to xn - l (x0, xn the hull's ends, l = `air_pocket_in`). A top cover as thick closes
  each pocket at the mold depth (the interior from D - b up, from the solid end to the bulkhead),
  so each pocket is a sealed void: one solid with an inner shell per pocket (STEP
  `BREP_WITH_VOIDS`; STL simply has the extra shells), so the mold shop sees where the foam/sealed
  volume goes. Bulkheads and covers are not in the analysis weight (10 in^3 on the
  examples/canoe.toml winner, 2 layers). `--no-pockets` leaves them out. A
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
  (radius, turn angle, entry / min / exit speed, drift), the path, speed along it, and the time sensitivities
  S = d ln t / d ln D for straight and turning drag. `weighted_time_s` = sum of event weight x
  time. `recommended`: `straight_weight` = sum w t S_straight / sum w t (S_straight + S_turn),
  `displacements` (same shares grouped by crew), `turn_radius_in` (turn-time weighted radius):
  values to put in `[objective]`.
* Courses in examples/canoe.toml follow the ASCE figures in `races/` (Fig. 3 slalom, Fig. 4
  endurance slalom, Fig. 5 sprint; lane 5, its buoy line y = 0). Endurance: the lane-5 slalom,
  buoy 8 on the lane line at 100 m kept to starboard, the three-buoy open-water loop, buoy 12 on
  the 100 m line 105 m across from buoy 8 (kept to starboard), then 100 m to the line. A course may set its own
  `lane_width_m` (>= the race lane) for open water, e.g. the endurance loop; the start stays in the
  race lane. Each event's `speed_m` is (distance, speed, time) about every metre.
* Web UI: the Race tab races the last winner with the form's `[race]` (`POST /api/race`, or
  `result.race` after a race-objective run) and plays it back: the course at equal scale (lane,
  start/finish line, buoys with clearance rings, the one being rounded highlighted), the winner's
  top-view outline moving along its path at the simulated speed and yawed by the turn model's drift
  in arcs, path covered so far; controls play/pause (space), restart, viewing speed 0.25-50x, a
  time scrubber and follow (40 m window on the canoe); live time, speed, distance and current
  turn; speed along the path with the canoe's cursor; the event table and recommended weightings.
  `[race]` has no form fields: the server always uses the CLI canoe.toml's, read fresh on each
  request (also for runs and saved prefs), so edit it there.
* `--measured "EVENT=SECONDS"` first fits `paddler_power_w` (bisection) so that event takes the
  measured time. Calibrate before trusting absolute times; the weightings depend on it less.
* Neglected: wind, current, yaw acceleration, stroke pulsation, heel. Drag is the rank-only
  model of the drag objective, so absolute times need the calibration.

## Performance
`examples/canoe.toml` (686 candidates, 161 stations) runs in about 0.5 s on a laptop (release; drag of every candidate and the seat corners).
In a shape search only the reported candidates get their drag (each shape's verified winner, or its
best failing candidate) unless drag is the objective; `[zones]` skips walls whose single-mix wall
misses freeboard and checks GM by ballast on a quick float before a full check.
Keep it under 1 s. `examples/shape_opt.toml` must stay under 60 s. `jaguar race` on the
examples/canoe.toml winner (9 events) runs in about 5 s; `examples/shape_race.toml` in about 44 s
(22 threads; mostly the per-shape straight-resistance tables, 24 Michell calls per crew).

## Out of scope (do not build)
Running Michlet/Godzilla, desktop GUI, mix database, more than two mixes, torsion. Anything else new needs the user's approval first (ideas: docs/PLAN.md).
