# tests/test_complete_hull.py
"""
Tests for the complete-canoe pipeline (roadmap Phase B):

  geometry.build_complete_canoe          — flared freeboard, gunwale rail,
                                           inner shell offset, degeneracy
  step_export_service.export_complete_stl — watertight manifold mesh
  domain.models.CanoeDesignState          — step recording, staleness,
                                           JSON round-trip
"""
from __future__ import annotations

import json
import struct
from collections import Counter

import numpy as np
import pytest

from geometry import build_complete_canoe
from services.step_export_service import export_complete_stl, _triangulate_polygon
from domain.models import CanoeDesignState

IN = 0.0254


# ═══════════════════════════════════════════════════════════════════
#  Test hull: smooth elliptical sections, zero width at bow/stern
# ═══════════════════════════════════════════════════════════════════

def _test_hull(n_st=21, n_wl=15, L=5.0, half_beam=0.4, T=0.3):
    z = np.linspace(0.0, T, n_wl)
    w = half_beam * np.sin(np.pi * np.linspace(0.0, 1.0, n_st))
    prof = np.sqrt(np.clip(1.0 - ((T - z) / T) ** 2, 0.0, 1.0))
    return np.outer(w, prof), L, T


MM = 1000.0        # exports are written in millimetres; geometry is in meters


def _build(flare=10.0, gw_w=1.0 * IN, gw_h=1.0 * IN, t=0.5 * IN, fb=0.15,
           rocker=0.0):
    offsets, L, T = _test_hull()
    return build_complete_canoe(
        offsets, L, T, depth_m=T + fb,
        flare_deg=flare, gw_width_m=gw_w, gw_height_m=gw_h, t_m=t,
        stem_rocker_m=rocker), L, T


def _rocker_hull(n_st=41, n_wl=15, L=6.0, half_beam=0.4, T=0.35, rocker=0.12):
    """
    Godzilla-realistic hull: the keel rises toward bow/stern (rocker) and
    every offset row below the local rocker line is PADDED WITH ZEROS —
    the exact input shape that once produced the razor-thin keel fin.
    """
    x = np.linspace(0.0, L, n_st)
    z = np.linspace(0.0, T, n_wl)
    zk = rocker * (2.0 * x / L - 1.0) ** 2          # local true keel height
    w = half_beam * np.sin(np.pi * x / L) ** 0.9
    offsets = np.zeros((n_st, n_wl))
    for i in range(n_st):
        for k in range(n_wl):
            if z[k] >= zk[i] - 1e-12:
                frac = (z[k] - zk[i]) / max(T - zk[i], 1e-9)
                offsets[i, k] = w[i] * np.sqrt(max(frac, 0.0))
    return offsets, x, z, zk, L, T


# ═══════════════════════════════════════════════════════════════════
#  build_complete_canoe
# ═══════════════════════════════════════════════════════════════════

