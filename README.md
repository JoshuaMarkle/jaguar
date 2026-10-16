# JAGUAR

Finds the lightest concrete canoe wall (thickness, mix density, mold depth) for a given hull that
survives the crew, and can optionally search the hull shape too.

## Download and run (no terminal, no install)

1. Download from [Releases](https://github.com/JoshuaMarkle/jaguar/releases/latest):
   **JAGUAR-windows.zip** or **JAGUAR-mac.zip** (Apple silicon and Intel).
2. Windows: extract the zip, double-click **JAGUAR.exe**. Mac: drag **JAGUAR** to Applications,
   double-click it.
3. JAGUAR opens in your browser. Edit the inputs, press **Run optimization**. Your files live in
   **Documents/JAGUAR** (button **Open folder**); **Quit** stops it.

It starts with the team's settings (`packaging/starter/canoe.toml`): hull shape search with the
race-time objective, 4000 shapes, which takes hours. For a quick look set **Samples** to 96
(minutes). **Pause** holds a run; **End** finishes it early with the best hull found so far.

The first open shows a security warning because the app is not signed by a paid certificate.
Windows: **More info** > **Run anyway**. Mac: System Settings > Privacy & Security > **Open
Anyway** (macOS 14 and earlier: Control-click > Open). Details: the `Read me.txt` in each zip
(`packaging/`).

## From source (terminal)

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
New release for the download links: `git tag v0.2.0 && git push origin v0.2.0` (bump `version` in
Cargo.toml first); `.github/workflows/release.yml` builds both zips and publishes them with
`packaging/release-notes.md` plus the generated changelog.

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
