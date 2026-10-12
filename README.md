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
```

* Parameters: `canoe.toml` (see `examples/canoe.toml`). Full contract: `docs/SPEC.md`
* Concrete strength: fitted from the team's lab breaks in `data/mix_tests.csv`. Run `jaguar mix` to
  see the fit (`docs/MIX_DATA.md`)
* Hull shape search: add `[shape_search]` to canoe.toml (`examples/shape_opt.toml`,
  `docs/SHAPE_SEARCH.md`)

Contributors and agents: read `AGENTS.md`. Open work: `docs/PLAN.md`.
