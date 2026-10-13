# Knee punching shear

## What it is

A kneeling paddler pushes their knee into the thin hull bottom. If the concrete
around the knee is too weak in shear, a cone of concrete breaks out and the knee
goes through the hull. Concrete design codes call this **punching** or
**two-way shear** (the crack forms all the way around the load, not just along
one line). For the canoe it is often the limiting check, because the wall is
very thin and the load is concentrated.

Settings live in `[punching]` in canoe.toml; code is `src/check.rs::punching_calc`.

## The two code methods (ACI 318-19)

Both treat the knee (plus pad) as a round contact of diameter
`d_load = knee_patch_in + 2 * pad_in` (3 in knee, no pad -> 3 in). A circle has
`beta = 1` (ratio of long to short side of the loaded area).

### `method = "aci_22_6"`: Table 22.6.5.2 (reinforced two-way slab), the default

* Effective depth `d` = distance from the face the knee bears on (inside) to the mesh. See
  "Mesh depth" below.
* Critical section `d/2` outside the contact (22.6.4.1): perimeter
  `b0 = pi * (d_load + d)`.
* Demand stress on that section (22.6.1.2): `v_u = P / (b0 * d)`.
* Capacity, least of three (Table 22.6.5.2), each times `lambda_s * lambda * sqrt(f'c)`:
  * (a) 4
  * (b) 2 + 4/beta = 6 for a round knee
  * (c) 2 + alpha_s d / b0, alpha_s = 40 (interior: the knee is far from the gunwale).
  For a thin wall (c) is always smallest: d/b0 is tiny, so the factor is ~2.2.
* `lambda_s = sqrt(2 / (1 + d/10)) <= 1` (size effect, 22.5.5.1.3): equals 1 for
  walls this thin, computed anyway.
* `lambda = 1`: f'c comes from YOUR lightweight mix breaks (via split tensile,
  `f_ct = 6.7 sqrt(f'c)`), so the lightweight reduction is already in the data.
* `material.method = "asd"` (default): allowable = nominal / `factor_of_safety` (1.5).
  `"lrfd"`: allowable = phi 0.75 x nominal against factored loads (1.2D+1.6L). Never both.

### `method = "plain_14_5"`: Table 14.5.5.1 (plain, unreinforced concrete)

* Uses the full thickness `h = t` and a critical section `h/2` out:
  `b0 = pi * (d_load + h)`, `v_u = P / (b0 * h)`.
* Capacity: lesser of `(4/3)(1 + 2/beta)` and `2 * (4/3)` times `lambda sqrt(f'c)`,
  i.e. `2.67 sqrt(f'c)` for a round knee.
* Use as a lower bound if the mesh is not reliably placed. Reading of the table
  from published ACI 318-19 / 318-25 excerpts; confidence high on the form, check
  the printed code before relying on it in the design paper.

## Mesh depth: casting layers and mold side

The wall is cast in `layers` equal passes with the mesh laid after pass `mesh_after_layer` = k.
Pass 1 goes against the mold, so the mesh sits k t / layers from the mold face:

* `mold = "male"`: pass 1 is the INSIDE face (the knee face): `d = k / layers * t`.
* `mold = "female"`: pass 1 is the OUTSIDE face: `d = (layers - k) / layers * t`.

t = 0.375 in, 3 passes, mesh after pass 2: male d = 0.250 in, female d = 0.125 in. On off1 that
moves the winner from 93.3 lb (male, t 0.375 in, 55 pcf; punching D/C 0.71: lighter mixes fail
GM at the 12 in depth freeboard needs, not punching) to 138.0 lb
(female, t 0.5 in, 61 pcf; punching governs at D/C 0.93).
`d_over_t` overrides all three (an error if given with them). Neither given = 2 passes, mesh
after 1, d = t/2 on either mold; the examples spell that out.

## How many knees, and how the weight splits

* `knees = 1`: the whole paddler (x dynamic factor 1.2) on one knee. Very conservative.
* `knees = 2` (default): the heavier knee carries `knee_split` of the paddler
  (default 0.6, which allows for uneven kneeling). If the two critical circles
  overlap (`knee_spacing_in < d_load + d`), they act together: the whole paddler
  on a merged racetrack-shaped perimeter `b0 = 2 * spacing + pi * (d_load + d)`.
  At 8 in spacing they do not overlap.

## Worked example (examples/canoe.toml, 175 lb paddler, t = 0.375 in)

P = 175 x 1.2 = 210 lb, d = 0.1875 in (2 passes, mesh after 1), b0 = pi (3 + 0.1875) = 10.01 in.

| model | demand v_u (psi) | allowable (psi) | D/C |
|---|---|---|---|
| aci_22_6, 1 knee, 51 pcf (f'c 760 psi) | 111.8 | 50.5 | 2.21 FAIL |
| aci_22_6, 2 knees 60/40, 51 pcf | 67.1 | 50.5 | 1.33 FAIL |
| aci_22_6, 2 knees 60/40, 58 pcf (f'c 1639 psi), **the winner** | 67.1 | 74.2 | 0.90 |
| plain_14_5, 1 knee, 51 pcf | 52.8 | 49.0 | 1.08 FAIL |
| wrong: demand on b0*t instead of b0*d, square 3 in, 1 knee, 51 pcf | 43.9 | 47.6 | 0.92 |

The demand must be divided by `b0 * d` (effective depth, half the wall), not `b0 * t`. Using the
full wall understates punching stress by 2x and makes 51 pcf look fine. With the correct demand,
two knees give back most of that, but the mix still has to be stronger (58 pcf), so punching sets
the mix and governs every example.

## How sure is this

* Table 22.6.5.2 (the three v_c expressions, alpha_s = 40 interior, size factor lambda_s) and
  Table 14.5.5.1 (plain, critical section at h/2) were checked against published excerpts of
  ACI 318-19 / 318-25.
* That two-way shear STRESS is taken on the critical section area b0*d (22.6.1.2 / 22.6.4) is the
  standard reading used in every slab design text; the clause wording was not re-read from the
  printed code here. Check it in the team's copy of ACI 318 before citing in the design paper.
* Dividing by b0*t would be 2x LESS conservative than that reading. If in doubt, run
  `method = "plain_14_5"`: it uses the full thickness and a lower capacity, and gives a similar
  answer (D/C 1.08 vs 2.21 for one knee at 51 pcf).

## What would change the answer (and is worth testing)

* **Knee pad** (`pad_in`, ASCE allows up to 0.5 in): 0.5 in pad = 4 in contact,
  ~25% more perimeter, ~20% less demand.
* **Mesh depth** (`layers`, `mesh_after_layer`, `mold`): mesh farther from the knee face
  increases d. Which face pass 1 is cast against matters as much as which pass the mesh follows.
* **A real test**: load a cast panel of the design wall with a knee-sized block
  until it fails. One test replaces every assumption on this page.
* Hull curvature and the surrounding shell help (the check treats the bottom as
  a flat slab), so the code check is conservative in that respect.
