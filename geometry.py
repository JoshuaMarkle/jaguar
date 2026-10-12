# geometry.py
import logging
import numpy as np
import sys
from domain.models import ShellProperties
from errors import domain_only

logger = logging.getLogger(__name__)


def _log(msg):
    logger.info(msg)

def parse_godzilla_dimensions(out_filepath="out.mlt"):
    """
    Scrapes the Godzilla report to find the exact Length and Draft,
    converting them from meters to imperial (feet and inches).
    """
    length_ft = 20.0 
    depth_in = 14.0  
    
    try:
        with open(out_filepath, 'r') as file:
            lines = file.readlines()
            
        for line in lines:
            # Look for Length Over All (LOA) or Length Between Perpendiculars (LPP)
            if "LOA (m)" in line or "LPP (m)" in line:
                parts = line.split(',')
                if len(parts) > 1:
                    try:
                        val = float(parts[1].strip())
                        if 4.0 < val < 7.5: 
                            length_ft = val * 3.28084
                    except ValueError:
                        pass
                        
            # Look for Maximum Draft (Tmax) or just Draft (T)
            if "Tmax (m)" in line or "T (m)" in line:
                parts = line.split(',')
                if len(parts) > 1:
                    try:
                        val = float(parts[1].strip())
                        if 0.05 < val < 1.0: 
                            depth_in = val * 39.3701
                    except ValueError:
                        pass
                        
        _log(f"  -> Extracted true dimensions from {out_filepath}: Length = {length_ft:.2f} ft, Draft = {depth_in:.2f} in")
        return length_ft, depth_in
        
    except FileNotFoundError:
        _log(f"  -> WARNING: {out_filepath} not found. Defaulting to 20ft length and 14in depth.")
        return length_ft, depth_in
    except Exception as e:
        domain_only(e)   # a bug is not a skippable condition
        _log(f"  -> Error reading dimensions ({e}). Defaulting to standard constraints.")
        return length_ft, depth_in

def parse_godzilla_offsets(filepath, L_ft=20.0, draft_in=4.26, mold_depth_in=14.0,
                           n_stations=None):
    """
    Parses the Michlet offset matrix, preserves the underwater hydrodynamics,
    and mathematically extrudes the freeboard straight up to the gunwales.

    ``n_stations`` (optional): resample the STATION axis to this many uniformly
    spaced stations (linear interpolation of every waterline column).  This is a
    NUMERICAL-ACCURACY control for the downstream V(x)/M(x)/weight integrations,
    NOT new hull data: the offset file has a fixed native station count (rows),
    and any value above it interpolates between real offsets rather than adding
    information.  The sweep integrations converge only well above native, so the
    production default oversamples; None keeps the file's native rows.
    """
    try:
        with open(filepath, 'r') as file:
            lines = file.readlines()

        matrix_data = []
        for line in lines:
            clean_line = line.split('#')[0].split('!')[0].replace(',', ' ').strip()
            if not clean_line: continue
            row_vals = [float(val) for val in clean_line.split()]
            if row_vals: matrix_data.append(row_vals)

        # 1. The Underwater Hull (From Godzilla)
        y_matrix_underwater = np.array(matrix_data)
        num_stations, num_waterlines = y_matrix_underwater.shape
        
        # Convert Michlet meters to inches
        y_matrix_underwater = y_matrix_underwater * 39.3701 

        # Map Godzilla's Z-axis strictly to the Draft (NOT the mold depth)
        z_underwater = np.linspace(0, draft_in, num_waterlines)

        # 2. The Freeboard Extrusion (Above the water)
        # If the mold depth is taller than the draft, we build the walls
        if mold_depth_in > draft_in:
            num_freeboard_lines = 10 # Add 10 vertical slices for structural resolution
            
            # Create Z-heights from just above the waterline up to the gunwale
            z_freeboard = np.linspace(draft_in + 0.1, mold_depth_in, num_freeboard_lines)
            
            # Grab the very top curve of the Godzilla hull (the waterline width at every station)
            top_waterline_y = y_matrix_underwater[:, -1]
            
            # Extrude that exact curve straight up to form the freeboard walls
            y_freeboard = np.tile(top_waterline_y, (num_freeboard_lines, 1)).T
            
            # 3. Stitch them together into one master hull
            z_total = np.concatenate((z_underwater, z_freeboard))
            y_total = np.hstack((y_matrix_underwater, y_freeboard))
        else:
            z_total = z_underwater
            y_total = y_matrix_underwater

        # X stations are evenly spaced from 0 to Canoe Length
        x_stations = np.linspace(0, L_ft, num_stations)

        # Optional station-axis resample (numerical-accuracy control).  Linear
        # interpolation of each waterline column onto n_stations uniform points;
        # the z grid (waterlines) is unchanged.  Native rows are the real data,
        # so this only refines the integration grid between known offsets.
        if n_stations and int(n_stations) != num_stations:
            n_new = int(n_stations)
            x_new = np.linspace(0, L_ft, n_new)
            y_total = np.column_stack([
                np.interp(x_new, x_stations, y_total[:, k])
                for k in range(y_total.shape[1])])
            x_stations = x_new

        return x_stations, z_total, y_total

    except Exception as e:
        # ERROR, not info: every caller turns this into "offsets could not be
        # parsed", which reads like a geometry limit rather than the exception
        # it is.  A bad n_stations sentinel hid here through an entire sweep.
        domain_only(e)   # a bug is not a skippable condition
        logger.error("Geometry parse failed for %s (L_ft=%s, draft_in=%s, "
                     "mold_depth_in=%s, n_stations=%r): %s: %s",
                     filepath, L_ft, draft_in, mold_depth_in, n_stations,
                     type(e).__name__, e)
        return None, None, None

