# tests/test_stem_overhang_per_task.py
"""
A HULL'S WEIGHT MUST BE A PROPERTY OF THE HULL.

`main.py` used to resolve the stem overhang ONCE per sweep, at
`min(thickness_options_in)`, and apply that one value to every task.  The
overhang threshold is `1.5 * t`, so the overhang is a function of thickness
(1.045 in at t=0.375, 1.394 in at t=0.500) -- which meant a thicker design was
evaluated with a thinner design's tip geometry whenever both were in the grid.

Measured before the fix: the SAME (t=0.500, depth=12.570) design scored

    161.4949  in a grid of [0.5]
    160.4491  in a grid of [0.375, 0.5, 0.625]

~1 lb apart, with seat positions 2.08 ft apart.  Neither number was wrong for
its own assumption; what was wrong is that the assumption came from the grid
rather than from the design, so a weight could not be compared across sweeps.

The sweep-wide rule WAS deliberate -- `geometry.stem_overhang_ft` argues for it
explicitly, because the shortest overhang is the least tip mass and tip mass
relieves the governing midship sagging.  That conservatism existed only because
one value had to serve every thickness.  Per task, each design gets its own
true overhang and the compensation is unnecessary.

These tests pin the property, not the implementation.
"""
from __future__ import annotations

import pytest

import config
import main
from geometry import stem_overhang_ft


@pytest.fixture
def hull(tmp_path):
    """The real offset geometry, parsed once."""
    from geometry import parse_godzilla_offsets
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    off = os.path.join(repo, "off1.mlt")
    if not os.path.exists(off):
        pytest.skip("off1.mlt not present")
    x_st, _z, y_uw = parse_godzilla_offsets(
        off, L_ft=20.00984316, draft_in=4.5760260931,
        mold_depth_in=4.5760260931)
    if x_st is None:
        pytest.skip("offsets could not be parsed")
    return x_st, y_uw


def test_the_overhang_actually_depends_on_thickness(hull):
    """
    The premise.  If this ever stops being true the whole concern evaporates,
    and a test that silently tests nothing is worse than no test.
    """
    x_st, y_uw = hull
    a = stem_overhang_ft(x_st, y_uw, 0.375)
    b = stem_overhang_ft(x_st, y_uw, 0.500)
    assert b > a > 0.0, (
        f"overhang is supposed to grow with t (1.5*t threshold): "
        f"t=0.375 -> {a * 12:.3f} in, t=0.500 -> {b * 12:.3f} in")


def test_one_thickness_resolves_the_same_in_any_grid(hull):
    """
    THE REGRESSION.  t=0.5's overhang must not change because 0.375 joined the
    grid.  This is the exact comparison that produced 161.4949 vs 160.4491.
    """
    x_st, y_uw = hull
    alone = main.publish_stem_overhang_map([0.5], x_st, y_uw)
    with_others = main.publish_stem_overhang_map(
        [0.375, 0.5, 0.625], x_st, y_uw)
    assert alone[0.5] == pytest.approx(with_others[0.5], rel=1e-12), (
        f"t=0.5 resolved to {alone[0.5]:.6f} in/end alone but "
        f"{with_others[0.5]:.6f} in/end alongside other thicknesses -- the "
        f"grid-coupling this fix removed has come back.")


def test_each_thickness_gets_its_own_value(hull):
    """Not merely stable -- CORRECT: every entry is that thickness's own."""
    x_st, y_uw = hull
    ts = [0.375, 0.5, 0.625]
    m = main.publish_stem_overhang_map(ts, x_st, y_uw)
    for t in ts:
        assert m[round(t, 6)] == pytest.approx(
            stem_overhang_ft(x_st, y_uw, t) * 12.0, rel=1e-12)
    assert len(set(round(v, 6) for v in m.values())) == len(ts), (
        "three thicknesses collapsed to fewer distinct overhangs; the map is "
        "not resolving per thickness")


def test_an_explicit_user_overhang_applies_to_every_thickness(hull):
    """
    A stated assumption is thickness-independent ON PURPOSE.  The user said
    "the overhang is X"; the code must not quietly derive something else.
    """
    x_st, y_uw = hull
    m = main.publish_stem_overhang_map(
        [0.375, 0.5, 0.625], x_st, y_uw, explicit_in=2.5)
    assert set(m.values()) == {2.5}


class TestResolverContract:
    """`_struct_rocker_ft` must never invent an overhang."""

    def _clear(self, monkeypatch):
        for name in ("STEM_OVERHANG_BY_T", "STEM_OVERHANG_IN",
                     "STRUCT_ROCKER_IN"):
            monkeypatch.delattr(config, name, raising=False)

    def test_it_raises_when_nothing_is_published(self, monkeypatch):
        self._clear(monkeypatch)
        with pytest.raises(RuntimeError, match="not published"):
            main._struct_rocker_ft(0.5)

    def test_a_published_map_missing_this_thickness_RAISES(self, monkeypatch):
        """
        The important one.  Falling back to another thickness's overhang is
        precisely the defect being removed, so a miss must be loud.  A silent
        fallback here would reinstate grid-coupling while every test above
        still passed.
        """
        self._clear(monkeypatch)
        monkeypatch.setattr(config, "STEM_OVERHANG_BY_T",
                            {0.375: 1.045}, raising=False)
        with pytest.raises(RuntimeError, match="no entry for t="):
            main._struct_rocker_ft(0.625)

    def test_the_map_is_preferred_over_the_scalar(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setattr(config, "STEM_OVERHANG_IN", 99.0, raising=False)
        monkeypatch.setattr(config, "STEM_OVERHANG_BY_T",
                            {0.5: 1.394}, raising=False)
        assert main._struct_rocker_ft(0.5) == pytest.approx(1.394 / 12.0)

    def test_the_scalar_still_serves_direct_callers(self, monkeypatch):
        """
        No map, no thickness: the explicit-input and unit-test path, unchanged.
        Several existing tests depend on this exact behaviour.
        """
        self._clear(monkeypatch)
        monkeypatch.setattr(config, "STEM_OVERHANG_IN", 1.045163,
                            raising=False)
        assert main._struct_rocker_ft() == pytest.approx(1.045163 / 12.0)


def test_the_map_is_run_level_config():
    """
    It must travel to spawned workers and take part in the fingerprint.  A
    worker that found no map would fall back to the scalar and silently
    reinstate the sweep-wide overhang -- passing every test above.
    """
    from config.run_state import RUN_LEVEL_CONFIG_ATTRS
    assert "STEM_OVERHANG_BY_T" in RUN_LEVEL_CONFIG_ATTRS
