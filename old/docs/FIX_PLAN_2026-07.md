# Fix Plan — July 2026 Punch List

*Written 2026-07-11 from the user's prioritized issue list + code audit.
Works alongside `DESIGN_ROADMAP.md` (Phases A–E); this plan covers defect
fixes and feature requests raised after Block 2 shipped.*

Priority order: **Phase 1 (canoe-specific engineering) first — these must
work above all else.** Phases 2–4 follow.

---

## Phase 1 — Canoe-specific engineering  ★ TOP PRIORITY

### C1. Uniform shell thickness at bow/stern in the CAD export
**Symptom:** exported walls get thinner toward the tips.
**Root cause (confirmed):** `geometry.py` tip machinery (`_pinched_tip`,
`_stem_rocker_one_end` caps) scales the WHOLE section loop's y — outer and
inner chains together — so wall thickness shrinks with the same ratio as the
half-beam (→ ~0 at the tip sliver).
**Fix:** rebuild cap/tip sections instead of scaling the closed loop:
1. Scale only the **outer** chain (plan taper unchanged).
2. Recompute the **inner** chain from the scaled outer with the existing
   rules (`_offset_inward`, solid-concrete squeeze: where the section is
   narrower than the shell, inner = outer + t vertically — the tips become
   properly SOLID, which is also the physical casting reality).
3. Reassemble the loop (same point layout → loft/point-count invariants and
   watertightness machinery untouched).
**Verify:** new test measuring outer→inner distance at every exported
station ≈ t (or solid) all the way to the ends; watertightness suite stays
green; visual check in Fusion.

### C2. Hogging vs sagging — is 3-person sagging really governing?
**Finding:** the current pipeline cannot answer this. `structural.py::
calculate_moment_envelope` folds all cases into an ABSOLUTE envelope
(`max|M|`), and `evaluate_canoe_weight` applies that magnitude to both
fibers. Moment sign (hogging vs sagging) and the identity of the governing
case are lost; both fibers are checked against tensile allowables
regardless of whether they are actually in tension.
**Fix (signed-envelope refactor):**
1. Keep **signed** envelopes: `M_sag(x) = max over cases of M⁺`,
   `M_hog(x) = min over cases of M⁻`, remembering which case produced each
   extreme at each station.
2. Fiber-correct tension checks:
   - bottom fiber tension ← sagging: `M_sag·c_bot/I` vs bottom-mix tensile
   - top fiber tension ← hogging: `|M_hog|·c_top/I` (with Kt and the mesh
     allowable) vs top allowable — this is exactly the user's concern: in
     hogging the tension lives in the small gunwale/top area
   - optional compression checks vs `strength_c` (already in the DB)
3. Report per-station, per-fiber, per-case demand/capacity and a
   "governing case" trace (4P / 2P / 3P, hog / sag) in the results panel,
   envelope plots, and the PDF report.
**Verify:** unit tests with contrived seatings where 2P hogging must govern
the top fiber; regression on the existing envelope tests.