def _resample_polyline(pts: np.ndarray, n: int) -> np.ndarray:
    """
    Resample a 2-D/3-D polyline to exactly *n* rows, uniformly spaced by arc
    length (linear interpolation).  Duplicate consecutive rows are dropped
    first so the parameterisation stays strictly increasing.
    """
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    pts = pts[np.concatenate(([True], seg > 1e-12))]
    if len(pts) < 2:
        return np.repeat(pts[:1], n, axis=0)
    s = np.concatenate(([0.0], np.cumsum(
        np.linalg.norm(np.diff(pts, axis=0), axis=1))))
    u = np.linspace(0.0, s[-1], n)
    return np.column_stack([np.interp(u, s, pts[:, k])
                            for k in range(pts.shape[1])])


def _offset_inward(pts: np.ndarray, t: float) -> np.ndarray:
    """
    Offset a 2-D section polyline [y, z] (ordered keel → sheer) inward by
    shell thickness *t* along the local inward normal.

    The outward normal of a starboard section running bottom-to-top is
    (tz, -ty); water lies outside, concrete interior lies inside.  Inner
    Y is clamped to the symmetry plane (y >= 0).
    """
    d = np.gradient(pts, axis=0)
    norm = np.hypot(d[:, 0], d[:, 1])
    norm[norm < 1e-12] = 1.0
    ty, tz = d[:, 0] / norm, d[:, 1] / norm
    inner = pts - t * np.column_stack([tz, -ty])
    inner[:, 0] = np.maximum(inner[:, 0], 0.0)
    return inner


# ── Bow/stern stem rocker (ETS-style ends) ────────────────────────────────────
# Plan view: the tips stay POINTED (the hull's own linear taper to the
# centerline is kept).  Side view: the keel line is swept UP to the sheer
# over a fore-aft setback, giving a rounded stem profile ("rocker") instead
# of a full-depth vertical end face.
_PAD_TOL = 1e-6           # m — half-breadths at/below this are padding
_STEM_N_INSERT = 8        # cap sections inserted across each stem sweep
_STEM_TIP_STUB = 0.04     # fraction of the stem rise left as a short
                          # near-vertical stem stub at the very tip (real
                          # canoes end in a small stem face; also keeps the
                          # tip loop's z-values distinct for manifoldness)


def stem_overhang_ft(x_ft, yhalf_in, t_in: float, breadth_factor: float = 1.5):
    """
    STEM OVERHANG (tip cantilever extent) per end [ft], measured from the hull's
    own geometry and INDEPENDENT of the offset grid resolution.

    Definition, the one suggest_stem_rocker_m's docstring already states: the
    fore-aft run at each end over which the hull is too narrow to host a shell
    section -- max half-breadth below ``breadth_factor * t``.  Beyond that point
    there is no section to build, so the material there is a slender cantilever
    tip rather than structural girder.

    WHY INTERPOLATED, NOT SNAPPED TO A STATION.  Scanning for the first station
    that clears the threshold measures the OFFSET FILE'S ROW SPACING, not the
    hull: the same hull returns 12.0 in at 21 stations, 6.0 at 41, 1.5 at 161.
    That is the same class of defect as the tip-divergence bug -- an answer that
    moves with the analysis grid.  Linearly interpolating the crossing of
    ``b(x) = breadth_factor*t`` gives a value fixed by the geometry: measured on
    the live hull it is 1.045 in/end at 41, 81, 161 and 321 stations alike (21
    differs only because it undersamples the 41-row source).

    NOTE the threshold is proportional to t, so a thicker shell needs more
    breadth before a section can exist and the overhang grows: 1.045 in at
    t=0.375, 1.394 in at t=0.500.  Callers that resolve this once for a whole
    thickness sweep should pass the SMALLEST t in the grid -- the shortest
    overhang, the least tip mass, and (because tip mass relieves the midship
    sagging that governs) the conservative choice.

    The docstring's "narrower than 1.5 t" is ambiguous between half-breadth and
    full breadth; this uses HALF-breadth < 1.5t, the more generous reading.
    Full breadth < 2t (i.e. the walls would touch) would give ~0.70 in here, so
    the choice moves the answer by a few tenths of an inch and neither reading
    supports anything close to the 3 %-of-L floor.

    Returns 0.0 when the hull never crosses the threshold (a hull with no
    slender tip has no overhang).
    """
    x = np.asarray(x_ft, dtype=float)
    b = np.asarray(yhalf_in, dtype=float)
    if b.ndim > 1:                       # (n_st, n_wl) -> widest point per station
        b = np.max(b, axis=1)
    thr = float(breadth_factor) * float(t_in)
    if x.size < 2 or not np.any(b >= thr):
        return 0.0

    def _cross(xs, bs):
        """First x where bs crosses thr from below, linearly interpolated."""
        for i in range(len(bs) - 1):
            if bs[i] < thr <= bs[i + 1]:
                span = bs[i + 1] - bs[i]
                f = (thr - bs[i]) / span if abs(span) > 1e-12 else 0.0
                return xs[i] + f * (xs[i + 1] - xs[i])
        return xs[0]                     # already wide enough at the tip

    bow = _cross(x, b) - x[0]
    stern = x[-1] - _cross(x[::-1], b[::-1])
    return float(max(0.5 * (bow + stern), 0.0))


