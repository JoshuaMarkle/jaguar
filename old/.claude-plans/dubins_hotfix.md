# Dubins Hotfix — Geometric Tangent-and-Arc Reconstruction

## Status
- **Date:** 2026-04-30
- **Author (architect):** Claude (Opus 4.7)
- **Target file:** `physics/race_path.py`
- **Implementing agent:** must NOT modify the file until this plan is read end-to-end.

---

## 1. Root-Cause Diagnosis (read this first)

The current implementation is **not** Pure Pursuit — `physics/race_path.py` already calls the Shkel & Lumelsky 2001 closed-form Dubins word formulas (`_lsl`, `_rsr`, `_lsr`, `_rsl`, `_rlr`, `_lrl`). Those primitives are correct in isolation.

The bug lives in the outer stitching loop, `analyse_course()` (lines ~426–501):

1. **Entry/exit headings at every intermediate anchor are pre-baked from chord directions** (lines ~440–460):
   - `entry_h[i] = chord_headings[i-1]`  (direction of incoming chord)
   - `exit_h[i]  = chord_headings[i]`    (direction of outgoing chord) — *unless* the anchor is a buoy
2. Each Dubins sub-path is solved with `start = (x_i, y_i, exit_h[i])` and `end = (x_{i+1}, y_{i+1}, entry_h[i+1])`. Because `entry_h[i+1] ≠ exit_h[i+1]` whenever the polyline bends, the **exit heading of one sub-path does not equal the entry heading of the next**. That is the G1 break.
3. The "forced buoy arc" is then **appended after** the Dubins solve, rotating the canoe in place from `entry_h[i+1]` to `exit_h[i+1]`. This grafts a third arc onto the path with no tangency continuity, producing the visible jagged loop.
4. Because each Dubins call is forced to land on a heading dictated by the chord polyline (not by the geometry of the next circle), the solver routinely picks `RLR`/`LRL` or wraps a `t` or `q` arc near `2π` — that is the "oscillation around the waypoint" the user reports.

**Conclusion:** the per-pair Dubins primitive is fine. The whole stitching strategy must be replaced with a true geometric construction in which **headings at every waypoint are derived from circle tangencies, not from chord directions**.

---

## 2. Required Mathematical Model

### 2.1 Configuration space

A vehicle state is `(x, y, θ)` ∈ SE(2). The minimum turning radius is `ρ = min_turn_radius` (m).

### 2.2 Turning circles at a waypoint

At any waypoint `W = (x_w, y_w)` the canoe can be tangent to one of two circles of radius `ρ`:

- **Left circle** `C_L(W, θ)`: centre at `W + ρ · (-sin θ, +cos θ)` — i.e., ρ to the left of the heading `θ`.
- **Right circle** `C_R(W, θ)`: centre at `W + ρ · (+sin θ, -cos θ)` — i.e., ρ to the right of the heading `θ`.

For the multi-waypoint problem the heading `θ` at each intermediate waypoint is **not given**. The correct geometric construction places a circle **tangent to both incoming and outgoing tangent lines simultaneously** (see §3.3).

### 2.3 The four common tangents between two circles

Given two circles `C₁ = (O₁, r₁)` and `C₂ = (O₂, r₂)` with `D = ‖O₂ − O₁‖`:

- **External tangents** (both circles on the same side of the tangent line) — exist when `D ≥ |r₁ − r₂|`. There are 2.
- **Internal / crossing tangents** (circles on opposite sides) — exist when `D ≥ r₁ + r₂`. There are 2.

For `r₁ = r₂ = ρ` (our case):

**External tangent (used by LSL and RSR):**
The two external tangent lines are parallel to `O₁O₂`, offset by ±ρ perpendicular to it.
- Unit vector along centres: `û = (O₂ − O₁) / D`.
- Unit perpendicular: `n̂ = (-û_y, û_x)` (left of `û`).
- Tangent points on `C₁`: `T₁ = O₁ ± ρ · n̂`.
- Tangent points on `C₂`: `T₂ = O₂ ± ρ · n̂` (same sign as on `C₁`).
- Tangent length = `D` (exactly the centre-to-centre distance, since `r₁ = r₂`).

The sign selects which tangent: for an LSL transition (canoe turns left around both buoys, both circles to the left of motion), use the tangent on the right side of the centre line so the canoe runs along it with both circles on its left. For RSR, mirror.

