"""
The process-pool safety net.

Under the thread executor the workers share the parent's `config` singleton, so
nothing here can fail.  Under the PROCESS executor each spawned child
re-imports `config` fresh and sees canoe_suite.cfg DEFAULTS.  Attributes that
only exist at run time (LONGITUDINAL_STATIONS) would raise; the dangerous ones
are those WITH import-time defaults -- FACTOR_OF_SAFETY, MAX_DENSITY_PCF,
DESIGN_METHOD, RACE_CATEGORIES, the gate limits -- because a worker missing
those returns a perfectly plausible weight for a design nobody asked for.

These tests are about that failure being impossible to reach silently.
"""
import pickle

import pytest

import config
import config.run_state as run_state_mod
from config.run_state import (RunState, RUN_LEVEL_CONFIG_ATTRS, ensure_applied,
                              _mark_active)


# -- the captured set --------------------------------------------------------

def test_capture_covers_every_attribute_the_service_writes():
    """
    Whatever StructuralService assigns onto `config` for a run must be in the
    captured set, or a process worker silently runs on the file default.
    """
    import inspect
    import re
    import services.structural_service as ss

    written = set(re.findall(r"(?:_cfg|config)\.([A-Z][A-Z0-9_]+)\s*=",
                             inspect.getsource(ss)))
    missing = sorted(written - set(RUN_LEVEL_CONFIG_ATTRS))
    assert not missing, (
        f"structural_service writes {missing} for a run but RunState does not "
        f"capture them; a process worker would use the canoe_suite.cfg default "
        f"and return a plausible number for the wrong design")


def test_derived_crew_aliases_are_never_captured():
    """
    RACE_4/2/3_PERSON are derived from RACE_CATEGORIES on read.  Capturing one
    would ship a snapshot that then shadows the derivation in the child -- the
    crew-weight split-brain, rebuilt across a process boundary.
    """
    derived = set(getattr(config, "_DERIVED_CREW_ALIASES", ()))
    assert derived, "the derived-alias registry vanished; this test is now blind"
    assert not (derived & set(RUN_LEVEL_CONFIG_ATTRS))


def test_get_mix_properties_is_not_shipped():
    """
    It is a module function -- picklable by reference in production, but NOT
    when a test monkeypatches it with a lambda.  The child rebinds config's own
    function instead of receiving one.
    """
    st = RunState.capture()
    assert "get_mix_properties" not in st.values


# -- the fingerprint ---------------------------------------------------------

def test_roundtrips_through_pickle_unchanged():
    st = RunState.capture()
    back = pickle.loads(pickle.dumps(st))
    assert back.fingerprint == st.fingerprint
    assert back.compute_fingerprint() == st.fingerprint


def test_fingerprint_is_float_exact():
    """
    A formatting-lossy fingerprint would miss exactly the small config
    differences most likely to move a winner.
    """
    a = RunState.capture()
    config.FACTOR_OF_SAFETY = float(config.FACTOR_OF_SAFETY) * (1 + 1e-15)
    b = RunState.capture()
    assert a.fingerprint != b.fingerprint


@pytest.mark.parametrize("attr,value", [
    ("FACTOR_OF_SAFETY", 99.0),
    ("DESIGN_METHOD", "ASD-but-wrong"),
    ("MAX_DENSITY_PCF", 12.5),
    ("MIN_GMT_IN", 7.25),
])
def test_verify_raises_when_the_config_drifts(attr, value):
    """This is THE assertion the whole refactor rests on."""
    st = RunState.capture()
    setattr(config, attr, value)
    with pytest.raises(RuntimeError, match="verification FAILED"):
        st.verify()


def test_verify_failure_names_the_attribute():
    st = RunState.capture()
    config.FACTOR_OF_SAFETY = 42.0
    with pytest.raises(RuntimeError, match="FACTOR_OF_SAFETY"):
        st.verify()


# -- applying ----------------------------------------------------------------

def test_apply_restores_a_drifted_config():
    st = RunState.capture()
    config.FACTOR_OF_SAFETY = 42.0
    config.MAX_DENSITY_PCF = 1.0
    st.apply()                                    # applies AND verifies
    assert config.FACTOR_OF_SAFETY == st.values["FACTOR_OF_SAFETY"]
    assert config.MAX_DENSITY_PCF == st.values["MAX_DENSITY_PCF"]


