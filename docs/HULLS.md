# Example hulls

| run file | hull | L x B (in) | t (in) | mix (pcf) | depth (in) | weight (lb) | GM (in) | governs |
|---|---|---|---|---|---|---|---|---|
| canoe.toml | off1 (real, Michlet import) | 240 x 20.7 | 0.375 | 58 | 12 | 98.2 | 1.07 | punching 0.90 |
| canoe.toml with its `[zones]` block on | off1, two mixes: 58 pcf knee pads (x 77..88 and 155..165 in, up to 1 5/8 in) | 240 x 20.7 | 0.375 | 51 / 58 | 12 | **86.7** | 1.03 | punching 0.90 |
| canoe_mixes.toml (tested mixes only) | off1 | 240 x 20.7 | 0.375 | "final target" 60 | 12 | 101.6 | 1.08 | punching 0.82 |
| sleek.toml | sleek: racing, narrow | 240 x 19 | NO DESIGN | | | | best -1.09 | stability |
| beamy.toml | beamy: wide, full | 230 x 28 | 0.375 | 58 | 10 | 103.5 | 10.3 | punching 0.90 |
| barge.toml | barge: bad on purpose | 192 x 40 | 0.375 | 58 | 9 | 125.7 | 55.5 | punching 0.90 |
| shape_opt.toml | shape search pick (entry 8.3 deg, rocker 3.8/3.4 in) | 216 x 23.6 (18 ft) | 0.375 | 58 | 12 | **82.5** | 1.74 | punching 0.90 |
| shape_gunwale.toml | shape search with the line-arc-line gunwale (R 430 in, waterline ends fore_wp 1.47 / aft_wp 1.60; 23.4 in at the waterline) | 228 x 26.0 overall (19 ft) | 0.375 | 58 | 11 | 89.1 | 2.65 | punching 0.90 |
| shape_race.toml | shape search, race-time objective (full ends, fore_wp 0.75 / aft_wp 0.89: pointed tips since 2026-10-18, was 99.8 lb, GM 1.08) | 256 x 21.9 (21 ft 4 in) | 0.375 | 58 | 12 | 100.0 | 0.91 | punching 0.90 |

All verified at 321 stations, on the smooth-keel loft (docs/SPEC.md Physics 1; until 2026-10-15
the table was blended at fixed heights, which put phantom hull under the rockered keel between
stations: off1 98.3 lb, shape search pick 86.8 lb, 216 x 19.6), with the default 4 in stem radius
(sharp stems: off1 98.24 lb): 2-person crew 175 + 165 lb, crew CG 12 in, gm_min 0.25 in, two-knee
punching with a 60/40 split, mesh at mid-wall (2 passes, docs/PUNCHING.md), auto seats checked at
+/- 3 in (`[seats]` defaults; GM is the worst seat corner, freeboard is gated at the marked seats,
`gates.freeboard_seat_shift_in` = 0, over the whole hull: the examples have no `[lettering]`). canoe.toml also checks
a placeholder 2F crew (140 + 130 lb, never governs). Its 3M, 3F and 4_coed cases (placeholder
weights) are commented out: with any of them off1 has no design, GM fails at crew CG 12 in
(best 3M -0.71, 3F -0.01, 4_coed -1.10 in, with auto seats).

* Seats: auto seats land within a few inches of even spacing on these hulls (the 2M auto order puts
  the 175 lb paddler aft); punching governs, so they do not move a fixed-hull winner. Freeboard is
  a static check at staging (RFP), so since 2026-10-18 it is gated at the marked seats, not at the
  worst +/- 3 in seat corner: the shape search pick went from 84.8 lb (13 in deep, freeboard bound
  at the shifted seats) to 82.5 lb (12 in deep, freeboard 6.01 in). The gunwale search lands on a
  different shape (89.1 lb vs 87.9 lb before: a sampled search; the earlier 87.9 lb shape still
  passes, freeboard only rises at the marked seats). Freeboard, GM and drag carry the real crew
  weight (the dynamic factor is for the stress checks only). At +/- 6 in the off1 winner stays
  98.2 lb (punching governs; freeboard 6.92 in at the marked seats, 6.41 in at the worst corner).

* Punching needs about a 58 pcf mix (f'c about 1640 psi on the lab fit), so it sets the mix for
  every hull. Hull shape then only changes how much wall there is.
* The shape search pick is 14% lighter than off1.
* Race-time objective (`[race]` events of canoe.toml, placeholder crews and paddler power): it picks
  a long hull, 15 lb heavier than the weight pick but faster: weighted race time 99.0 s vs
  104.1 s for the weight pick and 100.1 s for off1 (`jaguar race`, ASCE figure courses in `races/`;
  endurance: buoy 8 and the top-line buoy 12 kept to starboard, 639 m). Length cuts wave drag more than
  the extra wall costs. Re-check once paddler power is calibrated and the crews are real.
* Two mixes (`[zones]`): punching only needs the strong mix under the knees. 58 pcf pads under
  each knee footprint (x about 77..88 and 155..165 in, up to 1 5/8 in above the keel, covering the
  +/- 3 in seat range) with 51 pcf everywhere else: 86.7 lb, 11.4 lb (12%) lighter than the
  single-mix pick. The light mix sits at the low end of the lab data (51 pcf,
  +4 pcf extrapolation): lighter tested mixes would cut more.
* With `strength_model = "tested_mixes"` only real mixes are allowed, each lab row its own mix.
  M2 A (56.4 pcf) and M2 B (54.9 pcf) are too weak in punching, so the 60 pcf "final target" mix wins.

### Mold templates (`hull.templates`): how much does a simple mold cost?

Shape search with every shape reduced to N cross-section templates, straight between, before it
is analysed (the search tunes the buildable loft). Race time: weighted over the `[race]` events
(`jaguar race`, placeholder crews and power; measured on the earlier course layout, before the
ASCE figure courses: compare within the table). All verified.

| templates | shape_opt.toml (weight) | race s | shape_race.toml (race) | race s |
|---|---|---|---|---|
| smooth (41 stations) | 90.8 lb | 108.1 | 103.1 lb, 256 in | **103.3** |
| 15 | 91.5 lb (+0.8%) | 107.5 | 107.1 lb | 104.1 (+0.7%) |
| 11 | 91.6 lb (+0.9%) | 107.3 | 106.2 lb | 104.1 (+0.7%) |
| 9 | 92.8 lb (+2.2%) | 107.6 | 104.0 lb | 104.1 (+0.8%) |
| 7 | 91.8 lb (+1.1%) | 107.2 | 106.1 lb | 104.3 (+0.9%) |
| 5 | 94.1 lb (+3.6%) | 108.6 | 101.2 lb | 104.5 (+1.2%) |

Simplifying an already optimized smooth hull afterwards is worse: winners sit on their gates (the
race pick at freeboard 6.00 in, off1 at GM 0.26 vs 0.25), so sampling it at 21 templates already
costs a 1 in deeper mold (+10% wall) or a heavier mix. Search the template loft instead.

Re-run with `cargo run --release -- run examples/<x>.toml`. Update this table when a change moves
a winner.

## Making a new hull

* Parametric: write a shape toml (see `examples/*_shape.toml`, fields documented in
  `src/hullgen.rs`) and run `jaguar gen-hull my_shape.toml -o my_hull.json`.
* From Michlet: `jaguar import-michlet off1.mlt out.mlt -o my_hull.json`.

Then copy `examples/canoe.toml`, point `hull.file` at the new JSON, and run.
