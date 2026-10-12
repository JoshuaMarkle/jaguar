# JAGUAR: agent instructions

Rewrite in progress. Read this file fully, then `docs/PLAN.md` for your task.

## What this is
A terminal program that finds the **lightest concrete canoe wall** (thickness, mix density, mold depth)
for a given hull that does not crack under the crew. That is the whole product. Visualization is an
optional `jaguar serve` that reads `result.json`. Nothing else (no races, no GUI, no mix lab, no PDFs,
no Godzilla runs). If a feature is not in `docs/SPEC.md`, do not add it.

## Layout
```
src/params.rs       canoe.toml -> Params (the only input)         DONE
src/hull.rs         jaguar-hull/1 JSON, Michlet import, Grid      DONE
src/section.rs      shell A, zbar, I per station                  T2
src/equilibrium.rs  float + trim, V(x), M(x)                      T3
src/check.rs        bending D/C, freeboard                        T4
src/optimize.rs     grid search, verify at 2n-1                   T5
src/report.rs       terminal summary                              T6
src/serve.rs + web/ optional localhost viewer                     T7
src/main.rs         CLI: run | check | import-michlet | serve
tests/golden.rs     must match the Python prototype (oracle)
old/                legacy app. READ-ONLY reference. Never edit, never import.
```
Data flows one way: params/hull -> section -> equilibrium -> check -> optimize -> report/serve.
A module may only `use` modules to its left. `serve` and `report` never feed back into physics.

## Commands
```
make check     # fmt + clippy -D warnings + tests. MUST pass before every commit.
make golden    # include #[ignore]d golden tests (shows what is left to port)
make run       # cargo run --release -- run examples/canoe.toml
make serve     # view result.json at http://127.0.0.1:8000
```

## Rules
1. **Units:** inches, pounds, psi, pcf inside the crate. Convert only at the edges (`hull::from_michlet`).
2. **Fail, never fabricate.** When a value cannot be computed, return `Err` or mark the candidate
   failed with a reason. No `0.0`/`NaN`/`-1` sentinels a gate could read as a real number. No
   "best failing design" fallback. (Legacy lessons: `old/CLAUDE.md` "Sentinels and Absence".)
3. **Knee load is a 3 in patch, never a point load.** Point loads make D/C not converge.
4. **Inputs are strict.** `deny_unknown_fields` on every input struct. New parameter = add it to
   `Params`, `examples/canoe.toml`, and `docs/SPEC.md` in the same commit.
5. **Deterministic.** Same canoe.toml -> byte-identical result.json. Parallelism via rayon must not
   change ordering of output.
6. **Pure physics.** `section`, `equilibrium`, `check` do no I/O and no printing.
7. **Tests with the code.** Every task ships unit tests. When you finish a golden-tested stage, remove
   its `#[ignore]`. Never loosen a golden tolerance to make a test pass; find the bug.
8. **Small commits**, message `T<n>: what`. Stay inside your task's files (see PLAN) so parallel
   agents don't collide. If you must touch a shared type, say so in the commit message.
9. Dependencies: ask before adding a crate. Current set is enough for every task.

## Physics reference
* `docs/reference/prototype_optimize.py` is the exact algorithm the golden values came from. Port
  it faithfully first, improve later.
* `docs/PORTING.md` maps legacy files to new modules and lists what was cut on purpose.