def test_apply_routes_race_categories_through_the_setter():
    """A bare setattr would leave the DERIVED per-size aliases stale."""
    st = RunState.capture()
    cats = {k: dict(v) for k, v in st.values["RACE_CATEGORIES"].items()}
    key = next(iter(cats))
    cats[key]["weights_lb"] = [x + 25.0 for x in cats[key]["weights_lb"]]
    config.set_race_categories(cats)
    st.apply()                                    # back to the captured crew
    assert (config.RACE_CATEGORIES[key]["weights_lb"]
            == st.values["RACE_CATEGORIES"][key]["weights_lb"])
    # ... and the derived aliases followed rather than keeping the +25 lb crew
    for alias in getattr(config, "_DERIVED_CREW_ALIASES", ()):
        assert getattr(config, alias) is not None


def test_absent_attributes_stay_absent():
    """
    LONGITUDINAL_STATIONS has no import-time default.  A child must see it
    ABSENT rather than inherit a stale value, so absence is itself captured.
    """
    had = hasattr(config, "LONGITUDINAL_STATIONS")
    saved = getattr(config, "LONGITUDINAL_STATIONS", None)
    try:
        if had:
            delattr(config, "LONGITUDINAL_STATIONS")
        st = RunState.capture()
        config.LONGITUDINAL_STATIONS = 999
        st.apply()
        assert not hasattr(config, "LONGITUDINAL_STATIONS")
    finally:
        if had:
            config.LONGITUDINAL_STATIONS = saved


def test_absent_attributes_stay_absent_ACROSS_A_PICKLE():
    """The same guarantee where it actually has to hold: in a spawned worker.

    The in-process test above passed throughout the bug that motivated this
    one.  ``apply()`` recognises absence with ``is``, and the sentinel used to
    be a plain string — which pickle reconstructs as an equal-but-DISTINCT
    object.  In the parent the identity held; in a child it did not, so
    ``config.LONGITUDINAL_STATIONS`` was SET to the sentinel text instead of
    being deleted.  Nothing raised: parse_godzilla_offsets did
    ``int(n_stations)`` on it, the ValueError went into a blanket except, and
    the CLI sweep skipped all 504 configurations as "offsets could not be
    parsed at this mold depth".

    Anything that survives a pickle round-trip here survives spawn.
    """
    had = hasattr(config, "LONGITUDINAL_STATIONS")
    saved = getattr(config, "LONGITUDINAL_STATIONS", None)
    try:
        if had:
            delattr(config, "LONGITUDINAL_STATIONS")
        st = pickle.loads(pickle.dumps(RunState.capture()))
        config.LONGITUDINAL_STATIONS = 999          # the stale child value
        st.apply()
        assert not hasattr(config, "LONGITUDINAL_STATIONS"), (
            "an absent attribute came back as "
            f"{getattr(config, 'LONGITUDINAL_STATIONS', None)!r} after a "
            "pickle round-trip — the absence sentinel lost its identity")
    finally:
        if had:
            config.LONGITUDINAL_STATIONS = saved
        elif hasattr(config, "LONGITUDINAL_STATIONS"):
            delattr(config, "LONGITUDINAL_STATIONS")


def test_absence_sentinel_survives_pickle_by_identity():
    """The property the whole mechanism rests on, tested directly."""
    from config.run_state import _MISSING
    assert pickle.loads(pickle.dumps(_MISSING)) is _MISSING


# -- ensure_applied: one call, both executors --------------------------------

def test_ensure_applied_is_verify_only_in_the_capturing_process(monkeypatch):
    """
    Under threads the singleton IS the parent's, so re-applying is at best
    redundant and at worst a race (config._mix_db = None under a sibling
    thread's feet).  capture_for_run() stamps the process so it only verifies.
    """
    st = RunState.capture_for_run()
    called = []
    monkeypatch.setattr(RunState, "apply", lambda self: called.append(1))
    ensure_applied(st)
    assert called == [], "ensure_applied re-applied inside the capturing process"


