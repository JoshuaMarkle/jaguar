# tests/test_fit_age_provenance.py
"""
A regression described as 28-day must contain only breaks KNOWN to be at
design age.

THE HOLE THIS PINS.  fit_eligibility read

    age = int(row.get("curing_days") or 0)
    if age and design and age < design:      # 0 short-circuits

so a row with no recorded age was silently admitted, and the column defaulted
to 0, so a row imported without an age was indistinguishable from a legacy
one.  The check read as enforcing a 28-day basis while enforcing nothing --
the same shape as a sentinel never compared and a name never bound.

On the shipped database this took the f'c fit from an apparent 4 points to a
real 2, and removed the ENTIRE tensile lab fit (both f_t rows were
age-unrecorded), which is the tier that sets the density search floor.

Every test here runs against a COPY of the shipped database in tmp_path.
Opening the real one puts WAL journal files in the repo, which the suite's
working-tree guard rejects -- correctly.
"""
from __future__ import annotations

import os
import shutil

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_DB = os.path.join(REPO, "concrete_mixes.db")

pytestmark = pytest.mark.skipif(not os.path.exists(SHIPPED_DB),
                                reason="needs the shipped mix database")


@pytest.fixture
def db(tmp_path):
    """A throwaway copy of the shipped DB, so nothing touches the repo."""
    from mix_database import MixDatabase
    dst = tmp_path / "mixes.db"
    shutil.copy2(SHIPPED_DB, dst)
    d = MixDatabase(db_path=str(dst))
    yield d
    try:
        d._conn.close()
    except Exception:
        pass


def test_an_age_unrecorded_row_is_excluded(db):
    for raw in (None, 0):
        why = db.fit_eligibility({"density_pcf": 70.0, "fc_psi": 1500.0,
                                  "curing_days": raw})
        assert why == "age not recorded", (
            f"curing_days={raw!r} was admitted; a break of unknown age could "
            "be a 7-day break, which is exactly what the early-age exclusion "
            "exists to keep out")


def test_zero_and_null_are_the_same_state_not_two(db):
    """An age of zero days is not a physically meaningful break, so there is
    no third state to preserve -- but both must read as UNRECORDED, never as
    'recorded as 0, therefore assume design age'."""
    a = db.fit_eligibility({"density_pcf": 70.0, "fc_psi": 1.0,
                            "curing_days": None})
    b = db.fit_eligibility({"density_pcf": 70.0, "fc_psi": 1.0,
                            "curing_days": 0})
    assert a == b == "age not recorded"


def test_an_at_age_row_is_still_admitted(db):
    assert db.fit_eligibility({"density_pcf": 70.0, "fc_psi": 1500.0,
                               "curing_days": db.design_age_days()}) == ""


def test_an_early_break_is_still_excluded(db):
    why = db.fit_eligibility({"density_pcf": 70.0, "fc_psi": 1500.0,
                              "curing_days": 7})
    assert why.endswith("-day break")


def test_the_shipped_fc_fit_reports_two_points_not_four(db):
    """
    The number that matters: the f'c regression is a TWO point fit with rows
    held out, not the four-point fit it appeared to be.  If this reads four
    again without new lab data, the hole has been reopened.
    """
    p = db.fit_provenance("fc_psi")
    assert p["n_used"] == 2, f"expected 2 age-confirmed f'c points, got {p['n_used']}"
    assert p["n_excluded"] >= 2
    assert "age not recorded" in p["excluded"], (
        "age-unrecorded rows must be reported as held out, BY REASON")
    held = p["excluded"]["age not recorded"]
    assert any(abs(d - 65.0) < 1e-6 for d, _v in held), (
        "the 65.00 pcf point is one of the unrecorded ones -- it is the point "
        "an earlier density floor rested on")


def test_the_tensile_lab_tier_has_no_age_confirmed_data(db):
    """
    Consequence worth pinning: every f_t lab row was age-unrecorded, so the
    tier that sets the density search floor falls back to the seeded mixes
    table -- design targets, not measurements.
    """
    p = db.fit_provenance("ft_psi")
    assert p["n_used"] == 0
    assert p["n_excluded"] >= 2


def test_provenance_reports_the_span_it_is_valid_over(db):
    """A fit cannot defend a density below its own lowest measured point."""
    p = db.fit_provenance("fc_psi")
    lo, hi = p["span_pcf"]
    assert lo > 62.5, (
        "the age-confirmed data does not reach 62.5 pcf; a web at that "
        "density is an extrapolation below every confirmed break")
    assert lo == pytest.approx(66.898, abs=0.01)
    assert hi == pytest.approx(77.045, abs=0.01)


def test_the_escape_hatch_is_off_by_default(db):
    assert db._admit_unrecorded_age() is False
