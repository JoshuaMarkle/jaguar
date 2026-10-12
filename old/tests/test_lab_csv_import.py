# tests/test_lab_csv_import.py
"""
Importing the mix team's test-results sheets.

The team exports one CSV per test type out of a Google Sheet — a flexural tab
and a compressive tab — and either, or both at once, has to land in the Mix
Lab.  The workbook's third "import" tab pastes both tables side by side and is
deliberately REFUSED, since its two halves share one set of mix-name and date
columns without their rows lining up.  The fixtures below are trimmed copies of
the real 2026-2027 exports, typos and Excel error cells included.

Every test uses a THROWAWAY database file — never the user's live
concrete_mixes.db.
"""
from __future__ import annotations

import pytest


# This file's SUBJECT is the CSV importer, including the rows production
# rejects: sheets with no curing-age column, early-age breaks held out of the
# fit, refused side-by-side layouts.  The fixture-validity guard would flag
# every one of those as "a test building data production rejects" -- which is
# true, and is the point here rather than an oversight.
pytestmark = pytest.mark.invalid_lab_row(
    "exercises the importer itself, including sheets and rows the fit "
    "deliberately excludes; the rejected rows ARE the input under test")

from data.lab_csv import SpecimenConstants, parse_lab_csv
from mix_database import MixDatabase


CONSTS = SpecimenConstants()          # never read the user's canoe_suite.cfg


# ── Fixtures: the team's real column layouts ─────────────────────────
COMPRESSIVE = (
    "Mix Name,Date set,Date tested,Curing time (days),Dry Weight (lbs),"
    "Wet Weight (lbs(),Dry Density (pcf),Wet Density (pcf),"
    "Compressive Strength (psi),,,,,\n"
    "Fly + L,3/18/2026,4/15/2026,28,,4.48,,77.04458599,3390,,,,,\n"
    "Pearlite 2 A,4/19/2026,4/20/2026,1,3.603,3.67,61.96242038,63.11464968,242,,,,,\n"
    "Pearlite 2 B,4/19/2026,4/26/2026,7,3.559,3.98,61.20573248,68.44585987,948,,,,"
    "Cylinder Volume,0.058148148\n"
    "Pearlite 2 C,4/19/2026,5/17/2026,28,,,0,0,,,,,Cube Volume,0.00462963\n"
    "Baseline Perlite,6/20/2026,7/18/2026,28,,3.89,,66.89808917,968,,,,,\n"
)

FLEXURAL = (
    "Mix Name,Date set,Date tested,Curing time (days),Dry Weight (lbs),"
    "Wet Weight (lbs(),Dry Density (pcf),Wet Density (pcf),"
    "Compressive Load (lbs),Flexural Strength (psi),,,,,,,,\n"
    "Pearlite 2 A,4/19/2026,4/20/2026,1,0.567,,66.99323077,,,,,,,,,,,\n"
    "Perline 6/20 A,6/20/2026,7/18/2026,28,0.533,0.597,62.976,70.53784615,2349,3306,,"
    "Break on side of flexural region,,,,,,\n"
    "Perline 6/20 B,6/20/2026,7/18/2026,28,0.564,0.63,66.63876923,74.43692308,,,,,,,,,"
    "Length,4.75\n"
    "Perline + Mesh 6/27 B,6/27/2026,7/25/2026,28,0.654,0.702,77.27261538,82.944,2456,,,"
    "Good break right in middle,,,,,,\n"
    ",,,,,,,,,,,,,,,,,\n"
)

# Both tables pasted side by side (the workbook's "import" tab).  Not
# supported: the two halves share the mix-name and date columns while carrying
# their own curing times, so their rows do not correspond.
SIDE_BY_SIDE = (
    "Flexural Data,,,,,,,,,,,Compressive Data,,,,,,,,,,\n"
    "Mix Name,Date set,Date tested,Curing time (days),Dry Weight (lbs),"
    "Wet Weight (lbs(),Dry Density (pcf),Wet Density (pcf),"
    "Compressive Load (lbs),Flexural Strength (psi),,"
    "Curing time (days),Dry Weight (lbs),Wet Weight (lbs(),Dry Density (pcf),"
    "Wet Density (pcf),Compressive Strength (psi),,,,,\n"
    "Pearlite 2 A,4/19/2026,4/20/2026,28,0.567,,#DIV/0!,,,,,"
    "1,,4.48,,77.04458599,3390,,,,,\n"
    "Perline 6/20 A,6/20/2026,7/18/2026,28,,0.597,,#DIV/0!,,,,"
    "7,,3.89,,66.89808917,968,,,,Cylinder Volume,0.058148148\n"
)

