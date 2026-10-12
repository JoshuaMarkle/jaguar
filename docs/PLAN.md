# Plan

## Open work (inputs the team has to measure)

- **Knee load test**: load a cast panel of the design wall with a knee-sized block until it fails.
  Punching sets the mix for every hull (docs/PUNCHING.md), so this input moves the answer most.
- **More lab mixes**: the strength fit rests on 3 points. Light mixes that are strong in punching
  (55-60 pcf) would lower weight most (docs/MIX_DATA.md). The ASCE report lists mixes at 50.9, 51.2,
  55.5, 56.2, 70 and 80 pcf that are not in `data/mix_tests.csv`. Get the raw breaks and add them.
- **Measured crew CG** for the GM gate (`stability.crew_cg_in` is 12 in, assumed). The report's
  anthropometric model gives 19.86 in, which fails every hull at `gm_min_in` 0.25. Measure a
  kneeling paddler, then decide if GM stays a gate.
- **ASCE length bounds** for `[shape_search]` from this year's rules.
- **Drag reference**: run Michlet with wave drag on for one hull and compare (docs/SHAPE_SEARCH.md).

## Team task list: status

Source: `data/canoe_optimizer_tasks.md` (from `data/raw/Canoe_optimizer_tasks_to_fix.docx`), checked
against the code on 2026-10-12. Comparison with the legacy report: `data/review_asce_report_and_tasks.md`.

| task | status |
|---|---|
| Export canoe as STEP | **done** (T22 CLI, T23 web buttons) |
| Draft / length / beam increments | **done** (`shape_search.*_step_in`, T30). Not in the web form |
| Depth increments | **done** (`search.depth_in` step) |
| Rocker in inches | **done** (`shape_search.rocker_in`, T30). Not in the web form |
| Flare angle | **done** (`hull.flare_deg`) |
| Min entry angle | **done** (`shape_search.min_entry_angle_deg`, T30). Not in the web form |
| Doubly symmetric | **done** (`shape_search.symmetric`, T30). Not in the web form |
| Buoyancy / self weight distributed | **done** (real submerged area, draft and trim solved) |
| Displacement weighting 350/525/700 | **done** (`objective.displacements`, T31). Not in the web form, no presets |
| Straight 80 / turning 20 | **partial** (T32): turning is a lateral-profile proxy, not a model |
| Fixed-mold structural sweep | **done** (`run` without `[shape_search]`) |
| Mix CSV import, editable table, row delete | **done** (T21) |
| Density vs strength graph with fit | **partial**: split tensile measured, flexural and f'c derived from it |
| Accurate punching shear | **mostly done** (T16: ACI b0*d, round knee, two knees, pad). d is a guessed `d_over_t` |
| Mixes separate from construction layers | open (A1) |
| Race cases 4/3/2 entered separately | **engine done** (`[[load_case]]`), examples use 2M only (A2) |
| Graphs: M and V all cases with toggles | open, governing case only (A3) |
| Graph: utilization along length | **partial**: tension stress only, not every check's D/C (A3) |
| LRFD 1.2 / 1.6 | open (B1) |
| Auto seat solver + variance, manual override | manual done, auto open (B2) |
| Kupfer combined shear + moment | open (B3) |
| Air pockets / swamped flotation | open (B4) |
| Multi-mix zones, I-beam middle, light ends | open, SPEC out of scope (C1) |
| Graph: multi-pour side view | open, needs C1 |
| Race simulator / ML path | open, SPEC out of scope (C3) |

## Roadmap

Ordered by what makes the canoe lighter or the answer more trustworthy. Knee punching governs every
design (D/C ~0.9, bending 0.3-0.56), so bending-only work (Kupfer, seats, LRFD bending) will not
lower weight until punching is relieved. **B** and **C** change physics or scope: each needs the
user's approval and a SPEC update before starting.

### A. Improve what exists (small, no scope change)

