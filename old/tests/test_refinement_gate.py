# tests/test_refinement_gate.py
"""
The refinement gate: screen loose, refine the k lightest, accept on each
design's OWN extrapolated D/C.

WHY NOT A CONSTANT.  The gate replaces acceptance-by-pull-back
(kupfer_drift_per_doubling), which is a tail drift measured on ONE design and
applied to every other.  Drift does not transfer on this hull -- 3.16% against
1.43% on the same geometry -- so a better-sourced constant is still a constant.
"""
from __future__ import annotations

import numpy as np
import pytest

from structural import (extrapolate_kupfer_tail, KUPFER_TAIL_RATIO_FLOOR,
                        KUPFER_PROMOTE_BAND)

# The pinned winner, measured at five resolutions.  The only design on this
# hull with a known tail, so it is the only honest calibration for the
# extrapolator.
PINNED = {161: 0.91347797, 321: 0.92360569, 641: 0.92473731,
          1281: 0.92528002, 2561: 0.92549204}
PINNED_LIMIT = 0.925628          # from the FINEST pair, 1281 -> 2561


def test_the_naive_ratio_would_be_non_conservative():
    """
    THE REASON THE FLOOR EXISTS, pinned so nobody removes it as clutter.

    The first increment ratio is anomalously small (0.1117) because 161 is
    coarse; the tail settles near 0.39-0.48.  Believing that first ratio
    extrapolates BELOW the true limit -- non-conservative by 7.5e-04, which is
    the same order as the drift the gate exists to control.
    """
    d1 = PINNED[321] - PINNED[161]
    d2 = PINNED[641] - PINNED[321]
    r_naive = d2 / d1
    assert r_naive < 0.2, "the calibration case changed; re-derive the floor"
    naive = PINNED[641] + d2 * r_naive / (1 - r_naive)
    assert naive < PINNED_LIMIT, "naive extrapolation is supposed to under-shoot"
    assert PINNED_LIMIT - naive > 5e-4


def test_the_floored_extrapolation_is_conservative_on_the_known_design():
    got = extrapolate_kupfer_tail({n: PINNED[n] for n in (161, 321, 641)})
    assert got["ok"]
    assert got["ratio_used"] == pytest.approx(KUPFER_TAIL_RATIO_FLOOR)
    assert got["u_inf"] >= PINNED_LIMIT, (
        "the extrapolated value must not sit below the measured limit")
    assert got["u_inf"] - PINNED_LIMIT < 1e-3, "over-conservative to the point of useless"


def test_it_uses_the_measured_ratio_when_that_is_the_conservative_one():
    """The floor is a floor, not an override: a slowly-decaying tail keeps its
    own (larger) ratio."""
    u = {100: 0.80, 200: 0.90, 400: 0.97}          # ratio 0.7, well above the floor
    got = extrapolate_kupfer_tail(u)
    assert got["ratio_used"] == pytest.approx(0.7, abs=1e-9)
    assert got["u_inf"] == pytest.approx(0.97 + 0.07 * 0.7 / 0.3, rel=1e-9)


def test_non_decaying_increments_are_never_silently_accepted():
    """A design whose increments GROW has no converged value to accept."""
    got = extrapolate_kupfer_tail({100: 0.90, 200: 0.91, 400: 0.94})
    assert not got["ok"]
    assert got["u_inf"] is None
    assert "not decaying" in (got.get("reason") or "")


def test_a_flat_tail_adds_nothing():
    got = extrapolate_kupfer_tail({100: 0.90, 200: 0.92, 400: 0.92})
    assert got["ok"] and got["u_inf"] == pytest.approx(0.92)
    assert got["tail"] == 0.0


def test_too_few_resolutions_is_a_refusal_not_a_guess():
    got = extrapolate_kupfer_tail({100: 0.90, 200: 0.92})
    assert not got["ok"] and "three resolutions" in got["reason"]


def test_the_promote_band_is_small_enough_to_be_selective():
    """
    Promotion to 8n buys a real datum where extrapolation decides the verdict.
    If the band were wide, every candidate would be promoted and the gate would
    cost 8x on all k -- the thing it exists to avoid.
    """
    assert 0 < KUPFER_PROMOTE_BAND <= 0.005


