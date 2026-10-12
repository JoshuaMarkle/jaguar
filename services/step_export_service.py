# services/step_export_service.py
"""
Hull geometry exporters for the ASCE Concrete Canoe suite.

NURBS lofting (makeLoft) was abandoned: OpenCASCADE pole singularities at the
bow/stern produce self-intersecting geometry that cannot be fixed reliably in
code.  Two replacement strategies are provided:

    export_wireframe_step  — CadQuery STEP of bare cross-section splines.
                             Import into SolidWorks; use Insert → Surface → Loft
                             with the curves as profiles for a distortion-free skin.

    export_direct_stl      — Binary STL via structured quad mesh, bypassing
                             CadQuery / OpenCASCADE entirely.  Drop directly into
                             Bambu Studio or any slicer.

No Qt imports.
"""
from __future__ import annotations

import struct
import numpy as np
from scipy.interpolate import interp1d
from errors import domain_only

# ── Internal constants ────────────────────────────────────────────────────────
_DEGEN_THRESHOLD = 1e-5   # m  — stations narrower than this are skipped
_CENTERLINE_TOL  = 1e-6   # m  — half-breadths below this count as centerline padding
_RESAMPLE_N      = 25     # guaranteed point count per half-profile after resampling
# All internal geometry is metric [m], but STL carries no units and CadQuery
# STEP declares millimetres, so every consumer (Fusion 360, OnShape, slicers)
# reads raw meter values 1000x too small.  Scale to mm at write time.
_EXPORT_SCALE = 1000.0    # m → mm


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _stations_from_offsets(
    offsets: np.ndarray,
    x_coords: np.ndarray,
    z_coords: np.ndarray,
) -> list[np.ndarray]:
    """Expand the half-breadth grid into per-station (n_wl, 3) [x, y, z] arrays."""
    return [
        np.column_stack([
            np.full(len(z_coords), x, dtype=float),
            offsets[i].astype(float),
            np.asarray(z_coords, dtype=float),
        ])
        for i, x in enumerate(x_coords)
    ]


def _arclength_resample(pts: np.ndarray, num_points: int) -> np.ndarray:
    """
    Resample a 3-D polyline to exactly `num_points` rows, uniformly spaced by
    arc length.  Duplicate consecutive rows are dropped first so the
    interpolation parameter stays strictly increasing.
    """
    seg_lens = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    pts = pts[np.concatenate(([True], seg_lens > 1e-12))]
    if len(pts) < 2:
        return np.repeat(pts[:1], num_points, axis=0)

    cum_dists = np.concatenate(([0.0], np.cumsum(
        np.linalg.norm(np.diff(pts, axis=0), axis=1))))
    total_len = cum_dists[-1]

    if total_len < 1e-9:
        # Fully collapsed chord — index-sample directly
        idxs = np.round(np.linspace(0, len(pts) - 1, num_points)).astype(int)
        return pts[idxs]

    interp = interp1d(cum_dists / total_len, pts, axis=0,
                      kind='linear', assume_sorted=True)
    return interp(np.linspace(0.0, 1.0, num_points))   # (num_points, 3)


def _full_closed_section(half_pts: np.ndarray) -> np.ndarray:
    """
    Mirror a sanitized starboard half-profile into a full symmetric closed section.

    Layout of the returned array (shape: 2*N - 1, 3):
      half_pts[0]           keel              Y = 0 (centerline closure)
      half_pts[1..N-1]      starboard rise    Y positive, Z ascending
      mirrored [N-1..1]     port descent      Y negative, Z descending
      (loop closure returns to half_pts[0])

    The keel point is NOT duplicated on the port leg because it sits on the
    symmetry plane (Y = 0).  The waterline point IS mirrored so the sheer
    corner exists on both sides and the waterplane deck quads meet real loft
    vertices on the port edge.
    """
    port = half_pts[1:].copy()
    port[:, 1] *= -1          # mirror in Y
    port = port[::-1]         # descend from waterline back toward near-keel
    return np.vstack([half_pts, port])