class TestBuilder:
    def test_station_count_and_pinched_tips(self):
        canoe, L, T = _build()
        assert len(canoe["loops"]) == 21
        # Zero-width bow/stern tips must be present as pinched sliver loops
        # at the TRUE tip x — never dropped (that flat-capped the hull one
        # station early).
        for tip, x_tip in ((canoe["loops"][0], 0.0), (canoe["loops"][-1], L)):
            assert tip is not None
            assert np.allclose(tip[:, 0], x_tip)
            assert np.abs(tip[:, 1]).max() <= 1e-3 + 1e-9
        assert canoe["loops"][10] is not None

    def test_stl_mesh_reaches_true_tips(self, tmp_path):
        canoe, L, T = _build()
        out = tmp_path / "tips.stl"
        export_complete_stl(canoe, str(out))
        tris = _read_stl_triangles(out)
        v = np.array([p for t in tris for p in t]) / MM   # file is mm → m
        assert v[:, 0].min() == pytest.approx(0.0, abs=1e-9)
        assert v[:, 0].max() == pytest.approx(5.0, abs=1e-6)
        bow = v[np.isclose(v[:, 0], 0.0, atol=1e-9)]
        assert np.abs(bow[:, 1]).max() <= 1e-3 + 1e-6, \
            "bow does not taper to a point"

    def test_stl_written_in_millimetres(self, tmp_path):
        """STL has no unit field; consumers assume mm.  A 5 m hull must
        therefore span 5000 units in the file."""
        canoe, L, T = _build()
        out = tmp_path / "units.stl"
        export_complete_stl(canoe, str(out))
        v = np.array([p for t in _read_stl_triangles(out) for p in t])
        assert v[:, 0].max() == pytest.approx(5000.0, rel=1e-6)

    def test_uniform_loop_point_counts(self):
        canoe, _, _ = _build()
        counts = {len(lp) for lp in canoe["loops"] if lp is not None}
        assert len(counts) == 1, f"Non-uniform loop sizes: {counts}"

    def test_flare_applied_geometrically(self):
        flare = 10.0
        fb = 0.15
        canoe, L, T = _build(flare=flare, fb=fb)
        outer = canoe["outer"][10]          # (n, 3) columns [x, y, z]
        y_wl = 0.4                           # midship waterline half-breadth
        y_sheer_expected = y_wl + fb * np.tan(np.radians(flare))
        assert outer[-1][2] == pytest.approx(T + fb)
        assert outer[-1][1] == pytest.approx(y_sheer_expected, rel=1e-6)

    def test_inner_shell_offset_thickness(self):
        t = 0.5 * IN
        canoe, _, _ = _build(t=t)
        outer = canoe["outer"][10]
        inner = canoe["inner"][10]
        # On the near-vertical freeboard wall the normal offset should be ~t
        k = len(outer) - 4
        dist = np.hypot(outer[k][1] - inner[k][1], outer[k][2] - inner[k][2])
        assert dist == pytest.approx(t, rel=0.25)
        # Inner keel sits exactly one thickness above the outer keel
        assert inner[0][1] == pytest.approx(0.0)
        assert inner[0][2] == pytest.approx(t)

    def test_gunwale_rail_optional(self):
        with_rail, _, _ = _build(gw_w=1.0 * IN, gw_h=1.0 * IN)
        no_rail, _, _   = _build(gw_w=0.0, gw_h=0.0)
        assert with_rail["has_rail"] and not no_rail["has_rail"]
        n_with = len(next(lp for lp in with_rail["loops"] if lp is not None))
        n_wo   = len(next(lp for lp in no_rail["loops"] if lp is not None))
        assert n_with == n_wo + 6            # 3 rail points per side

    def test_depth_must_exceed_draft(self):
        offsets, L, T = _test_hull()
        with pytest.raises(ValueError):
            build_complete_canoe(offsets, L, T, depth_m=T)

    def test_all_degenerate_raises(self):
        offsets, L, T = _test_hull(half_beam=0.001)   # narrower than 1.5 t
        with pytest.raises(ValueError):
            build_complete_canoe(offsets, L, T, depth_m=T + 0.15, t_m=0.0127)


# ═══════════════════════════════════════════════════════════════════
#  export_complete_stl — watertightness
# ═══════════════════════════════════════════════════════════════════

def _read_stl_triangles(path):
    with open(path, "rb") as f:
        f.seek(80)
        (n_tri,) = struct.unpack("<I", f.read(4))
        tris = []
        for _ in range(n_tri):
            rec = struct.unpack("<12fH", f.read(50))
            v = rec[3:12]
            tris.append(((v[0], v[1], v[2]),
                         (v[3], v[4], v[5]),
                         (v[6], v[7], v[8])))
    return tris


def _edge_counts(tris, decimals=6):
    q = lambda p: tuple(round(c, decimals) for c in p)
    edges = Counter()
    for a, b, c in tris:
        for p1, p2 in ((a, b), (b, c), (c, a)):
            k1, k2 = q(p1), q(p2)
            if k1 == k2:
                continue                     # zero-length edge of a degenerate tri
            edges[tuple(sorted((k1, k2)))] += 1
    return edges


@pytest.mark.parametrize("gw", [(1.0 * IN, 1.0 * IN), (0.0, 0.0)])
def test_stl_export_is_watertight(tmp_path, gw):
    offsets, L, T = _test_hull()
    canoe = build_complete_canoe(
        offsets, L, T, depth_m=T + 0.15,
        flare_deg=8.0, gw_width_m=gw[0], gw_height_m=gw[1], t_m=0.5 * IN)
    out = tmp_path / "canoe.stl"
    export_complete_stl(canoe, str(out))

    tris = _read_stl_triangles(out)
    assert len(tris) > 100
    edges = _edge_counts(tris)
    bad = {e: n for e, n in edges.items() if n != 2}
    assert not bad, (
        f"{len(bad)} non-manifold edges (of {len(edges)}) — mesh is not "
        f"watertight. Sample: {list(bad.items())[:5]}")