# A flexural sheet whose density formulas errored out, leaving only weights —
# exactly what the workbook's own exports do when a column is missing.
FLEXURAL_NO_DENSITY = (
    "Mix Name,Date set,Date tested,Curing time (days),Dry Weight (lbs),"
    "Wet Weight (lbs(),Dry Density (pcf),Wet Density (pcf),"
    "Compressive Load (lbs),Flexural Strength (psi)\n"
    "Pearlite 2 A,4/19/2026,4/20/2026,28,0.567,,#DIV/0!,,,\n"
    "Perline 6/20 A,6/20/2026,7/18/2026,28,,0.597,,#DIV/0!,,\n"
)


@pytest.fixture()
def db(tmp_path):
    d = MixDatabase(db_path=str(tmp_path / "test_mixes.db"))
    yield d
    d.close()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="\r\n")
    return str(p)


def _by_name(records, name, kind=None):
    hits = [r for r in records if r.batch_id == name
            and (kind is None or r.specimen_type == kind)]
    assert hits, f"no record for {name!r} ({kind})"
    return hits[0]


# ══════════════════════════════════════════════════════════════════════════
#  Layout detection — one sheet, the other, or both
# ══════════════════════════════════════════════════════════════════════════

def test_compressive_sheet_is_recognised(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE),
                        constants=CONSTS)
    assert res.layout == "compressive"
    assert all(r.specimen_type == "cylinder" for r in res.records)
    assert _by_name(res.records, "Fly + L").fc_psi == 3390.0


def test_flexural_sheet_is_recognised(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "f.csv", FLEXURAL), constants=CONSTS)
    assert res.layout == "flexural"
    assert all(r.specimen_type == "beam" for r in res.records)
    assert _by_name(res.records, "Perline 6/20 A").fr_psi == 3306.0


def test_two_tables_side_by_side_are_refused_not_half_read(tmp_path):
    """One test type per file.  Reading the flexural half and silently
    dropping the compressive one is the failure this guards against."""
    res = parse_lab_csv(_write(tmp_path, "b.csv", SIDE_BY_SIDE),
                        constants=CONSTS)
    assert res.layout == "unsupported"
    assert res.records == []
    assert any("two test tables side by side" in w for w in res.warnings)
    assert any("separate files" in w for w in res.warnings)


def test_side_by_side_is_caught_without_the_banner_row(tmp_path):
    """Even with the banner stripped, a second copy of a measurement column
    gives the layout away."""
    text = "\n".join(SIDE_BY_SIDE.split("\n")[1:])
    res = parse_lab_csv(_write(tmp_path, "b.csv", text), constants=CONSTS)
    assert res.layout == "unsupported"
    assert any("two copies of" in w for w in res.warnings)


def test_suite_own_export_still_reads(db, tmp_path):
    db.add_lab_result("2026-07-01", 60.0, 600.0, 120.0, batch_id="A")
    db.add_lab_result("2026-07-02", 70.0, 900.0, 180.0, batch_id="B")
    path = str(tmp_path / "export.csv")
    db.export_lab_results_csv(path)
    res = parse_lab_csv(path, constants=CONSTS)
    assert res.layout == "plain"
    assert {r.density_pcf for r in res.records} == {60.0, 70.0}


# ══════════════════════════════════════════════════════════════════════════
#  The cells the spreadsheet left to a formula
# ══════════════════════════════════════════════════════════════════════════

def test_density_computed_from_weight_when_the_sheet_left_it_blank(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "f.csv", FLEXURAL_NO_DENSITY),
                        constants=CONSTS)
    # Dry density reads #DIV/0!, but the dry weight is there: 0.567 lb beam
    rec = _by_name(res.records, "Pearlite 2 A")
    assert rec.dry_density_pcf == pytest.approx(66.993, abs=0.01)
    assert rec.density_pcf == rec.dry_density_pcf
    assert "computed" in rec.notes
    # And a row with only a wet weighing falls back to that
    wet = _by_name(res.records, "Perline 6/20 A")
    assert wet.dry_density_pcf == 0.0
    assert wet.density_pcf == pytest.approx(0.597 / CONSTS.beam_volume_ft3)


def test_a_measured_density_is_never_recomputed(tmp_path):
    """The sheet's own number wins over anything derived from the weight."""
    res = parse_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE),
                        constants=CONSTS)
    rec = _by_name(res.records, "Pearlite 2 A")
    assert rec.dry_density_pcf == pytest.approx(61.96242038)
    assert "computed" not in rec.notes


def test_flexural_strength_computed_from_break_load(tmp_path):
    """f_r = P*L/(b*d^2) — the same formula the team's own sheet uses."""
    res = parse_lab_csv(_write(tmp_path, "f.csv", FLEXURAL), constants=CONSTS)
    rec = _by_name(res.records, "Perline + Mesh 6/27 B")
    assert rec.fr_psi == pytest.approx(2456 * 4.75 / (1.5 * 1.5 ** 2), rel=1e-9)