def suggest_stem_rocker_m(d_wedge_m: float, L_m: float) -> float:
    """
    "Auto / optimal" stem-rocker sweep length: the fore-aft distance over
    which the keel line rises to the sheer at each end.

    Derivation:
    * The natural sweep is the degenerate "wedge" span d_wedge — the region
      where the hull is narrower than 1.5 t and cannot host a shell section
      anyway, so sweeping its bottom up discards NO real (Godzilla-optimised)
      underwater geometry.
    * Floor of 3 % L: keeps the rocker visible on hulls whose offsets stay
      wide almost to the tips.
    * Cap of 12 % L: roughly the bow-sweep proportion of the ETS reference
      hull; prevents a coarse offset grid (huge wedge) from hollowing out
      the forebody.
    """
    return float(np.clip(float(d_wedge_m), 0.03 * float(L_m),
                         0.12 * float(L_m)))


def _stem_rise(d, a):
    """
    Side-view stem profile: fraction of the keel→sheer rise at fore-aft
    distance *d* from the tip, for sweep length *a*.

    Quarter-ellipse  1 - sqrt(1 - ((a-d)/a)^2):  horizontal tangent at the
    join (d = a, blends smoothly into the keel line) and a steep, rounded
    rise at the tip (d = 0) — the ETS elevation-view shape.  Clipped to
    (1 - _STEM_TIP_STUB) so the tip keeps a short stem face.
    """
    u = np.clip((a - np.minimum(d, a)) / a, 0.0, 1.0)
    return np.minimum(1.0 - np.sqrt(1.0 - u ** 2), 1.0 - _STEM_TIP_STUB)


def _stem_rocker_specs(outers, x_tip: float, sgn: float, a: float,
                       z_sheer: float, tip_half_beam: float):
    """
    Pure geometry for one rocker end (no section construction here — the
    caller rebuilds each section from its outer chain at full shell
    thickness, fix plan C1).

    * Valid stations with d < a get their bottoms raised along _stem_rise
      (plan-view widths untouched — the tip stays pointed).
    * Cap sections span the degenerate wedge (tip → first valid station):
      plan half-breadth tapers LINEARLY to a sliver (pointed top view),
      bottom follows the stem curve (rounded side view).
    * The terminal section at the true tip x is a sliver at near-sheer
      height.

    Returns (raises, caps, tip, src_i, a_used):
      raises : [(station_idx, z_bottom)] for real stations inside the sweep
      caps   : [(x, y_scale, z_bottom)] inserted sections, tip-outward order
      tip    : (x_tip, y_scale, z_bottom) or None
      src_i  : nearest valid station (cap shape source)
    """
    idxs = [i for i, o in enumerate(outers) if o is not None]
    d_of = lambda i: sgn * (float(outers[i][0, 0]) - x_tip)
    real = sorted((i for i in idxs if d_of(i) > 1e-9), key=d_of)
    if not real:
        return [], [], None, None, 0.0
    src_i = real[0]
    d_src = d_of(src_i)

    # Keel-line height at the join (keel outline interpolated at d = a)
    d_pts = np.array([d_of(i) for i in real])
    kz_pts = np.array([float(outers[i][:, 2].min()) for i in real])
    z_join = float(np.interp(a, d_pts, kz_pts))
    rise_span = z_sheer - z_join

    def z_bottom(d):
        base = float(np.interp(d, d_pts, kz_pts))   # hull's own keel line
        return max(z_join + rise_span * float(_stem_rise(d, a)), base)

    raises = [(i, z_bottom(d_of(i))) for i in real if d_of(i) < a]

    y_src = max(float(outers[src_i][:, 1].max()), 1e-12)
    caps = []
    for f in np.linspace(0.0, 1.0, _STEM_N_INSERT + 2)[1:-1]:
        d_k = f * d_src
        s = max(d_k / d_src, tip_half_beam / y_src)
        caps.append((x_tip + sgn * d_k, s, z_bottom(d_k)))

    tip = (x_tip, tip_half_beam / y_src, z_bottom(0.0))
    return raises, caps, tip, src_i, a


