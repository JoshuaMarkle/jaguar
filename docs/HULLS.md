# Example hulls

| run file | hull | L x B x T (in) | t (in) | rho (pcf) | depth (in) | weight (lb) |
|---|---|---|---|---|---|---|
| canoe.toml | off1 (real, Michlet import) | 240 x 20.7 x 5.9 | 0.375 | 68 | 12 | 123.5 |
| sleek.toml | sleek: racing, narrow, fine ends | 240 x 19 x 5.5 | 0.375 | 68 | 14 | 127.6 |
| beamy.toml | beamy: recreational, wide, full | 230 x 28 x 6.5 | 0.375 | 68 | 11 | 131.7 |
| barge.toml | barge: OBVIOUSLY BAD, short, boxy, blunt | 192 x 40 x 8 | 0.375 | 68 | 9 | 147.7 |

All winners verified at 321 stations (2M crew, 175 + 165 lb). The barge is ~20%
heavier than off1: more wall girth per inch of length. Sleek is slightly heavier
than off1 because its narrow, shallow body sinks deeper and needs a 14 in mold
to keep 6 in freeboard. barge.toml searches depth 8..14 in (its winner sits
below the default 10..16 range). Re-run with `jaguar run examples/<x>.toml`;
numbers will move if the checks change.

## Making a new hull

* Parametric: write a shape toml (see `examples/*_shape.toml`, fields documented
  in `src/hullgen.rs`) and run `jaguar gen-hull my_shape.toml -o my_hull.json`.
* From Godzilla/Michlet: `jaguar import-michlet off1.mlt out.mlt -o my_hull.json`.

Then copy `examples/canoe.toml`, point `hull.file` at the new JSON, and run.
