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
src/stepimport.rs   STEP B-rep (planes, B-splines, cylinders...) -> triangles for meshimport
src/meshimport.rs   CAD canoe (STEP / STL / OBJ) -> hull table, measured wall + depth, as-drawn canoe.toml
src/section.rs      shell A, zbar, I per station
src/equilibrium.rs  float + trim, GM, V(x), M(x)
src/flotation.rs    swamped flotation: bow/stern air pocket length
src/curves.rs       power vs speed and righting lever (GZ) vs heel of the winner (pictures, not gates)
src/resistance.rs   Michell wave + ITTC friction drag
src/race.rs         race simulator: optimal path + speed over ASCE courses, straight/turn weighting
src/check.rs        bending/compression/shear/punching (docs/PUNCHING.md), freeboard
src/stands.rs       empty winner on two stands (post-search verification, never a gate)
src/seats.rs        auto seats + crew order, seat-variance samples
src/optimize.rs     wall grid search, verify at 2n-1 stations, progress events
src/shapeopt.rs     outer hull-shape search (docs/SHAPE_SEARCH.md)
src/lib.rs          jaguar::run: dispatches to optimize or shapeopt
src/report.rs       terminal summary, result.json, race summary + result.json reader for race
src/export.rs       STEP / STL of the winner (CAD, mold shop)
src/manufacture.rs  male foam mold: hardboard tracing templates (or boards), packed on sheets, DXF
src/launch.rs       desktop app (`jaguar`, no command): Documents/JAGUAR starter folder, browser, dialogs
src/serve.rs + web/ localhost web UI (API documented in serve.rs, single index.html, no build step;
                    web/vendor/three.min.js r128 embedded, so it works offline)
src/main.rs         CLI: (none = desktop app) | run | check | mix | import-michlet | import-mesh | gen-hull | export | mold | race | serve

tests/golden.rs     must match tests/golden/reference.py (independent Python implementation)
tests/golden/       golden JSON, reference.py, and the Michlet files the off1 hull came from
tests/*.rs          end-to-end: example hulls, mixes, optimizer, shape search, race
packaging/          what the release zips add: macOS Info.plist, read-me per platform, release-notes.md,
                    starter/canoe.toml (the team's settings, first-launch canoe.toml)
                    (.github/workflows/release.yml builds JAGUAR-windows.zip, JAGUAR-mac.zip)
examples/           runnable canoe.toml files, hulls, frozen lab data mix_tests.csv (docs/HULLS.md)
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
| `docs/DESIGN.md` | touching `web/` (visual language: monochrome, pills, hairlines, no shadows) |
| `docs/HULLS.md` | example results. Update the table when a change moves a winner |

## Commands
```
make hooks     # once per clone: enables the pre-commit hook (runs make check)
make check     # fmt + clippy -D warnings + tests. MUST pass before every commit.
make run       # cargo run --release -- run examples/canoe.toml
make serve     # web UI at http://127.0.0.1:8000
git tag v0.4.2 && git push origin v0.4.2   # release: builds and publishes the Windows + Mac zips
```

## Rules
1. **Units:** inches, pounds, psi, pcf inside the crate. Convert only at the edges (`hull::from_michlet`,
   `resistance`).
2. **Fail, never fabricate.** When a value cannot be computed, return `Err` or mark the candidate
   failed with a reason. No `0.0`/`NaN`/`-1` sentinel a gate could read as a real number, and no
   "best failing design" fallback. A sentinel is safe only if failure and the legitimate value point
   the same way. Example: `GM_T = 0.0` reads as "neutrally stable", so a gate that treats it as
   "not computed" silently skips stability.
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
* The search gates at `dc_search` (0.93); the winner is re-run at 2n-1 and 4n-3 stations and its
  Richardson-extrapolated D/C is gated at `dc_verify` (0.95), falling back to the 2n-1 D/C when a
  series that could govern is irregular. Measured: punching does not move with stations; bending
  and biaxial move under 0.5% per station doubling, mostly down (closed-form knee patch), so the
  0.93/0.95 margin is conservative (PLAN B6b).
* Knee punching governs every example hull, so changes to `check::punching_calc`, the mix fit or
  crew loads move the answer most. Re-run the examples and update `docs/HULLS.md`.