def test_ensure_applied_applies_in_a_fresh_process():
    """A spawned child starts with no stamp, so it must apply."""
    st = RunState.capture_for_run()
    _mark_active(None)                            # simulate a fresh child
    config.FACTOR_OF_SAFETY = 42.0
    ensure_applied(st)
    assert config.FACTOR_OF_SAFETY == st.values["FACTOR_OF_SAFETY"]
    assert run_state_mod._ACTIVE_FINGERPRINT == st.fingerprint


def test_ensure_applied_tolerates_none():
    ensure_applied(None)


# -- the worker args tuple ---------------------------------------------------

def test_short_worker_args_are_padded_not_rejected():
    """
    The tuple has grown twice (RunState, then the tally proxy).  Direct callers
    -- several tests build it by hand -- pass the shorter form and must keep
    working.
    """
    from main import _pad_worker_args
    assert _pad_worker_args(tuple(range(15)), 17) == tuple(range(15)) + (None, None)
    assert _pad_worker_args(tuple(range(17)), 17) == tuple(range(17))


def test_over_long_worker_args_raise():
    """Padding hides a SHORT tuple on purpose; a long one is a real mismatch."""
    from main import _pad_worker_args
    with pytest.raises(ValueError, match="expected <="):
        _pad_worker_args(tuple(range(18)), 17)


# -- the tally across a process boundary -------------------------------------

def test_sweep_tally_survives_pickling():
    """
    Under processes the tally is hosted in a manager and repatriated as a
    snapshot.  If it could not pickle, the gate table would come back empty --
    silently, since nothing downstream distinguishes "no rejections" from "the
    rejections went into a copy in a dead child".
    """
    from structural import SweepTally
    t = SweepTally({"depth_in": "1.5 - 6.0"})
    t.note("pre-sweep context")
    back = pickle.loads(pickle.dumps(t))
    assert back.search_space == t.search_space
    assert back.notes == ["pre-sweep context"]
    back.note("still usable")                     # the lock was rebuilt
    assert len(back.notes) == 2


def test_sweep_tally_snapshot_is_detached():
    from structural import SweepTally
    t = SweepTally({})
    snap = t.snapshot()
    t.note("added after the snapshot")
    assert snap.notes == []


# -- the executor knobs must not be able to change a result ------------------

def test_executor_choice_is_not_run_level():
    """
    SWEEP_EXECUTOR / SWEEP_MAX_WORKERS select HOW the sweep runs, never WHAT it
    computes.  If either were ever captured, that would mean it can change a
    number -- which is the bug, not the fix.
    """
    assert "SWEEP_EXECUTOR" not in RUN_LEVEL_CONFIG_ATTRS
    assert "SWEEP_MAX_WORKERS" not in RUN_LEVEL_CONFIG_ATTRS


def test_executor_choice_comes_from_canoe_suite_cfg():
    """
    SWEEP_EXECUTOR must be READ FROM THE CFG, not hardcoded in Python.

    It shipped as a bare module constant first, which meant setting
    structural.sweep_executor in canoe_suite.cfg did exactly nothing -- the
    quietest possible failure, since the sweep still ran and still produced the
    right answer, just on the executor nobody asked for.
    """
    import tempfile, os, yaml
    from config.settings import Settings

    for want in ("thread", "process"):
        fh = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False)
        yaml.safe_dump({"structural": {"sweep_executor": want}}, fh)
        fh.close()
        try:
            assert Settings.load(fh.name).structural.sweep_executor == want
        finally:
            os.unlink(fh.name)


@pytest.mark.parametrize("bad", ["processes", "fork", "threads", "gpu"])
def test_an_unknown_executor_is_rejected_not_silently_defaulted(bad):
    """
    Falling back to threads on a typo would let someone who asked for processes
    wait out a thread run believing otherwise.
    """
    import tempfile, os, yaml
    from config.settings import Settings

    fh = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False)
    yaml.safe_dump({"structural": {"sweep_executor": bad}}, fh)
    fh.close()
    try:
        with pytest.raises(ValueError, match="sweep_executor"):
            Settings.load(fh.name)
    finally:
        os.unlink(fh.name)


def test_the_live_executor_setting_is_valid():
    """Whatever canoe_suite.cfg currently says must be one the sweep supports."""
    assert config.SWEEP_EXECUTOR in ("thread", "process")
