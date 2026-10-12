# tests/test_search_enforcement.py
"""
The sweep must enforce refinement-robustness itself.

THE FAILURE THIS PREVENTS.  The acceptance limit is a THRESHOLD, not a target,
so the optimiser sits just underneath it and banks the remaining weight.  At the
search resolution that yields a design which passes only because the grid is too
coarse to see its real utilisation.  It happened three times on the live hull --
winners at 0.9487, 0.9466 and 0.9380 that each exceeded 0.95 once refined -- and
each was caught by hand after the sweep had already finished.

Two mechanisms now prevent it, and both are tested here:
  * the SEARCH gates at acceptance/(1+drift), so the winner clears acceptance at
    the verification resolution by construction;
  * the winner is re-evaluated at 2x resolution and rejected if it does not.

Plus loaded freeboard and hull KG, the two constraints that were previously
computed once from an estimate and never reconciled with the winning design.
"""
from __future__ import annotations

import numpy as np
import pytest

import config
import structural
from geometry import calculate_shell_properties
from structural import (hull_vertical_cg_in, kupfer_acceptance_limit,
                        kupfer_drift_per_doubling, kupfer_search_limit,
                        loaded_freeboard_check, max_hull_weight_for_freeboard,
                        transverse_gmt_design_in)


def _hull(n_st=41, n_wl=17, L_ft=20.0, depth_in=13.0, half_beam_in=13.0,
          t_in=0.5):
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    b = half_beam_in * np.sin(np.pi * x / L_ft)
    y = np.outer(b, np.sqrt(z / depth_in))
    return x, z, y, calculate_shell_properties(x, z, y, t_in)


# ══════════════════════════════════════════════════════════════════════════
#  Drift-tightened search limit
# ══════════════════════════════════════════════════════════════════════════

class TestSearchLimit:
    def test_search_limit_is_below_acceptance(self):
        assert kupfer_search_limit() < kupfer_acceptance_limit()

    def test_derived_from_the_measured_drift(self):
        d = kupfer_drift_per_doubling()
        assert kupfer_search_limit() == pytest.approx(
            kupfer_acceptance_limit() / (1.0 + d))

    def test_drift_is_file_backed_not_hardcoded(self):
        from config.settings import get_settings
        assert config.KUPFER_DRIFT_PER_DOUBLING == pytest.approx(
            get_settings().structural.kupfer_drift_per_doubling)

    @pytest.mark.parametrize("util", [0.9487, 0.9466, 0.9380])
    def test_rejects_the_designs_that_needed_hand_catching(self, util, monkeypatch):
        """The three real winners that passed the search and then failed
        refinement.  Every one must now be rejected DURING the search."""
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95)
        monkeypatch.setattr(config, "KUPFER_DRIFT_PER_DOUBLING", 0.0219)
        assert util > kupfer_search_limit()
        assert util <= kupfer_acceptance_limit()   # they DID pass the old gate

    def test_more_doublings_tighten_further(self):
        assert kupfer_search_limit(2) < kupfer_search_limit(1)

    def test_zero_drift_degenerates_to_the_acceptance_limit(self, monkeypatch):
        monkeypatch.setattr(config, "KUPFER_DRIFT_PER_DOUBLING", 0.0)
        assert kupfer_search_limit() == pytest.approx(kupfer_acceptance_limit())

    def test_negative_drift_is_clamped(self, monkeypatch):
        """A nonsense config value must not LOOSEN the search."""
        monkeypatch.setattr(config, "KUPFER_DRIFT_PER_DOUBLING", -0.5)
        assert kupfer_search_limit() <= kupfer_acceptance_limit()


# ══════════════════════════════════════════════════════════════════════════
#  Loaded freeboard, per configuration
# ══════════════════════════════════════════════════════════════════════════

class TestLoadedFreeboardGate:
    """The inverted gate must agree with the root-finding check exactly --
    it is the same criterion, solved for weight instead of for draft."""

    @pytest.mark.parametrize("depth", [12.0, 13.0, 14.0])
    def test_agrees_with_the_root_finder(self, depth):
        x, z, y, _ = _hull(depth_in=depth)
        crew, minfb = 640.0, 6.0
        wmax = max_hull_weight_for_freeboard(depth, minfb, crew, x, z, y)
        for hull in (50.0, 100.0, wmax - 0.05, wmax + 0.05, 300.0):
            chk = loaded_freeboard_check(hull + crew, depth, x, z, y, minfb)
            assert (hull <= wmax) == chk["ok"], (
                f"depth {depth}, hull {hull}: gate says {hull <= wmax}, "
                f"root-finder says {chk['ok']} (fb {chk['freeboard_in']:.4f})")

    def test_heavier_hull_means_less_freeboard(self):
        x, z, y, _ = _hull()
        a = loaded_freeboard_check(700.0, 13.0, x, z, y, 6.0)["freeboard_in"]
        b = loaded_freeboard_check(800.0, 13.0, x, z, y, 6.0)["freeboard_in"]
        assert b < a

    def test_deeper_mold_admits_a_heavier_hull(self):
        x1, z1, y1, _ = _hull(depth_in=12.0)
        x2, z2, y2, _ = _hull(depth_in=13.0)
        assert (max_hull_weight_for_freeboard(13.0, 6.0, 640.0, x2, z2, y2)
                > max_hull_weight_for_freeboard(12.0, 6.0, 640.0, x1, z1, y1))

    def test_unreachable_requirement_returns_negative_infinity(self):
        x, z, y, _ = _hull()
        assert max_hull_weight_for_freeboard(5.0, 6.0, 640.0, x, z, y) == float("-inf")


