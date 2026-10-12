# tests/test_config_is_findable.py
"""
THE FROZEN BUILD NEVER LOADED ITS CONFIG FILE.

`Settings.load()` resolved `canoe_suite.cfg` relative to `config/settings.py`.
Under PyInstaller that is inside `_internal/` -- a directory the cfg was not
shipped to, and that nobody would think to edit.  So EVERY frozen run fell back
to built-in defaults: factor of safety, design method, density bounds, race
categories, station count.

It was caught by Tier 1: a frozen sweep reported a 3,251 lb hull at a 10.30 in
depth, below the freeboard floor real runs enforce.  The
`[!! CONFIG DEFAULTED]` banner had been announcing it on every single frozen
run, which is the only reason it was findable at all -- and is exactly why that
banner exists.
"""
from __future__ import annotations

import os

import config.settings as cs


def test_the_candidates_are_well_formed_and_deduped():
    """
    NOT a count assertion.  Run from the repo root, the cwd candidate and the
    source-relative one are the SAME directory, so a correct implementation
    collapses them to one -- the first version of this test demanded >= 2 and
    failed on correct behaviour.
    """
    paths = cs.config_search_paths()
    assert paths, "no candidates at all"
    assert all(p.endswith(cs._CFG_NAME) for p in paths)
    assert all(os.path.isabs(p) for p in paths), \
        "a relative candidate would move with the working directory"
    assert len(paths) == len({os.path.normcase(p) for p in paths}), \
        "duplicate candidates would make the 'tried' list misleading"


def test_a_frozen_build_looks_beside_the_executable_FIRST(monkeypatch):
    """
    The editable copy has to win.  A bundled default that outranked a user's
    own cfg would be the same defect in a new costume: settings that look
    applied and are not.
    """
    import sys
    import tempfile
    # A REAL directory, not a synthetic drive letter: os.path.abspath()
    # resolves "X:app" against the current directory of drive X:, so a fake
    # path silently becomes something else. That cost one debugging cycle.
    exe_dir = tempfile.mkdtemp()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", os.path.join(exe_dir, "jaguar.exe"),
                        raising=False)
    paths = cs.config_search_paths()
    assert os.path.normcase(paths[0]) == os.path.normcase(
        os.path.join(exe_dir, cs._CFG_NAME)), (
        f"first candidate is {paths[0]!r}, not the folder beside the exe")
    assert len(paths) >= 2, "the frozen search dropped its fallbacks"


def test_the_unfrozen_interpreter_still_finds_the_repo_copy():
    """The path that has always worked must keep working."""
    s = cs.Settings.load()
    assert s is not None
    assert cs.CONFIG_SOURCE.get("loaded") is True, (
        f"the repo's own config stopped loading: "
        f"{cs.CONFIG_SOURCE.get('reason')}")


def test_a_miss_names_every_place_it_looked(monkeypatch, tmp_path):
    """
    "No config file at <one path>" sent the reader to a directory that was
    never going to have it.  The message has to name the whole search.
    """
    monkeypatch.setattr(cs, "config_search_paths",
                        lambda: [str(tmp_path / "a" / cs._CFG_NAME),
                                 str(tmp_path / "b" / cs._CFG_NAME)])
    cs.Settings.load()
    assert cs.CONFIG_SOURCE["loaded"] is False
    reason = cs.CONFIG_SOURCE["reason"]
    assert "a" in reason and "b" in reason, (
        f"the failure names only part of the search: {reason}")


def test_the_config_ships_in_both_bundles():
    """
    A search path that points at a file nobody packaged is still a miss.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for spec in ("packaging/jaguar.spec", "packaging/jaguar_cli.spec"):
        src = open(os.path.join(root, spec), encoding="utf-8").read()
        assert "canoe_suite.cfg" in src, (
            f"{spec} does not ship canoe_suite.cfg; a fresh install would run "
            f"on built-in defaults")
