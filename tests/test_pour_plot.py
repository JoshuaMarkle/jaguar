# tests/test_pour_plot.py
"""
Pour-zones chart must render the real multi-zone (I-beam) result: N density
bands and N-1 hand-off lines, driven by zone_pour_lines / zone_densities.

Guards the bug where a 3-zone [65, 57.5, 65] winner rendered as a single
2-mix "hand-off height" band because the caller (the UI tab) dropped the
multi-zone arrays.
"""
from __future__ import annotations

import numpy as np

from visualization import plot_pour_zones


def _three_zone_inputs(n=21):
    """A 3-zone I-beam section: keel→3.999→10.0→sheer(13.576)."""
    x = np.linspace(0.0, 20.0, n)
    sheer = np.full(n, 13.576)
    z1 = np.full(n, 3.999)      # bottom flange top
    z2 = np.full(n, 10.0)       # web top
    na = np.full(n, 4.423)
    layout = np.zeros((n, 2), dtype=int)   # unused on the multi-zone path
    return dict(x=x, layout=layout, na_line=na, sheer_line=sheer,
                zone_pour_lines=[z1, z2], zone_densities=[65.0, 57.5, 65.0])


def _white(line):
    from matplotlib.colors import to_rgba
    return np.allclose(to_rgba(line.get_color()), to_rgba("white"))


def test_three_zones_render_three_bands_two_handoffs():
    d = _three_zone_inputs()
    # show_outline=False isolates the hand-off lines from the (also-white) hull
    # outline; the outline itself is covered by the mould/outline tests below.
    fig = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"],
        show_outline=False)
    ax = fig.axes[0]

    # 3 density bands
    assert len(ax.containers) == 3, f"expected 3 bands, got {len(ax.containers)}"

    # N-1 = 2 hand-off lines (white), plus the neutral-axis overlay
    white_lines = [ln for ln in ax.get_lines() if _white(ln)]
    assert len(white_lines) == 2, (
        f"expected 2 hand-off lines, got {len(white_lines)}")

    # multi-zone title + per-zone density labels
    assert "Multi-Zone" in ax.get_title()
    labels = [t.get_text() for t in ax.get_legend().get_texts()]
    assert any("65 pcf" in l for l in labels)
    assert any("58 pcf" in l or "57" in l for l in labels)
    assert any("neutral axis" in l.lower() for l in labels)
    fig.clf()


def test_band_boundaries_are_the_pour_lines():
    """Each band spans exactly [boundary_k, boundary_{k+1}]."""
    d = _three_zone_inputs()
    fig = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"])
    ax = fig.axes[0]
    # bar containers are stacked keel→sheer; bottoms/heights encode the bounds
    bottoms = [float(bar.get_y()) for bar in ax.containers[0]]
    assert np.allclose(bottoms, 0.0)                       # zone 1 starts at keel
    tops1 = [float(bar.get_y() + bar.get_height()) for bar in ax.containers[0]]
    assert np.allclose(tops1, 3.999)                       # ends at hand-off 1
    tops3 = [float(bar.get_y() + bar.get_height()) for bar in ax.containers[2]]
    assert np.allclose(tops3, 13.576)                      # zone 3 ends at sheer
    fig.clf()


def test_two_zone_result_uses_legacy_handoff_path():
    """No zone_pour_lines -> the single hand-off (2-mix) rendering, unchanged."""
    n = 21
    x = np.linspace(0.0, 20.0, n)
    sheer = np.full(n, 13.576)
    layout = np.zeros((n, 2), dtype=int)
    layout[:, 1] = 1
    fig = plot_pour_zones(x, layout, na_line=np.full(n, 4.4),
                          pour_line=np.full(n, 4.0), sheer_line=sheer,
                          mixes_pcf=[65.0, 60.5])
    ax = fig.axes[0]
    assert "hand-off height" in ax.get_title()   # legacy 2-mix title
    fig.clf()


# ── hull outline + mould orientation ─────────────────────────────────────────

def test_outline_on_by_default_and_labeled():
    d = _three_zone_inputs()
    fig = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"])
    labels = [t.get_text().lower() for t in fig.axes[0].get_legend().get_texts()]
    assert any("outline" in l for l in labels), "hull outline missing from legend"
    fig.clf()


def test_outline_can_be_disabled():
    d = _three_zone_inputs()
    fig = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"],
        show_outline=False)
    labels = [t.get_text().lower() for t in fig.axes[0].get_legend().get_texts()]
    assert not any("outline" in l for l in labels)
    fig.clf()


def test_female_mould_is_keel_down_male_is_inverted():
    d = _three_zone_inputs()
    fem = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"],
        mold="female")
    male = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"],
        mold="male")
    # Female: keel at the bottom (axis not inverted). Male: flipped (inverted).
    assert not fem.axes[0].yaxis_inverted()
    assert male.axes[0].yaxis_inverted()
    assert "female" in fem.axes[0].get_title().lower()
    assert "male" in male.axes[0].get_title().lower()
    fem.clf(); male.clf()


def test_mould_flip_preserves_the_zone_bands():
    """Flipping orientation is a view change only — the bands are identical."""
    d = _three_zone_inputs()
    fem = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"],
        mold="female")
    male = plot_pour_zones(
        d["x"], d["layout"], na_line=d["na_line"], sheer_line=d["sheer_line"],
        zone_pour_lines=d["zone_pour_lines"], zone_densities=d["zone_densities"],
        mold="male")
    for cf, cm in zip(fem.axes[0].containers, male.axes[0].containers):
        bf = [(b.get_y(), b.get_height()) for b in cf]
        bm = [(b.get_y(), b.get_height()) for b in cm]
        assert np.allclose(bf, bm), "mould flip changed the underlying bands"
    fem.clf(); male.clf()


def test_real_height_path_also_supports_mould_flip():
    n = 21
    x = np.linspace(0.0, 20.0, n)
    sheer = np.full(n, 13.576)
    layout = np.zeros((n, 2), dtype=int); layout[:, 1] = 1
    male = plot_pour_zones(x, layout, na_line=np.full(n, 4.4),
                           pour_line=np.full(n, 4.0), sheer_line=sheer,
                           mixes_pcf=[65.0, 60.5], mold="male")
    assert male.axes[0].yaxis_inverted()
    male.clf()