def test_stl_watertight_fine_entry_hull(tmp_path):
    """
    Regression: real Godzilla hulls have fine-entry sections whose half-breadth
    is zero for many waterlines (long centerline runs) and thinner than the
    shell near the keel.  Both once produced coincident mesh vertices and a
    non-manifold export.
    """
    n_st, n_wl, L, T = 21, 15, 5.0, 0.3
    z = np.linspace(0.0, T, n_wl)
    w = 0.4 * np.sin(np.pi * np.linspace(0.0, 1.0, n_st))
    prof = np.clip((z - T / 3.0) / (T - T / 3.0), 0.0, 1.0) ** 0.7
    offsets = np.outer(w, prof)          # zero width below T/3 everywhere

    canoe = build_complete_canoe(
        offsets, L, T, depth_m=T + 0.15, flare_deg=8.0,
        gw_width_m=1.5 * IN, gw_height_m=2.0 * IN, t_m=0.5 * IN)
    out = tmp_path / "fine_entry.stl"
    export_complete_stl(canoe, str(out))

    edges = _edge_counts(_read_stl_triangles(out))
    bad = {e: n for e, n in edges.items() if n != 2}
    assert not bad, f"{len(bad)} non-manifold edges on fine-entry hull"


@pytest.mark.parametrize("rocker", [0.0, None, 0.6])
def test_stl_watertight_rocker_hull(tmp_path, rocker):
    """Padded sub-keel rows + rocker must stay watertight in every stem
    mode (legacy vertical, auto sweep, long manual sweep that reaches into
    valid stations)."""
    offsets, x, z, zk, L, T = _rocker_hull()
    canoe = build_complete_canoe(
        offsets, L, T, depth_m=T + 0.15, flare_deg=8.0,
        gw_width_m=1.5 * IN, gw_height_m=2.0 * IN, t_m=0.5 * IN,
        stem_rocker_m=rocker)
    out = tmp_path / "rocker.stl"
    export_complete_stl(canoe, str(out))
    edges = _edge_counts(_read_stl_triangles(out))
    bad = {e: n for e, n in edges.items() if n != 2}
    assert not bad, f"{len(bad)} non-manifold edges on rocker hull"


# ═══════════════════════════════════════════════════════════════════
#  Keel fin regression (padded rows below the rocker line)
# ═══════════════════════════════════════════════════════════════════

class TestKeelFin:
    def test_no_fin_below_keel_line(self, tmp_path):
        """
        Regression: rows padded with y = 0 below the local rocker line used
        to be kept (nudged 0.2 mm off-center), producing a razor-thin fin
        from the baseline up to the true keel along the entire hull and past
        both tips.  No mesh vertex may sit more than one waterline spacing
        (the closure-row resolution) below the interpolated true keel line.
        """
        offsets, x, z, zk, L, T = _rocker_hull()
        dz = z[1] - z[0]
        canoe = build_complete_canoe(
            offsets, L, T, depth_m=T + 0.15, flare_deg=8.0,
            gw_width_m=1.5 * IN, gw_height_m=2.0 * IN, t_m=0.5 * IN,
            stem_rocker_m=0.0)
        out = tmp_path / "fin.stl"
        export_complete_stl(canoe, str(out))
        v = np.array([p for t in _read_stl_triangles(out) for p in t]) / MM
        zk_at = np.interp(v[:, 0], x, zk)
        fin = v[v[:, 2] < zk_at - dz - 1e-6]
        assert len(fin) == 0, (
            f"{len(fin)} vertices below the true keel line — the keel fin "
            f"is back (z down to {fin[:, 2].min():.4f})")

    def test_outer_bottom_follows_rocker(self):
        """Each station's outer chain must start at the local true keel
        (within one grid row), not at the z = 0 baseline; the inner keel
        sits exactly one shell thickness above it (solid-concrete nose)."""
        t = 0.5 * IN
        offsets, x, z, zk, L, T = _rocker_hull()
        dz = z[1] - z[0]
        canoe = build_complete_canoe(
            offsets, L, T, depth_m=T + 0.15, t_m=t, stem_rocker_m=0.0)
        checked = 0
        for i, (o, n) in enumerate(zip(canoe["outer"], canoe["inner"])):
            if o is None:
                continue
            z_bottom = o[:, 2].min()
            assert z_bottom >= zk[i] - dz - 1e-9, (
                f"station {i}: outer bottom z={z_bottom:.4f} is below the "
                f"true keel {zk[i]:.4f} by more than one grid row")
            assert z_bottom <= zk[i] + 1e-9
            # centerline seam: outer keel anchor on y=0, inner keel one t above
            assert o[np.argmin(o[:, 2])][1] == pytest.approx(0.0)
            assert n[:, 2].min() == pytest.approx(z_bottom + t, abs=1e-9)
            checked += 1
        assert checked > 10

    def test_tip_loops_do_not_protrude_below_keel(self):
        """The pinched tip slivers inherit the trimmed source section, so
        they must not reach anywhere near the z = 0 baseline (the old fin
        protruded past both tips at baseline height)."""
        offsets, x, z, zk, L, T = _rocker_hull()
        canoe = build_complete_canoe(
            offsets, L, T, depth_m=T + 0.15, t_m=0.5 * IN, stem_rocker_m=0.0)
        for tip in (canoe["loops"][0], canoe["loops"][-1]):
            assert tip[:, 2].min() > 0.5 * zk[0]