class TestGateSelection:
    """The k cap must WARN when it binds, never truncate silently."""

    def _pool(self, weights):
        return [{"weight_lbs": w, "mixes": [72.5, 62.5, 72.5], "thickness": 0.375,
                 "depth": 14.57, "pour_fracs": (0.2745, 0.6863), "multizone": True}
                for w in weights]

    def test_cap_binding_on_a_narrow_gap_is_announced(self, monkeypatch):
        import main
        # candidates 12 and 13 are 0.1% apart -- well inside the 0.72% weight
        # drift refinement introduces, so the ordering is not trustworthy.
        pool = self._pool([100.0 + 0.01 * i for i in range(20)])
        monkeypatch.setattr(main, "_evaluate_cfg_at",
                            lambda *a, **k: (0.90, 100.0))
        _w, _recs, notes = main._gate_refine_top_k(
            pool, "off1.mlt", 20.0, 4.5, 6.0, 20.0, False, k=12, n_screen=161)
        blob = " ".join(notes)
        assert "k cap BOUND" in blob
        assert "[!]" in blob and "GATE_TOP_K" in blob, (
            "a binding cap on a narrow gap must say so out loud")

    def test_cap_not_binding_says_so_quietly(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_evaluate_cfg_at",
                            lambda *a, **k: (0.90, 100.0))
        _w, _recs, notes = main._gate_refine_top_k(
            self._pool([100.0, 101.0, 102.0]), "off1.mlt", 20.0, 4.5,
            6.0, 20.0, False, k=12, n_screen=161)
        blob = " ".join(notes)
        assert "did not bind" in blob and "[!]" not in blob

    def test_nothing_surviving_refinement_is_loud(self, monkeypatch):
        import main
        # every candidate lands far above acceptance at every resolution
        monkeypatch.setattr(main, "_evaluate_cfg_at",
                            lambda *a, **k: (1.40, 100.0))
        winner, recs, notes = main._gate_refine_top_k(
            self._pool([100.0, 101.0]), "off1.mlt", 20.0, 4.5,
            6.0, 20.0, False, k=12, n_screen=161)
        assert winner is None
        assert any("NO candidate survived" in n for n in notes)
        assert all(not r["accepted"] for r in recs)

    def test_the_lightest_survivor_wins_even_if_the_lightest_candidate_fails(
            self, monkeypatch):
        """The whole point of a pool: the screen winner may not survive."""
        import main
        pool = self._pool([100.0, 101.0, 102.0])
        seen = {"i": 0}

        def fake(cfg, *a, **k):
            # candidate #1 (100.0 lb) is over the limit; the rest are fine
            return (1.30 if cfg["weight_lbs"] == 100.0 else 0.90), cfg["weight_lbs"]

        monkeypatch.setattr(main, "_evaluate_cfg_at", fake)
        winner, recs, _notes = main._gate_refine_top_k(
            pool, "off1.mlt", 20.0, 4.5, 6.0, 20.0, False, k=12, n_screen=161)
        assert winner is not None
        assert winner["weight_lbs"] == 101.0, "gate must promote the runner-up"
        assert not recs[0]["accepted"] and recs[1]["accepted"]


class TestTopMWorkerContract:
    """
    Each task used to return ONE best_config, chosen on the screen D/C -- the
    number the gate exists because it cannot be trusted.  A lighter design that
    screened slightly worse never reached refinement, so the candidate set was
    filtered by exactly the unreliable value the gate was built to stop relying
    on.  Workers now return their top-m feasible designs as lean records.
    """

    def test_m_is_at_least_the_floor_and_scales_with_task_size(self):
        import main
        assert main.gate_top_m_for_task(0) >= 4
        assert main.gate_top_m_for_task(50) >= 10
        # a task evaluating 5000 configs should offer more than one doing 50
        assert main.gate_top_m_for_task(5000) > main.gate_top_m_for_task(50)

    def test_m_is_capped_so_the_pool_cannot_explode(self):
        import main
        assert main.gate_top_m_for_task(10 ** 6) <= 25

    def test_the_worker_returns_four_values(self):
        """
        Arity is the contract.  If a worker returns three, the consumer's
        unpack raises -- which is the loud failure we want, not a silently
        empty pool.
        """
        import inspect, ast, main
        src = inspect.getsource(main._evaluate_geometry_worker)
        tree = ast.parse(src)
        returns = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)]
        assert returns, "the worker no longer returns a tuple"
        for r in returns:
            assert len(r.value.elts) == 4, (
                "every worker return must be "
                "(best_weight, best_config, n_evaluated, gate_candidates); "
                f"found one with {len(r.value.elts)}")

    def test_lean_records_carry_no_geometry(self):
        """
        The whole reason top-m is affordable: a lean record is ~0.52 KB against
        ~74.2 KB for a config, because x_raw/z_raw/y_matrix are ~90% of a
        config and identical for every design at the same depth.
        """
        import main
        rec = main._lean_gate_record({
            "mixes": [72.5, 62.5, 72.5], "thickness": 0.375, "depth": 14.57,
            "pour_fracs": (0.27, 0.68), "seats": {}, "multizone": True,
            "x_raw": list(range(161)), "y_matrix": [[0.0] * 51] * 161,
        })
        for banned in ("x_raw", "y_matrix", "z_raw"):
            assert banned not in rec, f"{banned} leaked into the lean record"

    def test_the_pool_is_the_union_not_one_per_task(self):
        """
        Two tasks each offering three candidates must give the gate six, not
        two.  This is the property that makes 'the k lightest designs' true
        rather than 'the k lightest task-bests'.
        """
        import main
        pool = []
        for _task in range(2):
            lean = [dict(weight_lbs=100.0 + i, mixes=[72.5, 62.5, 72.5],
                         thickness=0.375, depth=14.57,
                         pour_fracs=(0.27, 0.68), multizone=True)
                    for i in range(3)]
            pool.extend(lean)
        assert len(pool) == 6
        monkey = {"n": 0}

        def fake(cfg, *a, **k):
            monkey["n"] += 1
            return 0.90, cfg["weight_lbs"]

        import pytest as _pt
        with _pt.MonkeyPatch().context() as mp:
            mp.setattr(main, "_evaluate_cfg_at", fake)
            _w, recs, _notes = main._gate_refine_top_k(
                pool, "off1.mlt", 20.0, 4.5, 6.0, 20.0, False,
                k=6, n_screen=161)
        assert len(recs) == 6, "the gate did not see every task's candidates"


