# Plan

The rewrite is done (tasks T1-T18, see `git log`). The legacy Python app was removed. To read it,
use `git show legacy-final:old/<path>`.

## Open work
- **Knee load test**: measure the real knee patch and load split. Punching sets the mix for every
  hull (docs/PUNCHING.md), so this is the input that moves the answer most.
- **More lab mixes**: light mixes that are strong in punching (55-60 pcf and up), or a knee pad
  (docs/MIX_DATA.md).
- **Measured crew CG** for the GM gate (`stability.crew_cg_in` is an estimate).
- **ASCE length bounds** for `[shape_search]` from this year's rules.
- **Drag reference**: run Michlet with wave drag on for one hull and compare (docs/SHAPE_SEARCH.md).

## Ideas (each needs the user's approval before starting)
- Seating search (the legacy app used SLSQP over 40 arrangements). It moves hogging by about 40%.
- Kupfer-Gerstle biaxial check.
- Richardson extrapolation of D/C over station refinement.

## How to take a task
Give it an id `T<n>` (next free number), list the files it owns so parallel agents don't collide,
and commit as `T<n>: what`. Done = `make check` green plus a test that pins the new behavior.
