# JAGUAR

Finds the lightest concrete canoe wall (thickness, mix density, mold depth) for a given hull that
survives the crew, and can optionally search the hull shape too. Terminal first. Browser view optional.

```bash
cargo build --release
./target/release/jaguar check examples/canoe.toml
./target/release/jaguar run   examples/canoe.toml      # -> result.json
./target/release/jaguar serve examples/canoe.toml     # optional web UI: http://127.0.0.1:8000

# example hulls: canoe (real), sleek, beamy, barge (bad on purpose). See docs/HULLS.md
# bring your own hull from Michlet, or generate one: jaguar gen-hull shape.toml
./target/release/jaguar import-michlet off1.mlt out.mlt -o my_hull.json
./target/release/jaguar export result.json -o canoe.step   # winner as a STEP solid (or -o canoe.stl)
```

* Parameters: `canoe.toml` (see `examples/canoe.toml`). Full contract: `docs/SPEC.md`
* Concrete strength: fitted from the team's lab breaks in `data/mix_tests.csv`. Run `jaguar mix` to
  see the fit (`docs/MIX_DATA.md`)
* Hull shape search: add `[shape_search]` to canoe.toml (`examples/shape_opt.toml`,
  `docs/SHAPE_SEARCH.md`)

Contributors and agents: read `AGENTS.md`. Open work: `docs/PLAN.md`.

## Usage

1. **Copy the example:** `cp examples/canoe.toml my_canoe.toml`. Every setting in it is commented.
   Paths are relative to the toml, so in the copy set `hull.file` to your hull JSON and
   `material.mix_data` to `data/mix_tests.csv`. Then set the crew under `[[load_case]]` and the
   ranges to try under `[search]`.
2. **Check it:** `jaguar check my_canoe.toml` validates the file and the hull without running.
   Typos in keys are errors, and it warns if the lab data behind the strength fit is thin.
3. **Run it:** `jaguar run my_canoe.toml -o result.json`. The terminal shows the lightest passing
   wall per thickness and then the winner: thickness, mix density, mold depth, weight, every check
   with its D/C, and which check governs. `VERIFIED` means the winner still passes when re-checked
   at twice the resolution. Exit code 0 = verified design, 2 = nothing passes (the output says
   what blocked it), 1 = input error.
4. **Look at it (optional):** `jaguar serve my_canoe.toml`, then open http://127.0.0.1:8000 to see
   the canoe in 3D, edit parameters, start a run and watch candidates come in.

When a new lab break comes in, add a row to `data/mix_tests.csv`, check the fit with `jaguar mix`,
and run again. If the winner sits at the edge of a `[search]` range you get a `WARNING`: widen the
range. If it sits at the lightest density the lab data supports you get a `NOTE`: test lighter
mixes.