def _tri_normal(p1, p2, p3) -> tuple:
    """Unit normal via cross product; returns (0, 0, 1) for degenerate faces."""
    v1 = np.asarray(p2, dtype=float) - np.asarray(p1, dtype=float)
    v2 = np.asarray(p3, dtype=float) - np.asarray(p1, dtype=float)
    n  = np.cross(v1, v2)
    ln = float(np.linalg.norm(n))
    return tuple(n / ln) if ln > 1e-12 else (0.0, 0.0, 1.0)


def _write_stl_triangle(f, p1, p2, p3) -> None:
    """Write one triangle record to an open binary STL file handle."""
    nx, ny, nz = _tri_normal(p1, p2, p3)
    f.write(struct.pack('<3f', nx, ny, nz))
    for v in (p1, p2, p3):
        f.write(struct.pack('<3f', float(v[0]), float(v[1]), float(v[2])))
    f.write(struct.pack('<H', 0))   # attribute byte count (unused)


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def sanitize_hull_data(
    raw_stations_data: list[np.ndarray],
    num_points: int = _RESAMPLE_N,
    y_tol: float = _CENTERLINE_TOL,
) -> list[np.ndarray | None]:
    """
    Clean raw station point data before any wire or triangle generation.

    Fixes two physical defects present in raw hull offset grids:

    1. **Keel flange** — waterline rows below the local rocker line arrive
       padded with Y = 0; lofting them produces a flat fin under the keel.
       For each station the "true keel" is the lowest-Z row with Y > y_tol.
       All padded rows below it are discarded, except the single row
       immediately beneath it, which is kept with Y snapped to exactly 0.0 as
       the centerline closure point — each section must still touch the
       symmetry plane so `_full_closed_section` mirrors it into a watertight
       closed loop.

    2. **Blunt tips** — the extreme bow/stern stations do not converge to a
       point, so exports end in a box.  Every point of the first and last
       stations is forced to Y = 0.0 exactly, collapsing each into a sharp
       2-D stem/sternpost line.  (For rounded tips, add a fillet in CAD using
       this edge as the guide.)  A tip station that arrives fully zero-padded
       inherits its Z-range from the nearest valid neighbour so the stem does
       not extend below the rocker line.

    3. **Array integrity** — trimming leaves stations with unequal point
       counts, so every surviving station is arc-length resampled back to
       exactly `num_points` rows; downstream loft/triangulation loops see a
       uniform grid.

    Interior stations that are entirely degenerate (max half-breadth below
    `_DEGEN_THRESHOLD`) are returned as None and must be skipped by callers,
    matching previous behaviour.

    Parameters
    ----------
    raw_stations_data : list of (n_i, 3) float arrays [x, y, z], one per
                        station, ordered bow → stern
    num_points        : rows per station after resampling
    y_tol             : half-breadths at or below this are centerline padding

    Returns
    -------
    list of (num_points, 3) float64 arrays (or None), same length/order as
    the input.
    """
    stations = [np.asarray(s, dtype=float) for s in raw_stations_data]
    n = len(stations)
    cleaned: list[np.ndarray | None] = []

    # ── 1. Remove the keel flange ────────────────────────────────────────────
    for pts in stations:
        pts = pts[pts[:, 2].argsort()]              # sort keel → waterline
        if float(pts[:, 1].max()) < _DEGEN_THRESHOLD:
            cleaned.append(None)                    # degenerate (tips rebuilt below)
            continue

        true_keel = int(np.flatnonzero(pts[:, 1] > y_tol)[0])
        start = max(true_keel - 1, 0)               # keep one closure row below
        pts = pts[start:].copy()
        if true_keel > 0:
            pts[0, 1] = 0.0                         # snap closure row to centerline
        cleaned.append(pts)

    # ── 2. Sharpen the bow / stern tips ──────────────────────────────────────
    if n >= 2:
        for tip in (0, n - 1):
            sec = cleaned[tip]
            if sec is None:
                # Fully zero-padded tip: rebuild as a vertical centerline line
                # spanning the Z-range of the nearest valid neighbour.
                order = range(1, n) if tip == 0 else range(n - 2, -1, -1)
                nb = next((cleaned[k] for k in order if cleaned[k] is not None),
                          None)
                if nb is None:
                    continue                        # every station degenerate
                x_tip = float(stations[tip][0, 0])
                z = np.linspace(nb[0, 2], nb[-1, 2], num_points)
                sec = np.column_stack([
                    np.full(num_points, x_tip),
                    np.zeros(num_points),
                    z,
                ])
            else:
                sec = sec.copy()
                sec[:, 1] = 0.0                     # collapse to sharp 2-D line
            cleaned[tip] = sec

    # ── 3. Resample to a uniform point count ─────────────────────────────────
    return [
        _arclength_resample(sec, num_points) if sec is not None else None
        for sec in cleaned
    ]