# ══════════════════════════════════════════════════════════════════════════
#  KG from the actual mix distribution
# ══════════════════════════════════════════════════════════════════════════

class TestHullKG:
    def _z(self, p, v, lo, hi):
        sheer = p.c_bot[v] + p.c_top[v]
        return [np.zeros(v.sum()), lo * sheer, hi * sheer, sheer]

    def test_uniform_density_gives_the_area_centroid(self):
        """With one density the mass centroid IS the section centroid, so KG
        must not depend on the density value at all."""
        x, z, y, p = _hull()
        v = p.A_in2 > 0.001
        zb = self._z(p, v, 0.34, 0.67)
        a = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v], p.z_grid_in,
                                zb, [65.0, 65.0, 65.0], x[v])
        b = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v], p.z_grid_in,
                                zb, [80.0, 80.0, 80.0], x[v])
        assert a == pytest.approx(b, rel=1e-12)

    def test_heavy_top_flange_raises_kg(self):
        """The whole point: an I-beam with dense flanges sits higher than a
        uniform hull, and the form-only estimate cannot see that."""
        x, z, y, p = _hull()
        v = p.A_in2 > 0.001
        zb = self._z(p, v, 0.25, 0.75)
        uniform = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v],
                                      p.z_grid_in, zb, [70, 70, 70], x[v])
        top_heavy = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v],
                                        p.z_grid_in, zb, [60, 60, 90], x[v])
        assert top_heavy > uniform

    def test_heavy_bottom_lowers_kg(self):
        x, z, y, p = _hull()
        v = p.A_in2 > 0.001
        zb = self._z(p, v, 0.25, 0.75)
        uniform = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v],
                                      p.z_grid_in, zb, [70, 70, 70], x[v])
        bottom_heavy = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v],
                                           p.z_grid_in, zb, [90, 60, 60], x[v])
        assert bottom_heavy < uniform

    def test_kg_lies_inside_the_section(self):
        x, z, y, p = _hull()
        v = p.A_in2 > 0.001
        sheer = float(np.max(p.c_bot[v] + p.c_top[v]))
        kg = hull_vertical_cg_in(p.cum_area_in2[v], p.cum_Az_in3[v], p.z_grid_in,
                                 self._z(p, v, 0.34, 0.67), [72, 62, 72], x[v])
        assert 0.0 < kg < sheer


class TestDesignGMT:
    def test_raising_kg_lowers_gmt_one_for_one(self):
        a = transverse_gmt_design_in(3.0, 12.0, 5.0, 120.0)
        b = transverse_gmt_design_in(3.0, 12.0, 6.0, 120.0)
        assert a - b == pytest.approx(1.0)

    def test_crew_dominates_the_combined_kg(self):
        """640 lb of crew against a 120 lb hull: their CG sets KG, which is why
        it is an explicit, stated assumption rather than an implicit one."""
        hull_only = transverse_gmt_design_in(3.0, 12.0, 5.0, 120.0, crew_lbs=0.0)
        loaded = transverse_gmt_design_in(3.0, 12.0, 5.0, 120.0,
                                          crew_lbs=640.0, crew_kg_in=12.0)
        assert loaded < hull_only - 4.0

    def test_crew_cg_is_config_backed(self):
        assert hasattr(config, "CREW_VERTICAL_CG_IN")
        a = transverse_gmt_design_in(3.0, 12.0, 5.0, 120.0, crew_lbs=640.0)
        b = transverse_gmt_design_in(3.0, 12.0, 5.0, 120.0, crew_lbs=640.0,
                                     crew_kg_in=config.CREW_VERTICAL_CG_IN)
        assert a == pytest.approx(b)

    def test_form_only_estimate_is_unconservative_for_a_top_flanged_hull(self):
        """hull_generator's GM_T assumes KG == waterline.  A top-flanged hull
        loaded with crew sits well above that, so the form estimate OVERSTATES
        stability -- the trap this replaces."""
        draft = 6.9
        form_only = 3.0 + 12.0 - draft            # KG := waterline
        real = transverse_gmt_design_in(3.0, 12.0, 8.5, 120.0, crew_lbs=640.0,
                                        crew_kg_in=12.0)
        assert real < form_only
