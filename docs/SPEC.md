# JAGUAR spec (v1)

## Question answered
Given a hull shape, a crew, and a concrete strength fit: **what is the lightest uniform wall
(thickness t, mix density rho, mold depth D) that passes every gate for every load case?**

## Interface
```
jaguar check  [canoe.toml]                     validate inputs, print hull summary
jaguar run    [canoe.toml] [-o result.json]    optimize; exit 0 verified, 2 no verified design, 1 error
jaguar import-michlet off1.mlt out.mlt [-o hull.json]
jaguar serve  [result.json] [-p 8000]          optional read-only browser view
```

## Inputs

### `canoe.toml` (parameters)
See `examples/canoe.toml`, which is the canonical, commented example. Schema = `src/params.rs`.
Sections: `[hull]`, one or more `[[load_case]]`, `[material]`, `[search]`, `[gates]`. Unknown keys are
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
4. **Check**: sigma = |M| c / I, c = max(zbar, D - zbar), stations with I > 1e-3 max I.
   capacity = (slope*rho + intercept)/FS. D/C = max sigma / capacity. Freeboard = D - local draft,
   min over stations.
5. **Gates**: candidate passes iff D/C <= dc_search and freeboard >= freeboard_min_in, for every load
   case. Winner = lightest passing. Verified iff its D/C at 2n-1 stations <= dc_verify.

## Output: `jaguar-result/1` (JSON)
`{format, params, candidates: [...], winner: {candidate, verify_stations, verify_dc, verified,
on_grid_edge} | null}`. Schema = `src/optimize.rs`. Deterministic ordering (thickness, density,
depth ascending). Terminal output is a short summary; the JSON is the record.

## Performance target
Default example (4 t x 11 rho x 5 D = 220 candidates, 161 stations) in **< 1 s** release build on a
laptop. The Python prototype took ~2.5 s for 44 candidates at fixed depth.

## Out of scope for v1 (do not build)
Race simulation, wave resistance, Godzilla/Fortran runs, GUI, mix database, PDF/LaTeX reports,
multizone pours, Kupfer biaxial envelope, torsion, punching shear, stability GM_T, seating search,
STEP/STL export. Candidates for v2 are listed in `docs/PLAN.md`; each needs the user's approval.