### C3. Multi-mix by depth — pour line measured from the neutral axis
**Today:** `pour_layout` is a per-station top/bottom pair split at the NA,
but the 8-cell binning forces midbody top+bottom to the same heavy mix, and
the pour-zone plot draws flat bands with no height axis.
**Fix (builds on C2's signed demands):**
1. Per-station, per-fiber mix assignment: lightest mix whose allowable
   covers the local fiber demand (drop the 8-cell coarse bins, keep the
   convergence loop — self-weight changes with assignment).
2. Compute the **pour transition height** per station: with bending stress
   σ(z) = M·(z−NA)/I, solve z\*(x) where σ falls to the lighter mix's
   allowable → a continuous pour line in real inches, not a binary NA split.
3. Weight accounting: extend `calculate_shell_properties` to return the
   cumulative shell perimeter area vs height A(z) per station so the weight
   integral splits at z\*(x) exactly.
4. Feed the same z\*(x) curve to the pour-zone plot (Tab 3 item) with a
   real y-axis in inches above keel + NA line overlay, so the team knows at
   exactly what height to switch mixes at every station.
**Verify:** golden test — heavier mix band must shrink toward low-moment
regions; weight strictly ≤ current 8-cell layout for the same hull; sweep
results remain feasible.

### C4. Constant utilization (D/C) at FOS 1.5
**Finding:** FOS is applied exactly once — allowables are `strength/FOS`
(`mix_database.py:502`), and the tab's FOS slider does reach the sweep
(`services/structural_service.py:54` sets `config.FACTOR_OF_SAFETY`).
Checks therefore enforce D/C ≤ 1.0 at 1.5 FOS, but nothing pushes
utilization UP toward 1.0 — coarse cells and discrete mix/thickness steps
leave uneven margins, which is exactly what the user's D/C plots show.
**Fix:**
1. Add utilization arrays to `StructuralResult` (per station, per fiber:
   demand / allowable) and surface them as a plot + summary (max, mean,
   spread) in Tab 3 and the report.
2. C3's per-station depth zoning is the main lever that flattens
   utilization (capacity follows demand along length AND depth).
3. Sweep tie-break: among feasible configs of equal weight class, prefer
   the one with the highest minimum utilization (least wasted material).
4. **Shear envelope correction (moved here from C2):** the evaluator's τ
   uses only the POSITIVE shear envelope (`V⁺`); the full |V| envelope
   (max(V⁺, −V⁻)) roughly doubles τ where negative shear governs and
   re-gates historically validated designs (found 2026-07-11: the mesh
   flare-gate regression scenario fails under corrected τ).  Switch to |V|
   here, where the feasibility impact is surfaced through the utilization
   plots instead of silently rejecting sweeps.
5. Stretch (separate decision): longitudinal thickness taper as a sweep
   variable — bigger change, propose only after 1–4 land.
**Verify:** utilization plot shows flattened profile vs. today's on the
same hull; all checks still pass at FOS exactly 1.5.

**Phase-1 order:** C2 → C3 → C4 (each builds on the previous), C1 in
parallel (pure geometry, independent).

---

## Phase 2 — Tab 3 (Structural) UX — tied to Phase 1 outputs

| Item | Anchor | Plan |
|---|---|---|
| 3-D hull preview shows correct bow/stern tips | `ui/hull3d_view.py:138` skips all-zero stations → blunt ends | Reuse the trimmed/stem-rocker sections from `build_complete_canoe` (or `sanitize_hull_data`) for the preview mesh so preview matches export |
| Pour-zone graph y-axis | `visualization.py::plot_pour_zones` | Delivered by C3.4: pour line z\*(x) in inches + NA overlay |
| "Active Hull file: none" never updates | `structural_tab.set_active_hull_file` / `_refresh_file_status` | Call on upload AND on Godzilla completion / design-state load; show basename + timestamp |
| Seat optimization: say where paddlers sit | optimizer already returns `best_paddlers` (4P+2P) and `coords_3` | Print per-case seat stations (ft from bow) in the results summary + markers on the moment/shear diagrams |

## Phase 3 — Tabs 1, 2, 4

**Tab 1 (Race)**
- Remove "Skip → Go to Godzilla" button (`ui/race_tab.py:150`).
- Tournament Preset row cut off at bottom → put the settings column in a
  QScrollArea / fix stretch so the preset buttons are fully visible.

**Tab 2 (Hull Configurator)**
- Hull seed preview duplicates Tab 3's: keep ONE preview (Tab 2 authors the
  seed → keep it there; Tab 3 should show the ACTIVE hull from
  off1.mlt/design state instead, which also serves the staleness story).

**Tab 4 (Mix Lab)**
- "What does publishing do?" → it snapshots the regression fit as a named
  version and marks it the one the structural sweep uses
  (`mix_database.py:245-276`). Fix = explain it in the UI: tooltip + a help
  line next to the button + help panel entry.
- Manual regression expression: new dialog to enter strength-vs-density
  coefficients directly (stored as a regression version, publishable), for
  teams with an equation but no raw cylinder data.