def export_wireframe_step(
    offsets: np.ndarray,
    L: float,
    T: float,
    filepath: str,
) -> None:
    """
    Export a 3D cage of cross-section splines as a STEP file.

    Each cross-section is a fully closed, symmetric periodic spline
    (starboard + port mirror).  Import the STEP into SolidWorks and use
    Insert → Surface → Loft with these curves as profiles.  SolidWorks'
    native solver handles the bow/stern singularities without distortion.

    Parameters
    ----------
    offsets  : (n_stations × n_waterlines) half-breadth array [m]
    L        : waterline length [m]
    T        : draft [m]
    filepath : destination path (.step or .stp)

    Raises
    ------
    ImportError  : cadquery is not installed
    RuntimeError : no valid stations remain after degenerate filtering
    """
    try:
        import cadquery as cq
        from cadquery import exporters
    except ImportError as exc:
        raise ImportError(
            "cadquery is not installed.  Run:  pip install cadquery"
        ) from exc

    n_stations, _n_wl = offsets.shape
    x_coords = np.linspace(0.0, L, n_stations)
    z_coords = np.linspace(0.0, T, _n_wl)

    print(f"Generating CAD wireframe ({n_stations} stations)...")
    half_profiles = sanitize_hull_data(
        _stations_from_offsets(offsets, x_coords, z_coords))
    # Scale to millimetres for CAD import (STEP declares mm)
    half_profiles = [hp * _EXPORT_SCALE if hp is not None else None
                     for hp in half_profiles]

    wires = []
    for half_pts in half_profiles:
        if half_pts is None:
            continue
        x = float(half_pts[0, 0])

        if float(half_pts[:, 1].max()) < _DEGEN_THRESHOLD:
            # Sharp bow/stern tip: a straight 2-D stem/sternpost line on the
            # centerplane.  Use it as the loft guide curve in SolidWorks
            # (add a fillet along it if a rounded tip is wanted).
            p_lo, p_hi = half_pts[0], half_pts[-1]
            try:
                edge = cq.Edge.makeLine(
                    cq.Vector(x, 0.0, float(p_lo[2])),
                    cq.Vector(x, 0.0, float(p_hi[2])))
                wires.append(cq.Wire.assembleEdges([edge]))
            except Exception as e:
                domain_only(e)   # a bug is not a skippable condition
                print(f"  Skipping tip x={x:.4f} m — line error: {e}")
            continue

        section_pts = _full_closed_section(half_pts)
        vectors = [cq.Vector(float(p[0]), float(p[1]), float(p[2]))
                   for p in section_pts]
        if len(vectors) < 4:
            continue

        try:
            edge = cq.Edge.makeSpline(vectors, periodic=True)
            wires.append(cq.Wire.assembleEdges([edge]))
        except Exception as e:
            domain_only(e)   # a bug is not a skippable condition
            print(f"  Skipping station x={x:.4f} m — spline error: {e}")

    if not wires:
        raise RuntimeError(
            "No valid cross-section wires generated. "
            "All stations may be degenerate — check hull offsets."
        )

    print(f"  Bundling {len(wires)} curves into STEP file...")
    try:
        compound = cq.Compound.makeCompound(wires)
        exporters.export(compound, filepath)
        print(f"Wireframe STEP exported: {filepath}")
    except Exception as exc:
        raise RuntimeError(f"CadQuery STEP export failed: {exc}") from exc


