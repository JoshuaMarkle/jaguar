# JAGUAR

Finds the lightest concrete canoe wall (thickness, mix density, mold depth) for a given hull that
survives the crew. Terminal first. Browser view optional.

```bash
cargo build --release
./target/release/jaguar check examples/canoe.toml
./target/release/jaguar run   examples/canoe.toml      # -> result.json
./target/release/jaguar serve examples/canoe.toml     # optional web UI: http://127.0.0.1:8000
                                                       # (view canoe, edit params, Run, watch live)

# example hulls: canoe (real), sleek, beamy, barge (bad on purpose); see docs/HULLS.md
# bring your own hull from Godzilla/Michlet, or generate one: jaguar gen-hull shape.toml
./target/release/jaguar import-michlet off1.mlt out.mlt -o my_hull.json
```

* Parameters: `canoe.toml` (see `examples/canoe.toml`)
* Hull: `jaguar-hull/1` JSON offset table (see `docs/SPEC.md`)
* Result: `jaguar-result/1` JSON

Status: rewrite in progress, see `docs/PLAN.md`. Contributors and agents: read `AGENTS.md`.
The previous application lives in `old/` for reference.
