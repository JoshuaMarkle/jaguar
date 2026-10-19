# JAGUAR spec

## Question answered
Given a hull, a crew, and the team's concrete lab data: **what is the lightest uniform wall
(thickness t, mix density rho, mold depth D) that passes every check for every load case?**
Optionally the hull shape is searched too (`[shape_search]`).

## Interface
```
jaguar                                         desktop app (double-click): web UI on Documents/JAGUAR
jaguar check  [canoe.toml]                     validate inputs, print hull summary
jaguar run    [canoe.toml] [-o result.json]    optimize; exit 0 verified, 2 no verified design, 1 error
jaguar mix    [data/mix_tests.csv]             print lab points and the strength fit (docs/MIX_DATA.md)
jaguar gen-hull shape.toml [-o hull.json]      parametric hull (Michlet Series 7), see docs/HULLS.md
jaguar import-michlet off1.mlt out.mlt [-o hull.json]
jaguar import-mesh canoe.step|canoe.stp|canoe.stl|canoe.obj [-o hull.json] [--units ..] [--canoe canoe.toml [--analysis a.toml] [--thickness in] [--density pcf]]
jaguar export result.json|hull.json [-o canoe.step|canoe.stl] [--thickness in] [--depth in] [--flare deg] [--no-pockets] [--filled]
jaguar mold   [result.json] [-o mold/] [--in-mold] [--vertical] [--packing blocks|continuous|tight|fewest-shapes] [--foam in,in] [--foam-sheet LxW,LxW] [--board in] [--sheet LxW,LxW] [--gap in] [--nest-inside [--ring in]] [--no-holes] [--hole in] [--kerf in] [--dxf-marks] [--half-canoe]
jaguar race   [result.json] [--params canoe.toml] [-o race.json] [--measured "EVENT=SECONDS"]
jaguar serve  [canoe.toml] [-p 8000] [--prefs <path>]  web UI: 3D canoe, edit params, run + watch live
```
`serve`: the form is the only editor. Each edit is autosaved (validated, absolute paths) to
`--prefs`, default `$XDG_CONFIG_HOME/jaguar/prefs.toml` else `~/.config/jaguar/prefs.toml`, and
restored on reopen; "Reset to canoe.toml" deletes it. The mix panel charts and edits the lab CSV
(saved over `material.mix_data` after it parses and fits, previous kept as `.bak`). The last
result (`result.json` next to the canoe.toml) is shown again on reopen. Every run that finishes is
also kept in `runs/` next to it (`<UTC time>_<hull>.json` + a `.meta.json` summary); History lists
them (sorted by date, weight, race time, drag, wall or size; filtered by text, result and search kind),
opens one as the current result (charts, export, PDF, race) or deletes it. HTTP API: `src/serve.rs`.
3D view: left-drag orbits, right-, middle- or Shift-drag pans, the wheel zooms; a view cube in the
top right turns with the camera (faces BOW, STERN, PORT, STBD, TOP, BOTTOM) and a click on a face,
edge or corner turns the view there; Home returns to the starting view.
While a run is going the top bar has **Pause** (workers hold before their next candidate or hull
shape; **Resume** goes on) and **End** (`optimize::RunControl`). End on a shape search starts no new
shapes, lets those in progress finish, then verifies and reports the best one with `ended_early`;
on a fixed hull (or before the first shape finished) it cancels: no result, the last one stays.
The CLI has neither (Ctrl-C stops it). The status line shows the running time (pauses excluded)
and the time left: what is left at the rate so far, a shape search counting every refine round at
full size (`shapeopt::planned_shapes`, an upper bound), the final verification not included.

