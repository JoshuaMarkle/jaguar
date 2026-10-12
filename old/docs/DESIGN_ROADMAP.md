# Design Roadmap — Remainder of the Program

> **2026-07-11:** Defect fixes & feature requests from the July punch list
> are planned in `docs/FIX_PLAN_2026-07.md` — its Phase 1 (canoe-specific
> engineering: signed hog/sag envelopes, depth-based mix zoning, constant
> utilization, uniform tip thickness) takes priority over the remaining
> blocks below.

*Written 2026-07-07 from a full codebase review. Supersedes the completed
`frolicking-roaming-pumpkin.md` plan (all blocks B1–B11 shipped, 177 tests passing).*

## Target workflow (user-defined)

1. **Make seed** — race sim or manual configurator produces a Series-7 hull
2. **Run Godzilla** — `mlt933w.exe -godz` finds the best hydrodynamic underwater base
3. **Structural sweep** — finds the lightest upper section (thickness, depth, flare,
   gunwales, mixes) that survives the loading cases
4. **Export final model** — complete canoe viewable in OnShape / any CAD

Goal: fastest canoe = **light** (structural sweep) + **low drag** (Godzilla at the
*correct* displacement) + **stable** (GMT constraint) + **intuitive** program.

---

## Current state audit

| Step | Status | Where |
|------|--------|-------|
| 1. Seed | ✅ Works | `RaceService` (GA + `hull_matcher`), `ManualHullTab`, `godzilla_io.generate_godzilla_input` (CRLF-safe, golden-tested) |
| 2. Godzilla | ✅ Works | `HullService.launch_godzilla` (stop/log/progress, template validation, fallbacks) |
| 3. Structural | ✅ Works | 5-D sweep in `main.run_grand_optimization`, flare feasibility, torsion/lateral, Monte Carlo, compliance PDF, cost |
| 4. Export | ⚠️ Partial | Underwater-only wireframe STEP + STL (`step_export_service`); package zip has docs/data but no final geometry |

### Confirmed gaps (ranked by impact)

- **G1 — "Export Complete Canoe" and "Export Extruded Freeboard & Flare" are stubs**
  (`ui/main_window.py:402-412`). The final deliverable of the whole pipeline does
  not exist yet.
- **G2 — The sweep discards the winning upper-section parameters.** The result dict
  (`main.py:505-523`) returns `t_in` but **not** mold depth, flare angle, gunwale
  width/height, or fillet ratio (they live only in the local `global_best_config`).
  Even a finished exporter couldn't know what to build.
- **G3 — Geometry exports use the seed, not the optimized hull.**
  `_export_underwater_step/_stl` regenerate offsets from the Hull Configurator
  spinbox factors (`ui/main_window.py:350-358`) instead of reading the real
  Godzilla output (`off1.mlt` / `out.mlt` via `geometry.parse_godzilla_offsets`).
- **G4 — Race results aren't usable as a guideline.** *By design decision
  (2026-07-07), the race simulator must NOT dictate the starting seed — the user
  authors the seed manually in the Hull Configurator.* But today the race-sim
  recommendation (factors/beam/draft) lives only in the console log, so there is
  nothing to compare the manual seed against. Separately,
  `HullService.write_seed_hull` hardcodes `target_vol = 0.496` m³
  (`hull_service.py:230`); the displacement for the manual seed path should come
  from crew + hull mass via config.
- **G5 — Open mass loop.** Godzilla optimizes drag at an assumed displacement; the
  structural sweep then determines the *actual* hull weight; nothing feeds that
  weight back. `generate_godzilla_input` already has an unused-in-this-loop
  `total_mass_kg` parameter. A hull optimized at the wrong displacement is not the
  lowest-drag hull.
- **G6 — Workflow is implicit.** Tab order + console hint ("Start with Step ①") is
  the only guidance; files shuttle between steps by convention (`in.mlt`,
  `out.mlt`, `off1.mlt` in cwd) with no visible pipeline state or staleness check.

