"""
Pytest configuration — adds the project root to sys.path so all modules
are importable from any working directory.

Also guards the committed tree: see `repo_safe_cwd` (redirects report output
into tmp_path) and `pytest_sessionfinish` (fails the run if the suite left the
working tree dirtier than it found it).
"""
import subprocess
import sys
import os

import pytest

# Project root is one level above this file
_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _ROOT)


# ── Run-level config isolation ───────────────────────────────────────────────
# StructuralService.run_sweep / run_grand_optimization write per-run parameters
# onto the module-level `config` singleton and never restore them, so a test
# that runs a sweep can leak a heavy load case, a restricted mix set, a severe
# turn, a GMT floor, etc. into a LATER test.  That was always latent; the unified
# Kupfer biaxial gate (stricter than the old independent checks, and dependent
# on the turn/load state) turns it into order-dependent failures ("0
# configurations survived").  This autouse fixture snapshots the mutable
# run-level attributes before each test and restores them after, making the
# whole suite order-independent.

# THE canonical list now lives in production (config/run_state.py) because
# the process-pool refactor needs it to build a picklable RunState.  It is
# imported, never restated: two copies would drift, and this list is what
# stops one test's sweep settings leaking into the next.
from config.run_state import RUN_LEVEL_CONFIG_ATTRS as _PROD_ATTRS

_RUN_LEVEL_CONFIG_ATTRS = _PROD_ATTRS + ("get_mix_properties",)
_CONFIG_MISSING = object()


@pytest.fixture(autouse=True)
def _isolate_run_level_config():
    import config
    # Snapshotting a DERIVED attribute would store it on restore and freeze it.
    _derived = set(getattr(config, "_DERIVED_CREW_ALIASES", ()))
    _bad = _derived & set(_RUN_LEVEL_CONFIG_ATTRS)
    assert not _bad, (
        f"_RUN_LEVEL_CONFIG_ATTRS lists derived attribute(s) {sorted(_bad)}; "
        f"snapshot/restore would turn them into stored values and re-create the "
        f"crew-weight split-brain. Snapshot their SOURCE instead.")
    snap = {a: getattr(config, a, _CONFIG_MISSING) for a in _RUN_LEVEL_CONFIG_ATTRS}
    try:
        yield
    finally:
        for a, v in snap.items():
            if v is _CONFIG_MISSING:
                if hasattr(config, a):
                    delattr(config, a)
            else:
                setattr(config, a, v)
        config._mix_db = None          # drop any mix-db regression a test published
        # A DERIVED alias must never end up STORED in config.__dict__.  The way
        # it happens is subtle: monkeypatch.setattr on RACE_4_PERSON reads the
        # old value THROUGH the PEP 562 __getattr__ (a derived list), then on
        # teardown writes it back with setattr -- storing it, where it shadows
        # the derivation for the rest of the session.
        #
        # test_load_case_expansion already asserts this, but only catches it if
        # it runs AFTER the polluter: the suite passed in forward order and
        # failed reversed. Checking here names the test that did it, whichever
        # order the files happen to run in.
        _stored = [a for a in getattr(config, "_DERIVED_CREW_ALIASES", ())
                   if a in config.__dict__]
        for _a in _stored:
            del config.__dict__[_a]
        assert not _stored, (
            f"this test left {_stored} STORED on the config module; they are "
            "derived from RACE_CATEGORIES on read and storing one re-creates "
            "the crew-weight split-brain. Patch RACE_CATEGORIES (the source of "
            "truth, and already restored here), never the alias.")


# ── Fixture validity: test data must pass the SAME validators production uses ─
#
# THE FAILURE MODE THIS CLOSES.  A fixture that builds data production would
# REJECT is a test running against data the system considers invalid -- it
# passes while production fails, and the suite is then not an independent
# check but an agreement with the bug.  It has happened twice here:
#
#   * seven fixtures across three files added lab rows with NO curing age.
#     Production excludes those from every fit; the tests were silently
#     relying on them being admitted, which was the same assumption the
#     production fit was making.
#   * test_stability asserted "degrade to zeros, not exceptions" -- encoding
#     the fabricated GM_T as the desired behaviour.
#
# So: run fixture-built lab rows through fit_eligibility, the production
# validator, and fail unless the row is admissible OR the test has explicitly
# opted in.  The MARKER LIST is the audited artefact -- an opt-in must say why,
# the same rule the guards' exemptions follow.