# ── non-monotone sequences: wobble is not divergence ───────────────────────

def test_a_falling_then_wobbling_sequence_is_ACCEPTED_with_a_bound():
    """
    THE FALSE REJECTION THIS FIXES.  On a real multizone winner the D/C FELL
    from 161 to 321 (0.28110 -> 0.28050) and ticked up at 641.  d1 <= 0 made
    the ratio meaningless, the old code called that "not decaying", and the
    gate rejected ALL TEN survivors on a hull sitting at D/C 0.28 against a
    0.95 limit -- printing "do not build to it" every single run.

    A warning that always fires is a warning nobody reads, which is worse than
    no warning at all. So: bound it rather than fit it.
    """
    got = extrapolate_kupfer_tail({161: 0.28110, 321: 0.28050, 641: 0.28062})
    assert got["ok"], "a wobble near convergence must not read as divergence"
    assert got.get("non_monotone") is True
    # max(observed) + largest positive step, and nothing tighter
    assert got["u_inf"] == pytest.approx(0.28110 + (0.28062 - 0.28050))
    assert got["u_inf"] > max(0.28110, 0.28050, 0.28062), (
        "the bound must sit ABOVE every value actually observed")
    assert "BOUND" in (got.get("note") or ""), (
        "the record must say this is a bound, not a fitted limit")


def test_a_genuinely_climbing_sequence_is_STILL_REJECTED():
    """
    The other half, and the risk of the fix: it must not become a rubber
    stamp.  A design already climbing at n->2n and climbing at least as fast
    at 2n->4n is divergent, and no bound may rescue it.
    """
    got = extrapolate_kupfer_tail({161: 0.90, 321: 0.91, 641: 0.94})
    assert not got["ok"], "genuine divergence must still be refused"
    assert got["u_inf"] is None
    assert "not decaying" in (got.get("reason") or "")


def test_a_large_wobble_still_fails_the_LIMIT_even_though_it_extrapolates():
    """
    Division of labour: the extrapolator returns a BOUND, the gate compares it
    against acceptance.  A dip-then-leap bounds high enough that the gate
    rejects it on the value, without the extrapolator having to guess.
    """
    got = extrapolate_kupfer_tail({161: 0.90, 321: 0.85, 641: 0.99})
    assert got["ok"] and got.get("non_monotone") is True
    assert got["u_inf"] > 0.95, (
        "a 0.14 upward step must bound above the acceptance limit so the gate "
        "refuses it")


def test_the_bound_is_never_below_the_finest_measurement():
    """A bound that undercuts a value already measured is not a bound."""
    for u in ({161: 0.50, 321: 0.40, 641: 0.45},
              {161: 0.30, 321: 0.29, 641: 0.31},
              {161: 0.28110, 321: 0.28050, 641: 0.28062}):
        got = extrapolate_kupfer_tail(u)
        assert got["ok"]
        assert got["u_inf"] >= max(u.values()) - 1e-12