---

## Phase A — Design state & step handoffs (foundation)

**A1. `CanoeDesignState` dataclass** in `domain/models.py`, persisted as
`design_state.json` via `data/session.py`:
- `seed`: factors, L, B, T, timestamp (always user-authored in the Hull
  Configurator; the race sim is reference-only and never writes the seed)
- `race_guideline` (optional): latest race-sim `best_hull` for the comparison
  panel — informational, never consumed by downstream steps
- `godzilla`: dims from `out.mlt`, offsets from `off1.mlt`, run timestamp
- `structural`: t_in, depth_in, flare_deg, gw_width, gw_height, gw_fillet,
  mixes, weight_lbs, regression version, run timestamp
- `exports`: paths of generated geometry files

Timestamps enable staleness detection (step N older than step N−1 ⇒ stale).

**A2. Return the full winning config from the sweep.** Add `depth_in`,
`flare_deg`, `gw_width_mid`, `gw_height_mid`, `gw_fillet_ratio` to the
`run_grand_optimization` result dict and thread through
`StructuralService.run_sweep` → `structural_tab` → design state.
(Flare: persist the *minimum feasible* angle found in the flare-check loop,
`main.py:194-207`, not just pass/fail.)

**A3. Race results as guideline (NOT a seed handoff).** The manual Hull
Configurator remains the sole author of the seed. Add a read-only
**"Race Sim Guideline"** panel in `ManualHullTab` showing the latest
`results["best_hull"]` (factors, beam, draft) next to the user's current
spinbox values — e.g. a two-column comparison with per-factor deltas — so the
user can consult it while shaping the seed by hand. No button ever writes race
values into the seed widgets. Independently, replace the hardcoded
`target_vol = 0.496` in the seed-displacement calculation with displacement
derived from `config.settings` crew weights + estimated hull mass (editable
field, default from last sweep or a rule-of-thumb pcf × shell area estimate).

**A4. Godzilla → downstream handoff.** On successful Godzilla exit, parse
`out.mlt`/`off1.mlt` immediately into the design state, render the optimized
hull in the 3-D preview, and plot a seed-vs-optimized drag curve R(V)
(reuse `wave_resistance.total_resistance` on both offset sets) so the user
*sees* what Godzilla bought them.

## Phase B — Complete-canoe geometry & CAD export (the core remainder)

**B1. Pure geometry builder** (new module, e.g. `structural/complete_hull.py`
or extend `geometry.py`; no I/O, no Qt):

```
build_complete_canoe(offsets_uw, L, T, depth_in, flare_deg,
                     gw_width, gw_height, gw_fillet, t_in)
  -> (outer_grid, inner_grid)   # station × waterline point grids
```

- Underwater rows: Godzilla offsets as-is.
- Freeboard rows: `geometry.parse_godzilla_offsets` currently extrudes the top
  waterline **straight up** (`geometry.py:86-100`); here the flare must be baked
  in geometrically: `y(z) = y_wl + (z − T)·tan(flare_deg)`.
- Gunwale rail: closed cross-section (width × height, fillet ratio) swept along
  the sheer line.
- Inner surface: outer offset inward by `t_in` (normal offset per section is
  sufficient at canoe curvatures).

**B2. Exporters** in `services/step_export_service.py`:
- `export_complete_stl` — **watertight** solid shell: outer skin + inner skin +
  closing band across the gunwale top. Imports directly into OnShape (STL) for
  viewing and into slicers for mold sections.
- `export_complete_wireframe_step` — full-depth station curves (underwater +
  flared freeboard + gunwale profile) via the existing CadQuery wireframe path,
  for Surface-Loft in OnShape/SolidWorks. (NURBS auto-loft stays abandoned —
  documented pole-singularity problem.)
- Wire both into the stubbed menu actions `_export_extruded` and
  `_export_complete`, sourcing parameters from `CanoeDesignState` and refusing
  with a clear message if step ③ hasn't run.