_INVALID_ROW_MARK = "invalid_lab_row"


@pytest.fixture(autouse=True)
def _fixture_rows_must_satisfy_production_validators(request):
    """
    Wrap MixDatabase.add_lab_result so every row a TEST creates is checked by
    the same predicate the production fit applies.

    Opt out with a REASON:

        @pytest.mark.invalid_lab_row("exercises the age-exclusion path")
    """
    marker = request.node.get_closest_marker(_INVALID_ROW_MARK)
    if marker is not None:
        reason = (marker.args[0] if marker.args else "")
        assert isinstance(reason, str) and len(reason.strip()) >= 15, (
            f"@pytest.mark.{_INVALID_ROW_MARK} needs a REASON of at least 15 "
            "characters saying why this test builds data production rejects; "
            "an unexplained opt-in is how an allow-list stops being reviewed.")
        yield
        return

    try:
        from mix_database import MixDatabase
    except Exception:
        yield
        return

    # NOT monkeypatch: requesting it here made pytest build the monkeypatch
    # fixture earlier, which flipped teardown order so _isolate_run_level_config
    # deleted a run-level attribute BEFORE monkeypatch's own undo tried to --
    # and the second delete raised AttributeError. A plain try/finally keeps
    # this fixture out of the ordering entirely.
    real = MixDatabase.add_lab_result

    def checked(self, *a, **kw):
        out = real(self, *a, **kw)
        try:
            rows = self.get_lab_results(limit=1)
        except Exception:
            return out
        if not rows:
            return out
        why = self.fit_eligibility(dict(rows[0]))
        assert not why, (
            f"this test added a lab row production REJECTS ({why}). It would "
            "pass here while the real fit ignores the row entirely -- the "
            "suite agreeing with a bug rather than catching it. Either give "
            "the row what production needs (e.g. curing_days), or mark the "
            f"test @pytest.mark.{_INVALID_ROW_MARK}('...') with a reason.")
        return out

    MixDatabase.add_lab_result = checked
    try:
        yield
    finally:
        MixDatabase.add_lab_result = real


# ── ONE shared sweep for the assertion-only tests ────────────────────────────
#
# test_bending_signs, test_freeboard and test_report each paid for a full
# production run_grand_optimization purely to obtain a result dict, then
# asserted on cheap, resolution-INDEPENDENT properties of it -- fibre sense,
# gate/report reconciliation, buoyancy balance, LaTeX path formatting.  Seventeen
# tests, 13 h 51 m.
#
# The sweep below is the same pipeline at a coarse resolution: 41 stations
# instead of the production grid, one thickness, a coarse density step.  It
# produces a real, FEASIBLE winner through the real code path -- the same
# evaluator, the same five load cases, the same gate -- so every assertion those
# tests make still means what it meant.  What it does not do is take hours.
#
# A test whose assertion genuinely depends on production resolution must NOT use
# this fixture; mark it @pytest.mark.production_resolution and take the runtime.

_SMALL_SWEEP_STATIONS = 41


