# Example hulls

| run file | hull | L x B (in) | t (in) | rho (pcf) | depth (in) | weight (lb) | GM (in) | drag @2.5 m/s |
|---|---|---|---|---|---|---|---|---|
| canoe.toml | off1 (real, Michlet import) | 240 x 20.7 | 0.375 | 51 | 12 | 92.6 | 0.29 | 38.4 N |
| sleek.toml | sleek: racing, narrow, fine ends | 240 x 19 | NO DESIGN | | | | best -1.45 | |
| beamy.toml | beamy: recreational, wide, full | 230 x 28 | 0.375 | 51 | 11 | 98.8 | 8.7 | |
| barge.toml | barge: OBVIOUSLY BAD, short, boxy, blunt | 192 x 40 | 0.375 | 52 | 13 | 130.3 | 47.9 | |
| shape_opt.toml (weight) | optimizer's pick | 216 x 23.1 (18 ft) | 0.375 | 51 | 12 | **80.5** | 1.0 | |
| shape_opt.toml, kind = "drag" | optimizer's pick | 236 x 24.0 (19 ft 8 in) | 0.375 | 51 | 12 | 85.8 | 0.8 | **30.9 N** |

All winners verified at 321 stations (2M crew, 175 + 165 lb, crew CG 12 in, `gm_min_in` 0.25).
Strength comes from the team's lab data (`data/mix_tests.csv`, see docs/MIX_DATA.md), so every
hull stops at about 51 pcf, the lightest density the lab data supports (tested 55-60 pcf, 4 pcf
extrapolation allowed). Test lighter mixes and these numbers drop.

* **Shape optimization** (`shape_opt.toml`, ~23 s): with the weight objective it picks the
  shortest allowed hull (18 ft, the bound) with enough beam to stay upright: 12% lighter than
  off1. With `kind = "drag"` it picks a longer, finer hull: 20% less water resistance than off1
  for 7% more weight than the weight-optimal shape. Pick the objective that matches what you
  are optimizing for; the length bounds in `[shape_search]` should match the ASCE rules.
* **Stability** (GM): sleek is too narrow to stay upright with a crew CG 12 in above the keel.
  GM is very sensitive to `crew_cg_in` (assumed, not measured): measure it.
* The barge is ~40% heavier than off1: more wall girth per inch of length.

Re-run with `cargo run --release -- run examples/<x>.toml`; numbers move when checks or lab data
change.

## Making a new hull

* Parametric: write a shape toml (see `examples/*_shape.toml`, fields documented
  in `src/hullgen.rs`) and run `jaguar gen-hull my_shape.toml -o my_hull.json`.
* From Godzilla/Michlet: `jaguar import-michlet off1.mlt out.mlt -o my_hull.json`.

Then copy `examples/canoe.toml`, point `hull.file` at the new JSON, and run.
