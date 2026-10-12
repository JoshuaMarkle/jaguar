# tests/test_seating_starts.py
"""
The multi-start count must actually be tunable.

`seating_start_points` took `n_starts=SEATING_N_STARTS` as a DEFAULT ARGUMENT,
which Python evaluates once at import.  Assigning main.SEATING_N_STARTS or
config.SEATING_N_STARTS afterwards was a no-op, so every measurement that
"varied" the multi-start count compared 8 solves against 8 solves and reported
the difference as meaningful.  A test fixture also believed it had cut the count
to 2 and had not.

A tunable that silently ignores tuning is worse than a constant: it reports
success.  These tests pin that setting the knob changes the number of solves.
"""
from __future__ import annotations

import numpy as np
import pytest

import config
import main


# The real hull is ~20 ft.  At 18 ft the generator can only place three
# feasible 14-seat starts (five crews, >=3.5 ft within each), so a shorter L
# silently yields fewer starts than requested -- see
# test_short_hull_yields_fewer_starts_without_failing.
L = 20.0131


def test_default_start_count_matches_the_documented_value():
    starts = main.seating_start_points(L)
    assert len(starts) == main.SEATING_N_STARTS


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_explicit_n_starts_is_honoured(n):
    assert len(main.seating_start_points(L, n_starts=n)) == n


def test_short_hull_yields_fewer_starts_without_failing():
    """
    Documented degradation: when the geometry cannot fit another feasible
    seating, the generator stops early rather than raising or looping.  It must
    still return at least one start, and never more than asked.
    """
    got = main.seating_start_points(18.0, n_starts=8)
    assert 1 <= len(got) <= 8
    for v in got:
        assert len(v) == config.crew_seat_vector_len()


@pytest.mark.parametrize("n", [1, 2, 4])
def test_config_override_is_read_at_call_time(monkeypatch, n):
    """THE regression: a runtime assignment must take effect."""
    monkeypatch.setattr(config, "SEATING_N_STARTS", n, raising=False)
    assert len(main.seating_start_points(L)) == n


def test_module_level_assignment_is_read_at_call_time(monkeypatch):
    """
    main.SEATING_N_STARTS = k must work too -- that is what the fixture and
    every prior experiment used.
    """
    monkeypatch.setattr(main, "SEATING_N_STARTS", 3)
    assert len(main.seating_start_points(L)) == 3


def test_seed_is_also_read_at_call_time(monkeypatch):
    """Same bound-default bug; the sweep's reproducibility depends on it."""
    a = main.seating_start_points(L, n_starts=4)
    monkeypatch.setattr(config, "SEATING_START_SEED", 12345, raising=False)
    b = main.seating_start_points(L, n_starts=4)
    # element 0 is the deterministic hand-built start, so compare a random one
    assert not np.allclose(a[-1], b[-1]), "seed change had no effect"


def test_starts_are_reproducible_for_a_fixed_seed():
    """Bit-identical runs are a stated requirement of the sweep."""
    a = main.seating_start_points(L, n_starts=6, seed=99)
    b = main.seating_start_points(L, n_starts=6, seed=99)
    for u, v in zip(a, b):
        assert np.array_equal(u, v)


def test_every_start_is_the_full_seating_vector():
    for s in main.seating_start_points(L, n_starts=4):
        assert len(s) == config.crew_seat_vector_len()


def test_no_tunable_is_bound_as_a_default_argument():
    """
    The PATTERN, not the instance.  A module-level tunable used as a default
    argument is frozen at import and cannot be changed at runtime -- which is
    how the multi-start count silently ignored every attempt to vary it.
    """
    import inspect
    import re
    src = inspect.getsource(main)
    # module-level ALL_CAPS names that look like tunables
    tunables = set(re.findall(r"^([A-Z][A-Z0-9_]{3,})\s*=", src, re.MULTILINE))
    bad = []
    for m in re.finditer(r"^def\s+(\w+)\s*\(([^)]*)\)", src, re.MULTILINE):
        fname, params = m.group(1), m.group(2)
        for pm in re.finditer(r"(\w+)\s*=\s*([A-Z][A-Z0-9_]{3,})\b", params):
            if pm.group(2) in tunables:
                bad.append(f"{fname}({pm.group(1)}={pm.group(2)})")
    assert not bad, (
        "tunable(s) bound as default arguments, frozen at import and "
        f"untunable at runtime: {bad}. Default to None and resolve inside "
        f"the function instead.")
