# JAGUAR: agent instructions

Read this file fully. Open work is in `docs/PLAN.md`.

## What this is
A terminal program that finds the **lightest concrete canoe wall** (thickness, mix density, mold depth)
for a given hull that passes every check under the crew, optionally searching the hull shape too.
`jaguar serve canoe.toml` is an optional web UI (view the canoe, start a run, watch it live).
If a feature is not in `docs/SPEC.md`, do not add it.

## Layout
Data flows one way, top to bottom. A module may only `use` modules above it.
`serve` and `report` never feed back into physics.
```
src/units.rs        unit constants (in, lb, psi, pcf)
src/params.rs       canoe.toml -> Params (the only input)
src/mix.rs          lab CSV -> strength fit (docs/MIX_DATA.md)
src/hull.rs         jaguar-hull/1 JSON, Michlet import, Grid
src/hullgen.rs      parametric hull from shape factors (gen-hull, shape search)
src/section.rs      shell A, zbar, I per station
src/equilibrium.rs  float + trim, GM, V(x), M(x)
src/resistance.rs   Michell wave + ITTC friction drag
src/check.rs        bending/compression/shear/punching (docs/PUNCHING.md), freeboard
src/optimize.rs     wall grid search, verify at 2n-1 stations, progress events
src/shapeopt.rs     outer hull-shape search (docs/SHAPE_SEARCH.md)
src/lib.rs          jaguar::run: dispatches to optimize or shapeopt
src/report.rs       terminal summary, result.json
src/serve.rs + web/ localhost web UI (API documented in serve.rs, single index.html, no build step)
src/main.rs         CLI: run | check | mix | import-michlet | gen-hull | serve

tests/golden.rs     must match the Python prototype (docs/reference/prototype_optimize.py)
tests/golden/       golden JSON + the Michlet files the reference hull came from
tests/*.rs          end-to-end: example hulls, mixes, optimizer, shape search
examples/           runnable canoe.toml files and hulls (docs/HULLS.md)
data/               lab mix data (mix_tests.csv, cleaned from data/raw/)
```

## Docs
| file | read when |
|---|---|
| `docs/SPEC.md` | the contract: CLI, input/output formats, physics, scope |
| `docs/PLAN.md` | picking up work |
| `docs/PUNCHING.md` | touching knee punching (it governs every example) |
| `docs/MIX_DATA.md` | touching strength or `data/` |
| `docs/SHAPE_SEARCH.md` | touching `shapeopt.rs`, `hullgen.rs`, `resistance.rs` |
| `docs/HULLS.md` | example results. Update the table when a change moves a winner |

## Commands
```
make hooks     # once per clone: enables the pre-commit hook (runs make check)
make check     # fmt + clippy -D warnings + tests. MUST pass before every commit.
make run       # cargo run --release -- run examples/canoe.toml
make serve     # web UI at http://127.0.0.1:8000
```

## Rules
1. **Units:** inches, pounds, psi, pcf inside the crate. Convert only at the edges (`hull::from_michlet`,
   `resistance`).
2. **Fail, never fabricate.** When a value cannot be computed, return `Err` or mark the candidate
   failed with a reason. No `0.0`/`NaN`/`-1` sentinel a gate could read as a real number, and no
   "best failing design" fallback. A sentinel is safe only if failure and the legitimate value point
   the same way. The legacy app read `GM_T = 0.0` as "neutrally stable" and skipped the gate.
3. **Knee load is a 3 in patch, never a point load.** Point loads make D/C not converge.
4. **Inputs are strict.** `deny_unknown_fields` on every input struct. New parameter = add it to
   `Params`, `examples/canoe.toml`, and `docs/SPEC.md` in the same commit.
5. **Deterministic.** Same canoe.toml -> byte-identical result.json. Parallelism via rayon must not
   change ordering of output.
6. **Pure physics.** Everything above `optimize` in the layout does no I/O and no printing
   (except the file loaders/savers in `params`, `mix`, `hull`).
7. **Tests with the code.** Every change ships tests. Never loosen a golden tolerance to make a test
   pass. Find the bug.
8. **Small commits**, message `T<n>: what` for planned tasks. Stay inside your task's files so
   parallel agents don't collide. If you must touch a shared type, say so in the commit message.
9. **Dependencies:** ask before adding a crate.
10. **Keep docs true, not long.** If you change behavior, fix the doc that describes it in the same
    commit. Don't add new docs for things the code and its comments already say.

## Physics notes
* D/C drifts up about 1-2% per station doubling, so the search gates at `dc_search` (0.93) and the
  winner is re-checked at 2n-1 stations against `dc_verify` (0.95).
* The legacy Python app is gone from the tree. To look at it, use `git show legacy-final:old/<path>`.
  Do this for physics intent only, never architecture.