def export_direct_stl(
    offsets: np.ndarray,
    L: float,
    T: float,
    filepath: str,
) -> None:
    """
    Write a binary STL by triangulating the hull offset grid directly.

    Bypasses CadQuery / OpenCASCADE entirely.  The offset matrix is sanitized
    (`sanitize_hull_data`), converted into a structured quad mesh (two
    triangles per quad cell) and written as a binary STL.  Normals are
    computed analytically via cross product.

    Mesh regions:
      • Hull surface  — closed-loop quads between every pair of consecutive
        valid stations, including the wraparound edge.  The sanitized end
        stations are sharp centerline lines, so the loft collapses to a
        knife-edge stem/sternpost at both ends — no separate end caps needed.
      • Waterplane    — quads closing the top of the hull along the waterline

    Parameters
    ----------
    offsets  : (n_stations × n_waterlines) half-breadth array [m]
    L        : waterline length [m]
    T        : draft [m]
    filepath : destination path (.stl)

    Raises
    ------
    RuntimeError : file I/O failure
    """
    n_stations, _n_wl = offsets.shape
    x_coords = np.linspace(0.0, L, n_stations)
    z_coords = np.linspace(0.0, T, _n_wl)

    # ── Step 1: sanitize every station ───────────────────────────────────────
    print(f"Sanitizing {n_stations} station profiles...")
    half_profiles = [
        hp * _EXPORT_SCALE                     # m → mm for slicers/CAD
        for hp in sanitize_hull_data(
            _stations_from_offsets(offsets, x_coords, z_coords))
        if hp is not None
    ]
    if len(half_profiles) < 2:
        raise RuntimeError(
            "Fewer than two valid stations after sanitizing — check hull offsets.")

    full_sections = [_full_closed_section(hp) for hp in half_profiles]

    # ── Step 2: build triangle list ───────────────────────────────────────────
    triangles: list[tuple] = []

    # Hull surface: closed-loop quads between consecutive valid stations.
    # Lofting valid-to-valid (rather than index-adjacent) bridges any interior
    # degenerate stations without leaving holes; the wraparound edge closes
    # each loop, and the sharp tip sections collapse the ends to a point.
    # The single loop segment that crosses the top (starboard waterline →
    # port waterline) is skipped: the waterplane deck owns that surface, and
    # lofting it too would double-skin the top and break manifoldness.
    for sa, sb in zip(full_sections[:-1], full_sections[1:]):
        n_pts = min(len(sa), len(sb))
        wl_j = n_pts // 2                # loop index of the starboard waterline point
        for j in range(n_pts):
            if j == wl_j:
                continue
            j2 = (j + 1) % n_pts
            p1, p2, p3, p4 = sa[j], sb[j], sb[j2], sa[j2]
            triangles.append((p1, p2, p3))
            triangles.append((p1, p3, p4))

    # Waterplane deck: quads connecting starboard and port waterline edges
    # half_pts[-1] after Z-sort = widest point at Z = T (waterline)
    wl_stbd = [hp[-1]                                       for hp in half_profiles]
    wl_port = [np.array([hp[-1][0], -hp[-1][1], hp[-1][2]]) for hp in half_profiles]
    for i in range(len(wl_stbd) - 1):
        p1, p2 = wl_stbd[i],     wl_stbd[i + 1]
        p3, p4 = wl_port[i + 1], wl_port[i]
        triangles.append((p1, p3, p2))   # winding for upward-facing normal
        triangles.append((p1, p4, p3))

    # Drop zero-area triangles (collapsed quads at the sharp tips, deck
    # slivers where the waterline pinches to the centerline)
    def _area2(t) -> float:
        a, b, c = (np.asarray(p, dtype=float) for p in t)
        return float(np.linalg.norm(np.cross(b - a, c - a)))

    triangles = [t for t in triangles if _area2(t) > 1e-12]

    # ── Step 3: write binary STL ──────────────────────────────────────────────
    print(f"  Writing {len(triangles):,} triangles to binary STL...")
    try:
        with open(filepath, 'wb') as f:
            f.write(b'\x00' * 80)                            # 80-byte header (unused)
            f.write(struct.pack('<I', len(triangles)))        # uint32 triangle count
            for tri in triangles:
                _write_stl_triangle(f, tri[0], tri[1], tri[2])
        print(f"Direct STL exported: {filepath}")
    except OSError as exc:
        raise RuntimeError(f"STL write failed: {exc}") from exc


