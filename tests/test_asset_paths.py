# tests/test_asset_paths.py
"""
ONE RESOLVER, AND A STARTUP CHECK THAT SAYS WHERE EVERYTHING CAME FROM.

Five shipped bugs, one defect: an asset resolved relative to something a frozen
build does not have.

    canoe_suite.cfg   -> every frozen run used built-in defaults
    concrete_mixes.db -> sqlite CREATED an empty one; 0 of 20 lab results
    off1.mlt          -> a DEFAULT hull; a generic 20.0 ft boat, 5814 lb
    out.mlt           -> a 35-byte stub set length and draft, and so the
                         waterline, the depth floor and the whole search grid
    reports_dir       -> PDFs written INSIDE the application bundle

All five silent, all five plausible, four alive for months. The proximate cause
was a variable called `cwd` holding the MODULE directory, which under
PyInstaller is `_internal`. The logic was reimplemented per file, and each
reimplementation was wrong the same way.

These tests pin the resolver, the ORDER (a user's own file must beat the
bundled one), and that the startup audit reports a miss rather than letting it
be inferred from a weight three hours later.
"""
from __future__ import annotations

import io
import os

import pytest

import asset_paths


# ── the search order ───────────────────────────────────────────────────────

def test_candidates_are_absolute_and_deduped():
    for name, _why in asset_paths.EXPECTED_ASSETS:
        paths = asset_paths.asset_search_paths(name)
        assert paths, f"no candidates for {name}"
        assert all(os.path.isabs(p) for p in paths), (
            "a relative candidate would move with the working directory -- "
            "which is the bug this module exists to prevent")
        assert all(p.endswith(name) for p in paths)
        assert len(paths) == len({os.path.normcase(p) for p in paths})


def test_a_frozen_build_prefers_the_copy_beside_the_executable(monkeypatch,
                                                               tmp_path):
    """
    THE ORDER IS THE WHOLE POINT.  A bundled default that outranked the user's
    own file would be the same defect in new clothes: geometry and settings
    that look applied and are not.
    """
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "jaguar.exe"),
                        raising=False)
    paths = asset_paths.asset_search_paths("off1.mlt")
    assert os.path.normcase(paths[0]) == os.path.normcase(
        str(tmp_path / "off1.mlt"))
    assert len(paths) >= 2, "the frozen search dropped its fallbacks"


def test_the_bundled_copy_is_LAST(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "jaguar.exe"),
                        raising=False)
    paths = asset_paths.asset_search_paths("canoe_suite.cfg")
    module_dir = os.path.dirname(os.path.abspath(asset_paths.__file__))
    assert os.path.normcase(paths[-1]) == os.path.normcase(
        os.path.join(module_dir, "canoe_suite.cfg"))


def test_resolve_returns_None_rather_than_a_plausible_guess(tmp_path,
                                                            monkeypatch):
    """
    A path that merely LOOKS right is how a 35-byte invented out.mlt came to
    define a hull's length.  Callers that fabricate a fallback must do it
    knowingly.
    """
    monkeypatch.chdir(tmp_path)
    assert asset_paths.resolve_asset("definitely_not_a_real_asset.xyz") is None


# ── the startup audit ──────────────────────────────────────────────────────

def test_every_expected_asset_resolves_in_the_repo():
    rows = asset_paths.audit_assets()
    missing = [n for n, _w, p, _c in rows if p is None]
    assert not missing, (
        f"these assets no longer resolve from source: {missing}")
    assert len(rows) == len(asset_paths.EXPECTED_ASSETS)


def test_the_audit_names_a_missing_file_and_where_it_looked(monkeypatch,
                                                            tmp_path):
    """
    POSITIVE CONTROL.  If the audit could not report a miss, it would print a
    reassuring list on every run while the same five bugs happened again.
    """
    monkeypatch.setattr(asset_paths, "EXPECTED_ASSETS",
                        (("nonexistent_asset.dat", "a thing that matters"),))
    buf = io.StringIO()
    n_missing = asset_paths.print_asset_audit(stream=buf)
    out = buf.getvalue()
    assert n_missing == 1
    assert "NOT FOUND" in out
    assert "nonexistent_asset.dat" in out
    assert "a thing that matters" in out, \
        "the audit does not say what the missing file supplies"
    # and it must show the places it looked, or the reader cannot act on it
    for cand in asset_paths.asset_search_paths("nonexistent_asset.dat"):
        assert cand in out


def test_a_clean_audit_reports_zero_missing():
    buf = io.StringIO()
    assert asset_paths.print_asset_audit(stream=buf) == 0
    assert "NOT FOUND" not in buf.getvalue()


def test_the_audit_covers_the_files_that_actually_broke():
    """
    Every asset behind a shipped bug must be in the checked set -- otherwise
    the check is decoration.
    """
    names = {n for n, _w in asset_paths.EXPECTED_ASSETS}
    for n in ("canoe_suite.cfg", "concrete_mixes.db", "off1.mlt", "out.mlt"):
        assert n in names, f"{n} caused a shipped bug and is not audited"


# ── no more private copies of this logic ───────────────────────────────────

def test_main_delegates_rather_than_reimplementing():
    import main
    assert main.hull_offsets_path("off1.mlt") == \
        asset_paths.asset_search_paths("off1.mlt"), \
        "main has its own copy of the search again; five bugs came from five copies"


@pytest.mark.parametrize("rel", [
    "app.py", "ui/main_window.py", "ui/manual_hull_tab.py",
    "reporting/latex_report.py",
])
def test_no_module_resolves_a_data_file_against_its_own_folder(rel):
    """
    The pattern that caused this four times:
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "<data file>")
    Using the module folder for a DIALOG's starting directory is fine; using it
    to locate data is the bug.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, rel), encoding="utf-8").read()
    for asset in ("off1.mlt", "out.mlt", "in.mlt", "canoe_suite.cfg",
                  "concrete_mixes.db"):
        for line in src.splitlines():
            if asset in line and "__file__" in line:
                raise AssertionError(
                    f"{rel} resolves {asset} against its own module folder:\n"
                    f"    {line.strip()}\n"
                    f"  In a frozen build that is `_internal`. Use "
                    f"asset_paths.resolve_asset().")
