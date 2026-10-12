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
`[stability]`, `[objective]`, optional `[shape_search]`. Paths are relative to the toml.
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
2. **Section** per station: thin shell of thickness t on both sides + keel strip: A, zbar, I.
3. **Equilibrium**: self weight w = A*rho/1728 lb/in. Crew = weight * `dynamic_load_factor`, each
   paddler on a `knee_patch_in` patch at their seat (default seats L*(i+1)/(n+1)). Draft and trim
   are solved by bracketing so fresh-water buoyancy (62.4 pcf) balances force and moment, then
   q -> V -> M by integration. Free-free closure: |V(L)| <= 1e-3 W, |M(L)| <= 1e-3 W L.
   Also gives loaded freeboard (D minus local draft, min over stations) and transverse GM_T
   (crew CG at `stability.crew_cg_in`).
4. **Material** (`src/mix.rs`, docs/MIX_DATA.md): least-squares line of FINAL split tensile strength
   vs density from the lab CSV. Flexural = `flex_over_split` * f_sp, f'c = (f_sp/6.7)^2.
   Densities are searched only within `density_extrapolation_pcf` of the tested range, or only at
   tested mixes with `strength_model = "tested_mixes"`. Every capacity is divided by
   `factor_of_safety`.
5. **Checks** (`src/check.rs`; governing D/C = max):
   * bending tension: |M| c / I with c = max(zbar, D - zbar), vs flexural strength
     (stations with I > 1e-3 max I)
   * bending compression vs 0.85 f'c
   * shell shear VQ/(I*2t) vs 2 sqrt(f'c)
   * knee punching for the heaviest paddler, ACI 318-19 Table 22.6.5.2 or plain Table 14.5.5.1,
     one or two knees (docs/PUNCHING.md)
6. **Gates**: a candidate passes iff D/C <= `dc_search`, freeboard >= `freeboard_min_in` and
   GM_T >= `gm_min_in` for every load case. Thicknesses below `thickness_min_buildable_in` are
   never tried.
7. **Objective** (`objective.kind`, default `drag` when omitted): `weight` = lightest passing; `drag` = lowest Michell wave +
   ITTC-57 friction resistance at `speed_mps` (`src/resistance.rs`; use it to rank shapes, not as a
   race prediction).
8. **Verify**: the winner is re-run at 2n-1 stations. It is VERIFIED iff D/C there <= `dc_verify`
   (D/C drifts up 1-2% per station doubling, hence dc_search < dc_verify).
9. **Shape search** (optional, `src/shapeopt.rs`, docs/SHAPE_SEARCH.md): Latin hypercube + pattern
   search over Michlet Series 7 shape factors within bounds. Each shape runs the wall search above,
   and the best objective wins.

## Output: `jaguar-result/1` (JSON)
`{format, params, strength (mix fit + points), winner_hull (shape runs) | null, candidates: [...],
winner: {candidate, verify_stations, verify_dc, verified, on_grid_edge, at_mix_data_edge, diagrams}
| null}`. Each candidate has t, rho, mix, depth, length, beam, shape, weight, GM, drag and the
per-case verdict (every check's demand, capacity, D/C). Shape runs list one candidate per shape.
Schema: `src/optimize.rs`. Ordering is deterministic (thickness, density, depth ascending), and the
same input gives byte-identical output. The terminal prints a short summary; the JSON is the record.

## Performance
`examples/canoe.toml` (686 candidates, 161 stations) runs in about 0.1 s on a laptop (release).
Keep it under 1 s. `examples/shape_opt.toml` must stay under 60 s.

## Out of scope (do not build)
Race simulation, running Michlet/Godzilla, desktop GUI, mix database, PDF reports, multizone pours,
torsion, STEP/STL export. Anything else new needs the user's approval first (ideas: docs/PLAN.md).