def test_recorded_flexural_strength_is_never_overwritten(tmp_path):
    """The row with BOTH a load and a strength keeps the strength as written —
    and the sheet's own 2349 lb -> 3306 psi confirms the formula."""
    res = parse_lab_csv(_write(tmp_path, "f.csv", FLEXURAL), constants=CONSTS)
    rec = _by_name(res.records, "Perline 6/20 A")
    assert rec.fr_psi == 3306.0
    assert rec.fr_psi == pytest.approx(2349 * 4.75 / (1.5 * 1.5 ** 2), rel=1e-3)


def test_constants_in_the_sheet_override_the_defaults(tmp_path):
    text = FLEXURAL.replace("Length,4.75", "Length,9.5")
    res = parse_lab_csv(_write(tmp_path, "f.csv", text), constants=CONSTS)
    rec = _by_name(res.records, "Perline + Mesh 6/27 B")
    assert rec.fr_psi == pytest.approx(2456 * 9.5 / (1.5 * 1.5 ** 2), rel=1e-9)


def test_excel_errors_and_zeros_are_missing_data_not_measurements(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE),
                        constants=CONSTS)
    # "Pearlite 2 C" has 0 densities and no strength: a test not yet run
    assert not [r for r in res.records if r.batch_id == "Pearlite 2 C"]
    assert res.invalid_rows == 0


def test_free_text_in_a_spare_column_becomes_the_row_note(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "f.csv", FLEXURAL), constants=CONSTS)
    assert "Break on side" in _by_name(res.records, "Perline 6/20 A").notes
    # ...while the constant LABELS beside it do not
    assert "Length" not in _by_name(res.records, "Perline 6/20 B").notes


def test_dates_normalise_to_iso_storage(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE),
                        constants=CONSTS)
    assert _by_name(res.records, "Fly + L").test_date == "2026-04-15"


def test_implausible_flexural_strength_is_flagged(tmp_path):
    res = parse_lab_csv(_write(tmp_path, "f.csv", FLEXURAL), constants=CONSTS)
    assert any("1500 psi" in w for w in res.warnings)


# ══════════════════════════════════════════════════════════════════════════
#  Into the database
# ══════════════════════════════════════════════════════════════════════════

def test_import_one_sheet_then_the_other(db, tmp_path):
    comp = _write(tmp_path, "c.csv", COMPRESSIVE)
    flex = _write(tmp_path, "f.csv", FLEXURAL)
    r1 = db.import_lab_csv(comp)
    assert (r1.layout, r1.added) == ("compressive", 4)
    r2 = db.import_lab_csv(flex)
    assert (r2.layout, r2.added) == ("flexural", 4)
    assert len(db.get_lab_results()) == 8


def test_importing_both_sheets_at_once_refits_once(db, tmp_path):
    paths = [_write(tmp_path, "c.csv", COMPRESSIVE),
             _write(tmp_path, "f.csv", FLEXURAL)]
    before = len(db.get_regression_versions())
    reports = db.import_lab_csv_files(paths)
    assert [r.layout for r in reports] == ["compressive", "flexural"]
    assert len(db.get_regression_versions()) == before + 1


def test_reimporting_the_same_sheet_changes_nothing(db, tmp_path):
    path = _write(tmp_path, "c.csv", COMPRESSIVE)
    first = db.import_lab_csv(path)
    again = db.import_lab_csv(path)
    assert (again.added, again.updated) == (0, 0)
    assert again.skipped == first.added
    assert len(db.get_lab_results()) == first.added


def test_a_later_sheet_completes_a_break_instead_of_duplicating_it(db, tmp_path):
    """The same specimen, first logged without its flexural number."""
    db.add_lab_result("2026-07-18", 62.976, batch_id="Perline 6/20 A",
                      curing_days=28, specimen_type="beam",
                      dry_density_pcf=62.976)
    rep = db.import_lab_csv(_write(tmp_path, "f.csv", FLEXURAL))
    assert rep.updated == 1
    row = next(r for r in db.get_lab_results()
               if r["batch_id"] == "Perline 6/20 A")
    assert row["fr_psi"] == 3306.0
    assert len([r for r in db.get_lab_results()
                if r["batch_id"] == "Perline 6/20 A"]) == 1