# ═══════════════════════════════════════════════════════════════════
#  Bow/stern stem rocker (pointed plan view, rounded side view)
# ═══════════════════════════════════════════════════════════════════

class TestStemRocker:
    def test_zero_sweep_keeps_legacy_vertical_stems(self):
        canoe, L, T = _build(rocker=0.0)
        assert len(canoe["loops"]) == 21
        assert canoe["stem_rocker_bow_m"] == 0.0
        assert canoe["stem_rocker_stern_m"] == 0.0
        # legacy tips span the full section depth (vertical stem)
        src = canoe["loops"][1]
        assert canoe["loops"][0][:, 2].min() == pytest.approx(
            src[:, 2].min(), abs=1e-9)

    def test_auto_mode_inserts_cap_sections(self):
        sharp, L, _ = _build(rocker=0.0)
        auto, _, _ = _build(rocker=None)
        assert len(auto["loops"]) > len(sharp["loops"])
        assert auto["stem_rocker_bow_m"] > 0.0
        assert auto["stem_rocker_stern_m"] > 0.0
        # overall length is never changed by the stem sweep
        xs = [lp[0, 0] for lp in auto["loops"] if lp is not None]
        assert min(xs) == pytest.approx(0.0, abs=1e-9)
        assert max(xs) == pytest.approx(L, abs=1e-9)
        # loft machinery requires one uniform point count, caps included
        counts = {len(lp) for lp in auto["loops"] if lp is not None}
        assert len(counts) == 1

    def test_manual_sweep_is_honoured(self):
        a = 0.5
        canoe, _, _ = _build(rocker=a)
        assert canoe["stem_rocker_bow_m"] == pytest.approx(a)
        assert canoe["stem_rocker_stern_m"] == pytest.approx(a)

    def test_plan_view_stays_pointed(self):
        """Top view: the stem sweep must never widen the hull — cap widths
        taper monotonically down to a sliver at the true tip."""
        canoe, L, _ = _build(rocker=None)
        loops = [lp for lp in canoe["loops"] if lp is not None]
        xs = np.array([float(lp[0, 0]) for lp in loops])
        ys = np.array([float(lp[:, 1].max()) for lp in loops])
        bow = xs < 0.3 * L
        order = np.argsort(xs[bow])
        y_bow = ys[bow][order]
        assert y_bow[0] <= 1e-3 + 1e-9, "tip is not a sliver in plan view"
        assert np.all(np.diff(y_bow) >= -1e-9), (
            "plan-view outline is not a monotone taper (nose was widened)")

    def test_side_view_stem_rises_to_sheer(self):
        """Side view: the section bottoms must sweep up toward the sheer so
        the end profile is rounded — the tip loop's lowest point sits near
        the sheer, not at the keel (the old full-depth vertical stem)."""
        fb = 0.15
        canoe, L, T = _build(rocker=None, fb=fb)
        depth = T + fb
        loops = [lp for lp in canoe["loops"] if lp is not None]
        xs = np.array([float(lp[0, 0]) for lp in loops])
        zb = np.array([float(lp[:, 2].min()) for lp in loops])
        # tip bottoms near the sheer (a short stem stub is allowed)
        assert zb[0] >= 0.9 * depth
        assert zb[-1] >= 0.9 * depth
        # bottoms rise monotonically across the bow cap sections
        bow = xs < 0.2 * L
        order = np.argsort(xs[bow])
        assert np.all(np.diff(zb[bow][order]) <= 1e-9), (
            "stem bottom does not rise monotonically toward the bow tip")
        # midship keel is untouched
        mid = np.argmin(np.abs(xs - L / 2))
        assert zb[mid] == pytest.approx(0.0, abs=1e-6)

    def test_gunwale_rail_reaches_the_tip(self):
        """The rail band (z > sheer) must survive the bottom remap so the
        gunwale line runs to the very end, as in the ETS reference."""
        fb = 0.15
        gw_h = 1.0 * IN
        canoe, L, T = _build(rocker=None, fb=fb, gw_h=gw_h)
        depth = T + fb
        tip = canoe["loops"][0]
        assert tip[:, 2].max() == pytest.approx(depth + gw_h, abs=1e-9)

    def test_suggest_stem_rocker_derivation(self):
        from geometry import suggest_stem_rocker_m
        # natural sweep = the degenerate wedge span
        assert suggest_stem_rocker_m(0.30, 5.0) == pytest.approx(0.30)
        # floor: 3 % L keeps the rocker visible on full-ended hulls
        assert suggest_stem_rocker_m(0.05, 5.0) == pytest.approx(0.15)
        # cap: 12 % L so a coarse grid cannot hollow out the forebody
        assert suggest_stem_rocker_m(1.0, 5.0) == pytest.approx(0.60)