@pytest.fixture(scope="session")
def small_sweep(tmp_path_factory):
    """
    ``(result_dict, reports_dir)`` from ONE coarse sweep, shared session-wide.

    Session-scoped on purpose: the cost is the sweep, and every consumer only
    reads the result.  Nothing may mutate the returned dict.
    """
    import os
    import config
    from main import run_grand_optimization

    out = tmp_path_factory.mktemp("small_sweep")
    # FIXED seating.  Measured: the SLSQP placement solve is 95% of a task
    # (26,717 objective evaluations, 180 s of a 189 s task at 41 stations), and
    # none of the seventeen assertions this fixture serves is about seat
    # placement -- they are about fibre sense, gate/report identity, buoyancy
    # balance and LaTeX path formatting.
    #
    # Optimizer mode is NOT lost from the suite: test_load_cases
    # (test_mesh_enabled_task_not_rejected), test_sweep_controls,
    # test_structural_sweep and test_paddler_jacobian all exercise it, which is
    # what caught the two index-slicing defects that lived there.  This fixture
    # does not need to pay for it a second time.
    saved = {a: getattr(config, a, None) for a in
             ("FIXED_PADDLER_POSITIONS", "PADDLER_POS_VARIANCE_FT", "MAX_MIXES",
              "N_MIXES_MAX", "MAX_DENSITY_PCF", "LONGITUDINAL_STATIONS")}
    cwd = os.getcwd()
    try:
        # Production-like env, matching the _real_env helper these tests used.
        config.FIXED_PADDLER_POSITIONS = {
            "2M": [6.0, 12.0], "2F": [6.0, 12.0],
            "3M": [5.0, 9.0, 13.0], "3F": [5.0, 9.0, 13.0],
            "4_coed": [3.0, 7.5, 12.0, 15.0]}
        config.PADDLER_POS_VARIANCE_FT = 0.0
        config.MAX_MIXES = 2
        config.N_MIXES_MAX = 2
        config.MAX_DENSITY_PCF = 80.0
        config.LONGITUDINAL_STATIONS = _SMALL_SWEEP_STATIONS
        os.chdir(out)                     # sweep writes some artefacts relative
        res = run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=True, num_zones=3, reports_dir=str(out),
            depth_step_in=2.0, depth_span_in=4.0, density_step_pcf=2.0)
    finally:
        os.chdir(cwd)
        for a, v in saved.items():
            setattr(config, a, v)
    assert isinstance(res, dict), (
        "the shared small sweep found no feasible design; the fixture grid is "
        "too tight to test anything and must be widened, not skipped")
    return res, str(out)


@pytest.fixture(scope="session")
def small_sweep_model(small_sweep):
    """``(report_model, result_dict)`` -- the built LaTeX model, built once."""
    from reporting.latex_report import build_report_model
    res, _out = small_sweep
    return build_report_model(res), res


# ── Keeping generated output out of the repo ─────────────────────────────────

@pytest.fixture
def repo_safe_cwd(tmp_path, monkeypatch):
    """
    Run the test with the CURRENT DIRECTORY inside tmp_path.

    Required by any test that completes a `run_grand_optimization` sweep.  That
    function resolves its INPUTS (off1.mlt / out.mlt) from `main.__file__` —
    absolute, so they still come from the repo — but writes its five ASCE_*.pdf
    reports to RELATIVE paths, i.e. the current directory.  Without this
    fixture those writes land on the committed PDFs in the repo tree.

    Returns the temp directory, so a test can assert on the emitted reports.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── Session guard: the suite must not mutate committed files ─────────────────

def _git_dirty():
    """
    Paths git reports as modified/untracked, or None when unavailable (not a
    git checkout, git missing) — in which case the guard stays quiet.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=_ROOT,
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {ln[3:].strip().strip('"')
            for ln in out.stdout.splitlines() if ln.strip()}


def pytest_configure(config):
    config._repo_dirty_before = _git_dirty()


def pytest_sessionfinish(session, exitstatus):
    """
    Fail the run if the suite dirtied files it did not find dirty.

    Tests must not write into the working tree: doing so silently edits
    committed engineering data (hull offsets, reports, design state), which
    then shows up in `git status` as if it were a real change and can be
    committed by accident.  Only NEW dirt counts — pre-existing local edits
    are ignored, so this is usable mid-development.
    """
    before = getattr(session.config, "_repo_dirty_before", None)
    if before is None:
        return                      # not a git checkout — nothing to compare
    after = _git_dirty()
    if after is None:
        return
    new = sorted(f for f in (after - before)
                 if not f.endswith((".pyc", "-shm", "-wal"))
                 and "__pycache__" not in f)
    if not new:
        return

    msg = ("The test suite mutated the working tree.  Tests must write to "
           "tmp_path (see the `repo_safe_cwd` fixture), never into the repo:\n"
           + "\n".join(f"    {f}" for f in new))
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "REPO MUTATED BY TESTS", red=True)
        reporter.write_line(msg)
    else:
        print(msg)
    session.exitstatus = 1
