# Rewrite plan

Each task lists the files it owns. Tasks in the same wave touch disjoint files and can run as
parallel agents. Done = `make check` green + the listed acceptance check + committed as `T<n>: ...`.

```mermaid
graph LR
  T1[T1 params+hull DONE] --> T2[T2 section]
  T2 --> T3[T3 equilibrium]
  T3 --> T4[T4 check]
  T4 --> T5[T5 optimize]
  T5 --> T6[T6 report]
  T5 --> T7[T7 serve+web]
  T5 --> T8[T8 perf]
```

| Wave | Task | Owns | Acceptance |
|---|---|---|---|
| - | **Status** | | Shape optimization (docs/SHAPE_PLAN.md): T12 physics, T13 shapeopt, T14 web in progress; T15 report+docs DONE (mix fit/edge note, hull/GM/drag/shape lines, `jaguar mix`, docs/MIX_DATA.md). Lab CSV strength fit DONE (src/mix.rs). T1-T7 DONE (T6 landed inside commit a287e63). Next: T8 perf is already met (693 candidates ~50 ms CLI); v2 items below need approval. |
| 0 | **T1** params + hull format + Michlet import | `params.rs` `hull.rs` | DONE (unit tests) |
| 1 | **T2** section properties | `section.rs` | unit tests: rectangle-ish box section vs hand calc; positive A, I everywhere off the tips |
| 1 | **T7a** web viewer, static | `web/index.html` | renders `tests/golden/off1_hull.json` + a hand-written sample result; no build step |
| 2 | **T3** equilibrium | `equilibrium.rs` | `golden_weight_and_draft` un-ignored and passing; Err on a sinking hull (test) |
| 3 | **T4** checks | `check.rs` | `golden_bending_dc` un-ignored and passing; capacity <= 0 gives fail (test) |
| 4 | **T5** optimizer | `optimize.rs` | `jaguar run examples/canoe.toml` finds a verified winner (expected after T9 strength checks: t=0.375 in, 68 pcf, depth 12 in, ~123.5 lb, D/C ~0.907 governed by punching, verify ~0.907, min freeboard ~6.14 in); two runs give identical result.json (test) |
| 5 | **T6** report | `report.rs` | summary <= 40 lines, shows winner, VERIFIED flag, grid-edge warning |
| 5 | **T7b** serve | `serve.rs` | API per `serve.rs` docs; Run from browser streams live candidates. DONE (verified in headless Chromium) |
| 5 | **T8** performance | `section.rs` `equilibrium.rs` (perf only) | default example < 1 s release; golden still passes; add `benches/` or a timed test |

## Contract proven
A throwaway implementation of T2-T5 written only against the stub signatures in this repo passed
all golden tests unchanged, produced the expected T5 winner above, ran 693 candidates in ~0.1 s
(release, 20 threads), was byte-deterministic across runs, and returned exit 2 with per-candidate
errors on a sinking crew. So the contracts are sufficient; agents should not need to change them.

## Notes per task

**T2 section**: port `section()` from `docs/reference/prototype_optimize.py`. Iterate stations with
`grid.row(i)`. Keep it allocation-free inside the loop.

**T3 equilibrium**: port `moments()`. You need a scalar root finder: write a small `brent()` (or
bisection + secant) in this file, ~30 lines, with its own tests. The prototype uses
`np.interp(d, z, cum)` for submerged area at local draft d; precompute `cum` per station once.
Nested solve: outer on trim, inner on draft. Both must bracket or return `Err`.

**T4 check**: freeboard is new (the prototype has no freeboard gate). Local draft at station i is
`draft + trim * (x_i - L/2) / L`; freeboard = depth - max local draft. Return it in `Verdict`.
For this, T3's `Loads` already exposes `draft_in` and `trim_in`.

**T5 optimize**: build each `Grid` once per depth and each `Sections` once per (depth, t), then
sweep density (density only scales self weight). rayon `par_iter` over (depth, t) pairs, collect, then
sort deterministically. Compute `hull_weight_lb` from sections (not from `Loads`) so failed
candidates still carry a real weight. Default seats when `seats_in` is None: `L*(i+1)/(n+1)`.

**T7 web**: single `web/index.html`, three.js r128 from CDN, fetches `/result.json` and `/hull.json`.
Show: 3D hull at winning depth, info box (t, rho, D, weight, D/C, VERIFIED), and a 2D plot of D/C vs
weight for all candidates (canvas, no chart library). If `winner` is null, say NO DESIGN PASSES in red.

## v2 candidates (need user approval before starting)
- Seating search (old: SLSQP over 40 arrangements) - moves hogging ~40%.
- Kupfer-Gerstle biaxial check (old: `structural.py::kupfer_*`).
- (approved, in progress: docs/SHAPE_PLAN.md) hull shape as a search variable.
- Richardson extrapolation of D/C over station refinement.
- (DONE) Lab CSV -> strength fit, see docs/MIX_DATA.md.
