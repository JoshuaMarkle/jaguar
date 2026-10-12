# tests/test_audit_log.py
"""
The sweep audit log must be able to tell two runs apart.

Every entry in the shipped sweep_audit.log carries input_hash eda79cfa41a0 --
thirty-odd runs spanning July and August, different winners, different weights,
one hash.  The six fields being hashed (length, draft, FoS, freeboard, max mix)
never changed, and the dimensions that DID change between runs -- longitudinal
station count, the density grid, the executor -- were not recorded at all.

That made the log useless for the one question it exists to answer: which run
produced this number, and under what search.  Reconstructing a 41 vs 161
station comparison took eleven shell commands and two recovered scratch
scripts, because the log could not distinguish the runs.

These tests pin the property, not the field list: runs that searched
differently must hash differently.
"""
from __future__ import annotations

import json

import pytest

from data.session import log_run, _hash_inputs


BASE = {
    "run_type": "structural_sweep",
    "length_ft": 20.0, "draft_m": 0.2, "fos": 1.5,
    "freeboard_in": 6.0, "mix_max_pcf": 80.0,
    "longitudinal_stations": 161,
    "mix_min_pcf": 60.0, "density_step_pcf": None,
    "n_mixes": 2, "num_zones": 3, "multizone": True,
    "existing_mixes_only": False, "precision_level": 1,
    "executor": "process", "code_version": "abc1234",
}


@pytest.mark.parametrize("field, other", [
    ("longitudinal_stations", 41),        # THE one that started this
    ("mix_min_pcf", 57.5),
    ("density_step_pcf", 2.0),
    ("n_mixes", 3),
    ("num_zones", None),
    ("multizone", False),
    ("existing_mixes_only", True),
    ("precision_level", 3),
    ("executor", "thread"),
    ("mix_max_pcf", 75.0),
    ("fos", 2.0),
])
def test_hash_changes_when_the_search_changes(field, other):
    """Two runs that searched different spaces must not share a hash."""
    variant = {**BASE, field: other}
    assert variant[field] != BASE[field], "the test data must actually differ"
    assert _hash_inputs(variant) != _hash_inputs(BASE), (
        f"input_hash is blind to {field!r}: a run at "
        f"{BASE[field]!r} and one at {other!r} log as the same run")


def test_hash_is_stable_for_an_identical_search():
    """It must still be a fingerprint -- same search, same hash."""
    assert _hash_inputs(dict(BASE)) == _hash_inputs(dict(reversed(
        list(BASE.items()))) | {})


def test_the_record_carries_the_search_dimensions(tmp_path):
    """Hashing them is not enough; a human has to be able to READ them back."""
    path = tmp_path / "audit.log"
    h = log_run("structural_sweep", BASE,
                {"best_weight_lbs": 132.66, "zone_densities": [72.5, 62.5, 72.5],
                 "kupfer_dc": 0.9230, "n_stations": 161},
                log_path=str(path))
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["input_hash"] == h
    for key in ("longitudinal_stations", "mix_min_pcf", "mix_max_pcf",
                "executor", "density_step_pcf"):
        assert key in rec["inputs"], f"{key} is not recorded in the log line"
    # and the winner itself, so the log says WHAT was chosen
    assert rec["result"]["zone_densities"] == [72.5, 62.5, 72.5]
    assert rec["result"]["n_stations"] == 161


def test_the_shipped_logs_blind_spot_is_actually_fixed():
    """
    The historical failure, reproduced: the OLD six-field record cannot tell a
    41-station run from a 161-station one.  The new one can.
    """
    old_fields = ("run_type", "length_ft", "draft_m", "fos",
                  "freeboard_in", "mix_max_pcf")
    old_41 = {k: BASE[k] for k in old_fields}
    old_161 = dict(old_41)                      # stations were never in here
    assert _hash_inputs(old_41) == _hash_inputs(old_161)   # the bug

    new_41 = {**BASE, "longitudinal_stations": 41}
    new_161 = {**BASE, "longitudinal_stations": 161}
    assert _hash_inputs(new_41) != _hash_inputs(new_161)   # the fix


# ══════════════════════════════════════════════════════════════════════════
#  The CLI/script path must log too
# ══════════════════════════════════════════════════════════════════════════

def test_the_engine_logs_its_own_run(monkeypatch):
    """
    run_grand_optimization never called log_run, so every sweep scripted
    against it -- which is how verification runs are driven -- left no trace
    at all.  The audit file held GUI runs only, and that is why a 41- vs
    161-station comparison had to be reconstructed from recovered scratch
    scripts instead of read out of the log.
    """
    import config, main

    seen = {}

    def _fake_log_run(run_type, inputs, summary, **kw):
        seen["run_type"] = run_type
        seen["inputs"] = inputs
        seen["summary"] = summary
        return "deadbeef1234"

    import data.session
    monkeypatch.setattr(data.session, "log_run", _fake_log_run)
    monkeypatch.setattr(config, "LONGITUDINAL_STATIONS", 161, raising=False)
    monkeypatch.setattr(config, "SWEEP_EXECUTOR", "process", raising=False)

    main._audit_run(
        {"L_ft": 20.0, "draft_in": 4.58, "weight_lbs": 132.66, "t_in": 0.375,
         "depth_in": 14.57, "zone_densities": [72.5, 62.5, 72.5],
         "pour_fracs": [0.2745, 0.6863], "kupfer_dc": 0.9230,
         "x": list(range(161)), "fit_override": None},
        freeboard_min_in=6.0, precision_level=1, density_step_pcf=None,
        num_zones=3, multizone=True, existing_only=False)

    assert seen, "the engine did not log its run"
    assert seen["inputs"]["longitudinal_stations"] == 161
    assert seen["inputs"]["executor"] == "process"
    assert seen["inputs"]["entry_point"] == "cli"
    assert seen["summary"]["zone_densities"] == [72.5, 62.5, 72.5]
    assert seen["summary"]["n_stations"] == 161


def test_the_gui_path_does_not_double_log():
    """The service suppresses the engine's record because the UI writes its
    own from the widgets.  Two entries for one sweep is its own kind of lie."""
    import inspect
    from services import structural_service
    src = inspect.getsource(structural_service.StructuralService.run_sweep)
    assert "audit_log=False" in src, (
        "the service must suppress the engine's audit record, or every GUI "
        "sweep lands in the log twice")
