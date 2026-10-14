# Hull shape search

With `[shape_search]` in canoe.toml (and no `hull.file`), JAGUAR picks the hull shape as well as
the wall. Example: `examples/shape_opt.toml`. Shape factors are documented in `src/hullgen.rs`.

## Two nested searches

```
outer (shapeopt.rs): shape s inside the [shape_search] bounds   ~100-400 shapes
  hull = hullgen::generate(s, 41 stations, 21 waterlines)
  inner (optimize.rs): wall grid t x rho x depth               ~700 candidates, ~0.1 s
  score(s) = objective of the inner winner (+inf if nothing passes)
```

The outer loop only changes the hull it hands to the inner search, so all strength, freeboard and
stability logic lives in one place.

## Why the gates matter here

* With the **weight** objective alone the optimizer would shrink the hull: shorter and narrower
  means less wall. Freeboard (the crew must float with 6 in to spare), the strength checks (a short
  hull concentrates the crew) and the **stability gate** GM_T >= `stability.gm_min_in` stop it.
  Without the GM gate a "canoe" 10 in wide wins.
* The **drag** objective (`objective.kind = "drag"`) rewards long and slender, held in check by
  the same gates. Weight still matters through draft and wetted surface.
* `[shape_search]` bounds encode the ASCE rules and mold limits (e.g. length 18-22 ft).

## Manufacturability (`[shape_search]`, all optional)

| key | effect |
|---|---|
| `draft_step_in`, `length_step_in`, `beam_step_in` | sampled values snap to the nearest multiple inside the bounds (depth already steps by `search.depth_in`) |
| `symmetric = true` | `aft_wp`, `aft_rocker`, `aft_len_frac` follow the fore values (3 fewer dimensions, aft bounds ignored) |
| `rocker_in = [min, max]` | keel rise above the lowest keel point at 5% L from the bow and from the stern, in. Outside = **rejected** (not remapped) |
| `min_entry_angle_deg` | design-waterline entry half-angle = atan(half-breadth / x) of the chord over the first 5% L. Finer = **rejected** |

A rejected shape keeps its inner result in `candidates` with `passes = false` and
`error = "shape rejected: ..."`, scores +inf, and can never be the winner. Every shape carries
`shape.metrics` (`entry_angle_deg`, `rocker_bow_in`, `rocker_stern_in`, `lateral_moment_in4` (the
old turning proxy), `turn`, `rejected`). `flare_deg` stays a fixed `[hull]` value, not a searched variable (it changes the grid
above the waterline, not the generator, so searching it would need a second outer axis).

## Drag objective options (`[objective]`)

* `displacements = [{ crew_lb, weight }, ...]`: drag = sum of weight x resistance at the level draft
  where hull weight + crew_lb floats (bisection on displaced volume, no trim). Weights sum to 1.
  Absent = heaviest load case at its solved draft (old behaviour). Presets in
  `examples/shape_opt.toml`: Regionals 350/700 at 75/25, Nationals 350/525/700 at 60/20/20,
  Total 67.5/10/22.5.
* `straight_weight` (default 1.0): score = `sw * drag / drag_ref + (1 - sw) * turn / turn_ref`.
  `turn` is the **steady-turn drag** at radius `turn_radius_in` (default 600 in = 50 ft) and
  `speed_mps`, from a cross-flow (strip) model (Allen-Perkins cross-flow analogy, as in ship
  manoeuvring models; Hoerner for Cd). Each strip of the underwater lateral profile T(x) (draft
  minus keel height, so rocker counts) moves sideways at w(x) = U (x/R - beta), x from midships
  toward the bow, and carries f = 0.5 rho Cd T |w| w, Cd = 1.0. The drift beta is solved so the
  net lateral force is the centripetal force m U^2 / R (m = hull + crew). Then
  `turn = integral 0.5 rho Cd T |w|^3 dx / U`, the power spent holding the turn divided by speed
  (= steering moment / R + induced drag beta m U^2 / R). It is evaluated like the drag: at the level
  draft of the inner winner's hull weight plus each `displacements` crew (weighted), else the
  heaviest load case. U cancels from beta, so the ranking does not depend on `speed_mps`.
  Refs are the straight drag and turn drag of the fixed midpoint-of-bounds shape at its design
  draft (deterministic, independent of sampling). Neglected: the linear lift term, added mass,
  waves, heel. Use it to rank shapes; `shape.metrics.turn` gives `drag_n`, `moment_nm` (yaw moment
  the paddlers hold) and `drift_deg`.
* `kind = "race"` (needs `[race]`, src/race.rs): score = weighted race time over the `[race]`
  events, so wall weight, displacement, straight and turning drag are weighed by how they move
  the clock instead of by `straight_weight` / `displacements` (`jaguar race` prints the
  equivalent weights). Paths are planned once, on the midpoint-of-bounds shape with its inner
  winner's wall (or its lowest-D/C candidate's), then held: each shape's inner winner is timed on
  them (`race::planned_time`, exact turn drag at the planned radii). A path is optimal for the
  reference, so the time on it is off by second order for another shape (< 0.2% for a 40 lb
  heavier wall, tested). The winner is raced again on paths re-planned for it (`result.race`,
  never slower than its `race_s`). A shape whose canoe cannot run a race (sinks with a crew)
  fails with the reason. `examples/shape_race.toml`: about 44 s on 22 threads.

## Algorithm (deterministic)

1. **Latin hypercube** of `samples` shapes inside the bounds (seeded SplitMix64). Parameters with
   min == max are held fixed.
2. Shapes are evaluated in parallel on plain threads, each owning a private 1-thread rayon pool
   for its inner searches (not `par_iter` + `install`: a blocked rayon worker steals other shapes
   and nests their stacks until a debug build overflows).
3. **Pattern search**, `refine_rounds` rounds: around the best 4 shapes, step +/- on each free
   parameter (step starts at range/8 and halves after a round without improvement), clamped to
   the bounds, skipping shapes already seen.
4. The best shape is re-run with the normal parallel inner search, so the winner is verified at
   2n-1 stations. `winner_hull` in result.json is the generated hull.
5. `candidates` holds one entry per shape (its inner winner, or its best failing candidate with
   the reason), so the web UI scatter shows one dot per shape.

`tests/shapeopt.rs` pins determinism and bounds. `docs/HULLS.md` has the current result.

## Drag model accuracy

Measured on the off1 hull at design draft:

| speed m/s | wave N | friction N |
|---|---|---|
| 1.5 | 2.6 | 11.1 |
| 2.5 | 10.7 | 28.3 |
| 3.5 | 33.6 | 52.5 |

* Wave drag (Michell integral) is grid-converged: within 1% of a 4x finer angle grid. There is no
  external reference yet: the Michlet `out.mlt` for this hull was run without wave drag.
* Wetted surface is 3.18 m^2 vs Michlet's 3.19 m^2.
* Use drag to **rank** shapes (every shape gets the same model), not as an absolute race
  prediction. To validate it, run Michlet with wave drag on for one hull and compare.
