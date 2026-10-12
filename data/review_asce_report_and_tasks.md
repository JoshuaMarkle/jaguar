# Review: ASCE structural report vs JAGUAR, and the task list

Date: 2026-10-12. Inputs: `data/ASCE_Structural_Report.pdf` (16 pp), `data/canoe_optimizer_tasks.md`
(+ the original `.docx`).

The report was produced by the **legacy Python app** (it cites `canoe_suite.cfg`,
`ADVANCED_PADDLER_LOADING`, Kupfer gate. All of these live under `old/` in the `legacy-final` tag),
not by JAGUAR. The two programs share the off1 hull and some philosophy, but differ in loads, mix
model and punching.

## 1. Like-for-like run

JAGUAR run on the report's design as closely as it can be expressed (off1, t = 0.375 in, depth 15 in,
uniform mix since JAGUAR has no zoning, 2M + 4_coed with the report's seats, crew CG 19.86 in):

| quantity | report | JAGUAR | note |
|---|---|---|---|
| hull weight | 130.9 lb (68.5/61.5/68.5 zoned) | 127.8 lb (60 pcf uniform) | consistent |
| loaded freeboard, 4-person | 8.20 in | 8.19 in | **agrees**, independent check of both hydrostatics |
| acceptance margin | D/C <= 0.95, re-check at finer grid | dc_search 0.93, dc_verify 0.95 at 2n-1 | same idea, same drift data |
| longitudinal bending D/C | 0.527 | 0.26-0.29 | report crew x 1.2 x 1.6 (LRFD) vs JAGUAR x 1.2. Different mix fit too |
| punching D/C | 0.67 (PASS) | 0.83 at 60 pcf | see sec. 3, the report's number is unconservative |
| governing check | Kupfer biaxial 0.903 | punching 0.83-0.90 | |
| loaded GM | -2.99 in | -8.6 in | different KG/draft basis. Both say: not self-righting with crew |

## 2. How each calculation is handled

