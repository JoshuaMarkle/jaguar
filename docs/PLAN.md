# Plan

## Open work

- **NEXT: web UI cleanup** (`web/index.html`, `src/serve.rs`, `examples/canoe.toml`):
  * remove the raw TOML and generated-TOML views from the served site (the form is the only editor);
  * default `objective.kind` to `"drag"`;
  * auto-save input prefs (the canoe.toml the form builds) to a fixed spot on every edit and load it
    on reopen, so settings survive closing the browser/server.
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
