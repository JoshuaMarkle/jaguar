# tests/test_mix_exchange.py
"""
Mix Lab data features (fix plan Phase 3, Tab 4):

  - manual regression expressions (typed coefficients, publish = pinned as
    the ACTIVE relationship, survives restarts and lab edits)
  - CSV export / import with de-duplication (team spreadsheet sharing)
  - multi-delete in one transaction with one refit

Every test uses a THROWAWAY database file — never the user's live
concrete_mixes.db.
"""
from __future__ import annotations

import numpy as np
import pytest

from mix_database import MixDatabase


@pytest.fixture()
def db(tmp_path):
    d = MixDatabase(db_path=str(tmp_path / "test_mixes.db"))
    yield d
    d.close()


def _add(db, date, density, fc, ft, batch="B1"):
    # curing_days: unrecorded age is inadmissible to a fit now, and these
    # tests exercise fit PUBLISHING, not age policy.
    return db.add_lab_result(curing_days=db.design_age_days(),
                             test_date=date, density_pcf=density,
                             fc_psi=fc, ft_psi=ft, batch_id=batch)


# ══════════════════════════════════════════════════════════════════════════
#  Manual regression expression
# ══════════════════════════════════════════════════════════════════════════

def test_manual_regression_drives_mix_properties(db):
    db.set_manual_regression(10.0, 0.0, 2.0, 0.0, version_tag="paper-eq",
                             publish=True)
    p = db.get_mix_properties(70.0, fos=1.0)
    assert p["strength_c"] == pytest.approx(700.0)
    assert p["strength_t"] == pytest.approx(140.0)
    assert "manual" in db.get_regression_stats().lower()


def test_manual_regression_survives_restart_and_lab_edits(db, tmp_path):
    db.set_manual_regression(10.0, 0.0, 2.0, 0.0, publish=True)
    # lab data arrives — the pinned manual expression must NOT be clobbered
    _add(db, "2026-07-11", 60.0, 500.0, 100.0)
    _add(db, "2026-07-11", 80.0, 2000.0, 400.0)
    assert db.get_mix_properties(70.0, fos=1.0)["strength_t"] == pytest.approx(140.0)
    # restart (new connection on the same file)
    db2 = MixDatabase(db_path=str(tmp_path / "test_mixes.db"))
    try:
        assert db2.get_mix_properties(70.0, fos=1.0)["strength_t"] == \
            pytest.approx(140.0)
    finally:
        db2.close()


def test_publishing_lab_fit_unpins_manual(db):
    db.set_manual_regression(10.0, 0.0, 2.0, 0.0, publish=True)
    _add(db, "2026-07-11", 60.0, 600.0, 120.0)
    _add(db, "2026-07-11", 80.0, 1000.0, 200.0)
    db.publish_current_regression(version_tag="back-to-lab")
    # active fit is now the lab regression: ft(70) = 160 (linear midpoint)
    assert db.get_mix_properties(70.0, fos=1.0)["strength_t"] == \
        pytest.approx(160.0, rel=1e-6)


def test_unpublished_manual_does_not_take_effect(db):
    _add(db, "2026-07-11", 60.0, 600.0, 120.0)
    _add(db, "2026-07-11", 80.0, 1000.0, 200.0)
    db.set_manual_regression(999.0, 0.0, 999.0, 0.0, publish=False)
    assert db.get_mix_properties(70.0, fos=1.0)["strength_t"] == \
        pytest.approx(160.0, rel=1e-6)


# ══════════════════════════════════════════════════════════════════════════
#  CSV exchange
# ══════════════════════════════════════════════════════════════════════════

def test_csv_round_trip_and_dedupe(db, tmp_path):
    _add(db, "2026-07-01", 60.0, 600.0, 120.0, batch="A")
    _add(db, "2026-07-02", 70.0, 900.0, 180.0, batch="B")
    _add(db, "2026-07-03", 80.0, 1400.0, 260.0, batch="C")
    csv_path = str(tmp_path / "share.csv")
    assert db.export_lab_results_csv(csv_path) == 3

    # teammate's fresh database imports the file
    db2 = MixDatabase(db_path=str(tmp_path / "mate.db"))
    try:
        added, skipped = db2.import_lab_results_csv(csv_path)
        assert (added, skipped) == (3, 0)
        assert len(db2.get_lab_results()) == 3
        # importing the same file again must be a no-op
        added, skipped = db2.import_lab_results_csv(csv_path)
        assert (added, skipped) == (0, 3)
        # and the regression refit from imported data matches the source
        a = db.get_mix_properties(70.0, fos=1.0)["strength_t"]
        b = db2.get_mix_properties(70.0, fos=1.0)["strength_t"]
        assert a == pytest.approx(b, rel=1e-9)
    finally:
        db2.close()


@pytest.mark.invalid_lab_row(
    "imports a deliberately malformed CSV to prove the importer skips "
    "bad rows; the surviving rows carry no curing age by construction, "
    "which is the input under test rather than an oversight")
def test_csv_import_skips_malformed_rows(db, tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("test_date,batch_id,density_pcf,fc_psi,ft_psi\n"
                 "2026-07-01,A,60.0,600.0,120.0\n"
                 "2026-07-02,B,not_a_number,900.0,180.0\n",
                 encoding="utf-8")
    added, skipped = db.import_lab_results_csv(str(p))
    assert (added, skipped) == (1, 1)


# ══════════════════════════════════════════════════════════════════════════
#  Multi-delete
# ══════════════════════════════════════════════════════════════════════════

def test_delete_lab_results_bulk(db):
    ids = [_add(db, "2026-07-01", 60.0, 600.0, 120.0),
           _add(db, "2026-07-02", 70.0, 900.0, 180.0),
           _add(db, "2026-07-03", 80.0, 1400.0, 260.0)]
    assert db.delete_lab_results(ids[:2]) == 2
    remaining = db.get_lab_results()
    assert len(remaining) == 1 and remaining[0]["id"] == ids[2]
    assert db.delete_lab_results([]) == 0
