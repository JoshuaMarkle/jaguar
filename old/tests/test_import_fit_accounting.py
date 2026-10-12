# tests/test_import_fit_accounting.py
"""
ADDED IS NOT ADMITTED.

A lab CSV can import perfectly -- added=4, skipped=0, invalid=0 -- and put
ZERO rows into any regression, because a row with no curing age is
inadmissible to a fit (a break of unknown age could be a 7-day break).  When
that happens the tensile tier falls back to the seeded `mixes` table, which is
DESIGN INTENT rather than measurement, and every density floor and predicted
strength downstream is built on it.

That is the shape this file exists to prevent: a teammate imports the team
spreadsheet, sees a clean success, and gets defensible-LOOKING designs built on
nothing.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from mix_database import MixDatabase

_ROWS = [
    ("A-1", "2026-08-01", 66.9, 968, 190),
    ("A-2", "2026-08-01", 71.9, 1750, 350),
    ("B-1", "2026-08-02", 77.0, 3390, 610),
    ("B-2", "2026-08-02", 74.2, 2400, 470),
]


def _csv(tmp_path, with_age):
    p = tmp_path / ("with_age.csv" if with_age else "no_age.csv")
    head = "Mix ID,Test Date," + ("Curing Days," if with_age else "") + \
           "Density (pcf),Compressive Strength (psi),Split Tensile (psi)"
    lines = [head]
    for mix, date, d, fc, ft in _ROWS:
        age = "28," if with_age else ""
        lines.append(f"{mix},{date},{age}{d},{fc},{ft}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


@pytest.fixture
def db(tmp_path):
    d = MixDatabase(db_path=str(tmp_path / "imp.db"))
    yield d
    try:
        d._conn.close()
    except Exception:
        pass


@pytest.mark.invalid_lab_row(
    "imports a sheet with no curing-age column on purpose; that is the "
    "production failure mode under test, not an oversight in the fixture")
def test_a_sheet_with_no_age_column_is_reported_as_unusable(db, tmp_path):
    rep = db.import_lab_csv(_csv(tmp_path, with_age=False))
    assert rep.added == 4, "the rows should still land in the TABLE"
    assert rep.fit_admitted == 0, "none of them may enter a fit"
    assert rep.fit_usable is False
    assert rep.fit_excluded.get("age not recorded") == 4

    blob = " ".join(rep.warnings)
    assert "EXCLUDED FROM EVERY FIT" in blob
    assert "NO IMPORTED ROW REACHED ANY REGRESSION" in blob, (
        "an import that buys the fit nothing must fail VISIBLY, not fall back "
        "to the seeded mixes table in silence")
    assert "curing days" in blob.lower(), (
        "the warning must name the column the sheet is missing")


def test_the_same_sheet_with_an_age_column_is_fully_admitted(db, tmp_path):
    rep = db.import_lab_csv(_csv(tmp_path, with_age=True))
    assert rep.added == 4
    assert rep.fit_admitted == 4
    assert rep.fit_usable is True
    assert not rep.fit_excluded


@pytest.mark.invalid_lab_row(
    "checks the fallback SOURCE is reported when no row is admissible; the "
    "unaged rows are the input under test")
def test_the_fallback_source_is_named_not_implied(db, tmp_path):
    rep = db.import_lab_csv(_csv(tmp_path, with_age=False))
    assert rep.fit_sources.get("strength"), "the report must name the fit source"
    assert "mixes" in " ".join(rep.warnings), (
        "the warning must say WHICH source the fit fell back to, so a reader "
        "can tell measurement from seeded design intent")


def test_accounting_survives_a_db_with_no_measurements(db, tmp_path):
    """An empty import must not crash the accounting."""
    p = tmp_path / "empty.csv"
    p.write_text("Mix ID,Test Date,Density (pcf)\n", encoding="utf-8")
    rep = db.import_lab_csv(str(p))
    assert rep.fit_admitted == 0
    assert isinstance(rep.fit_excluded, dict)


@pytest.mark.invalid_lab_row(
    "a misnamed strength column is the input under test; the rows it produces "
    "are deliberately short of the measurement they should carry")
def test_an_unrecognised_measurement_column_is_reported(db, tmp_path):
    """
    THE SECOND SILENT FAILURE.  A sheet whose compressive column reads
    'Compression (psi)' rather than 'Compressive Strength (psi)' imported
    perfectly -- added=2, fit_admitted=2, no warnings -- while every f'c value
    was dropped and the compressive fit ran on fewer points than anyone knew.

    An unrecognised header is ignored by design (sheets carry labels and
    scratch notes). What must not be silent is ignoring one that LOOKS like a
    measurement.
    """
    p = tmp_path / "misnamed.csv"
    p.write_text(
        "Mix ID,Test Date,Curing Days,Density (pcf),Compression (psi)\n"
        "A-1,2026-08-01,28,66.9,968\n"
        "A-2,2026-08-01,28,71.9,1750\n", encoding="utf-8")
    rep = db.import_lab_csv(str(p))
    blob = " ".join(rep.warnings)
    assert "NOT RECOGNISED" in blob, (
        "a dropped measurement column must be reported; silently ignoring it "
        "builds the fit on fewer points than the team believes")
    assert "Compression (psi)" in blob, "the warning must name the column"


def test_ordinary_non_measurement_columns_do_not_warn(db, tmp_path):
    """
    The other half: warning about every label and note would train the reader
    to skip the warnings, which is how the first silent failure survived.
    """
    p = tmp_path / "labels.csv"
    p.write_text(
        "Mix ID,Test Date,Curing Days,Density (pcf),"
        "Compressive Strength (psi),Technician,Cylinder Colour\n"
        "A-1,2026-08-01,28,66.9,968,jo,blue\n"
        "A-2,2026-08-01,28,71.9,1750,sam,red\n", encoding="utf-8")
    rep = db.import_lab_csv(str(p))
    assert "NOT RECOGNISED" not in " ".join(rep.warnings), (
        "Technician/Cylinder Colour are not measurements; warning about them "
        "would make the real warning invisible")
    assert rep.fit_admitted == 2


# ── run-critical warnings must reach the GUI, not just the log ──────────────

def test_the_four_run_critical_warnings_are_detected():
    """
    The frozen build is windowed, so everything printed goes to a log nobody
    opens.  These four decide whether a result can be trusted at all.
    """
    import ui.warning_banner as wb
    res = {"boundary_flags": ["depth = 15.07 at max"],
           "mix_regression": {"n": 2},
           "gate_records": []}
    keys = {w["key"] for w in wb.collect(res, "[!! CONFIG DEFAULTED]")}
    assert {"boundary", "thin_fit", "ungated", "config_defaulted"} <= keys, (
        f"missing run-critical warnings: {keys}")


def test_a_clean_result_shows_no_banner():
    """Warning about everything trains the reader to skip the banner."""
    import ui.warning_banner as wb
    res = {"boundary_flags": [], "mix_regression": {"n": 12},
           "gate_records": [{"accepted": True}], "gate_notes": []}
    assert wb.collect(res, "all fine here") == []
    assert wb.as_html([], None) == ""


def test_the_banner_names_the_boundary_it_found():
    import ui.warning_banner as wb
    got = wb.collect({"boundary_flags": ["depth = 15.07 at max"]}, "")
    assert any("15.07" in w["headline"] for w in got), (
        "a boundary warning that does not say WHICH bound is unactionable")


# ── the team's real two-sheet export ────────────────────────────────────────

_H_FLEX = ("Mix Name,Date set,Date tested,Curing time (days),Dry Weight (lbs),"
           "Wet Weight (lbs(),Dry Density (pcf),Wet Density (pcf),"
           "Compressive Load (lbs),Flexural Strength (psi)")


def test_a_typed_zero_density_never_becomes_a_derived_impossibility(db, tmp_path):
    """
    THE BLOCKING ONE.  The team's sheet writes a typed 0 into a density cell
    when its weight cell is empty.  _pos() correctly reads that as missing --
    and the row then fell through to weight/volume, which produced 384 pcf
    from a 3.25 lb specimen and admitted it straight into the regression.
    Concrete is not 384 pcf; the specimen volume assumed for that row was
    simply wrong.

    _PLAUSIBLE_PCF existed all along but was only used to choose
    cylinder-vs-cube, never as a check on the result.
    """
    p = tmp_path / "flex.csv"
    p.write_text(_H_FLEX + "\n"
                 "B-1,2026-07-02,2026-07-30,28,3.15,3.45,67.5,70.8,2100,540\n"
                 "B-2,2026-07-02,2026-07-30,28,3.25,3.55,0,73.0,2200,560\n",
                 encoding="utf-8")
    db.import_lab_csv(str(p))
    rows = {dict(r)["batch_id"]: dict(r) for r in db.get_lab_results(limit=20)}
    d = float(rows["B-2"]["density_pcf"])
    assert 20.0 <= d <= 200.0, f"stored an impossible density: {d} pcf"
    assert d == pytest.approx(73.0), (
        "a MEASURED wet density must beat a value derived from weight; the "
        "row had 73.0 on the scale and the derivation won anyway")
    assert "DISCARDED" in (rows["B-2"]["notes"] or ""), (
        "the row must carry why the number it looks like it should have is "
        "absent")


def test_a_zero_density_row_with_no_usable_density_is_invalid(db, tmp_path):
    """Both densities zero and no usable weight -> the row must not import."""
    p = tmp_path / "comp.csv"
    p.write_text(
        "Mix Name,Date set,Date tested,Curing time (days),"
        "Dry Density (pcf),Wet Density (pcf),Compressive Strength (psi)\n"
        "A-3,2026-07-01,2026-07-29,28,0,0,1200\n", encoding="utf-8")
    rep = db.import_lab_csv(str(p))
    assert rep.added == 0 and rep.invalid == 1
    assert any("no weight or density" in w for w in rep.warnings)


def test_a_zero_strength_never_enters_a_fit(db, tmp_path):
    """A typed 0 in a strength column is missing data, not a 0 psi specimen."""
    p = tmp_path / "zero_fc.csv"
    p.write_text(
        "Mix Name,Date set,Date tested,Curing time (days),"
        "Dry Density (pcf),Compressive Strength (psi)\n"
        "A-4,2026-07-01,2026-07-29,28,74.2,0\n"
        "A-1,2026-07-01,2026-07-29,28,66.9,968\n", encoding="utf-8")
    db.import_lab_csv(str(p))
    prov = db.fit_provenance("fc_psi")
    assert all(v > 0 for _d, v in prov["used"]), (
        "a 0 psi point would drag the whole strength regression toward the "
        "origin")
    assert prov["n_used"] == 1


# ── the banner must not print Python at the user ───────────────────────────

BOUNDARY_FLAGS = [
    {"variable": "thickness", "value": 0.75, "edge": "min",
     "physical": False, "why": "min buildable trowel"},
    {"variable": "density", "value": 70.0, "edge": "min",
     "physical": True, "why": "regression viability floor"},
]


def test_the_boundary_warning_is_prose_not_a_dict_literal():
    """
    The banner str()-ed the flag dicts, putting

        (at: {'variable': 'thickness', 'value': 0.75, 'edge': 'min', ...})

    in front of the user, while the CONSOLE printed the same facts readably.
    The banner is the copy the team actually reads; it must not be the worse
    one.
    """
    import ui.warning_banner as wb
    head = wb.collect({"boundary_flags": BOUNDARY_FLAGS}, "")[0]["headline"]
    for leak in ("{", "}", "'variable'", "'physical'", "None"):
        assert leak not in head, f"the banner leaked Python syntax: {leak!r}"
    assert "thickness" in head and "0.750" in head


def test_a_physical_limit_reads_differently_from_a_range_edge():
    """The actionable distinction: one can be widened, the other cannot."""
    import ui.warning_banner as wb
    head = wb.collect({"boundary_flags": BOUNDARY_FLAGS}, "")[0]["headline"]
    assert "widen" in head.lower(), "a movable edge must say to widen it"
    assert "cannot be widened" in head, (
        "a physical limit must say so, or the reader wastes a run widening "
        "something that will not move")


def test_malformed_flags_do_not_crash_the_banner():
    """A banner failure would take out the warnings themselves."""
    import ui.warning_banner as wb
    for junk in ("just a string", [{"variable": "x"}], [None], {"a": 1}):
        out = wb.collect({"boundary_flags": junk}, "")
        assert isinstance(out, list)


# ── the audit summary must survive numpy fields ────────────────────────────

def test_the_audit_summary_handles_a_numpy_station_array():
    """
    `len(result.get("x") or ())` evaluates the ARRAY's truth value, which numpy
    refuses for more than one element -- it aborted the whole audit block, so
    no record was written for the run at all.
    """
    import numpy as np
    result = {"x": np.linspace(0.0, 20.0, 161)}
    # the form main.py always used, and the UI now uses
    n = len(result["x"]) if result.get("x") is not None else None
    assert n == 161
    with pytest.raises(ValueError):
        len(result.get("x") or ())        # the shape that broke


def test_the_ui_audit_block_no_longer_uses_the_truthy_or_form():
    """Pin the fix in the file, so the idiom cannot come back."""
    import io as _io
    import ui.structural_tab as st
    # Strip comments before matching: the FIX's own comment names the old
    # form, and matching raw text flagged the explanation as if it were the
    # defect it documents.
    src = _io.open(st.__file__, encoding="utf-8").read()
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert 'len(result.get("x") or ())' not in code, (
        "the numpy-truthiness form is back in the audit summary")
    assert '"feasible": result.get("feasible")' not in code, (
        "the result dict has no 'feasible' key; reading it logged None on "
        "every run, including successful ones")


# ── one audit summary, not two ─────────────────────────────────────────────

def test_the_gui_and_cli_build_the_SAME_audit_summary():
    """
    THE DEFECT WAS THE DUPLICATION, not the two bugs it produced.

    The summary existed in main._audit_run and again in the GUI's audit block,
    and they disagreed in ways that surfaced one at a time: the GUI's
    `len(result.get("x") or ())` raised on a numpy array so NO record was
    written, and its `result.get("feasible")` read a key the result dict does
    not have so it logged None forever. main.py's copy had neither.
    """
    import inspect, io as _io
    import main
    import ui.structural_tab as st

    src = _io.open(st.__file__, encoding="utf-8").read()
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "build_audit_summary" in code, (
        "the GUI must call the shared builder, not assemble its own summary")
    assert '"best_weight_lbs":' not in code, (
        "the GUI is building an audit summary of its own again; that is how "
        "the two drifted apart the first time")


def test_the_summary_field_set_is_declared_and_complete():
    """A field omitted here is a dimension two runs can differ on while
    logging identically -- which is exactly how every historical entry ended
    up carrying the same hash."""
    import numpy as np, main
    got = main.build_audit_summary(
        {"weight_lbs": 1.0, "x": np.zeros(41), "t_in": 0.5})
    assert set(got) == set(main.AUDIT_SUMMARY_FIELDS), (
        f"summary fields drifted from AUDIT_SUMMARY_FIELDS: "
        f"{set(got) ^ set(main.AUDIT_SUMMARY_FIELDS)}")


def test_the_summary_survives_the_shapes_that_broke_it():
    import numpy as np, main
    got = main.build_audit_summary({"weight_lbs": 266.9,
                                    "x": np.linspace(0, 20, 161)})
    assert got["n_stations"] == 161, "the numpy array must be measured, not truthed"
    assert got["feasible"] is True, "a result dict means a winner was found"
    assert main.build_audit_summary(None)["feasible"] is False
    assert main.build_audit_summary(None)["n_stations"] is None


def test_the_secondary_checks_are_labelled_advisory():
    """
    A bare "[WARNING]" on a number that does not gate and overstates by
    construction is indistinguishable from a real failure, so the reader
    cannot tell that an exceedance needs no action.
    """
    import inspect, structural
    src = inspect.getsource(structural.run_torsion_lateral_checks)
    assert "ADVISORY" in src
    assert "does not gate" in src
    assert '"OK" if torsion_ok else "WARNING"' not in src, (
        "the bare WARNING label is back")
