# Hull shape search

With `[shape_search]` in canoe.toml (and no `hull.file`), JAGUAR picks the hull SHAPE as well as the wall
(thickness, mix density, mold depth). The user is a programmer, not a hull designer, so the
search must be bounded by things that make a canoe physically sensible, and every number it
reports must come from a check, never a guess.

## Structure: two nested searches

```
outer (shapeopt.rs): shape s in [shape_search] bounds         ~ 100-400 shapes
  hull = hullgen::generate(s, 41 stations, 21 waterlines)
  inner (optimize.rs, unchanged): wall grid t x rho x depth   ~ 700 candidates, ~0.1 s
  score(s) = objective of the inner winner (+inf if nothing passes)
```

The inner search already exists and is fast and verified. The outer loop only changes the
hull it is given, so all strength/freeboard logic stays in one place.

## Why gates are needed (or the optimizer cheats)

* **Weight objective alone** shrinks the hull: shorter and narrower = less wall. It is stopped
  by real constraints: freeboard (the crew must float with 6 in to spare), the strength checks
  (a short hull concentrates the crew), and a NEW **stability gate**: loaded transverse
  metacentric height GM_T >= `stability.gm_min_in`. Without GM a "canoe" 10 in wide wins.
* **Drag objective** (`objective.kind = "drag"`): lowest total water resistance at race speed
  (Michell wave resistance + ITTC-57 friction, loaded draft). This rewards long and slender,
  balanced by the same gates. This is what a racing team actually wants once the hull is
  strong enough; weight still matters through draft and wetted surface.
* Bounds in `[shape_search]` encode the ASCE rules / mold limits (e.g. length 18-22 ft).

## Outer search algorithm (deterministic)

1. **Latin hypercube** of `samples` shapes inside the bounds (seeded PRNG implemented in the
   file, e.g. SplitMix64; no new crates). Parameters with min == max are held fixed.
2. Evaluate all in parallel (rayon over shapes; the inner search runs serially per shape,
   `optimize::run_inner(.., parallel = false, ..)`, to avoid nested parallelism thrash).
3. **Pattern search refinement**, `refine_rounds` rounds: around the current best K=4 shapes,
   try +/- step on each free parameter (step starts at 1/8 of the range, halves when no
   improvement), clamp to bounds, skip already-seen shapes.
4. Winner = best score; its full inner RunResult (with verification at 2n-1 stations) is the
   run result, with `candidate.shape = Some(s)`, `winner_hull` = generated hull.
5. `RunResult.candidates` = for each shape, its inner winner (or its best failing candidate
   with passes=false and the reason), so the web scatter shows one dot per shape.

Reported per winner: length, beam, draft, all shape factors, weight, drag, GM, every check.

## Acceptance (pinned by `tests/shapeopt.rs` and `docs/HULLS.md`)

* `jaguar run examples/shape_opt.toml` finishes in < 60 s release, returns a VERIFIED winner whose
  shape is inside the bounds and lighter (weight objective) than every fixed example hull run
  with the same settings, with GM >= gm_min and freeboard >= 6 in.
* Same toml twice -> byte-identical result.json.
* drag objective winner is longer/slenderer than the weight objective winner.

## Drag model status (measured 2026-10-12, off1 hull at design draft)

| speed m/s | wave N (Rust) | wave N (old Python) | friction N (Rust) |
|---|---|---|---|
| 1.5 | 2.6 | 3.8 | 12.3 |
| 2.5 | 10.7 | 14.5 | 31.3 |
| 3.5 | 33.6 | 33.5 | 58.0 |

* Wave: Rust is grid-converged (<1% vs a 4x finer theta grid). The old Python kernel undersamples
  short transverse waves at low speed, which is where the two differ. Michlet's own out.mlt has
  Rw = 0 (wave drag was not computed in that run), so there is no external reference yet.
* Wetted surface: Rust 3.52 m^2 vs Michlet's S = 3.19 m^2 (+10%), so friction is ~10% high
  and conservative. The old Python's 1.84 m^2 ignores girth slope and is not a valid reference.
* Use drag to RANK shapes (all shapes get the same model), not as an absolute race prediction.
  To validate: run Michlet with wave drag on for one hull and compare.
