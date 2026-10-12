# Example hulls

| run file | hull | L x B x T (in) | t (in) | rho (pcf) | depth (in) | weight (lb) |
|---|---|---|---|---|---|---|
| canoe.toml | off1 (real, Michlet import) | 240 x 20.7 x 5.9 | 0.375 | 51 | 12 | 92.6 |
| sleek.toml | sleek: racing, narrow, fine ends | 240 x 19 x 5.5 | 0.375 | 51 | 14 | 95.7 |
| beamy.toml | beamy: recreational, wide, full | 230 x 28 x 6.5 | 0.375 | 51 | 11 | 98.8 |
| barge.toml | barge: OBVIOUSLY BAD, short, boxy, blunt | 192 x 40 x 8 | 0.375 | 52 | 13 | 130.3 |

All winners verified at 321 stations (2M crew, 175 + 165 lb). Strength now comes from the
team's lab data (`data/mix_tests.csv`, see docs/MIX_DATA.md), so three hulls stop at 51 pcf,
the lightest density the lab data supports (tested 55-60 pcf, 4 pcf extrapolation allowed).
Test lighter mixes and these numbers drop. The barge is ~40% heavier than off1: more wall girth
per inch of length and it needs slightly denser (stronger) mix. Sleek's narrow, shallow body sinks
deeper and needs a 14 in mold to keep 6 in freeboard. Re-run with
`cargo run --release -- run examples/<x>.toml` (x = canoe, sleek, beamy, barge); numbers move
when checks or lab data change.

Table above is before the T12 stability gate. With T12's GM_T >= `gm_min_in` = 2.0 in (crew CG
12 in) in the working tree: beamy (GM 8.7 in) and barge (GM 47.9 in) keep the winners above, but
canoe (off1, best GM 0.8 in) and sleek report NO DESIGN PASSES, blocker stability. Either lower
`stability.gm_min_in`/`crew_cg_in` for those hulls or let `[shape_search]` pick a wider hull.

## Making a new hull

* Parametric: write a shape toml (see `examples/*_shape.toml`, fields documented
  in `src/hullgen.rs`) and run `jaguar gen-hull my_shape.toml -o my_hull.json`.
* From Godzilla/Michlet: `jaguar import-michlet off1.mlt out.mlt -o my_hull.json`.

Then copy `examples/canoe.toml`, point `hull.file` at the new JSON, and run.