**Internal tangent (used by LSR and RSL):**
Exists only when `D ≥ 2ρ`. The tangent line crosses the segment `O₁O₂`.
- Mid-angle: let `α = atan2(O₂.y − O₁.y, O₂.x − O₁.x)`.
- Inner half-angle: `β = acos((r₁ + r₂) / D) = acos(2ρ / D)`.
- Tangent point on `C₁` for **LSR** (left circle 1 → right circle 2): `T₁ = O₁ + ρ · (cos(α + β + π/2), sin(α + β + π/2))`.
- Tangent point on `C₂` for **LSR**: `T₂ = O₂ + ρ · (cos(α + β − π/2), sin(α + β − π/2))`.
- For **RSL** swap the sign of `β`.
- Tangent length = `√(D² − (2ρ)²)`.

### 2.4 Arc length on a circle

Once `T_in` (tangent point where the canoe enters the circle) and `T_out` (tangent point where it leaves) are known on the same circle of centre `O` and radius `ρ`, the arc length is:

- `φ_in  = atan2(T_in.y  − O.y, T_in.x  − O.x)`
- `φ_out = atan2(T_out.y − O.y, T_out.x − O.x)`
- For a **left (CCW) arc**: `Δφ = mod2π(φ_out − φ_in)`; arc length = `ρ · Δφ`.
- For a **right (CW) arc**: `Δφ = mod2π(φ_in − φ_out)`; arc length = `ρ · Δφ`.

The heading at any point on the circle is the tangent direction: `θ = φ + π/2` for CCW, `θ = φ − π/2` for CW. **G1 continuity is automatic** because the straight tangent line is, by construction, perpendicular to the circle radius at the tangent point — so the tangent line direction equals `θ` at `T_out` of one segment and equals `θ` at `T_in` of the next.

---

## 3. Required Algorithm

### 3.1 Inputs

- Ordered list of waypoints `W₀, W₁, …, W_{n-1}` from `RaceCourse.waypoints` (after `_collapse_waypoints`).
- For each waypoint, a flag indicating whether it is a **buoy** (`turn_radius > 0`) or a **pass-through** (`turn_radius == 0`). Buoys have a forced rounding side.
- Global `min_turn_radius` ρ.

### 3.2 Determining rounding side at each buoy

For a buoy `W_i` with neighbours `W_{i-1}, W_{i+1}`:

- Compute the signed cross product of the incoming and outgoing chords:
  `s_i = (W_i − W_{i-1}) × (W_{i+1} − W_i)`
  (2-D scalar cross: `a.x·b.y − a.y·b.x`).
- If `s_i > 0`: the natural turn is **left (CCW)** → use **L circle** (centre to the left of the chord).
- If `s_i < 0`: **right (CW)** → use **R circle**.
- If `s_i == 0` (collinear), the waypoint is a pass-through; no circle required.

For the canoe-course rules: the buoy specifies `turn_angle` and `turn_radius`. The buoy radius must satisfy `r_buoy ≥ ρ`. If the user provides a tighter `turn_radius`, **clamp up to ρ** (cannot turn tighter than the kinodynamic limit).

### 3.3 Placing the turning circles

For the simplest geometrically-correct construction (the one this hotfix must implement first), use a **fixed-side circle at each buoy and skip non-buoy waypoints**:

1. For each buoy `W_i` with rounding side `σ_i ∈ {+1 left, −1 right}` and circle radius `r_i = max(ρ, buoy.turn_radius)`:
   - Place the circle centre at `O_i = W_i + r_i · n̂_i`, where `n̂_i` is the unit vector pointing from the buoy toward the inside of the turn. Compute `n̂_i` as the perpendicular to the **angle-bisector** of the incoming and outgoing chords, pointing on the side opposite to where the canoe must pass.
   - Concretely: let `û_in = (W_i − W_{i-1})/‖·‖` and `û_out = (W_{i+1} − W_i)/‖·‖`. The inward normal is `n̂_i = σ_i · perp(û_in + û_out) / ‖·‖`, where `perp(v) = (-v.y, v.x)`.

2. For pass-through waypoints (`turn_radius == 0`), do **not** place a circle. The path just passes through the point on a straight tangent line; treat them as "soft" hints — see §3.5 for how to incorporate them without forcing a heading.

