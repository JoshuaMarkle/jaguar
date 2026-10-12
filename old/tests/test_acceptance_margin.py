# tests/test_acceptance_margin.py
"""
The acceptance margin on the governing Kupfer utilisation.

WHY IT EXISTS.  The sweep's WEIGHT converges quickly with longitudinal station
count (0.58% between 161 and 321), but the utilisation that decides
FEASIBILITY does not: measured across five designs on the live hull it drifts
1-2% per doubling with no plateau out to 641 stations.  A design accepted at
1.0 on the 161-station search grid is therefore not demonstrably feasible --
the 110.39 lb winner that motivated this sat at 0.9957 at 161 and failed at
1.0040 at 321.

The margin is a NUMERICAL guard, not extra structural conservatism: the factor
of safety covers the physics and lives inside the allowables.  These tests pin
that it is configurable, that it actually binds, and that it is not silently
conflated with the FoS.
"""
from __future__ import annotations

import numpy as np
import pytest

import config
import structural
from geometry import calculate_shell_properties
from structural import evaluate_multizone_weight, kupfer_acceptance_limit


def _hull(n_st=41, n_wl=17, L_ft=20.0, depth_in=13.0, half_beam_in=13.0,
          t_in=0.5):
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    w = half_beam_in * np.sin(np.pi * x / L_ft)
    y = np.outer(w, np.sqrt(z / depth_in))
    p = calculate_shell_properties(x, z, y, t_in)
    v = p.A_in2 > 0.001
    return x[v], p, v


def _props(scale=1.0):
    return lambda d: {"density": float(d),
                      "strength_t": (18.0 * (float(d) - 55.0) + 15.0) * scale,
                      "strength_c": (90.0 * (float(d) - 55.0) + 120.0) * scale}


def _ev(scale, t_in=0.5):
    """Evaluate one design; ``scale`` tunes the allowables so the utilisation
    can be placed on either side of the margin."""
    x, p, v = _hull(t_in=t_in)
    sheer = p.c_bot[v] + p.c_top[v]
    dx = float(np.max(x)) / (len(x) - 1)
    return evaluate_multizone_weight(
        [3., 7.5, 12., 15.], [5., 13.], [80.0, 60.0, 80.0], (0.34, 0.67),
        x, dx, p.width_ft[v], t_in, p.cum_area_in2[v], p.cum_Az_in3[v],
        p.cum_Iz_in4[v], p.z_grid_in, sheer, p.c_bot[v], _props(scale),
        coords_3=[5., 10., 15.], cum_Iy=p.cum_Iy_in4[v],
        yhalf_grid=p.yhalf_in[v], return_details=True)


class TestConfigurable:
    def test_default_is_the_derived_value(self):
        assert kupfer_acceptance_limit() == pytest.approx(0.95)

    def test_reads_the_config_singleton(self, monkeypatch):
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.80)
        assert kupfer_acceptance_limit() == pytest.approx(0.80)

    def test_file_backed_through_settings(self):
        from config.settings import get_settings
        assert config.KUPFER_ACCEPTANCE_MAX == pytest.approx(
            get_settings().structural.kupfer_acceptance_max)

    def test_optimization_config_carries_it(self):
        from domain.models import OptimizationConfig
        assert OptimizationConfig().kupfer_acceptance_max == pytest.approx(0.95)
        assert OptimizationConfig(
            kupfer_acceptance_max=0.9).kupfer_acceptance_max == pytest.approx(0.9)

    def test_one_can_be_disabled(self, monkeypatch):
        """1.0 restores the old accept-anything-that-passes behaviour."""
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 1.0)
        assert kupfer_acceptance_limit() == pytest.approx(1.0)


class TestItBinds:
    def _scale_for_util(self, target):
        """Find an allowable scale putting the utilisation near ``target``."""
        lo, hi = 0.05, 50.0
        for _ in range(40):
            mid = (lo + hi) / 2.0
            u = _ev(mid)["kupfer_max"]
            if u > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    def test_design_between_the_margin_and_one_is_rejected(self, monkeypatch):
        """The whole point: 0.95 < D/C < 1.0 passes the physics but is NOT
        accepted, because refinement can push it over."""
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95)
        s = self._scale_for_util(0.97)
        d = _ev(s)
        assert 0.95 < d["kupfer_max"] < 1.0, "fixture missed the target band"
        assert d["feasible"] is False
        assert d["reject"] == "kupfer_tensile"
        import structural
        assert structural.is_rejected(d["weight"])

    def test_the_same_design_is_accepted_with_the_margin_disabled(self, monkeypatch):
        s = self._scale_for_util(0.97)
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 1.0)
        d = _ev(s)
        assert 0.95 < d["kupfer_max"] < 1.0
        import structural
        assert d["feasible"] is True and not structural.is_rejected(d["weight"])

    def test_design_below_the_margin_is_accepted(self, monkeypatch):
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95)
        s = self._scale_for_util(0.60)
        d = _ev(s)
        assert d["kupfer_max"] < 0.95
        import structural
        assert d["feasible"] is True and not structural.is_rejected(d["weight"])

    def test_a_tighter_margin_rejects_more(self, monkeypatch):
        s = self._scale_for_util(0.90)
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95)
        assert _ev(s)["feasible"] is True
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.85)
        assert _ev(s)["feasible"] is False


class TestNotConflatedWithSafetyFactor:
    def test_margin_is_independent_of_the_factor_of_safety(self, monkeypatch):
        """The FoS scales the ALLOWABLES (physics); the margin is a threshold on
        the resulting ratio (numerics).  Changing one must not move the other."""
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95)
        monkeypatch.setattr(config, "FACTOR_OF_SAFETY", 1.5)
        assert kupfer_acceptance_limit() == pytest.approx(0.95)
        monkeypatch.setattr(config, "FACTOR_OF_SAFETY", 3.0)
        assert kupfer_acceptance_limit() == pytest.approx(0.95)

    def test_reported_utilisation_is_the_raw_ratio_not_the_margin(self, monkeypatch):
        """kupfer_max must stay the true D/C so the report can show how close
        the design is to BOTH the margin and 1.0."""
        monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.60)
        d = _ev(1.0)
        assert d["kupfer_max"] > 0.0
        assert d["kupfer_max"] != pytest.approx(0.60)
