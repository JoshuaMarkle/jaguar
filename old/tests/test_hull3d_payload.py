# tests/test_hull3d_payload.py
"""
3-D preview payload tests (fix plan Phase 2): the Tab-3 hull preview must
show the SAME geometry treatment as the CAD exporters — no keel fin from
padded sub-rocker rows, keel line following the rocker, and sharp
centerline tips instead of blunt boxes.
"""
from __future__ import annotations

import numpy as np

from ui.hull3d_view import _hull_to_json


def _rocker_hull(n_st=41, n_wl=15, L=6.0, half_beam=0.4, T=0.35, rocker=0.12):
    x = np.linspace(0.0, L, n_st)
    z = np.linspace(0.0, T, n_wl)
    zk = rocker * (2.0 * x / L - 1.0) ** 2
    w = half_beam * np.sin(np.pi * x / L) ** 0.9
    offsets = np.zeros((n_st, n_wl))
    for i in range(n_st):
        for k in range(n_wl):
            if z[k] >= zk[i] - 1e-12:
                offsets[i, k] = w[i] * np.sqrt(
                    max((z[k] - zk[i]) / max(T - zk[i], 1e-9), 0.0))
    return offsets, x, z, zk, L, T


def test_payload_shape_is_consistent():
    offsets, x, z, zk, L, T = _rocker_hull()
    p = _hull_to_json(offsets, L, T)
    assert p["ns"] >= 2 and p["nw_ring"] > 0
    assert len(p["grid"]) == p["ns"] * p["nw_ring"] * 3
    assert len(p["colors"]) == len(p["grid"])
    assert len(p["waterline"]) > 2
    assert p["meta"]["L"] == L and p["meta"]["T"] == T


def test_tips_are_sharp_centerline_slivers():
    """The extreme stations must collapse onto the centerline (breadth 0),
    matching sanitize_hull_data's exporter treatment — not blunt boxes."""
    offsets, x, z, zk, L, T = _rocker_hull()
    p = _hull_to_json(offsets, L, T)
    grid = np.asarray(p["grid"]).reshape(-1, 3)
    ns = p["ns"]
    for i in (0, ns - 1):                       # vi = p_ring * ns + i
        tip = grid[i::ns]
        assert np.abs(tip[:, 2]).max() < 1e-9, "tip station is not sharp"
        # tip spans real height (a stem line, not a collapsed point)
        assert tip[:, 1].max() - tip[:, 1].min() > 0.05 * T


def test_keel_line_follows_the_rocker():
    """No preview vertex of the keel line may sit more than one grid row
    below the true local keel (the old payload pinned it to z = 0)."""
    offsets, x, z, zk, L, T = _rocker_hull()
    dz = z[1] - z[0]
    p = _hull_to_json(offsets, L, T)
    keel = np.asarray(p["keel"])                # [x, height, 0]
    assert len(keel) >= 2
    zk_at = np.interp(keel[:, 0], x, zk)
    assert np.all(keel[:, 1] >= zk_at - dz - 1e-9), "keel fin in the preview"
    # and the keel genuinely rises toward the ends
    assert keel[0, 1] > keel[len(keel) // 2, 1] + 0.5 * 0.12