| topic | report (legacy) | JAGUAR | verdict |
|---|---|---|---|
| self weight | per-station area x local zone density | per-station area x rho (`equilibrium.rs`) | same, JAGUAR single mix |
| buoyancy | "proportional to local water-plane breadth" | real submerged section area, draft **and trim** root-found | JAGUAR more exact |
| crew load | point loads | 3 in patch (point loads don't converge, AGENTS rule 3) | JAGUAR better |
| race cases | 2F, 2M, 3F, 3M, 4_coed, every seat ordering, position variance | any number of `[[load_case]]`, fixed or evenly spaced seats, no variance/permutation | report wider envelope |
| seat positions | optimized and recorded | user-given or L(i+1)/(n+1) | gap |
| load factors | LRFD 1.2D/0.9D + 1.6L, L x 1.2 dynamic, phi 0.60 **and** allowables = ult/1.5 | ASD: crew x 1.2, every capacity / 1.5 | report's text is ambiguous whether phi and FoS stack (double counting) |
| bending fibre | sign-aware (tension fibre only), transformed multi-zone section | max(c) regardless of sign (conservative, matches golden) | report sharper, JAGUAR safer |
| compression | advisory only | gated vs 0.85 f'c | JAGUAR stricter |
| shear | "well below", not quantified | VQ/(I 2t) vs 2 sqrt(f'c), gated | JAGUAR stricter |
| combined | Kupfer-Gerstle with hoop stress from turning (k = V^2/gR) | none | gap, but small effect (see below) |
| torsion | advisory D/C 3.53, not gated | out of scope | same outcome |
| strength model | f'c = 161.29 rho - 9566, ft = f'c/5, "n = 3" but lists 7 mixes (50.9-80 pcf) | split tensile fit from 3 breaks (54.9-60 pcf), f'c = (fsp/6.7)^2 | **disagree badly** (61.5 pcf: ft 71 psi vs 314 psi) |
| stability | crew KG 19.86 in from anthropometry, GM -2.99 in, reported not gated | crew_cg_in = 12 (assumed), gated at gm_min 0.25 | JAGUAR gate is optimistic if 19.86 is right |
| wave drag | not reported | Michell + ITTC, for ranking | |

### Report inconsistencies worth fixing before it is submitted

1. **Punching demand uses b0 x t, not b0 x d.** Eq. 3 is 175/(13.00 x 0.375) while d = 0.250 in.
   With b0 x d the demand is 53.8 psi vs allowable 53.3 psi: **D/C 1.01, FAIL**. Add the 1.2 dynamic
   factor the report applies everywhere else and it is **1.21**. Step 2 (two circles) has the same
   b0 x t error. This is exactly the mistake `docs/PUNCHING.md` warns about.
2. **Knee/toe split is stated three ways**: 100/0 (Sec 6.5), 80/20 (Sec 6.6), 90/10 (Sec 10).
3. **Kupfer numbers don't reconcile.** Governing fibre sigma_x 159 psi, sigma_y -9.8 psi vs ft 197.7:
   159/197.7 + 0.8 x 9.8/988 = 0.81, not 0.903. Also the governing fibre is the top fibre in tension
   at station 102, where Sec 4 says hogging M = 0 there.
4. **Mix data**: "n = 3" next to 7 mixes on record, 70 and 80 pcf points are not in
   `data/mix_tests.csv`. The two fits can't both be right.
5. **Mesh depth vs mold side.** d = 0.250 in (mesh after pass 2 of 3) is correct only if pass 1 is
   the **inside** face (male mold). On a female mold the same mesh is 0.125 in from the knee face
   and D/C rises to the 0.81-row value (or higher with fix 1).
6. KB = 5.72 in is quoted with a design draft of 4.58 in (KB can't exceed draft), so GM is
   probably at the loaded draft. State the basis.
7. lambda = 0.75 (lightweight) is used in punching without being stated. Fine per ACI, but say so.
   JAGUAR uses lambda = 1 because its f'c is backed out of split tensile, which already carries it.

Things the report does well that JAGUAR could borrow: per-zone allowables, mix provenance
(batched vs predicted, "reach"), search-space boundary table, recorded winning seats.

## 3. Task list: done vs open

| task | status in JAGUAR |
|---|---|
| Accurate punching shear | **mostly done** (ACI 22.6 with b0 x d, round knee, 2-knee split/merge, pad, plain 14.5 lower bound). Missing: d tied to layers + mold side |
| Mixes separate from construction layers | open (no layers, no multi-mix) |
| STEP export | open (SPEC lists it out of scope, needs approval + probably a crate or hand-written AP214) |
| Draft / depth increments | depth step done (`search.depth_in`). Draft is solved, not chosen |
| Rocker (in) | partial: rocker is a Series-7 exponent, not inches |
| Flare angle | done (`hull.flare_deg`) |
| Min entry angle | open |
| Doubly symmetric | open (set fore = aft bounds by hand, nothing ties them) |
| Buoyancy distributed force | **done** |
| Self weight distributed force | **done** |
| Kupfer (shear + moment) | open (PLAN idea) |
| LRFD 1.2 / 1.6 | open (ASD with FS 1.5 + DLF 1.2 now) |
| Race cases 4/3/2 separately | **done in the engine** (`[[load_case]]`), examples only use 2M |
| Auto seat solver + variance + manual override | manual done, auto/variance open (PLAN idea) |
| Multi-mix vertical/horizontal zones, I-beam middle, light ends | open (SPEC: out of scope, needs approval) |
| Displacement weighting 350/525/700 lb | open (drag uses heaviest case only) |
| Straight 80 / turning 20 | open, no turning metric yet |
| Graph: multi-pour side view | open (needs multi-mix) |
| Graph: stress utilization along length | partial (tension vs allowable, governing case) |
| Graph: M and V, all cases, toggles | partial (governing case only) |
| Fixed-mold structural sweep | **done** (`run` without `[shape_search]`, fix depth range) |
| Bow/stern air pockets, swamped flotation | open |
| Race simulator / ML path | open (SPEC: out of scope) |
| Mix CSV import | **done** |
| Density vs strength graph with fit | partial (fit + table in UI). Only split tensile data exists |
| Editable mix table | open |

## 4. My priority

Key fact driving it: **knee punching governs every design** (D/C ~0.90), bending sits at 0.3-0.56.
Anything that only improves bending (Kupfer, seat solver, LRFD bending) will not make the canoe
lighter until punching is relieved.

1. **Reconcile the mix data** (small, highest leverage). Add the 50.9/51.2/55.5/56.2/70/80 pcf breaks
   to `data/mix_tests.csv` if they exist. The two programs' strength fits disagree ~4x at 61.5 pcf.
   Every answer moves with this.
2. **Construction layers in punching** (small). `layers`, `mesh_after_layer`, `mold = male|female`
   -> d. Replaces the guessed `d_over_t`, and catches report issue 5.
3. **Multi-mix zones** (large, biggest weight win). Only the bottom (knee zone) needs the punching
   mix. Sides and ends can drop to the lightest bending allows. Includes the I-beam middle / light
   ends layout and the pour-zone plot. Needs SPEC change.
4. **Race-case examples + all-case V/M/utilization plots with toggles** (small). Engine already
   supports it.
5. **Swamped flotation / air pockets** (small-medium). ASCE compliance item, simple volume gate.
6. **LRFD as an option** (medium). `method = "asd" | "lrfd"`, envelope 1.2D and 0.9D, phi replaces
   FS (never both). Expect it to make designs heavier: crew factor 1.2 x 1.6 / 0.75 = 2.56 vs 1.8.
7. **Crew CG / GM** (input, not code). With the report's 19.86 in, every JAGUAR hull fails
   gm_min 0.25. Measure it, then decide whether GM stays a gate.
8. **STEP export** (medium). Valuable for the mold shop, independent of physics.
9. **Weighted displacement drag** 67.5/10/22.5 (small-medium, shape search only).
10. **Geometry constraints**: entry angle gate, symmetric flag, rocker in inches (small each).
11. **Auto seat solver with variance**, then **Kupfer**: worth it after 1-3, when bending can govern.
12. Later: turning weight (needs a model first), editable mix table, race simulator.