# ═══════════════════════════════════════════════════════════════════════════════
#  COMPLETE-CANOE EXPORT  (underwater hull + flared freeboard + gunwale + shell)
# ═══════════════════════════════════════════════════════════════════════════════

def _triangulate_polygon(poly: np.ndarray) -> list[tuple[int, int, int]]:
    """
    Ear-clipping triangulation of a simple (non-self-intersecting) 2-D polygon.

    Needed for the bow/stern end caps of the complete-canoe solid: the shell
    cross-section is strongly non-convex (a U-shaped channel), so a centroid
    fan would cross the cockpit void.  Falls back to a fan over any remainder
    if clipping stalls on collinear runs.

    Returns index triples into *poly* (no repeated closing vertex expected).
    """
    def _cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def _in_tri(p, a, b, c):
        d1, d2, d3 = _cross(a, b, p), _cross(b, c, p), _cross(c, a, p)
        has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (has_neg and has_pos)

    idx = list(range(len(poly)))
    # Orient counter-clockwise so 'ear' convexity tests are consistent
    signed2 = sum(_cross(poly[idx[k - 1]], poly[idx[k]], poly[idx[(k + 1) % len(idx)]])
                  for k in range(len(idx)))
    if signed2 < 0:
        idx.reverse()

    tris: list[tuple[int, int, int]] = []
    guard = 0
    while len(idx) > 3 and guard < 100_000:
        guard += 1
        n = len(idx)
        clipped = False
        for k in range(n):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % n]
            a, b, c = poly[i0], poly[i1], poly[i2]
            cr = _cross(a, b, c)
            if cr < -1e-14:                        # reflex corner — never an ear
                continue
            if cr <= 1e-14:
                # Collinear vertex (straight wall run): clip it as a zero-area
                # ear.  Emitting the degenerate triangle keeps the cap's
                # boundary edges identical to the loft's, so edge pairing
                # (manifoldness) is preserved.
                tris.append((i0, i1, i2))
                del idx[k]
                clipped = True
                break
            if any(_in_tri(poly[j], a, b, c)
                   for j in idx if j not in (i0, i1, i2)):
                continue
            tris.append((i0, i1, i2))
            del idx[k]
            clipped = True
            break
        if not clipped:
            break
    if len(idx) == 3:
        tris.append((idx[0], idx[1], idx[2]))
    elif len(idx) > 3:
        tris.extend((idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1))
    return tris