# ═══════════════════════════════════════════════════════════════════
#  Uniform shell thickness at bow/stern (fix plan C1)
# ═══════════════════════════════════════════════════════════════════

def _point_chain_dist(p, chain):
    """Min distance from point p (2,) to polyline chain (m, 2)."""
    a, b = chain[:-1], chain[1:]
    ab, ap = b - a, p - a
    tt = np.clip((ap * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-18),
                 0.0, 1.0)
    proj = a + tt[:, None] * ab
    return float(np.sqrt(((p - proj) ** 2).sum(1)).min())


class TestTipWallThickness:
    """
    Regression: tip/cap sections used to be built by scaling the CLOSED
    loop, which scaled the wall thickness down with the plan taper — the
    bow/stern walls came out paper-thin.  Every section must now either
    keep a full-thickness wall (inner chain ≥ ~t from the outer skin) or
    be properly SOLID (interior collapsed to the centerline nudge, one
    shell thickness above the outer skin).
    """
    N_O = 15 + 8          # n_wl + n_freeboard(default 8) → outer chain rows
    EPS_Y = 2e-4

    def _chains(self, loop, has_rail):
        r = 3 if has_rail else 0
        outer = loop[:self.N_O, 1:3]
        inner = loop[self.N_O + r: self.N_O + r + self.N_O, 1:3][::-1]
        return outer, inner

    @pytest.mark.parametrize("rocker", [None, 0.0])
    def test_full_thickness_or_solid_everywhere(self, rocker):
        t = 0.5 * IN
        offsets, x, z, zk, L, T = _rocker_hull()
        canoe = build_complete_canoe(
            offsets, L, T, depth_m=T + 0.15, flare_deg=8.0,
            gw_width_m=1.5 * IN, gw_height_m=2.0 * IN, t_m=t,
            stem_rocker_m=rocker)

        grid = {round(float(v), 9) for v in np.linspace(0, L, offsets.shape[0])}
        n_caps = 0
        for lp in canoe["loops"]:
            if lp is None:
                continue
            is_cap = round(float(lp[0, 0]), 9) not in grid
            n_caps += is_cap
            outer, inner = self._chains(lp, canoe["has_rail"])
            solid_any = False
            for k in range(1, self.N_O - 3):     # skip keel anchor + sheer clip
                solid = inner[k, 0] <= 2 * self.EPS_Y + 1e-9
                solid_any |= solid
                if solid:
                    # interior sits one shell thickness above the outer skin
                    assert inner[k, 1] > outer[k, 1], \
                        f"x={lp[0,0]:.3f}: interior below outer skin"
                else:
                    d = _point_chain_dist(inner[k], outer)
                    assert d >= 0.7 * t, (
                        f"x={lp[0,0]:.3f} row {k}: wall {d*1000:.2f} mm "
                        f"< 0.7·t ({0.7*t*1000:.2f} mm) — thin tip wall is back")
            if is_cap:
                assert solid_any or float(np.abs(lp[:, 1]).max()) > 1.5 * t, \
                    "narrow cap section is not solid"
        if rocker is None:
            assert n_caps >= 8, "expected inserted stem-rocker cap sections"

    def test_tip_sliver_is_solid(self):
        """The 1 mm tip slivers must be solid concrete: interior collapsed
        to the centerline nudge, one t above the outer skin (the old scaled
        copies kept a scaled-down hollow interior)."""
        t = 0.5 * IN
        offsets, x, z, zk, L, T = _rocker_hull()
        canoe = build_complete_canoe(
            offsets, L, T, depth_m=T + 0.15, flare_deg=8.0,
            gw_width_m=1.5 * IN, gw_height_m=2.0 * IN, t_m=t,
            stem_rocker_m=0.0)
        for lp in (canoe["loops"][0], canoe["loops"][-1]):
            outer, inner = self._chains(lp, canoe["has_rail"])
            core = slice(1, self.N_O - 3)
            assert float(inner[core, 0].max()) <= 2 * self.EPS_Y + 1e-9
            dz = inner[core, 1] - outer[core, 1]
            assert np.all(dz > 0)
            assert float(np.median(dz)) >= 0.8 * t


