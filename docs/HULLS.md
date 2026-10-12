# Example hulls

| run file | hull | L x B (in) | t (in) | mix (pcf) | depth (in) | weight (lb) | GM (in) | governs |
|---|---|---|---|---|---|---|---|---|
| canoe.toml | off1 (real, Michlet import) | 240 x 20.7 | 0.375 | 58 | 12 | 105.4 | 0.33 | punching 0.90 |
| canoe_mixes.toml (tested mixes only) | off1 | 240 x 20.7 | 0.375 | "final target" 60 | 12 | 109.0 | 0.34 | punching 0.82 |
| sleek.toml | sleek: racing, narrow | 240 x 19 | NO DESIGN | | | | best -1.45 | stability |
| beamy.toml | beamy: wide, full | 230 x 28 | 0.375 | 58 | 11 | 112.3 | 8.7 | punching 0.90 |
| barge.toml | barge: bad on purpose | 192 x 40 | 0.375 | 58 | 11 | 135.7 | 47.7 | punching 0.90 |
| shape_opt.toml | shape search pick (entry 7.4 deg, rocker 3.2/2.1 in) | 216 x 23.1 (18 ft) | 0.375 | 58 | 12 | **91.7** | 1.2 | punching 0.90 |

All verified at 321 stations: 2-person crew 175 + 165 lb, crew CG 12 in, gm_min 0.25 in, two-knee
punching with a 60/40 split (docs/PUNCHING.md).

* Punching needs about a 58 pcf mix (f'c about 1640 psi on the lab fit), so it sets the mix for
  every hull. Hull shape then only changes how much wall there is.
* The shape search pick is 13% lighter than off1.
* With `strength_model = "tested_mixes"` only real mixes are allowed. M2 (55.6 pcf) is too weak in
  punching, so the 60 pcf "final target" mix wins.

Re-run with `cargo run --release -- run examples/<x>.toml`. Update this table when a change moves
a winner.

## Making a new hull

* Parametric: write a shape toml (see `examples/*_shape.toml`, fields documented in
  `src/hullgen.rs`) and run `jaguar gen-hull my_shape.toml -o my_hull.json`.
* From Michlet: `jaguar import-michlet off1.mlt out.mlt -o my_hull.json`.

Then copy `examples/canoe.toml`, point `hull.file` at the new JSON, and run.
