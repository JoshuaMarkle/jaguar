# Example hulls

| run file | hull | L x B (in) | t (in) | mix (pcf) | depth (in) | weight (lb) | GM (in) | governs |
|---|---|---|---|---|---|---|---|---|
| canoe.toml | off1 (real, Michlet import) | 240 x 20.7 | 0.375 | 58 | 12 | 98.4 | 0.26 | punching 0.90 |
| canoe_mixes.toml (tested mixes only) | off1 | 240 x 20.7 | 0.375 | "final target" 60 | 12 | 101.8 | 0.27 | punching 0.82 |
| sleek.toml | sleek: racing, narrow | 240 x 19 | NO DESIGN | | | | best -1.56 | stability |
| beamy.toml | beamy: wide, full | 230 x 28 | 0.375 | 58 | 11 | 109.3 | 8.7 | punching 0.90 |
| barge.toml | barge: bad on purpose | 192 x 40 | 0.375 | 58 | 9 | 125.7 | 48.5 | punching 0.90 |
| shape_opt.toml | shape search pick (entry 9.3 deg, rocker 3.5/3.0 in) | 216 x 24.6 (18 ft) | 0.375 | 58 | 12 | **90.8** | 2.6 | punching 0.90 |
| shape_gunwale.toml | shape search with the line-arc-line gunwale (R 318 in, waterline ends fore_wp 1.22 / aft_wp 1.27) | 224 x 25.1 (18 ft 8 in) | 0.375 | 58 | 12 | 100.4 | 2.3 | punching 0.90 |
| shape_race.toml | shape search, race-time objective | 256 x 21.9 (21 ft 4 in) | 0.375 | 58 | 12 | 103.1 | 0.54 | punching 0.90 |

All verified at 321 stations: 2-person crew 175 + 165 lb, crew CG 12 in, gm_min 0.25 in, two-knee
punching with a 60/40 split, mesh at mid-wall (2 passes, docs/PUNCHING.md), auto seats checked at
+/- 3 in (`[seats]` defaults; GM and freeboard are the worst seat corner). canoe.toml also checks
a placeholder 2F crew (140 + 130 lb, never governs). Its 3M, 3F and 4_coed cases (placeholder
weights) are commented out: with any of them off1 has no design, GM fails at crew CG 12 in
(best 3M -0.71, 3F -0.01, 4_coed -1.10 in, with auto seats).

* Seats: auto seats land within a few inches of even spacing on these hulls (the 2M auto order puts
  the 175 lb paddler aft); punching governs, so they do not move a fixed-hull winner. The +/- 3 in
  seat variance costs freeboard: the shape search pick is 87.2 lb without it (freeboard binds at
  6.0 in), 90.8 lb with it. At +/- 6 in the off1 winner jumps to 0.5 in, 63 pcf, 13 in deep, 151 lb (GM and freeboard).

* Punching needs about a 58 pcf mix (f'c about 1640 psi on the lab fit), so it sets the mix for
  every hull. Hull shape then only changes how much wall there is.
* The shape search pick is 8% lighter than off1.
* Race-time objective (`[race]` events of canoe.toml, placeholder crews and paddler power): it picks
  a long hull, 12 lb heavier than the weight pick but faster: weighted race time 103.3 s vs
  108.1 s for the weight pick and 103.6 s for off1 (`jaguar race`). Length cuts wave drag more than
  the extra wall costs. Re-check once paddler power is calibrated and the crews are real.
* With `strength_model = "tested_mixes"` only real mixes are allowed. M2 (55.6 pcf) is too weak in
  punching, so the 60 pcf "final target" mix wins.

Re-run with `cargo run --release -- run examples/<x>.toml`. Update this table when a change moves
a winner.

## Making a new hull

* Parametric: write a shape toml (see `examples/*_shape.toml`, fields documented in
  `src/hullgen.rs`) and run `jaguar gen-hull my_shape.toml -o my_hull.json`.
* From Michlet: `jaguar import-michlet off1.mlt out.mlt -o my_hull.json`.

Then copy `examples/canoe.toml`, point `hull.file` at the new JSON, and run.
