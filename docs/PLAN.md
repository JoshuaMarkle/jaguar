# Plan

## Open work

- **Knee load test**: load a cast panel of the design wall with a knee-sized block until it fails.
  Punching sets the mix for every hull (docs/PUNCHING.md), so this input moves the answer most.
- **More lab mixes**: the strength fit rests on 3 points. Light mixes that are strong in punching
  (55-60 pcf) would lower weight most (docs/MIX_DATA.md).
- **Measured crew CG** for the GM gate (`stability.crew_cg_in` is an estimate, and GM is very
  sensitive to it).
- **ASCE length bounds** for `[shape_search]` from this year's rules.
- **Drag reference**: run Michlet with wave drag on for one hull and compare (docs/SHAPE_SEARCH.md).

## Ideas (each needs the user's approval before starting)

- Seating search: optimize seat positions. It can move hogging moment by about 40%.
- Kupfer-Gerstle biaxial check.
- Richardson extrapolation of D/C over station refinement.

## How to take a task

Give it an id `T<n>` (next free number after the highest in `git log`), list the files it owns so
parallel agents don't collide, and commit as `T<n>: what`. Done = `make check` green plus a test
that pins the new behavior.