No command (double-clicking JAGUAR.exe / JAGUAR.app, `src/launch.rs`): the canoe folder is
`Documents/JAGUAR` (Windows: the Documents folder Explorer shows, also when OneDrive moved it).
The first launch writes the starter `canoe.toml` (packaging/starter/canoe.toml, the team's
settings), `off1_hull.json` and
`mix_tests.csv` (data/mix_tests.csv, the team's current lab data) into it; an existing file is never
overwritten. Then it serves that canoe.toml as `serve` does, with prefs in
`~/.config/jaguar/app-prefs.toml` (home = `$HOME`, else `%USERPROFILE%`), on the first free port of
8000-8009, and opens it in the default browser; a desktop JAGUAR already answering on one of those
ports is opened instead of starting another. The page shows "Open folder" and "Quit". The server
stops on Quit, or 5 min after the last request while no run is going (the open page pings every
30 s). Windows: a console window the double-click created is closed. Errors show in a dialog.
Downloads: `.github/workflows/release.yml` builds `JAGUAR-windows.zip` (JAGUAR.exe) and
`JAGUAR-mac.zip` (JAGUAR.app, universal, ad-hoc signed; `packaging/`) and publishes them on a `v*` tag.

## Inputs

### `canoe.toml`
`examples/canoe.toml` is the canonical, commented example; the schema is `src/params.rs`.
Sections: `[hull]`, one or more `[[load_case]]`, `[material]`, `[search]`, `[gates]`, `[punching]`,
`[stability]`, `[objective]`, optional `[shape_search]`, optional `[flotation]`, optional `[turning]`,
optional `[zones]`, optional `[stands]`, `[seats]`,
optional `[race]` (read only by `jaguar race`). Paths are relative to the toml.
Unknown keys are errors, so a typo never becomes a silent default.

### `jaguar-hull/1` (hull geometry, JSON)
```json
{
  "format": "jaguar-hull/1",
  "units": "in",
  "name": "off1",
  "source": "where the offsets came from",
  "length": 240.12,
  "draft_design": 5.91,
  "x": [0.0, ..., 240.12],              // stations, bow = 0, strictly increasing
  "z": [0.0, ..., 5.91],                // waterlines, keel = 0, strictly increasing
  "half_breadth": [[...], ...]          // [station][waterline], in, >= 0
}
```
Optional `"stem"`: written by JAGUAR on a prepared hull with a stem radius (radius, and per end
[stem x, arc top, x where the arc meets the keel], from that end's tip; Physics 0). Hull files
leave it out.
A dense rectangular offset table is what every hull tool (Michlet, CAD section cuts, spreadsheets)
can produce, needs only linear interpolation, and the browser can mesh it directly.

## Physics
0. **Templates** (optional `hull.templates = N`, the constructability simplification): the hull
   is replaced by N cross-section templates, the exact sections at cosine-spaced stations (tips
   included, closer at the ends), with a straight (ruled) loft between neighbours, i.e. the mold a
   hot-wire foam cut or frames with straight battens produces (`Hull::templated`). Fixed hull: the
   table is reduced before the search. Shape search: every generated shape is reduced before it is
   analysed, so the search tunes the buildable loft (not an approximation of a smooth optimum,
   which sits on its gates and falls off them when simplified). The winner hull, export and web
   view are the loft. Topsides follow it with the constant flare (draft everywhere).
   **Doubly symmetric** (optional `hull.doubly_symmetric = true`, applied first): bow and stern
   identical as well as port and starboard, so one end mold makes both ends. A hull file becomes
   the mean of the hull at x and at L - x, on the stations of both (`Hull::symmetrized`; an
   already symmetric hull is unchanged); a shape search ties the stern factors to the bow's
   (`shape_search.symmetric`, the same switch). Templates, stem radius, flare, the gunwale
   outline and the export treat both ends alike, so the built canoe stays symmetric.
   **Stem radius** (`hull.stem_radius_in = r`, default 4 in, 0 = sharp, applied after templates;
   old name `keel_fillet_radius_in` still reads): in side view, the corner where the stem meets
   the keel line at the bow and at the stern (the forefoot) is rounded on an arc of radius r
   tangent to the stem (the last station without breadth) and resting on the keel line K(x)
   (Physics 1) within 2 r: centre height zc = max over the keel there of K + sqrt(r^2 - dx^2), the
   arc meeting K (tangent) where that maximum is (`Hull::filleted`, `forefoot`). From the stem to
   that point the keel line is the arc K'(x), vertical at the stem; there each column of the grid
   is its uncut surface (table loft, then the topsides) stretched from K .. D onto K' .. D,
   z -> K + (z - K')(D - K)/(D - K') (`Grid::cut_stem`): every section keeps its shape and its
   pointed bottom, every waterline stays pointed, the top of the stem, the gunwale, length overall
   and everything aft of the arc are unchanged, concrete is only removed, nothing folds. The hull
   table itself is kept; the arcs ride along in the hull JSON as `stem` (kept in result.json's
   `winner_hull`). An arc may reach above the design waterline (off1: stem foot about z 4.3, r 4
   reaches z 8.3), where the grid gets a row every r/8; a mold shallower than the arc top fails
   that candidate with the reason (a shape search goes on). Weight, displacement, freeboard, GM,
   D/C, flotation and drag all read the cut grid; the export draws it (24 more stations over each
   arc, closest at the stem). off1 at r 4 vs sharp: 98.18 vs 98.24 lb, freeboard 6.666 vs 6.667 in, GM 1.070 vs 1.071 in,
   drag 31.81 N both, punching D/C 0.904 both.
1. **Grid**: resample to `hull.stations` evenly spaced x on the table's loft (`hull::Loft`): each
   station with breadth has a keel height (the parabola z = k + c y^2 through its two lowest
   offsets, kept between the last zero waterline and the first non-zero one); the keel line K(x)
   is the monotone cubic (Fritsch-Carlson PCHIP: interior slopes the weighted harmonic mean of the
   neighbouring secants, 0 at an extremum; end slopes the end secants) through them, continued
   straight with the end slopes to the stems and kept within the table (C1, no steps). A section
   is its offsets, linear between waterlines, and below its lowest offset a straight line down to
   its keel. Between stations a and b (fraction t, T the table top) the half-breadth at z >= K(x)
   is (1 - t) Y_a(K_a + s (T - K_a)) + t Y_b(K_b + s (T - K_b)), s = (z - K)/(T - K): sections
   stretched between the keel line and the top, so the keel follows K(x) instead of stepping from
   waterline to waterline, and the table top is the plain linear blend. 0 below K. (Up to
   2026-10-15 the table was blended at fixed heights: the keel sat at the lower station's height
   for a whole interval and then stepped, up to 0.4 in on off1.) Topsides above `draft_design` are
   extended to the mold depth D (10 extra waterlines), vertical or leaning out by `hull.flare_deg`,
   eased in near the tips so the stem stays a sharp line up to the gunwale: with F = (D - draft)
   tan(flare), w = max(4 in, 2F) and v = waterline half-breadth / w, the gunwale offset is
   F v (2 - v) up to v = 1, then F (`hull::flared`): smooth in top view, leaving the tip at
   (1 + 2F/w) times the waterline's slope (team winner, 3 deg: gunwale 19.6 -> 16.8 deg over the
   first 15 in), with less side lean there. Until 2026-10-18 a tip with a sliver of waterline got
   the full flare: a flat face 2F wide above the water, 0.74 in at 3 deg over 7 in. Unchanged
   wherever the waterline half-breadth is >= w.
   Optional constructable gunwale (`hull.gunwale_radius_in` = R, or `shape_search.gunwale_radius_in`
   = [min, max] searched as a shape variable): in top view the gunwale runs straight from the bow
   tip, tangent onto one arc of radius R, then straight to the stern tip (`hull::Gunwale`); each
   side is ruled from the design waterline up to it. The arc peaks at the middle of the widest
   waterline stations; its half-beam is the smallest that clears the design waterline widened by
   (D - draft) tan(flare) wherever it has breadth, so every side leans out at least the flare (the
   demolding draft; checked at the table stations, exact since the outline is concave). Every candidate of a depth fails, with the reason, when no outline of
   radius R fits or the sides would lean out more than `hull.gunwale_max_flare_deg` (default 30)
   anywhere (401 even stations). Off = byte-identical to before.
   **Flare as a search axis** (optional `search.flare_deg = [min, max, step]`, then `hull.flare_deg`
   must be 0): each (depth, flare) is its own topsides; candidates record `flare_deg`, ordered
   (thickness, density, depth, flare). More flare only adds wall, so it is chosen only when a gate
   needs it; the low end is reported as the "demolding draft" limit. Each value multiplies the
   wall grid (a shape search with 6 flares took 120 s). Export and the web view build the same surface.
2. **Section** per station: thin shell of thickness t on both sides + keel strip: A, zbar, I. The
   girth follows the hull: waterline segments with zero half-breadth at both ends (below the
   rockered keel, the closed stem) are not wall. Same rule for the wetted surface in drag.
3. **Equilibrium**: self weight w = A*rho/1728 lb/in. Crew = weight * `dynamic_load_factor`, each
   paddler on a `knee_patch_in` patch at their seat. Draft and trim
   are solved (Newton, bracketing fallback) so fresh-water buoyancy (62.4 pcf) balances force and moment, then
   q -> V -> M by integration. Free-free closure: |V(L)| <= 1e-3 W, |M(L)| <= 1e-3 W L.
   Also gives loaded freeboard (D minus local draft, min over stations) and transverse GM_T
   (crew CG at `stability.crew_cg_in`).
   **Seats** (`src/seats.rs`): `seats_in` if given; else `[seats] auto = true` (default) picks knee
   positions and crew order (every distinct order, <= 4 unpinned paddlers) minimizing the highest
   seat-dependent utilization max(D/C without punching / dc_search, 1 - (fb - fb_min)/s,
   1 - (GM - gm_min)/s), s = max(min, 1 in); knees >= `spacing_min_in` apart within
   [`bow_clear_in`, L - `stern_clear_in`]. Solved once per (t, depth) at the middle density of the
   axis and reused for every density and for verify. `auto = false`: evenly spaced L(i+1)/(k+1).
   `positions` (per paddler, `weights_lb` order; not with `seats_in`): seat number 1 = bow ... k =
   stern pins that paddler there, 0 = free. Auto tries only orders that keep the pins (and even
   spacing in the entered order first); evenly spaced seats fill the free seats with the free
   paddlers in entered order. Pinned to the same seat, a number outside 0..k, or a length other
   than weights_lb: input error.
   Every case is then checked at the nominal seats and all 2^k corners with each knee moved
   +/- `variance_in` (default 3 in); the case verdict is the worst sample, GM the minimum.
   Search candidates stop sampling at the first sample that misses a gate.
   **Freeboard** is a static check at staging (RFP), so it has its own seats: the gate sees the
   lowest freeboard over the marked seats and the whole crew moved +/- `gates.freeboard_seat_shift_in`
   (default 0 = marked seats; freeboard depends on the crew only through its centroid, so the
   all-forward and all-aft shifts are the extremes), where the gate looks: with `[lettering]`, over
   the lettered spans (the school name from `end_margin_in` behind the bow, the canoe name ending
   `end_margin_in` ahead of the stern, each character `width_ratio` x `height_in` long; names that
   run off the hull or overlap: the case errors), else the whole hull. The local freeboard
   D - (d + trim (x - L/2)/L) is linear in x, so the lowest is at a span end. The gate is
   >= `freeboard_min_in` + `freeboard_margin_in` (the RFP's 6 in less its 0.25 in tolerance, plus
   a staging margin). Auto seats score freeboard the same way. Each case reports `freeboard`:
   `gate`, and `expected` (marked seats), `best`, `worst` over the strength seat samples (how far
   the crew's position moves it), each with seats, draft, trim, the x and waterline of its lowest
   point, the gated and the hull-wide (`hull_min_in`) freeboard; plus `required_in`,
   `seat_shift_in` and the `lettering` spans.
4. **Material** (`src/mix.rs`, docs/MIX_DATA.md): least-squares line of FINAL split tensile strength
   vs density from the lab CSV. Flexural and f'c use their own least-squares lines when the CSV's
   optional `flexural_psi` / `compressive_psi` columns have data at >= 2 densities (under
   tested_mixes each lab row is its own mix with its own values, never averaged), else flexural = `flex_over_split` * f_sp, f'c = (f_sp/6.7)^2.
   Densities are searched only within `density_extrapolation_pcf` of the tested range of every
   fitted line (split and each measured one; none below the lightest test of a line with a
   negative slope), or only at
   tested mixes with `strength_model = "tested_mixes"`. `material.method = "asd"` (default): service
   loads (crew x `dynamic_load_factor`), every capacity divided by `factor_of_safety`. `"lrfd"`:
   the ASCE 7-22 2.3.1 gravity combinations, each load case re-solved for `lrfd_dead` D +
   `lrfd_live` L and `lrfd_dead_only` D alone (defaults 1.2D + 1.6L and 1.4D; D = self weight,
   L = crew x dynamic factor, impact being part of the live load before factoring, ASCE 7 4.6;
   buoyancy is a fluid load factored like D, balancing the factored weight) and the worse
   combination governs (the removed `lrfd_dead_min`, 0.9D + 1.6L, is refused); capacities x phi (0.60 plain concrete, 0.75
   for `aci_22_6` punching), never both. Freeboard, GM, draft and drag always use the real crew
   weight (no dynamic factor, no load factor): the freeboard gate already holds its margin.
5. **Checks** (`src/check.rs`; governing D/C = max):
   * bending tension: |M| c / I at the tension fibre (bottom, c = zbar, when sagging M > 0; top,
     c = D - zbar, when hogging), vs flexural strength
     (stations with I > 1e-3 max I)
   * bending compression vs 0.85 f'c
   * shell shear VQ/(I*2t) vs 2 sqrt(f'c)
   * knee punching for the heaviest paddler, ACI 318-19 Table 22.6.5.2 or plain Table 14.5.5.1,
     one or two knees, the heavier carrying `knee_split` (default 0.5, even) (docs/PUNCHING.md). Effective depth d (knee/inside face to mesh) comes from
     `[punching] layers`, `mesh_after_layer` (k) and `mold`: male d = k/layers t, female
     d = (layers-k)/layers t. `mesh_after_layer` may list several meshes (`[1, 2]`); the one
     farthest from the knee face sets d. `d_over_t` is an explicit override and an error together with them;
     with neither, 2 layers and mesh after 1 (d = t/2).
   * biaxial (Kupfer-Gerstle): per station, principal stresses of the bending fibre stress (both
     fibres) with that station's shell shear and, with `[turning]` (`speed_mps`, `radius_in`), the
     transverse turning stress at both signs; sigma_1 <= f_t/FS (1 - 0.8 |min(sigma_2, 0)| /
     (f'c/FS)), f_t = flexural strength. Turning: k = V^2/(gR), the floated weight W (hull + crew,
     as factored) is reacted by a uniform lateral pressure p = k W / A_lat on one side's underwater
     profile (h = local waterline - keel), each side a cantilever from the keel:
     sigma_y = 3 p h^2 / t^2 (`check::hoop_psi`). Absent = sigma_y 0
6. **Gates**: a candidate passes iff D/C <= `dc_search`, gated freeboard (step 3) >= `freeboard_min_in` + `freeboard_margin_in` and
   GM_T >= `gm_min_in` for every load case (and seat sample), the empty canoe (no crew) has GM_T >=
   `stability.gm_empty_min_in` when that is given (optional; always computed: candidate `gm_empty_in`,
   per-case `gm_cases_in`, winner `empty.gm_in`), and, with `[flotation]`, the canoe floats swamped
   (`src/flotation.rs`): wall volume + sealed bow and stern air pockets (equal length l from each
   tip, full hull interior up to D = enclosed area minus wall area) displace >= `swamp_factor` x self weight at 62.4 pcf. l is found
   by bisection and reported as `air_pocket_in`; l > `air_pocket_max_in` fails with a reason. Thicknesses below `thickness_min_buildable_in` are
   never tried.
7. **Objective** (`objective.kind`, default `drag` when omitted; drag is computed and reported for every candidate whatever the objective): `weight` = lightest passing; `drag` = lowest Michell wave +
   friction resistance at `speed_mps` (`src/resistance.rs`; use it to rank shapes, not as a
   race prediction). Friction = 0.5 rho V^2 S Cf on the wetted surface S, Re = V L / nu, Cf = the
   larger of ITTC-57 (smooth) and, for `hull.roughness_um` = ks > 0 (equivalent sand roughness of
   the finished surface, default 0 = smooth), Schlichting's fully rough plate
   (1.89 + 1.62 log10(L / ks))^-2.5; no form factor. The same drag feeds the race and the power
   curves. Drag is taken at the heaviest load case's loaded draft, or, with
   `objective.displacements = [{crew_lb, weight}, ...]` (weights sum to 1), as the weighted sum of
   resistance at the level draft each (hull weight + crew_lb) floats at. Each entry gives `crew_lb`
   or `load_case` (a `[[load_case]]` name: its crew total, like a race event's), not both. `objective.straight_weight`
   (default 1, needs drag + `[shape_search]`) blends in the steady-turn drag at `objective.turn_radius_in`
   (default 600), see docs/SHAPE_SEARCH.md. `race` (needs `[race]` and `[shape_search]`) = shortest
   weighted race time over the `[race]` events on paths planned for the midpoint shape
   (`candidate.race_s`); the winner is re-raced on its own paths (`result.race`, a
   `jaguar-race/1` record). The inner wall search ranks by weight (on one shape the lightest
   wall is also the fastest).
8. **Verify**: the winner is re-run at 2n-1 and 4n-3 stations (spacing h/2, h/4; same seats).
   Each (load case, check) D/C series, max over the seat samples, gets Richardson extrapolation
   (`optimize::richardson`): converged (steps < 1e-6 relative) = the finest value; monotone and
   contracting with observed order p = log2(d1/d2) in [0.5, 4] = f3 + d2/(2^p - 1); else
   irregular. `dc_extrapolated` = max over the regular series. It is used when every irregular
   series stays below it even at max + 2 x its largest step; then VERIFIED iff
   max(dc_extrapolated, D/C at 4n-3) <= `dc_verify` (never below a computed value). Otherwise
   `dc_extrapolated` is null, `convergence.note` says which series, and VERIFIED iff the 2n-1 D/C
   <= `dc_verify`. The search gate `dc_search` < `dc_verify` keeps its margin. The floating gates
   are re-checked at 2n-1 stations too: VERIFIED also needs freeboard >= `freeboard_min_in` + `freeboard_margin_in` and
   loaded GM >= `gm_min_in` there (a design the search passed by a hair can miss them).
   `verify_failure` says why when not verified.
8b. **Stands** (optional `[stands]`, `src/stands.rs`; `optimize::verify_stands`, called once by
   `jaguar::run` after either search, never a search gate): the winner at 2n-1 stations, empty,
   on two `pad_in` (default 6) pads centred x_s from each end. Load: the real self weight A(x)
   rho/1728 x the dead-only factor (asd 1, lrfd `lrfd_dead_only`); reactions from force and
   moment balance; each spread over its pad in closed form. `orientation` = upright (keel on
   the stands) or inverted (on the gunwales: checks see -M). Checks: bending, compression,
   shear, biaxial as in 5 (no turning) and, at the heavier pad, `stand_punching` (upright:
   ACI 22.6 on a pad x pad square, b0 = 4 (c + d), or Table 14.5.5.1 with `plain_14_5`) or
   `stand_bearing` (inverted: 0.85 f'c on A1 = 2 t pad, phi 0.65 lrfd). x_s is swept from pad/2
   to L/2 - pad/2 in `step_in` (default 0.5) steps; the kept position has the lowest beam D/C
   (bending, shear, biaxial: the pad check does not depend on position on a symmetric hull);
   uniform weight gives 0.207 L. A stands D/C > `dc_verify`, or a stands case that cannot be
   computed (e.g. a two-mix wall), makes the winner NOT VERIFIED with the reason. Also after
   the search: the heaviest paddler's punching D/C at knee splits 0.5, 0.6 and 1.0.
8c. **Curves** (`src/curves.rs`, `optimize::winner_curves`, once after the search; pictures, never
   gated): effective power P = R(v) v (Michell + ITTC, level draft of hull + real crew) at 3 to 11
   ft/s per load case; righting lever GZ(phi) at 0 to 180 deg in 2 deg steps per load case and
   empty, each section closed at the gunwale and cut by the heeled waterline, h solved for
   buoyancy = weight, GZ = Y_B - Y_G (G on the centreline at the GM check's KG), level trim. Per
   curve: max GZ and its angle, the angle GZ stops being positive, and `gunwale_immersion_deg`
   (first angle a gunwale is under: an open canoe takes water there). A curve that cannot be
   computed carries `error`, never zeros. The web UI plots both (Performance tab) and overlays
   other result.json files for comparison.
9. **Shape search** (optional, `src/shapeopt.rs`, docs/SHAPE_SEARCH.md): Latin hypercube + pattern
   search over Michlet Series 7 shape factors within bounds. Each shape runs the wall search above,
   and the best objective wins. `beam_in = [min, max]`: the waterline beam is sampled in it, and a
   wall candidate whose overall beam (flared topsides and gunwale at its depth included) exceeds
   max fails with the reason. Optional manufacturability rules: `draft_step_in`, `length_step_in`,
   `beam_step_in` (snap), `rocker_in = [min, max]` and `min_entry_angle_deg` (reject, with the reason
   in the candidate), `symmetric` (aft factors = fore; same switch as `hull.doubly_symmetric`). Each shape's `shape.metrics` (entry angle,
   rocker in, lateral moment, rejection reason) is in result.json.
10. **Two-mix zones** (optional `[zones]`, `optimize::zoned_search`): the wall search also tries
   walls of two mixes: the light mix (`search.density_pcf`) everywhere except side-view rectangles
   of the dense mix (`dense_pcf`, heavier than the light one), x from the bow and z above the keel,
   each covering the whole girth between its heights. Every edge is a multiple of `increment_in`
   (default 1/8 in; a rectangle may also end at a hull tip). Sections are transformed (light =
   reference, n = E_dense / E_light, E = 33 w^1.5 sqrt(f'c), ACI 318-19 19.2.2.1); self weight and
   its centroid use each mix's density. Bending is checked at each mix's extreme fibres,
   sigma = n M (z - zbar) / I, against that mix's strengths; shell shear at the neutral axis against
   the mix there; Kupfer per mix. Punching uses the dense mix only when every knee's footprint is
   dense over its whole seat range: x = seat -/+ (seat variance + (patch + d)/2), z from the keel up
   to where the hull is (knee spacing / 2 + (patch + d)/2) wide each side; else the light mix.
   Placement (`place_zones`) per (t, depth, flare, light, dense), seats from that wall's single-mix
   plan: start all light; if the light mix fails punching, make every knee footprint dense (top
   rounded up to the increment, plus one increment of margin); then repeat: while a D/C check over
   `dc_search` fails in the light mix, make that fibre dense at that station (0.5 in in from a
   bending fibre, around the neutral axis for shear; a check failing in the dense mix stops it);
   while only GM is short, add a dense keel strip z 0..h centred on midships, 24 in longer per
   round, h up one 0.5 in step once it spans the hull. Freeboard failures stop it (dense only adds
   weight). An all-light wall that passes is the single-mix candidate and is not repeated.
   Combinations go lightest all-light weight first, in batches of 32; one whose all-light weight is
   not below the lightest pass so far cannot win and is not placed; at most `max_checks`
   (default 400) are placed. The winner is the best passing wall of either kind by the objective,
   verified as above; its `calc` is an `error` (the worked calculation covers single-mix walls).
   The result reports the saving against the single-mix pick of the same run. A fixed-mold
   re-sweep with new mixes is `run` on a fixed hull with `[zones]` after updating the mix data.

## Output: `jaguar-result/1` (JSON)
`{format, params, strength (mix fit + points; `flexural_fit` / `compressive_fit` only when
measured), winner_hull (shape runs) | null, candidates: [...],
winner: {candidate, verify_stations, verify_dc, verified, dc_extrapolated, dc_order, convergence
{stations, dc, extrapolated, order, governing, note}, on_grid_edge, at_mix_data_edge,
mix_provenance, dense_mix_provenance, search_space, diagrams, cases, empty, calc, verify_failure,
stands {error | orientation, pad_in, dead_factor, x_stand_in, x_over_l, spacing_in, reactions_lb,
sag_max_lbin, x_sag_in, hog_max_lbin, x_hog_in, x_in, shear_lb, moment_lbin, verdict, local,
beam_dc, sweep_*}, knee_split_sensitivity [{knee_split, load_lb, demand_psi, capacity_psi, dc}],
curves {error | speeds_fps, power [{name, crew_lb, displacement_lb, draft_in, drag_n, power_w}],
angles_deg, gz [{name, weight_lb, kg_in, gz_in, max_gz_in, max_gz_deg, vanishing_deg,
gunwale_immersion_deg}]}}
| null, closest, zones, ended_early}`.
`closest` (only when no design passes): `{fails, design}`, the candidate that misses the gates by
the least (largest relative miss over D/C, freeboard, loaded and empty GM; the 16 best by the
search's early-exit numbers are re-checked with every case and gate), analysed in full like a
winner (`design` has the winner's fields: every case, stands, curves, worked proof); `fails` lists
every gate it misses with its numbers, plus a failed verification. It is never the answer: `winner`
stays null, the terminal prints it as `CLOSEST DESIGN (FAILS)`, the web UI and PDF mark it FAILS,
`jaguar race` races it with a note, and STEP/STL export refuses it. `winner_hull` is then its hull.
`ended_early` (only when a shape search was ended from the web UI): how far it got, e.g. "ended by
the user after 812 shapes (812 of 4000 samples, 0 of 25 refine rounds started)"; the winner is the
best shape checked, verified as usual. The terminal adds `ENDED EARLY` under WINNER.
With `[zones]`: a zoned candidate has `zones` {light_pcf, dense_pcf, dense: [{x0_in, x1_in,
z0_in, z1_in}]} (disjoint rectangles; its `density_pcf` is the light mix) and `dense_mix` (tested
mixes); the winner has `dense_mix_provenance`; `search_space` adds `zones.dense_pcf`; the result's
`zones` has combinations (`walls`; with none, `note` says why, e.g. no dense mix heavier than a light one), skipped (`heavier`, `punching`), `checked` (placed), `freeboard` (walls that miss freeboard, which dense concrete cannot fix: skipped when their single-mix wall already misses it, or placed and ending short), `capped`,
`single_mix_lb` (the single-mix pick, null if none passes) and `saved_lb` (single_mix_lb - winner
weight; 0 when the winner is single-mix). Zoned candidates follow the single-mix ones. Zoned D/C(x)
and stresses are the governing mix's; `inertia_in4` is transformed. The terminal lists the
rectangles as mold marks (`60 1/8`) and the saving. `mix_provenance`: the winner density is
`tested` (a tested mix within 0.05 pcf, or the tested mix itself) or `predicted` (fit line only),
the nearest tested mixes below/above it with their signed distance, and `reach_pcf` (distance to the
nearest). `search_space`: per searched variable (thickness, density, depth, then each free
`[shape_search]` factor) the min, max, step and count actually tried, the winner value, the bound it
sits on (`min`/`max`) and `limit` when that bound is real (`buildable floor`, `mix data`,
`tested mixes`). `on_grid_edge` = a wall variable on a bound with no limit; shape bounds are listed,
not flagged. `diagrams` is the governing case at verification resolution. `cases` has one entry
per `[[load_case]]` (input order): name, nominal `seats_in` (`weights_lb` order), `crew_order`
(paddler indices bow to stern), `seat_source` (given/even/auto), `seat_variance_in`,
`verdict_seats_in` (the sample the verdict comes from), weights, draft, trim (nominal seats), GM,
freeboard (gated; `freeboard` = gate / expected / best / worst), x, V, M, tension/compression/shear stress, waterline, `zbar_in`,
`buoyancy_lbpin` and `self_weight_lbpin` (strength combination, lb/in; their difference integrates
to V, the knee patches added in closed form), and
per-station D/C for bending tension, bending compression, shear and biaxial (Kupfer-Gerstle; 0 at
masked tip stations, same formulas as the checks; nominal seats), plus that case's verdict. A case whose equilibrium fails carries `error`
and no numbers. `empty` floats the bare hull (no crew): draft, trim, min freeboard, x and
waterline, or `error` and no numbers. `calc` is the worked calculation of the governing case at
its worst seat sample and governing combination: factors, weights, both equilibria (with KB, BM,
KG), strength fit values, and per check the station, section values (M, V, A, zbar, I, c, Q, b)
or punching intermediates (d_load, depth, b0, P, v_u, v_c) with capacity and D/C; recomputed by
the checks' own code so each demand and capacity equals that case's verdict (or `error`).
`case_calcs` is the same for every load case (input order; empty for a zoned wall). The terminal prints one line per case under the winner (seats bow to stern as
knee@lb, source, variance, D/C), the mix provenance
line and the search-space table (shape factors inside their bounds are counted, not listed). Each candidate has t, rho, mix, depth, flare, length, beam (overall, gunwale included; a shape run's `shape.beam_in` is its waterline beam), shape, weight, GM, drag, race_s and the
per-case verdict (every check's demand, capacity, D/C). Shape runs list one candidate per shape.
Schema: `src/optimize.rs`. Ordering is deterministic (thickness, density, depth ascending), and the
same input gives byte-identical output. The terminal prints a short summary; the JSON is the record.

## CAD import (`src/meshimport.rs`)
`jaguar import-mesh` (and the web form's "Import CAD file", `POST /api/import-mesh`) reads a canoe
drawn in CAD, as a STEP file or an STL (binary or ASCII) or OBJ mesh, and writes a jaguar-hull/1
table. A STEP B-rep (`src/stepimport.rs`: faces on planes, B-splines incl. rational, cylinders,
cones, spheres, tori; trimmed faces and holes; edges shared between faces are sampled once, so the
mesh has no gaps) is meshed into triangles about 1/2 in across, boundary chords within 0.005 in of
the surface; a boundary point more than 0.02 in off its surface, a face wrapping round a closed
surface with no seam, or parts placed by an assembly transform are refused (export STL instead).
Units (in/mm/cm/m/ft: the unit a STEP file declares, else detected from a 12-30 ft length, or
given), axes (length = longest side, up =
shortest, keel at the narrower end; or given), bow at the low end of the length axis (or
`--bow-at-max`). Each of `--stations` x `--waterlines` cuts takes the outer crossing on each side
(their mean; the difference is reported as asymmetry). The table runs from the keel to the lowest
gunwale over the middle 80% (a sheer rising toward the ends is reported, not analysed). A solid
mesh's wall is measured from the inner crossing (normal to the wall; median, min, max over the
middle) and cross-checked by 2 x volume / area; a surface mesh has none. With `--canoe`, it also
writes a canoe.toml that analyses the design as drawn: that hull with no flare, templates, stem
radius, gunwale outline or shape search (the CAD shape is the shape), search pinned to the drawn
wall (or `--thickness`) and depth, and to `--density` if given (else the mix range: the lightest
passing mix). `jaguar run` then gives every check, drag, stability and flotation; a single pinned
design that fails is shown with all its checks (it is the design asked about, not a fallback), and
`jaguar race` races it (with a note). The web import also turns shape search off and, if the
objective is race time, sets it to weight (the Race tab races the result). Verified on JAGUAR's own
exported solids (STL facets, and the B-spline STEP with air pockets): wall within 0.02 in (STEP 0.01),
offsets within 0.15 in, the off1 winner reproduced (weight within 2%, punching D/C within 0.005;
GM within 0.05 in, which is enough to cross off1's GM gate at 0.25).

## Export (`src/export.rs`)
`jaguar export` writes the canoe for CAD and the mold shop, in inches. From a result.json it takes
the winner's hull (generated or `hull.file`), wall thickness, mold depth and flare; from a hull.json
the outer mold surface to `--depth` (add `--thickness` for the solid wall). Same surface as the
analysis grid, both sides mirrored, each station's girth resampled to `--girth` points.
* **STEP** (AP214): a closed `MANIFOLD_SOLID_BREP` (`BREP_WITH_VOIDS` with air pockets) for a
  wall, or an `OPEN_SHELL` surface model for the mold surface, of smooth faces: the outer hull,
  the inner wall and each pocket's void wall are cubic B-spline surfaces (a starboard and a port
  half each, clamped uniform knots, control net = the exported stations x girth points), the
  top at the mold depth (gunwale, rim and solid ends, one face with the opening as its hole), the
  bulkheads closing the hollow, the pocket lids and caps are planes. Neighbouring faces share
  their boundary curves (the same B-spline curve: the keel, the stem lines, the sheers, the end
  sections), so the solid is watertight by construction. Verified in OpenCASCADE 7.x (OCP):
  valid (BRepCheck), closed shells with no free edges, no self-intersection
  (BOPAlgo_ArgumentAnalyzer), volume within 0.25% of the mesh (the B-spline is smoothed through
  its control net). A hull whose ends are not stem lines, or whose inner wall does not reach the
  mold depth at the sheer, is written as planar facets instead.
* **STL** (binary): the same triangles, watertight for a wall.
* The keel line is the loft's K(x) (Physics 1), with the stem arcs: smooth (C1) from stem to stem,
  never snapped to waterlines, and nothing on the centreline below it.
* The inner face is the mold line offset inward by t, point for point (mitred), with the offset
  stretched where the hull tapers along x so the wall is t thick normal to the surface (off1 at
  t = 0.375: within 1% over the middle 80% of the length). At the bow and stern, where that offset
  closes, the ends are solid concrete closed by a bulkhead. Its volume matches the analysis wall
  within 0.1% on off1 at t = 0.375 (the analysis thin shell is about 1.6% heavy amidships, the
  solid ends make up the difference).
* **Air pockets**: when the winner has `air_pocket_in` (`[flotation]`), the wall gets a solid
  bulkhead one casting layer thick inboard of each sealed pocket: b = t / `punching.layers` (2 when
  no layer fields are given; t with an explicit `d_over_t`, which gives no pass count), so a 3/8 in
  wall cast in 3 layers gets 1/8 in bulkheads. They run from x0 + l to x0 + l + b and from
  xn - l - b to xn - l (x0, xn the hull's ends, l = `air_pocket_in`). A top cover as thick closes
  each pocket at the mold depth (the interior from D - b up, from the solid end to the bulkhead),
  so each pocket is a sealed void: one solid with an inner shell per pocket (STEP
  `BREP_WITH_VOIDS`; STL simply has the extra shells), so the mold shop sees where the foam/sealed
  volume goes. Bulkheads and covers are not in the analysis weight (10 in^3 on the
  examples/canoe.toml winner, 2 layers). `--no-pockets` leaves them out. A
  pocket shorter than the solid end at the export's station spacing is an error (use more
  `--stations`, or `--no-pockets`).
* A flat bottom (lowest offset at the keel height) keeps its chine corner.
* **Unshelled** (`--filled`): the outer hull filled in, closed by a flat deck at the mold depth
  (and a planar end over a station that is not a stem line), one solid with no wall or pockets:
  for measuring freeboard and displacement in CAD. STEP: the outer B-spline faces and the deck
  plane as a `MANIFOLD_SOLID_BREP`. Its volume is the analysis grid's to the mold depth within 1%.
* `GET /api/export?format=step|stl[&shell=pockets|wall|filled]` in the web UI exports the last
  run's winner. The Export STEP / Export STL buttons give the complete shelled canoe with its air
  pockets (`pockets`, the default; stations refined below the pocket length); their dropdown arrow
  also gives the shelled wall without pockets (`wall`, as `--no-pockets`, file `<hull>_no_pockets`)
  and the unshelled canoe (`filled`, as `--filled`, file `<hull>_filled`).
* **PDF report** (web UI only, `Export PDF` beside STEP/STL): built in the browser from the last
  result and printed via the print dialog's "Save as PDF" (no server endpoint, no dependency).
  Page 1: snapshot of the 3D canoe top left, weight and verification, winner stats, checks, load
  cases, stands, and the race results per event when this winner was raced. Then search space, station convergence, mix fit and lab points, every input and every
  chart (all cases shown), then the design proof: (0) every check of every case and every gate
  (freeboard, GM, empty GM, convergence, stands, flotation) with demand, capacity, D/C or value,
  the requirement and OK/FAILS, and a conclusion; (1) design basis: load and resistance factors,
  model and assumptions; (2) the split tensile least-squares fit worked from the lab points, the
  strengths at the winning density and every design capacity; (3) section properties (A, zbar, I,
  S top/bottom, self weight) every L/20; (4) one page per load case from `case_calcs`: crew
  (weights, pinned seats, order, nominal and worst-sample knees), service and factored weight,
  draft, trim, freeboard and GM worked, the load table every L/20 plus each critical station
  (w, b, q, V, M, stresses, beam D/C) with the equilibrium closure V(L), M(L), every check worked
  with its numbers, and punching for every paddler; (5) station convergence, empty canoe, stands
  and flotation worked, and the knee-split sensitivity; (6) drag at the objective speed for every
  load case (draft, L, S, Fn, Re, Cf smooth / rough / used, friction, wave, total; formulas and the
  heaviest crew worked; the objective's weighted sum), from `curves.drag` / `curves.objective_drag`;
  (7) race time: T_w = sum w_e t_e, the momentum equation, thrust, turning-drag and path models
  with their parameters, every event's mass, draft, power, path, straight / arc split, speeds,
  drag and thrust at top speed, time and w t, the thrust = drag check and the sensitivities.
  Export PDF races the design first when it has a `[race]` and was not raced. No per-candidate table: result.json is
  the full record.
  Each page carries a small "Generated by JAGUAR" footer.

## Mold hardboard (`src/manufacture.rs`)
`jaguar mold` and the web Manufacture tab cut hardboard on the water jet for the winner's MALE
foam mold. The mold surface is the canoe's inner wall (the export's inner face: hull offset inward
by t, solid concrete ends left out, no air pocket bulkheads), built upside down with the gunwale on
the floor from foam sheets, cut and sanded by hand. `--foam` lists the thicknesses on hand
(default 2 in; e.g. `1.5,2`): the layers use the count of each that covers the mold (flat: floor
to the inner keel; upright: the hollow run, boards included in the mold) with the least left over,
then the fewest layers, thickest on the floor and thinnest at the keel (upright: thin slabs at the
two ends); `stack.foam_order_in`, `stack.over_in`. `--foam-sheet` lists the foam sheet sizes
(default 96x48): every foam piece fits one of them.
* **Tracing templates** (default): the mold is foam only. Each foam layer gets hardboard templates
  to trace it on the foam sheets: layer k (k = 0 on the floor) from h = k foam to (k + 1) foam,
  template = the envelope of the waterlines from its underside to its top face, every 1/4 in
  (what the layer covers in plan; foam cut oversize, never short), each tip bridged over any
  inward dip within 12 in of its point (the upper hull there: no neck or spike), the top
  face's outline etched as the sanding line. A layer longer than a foam sheet is several foam
  pieces; each template piece is one foam piece (its ends are the foam joints). Layer k starts
  at the sum of the layers below it.
* **Boards in the mold** (`--in-mold`): a hardboard sheet (`--board`, default 1/8 in) between each
  pair of foam layers, board k at h = k (foam + board), cut to the waterline at its mid-plane.
* **Flat** (default): outlines are waterlines of the inner surface, symmetric about the
  centreline, layers until the keel; a centripetal Catmull-Rom spline through the waterline at the
  hull table stations (the loft's linear blend between stations kinks it near the keel), tapering
  to a point at a tip, square at the solid-end bulkhead. A layer is cut across
  into equal lengths (`L3-1`, `L3-2`, ... bow to stern).
* **Upright** (`--vertical`): layers stand across the canoe like ribs from the bow end xa of the
  hollow run, every foam (+ board) thickness, each a cross-section closed by the gunwale line on
  the floor (`R1`, `R2`, ...): templates take the bigger of the slab's two faces (the other etched),
  boards the section at their mid-plane.
* **Packing** (`--packing`): `blocks` (default) cuts every flat layer at the same joints, the
  fewest equal lengths over the floor layer's span for which every layer's piece in every block
  fits (foam and hardboard): the joints line up from floor to keel, so the mold comes apart in
  whole blocks for demolding (pieces `L3-2` = layer 3, block 2; a layer that does not reach a
  block has no piece there; joints in `stack.joints_in`; upright layers: as continuous);
  `continuous` cuts each layer into the fewest lengths whose
  foam pieces fit a foam sheet (quarter turns) and whose hardboard pieces fit a hardboard sheet
  (any angle); `tight` also tries shorter lengths (down to a quarter of the sheet) and keeps the
  fewest hardboard sheets, then fewest pieces; `fewest-shapes` makes each template half a layer
  (centreline to side), traced on both sides by flipping it over, and lets the bow template trace
  the stern piece too when the two are mirror images within 0.02 in (each piece lists what it
  traces: `L3-1 S`, `L3-1 P (flip)`, `L3-3 P`, `L3-3 S (flip)`).
* **Nest inside** (`--nest-inside`, off by default): pieces may be cut out of the middle of bigger
  ones (a tracing template only needs its outer edge, so it becomes a ring at least `--ring` wide,
  default 1 in): a placed piece takes only the band within ring + gap of its edge, its middle stays
  free. Layers narrower than the widest by more than 2 (ring + gap) are also tried cut shorter (as
  `tight` does for every layer; not with `blocks`, whose joints are shared), fewest sheets kept.
  A host loses the marks over the pieces inside it and its label moves onto the ring; the DXF
  cuts inner pieces first (`sheets[].parts[].inside`).
* **Nesting** onto the hardboard sheet sizes of `--sheet` (default 96x48; several allowed: a new
  sheet is the largest that holds the piece, then each sheet's pieces move to the smallest size
  that holds them all, and of two packings with as many sheets the less hardboard wins): pieces
  rasterised to 1/4 in cells grown by half of `--gap` (default 1/4 in) so no two pieces, and no
  piece and the sheet edge, come closer than the gap; largest first at the lowest free spot along
  the length, first sheet with room; three piece orders, each with quarter turns and with every
  5 deg (lowest-spot-first can tilt a piece into a spot that blocks others, so both are kept and
  the fewer sheets win). Err if a piece fits no sheet. Deterministic.
* **Half canoe** (`--half-canoe`, off; flat tracing templates only): for a canoe the same fore and
  aft, a piece wholly aft of midships (of the hollow run) reuses the template of its mirror image
  at the bow (within 0.02 in), turned end for end, so only the bow half is cut; Err naming the
  piece if it has none. A piece across midships is cut whole.
* **Alignment holes** (on; `--no-holes`, `--hole` size, default 1/4 in): cut in every template at
  the 12 in stations from the bow end of the hull, on the centreline (half templates 1 in off it;
  upright slabs on the centreline 1 and 4 in above the floor), where at least the hole radius plus
  3/8 in from the edge: the same places in every layer, to mark the foam or dowel the stack.
* **Cutting**: each sheet's parts in [`cut_order`]: pieces cut out of others first (deepest
  first), then nearest first from the sheet corner; each piece's holes before its outline, the
  outline from its sharpest corner (a square joint end or a tip, so the lead-in marks a corner).
  `--kerf` (default 0: the water-jet software compensates) moves outlines out and holes in by half
  of it in the DXF; it must be less than the gap.
* **Shop packet** (web tab): a printable page of the settings, steps, stack (a drawing of each
  layer with its foam pieces, holes and block joints), every template with its sheet and what it
  traces, and every sheet in cutting order, with boxes to tick.
* **DXF**: R12 (AC1009), inches, one file per sheet at its own size (`<hull>_sheet_01.dxf`), cut lines
  only by default (OMAX Layout, which the shop likely uses, cuts every imported line unless set
  not to; it adds the lead-ins and cut-width compensation itself); `--dxf-marks` also writes the
  reference lines: layer CUT = closed
  piece outlines, LABEL = piece name (flat: with `<BOW`, `xN` when it traces N pieces), centreline,
  12 in station ticks and sanding line (etch or skip), SHEET = the sheet edge. `-o` also gets
  `mold.json` (`jaguar-mold/1`: spec, stack, layers with their foam pieces, pieces with what they
  trace, sheets).

## Race simulator (`src/race.rs`)
`jaguar race` races the winner of a result.json (its hull, `hull_weight_lb`, depth and flare;
`[race]` from the result's params or `--params`) over each `[[race.event]]` and writes
`jaguar-race/1` JSON. Courses are data (`[[race.course]]`: marks at (x, y) m with the side
each is kept on, finish line x = `finish_x_m`); `examples/canoe.toml` has the ASCE 2026 sprint
200/400, slalom 200 and endurance 600 courses and the Symposium and Finals events, with the
geometry the rules leave to the site marked as assumptions.
* **Crew**: each event gives `load_case = "<name>"` (that `[[load_case]]`'s `weights_lb`, filled
  in by `RaceParams::resolve_crews` when the params are read) or its own `weights_lb`, exactly one;
  an unknown name is an error. A shared crew stays one input; light crews that never govern the
  structure keep their own weights and cost the search no load case. Params serialize an event
  in that input form; a result's race output names each event's `load_case`.
* **Path**: standing start on x = 0 heading +x, a circular arc around each mark (buoy inside
  the circle, `buoy_clearance_m` in from the arc), common tangents between arcs, straight to the
  finish once heading at it. Free: start y, each arc's radius (>= `min_turn_radius_m`) and centre
  direction; compass search with penalty continuation from deterministic starts. The path keeps
  the lane and every buoy's clearance (worst overshoot <= 1 cm) or the event is an error.
* **Speed**: m (1 + 0.05 added mass) dv/dt = T - D along the path. T = min(n `max_thrust_n`,
  n `propulsive_efficiency` P / v), P = `paddler_power_w` (per event override). D = Michell + ITTC
  resistance at the level draft of hull + crew, plus on arcs the steady-turn cross-flow drag
  (`resistance::turn`), both tabulated. Integrated in distance on v^2 (RK4, 0.25 m).
  The drag table runs to 6 m/s; a faster canoe is an error, not an extrapolation.
* **Output** per event: time, distance, straight and turning distance and time, effective mass,
  straight drag and thrust at the top speed, each segment
  (radius, turn angle, entry / min / exit speed, drift), the path, speed along it, and the time sensitivities
  S = d ln t / d ln D for straight and turning drag. `weighted_time_s` = sum of event weight x
  time. `recommended`: `straight_weight` = sum w t S_straight / sum w t (S_straight + S_turn),
  `displacements` (same shares grouped by load case, or by crew for own-weights events),
  `turn_radius_in` (turn-time weighted radius):
  values to put in `[objective]`.
* Courses in examples/canoe.toml follow the ASCE figures in `races/` (Fig. 3 slalom, Fig. 4
  endurance slalom, Fig. 5 sprint; lane 5, its buoy line y = 0). Endurance: the lane-5 slalom,
  buoy 8 on the lane line at 100 m kept to starboard, the three-buoy open-water loop, buoy 12 on
  the 100 m line 105 m across from buoy 8 (kept to starboard), then 100 m to the line. A course may set its own
  `lane_width_m` (>= the race lane) for open water, e.g. the endurance loop; the start stays in the
  race lane. Each event's `speed_m` is (distance, speed, time) about every metre.
* Web UI: the Race tab races the last winner with the form's `[race]` (`POST /api/race`, or
  `result.race` after a race-objective run) and plays it back: the course at equal scale (lane,
  start/finish line, buoys with clearance rings, the one being rounded highlighted), the winner's
  top-view outline moving along its path at the simulated speed and yawed by the turn model's drift
  in arcs, path covered so far; controls play/pause (space), restart, viewing speed 0.25-50x, a
  time scrubber and follow (40 m window on the canoe); live time, speed, distance and current
  turn; speed along the path with the canoe's cursor; the event table and recommended weightings.
  The form's Race events section picks each event's crew (a load case, or custom weights) and
  posts the whole `[race]`, used as posted and kept in the saved prefs; prefs without a `[race]`
  get the CLI canoe.toml's (Reset to canoe.toml brings it back). The race tab's event list and
  table show each event's load case, or "own weights".
* `--measured "EVENT=SECONDS"` first fits `paddler_power_w` (bisection) so that event takes the
  measured time. Calibrate before trusting absolute times; the weightings depend on it less.
* Neglected: wind, current, yaw acceleration, stroke pulsation, heel. Drag is the rank-only
  model of the drag objective, so absolute times need the calibration.

## Performance
`examples/canoe.toml` (686 candidates, 161 stations) runs in about 0.5 s on a laptop (release; drag of every candidate and the seat corners).
In a shape search only the reported candidates get their drag (each shape's verified winner, or its
best failing candidate) unless drag is the objective; `[zones]` skips walls whose single-mix wall
misses freeboard and checks GM by ballast on a quick float before a full check.
Keep it under 1 s. `examples/shape_opt.toml` must stay under 60 s. `jaguar race` on the
examples/canoe.toml winner (9 events) runs in about 5 s; `examples/shape_race.toml` in about 44 s
(22 threads; mostly the per-shape straight-resistance tables, 24 Michell calls per crew).

## Out of scope (do not build)
Running Michlet/Godzilla, a native desktop GUI (the desktop app is the web UI), mix database, more than two mixes, torsion. Anything else new needs the user's approval first (ideas: docs/PLAN.md).