- Spreadsheet sharing: CSV/XLSX **import + export** of the mix log
  (openpyxl already available via fpdf2? if not, CSV first). Round-trip
  tested; dedupe on import. (Live shared sheets = out of scope; file-based
  exchange solves the team need.)
- Multi-delete mix results: list already multi-selects → make Delete act on
  `selectedItems()` with a single confirm + one DB transaction.

## Phase 4 — General UI & platform

1. **Progress bar / Stop button**: hidden unless a worker is running; solid
   accent fill (no gradient) via stylesheet; Stop shown only while running.
2. **Scrollbar contrast**: raise handle contrast in `ui/style.py`.
3. **Exportable graphs**: shared "Save chart…" (PNG/SVG) context-menu/
   toolbar for every embedded matplotlib canvas (one helper, applied at
   `_embed`).
4. **Global shortcuts**: Ctrl+1–4 switch main tabs; Esc = stop running
   computation; Ctrl+E = export current chart; F1 = help. Ctrl+Z: scoped to
   text/spin inputs (Qt built-in) — a true app-wide undo stack is out of
   scope for now; document that.
5. **JAGUAR branding**: window title, About, wizard, report header →
   "JAGUAR — James Andrea Grappe's Utility for Aquatic Resistance" + icon.
6. **Packaging**: PyInstaller spec for a Windows exe (hooks for cadquery/
   OCP, matplotlib, PyQt6; bundle mlt933w.exe + cfg template; expect
   300–600 MB with OCP). macOS build must run ON a Mac (or CI runner) —
   deliver spec + GitHub Actions workflow for both.

---

## Suggested execution order