### 3.4 Connecting consecutive buoy circles with tangents

For each consecutive pair of buoy circles `(C_i, C_{i+1})` with centres `O_i, O_{i+1}` and radii `r_i, r_{i+1}`:

1. Decide the **word** for this pair from the rounding sides:
   - `σ_i = +1, σ_{i+1} = +1` → **LSL** (external tangent, both circles to the left).
   - `σ_i = −1, σ_{i+1} = −1` → **RSR** (external tangent, both circles to the right).
   - `σ_i = +1, σ_{i+1} = −1` → **LSR** (internal tangent).
   - `σ_i = −1, σ_{i+1} = +1` → **RSL** (internal tangent).

2. Compute the tangent points `T_i^{out}` (on `C_i`) and `T_{i+1}^{in}` (on `C_{i+1}`) using the formulas in §2.3. **Verify feasibility** for internal tangents: `D ≥ r_i + r_{i+1}`. If infeasible (circles too close for internal tangent), the canoe cannot perform the prescribed left-then-right (or right-then-left) sequence at the requested radii — **fail loudly with a `ValueError`** that names the buoy pair and the deficit. Do **not** silently substitute.

3. For the very first segment (start of course → first buoy circle) and the last segment (last buoy circle → finish), treat the start/finish point as a degenerate **circle of radius 0** — i.e., a point. The tangent from a point to a circle has tangent length `√(D² − r²)` and tangent point computed from the same formula with `r₁ = 0`. Pick the tangent on the side that minimises total path length (try both and keep the shorter).

### 3.5 Handling pass-through waypoints (NOT in the first cut)

The first version of this hotfix should ignore non-buoy waypoints — they were always advisory anyway. Do **not** try to interpolate or steer toward them. If a future extension wants to honour them, the correct way is to insert a virtual "soft" circle of radius ρ at the pass-through and run the same tangent construction; that is out of scope for this hotfix.

### 3.6 Constructing the final path

Walk the buoy list in order. For buoy `i` (1-indexed, with i=0 being the start point and i=n−1 being the finish point):

1. **Tangent line** from `T_i^{out}` on circle `C_i` to `T_{i+1}^{in}` on circle `C_{i+1}` (or from start point to `T_1^{in}` for the first leg).
2. **Arc** on circle `C_{i+1}` from `T_{i+1}^{in}` to `T_{i+1}^{out}` in direction `σ_{i+1}` (CCW if σ=+1, CW if σ=−1).
3. Repeat until the finish point.

Each tangent line and each arc is a primitive of known geometry — sample them with the existing `_sample_segment_xy` helper (which is already correct for L, R, S primitives given a centre/radius/heading). G1 continuity is automatic.

### 3.7 Outputs

The function `analyse_course()` must continue to return the exact same dict keys it does today (see lines 512–527 of `physics/race_path.py`). Specifically:

- `path_xy`: (N×2) sampled points
- `total_path_length_m`, `total_arc_length_m`, `total_tangent_length_m`
- `pct_straight`, `pct_turn`, `Cs`, `Ct`
- `dubins_words`: list of the per-segment words actually used (`"LSL"`, `"RSR"`, `"LSR"`, `"RSL"`)
- `min_turn_radius_m`
- Backwards-compat aliases: `straight_m`, `turn_m`, `total_m`

`Cs` and `Ct` MUST sum to 1.0 (within floating-point tolerance). `total_path_length_m == total_arc_length_m + total_tangent_length_m` exactly.

---

## 4. What to Discard

The implementing agent MUST delete or refactor away the following pieces of `physics/race_path.py`:

1. The chord-direction heading calculation in `analyse_course()` (lines ~440–460): `chord_headings`, `entry_h`, `exit_h`. These are the source of the G1 break.
2. The "forced buoy arc" graft (lines ~484–501) that runs *after* the Dubins solve. The new construction integrates the buoy arc *into* the geometric path.
3. The per-pair call to `shortest_dubins(start, end, min_turn_radius)` inside the analyse loop (line ~474). Replace it with the new tangent-and-arc constructor.

The implementing agent MUST KEEP:

- All six Shkel/Lumelsky word functions (`_lsl`, `_rsr`, `_lsr`, `_rsl`, `_rlr`, `_lrl`) and `shortest_dubins()` and the `DubinsPath` dataclass — they remain useful as a fallback for the SE(2) two-pose problem and are exercised by other code paths.
- `_seg_step()`, `_sample_segment_xy()`, `_sample_path_xy()` — geometry-correct primitives the new construction will reuse.
- `_collapse_waypoints()`, `_Anchor`, `_signed_turn_at()` — preprocessing utilities.
- The public API surface: `analyse_course`, `analyse_all_courses`, `race_capability_targets`, `plot_racing_line`, and the `DEFAULT_MIN_TURN_RADIUS_M` constant. **No call sites may change.**

---

## 5. Required Tests (must accompany the implementation)

Add to `tests/test_race_path.py` (create if absent):

1. **G1 continuity test**: for every course in `race_course.RACE_COURSES`, sample the returned `path_xy` at a fine resolution (≤ 0.1 m) and verify that the **discrete heading** (atan2 of consecutive sample deltas) has no jump greater than a tolerance (e.g. 5° between adjacent samples after an arc/tangent transition). This is the regression that catches the current bug.
2. **Length consistency**: `abs(total_path_length_m − (total_arc_length_m + total_tangent_length_m)) < 1e-6`.
3. **Capability sum**: `abs(Cs + Ct − 1.0) < 1e-9`.
4. **Tangent-feasibility error**: construct a synthetic course with two buoys 0.5·ρ apart requiring an LSR turn; confirm the new code raises `ValueError` mentioning both buoy labels (per §3.4 step 2).
5. **Single-buoy sanity**: a course with start, one 90° buoy at radius ρ, and finish — verify the path is exactly `tangent line, ρ·(π/2) arc, tangent line` and total length matches the closed-form value.
6. **Straight-line sanity**: a course with two collinear pass-through waypoints — verify `total_arc_length_m == 0` and `total_tangent_length_m == ‖W₁ − W₀‖`.

---

## 6. Implementation Notes for the Next Agent

- All math is plane-Euclidean — no SE(2) lifting needed. Keep types `float` and `numpy.ndarray` of shape `(2,)` for points.
- Use `numpy` for vector ops where it improves clarity; do **not** vectorise prematurely — n is small (≤ ~50 buoys).
- Add a small private helper `_external_tangent(O1, r1, O2, r2, side)` returning `(T1, T2)` and `_internal_tangent(O1, r1, O2, r2, side)` returning `(T1, T2)`. Both should raise `ValueError` on infeasibility with a descriptive message.
- Add a small private helper `_arc_length_signed(O, r, T_in, T_out, direction)` returning the physical arc length in metres.
- Reuse `_sample_segment_xy` for sampling — it already handles both arc and straight primitives correctly given a starting `(x, y, θ)`. The new constructor must compute the heading at each `T_in` from the tangent direction (perpendicular to the radius) and pass it in.
- Do **not** add backward-compat shims for the discarded chord-heading logic. It was wrong; delete it.
- Do **not** introduce any new dependencies. Pure `math` + `numpy` only, per `CLAUDE.md`.
- Honour the layering rule from `CLAUDE.md`: `physics/` is pure math. No Qt, no I/O, no service imports.

---

## 7. Out of Scope (do NOT do these in this hotfix)

- Optimising heading at pass-through waypoints (§3.5).
- Adding `RLR` / `LRL` triple-arc support to the multi-waypoint constructor (the geometric construction does not need them; they exist in `shortest_dubins` for the two-pose case and stay there).
- Changing the public dict schema returned by `analyse_course`.
- Modifying `race_simulator.py`, `ui/race_tab.py`, or any caller. The hotfix is internal to `physics/race_path.py`.
- Performance work. Correctness first; the courses are tiny.

---

## 8. Definition of Done

- `physics/race_path.py` is rewritten per §3, with the discards in §4 carried out.
- All tests in §5 pass.
- Running `python app.py`, opening the Race tab, and viewing every default course in `race_course.RACE_COURSES` shows a smooth, single-stroke path with no visible cusps, loops, or oscillation around buoys.
- `pct_straight + pct_turn == 100.0` and `total_path_length_m == total_arc_length_m + total_tangent_length_m` (to floating-point tolerance) for every course.
- The architect (this plan's author) is shown the new `path_xy` plot for the slalom course and confirms G1 continuity by eye before close-out.