- **A1. Construction layers in punching.** `[punching] layers`, `mesh_after_layer`,
  `mold = "male"|"female"` give d, replacing `d_over_t`. A female mold puts the first pass on the
  outside, so the same mesh is shallower from the knee face. Files: `params.rs`, `check.rs`,
  `docs/PUNCHING.md`, examples, web form.
- **A2. ASCE race-case examples.** 2F, 2M, 3F, 3M, 4_coed `[[load_case]]` blocks in
  `examples/canoe.toml` with the team's weights, and regionals / nationals / total displacement
  presets (350/525/700 lb) commented in. Update `docs/HULLS.md`.
- **A3. Along-hull plots for every case.** result.json `diagrams` per load case (not only the
  governing one), with every check's D/C(x). Web: V, M and D/C(x) for all cases overlaid, a toggle
  per case. Files: `optimize.rs` (Diagrams), `web/index.html`.
- **A4. Web form for shape-search and objective fields** already in `Params`: step sizes,
  `rocker_in`, `min_entry_angle_deg`, `symmetric`, `displacements`, `straight_weight`.
- **A5. Record winning seats** (resolved seat x per case) in result.json and the terminal summary,
  so a winning load state can be reproduced.
- **A6. Mix provenance in the report**: per winner density, "tested" or "predicted", with the nearest
  tested mixes and the distance to them (the report's "reach").
- **A7. Search-space table** in the report: range, step, count per variable, and which bound the
  winner sits on (extends `on_grid_edge`).
- **A8. Mix chart for every measured property**: accept optional `flexural_psi` / `compressive_psi`
  columns in the CSV, fit each that has data, plot each against density with its fit line. Derived
  values only where no measurement exists. Files: `mix.rs`, `docs/MIX_DATA.md`, web mix panel.
- **A9. Analysis vs export weight.** The analysis counts zero-breadth waterlines at the rockered
  ends as wall (about 8% heavier than the exported solid on off1). Decide whether to match the
  export, then re-run examples.

### B. Physics additions (need approval)

- **B1. LRFD option.** `material.method = "asd"|"lrfd"`: envelope 1.2D+1.6L and 0.9D+1.6L, phi on
  capacity instead of FS (never both). Expect heavier designs (crew factor ~2.6 vs 1.8 now).
- **B2. Auto seat solver.** Default: per case, seats that minimize the wall that passes, then the
  check enveloped over +/- `seat_variance_in`. Manual `seats_in` overrides. Can move hogging ~40%.
- **B3. Kupfer-Gerstle biaxial check** (shear + bending, plus hoop stress from turning). Matters
  once bending governs.
- **B4. Swamped flotation.** Bow/stern air pocket length (max `air_pocket_max_in`) sized so
  concrete volume + pockets displace >= `swamp_factor` x canoe weight. Gate, and pocket length in
  the result and the STEP export.
- **B5. Sign-aware bending fibre** (tension fibre checked against tension only). Less conservative
  than max(c), so it needs the golden reference updated in step.
- **B6. Richardson extrapolation** of D/C over station refinement, to replace the fixed
  dc_search / dc_verify margin.

### C. Scope changes (SPEC lists these as out of scope)

- **C1. Multi-mix zones.** Horizontal and vertical boundaries only: I-beam (dense flanges, light
  web) over the middle, all light mix at the ends. Separate `mixes` (count) from construction
  layers (A1). Needs a transformed section in `section.rs`, per-zone allowables in `check.rs`, and
  the multi-pour side-view graph. Biggest expected weight win: only the knee zone needs the
  punching mix.
- **C2. Re-sweep a fixed mold for new mixes** with C1: keep the hull and depth, redo wall and zones.
- **C3. Race simulator**: optimal path per course, straight vs turning distance, time and turn
  angle, to replace the 0.8 straight weight. Needs a turning model first.

## How to take a task

Give it an id `T<n>` (next free number after the highest in `git log`), list the files it owns so
parallel agents don't collide, and commit as `T<n>: what`. Done = `make check` green plus a test
that pins the new behavior.
