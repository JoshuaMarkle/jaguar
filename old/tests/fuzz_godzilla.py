"""
Fuzz suite for services/hull_service.write_seed_hull.

Generates 50 pseudo-random hull seeds (shape factors, beam, draft, length)
and calls write_seed_hull for each one.  The contract being verified:

  • The writer NEVER raises an exception other than ValueError.
  • When it does raise ValueError the message must mention either
    "validation failed" or "out of physical range".
  • When it succeeds the output in.mlt must have exactly _EXPECTED_LINE_COUNT
    lines, all terminated with CRLF (no bare LF).

Godzilla (mlt933w.exe) is NOT required — only write_seed_hull is exercised.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# Seed for reproducibility
RNG = np.random.default_rng(seed=0xC0FFEE)


def _make_results(factors, beam_m, draft_m):
    return {
        "best_hull": {
            "factors": list(factors),
            "beam": float(beam_m),
            "draft": float(draft_m),
        }
    }


def _generate_seeds(n: int = 50):
    """
    Yield (results_dict, hull_length_m) tuples with varied parameters.

    Mostly stays in physically valid ranges, but intentionally includes a few
    edge-case seeds that should trigger clean ValueError (out-of-range bounds).
    """
    seeds = []

    # ── 40 realistic seeds (should succeed) ────────────────────────────────
    for _ in range(40):
        # Shape factors: first 5 in [0.1, 2.9], last 2 in [0.01, 0.74]
        f = np.concatenate([
            RNG.uniform(0.1, 2.9, size=5),
            RNG.uniform(0.01, 0.74, size=2),
        ])
        beam   = RNG.uniform(0.45, 0.85)
        draft  = RNG.uniform(0.12, 0.35)
        length = RNG.uniform(4.5, 7.5)
        seeds.append((_make_results(f, beam, draft), length))

    # ── 5 boundary seeds (may or may not succeed) ──────────────────────────
    # Very short and shallow — vol may end up very small
    for _ in range(5):
        f = np.concatenate([
            RNG.uniform(0.5, 1.5, size=5),
            RNG.uniform(0.05, 0.40, size=2),
        ])
        beam   = RNG.uniform(0.40, 0.60)
        draft  = RNG.uniform(0.05, 0.12)
        length = RNG.uniform(1.5, 3.5)
        seeds.append((_make_results(f, beam, draft), length))

    # ── 5 out-of-range seeds (must raise clean ValueError) ─────────────────
    # Draft > 1.0 m — exceeds _BOUNDS["T_m"]
    seeds.append((_make_results([1.4]*5 + [0.15, 0.05], 0.61, 1.5), 6.0))
    # Length > 15 m — exceeds _BOUNDS["L_m"]
    seeds.append((_make_results([1.4]*5 + [0.15, 0.05], 0.61, 0.20), 20.0))
    # Very negative-ish shape factors (still legal python floats)
    seeds.append((_make_results([-0.5]*5 + [0.0, 0.0], 0.61, 0.20), 6.0))
    # Beam > 3.0 m — exceeds _BOUNDS["B_m"]
    seeds.append((_make_results([1.4]*5 + [0.15, 0.05], 4.0, 0.20), 6.0))
    # Tiny draft + short length (vol < 0.01 m³ expected)
    seeds.append((_make_results([0.1]*5 + [0.01, 0.01], 0.20, 0.01), 1.2))

    return seeds[:n]


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def hull_svc():
    from services.hull_service import HullService
    return HullService()


@pytest.fixture
def workdir(tmp_path):
    src = PROJECT_ROOT / "in_temp.mlt"
    if not src.exists():
        pytest.skip("in_temp.mlt not present in project root")
    shutil.copy(src, tmp_path / "in_temp.mlt")
    return tmp_path


# ── parametrised fuzz test ────────────────────────────────────────────────────

SEEDS = _generate_seeds(50)


@pytest.mark.parametrize("seed_idx,seed_pair", enumerate(SEEDS))
def test_write_seed_no_crash(seed_idx, seed_pair, hull_svc, tmp_path):
    """
    write_seed_hull must not raise anything other than ValueError.

    If it succeeds, the output must satisfy structural invariants.
    """
    src = PROJECT_ROOT / "in_temp.mlt"
    if not src.exists():
        pytest.skip("in_temp.mlt not present")
    shutil.copy(src, tmp_path / "in_temp.mlt")

    results, length_m = seed_pair

    try:
        hull_svc.write_seed_hull(results, length_m, str(tmp_path))
    except ValueError as exc:
        # Clean validation rejection — acceptable.
        msg = str(exc).lower()
        assert (
            "validation failed" in msg or "out of physical range" in msg
        ), (
            f"Seed {seed_idx}: ValueError raised with unexpected message: {exc}"
        )
        return
    except Exception as exc:
        pytest.fail(
            f"Seed {seed_idx} raised unexpected {type(exc).__name__}: {exc}\n"
            f"  results={results}\n  length_m={length_m}"
        )

    # ── structural invariants for successful writes ────────────────────────
    from services.hull_service import _EXPECTED_LINE_COUNT

    out = tmp_path / "in.mlt"
    assert out.exists(), f"Seed {seed_idx}: in.mlt was not created"

    raw = out.read_bytes()

    # CRLF-only: no bare LF
    bare_lf = re.search(rb"(?<!\r)\n", raw)
    assert not bare_lf, (
        f"Seed {seed_idx}: bare LF found in output — Godzilla would reject this"
    )

    # Correct line count
    crlf_lines = raw.split(b"\r\n")
    n_lines = len(crlf_lines) - 1 if crlf_lines[-1] == b"" else len(crlf_lines)
    assert n_lines == _EXPECTED_LINE_COUNT, (
        f"Seed {seed_idx}: output has {n_lines} lines; expected {_EXPECTED_LINE_COUNT}"
    )
