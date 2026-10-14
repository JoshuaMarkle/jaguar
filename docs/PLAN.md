# Plan

Open work only. Done tasks live in `git log` (`T<n>:` commits). Audited against the code on
2026-10-13: all tests pass, examples re-run (`docs/HULLS.md` current), `canoe.toml` 0.2 s,
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
- **Race times for calibration**: a timed 200 m sprint (crew weights noted) to fit
  `race.paddler_power_w` with `jaguar race --measured "EVENT=SECONDS"`; a timed endurance race
  for its own power. Until then race times are model numbers (the weightings move less).
- **Course drawings** (ASCE Exhibit 8, figures 3-5): first slalom buoy distance, 180 deg turn
  direction, endurance leg layout. `examples/canoe.toml` `[race]` marks these as assumptions.

## A. Improve what exists (no scope change, do in order)

- **A12. CPU cost.** Auto seats + seat variance (T57) and 4n-3 verify (T58) raised `canoe.toml` to
  ~3 CPU-s (0.23 s wall on 20 cores; budget < 1 s on a laptop) and `shape_opt.toml` to ~257 CPU-s.
  Cut it in `seats.rs` / `equilibrium.rs` with exactness-preserving changes (byte-identical results).


## B. Physics additions (need the user's approval and a SPEC update)

- **B6b. Revisit `dc_search`.** With Richardson verify (T58) the search margin 0.93 vs 0.95 is
  conservative (bending drifts down with stations). Raising it needs a check that winners with
  bending governing still verify; punching governs every example today, so it would not move them.

## C. Race simulator (approved 2026-10-13)

T61 (`src/race.rs`, `jaguar race`) is done: optimal path per course (deterministic compass search,
chosen over ML: same physics, reproducible, and an ML policy can only approach the optimum of the
model it is trained on), straight vs turning distance and time, turn angles and speeds, and the
recommended `straight_weight` / `displacements` / `turn_radius_in` from time sensitivities.
On the off1 winner with the placeholder crews it recommends straight_weight 0.73.

T62 (`objective.kind = "race"`) is done: the shape search minimises weighted race time on paths
planned for the midpoint shape, the winner re-raced on its own (`examples/shape_race.toml`,
44 s on 22 threads; docs/HULLS.md). On a laptop with fewer cores it will be slower: if it matters,
share straight-resistance tables between crews of similar weight (draft interpolation).
T63 (web Race tab, `POST /api/race`) is done. Open: editing `[race]` courses and events in the
form (today they come from canoe.toml).

T65 (constructable line-arc-line gunwale, `hull.gunwale_radius_in` / `shape_search.gunwale_radius_in`,
side lean limit `hull.gunwale_max_flare_deg`) is done. On full-ended hulls such as off1 no buildable
radius exists at 30 deg; the team should confirm the lean limit the mold shop can build.

Declined 2026-10-13 (do not build): multi-mix zones / I-beam pours, multi-pour graph, mix re-sweep
with zones.

## Team task list: status

Source: `data/canoe_optimizer_tasks.md` (from `data/raw/Canoe_optimizer_tasks_to_fix.docx`).

| task | status |
|---|---|
| Export canoe as STEP | done (T22 CLI, T23 web, T42 wall normal to surface) |
| Export PDF report (canoe, stats, inputs, charts) | done (web UI, browser print) |
| Draft / length / beam / depth increments, rocker in, min entry angle, symmetric | done (T30, web form T49) |
| Flare angle | done (`hull.flare_deg`) |
| Buoyancy / self weight distributed | done (real submerged area, draft and trim solved) |
| Displacement weighting 350/525/700 | done (T31). Presets in canoe.toml comments and the web form (T49) |
| Straight 80 / turning 20 | done (T32, T56); `jaguar race` recommends the value (T61) |
| Fixed-mold structural sweep | done (`run` without `[shape_search]`); zone re-sweep declined |
| Mix CSV import, editable table, row delete | done (T21) |
| Density vs strength graph with fit | done (T54): split, flexural, f'c; measured fits solid, derived dashed |
| Accurate punching shear | done (T16, T47: d from casting passes and mold side) |
| Mixes separate from construction layers | layers done (T47); multi-mix declined |
| Race cases 4/3/2 entered separately | done: 2M + 2F on in canoe.toml; 3M/3F/4_coed commented (fail GM at assumed crew CG) |
| Graphs: M, V, D/C(x), all cases with toggles | done (T40, T41, T50 biaxial in D/C(x)) |
| LRFD 1.2 / 1.6 | done (T46; factors are inputs since T64) |
| Auto seat solver + variance, manual override | done (T57, `[seats]`) |
| Kupfer combined shear + moment | done (T45); turning hoop stress `[turning]` (T59) |
| Bow/stern air pockets, swamped flotation | done (T43, web form + result + 3D bulkheads T49); STEP/STL bulkheads (T52) |
| Multi-mix zones, I-beam middle, light ends; multi-pour graph | declined 2026-10-13 |
| Race simulator, optimal path, straight vs turn metrics | done (T61 `jaguar race`, T62 race objective, T63 web tab) |
| LRFD factors as inputs (1.2 / 1.6 blanks) | done (T64: `material.lrfd_dead`, `lrfd_dead_min`, `lrfd_live`) |

## How to take a task

Give it an id `T<n>` (next free number after the highest in `git log`), list the files it owns so
parallel agents don't collide, and commit as `T<n>: what`. Done = `make check` green plus a test
that pins the new behavior.
