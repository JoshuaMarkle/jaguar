# tests/test_stem_overhang.py
"""
Stem overhang (tip cantilever extent) must describe the HULL, not the file.

The overhang is un-buoyed cantilever mass beyond the supported span.  It raises
tip hogging, which OPPOSES the crew's midship sagging -- so on this hull a
longer overhang LOWERS the governing D/C.  That makes it a parameter that buys
structural margin, which is exactly why it has to be a truthful measurement and
not a tunable.

The rule it replaced scanned for the first station with non-zero underwater
area and clipped to 3-12 % of L.  That measured the offset file's ROW SPACING
(12.0 in at 21 stations, 6.0 at 41, 1.5 at 161) and was then overridden by a
3 %-of-L floor chosen so the stem would be visible in CAD.  On the live hull it
returned 7.20 in/end against a true ~1.05 in -- an answer that moved with the
analysis grid, the same class of defect as the tip-divergence bug.
"""
from __future__ import annotations

import numpy as np
import pytest

from geometry import stem_overhang_ft


def _wedge_hull(n_st, L_ft=20.0, half_beam_in=13.0, n_wl=17, depth_in=13.0):
    """Hull tapering linearly to a point at both ends, sampled at n_st."""
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    b = half_beam_in * np.sin(np.pi * x / L_ft)      # 0 at both tips
    return x, np.outer(b, np.sqrt(z / depth_in))


class TestResolutionIndependence:
    @pytest.mark.parametrize("t_in", [0.375, 0.5, 0.75])
    def test_same_answer_across_station_counts(self, t_in):
        """The whole point: refining the grid must not move the answer."""
        vals = [stem_overhang_ft(*_wedge_hull(n), t_in)
                for n in (41, 81, 161, 321, 641)]
        spread = max(vals) - min(vals)
        assert spread < 1e-3 * max(vals), (
            f"overhang moves with resolution: {[round(v*12, 4) for v in vals]} in")

    def test_snapping_to_a_station_is_what_moved_with_the_grid(self):
        """Characterise the defect being fixed, so the implementation cannot
        regress to a station-snapped measure and still pass.

        The comparison is snapped-vs-interpolated on the SAME hulls: snapping
        quantises the answer to the station spacing, interpolation does not.
        (On the live hull the snapped spread is far larger still -- 12.0 / 6.0 /
        1.5 in -- because its taper is gentler than this fixture's.)
        """
        snapped, interp = [], []
        for n in (41, 81, 161, 321):
            x, y = _wedge_hull(n)
            b = np.max(y, axis=1)
            snapped.append(float(x[int(np.argmax(b >= 1.5 * 0.375))] - x[0]))
            interp.append(stem_overhang_ft(x, y, 0.375))
        snap_spread = max(snapped) - min(snapped)
        interp_spread = max(interp) - min(interp)
        assert snap_spread > 0.05, \
            "fixture no longer exercises the resolution artifact"
        assert interp_spread < 0.01 * snap_spread, (
            f"interpolated spread {interp_spread:.2e} ft is not decisively "
            f"smaller than the snapped spread {snap_spread:.2e} ft")

    def test_scales_with_thickness_not_with_the_grid(self):
        """A thicker shell needs more breadth before a section exists, so the
        overhang grows with t -- a real dependence, unlike the grid one."""
        a = stem_overhang_ft(*_wedge_hull(161), 0.375)
        b = stem_overhang_ft(*_wedge_hull(161), 0.750)
        assert b > a * 1.5


class TestDefinition:
    def test_measures_where_half_breadth_crosses_the_threshold(self):
        """Linear taper: b(x) = slope*x, so the crossing is analytic."""
        n, L, hb = 161, 20.0, 13.0
        x, y = _wedge_hull(n, L_ft=L, half_beam_in=hb)
        t = 0.375
        got = stem_overhang_ft(x, y, t)
        # near the tip b ~ hb*sin(pi x/L) ~ hb*pi*x/L
        want = (1.5 * t) * L / (hb * np.pi)
        assert got == pytest.approx(want, rel=0.02)

    def test_zero_when_the_hull_is_never_slender(self):
        x = np.linspace(0.0, 20.0, 41)
        y = np.full((41, 5), 10.0)          # wide everywhere, no taper
        assert stem_overhang_ft(x, y, 0.375) == 0.0

    def test_accepts_a_per_station_breadth_vector(self):
        x, y = _wedge_hull(161)
        assert stem_overhang_ft(x, y, 0.375) == pytest.approx(
            stem_overhang_ft(x, np.max(y, axis=1), 0.375))

    def test_symmetric_hull_gives_equal_ends(self):
        """The returned value is the bow/stern mean; on a symmetric hull that
        must equal either end."""
        x, y = _wedge_hull(161)
        b = np.max(y, axis=1)
        thr = 1.5 * 0.375
        i = int(np.argmax(b >= thr))
        f = (thr - b[i - 1]) / (b[i] - b[i - 1])
        bow = x[i - 1] + f * (x[i] - x[i - 1]) - x[0]
        assert stem_overhang_ft(x, y, 0.375) == pytest.approx(bow, rel=1e-9)


class TestLiveHull:
    """The real off1.mlt, which is what the reported 7.20 in came from."""

    def _hull(self, n_st):
        from geometry import (parse_godzilla_offsets,
                              parse_godzilla_dimensions)
        L_ft, draft_in = parse_godzilla_dimensions("out.mlt")
        x, z, y = parse_godzilla_offsets("off1.mlt", L_ft=L_ft,
                                         draft_in=draft_in,
                                         mold_depth_in=12.784, n_stations=n_st)
        return x, y

    def test_live_hull_is_resolution_independent(self, repo_safe_cwd=None):
        vals = [stem_overhang_ft(*self._hull(n), 0.375) * 12.0
                for n in (41, 81, 161, 321)]
        assert max(vals) - min(vals) < 1e-3, \
            f"live hull overhang varies with resolution: {vals}"

    def test_live_hull_is_nowhere_near_the_old_constant(self):
        """The old rule returned 0.03*LOA = 7.20 in/end.  The geometry says
        ~1.05.  If this ever drifts back toward 7 in, the CAD-visibility floor
        has crept back into a structural input."""
        got = stem_overhang_ft(*self._hull(161), 0.375) * 12.0
        assert 0.5 < got < 2.5, f"expected ~1.05 in/end, got {got:.3f}"
        assert got < 0.5 * 7.2036
