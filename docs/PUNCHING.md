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

* Effective depth `d = d_over_t * t` (mesh at mid-wall: `d_over_t = 0.5`).
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
* Allowable = nominal / `factor_of_safety` (1.5). ACI's strength-design phi = 0.75
  is NOT applied on top: the project uses one safety factor, not two.

### `method = "plain_14_5"`: Table 14.5.5.1 (plain, unreinforced concrete)

* Uses the full thickness `h = t` and a critical section `h/2` out:
  `b0 = pi * (d_load + h)`, `v_u = P / (b0 * h)`.
* Capacity: lesser of `(4/3)(1 + 2/beta)` and `2 * (4/3)` times `lambda sqrt(f'c)`,
  i.e. `2.67 sqrt(f'c)` for a round knee.
* Use as a lower bound if the mesh is not reliably placed. Reading of the table
  from published ACI 318-19 / 318-25 excerpts; confidence high on the form, check
  the printed code before relying on it in the design paper.

## How many knees, and how the weight splits

* `knees = 1`: the whole paddler (x dynamic factor 1.2) on one knee. Very conservative.
* `knees = 2` (default): the heavier knee carries `knee_split` of the paddler
  (default 0.6, which allows for uneven kneeling). If the two critical circles
  overlap (`knee_spacing_in < d_load + d`), they act together: the whole paddler
  on a merged racetrack-shaped perimeter `b0 = 2 * spacing + pi * (d_load + d)`.
  At 8 in spacing they do not overlap.

## Worked example (examples/canoe.toml, 175 lb paddler, t = 0.375 in)

P = 175 x 1.2 = 210 lb, d = 0.1875 in, b0 = pi (3 + 0.1875) = 10.01 in.

| model | demand v_u (psi) | allowable (psi) | D/C |
|---|---|---|---|
| OLD (before this fix): square 3 in, demand on b0*t, 1 knee, 51 pcf | 43.9 | 47.6 | 0.92 |
| NEW aci_22_6, 1 knee, 51 pcf (f'c 760 psi) | 111.8 | 50.5 | 2.21 FAIL |
| NEW aci_22_6, 2 knees 60/40, 51 pcf | 67.1 | 50.5 | 1.33 FAIL |
| NEW aci_22_6, 2 knees 60/40, 58 pcf (f'c 1639 psi), **the new winner** | 67.1 | 74.2 | 0.90 |
| plain_14_5, 1 knee, 51 pcf | 52.8 | 49.0 | 1.08 FAIL |

**The old check was unconservative.** It divided the knee load by `b0 * t` (full
wall) while the code divides by `b0 * d` (effective depth, half the wall). That
understated punching stress by 2x. Two knees instead of one gives back most of
that, but the net result is that the lab mix must be stronger (58 pcf instead of
51 pcf), so the example winner gets heavier: 92.6 lb -> 105.4 lb. Punching still
governs, now honestly.

## How sure is this

* Table 22.6.5.2 (the three v_c expressions, alpha_s = 40 interior, size factor lambda_s) and
  Table 14.5.5.1 (plain, critical section at h/2) were checked against published excerpts of
  ACI 318-19 / 318-25.
* That two-way shear STRESS is taken on the critical section area b0*d (22.6.1.2 / 22.6.4) is the
  standard reading used in every slab design text; the clause wording was not re-read from the
  printed code here. Check it in the team's copy of ACI 318 before citing in the design paper.
* The old check (b0*t) was 2x LESS conservative than that reading. If in doubt, run
  `method = "plain_14_5"`: it uses the full thickness and a lower capacity, and gives a similar
  answer (D/C 1.08 vs 2.21 for one knee at 51 pcf), i.e. the old 0.92 was optimistic either way.

## What would change the answer (and is worth testing)

* **Knee pad** (`pad_in`, ASCE allows up to 0.5 in): 0.5 in pad = 4 in contact,
  ~25% more perimeter, ~20% less demand.
* **Mesh depth** (`d_over_t`): mesh nearer the outside face increases d.
* **A real test**: load a cast panel of the design wall with a knee-sized block
  until it fails. One test replaces every assumption on this page.
* Hull curvature and the surrounding shell help (the check treats the bottom as
  a flat slab), so the code check is conservative in that respect.
