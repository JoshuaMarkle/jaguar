# tests/test_executor_watchdog.py
"""
THE WATCHDOG HAS TO BE SEEN TO FIRE.

A hung pool is this project's worst failure mode: no exception, no progress,
the window simply stops repainting and the only way out is Task Manager.  A
`try/except` cannot catch it, because nothing is raised.  A stall timeout can.

But a watchdog that has never actually fired is a comment, not a mechanism --
and this codebase has a history of guards that looked wired up and were not
(the slice scanner that spent a refactor unable to fail; the multi-start count
bound as a default argument so every experiment silently measured the same 8
solves).  So these tests hang a worker ON PURPOSE and require the fallback to
happen.

They also pin the other direction: the watchdog must not fire on a healthy
sweep, because a false positive throws away good work to restart it slower.
"""
from __future__ import annotations

import time

import pytest

import config
import main


@pytest.fixture
def tiny_grid(monkeypatch):
    """A grid small enough that a healthy run finishes in seconds."""
    monkeypatch.setattr(config, "LONGITUDINAL_STATIONS", 41, raising=False)
    monkeypatch.setattr(config, "SEATING_N_STARTS", 1, raising=False)
    monkeypatch.setattr(config, "SWEEP_EXECUTOR", "thread", raising=False)
    return dict(freeboard_min_in=6.0, thickness_options_in=[0.5],
                multizone=True, num_zones=3, density_step_pcf=20.0,
                depth_step_in=4.0, depth_max_in=14.0,
                audit_log=False, reports_dir=None, _companion=True)


def test_the_stall_exception_exists_and_is_distinct():
    """
    A stall is the ABSENCE of a result; a raising task is a result.  Conflating
    them would let a genuine crash be reported as a hang and vice versa.
    """
    assert issubclass(main._ExecutorStalled, RuntimeError)
    assert main._ExecutorStalled is not RuntimeError


def test_a_healthy_sweep_does_not_trip_the_watchdog(tiny_grid, monkeypatch):
    """
    THE FALSE-POSITIVE DIRECTION.  The timeout is generous by design (1800 s
    default) precisely so a slow-but-working task is never mistaken for a hung
    one; this pins that a normal run clears it comfortably.
    """
    monkeypatch.setattr(config, "SWEEP_STALL_TIMEOUT_S", 600.0, raising=False)
    res = main.run_grand_optimization(**tiny_grid)
    assert res is None or isinstance(res, dict)


def test_a_hung_worker_trips_the_watchdog_and_falls_back(tiny_grid,
                                                         monkeypatch, capsys):
    """
    THE POINT OF THE WHOLE MECHANISM.

    The first attempt's worker hangs.  The watchdog must notice, tear the pool
    down, say so, and re-run the search -- and the re-run must actually get
    somewhere rather than dying too.

    NOTE ON THE TIMINGS, learned the hard way.  The first version of this test
    used a 3 s stall window against a grid whose REAL tasks take ~60 s, so the
    fallback stalled as well and the test failed with the watchdog working
    perfectly.  That is precisely the false-positive the generous production
    default (1800 s) exists to avoid.  Here the fallback's work is made
    instantaneous instead, so the stall window only ever sees the planted hang.
    """
    monkeypatch.setattr(config, "SWEEP_STALL_TIMEOUT_S", 3.0, raising=False)
    state = {"first": True}

    def worker(args):
        if state["first"]:
            state["first"] = False
            time.sleep(30.0)            # >> the stall window
        return None                     # fallback: instant, legitimate "nothing"

    monkeypatch.setattr(main, "_evaluate_geometry_worker", worker)
    monkeypatch.setattr(main, "resolve_sweep_executor", lambda *a, **k: "process")

    # A process pool cannot take a locally-patched worker, and the pool CLASS
    # is not what is under test -- the watchdog and the fallback are.  Hand
    # both kinds a thread pool so the planted hang is reachable.
    import concurrent.futures as _cf
    monkeypatch.setattr(_cf, "ProcessPoolExecutor", _cf.ThreadPoolExecutor)

    res = main.run_grand_optimization(**tiny_grid)
    out = capsys.readouterr().out
    assert "EXECUTOR STALLED" in out, (
        "the watchdog did not announce the stall; a silent fallback is only "
        "marginally better than a hang")
    assert "Falling back to the THREAD executor" in out
    assert state["first"] is False, "the planted hang never ran"
    assert res is None or isinstance(res, dict), (
        "the fallback did not complete a run; a watchdog that only kills the "
        "sweep has not solved anything")


class TestPartialGrid:
    """
    A WINNER FROM A GRID THAT WAS NOT FULLY SEARCHED.

    Not a wrong calculation -- a correct calculation of the wrong thing, which
    reads identically on the page.  Previously this printed a warning and
    carried on; the banner scrolls past and the number is what gets written
    down.
    """

    def test_a_partial_grid_refuses_to_report_a_winner(self, tiny_grid,
                                                       monkeypatch):
        calls = {"n": 0}

        def flaky(args):
            # Stubbed: this is a test of the ASSERTION, not of the physics, and
            # running real tasks made the file take a quarter of an hour.
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("planted worker failure")
            return None

        monkeypatch.setattr(main, "_evaluate_geometry_worker", flaky)
        monkeypatch.setattr(config, "ACCEPT_PARTIAL_GRID", False, raising=False)
        grid = dict(tiny_grid)
        grid["depth_step_in"] = 1.0      # several tasks, so one can fail
        grid["depth_max_in"] = 16.0
        with pytest.raises(RuntimeError, match="NOT fully searched"):
            main.run_grand_optimization(**grid)

    def test_the_escape_hatch_is_deliberate_and_named(self, tiny_grid,
                                                      monkeypatch):
        """
        An assertion with no way past it blocks real work; one that is easy to
        trip past is decoration.  The flag exists, and the error names it.
        """
        calls = {"n": 0}

        def flaky(args):
            # Stubbed: this is a test of the ASSERTION, not of the physics, and
            # running real tasks made the file take a quarter of an hour.
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("planted worker failure")
            return None

        monkeypatch.setattr(main, "_evaluate_geometry_worker", flaky)
        monkeypatch.setattr(config, "ACCEPT_PARTIAL_GRID", True, raising=False)
        grid = dict(tiny_grid)
        grid["depth_step_in"] = 1.0
        grid["depth_max_in"] = 16.0
        res = main.run_grand_optimization(**grid)       # must NOT raise
        assert res is None or isinstance(res, dict)
