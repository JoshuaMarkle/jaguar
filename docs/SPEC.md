# JAGUAR spec (v1)

## Question answered
Given a hull shape, a crew, and a concrete strength fit: **what is the lightest uniform wall
(thickness t, mix density rho, mold depth D) that passes every gate for every load case?**

## Interface
```
jaguar check  [canoe.toml]                     validate inputs, print hull summary
jaguar run    [canoe.toml] [-o result.json]    optimize; exit 0 verified, 2 no verified design, 1 error
jaguar import-michlet off1.mlt out.mlt [-o hull.json]
jaguar gen-hull shape.toml [-o hull.json]       parametric hull (Michlet Series 7), see docs/HULLS.md
jaguar mix    [data/mix_tests.csv]             print lab points and the strength fit (docs/MIX_DATA.md)
jaguar serve  [canoe.toml] [-p 8000]           optional web UI: 3D canoe, edit params, run + watch live
```

## Inputs

### `canoe.toml` (parameters)
See `examples/canoe.toml`, which is the canonical, commented example. Schema = `src/params.rs`.
Sections: `[hull]`, one or more `[[load_case]]`, `[material]`, `[search]`, `[gates]`, `[stability]`,
`[objective]`, optional `[shape_search]`. Unknown keys are
errors. Why TOML: human-editable, comments for provenance, strict typed parse, no code execution.

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
Why this shape: a dense rectangular offset table is what every hull tool (Michlet/Godzilla, CAD
section cuts, spreadsheets) can produce; the optimizer only needs linear interpolation on it, so
resampling to any station count is O(n) with no geometry library; the browser can mesh it directly.
Topsides above `draft_design` are extruded vertically to the mold depth, which is a search variable.

## Physics (v1, matches the prototype)
1. **Grid**: resample to `stations` evenly spaced x; append 10 waterlines from draft+0.1 to depth,
   half-breadth = top waterline value.
2. **Section** per station: thin shell of thickness t on both sides + keel strip: A, zbar, I.
3. **Equilibrium**: self weight w = A*rho/1728 lb/in; crew = weight*dynamic_load_factor on a
   `knee_patch_in` patch at each seat (default seats: evenly spaced L*(i+1)/(n+1)). Solve midship draft
   and trim (brentq-style bracketing) so buoyancy (fresh water 62.4 pcf) balances force and moment.
   Integrate q -> V -> M (trapezoid). Free-free closure: |V(L)| <= 1e-3 W, |M(L)| <= 1e-3 W L.
4. **Checks** (all must pass; governing D/C = max; details in `src/check.rs`):
   bending tension (below), bending compression vs 0.85 f'c/FS, shell shear VQ/(I*2t) vs
   2 lambda sqrt(f'c)/FS, and knee punching shear for the heaviest paddler, ACI 318-19 Table 22.6.5.2
   (demand on b0*d, round knee, one or two knees) or plain Table 14.5.5.1; see docs/PUNCHING.md.
   **Material** (`src/mix.rs`, `docs/MIX_DATA.md`): least-squares fit of FINAL split tensile
   strength vs density from `material.mix_data` (lab rows scaled by 1/fraction_of_final).
   Flexural = flex_over_split * f_sp; f'c = (f_sp/6.7)^2 (ACI f_sp = 6.7 sqrt f'c). Densities are
   searched only within `density_extrapolation_pcf` of the tested range.
   Bending tension: sigma = |M| c / I, c = max(zbar, D - zbar), stations with I > 1e-3 max I.
   capacity = flexural/FS. D/C = max sigma / capacity. Freeboard = D - local draft,
   min over stations.
5. **Stability**: loaded transverse metacentric height GM_T (crew CG at `stability.crew_cg_in`)
   must be >= `stability.gm_min_in`.
6. **Gates**: candidate passes iff D/C <= dc_search, freeboard >= freeboard_min_in and
   GM_T >= gm_min_in, for every load case. **Objective** `objective.kind`: `weight` = lightest
   passing; `drag` = lowest wave (Michell) + friction (ITTC-57) resistance at `speed_mps`. Verified iff its D/C at 2n-1 stations <= dc_verify.
7. **Shape search** (optional `[shape_search]`, `src/shapeopt.rs`, `docs/SHAPE_PLAN.md`): outer
   deterministic Latin hypercube + pattern search over Michlet Series 7 shape factors within
   bounds; each shape runs the wall search above; best objective wins.

## Output: `jaguar-result/1` (JSON)
`{format, params, strength (mix fit + points), winner_hull (generated hull, shape runs) | null,
candidates: [...], winner: {candidate, verify_stations, verify_dc, verified, on_grid_edge,
at_mix_data_edge} | null}`. Each candidate carries `length_in, beam_in, shape | null, gm_in | null,
drag_n | null` besides t, rho, depth, weight, verdict. In shape runs there is one candidate per shape. Schema = `src/optimize.rs`. Deterministic ordering (thickness, density,
depth ascending). Terminal output is a short summary; the JSON is the record.

## Performance target
Default example (9 t x 11 rho x 7 D = 693 candidates, 161 stations) in **< 1 s** release build on a
laptop. The Python prototype takes ~30 s for the same 693 candidates.

## Out of scope for v1 (do not build)
Race simulation, Godzilla/Fortran runs, GUI, mix database (one CSV is enough), PDF/LaTeX reports,
multizone pours, Kupfer biaxial envelope, torsion, seating search,
STEP/STL export. Candidates for v2 are listed in `docs/PLAN.md`; each needs the user's approval.