**B3. Fix G3**: all geometry exports prefer parsed `off1.mlt` offsets when the
design state has a Godzilla run; fall back to spinbox factors with a console
note "(exporting seed hull — Godzilla not run)".

**B4.** Add exported geometry files + `design_state.json` to
`export_design_package` zip.

*OnShape note:* STL imports as a graphics/mesh body (fine for viewing); the
wireframe STEP + built-in Loft gives a true parametric surface. An OnShape
REST-API/FeatureScript auto-loft is possible later but is not needed for
"viewable in OnShape".*

## Phase C — Close the mass loop (fastest-canoe correctness)

**C1.** After a sweep completes: compute actual total displacement
(`weight_lbs` + crew from config) and compare with the displacement Godzilla
optimized at. If the mismatch exceeds a tolerance (suggest 2%), surface a
one-click **"Re-converge"** action: `write_seed_hull(total_mass_kg=actual)` →
Godzilla → structural sweep. Two to three iterations converge; run through the
existing worker with the existing stop event.

**C2.** Show Δ-displacement (%) as a status chip in the structural results so
the user knows whether the hull is hydrodynamically honest.

## Phase D — Guided workflow UX (intuitive)

**D1. Pipeline stepper** across the top of the main window:
`① Seed → ② Godzilla → ③ Structural → ④ Export`, each chip
done / pending / **stale** (from design-state timestamps); clicking a chip
switches to the owning tab. This replaces tribal knowledge of tab order.

**D2. "Run Full Pipeline"** button: seed (from current configurator state) →
Godzilla → structural sweep → jumps to export dialog. Chained inside one
`WorkerThread` job with the existing progress/stop plumbing.

**D3. Save / Open Design** (File menu): the whole `CanoeDesignState` as one
`.canoe` JSON so a session survives restarts and can be shared between
teammates.

## Phase E — Tests & reporting

- **E1.** STL watertightness test (manifold-edge / Euler check on the emitted
  triangles), STEP export smoke test, golden test asserting section counts and
  flare application in `build_complete_canoe`.
- **E2.** PDF report addendum: "Final Geometry" page (t, depth, flare, gunwale,
  mixes, weight, displacement convergence) + a rendered 3-D view; update
  `docs/help/` for the new export and pipeline features.

---

## Suggested execution order

| Block | Items | Status |
|-------|-------|--------|
| 1 | A1, A2 | ✅ Done 2026-07-07 — `CanoeDesignState` (+ staleness) in `domain/models.py`, save/load in `data/session.py`, wired into Godzilla run & sweep completion; sweep result now carries depth/flare/gunwale/weight/dims |
| 2 | B1, B2, B3, B4 | ✅ Done 2026-07-07 — `geometry.build_complete_canoe`, `export_complete_stl` (verified watertight on real off1.mlt) + `export_complete_wireframe_step`, menu stubs wired, exports prefer Godzilla offsets, zip includes design state + geometry; 15 tests in `tests/test_complete_hull.py` |
| 3 | A3, A4 | Pending — race guideline panel + Godzilla→preview/drag-comparison |
| 4 | C1, C2 | Pending — displacement re-convergence loop |
| 5 | D1–D3 | Pending — pipeline stepper (design-state statuses already available), run-full-pipeline, save/open design |
| 6 | E1, E2 | E1 largely done with Block 2 (watertightness + builder tests); E2 (report page, help docs) pending |

**Known pre-existing test failures (not from this work, present before Block 1/2):**
15 failures in `test_hull_service` (write_seed_hull passes a `design_speed` kwarg
`generate_godzilla_input` doesn't accept — dead UI path, fix alongside A3's
displacement work), `test_p0` envelope closure, `test_race_path` bounded
waypoints, `test_structural_sweep` toe-load/pour-zone assertions.

Guardrails that still apply throughout: CRLF + fixed-width writes for anything
touching `in.mlt`; `ThreadPoolExecutor` only; no Qt below `ui/`; config via
`canoe_suite.cfg`.
