# Plan

Open work only. Done tasks live in `git log` (`T<n>:` commits). Audited against the code on
2026-10-13: all tests pass, examples re-run (`docs/HULLS.md` current), `canoe.toml` 0.09 s,
`shape_opt.toml` 20 s.

Knee punching governs every design (D/C 0.90, bending 0.3, biaxial 0.32), so bending-only work will
not lower weight until punching is relieved. Order below = what makes the canoe lighter or the
answer more trustworthy first.

## 0. Inputs the team has to measure (no code)

- **Knee load test**: load a cast panel of the design wall with a knee-sized block until it fails.
  Punching sets the mix for every hull (docs/PUNCHING.md), so this input moves the answer most.
- **More lab mixes**: the strength fit rests on 3 points (54.9-60 pcf). The ASCE report lists mixes
  at 50.9, 51.2, 55.5, 56.2, 70 and 80 pcf that are not in `data/mix_tests.csv`; get the raw
  breaks and add them. Light mixes strong in punching (55-60 pcf) would lower weight most.
  Flexural and compressive breaks too, if they exist (unlocks A8).
- **Measured crew CG** (`stability.crew_cg_in` 12 in is assumed). The report's anthropometric 19.86 in
  fails every hull at `gm_min_in` 0.25. Measure a kneeling paddler, then decide if GM stays a gate.
- **Team crew weights** for the ASCE race cases (2F, 2M, 3F, 3M, 4 coed), needed by A2.
- **Mold side and mesh layer** used this year (male/female, mesh after which pass), needed by A1.
- **ASCE length bounds** for `[shape_search]` from this year's rules.
- **Drag reference**: run Michlet with wave drag on for one hull and compare (docs/SHAPE_SEARCH.md).

## A. Improve what exists (no scope change, do in order)

- **A9. Mix chart for every measured property**: optional `flexural_psi` / `compressive_psi`
  columns in the CSV, fit each that has data, plot each vs density with its fit line. Derived values
  only where no measurement exists. Files: `mix.rs`, `docs/MIX_DATA.md`, web mix panel.
- **A10. Analysis vs export weight.** The analysis counts zero-breadth waterlines at the rockered ends
  as wall (about 8% heavier than the exported solid on off1). Decide whether to match the export,
  then re-run examples.
- **A11. Air pockets in the STEP/STL export**: bulkheads at `air_pocket_in` from each tip, so the mold
  shop sees where the foam/sealed volume goes.

## B. Physics additions (need the user's approval and a SPEC update)

- **B2. Auto seat solver.** Per case, seats that minimize the wall that passes, then the check
  enveloped over +/- `seat_variance_in`; every seat ordering of the crew. Manual `seats_in` overrides.
  Can move hogging ~40%. Pays off only after punching is relieved (A1, C1).
- **B6. Richardson extrapolation** of D/C over station refinement, to replace the fixed
  dc_search / dc_verify margin.
- **B7. Hoop stress from turning in the biaxial check** (k = V^2/gR, as in the ASCE report). Needs a
  turn radius input; small effect while bending is at 0.3.
- **B8. Turning model** to replace the lateral-profile proxy behind `straight_weight` (T32). Needed
  before C3.

## C. Scope changes (SPEC lists these as out of scope)

- **C1. Multi-mix zones.** Horizontal and vertical boundaries only: I-beam (dense flanges, light
  web) over the middle, all light mix at the ends. Separate `mixes` (count) from construction layers
  (A1). Needs a transformed section in `section.rs`, per-zone allowables in `check.rs`, and the
  multi-pour side-view graph. Biggest expected weight win: only the knee zone needs the punching mix.
- **C2. Re-sweep a fixed mold for new mixes** with C1: keep the hull and depth, redo wall and zones.
- **C3. Race simulator**: optimal path per course, straight vs turning distance, time and turn angle,
  to replace the 0.8 straight weight. Needs B8 first.

## Team task list: status

Source: `data/canoe_optimizer_tasks.md` (from `data/raw/Canoe_optimizer_tasks_to_fix.docx`).

| task | status |
|---|---|
| Export canoe as STEP | done (T22 CLI, T23 web, T42 wall normal to surface) |
| Draft / length / beam / depth increments, rocker in, min entry angle, symmetric | done (T30, web form T49) |
| Flare angle | done (`hull.flare_deg`) |
| Buoyancy / self weight distributed | done (real submerged area, draft and trim solved) |
| Displacement weighting 350/525/700 | done (T31). Presets in canoe.toml comments and the web form (T49) |
| Straight 80 / turning 20 | partial (T32): turning is a proxy (B8) |
| Fixed-mold structural sweep | done (`run` without `[shape_search]`); mix re-sweep with zones is C2 |
| Mix CSV import, editable table, row delete | done (T21) |
| Density vs strength graph with fit | partial: only split tensile is measured (A9) |
| Accurate punching shear | done (T16, T47: d from casting passes and mold side) |
| Mixes separate from construction layers | layers done (T47); multi-mix open (C1) |
| Race cases 4/3/2 entered separately | done: 2M + 2F on in canoe.toml; 3M/3F/4_coed commented (fail GM at assumed crew CG) |
| Graphs: M, V, D/C(x), all cases with toggles | done (T40, T41, T50 biaxial in D/C(x)) |
| LRFD 1.2 / 1.6 | done (T46) |
| Auto seat solver + variance, manual override | manual done, auto open (B2) |
| Kupfer combined shear + moment | done (T45); turning hoop stress open (B7) |
| Bow/stern air pockets, swamped flotation | done (T43, web form + result + 3D bulkheads T49); export (A11) open |
| Multi-mix zones, I-beam middle, light ends; multi-pour graph | open, out of scope (C1) |
| Race simulator / ML path | open, out of scope (C3) |

## How to take a task

Give it an id `T<n>` (next free number after the highest in `git log`), list the files it owns so
parallel agents don't collide, and commit as `T<n>: what`. Done = `make check` green plus a test
that pins the new behavior.
