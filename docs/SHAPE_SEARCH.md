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
`shape.metrics` (`entry_angle_deg`, `rocker_bow_in`, `rocker_stern_in`, `lateral_moment_in4`,
`rejected`). `flare_deg` stays a fixed `[hull]` value, not a searched variable (it changes the grid
above the waterline, not the generator, so searching it would need a second outer axis).

## Drag objective options (`[objective]`)

* `displacements = [{ crew_lb, weight }, ...]`: drag = sum of weight x resistance at the level draft
  where hull weight + crew_lb floats (bisection on displaced volume, no trim). Weights sum to 1.
  Absent = heaviest load case at its solved draft (old behaviour). Presets in
  `examples/shape_opt.toml`: Regionals 350/700 at 75/25, Nationals 350/525/700 at 60/20/20,
  Total 67.5/10/22.5.
* `straight_weight` (default 1.0): score = `sw * drag / drag_ref + (1 - sw) * turn / turn_ref`.
  `turn` is a **proxy, not a turning model**: the second moment of the underwater lateral profile
  about midships at design draft, integral of (draft - keel_z(x)) (x - L/2)^2 dx. More rocker
  lifts the keel ends and shrinks it, so rocker is credited automatically. Refs are the drag and
  moment of the fixed midpoint-of-bounds shape (deterministic, independent of sampling).
  Lower = easier to turn by this proxy only; do not read it as a race prediction.

## Algorithm (deterministic)

1. **Latin hypercube** of `samples` shapes inside the bounds (seeded SplitMix64). Parameters with
   min == max are held fixed.
2. Shapes are evaluated in parallel. Each inner search runs on one thread
   (`optimize::run_inner(.., parallel = false, ..)`) to avoid nested parallelism.
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
| 1.5 | 2.6 | 12.3 |
| 2.5 | 10.7 | 31.3 |
| 3.5 | 33.6 | 58.0 |

* Wave drag (Michell integral) is grid-converged: within 1% of a 4x finer angle grid. There is no
  external reference yet: the Michlet `out.mlt` for this hull was run without wave drag.
* Wetted surface is 3.52 m^2 vs Michlet's 3.19 m^2 (+10%), so friction is about 10% high
  (conservative).
* Use drag to **rank** shapes (every shape gets the same model), not as an absolute race
  prediction. To validate it, run Michlet with wave drag on for one hull and compare.