def test_a_contradicting_result_is_kept_as_its_own_row(db, tmp_path):
    db.add_lab_result("2026-07-18", 62.976, fr_psi=800.0,
                      batch_id="Perline 6/20 A", curing_days=28,
                      specimen_type="beam", dry_density_pcf=62.976)
    rep = db.import_lab_csv(_write(tmp_path, "f.csv", FLEXURAL))
    assert rep.updated == 0
    assert any("different strength" in w for w in rep.warnings)
    assert len([r for r in db.get_lab_results()
                if r["batch_id"] == "Perline 6/20 A"]) == 2


def test_the_same_numbers_under_a_new_name_are_flagged(db, tmp_path):
    """A mix renamed between exports logs the same break twice — say so."""
    db.import_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE))
    renamed = COMPRESSIVE.replace("Fly + L", "Fly Ash + Lime")
    rep = db.import_lab_csv(_write(tmp_path, "c2.csv", renamed))
    assert any("different mix name" in w for w in rep.warnings)


def test_a_refused_file_does_not_stop_the_others(db, tmp_path):
    """Selecting the workbook's combined tab by mistake must not cost you the
    two real sheets picked alongside it."""
    reports = db.import_lab_csv_files([
        _write(tmp_path, "b.csv", SIDE_BY_SIDE),
        _write(tmp_path, "c.csv", COMPRESSIVE),
        _write(tmp_path, "f.csv", FLEXURAL)])
    assert [r.imported for r in reports] == [False, True, True]
    assert reports[0].added == 0
    assert len(db.get_lab_results()) == reports[1].added + reports[2].added


def test_round_trip_through_the_suites_own_export(db, tmp_path):
    db.import_lab_csv_files([_write(tmp_path, "c.csv", COMPRESSIVE),
                             _write(tmp_path, "f.csv", FLEXURAL)])
    path = str(tmp_path / "export.csv")
    n = db.export_lab_results_csv(path)
    mate = MixDatabase(db_path=str(tmp_path / "mate.db"))
    try:
        rep = mate.import_lab_csv(path)
        assert rep.added == n
        # A mix name can appear twice — one beam, one cylinder — so the key
        # has to carry the specimen too.
        def _key(r):
            return (r["batch_id"], r["specimen_type"], r["test_date"])
        rows = {_key(r): r for r in mate.get_lab_results()}
        src  = {_key(r): r for r in db.get_lab_results()}
        assert set(rows) == set(src)
        for key, row in rows.items():
            for f in ("curing_days", "specimen_type", "density_pcf",
                      "dry_density_pcf", "wet_density_pcf", "fr_psi"):
                assert row[f] == src[key][f], f"{key}.{f}"
        assert mate.import_lab_csv(path).added == 0
    finally:
        mate.close()


# ══════════════════════════════════════════════════════════════════════════
#  What the imported data does to the regression
# ══════════════════════════════════════════════════════════════════════════

def test_flexural_only_rows_do_not_drag_the_compressive_fit(db, tmp_path):
    """A beam has no cylinder strength; its fc = 0 is missing data, and
    counting it as a measurement would pull the whole f'c line to the floor."""
    db.add_lab_result("2026-06-01", 60.0, fc_psi=600.0, ft_psi=120.0,
                      curing_days=28, batch_id="cyl-1")
    db.add_lab_result("2026-06-02", 80.0, fc_psi=1000.0, ft_psi=200.0,
                      curing_days=28, batch_id="cyl-2")
    baseline = db.get_mix_properties(70.0, fos=1.0)["strength_c"]
    db.import_lab_csv(_write(tmp_path, "f.csv", FLEXURAL))
    assert db.get_mix_properties(70.0, fos=1.0)["strength_c"] == \
        pytest.approx(baseline)


def test_early_age_breaks_are_recorded_but_held_out_of_the_fit(db, tmp_path):
    db.import_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE))
    rows = {r["batch_id"]: r for r in db.get_lab_results()}
    assert rows["Pearlite 2 A"]["curing_days"] == 1
    assert db.fit_eligibility(rows["Pearlite 2 A"]) == "1-day break"
    assert db.fit_eligibility(rows["Pearlite 2 B"]) == "7-day break"
    assert db.fit_eligibility(rows["Fly + L"]) == ""
    fitted = {r["batch_id"] for r in db.get_fit_rows()}
    assert fitted == {"Fly + L", "Baseline Perlite"}


def test_the_compressive_fit_uses_only_the_design_age_breaks(db, tmp_path):
    db.import_lab_csv(_write(tmp_path, "c.csv", COMPRESSIVE))
    # Fly + L (77.04 pcf, 3390 psi) and Baseline Perlite (66.90, 968) —
    # the 1-day and 7-day cylinders must not be on the line.
    slope = (3390.0 - 968.0) / (77.04458599 - 66.89808917)
    assert db.fc_regression is not None
    assert float(db.fc_regression[0]) == pytest.approx(slope, rel=1e-6)