def build_complete_canoe(offsets_uw_m, L_m, T_m, depth_m,
                         flare_deg=0.0, gw_width_m=0.0, gw_height_m=0.0,
                         t_m=0.0127, n_freeboard=8, stem_rocker_m=None):
    """
    Assemble the final as-built canoe geometry from the optimized underwater
    hull plus the winning upper-section parameters of the structural sweep.

    Every non-degenerate station produces one *closed* cross-section loop:

        centerline keel → outer hull (underwater) → flared freeboard wall
        → gunwale rail outline (up, inboard, down) → exposed rail underside
        → inner wall (outer offset inward by t) → centerline inner keel
        → port-side mirror back to start

    Point counts are identical at every station (the upper-section parameters
    are per-canoe constants), so consecutive loops loft directly into a
    watertight solid shell.

    Simplifications (viewing/printing geometry, not analysis geometry):
    - gunwale fillets are omitted (rectangular rail outline)
    - the inner surface is a constant-thickness normal offset; stations whose
      waterline half-breadth is below 1.5 t are treated as degenerate (bow /
      stern tips where the shell walls merge) and closed with end caps

    Parameters
    ----------
    offsets_uw_m : (n_stations x n_waterlines) underwater half-breadths [m]
    L_m          : waterline length [m]
    T_m          : design draft [m]  (top row of offsets_uw_m sits at z = T)
    depth_m      : mold depth from keel to sheer [m]; must exceed T_m
    flare_deg    : outward flare of the freeboard walls above the waterline
    gw_width_m   : gunwale rail width (inboard from the outer face) [m]
    gw_height_m  : gunwale rail height above the sheer [m]
    t_m          : shell thickness [m]
    n_freeboard  : number of grid rows added between waterline and sheer
    stem_rocker_m: bow/stern stem-rocker sweep length [m] — the fore-aft
                   distance over which the keel line rises to the sheer at
                   each end.  Plan view stays POINTED; side view gets a
                   rounded stem profile (ETS-style rocker).
                   None (default) → auto via suggest_stem_rocker_m();
                   0.0 → legacy full-depth vertical stem slivers;
                   > 0 → requested sweep (clamped to [1 mm, 0.25 L])

    Returns
    -------
    dict with keys:
      x_m      : (n_stations,) station positions [m]
      loops    : list of (M, 3) closed full-section loops, or None (degenerate).
                 When stem rocker is active this list additionally contains
                 inserted cap sections near the bow/stern, so it can be LONGER
                 than n_stations; loops remain ordered bow → stern and all
                 share one point count.
      outer    : list of (n_pts, 3) starboard outer-wall points (keel → sheer)
      inner    : list of (n_pts, 3) starboard inner-wall points (index-matched)
      rail     : list of (4, 3) rail end-face corner points, or None
                 (outer/inner/rail are per-station and describe the sections
                 before the stem-rocker bottom raise)
      has_rail : bool — whether a gunwale rail is present
      stem_rocker_bow_m / stem_rocker_stern_m : applied sweep lengths [m]
    """
    offsets = np.asarray(offsets_uw_m, dtype=float)
    n_st, n_wl = offsets.shape
    if depth_m <= T_m + 1e-9:
        raise ValueError(
            f"Mold depth ({depth_m:.4f} m) must exceed draft ({T_m:.4f} m).")

    x_coords = np.linspace(0.0, float(L_m), n_st)
    z_uw = np.linspace(0.0, float(T_m), n_wl)
    z_fb = np.linspace(float(T_m), float(depth_m), n_freeboard + 1)[1:]
    tan_th = np.tan(np.radians(float(flare_deg)))
    cos_th = max(np.cos(np.radians(float(flare_deg))), 1e-9)
    t_h = float(t_m) / cos_th          # horizontal wall thickness of flared wall

    has_rail = gw_width_m > 1e-6 and gw_height_m > 1e-6
    # Rail must reach at least the inner wall face; the 1 µm nudge prevents
    # coincident loop vertices (zero-length edges) when gw_width == t_h.
    gw_eff = max(float(gw_width_m), t_h + 1e-6) if has_rail else 0.0

    degen_y = 1.5 * float(t_m)         # waterline half-breadth below this: skip

    # Fine-entry sections have y = 0 at several waterlines; mirrored copies
    # of on-centerline points would coincide and break mesh manifoldness.
    # Interior points are nudged 0.2 mm off the centerline (a visually
    # invisible wedge); only the keel anchors stay at y = 0.
    _EPS_Y = 2e-4

    def _make_section(outer_in: np.ndarray, rail_w: float):
        """
        Assemble one closed full section from a starboard OUTER chain [y, z]
        (keel → sheer).  Single source of truth for wall thickness: the
        inner chain is always RE-DERIVED from the outer at full shell
        thickness — solid concrete where the halves merge — so tip/cap
        sections built from a scaled outer keep the true wall thickness
        instead of a proportionally thinned copy (fix plan C1).

        Returns (loop2d, outer, inner, rail_face).
        """
        outer = outer_in.copy()
        outer[1:, 0] = np.maximum(outer[1:, 0], _EPS_Y)
        outer[0, 0] = 0.0              # anchor keel point on the centerline
        z_keel = float(outer[0, 1])

        # Inner wall: constant-thickness normal offset.  Where the section
        # is thinner than the shell the inward offset collapses onto the
        # outer skin; physically that region is solid concrete, so the
        # interior surface sits one thickness above the outer skin there.
        # The 2x nudge keeps squeezed points distinct from the outer chain.
        inner = _offset_inward(outer, float(t_m))
        squeezed = (inner[:, 0] < 2 * _EPS_Y) | (inner[:, 1] < z_keel + float(t_m))
        inner[squeezed, 0] = 2 * _EPS_Y
        inner[squeezed, 1] = np.minimum(outer[squeezed, 1] + float(t_m),
                                        float(depth_m))
        inner[:, 0] = np.maximum(inner[:, 0], 2 * _EPS_Y)
        # The solid-concrete floor sits one shell thickness above the LOCAL
        # keel (z_keel follows the rocker), never above the baseline z = 0.
        inner[:, 1] = np.clip(inner[:, 1], z_keel + float(t_m), float(depth_m))
        inner[0] = (0.0, z_keel + float(t_m))   # centerline inner keel
        y_sheer = outer[-1, 0]
        # 2·EPS floor (not 0): a zero here would mirror into a duplicate
        # centerline vertex on narrow cap sections and break manifoldness.
        inner[-1] = (max(y_sheer - t_h, 2 * _EPS_Y), float(depth_m))
        # Strictly increasing interior heights: on strongly-raised stem
        # sections the solid-region ceiling (outer_z + t clipped at the
        # sheer) lands several points exactly at depth, and coincident
        # mirrored vertices break manifoldness.  Walk down from the sheer
        # enforcing a 0.01 mm separation.
        for k in range(len(inner) - 2, -1, -1):
            if inner[k, 1] > inner[k + 1, 1] - 1e-5:
                inner[k, 1] = inner[k + 1, 1] - 1e-5

        # Gunwale rail outline (outer face flush with the wall)
        if has_rail:
            # Rail inner corner: strictly between the centerline nudge and
            # the inner sheer point (1 µm inboard of it), so narrow cap
            # sections never produce coincident or centerline-crossing rail
            # vertices.  Real stations keep y_sheer − rail_w unchanged.
            corner = min(max(y_sheer - float(rail_w), _EPS_Y),
                         float(inner[-1, 0]) - 1e-6)
            rail_pts = np.array([
                [y_sheer, depth_m + gw_height_m],
                [corner,  depth_m + gw_height_m],
                [corner,  depth_m],
            ])
            rail_face = np.array([
                [y_sheer, depth_m],
                [y_sheer, depth_m + gw_height_m],
                [corner,  depth_m + gw_height_m],
                [corner,  depth_m],
            ])
        else:
            rail_pts = np.empty((0, 2))
            rail_face = None

        # Close the half-section and mirror to port
        half = np.vstack([outer, rail_pts, inner[::-1]])
        port = half[1:-1].copy()
        port[:, 0] *= -1.0
        loop2d = np.vstack([half, port[::-1]])
        return loop2d, outer, inner, rail_face

    loops, outers, inners, rails = [], [], [], []
    for i in range(n_st):
        y_wl = float(offsets[i, -1])
        if y_wl < degen_y:
            loops.append(None); outers.append(None)
            inners.append(None); rails.append(None)
            continue

        # ── outer wall: underwater + flared freeboard ────────────────────
        # Offset rows below the local rocker line arrive padded with y = 0.
        # Keeping them (nudged off-center) produced a razor-thin fin from
        # the baseline up to the true keel along the entire hull.  Trim to
        # the true keel (lowest wet row), keep exactly one padded row below
        # it as the centerline closure anchor, then arc-length resample back
        # to n_wl rows so every station keeps an identical point count
        # (required for direct loop-to-loop lofting).
        wet = np.flatnonzero(offsets[i, :] > _PAD_TOL)
        start = max(int(wet[0]) - 1, 0)
        uw = _resample_polyline(
            np.column_stack([offsets[i, start:], z_uw[start:]]), n_wl)
        y_outer = np.concatenate([uw[:, 0], y_wl + (z_fb - T_m) * tan_th])
        z_outer = np.concatenate([uw[:, 1], z_fb])

        loop2d, outer, inner, rail_face = _make_section(
            np.column_stack([y_outer, z_outer]), gw_eff)

        _x = np.full((len(loop2d), 1), x_coords[i])
        loops.append(np.hstack([_x, loop2d]))
        outers.append(np.column_stack(
            [np.full(len(outer), x_coords[i]), outer[:, 0], outer[:, 1]]))
        inners.append(np.column_stack(
            [np.full(len(inner), x_coords[i]), inner[:, 0], inner[:, 1]]))
        rails.append(
            np.column_stack([np.full(4, x_coords[i]),
                             rail_face[:, 0], rail_face[:, 1]])
            if rail_face is not None else None)

    valid = [i for i, lp in enumerate(loops) if lp is not None]
    if not valid:
        raise ValueError(
            "All stations degenerate — hull is narrower than 1.5x the shell "
            "thickness everywhere. Check units (expected meters).")

    # ── True hull tips ────────────────────────────────────────────────────
    # The terminal grid stations carry zero beam (the hull tapers to a point
    # at x = 0 and x = L) and cannot host a full shell section, but the mesh
    # must still reach them — otherwise exports get flat-capped one station
    # early.  Tip/cap sections are REBUILT from the source station's outer
    # chain (plan taper scales only the outer skin, the stem rocker remaps
    # its heights) and re-sectioned through _make_section — scaling the
    # closed loop scaled the wall thickness with it, which made the ends
    # paper-thin (fix plan C1).  Near the tips the halves merge and the
    # sections become properly SOLID concrete.
    # 1 mm half-beam: a 2 mm stem edge — visually/physically a sharp point
    # (concrete cannot be cast thinner), while keeping every vertex above
    # mesh quantization so the sliver loop stays manifold.
    _TIP_HALF_BEAM = 1e-3

    def _rebuilt_cap(src_i: int, x_pos: float, y_scale: float,
                     z_bottom) -> np.ndarray:
        o = outers[src_i][:, 1:3].copy()          # source outer chain [y, z]
        z_min = float(o[:, 1].min())
        if z_bottom is not None and z_bottom > z_min + 1e-12:
            # Stem-rocker height remap: bottom rises to z_bottom, sheer fixed
            o[:, 1] = (float(depth_m) - (float(depth_m) - float(z_bottom))
                       * (float(depth_m) - o[:, 1])
                       / max(float(depth_m) - z_min, 1e-12))
        o[:, 0] *= y_scale
        loop2d, _, _, _ = _make_section(o, gw_eff * y_scale)
        _x = np.full((len(loop2d), 1), x_pos)
        return np.hstack([_x, loop2d])

    def _tip_scale(src_i: int) -> float:
        return _TIP_HALF_BEAM / max(float(outers[src_i][:, 1].max()), 1e-12)

    # ── Bow / stern termination ───────────────────────────────────────────
    # stem_rocker_m = None → auto sweep from suggest_stem_rocker_m()
    # stem_rocker_m = 0.0  → legacy full-depth vertical stems (pinched slivers)
    # stem_rocker_m > 0    → requested sweep length [m]
    rocker_bow = rocker_stern = 0.0
    if stem_rocker_m is not None and float(stem_rocker_m) <= 1e-9:
        # Legacy: carry the nearest valid section to the true tip as a 1 mm
        # sliver (plan view pointed, side view full-depth vertical)
        if valid[0] > 0:
            loops[0] = _rebuilt_cap(valid[0], float(x_coords[0]),
                                    _tip_scale(valid[0]), None)
        if valid[-1] < n_st - 1:
            loops[-1] = _rebuilt_cap(valid[-1], float(x_coords[-1]),
                                     _tip_scale(valid[-1]), None)
    else:
        # ETS-style ends: pointed in plan view, rounded (rocker) in side view
        L = float(L_m)
        if stem_rocker_m is None:
            a_bow = suggest_stem_rocker_m(
                float(x_coords[valid[0]] - x_coords[0]), L)
            a_stern = suggest_stem_rocker_m(
                float(x_coords[-1] - x_coords[valid[-1]]), L)
        else:
            a_bow = a_stern = float(np.clip(float(stem_rocker_m),
                                            1e-3, 0.25 * L))
        raises_b, caps_b, tip_b, src_b, rocker_bow = _stem_rocker_specs(
            outers, float(x_coords[0]), +1.0, a_bow,
            float(depth_m), _TIP_HALF_BEAM)
        raises_s, caps_s, tip_s, src_s, rocker_stern = _stem_rocker_specs(
            outers, float(x_coords[-1]), -1.0, a_stern,
            float(depth_m), _TIP_HALF_BEAM)
        # Real stations inside the sweep: rebuilt from their OWN outer chain
        # with the raised bottom (keeps full wall thickness through the remap)
        for i, zb in raises_b + raises_s:
            loops[i] = _rebuilt_cap(i, float(x_coords[i]), 1.0, zb)
        cap_loops_b = [_rebuilt_cap(src_b, xp, s, zb) for xp, s, zb in caps_b]
        cap_loops_s = [_rebuilt_cap(src_s, xp, s, zb) for xp, s, zb in caps_s]
        if tip_b is not None:
            loops[0] = _rebuilt_cap(src_b, *tip_b)
        if tip_s is not None:
            loops[-1] = _rebuilt_cap(src_s, *tip_s)
        # Keep loops ordered bow → stern for the loft
        loops = ([loops[0]] + cap_loops_b + loops[1:-1]
                 + cap_loops_s[::-1] + [loops[-1]])

    return {
        "x_m": x_coords,
        "loops": loops,
        "outer": outers,
        "inner": inners,
        "rail": rails,
        "has_rail": has_rail,
        "stem_rocker_bow_m": rocker_bow,
        "stem_rocker_stern_m": rocker_stern,
    }


