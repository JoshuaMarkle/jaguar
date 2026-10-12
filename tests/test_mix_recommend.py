# tests/test_mix_recommend.py
"""
MixDatabase.recommend_mix_targets — what density/strength the mix team should
develop next.  Pure/Qt-free logic, so tested directly with a controlled
regression and lab-data coverage (the live concrete_mixes.db never decides
whether these pass).
"""
from __future__ import annotations

import numpy as np
import pytest

import config


def _db(monkeypatch, ft=(8.0, -472.0), fc=(40.0, -2280.0), lab_dens=(70.0, 80.0)):
    """A db with a known linear fit and known lab-data coverage.

    ft = 8ρ − 472  → zero-tension floor at ρ = 59.0 pcf.
    Lab data at 70/80 pcf, so 57.5 and 64 pcf targets both EXTRAPOLATE below it.
    """
    db = config.get_mix_db()
    monkeypatch.setattr(db, "ft_regression", np.array(ft), raising=False)
    monkeypatch.setattr(db, "fc_regression", np.array(fc), raising=False)
    monkeypatch.setattr(db, "get_lab_results",
                        lambda limit=500: [{"density_pcf": d} for d in lab_dens])
    return db


class TestDesignMode:
    def test_targets_group_by_density_with_roles(self, monkeypatch):
        db = _db(monkeypatch)
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0],
                                       bend_dc=0.64, hoop_dc=0.23,
                                       density_at_floor=True)
        assert rec["mode"] == "design"
        by = {t["density"]: t for t in rec["targets"]}
        assert set(by) == {64.0, 57.5}                 # flanges collapse to one row
        assert "flange" in by[64.0]["role"]
        assert "web" in by[57.5]["role"]

    def test_flange_reports_bending_dc_and_real_strength(self, monkeypatch):
        db = _db(monkeypatch)
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0],
                                       bend_dc=0.64, hoop_dc=0.23)
        t64 = next(t for t in rec["targets"] if t["density"] == 64.0)
        assert t64["utilization"] == pytest.approx(0.64)
        assert t64["ft_achieve_psi"] == pytest.approx(8 * 64 - 472)     # 40 psi
        # the design still passes down to D/C × assumed strength
        assert t64["min_ft_to_pass_psi"] == pytest.approx(0.64 * (8 * 64 - 472))

    def test_web_below_floor_reports_zero_tension_and_hoop_dc(self, monkeypatch):
        db = _db(monkeypatch)
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0],
                                       bend_dc=0.64, hoop_dc=0.23)
        t = next(t for t in rec["targets"] if t["density"] == 57.5)
        assert t["ft_achieve_psi"] == 0.0            # 8*57.5-472 < 0 → clamped
        assert t["utilization"] == pytest.approx(0.23)

    def test_extrapolated_targets_flagged(self, monkeypatch):
        db = _db(monkeypatch)                         # lab at 70/80; targets below
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0],
                                       bend_dc=0.64, hoop_dc=0.23)
        assert all("EXTRAPOLATION" in t["confidence"] for t in rec["targets"])

    def test_interpolated_target_not_flagged(self, monkeypatch):
        db = _db(monkeypatch, lab_dens=(60.0, 68.0, 80.0))   # 64 now inside
        rec = db.recommend_mix_targets(zone_densities=[64.0, 64.0, 64.0],
                                       bend_dc=0.7, hoop_dc=0.2)
        t64 = rec["targets"][0]
        assert "EXTRAPOLATION" not in t64["confidence"]

    def test_lighten_web_lever_when_at_floor(self, monkeypatch):
        db = _db(monkeypatch)
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0],
                                       bend_dc=0.64, hoop_dc=0.23,
                                       density_at_floor=True)
        kinds = [im["kind"] for im in rec["improvements"]]
        assert "lighten_web" in kinds
        lever = next(im for im in rec["improvements"] if im["kind"] == "lighten_web")
        assert "57.5" in lever["text"]

    def test_uniform_single_mix_role_and_governing_dc(self, monkeypatch):
        # A uniform [65,65,65] winner is one mix, not an I-beam: one target row,
        # labelled as such, reporting the worst of bending/hoop.
        db = _db(monkeypatch, lab_dens=(60.0, 65.0, 80.0))
        rec = db.recommend_mix_targets(zone_densities=[65.0, 65.0, 65.0],
                                       bend_dc=0.70, hoop_dc=0.22)
        assert len(rec["targets"]) == 1
        t = rec["targets"][0]
        assert "uniform" in t["role"]
        assert t["utilization"] == pytest.approx(0.70)     # max(bend, hoop)

    def test_no_lighten_lever_when_not_at_floor(self, monkeypatch):
        db = _db(monkeypatch)
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0],
                                       bend_dc=0.64, hoop_dc=0.23,
                                       density_at_floor=False)
        assert all(im["kind"] != "lighten_web" for im in rec["improvements"])