def export_complete_stl(canoe: dict, filepath: str) -> None:
    """
    Write the complete as-built canoe (shell solid with cockpit void) as a
    watertight binary STL.

    Mesh regions:
      • Loft — quads between every pair of consecutive valid station loops,
        including the wraparound edge (loops are closed curves)
      • Bow / stern caps — ear-clipped triangulation of the first and last
        valid full cross-sections (the U-shaped shell end faces)

    Parameters
    ----------
    canoe    : dict from geometry.build_complete_canoe (units: meters;
               written to file in MILLIMETRES — the de-facto STL standard)
    filepath : destination path (.stl)

    Raises
    ------
    RuntimeError : no valid stations, or file I/O failure
    """
    # Terminal entries are guaranteed present by build_complete_canoe (true
    # zero-beam tips arrive as pinched sliver loops at x = 0 / x = L), so the
    # end caps below always close the mesh at the real hull extremities.
    # Scaled to millimetres: STL has no unit field and every consumer
    # (Fusion 360, OnShape, Bambu Studio) assumes mm.
    loops = [lp * _EXPORT_SCALE for lp in canoe["loops"] if lp is not None]
    if len(loops) < 2:
        raise RuntimeError(
            "Fewer than two valid stations — cannot loft the complete canoe.")

    triangles: list[tuple] = []

    # Loft between consecutive valid loops (equal point counts by construction)
    for a, b in zip(loops[:-1], loops[1:]):
        m = len(a)
        for j in range(m):
            j2 = (j + 1) % m
            p1, p2, p3, p4 = a[j], b[j], b[j2], a[j2]
            triangles.append((p1, p2, p3))
            triangles.append((p1, p3, p4))

    # End caps: triangulate the actual section polygons in the (y, z) plane
    for loop, flip in ((loops[0], True), (loops[-1], False)):
        poly = loop[:, 1:3]
        for i0, i1, i2 in _triangulate_polygon(poly):
            tri = (loop[i0], loop[i2], loop[i1]) if flip else (loop[i0], loop[i1], loop[i2])
            triangles.append(tri)

    print(f"  Writing {len(triangles):,} triangles to binary STL...")
    try:
        with open(filepath, 'wb') as f:
            f.write(b'\x00' * 80)
            f.write(struct.pack('<I', len(triangles)))
            for tri in triangles:
                _write_stl_triangle(f, tri[0], tri[1], tri[2])
        print(f"Complete-canoe STL exported: {filepath}")
    except OSError as exc:
        raise RuntimeError(f"STL write failed: {exc}") from exc


def export_complete_wireframe_step(canoe: dict, filepath: str) -> None:
    """
    Export the complete canoe as a STEP wireframe of closed full-section
    splines (outer skin + gunwale outline + inner skin at every station).
    Written in millimetres (the unit STEP files declare).

    Import into OnShape / SolidWorks and loft the closed profiles to obtain
    the solid concrete shell directly.  Sharp gunwale corners are smoothed
    slightly by the periodic spline fit.

    Raises
    ------
    ImportError  : cadquery is not installed
    RuntimeError : no valid stations remain
    """
    try:
        import cadquery as cq
        from cadquery import exporters
    except ImportError as exc:
        raise ImportError(
            "cadquery is not installed.  Run:  pip install cadquery") from exc

    wires = []
    for loop in canoe["loops"]:
        if loop is None:
            continue
        loop = loop * _EXPORT_SCALE            # m → mm (STEP declares mm)
        vectors = [cq.Vector(float(p[0]), float(p[1]), float(p[2])) for p in loop]
        if len(vectors) < 4:
            continue
        try:
            edge = cq.Edge.makeSpline(vectors, periodic=True)
            wires.append(cq.Wire.assembleEdges([edge]))
        except Exception as e:
            domain_only(e)   # a bug is not a skippable condition
            print(f"  Skipping station x={loop[0][0]:.4f} m — spline error: {e}")

    if not wires:
        raise RuntimeError("No valid cross-section wires generated.")

    print(f"  Bundling {len(wires)} full-section curves into STEP file...")
    try:
        compound = cq.Compound.makeCompound(wires)
        exporters.export(compound, filepath)
        print(f"Complete-canoe wireframe STEP exported: {filepath}")
    except Exception as exc:
        raise RuntimeError(f"CadQuery STEP export failed: {exc}") from exc


# ── Legacy stub ───────────────────────────────────────────────────────────────

def generate_hull_step(
    offsets: np.ndarray,
    L: float,
    T: float,
    filepath: str,
) -> None:
    """
    Removed: NURBS solid lofting via makeLoft() abandoned due to uncorrectable
    pole singularities at the bow and stern.

    Replacements:
      export_wireframe_step(offsets, L, T, filepath)  →  STEP for SolidWorks
      export_direct_stl(offsets, L, T, filepath)      →  STL for Bambu Studio
    """
    raise NotImplementedError(
        "generate_hull_step() has been removed.\n"
        "  For SolidWorks lofting : call export_wireframe_step(offsets, L, T, filepath)\n"
        "  For slicer / 3D print  : call export_direct_stl(offsets, L, T, filepath)"
    )