| Block | Items | Notes |
|---|---|---|
| 1 | C2 signed envelopes + governing-case reporting | ✅ Done 2026-07-11 — signed sag/hog envelopes + per-station governing-case trace in `calculate_moment_envelope`; fiber-correct tension checks (bottom←sag, top←hog, signed principal stress) in `evaluate_canoe_weight`; utilization arrays (tension gated, compression advisory) on `StructuralResult`; governing-case console report + result keys in `main.py`; sag/hog curves on the moment plot; 7 tests in `tests/test_signed_envelopes.py`. τ still uses the V⁺-only envelope (moved to C4.4). NOTE: several worker tests were coupled to the live `concrete_mixes.db` published regression — now monkeypatched (a regression published 2026-07-11 gives 60 pcf → 12.8 psi tensile, which silently failed them and will reject real sweeps too). |
| 2 | C3 depth zoning + pour line (incl. Tab 3 pour graph) | ✅ Done 2026-07-11 — per-station/per-fiber lightest-adequate assignment replaces the 8-cell map; pour hand-off z*(x) solved from the linear stress profile (σ ≤ a − τ²/a handles diagonal tension); weight splits at z*(x) via the new `cum_area_in2` table from `calculate_shell_properties` (legacy NA split when table absent); `pour_line_in`/`sheer_line_in` on StructuralResult; pour-zone plot now draws real heights in inches (per-station bars + NA + hand-off curve); 5 tests in `tests/test_pour_zones.py` (incl. exact z* formula check and weight ≤ NA-split). `generate_2d_pour_map` (standalone helper, pre-existing failure) untouched. |
| 3 | C4 utilization reporting + tie-break | ✅ Done 2026-07-11 — τ now uses the full \|V\| envelope (C4.4, was V⁺-only); `plot_utilization` + new "Utilization" tab in structural_tab + `ASCE_Utilization.pdf`; console D/C summary (peak/mean/≥0.85 count); gate walk keeps candidates within 0.25 lb and crowns the lowest peak tension D/C (`peak_util` in best_config). Suite: 237 passed / 15 pre-existing. |
| 4 | C1 uniform tip thickness | ✅ Done 2026-07-11 — section assembly extracted into `_make_section` (single source of truth: inner wall always RE-DERIVED from the outer chain at full t, solid where halves merge); tip/cap sections rebuilt from the source station's OUTER chain via `_rebuilt_cap` (plan taper scales outer only; stem-rocker height remap on the outer; `_stem_rocker_specs` is now pure math returning raise/cap specs); raised in-sweep stations also rebuilt from their own outer.  Manifoldness guards: strictly-increasing interior heights (solid-ceiling clip collapsed points at the sheer), rail inner corner clamped strictly inboard of the inner sheer point.  3 thickness regressions in `tests/test_complete_hull.py` (33 total).  Suite: 240 passed / 15 pre-existing. **PHASE 1 COMPLETE.** |
| 5 | Phase 2 Tab-3 UX (preview tips, active file, seats) | ✅ Done 2026-07-11 — `hull3d_view._hull_to_json` rebuilt on `sanitize_hull_data` (preview now matches the exporters: no keel fin, rocker-following keel line, sharp centerline tips; 3 tests in `tests/test_hull3d_payload.py`); Active Hull File label shows basename + mtime, is claimed by uploads (`set_active_hull_file(source=…)`) and falls back to the cwd `out.mlt` instead of "none"; paddler seating displayed after every sweep (`_seats_label` + console log per case, ft from bow, drift note) and drawn as ▼ markers on the moment envelope (`plot_structural_envelope(seats=…)`, wired in tab + ASCE PDF).  Pour-graph y-axis was already delivered by C3. |
| 6 | Phase 3 Tabs 1/2/4 | ✅ Done 2026-07-11 — **Tab 1:** Skip→Godzilla button removed; preset buttons min-height 30 + row min-height 38 (fixed-26 clipped text). **Tab 2/3:** previews differentiated — Tab ③'s "Optimized Hull 3D" now auto-renders the ACTIVE hull (parsed off1.mlt via `_render_active_hull_3d`, never clobbers a sweep render); Tab ② stays the seed-authoring preview. **Tab 4:** publish explained in-UI (info label + tooltips; note: sweep uses the LIVE fit, publish is provenance EXCEPT manual versions); "Enter Regression Manually…" dialog → `set_manual_regression` (data_source='manual'; published manual is PINNED as the active relationship in `_train_regression_curves`, survives restarts/lab edits; publishing a lab fit unpins — `publish_regression` now retrains); CSV export/import with dedupe (`export/import_lab_results_csv`, one refit per import); multi-select delete (`ExtendedSelection` + `delete_lab_results` in one transaction). 7 tests in `tests/test_mix_exchange.py` (throwaway DB). Suite: 250 passed / 15 pre-existing. |
| 7 | Phase 4 general UI, shortcuts, branding | ✅ Done 2026-07-11 — audit found several items ALREADY implemented (progress bar/label/Stop hidden unless a worker runs via `_set_running`; chunk already solid; Ctrl+1–6 tab switch, Esc & Ctrl+. stop, Ctrl+R/G/E/T/U/F1 shortcuts; JAGUAR window title).  New: high-contrast QScrollBar styling in BOTH themes + `margin:0` anti-segment fix on the progress chunk (`ui/style.py`); right-click "Save chart as… (PNG/SVG/PDF)" on every embedded matplotlib canvas (`ui/chart_export.py`, wired into main_window/structural/mix/manual-hull/race tabs + replay widget); `setApplicationName/DisplayName("JAGUAR…")` in app.py + wizard title.  Ctrl+Z: Qt-native in every text/spin field; a true app-wide undo stack was scoped out. |
| 8 | Packaging (Win exe, then Mac via CI) | ✅ Delivered 2026-07-11 (build not yet run — PyInstaller not installed locally): `packaging/jaguar.spec` (one-dir bundle, scipy/cadquery/pymoo hidden imports, ui/static + docs/help datas, mlt933w.exe Windows-only, BUNDLE on macOS), `.github/workflows/build-installers.yml` (windows-latest + macos-latest matrix, artifact upload, tag/manual trigger), `packaging/README.md` (first-build checklist incl. known `_MEIPASS` lookups — ui/static already fixed in hull3d_view).  First real build will need one verification pass per the checklist. |

Guardrails throughout: CRLF fixed-width for `in.mlt` writes,
ThreadPoolExecutor only, no Qt below `ui/`, config via `canoe_suite.cfg`,
keep `tests/test_complete_hull.py` + envelope tests green.