def calculate_shell_properties(x_stations, z_heights, y_matrix, thickness_in):
    """
    Calculates Area, Inertia, and Neutral Axis for every hull slice.

    Updated for 2D Pour Strategy: Now returns split areas (A_top, A_bot) 
    separated by the moving Neutral Axis.
    """
    EPS = 1e-6  # Epsilon floor for all division-critical quantities

    num_stations = len(x_stations)
    A_total, A_top, A_bot = np.zeros(num_stations), np.zeros(num_stations), np.zeros(num_stations)
    I_array, Q_array = np.zeros(num_stations), np.zeros(num_stations)
    c_top_array, c_bot_array = np.zeros(num_stations), np.zeros(num_stations)
    width_array = np.zeros(num_stations)
    # Cumulative shell area below each grid height (fix plan C3): lets the
    # evaluator split the weight integral at an arbitrary pour height z*(x)
    # instead of only at the neutral axis.  cum_Az / cum_Iz are the matching
    # cumulative first/second moments about the keel baseline, needed for
    # composite (transformed-section) properties of arbitrary pour zones.
    z_grid = np.asarray(z_heights, dtype=float)
    cum_area = np.zeros((num_stations, len(z_grid)))
    cum_Az   = np.zeros((num_stations, len(z_grid)))
    cum_Iz   = np.zeros((num_stations, len(z_grid)))
    # Cumulative lateral inertia about the vertical centerplane (∫y²) and the
    # per-grid-height half-breadth — the raw material for the transformed
    # transverse (hoop) check.
    cum_Iy   = np.zeros((num_stations, len(z_grid)))
    yhalf    = np.zeros((num_stations, len(z_grid)))
    # Stations with no formable section (the stem tips).  Collected rather than
    # absorbed -- see the repair after the loop.
    degenerate = []

    for i in range(num_stations):
        y_vals = y_matrix[i, :]
        valid_idx = y_vals > 0.001
        y, z = y_vals[valid_idx], z_heights[valid_idx]

        if len(y) < 2:
            # DEGENERATE STATION -- fewer than two half-breadths clear the
            # threshold, so no perimeter can be formed here.  Recorded, never
            # absorbed: this branch silently assigned A = 1e-6 to both stem
            # stations at every resolution, which is a genuine STEP in A(x)
            # (8.53 in^2 -> 0) and cost 2.07 lb at 41 stations and 0.50 lb at
            # 161.  The area is repaired after the loop by _taper_stem_area();
            # what this must never do again is look like a real section.
            degenerate.append(i)
            A_total[i], A_top[i], A_bot[i] = EPS, EPS/2, EPS/2
            I_array[i], Q_array[i] = EPS, EPS
            c_top_array[i], c_bot_array[i] = EPS, EPS
            width_array[i] = EPS
            yhalf[i, :] = EPS
            continue

        dy, dz = np.diff(y), np.diff(z)
        ds = np.sqrt(dy**2 + dz**2)
        dA = ds * thickness_in
        z_c = (z[:-1] + z[1:]) / 2.0

        # Each perimeter segment's own second moment about its centroid
        # (the ds³/12·sin²θ term), so cum_Iz matches the homogeneous I below.
        sin_theta = np.divide(dz, ds, out=np.zeros_like(dz), where=ds != 0)
        dI_own = (thickness_in * ds ** 3 / 12.0) * (sin_theta ** 2)

        # Cumulative full-section (×2 for port+starboard) moments below height.
        # searchsorted maps each grid height to the running sum up to it.
        idx = np.searchsorted(z_c, z_grid, side="right")
        cs_A = np.concatenate(([0.0], np.cumsum(dA)))
        cs_Az = np.concatenate(([0.0], np.cumsum(dA * z_c)))
        cs_Iz = np.concatenate(([0.0], np.cumsum(dA * z_c ** 2 + dI_own)))
        cum_area[i, :] = 2.0 * cs_A[idx]
        cum_Az[i, :]   = 2.0 * cs_Az[idx]
        cum_Iz[i, :]   = 2.0 * cs_Iz[idx]

        # Lateral inertia about the vertical centerplane: parallel-axis y_c²·dA
        # plus each segment's own horizontal second moment (cos²θ term).
        y_c = (y[:-1] + y[1:]) / 2.0
        cos_theta = np.divide(dy, ds, out=np.zeros_like(dy), where=ds != 0)
        dIy_own = (thickness_in * ds ** 3 / 12.0) * (cos_theta ** 2)
        cs_Iy = np.concatenate(([0.0], np.cumsum(dA * y_c ** 2 + dIy_own)))
        cum_Iy[i, :] = 2.0 * cs_Iy[idx]
        # Half-breadth at each grid height (for each zone's outboard fibre).
        yhalf[i, :] = np.interp(z_grid, z, y, left=EPS, right=y[-1])

        Area_half = max(np.sum(dA), EPS)
        Z_bar = np.sum(dA * z_c) / Area_half
        
        # Split areas relative to the Neutral Axis
        top_mask = z_c >= Z_bar
        bot_mask = z_c < Z_bar
        A_bot[i] = np.sum(dA[bot_mask]) * 2.0
        A_top[i] = np.sum(dA[top_mask]) * 2.0
        A_total[i] = Area_half * 2.0

        # First Moment of Area (Q) at NA for shear stress: Q = A_top * (centroid_top - NA)
        if np.any(top_mask):
            z_top_centroid = np.sum(dA[top_mask] * z_c[top_mask]) / np.sum(dA[top_mask])
            Q_array[i] = (A_top[i] / 2.0) * (z_top_centroid - Z_bar) * 2.0 # Total Q for full section
        else:
            Q_array[i] = EPS

        sin_theta = np.divide(dz, ds, out=np.zeros_like(dz), where=ds!=0)
        I_local = (thickness_in * ds**3 / 12.0) * (sin_theta**2)
        I_parallel = dA * (z_c - Z_bar)**2
        
        I_array[i] = max(np.sum(I_local + I_parallel) * 2.0, EPS)
        c_bot_array[i] = Z_bar                   
        c_top_array[i] = max(np.max(z) - Z_bar, EPS)
        width_array[i] = max(np.max(y) * 2.0 / 12.0, EPS)
        
    # ── STEM STATIONS: taper, don't zero ──────────────────────────────────
    # A(x) is SHELL area (girth x t), and at a knife-edge stem the girth does
    # not vanish -- it tends to twice the section height.  Measured on the live
    # hull, A(x) approaches ~8.53 in^2 as x -> 0 (log-log slope +0.081, i.e. a
    # finite limit, NOT the x^1.5 of a solid section).  Forcing EPS there put a
    # step of the full 8.53 in^2 into the first panel at every resolution.
    #
    # The repair is a linear taper of A itself, extrapolated from the two
    # nearest REAL stations, which lands just BELOW the neighbour as a real
    # stem must.  Carrying the neighbour's value across instead overstates it.
    # Worth +2.07 lb at 41 stations and +0.50 lb at 161.
    if degenerate:
        _xs = np.asarray(x_stations, dtype=float)
        for i in degenerate:
            # nearest two stations that are NOT degenerate, on the hull side
            step = 1 if i < num_stations / 2 else -1
            j, k = i + step, i + 2 * step
            if not (0 <= j < num_stations and 0 <= k < num_stations):
                continue
            if j in degenerate or k in degenerate:
                continue
            span = _xs[k] - _xs[j]
            if abs(span) < 1e-12:
                continue
            slope = (A_total[k] - A_total[j]) / span
            A_total[i] = max(A_total[j] + slope * (_xs[i] - _xs[j]), 0.0)
            # THE WHOLE SECTION, not just A_in2.  The multi-zone weight
            # integral reads cum_area (via _cum_at), not A_in2, so tapering the
            # area alone left the stem still weightless on the path that
            # actually matters -- and left cum[:, -1] != A_in2, which is the
            # table's own consistency invariant.  Scale the neighbour's
            # cumulative profile by the area ratio: same vertical distribution,
            # correct total.  Heights (c_top/c_bot) are intensive and carry
            # across unscaled, so sheer_in is a real section depth rather than
            # EPS -- without it every pour-zone boundary here collapses to zero
            # and the zones pick up no area no matter what cum_area says.
            _ratio = (A_total[i] / A_total[j]) if A_total[j] > 0 else 0.0
            _tot = A_top[j] + A_bot[j]
            _f = (A_top[j] / _tot) if _tot > 0 else 0.5
            A_top[i], A_bot[i] = A_total[i] * _f, A_total[i] * (1.0 - _f)
            cum_area[i, :] = cum_area[j, :] * _ratio
            cum_Az[i, :]   = cum_Az[j, :] * _ratio
            cum_Iz[i, :]   = cum_Iz[j, :] * _ratio
            cum_Iy[i, :]   = cum_Iy[j, :] * _ratio
            c_top_array[i], c_bot_array[i] = c_top_array[j], c_bot_array[j]
            I_array[i] = max(I_array[j] * _ratio, EPS)
            Q_array[i] = max(Q_array[j] * _ratio, EPS)
            # yhalf STAYS AT EPS -- this station carries MASS, not STRESS.
            #
            # structural_station_mask excludes a station when its area is below
            # DEGENERATE_SECTION_A_IN2 *or* its half-breadth is under 5% of the
            # hull's widest, because transverse stress goes as M*y/I_lat with
            # I_lat ~ b^2*A: as b -> 0 sigma_y diverges as 1/b^2 while sigma_x
            # stays finite.  Both transverse gates have had to learn that
            # separately, the second time at the cost of a sweep reporting 0
            # feasible designs out of 10,350.
            #
            # Tapering cum_area lifts this station past the AREA test, so the
            # BREADTH test is the only thing still excluding it -- and a stem
            # really does have ~zero half-breadth, so leaving yhalf at EPS is
            # both the correct physics and what keeps the exclusion working.
            # Scaling it here re-opened the divergence: the tip governed the
            # Kupfer gate at util 0.4513 where the contract requires 0.
            # width also stays EPS: a knife-edge stem has no waterplane, and
            # the overhang model already treats this region as un-buoyed.
        logger.warning(
            "degenerate stem station(s) at index %s (x = %s ft): fewer than "
            "two half-breadths above %.3f in, so no section perimeter exists "
            "there. Shell area TAPERED from the two nearest real stations "
            "(now %s in^2); it is NOT a measured section.",
            degenerate, [round(float(x_stations[i]), 4) for i in degenerate],
            0.001, [round(float(A_total[i]), 4) for i in degenerate])

    return ShellProperties(
        A_in2=A_total,
        A_top_in2=A_top,
        A_bot_in2=A_bot,
        I_in4=I_array,
        Q_in3=Q_array,
        c_top=c_top_array,
        c_bot=c_bot_array,
        width_ft=width_array,
        cum_area_in2=cum_area,
        z_grid_in=z_grid,
        cum_Az_in3=cum_Az,
        cum_Iz_in4=cum_Iz,
        cum_Iy_in4=cum_Iy,
        yhalf_in=yhalf,
    )