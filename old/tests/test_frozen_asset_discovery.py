# tests/test_frozen_asset_discovery.py
"""
TWO BUGS THE FROZEN BINARY FOUND THAT NOTHING ELSE COULD.

Both were invisible from source, because from source the working directory
happens to be the repo and every file is simply there.

1. THE HULL OFFSETS WERE NEVER FOUND IN A FROZEN RUN.
   `filepath_matrix = os.path.join(cwd, "off1.mlt")` -- and a frozen run's cwd
   is wherever the user launched the exe.  The fallback generator then produced
   a DEFAULT hull, silently: the sweep analysed a 20.0 ft generic boat instead
   of the team's 20.0098 ft one and reported a 5,814 lb winner.  Nothing said
   the geometry had been invented.

2. A GATE PROMOTION CRASHED THE REPORT.
   The gate pool holds LEAN records (13 fields, no geometry) on purpose.  When
   the gate promotes one -- the screen winner did not survive refinement and a
   heavier candidate did -- that record became `global_best_config`, and the
   report path did `global_best_config['x']`:

       KeyError: 'x'

   An entire completed sweep lost at the report step, on the one path that
   exists because the gate did its job.  No test covered promotion FOLLOWED BY
   reporting.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

import main


# ── 1. asset discovery ─────────────────────────────────────────────────────

def test_the_hull_search_has_a_real_order():
    paths = main.hull_offsets_path()
    assert paths, "no candidates at all"
    assert all(p.endswith("off1.mlt") for p in paths)
    assert all(os.path.isabs(p) for p in paths), \
        "a relative candidate would move with the working directory -- which " \
        "is exactly the bug"
    assert len(paths) == len({os.path.normcase(p) for p in paths})


def test_a_frozen_build_looks_beside_the_executable_first(monkeypatch,
                                                          tmp_path):
    """
    The editable copy must win.  A bundled default that outranked the user's
    own offsets would analyse the wrong boat just as silently as before.
    """
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "jaguar.exe"),
                        raising=False)
    paths = main.hull_offsets_path()
    assert os.path.normcase(paths[0]) == os.path.normcase(
        str(tmp_path / "off1.mlt"))
    assert len(paths) >= 2, "the frozen search dropped its fallbacks"


def test_the_repo_copy_is_still_found_unfrozen():
    paths = main.hull_offsets_path()
    assert any(os.path.exists(p) for p in paths), (
        "off1.mlt is no longer discoverable from source; the search order "
        "regressed")


# ── 2. gate promotion ──────────────────────────────────────────────────────

@pytest.fixture
def lean_record():
    """A lean gate record, exactly the shape the pool stores."""
    return {
        "thickness": 0.75,
        "depth": 14.07035257349158,
        "mixes": [72.0, 70.0, 72.0],
        "pour_fracs": (0.2843, 0.7107),
        "multizone": True,
        "weight_lbs": 266.9,
        "kupfer_dc": 0.28,
        "pos_var_ft": 0.0,
        "enable_mesh": False,
        "gw_width_mid": 0.0, "gw_height_mid": 0.0, "gw_fillet_ratio": 1.0,
        "seats": {k: np.linspace(6.0, 15.0, n) for k, n in
                  (("2M", 2), ("2F", 2), ("3M", 3), ("3F", 3), ("4_coed", 4))},
    }


def test_the_lean_record_really_is_missing_the_geometry(lean_record):
    """
    POSITIVE CONTROL.  If lean records started carrying geometry, every
    assertion below would pass while testing nothing -- and the bug it guards
    would be gone for a different reason than the one recorded here.
    """
    for k in ("x", "dx", "width_ft", "cum_area"):
        assert k not in lean_record, (
            f"the lean record now carries {k!r}; this test's premise is stale")


def test_rehydrating_produces_every_key_the_report_indexes(lean_record):
    if not any(os.path.exists(p) for p in main.hull_offsets_path()):
        pytest.skip("off1.mlt not available")
    path = next(p for p in main.hull_offsets_path() if os.path.exists(p))
    full = main.rehydrate_gate_winner(
        lean_record, path, 20.00984316, 4.5760260931, 161)
    assert full is not None
    # The keys the report path and _build_multizone_result actually index.
    for k in ("x", "dx", "width_ft", "cum_area", "cum_Az", "cum_Iz",
              "cum_Iy", "yhalf", "z_grid", "sheer_in", "na_ref",
              "coords_4", "coords_2", "coords_3"):
        assert full.get(k) is not None, f"rehydrated config is missing {k!r}"
    assert len(full["x"]) > 1 and full["dx"] > 0.0
    assert np.all(np.isfinite(full["sheer_in"]))


def test_rehydrating_preserves_the_design_it_describes(lean_record):
    """The rebuild must not quietly become a DIFFERENT design."""
    if not any(os.path.exists(p) for p in main.hull_offsets_path()):
        pytest.skip("off1.mlt not available")
    path = next(p for p in main.hull_offsets_path() if os.path.exists(p))
    full = main.rehydrate_gate_winner(
        lean_record, path, 20.00984316, 4.5760260931, 161)
    for k in ("thickness", "depth", "mixes", "pour_fracs", "weight_lbs"):
        assert full[k] == lean_record[k], (
            f"{k} changed during rehydration: {lean_record[k]} -> {full[k]}")
    assert full["rehydrated_from_gate"] is True, (
        "a promoted winner must be marked as such; its provenance differs "
        "from a screen winner's")


def test_an_unreadable_hull_returns_None_rather_than_a_partial_config():
    """
    The caller RAISES on None.  Returning a half-built config instead would
    put the KeyError back, one field further along.
    """
    out = main.rehydrate_gate_winner(
        {"thickness": 0.75, "depth": 14.07},
        "no_such_offsets_file.mlt", 20.0, 4.5, 161)
    assert out is None