def test_triangulate_polygon_square_with_collinear_points():
    # Square boundary with a midpoint on each side (collinear runs)
    poly = np.array([
        [0, 0], [0.5, 0], [1, 0], [1, 0.5], [1, 1],
        [0.5, 1], [0, 1], [0, 0.5],
    ], dtype=float)
    tris = _triangulate_polygon(poly)
    area = 0.0
    for i0, i1, i2 in tris:
        a, b, c = poly[i0], poly[i1], poly[i2]
        area += abs((b[0] - a[0]) * (c[1] - a[1])
                    - (b[1] - a[1]) * (c[0] - a[0])) / 2.0
    assert area == pytest.approx(1.0, rel=1e-9)


# ═══════════════════════════════════════════════════════════════════
#  CanoeDesignState
# ═══════════════════════════════════════════════════════════════════

class TestDesignState:
    def test_step_status_progression(self):
        ds = CanoeDesignState()
        assert ds.step_status("seed") == "pending"
        ds.mark_seed([0.5] * 7, 5.0, 0.7, 0.3)
        assert ds.step_status("seed") == "done"
        assert ds.step_status("godzilla") == "pending"

    def test_staleness_when_upstream_reruns(self):
        ds = CanoeDesignState(
            seed={"timestamp": "2026-07-07T10:00:00.000000"},
            godzilla={"timestamp": "2026-07-07T10:05:00.000000"},
            structural={"timestamp": "2026-07-07T10:10:00.000000"},
        )
        assert not ds.is_stale("structural")
        ds.seed["timestamp"] = "2026-07-07T11:00:00.000000"      # seed re-run
        assert ds.is_stale("godzilla")
        assert ds.is_stale("structural")
        assert ds.step_status("structural") == "stale"

    def test_mark_structural_prefers_tab_flare(self):
        ds = CanoeDesignState()
        ds.mark_structural({
            "t_in": 0.5, "depth_in": 12.0,
            "flare_angle_deg": 7.0, "flare_req_deg": 3.0,
            "gw_width_in": 1.5, "gw_height_in": 2.0,
            "best_mixes": [80.0, 60.0], "weight_lbs": 185.0,
        })
        assert ds.structural["flare_deg"] == 7.0
        assert ds.structural["mixes_pcf"] == [80.0, 60.0]

    def test_json_roundtrip(self, tmp_path):
        from data.session import save_design_state, load_design_state
        ds = CanoeDesignState()
        ds.mark_seed([1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05], 5.5, 0.75, 0.32)
        ds.mark_export("complete_canoe_stl", "C:/tmp/canoe.stl")
        p = tmp_path / "design_state.json"
        save_design_state(ds, path=str(p))
        loaded = load_design_state(path=str(p))
        assert loaded.to_dict() == ds.to_dict()
        # Corrupt file → fresh empty state, never raises
        p.write_text("{not json", encoding="utf-8")
        assert load_design_state(path=str(p)).seed is None