class TestDataGapMode:
    def test_no_design_yields_data_gap_guidance(self, monkeypatch):
        db = _db(monkeypatch)
        rec = db.recommend_mix_targets()
        assert rec["mode"] == "data_gap"
        assert rec["targets"] == []
        assert rec["improvements"]                    # some tests suggested
        assert any(im.get("kind") == "probe_floor" for im in rec["improvements"])

    def test_two_point_fit_is_flagged(self, monkeypatch):
        db = _db(monkeypatch, lab_dens=(70.0, 80.0))
        rec = db.recommend_mix_targets(zone_densities=[70.0, 70.0, 70.0],
                                       bend_dc=0.5, hoop_dc=0.2)
        assert any("two-point" in n for n in rec["notes"])


class TestFlexuralAllowable:
    """f_r (MOR) is the bending-gate allowable when a MOR fit exists; the gate
    falls back to the (conservative) direct-tensile f_t otherwise."""

    def _fr(self, monkeypatch, fr=(12.0, -500.0), **kw):
        db = _db(monkeypatch, **kw)
        monkeypatch.setattr(db, "fr_regression", np.array(fr), raising=False)
        return db

    def test_falls_back_to_ft_without_mor_fit(self, monkeypatch):
        db = _db(monkeypatch)
        monkeypatch.setattr(db, "fr_regression", None, raising=False)
        mp = db.get_mix_properties(70.0, fos=1.5)
        assert mp["flexural_source"] == "ft"
        assert mp["strength_r"] is None
        assert mp["strength_flexural"] == pytest.approx(mp["strength_t"])

    def test_uses_mor_when_available(self, monkeypatch):
        db = self._fr(monkeypatch)
        mp = db.get_mix_properties(70.0, fos=1.5)
        assert mp["flexural_source"] == "fr"
        assert mp["strength_flexural"] == pytest.approx((12 * 70 - 500) / 1.5)
        # MOR > direct tensile for concrete, so the gate is less conservative
        assert mp["strength_flexural"] > mp["strength_t"]

    def test_recommend_reports_both_and_marks_fr_governing(self, monkeypatch):
        db = self._fr(monkeypatch, lab_dens=(60.0, 70.0, 80.0))
        rec = db.recommend_mix_targets(zone_densities=[70.0, 70.0, 70.0],
                                       bend_dc=0.7, hoop_dc=0.2)
        assert rec["governing_strength"] == "fr"
        t = rec["targets"][0]
        assert t["ft_achieve_psi"] == pytest.approx(8 * 70 - 472)
        assert t["fr_achieve_psi"] == pytest.approx(12 * 70 - 500)
        assert t["governing_strength"] == "fr"

    def test_recommend_ft_governs_and_suggests_logging_fr(self, monkeypatch):
        db = _db(monkeypatch)
        monkeypatch.setattr(db, "fr_regression", None, raising=False)
        rec = db.recommend_mix_targets(zone_densities=[70.0, 70.0, 70.0],
                                       bend_dc=0.7, hoop_dc=0.2)
        assert rec["governing_strength"] == "ft"
        assert rec["targets"][0]["fr_achieve_psi"] is None
        assert any(im.get("kind") == "log_fr" for im in rec["improvements"])


class TestFrTrainingAndMigration:
    """End-to-end on a real (temp) DB: the fr_psi column migration, the
    independent MOR fit, and the gate switching from ft to fr."""

    def test_fr_fit_forms_and_gate_switches(self, tmp_path):
        from mix_database import MixDatabase
        db = MixDatabase(db_path=str(tmp_path / "mix.db"))
        _age = db.design_age_days()   # unrecorded age no longer enters a fit
        db.add_lab_result("2026-01-01", 70, 1500, 300, curing_days=_age)
        db.add_lab_result("2026-01-01", 80, 2200, 440, curing_days=_age)
        assert db.fr_regression is None
        assert db.get_mix_properties(75)["flexural_source"] == "ft"
        db.add_lab_result("2026-01-02", 70, 1500, 300, fr_psi=500,
                          curing_days=_age)                      # +MOR
        db.add_lab_result("2026-01-02", 80, 2200, 440, fr_psi=760,
                          curing_days=_age)
        assert db.fr_regression is not None
        assert db._regression_n_fr == 2
        assert db.get_mix_properties(75)["flexural_source"] == "fr"
        db.close()


class TestDegenerate:
    def test_no_regression_returns_empty_guidance(self, monkeypatch):
        db = config.get_mix_db()
        monkeypatch.setattr(db, "ft_regression", None, raising=False)
        monkeypatch.setattr(db, "fc_regression", None, raising=False)
        rec = db.recommend_mix_targets(zone_densities=[64.0, 57.5, 64.0])
        assert rec["mode"] == "data_gap"
        assert rec["targets"] == []
        assert rec["notes"]
