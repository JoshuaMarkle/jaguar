# tests/test_bending_signs.py
"""
Each bending fibre must be checked against the allowable for the SIGN of stress
it actually carries.  Guards the mock-section error class where the report
divided the top fibre's SAGGING (compressive) stress by the TENSILE allowable
and produced a spurious D/C of 1.65 -- while the sweep gate, which pairs the
senses correctly, reported 0.94 for the same design.
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def model(small_sweep_model):
    """
    The SHARED coarse sweep (tests/conftest.py::small_sweep).

    Every assertion in this file is about fibre SENSE and gate/report
    reconciliation -- which fibre a bending sense tensions, and whether the
    headline D/C equals the larger gated tension utilisation.  None of that
    depends on station count, so a production-resolution sweep bought nothing
    but hours.  This file's own module-level cache was the right shape; the
    fixture just shares it across files too.
    """
    return small_sweep_model


def test_top_fibre_sagging_is_checked_against_compressive_allowable(model):
    m, _ = model
    gs = m["gov_section"]
    # Under sagging the top fibre is in COMPRESSION -> f'c, not f_t.
    assert gs["zone_fc"][2] > gs["zone_ft"][2], "f'c must exceed f_t"
    assert gs["dc_top_c"] == pytest.approx(gs["sig_top_c"] / gs["zone_fc"][2])
    # The bug: same stress over the TENSILE allowable inflates D/C past 1.
    bogus = gs["sig_top_c"] / gs["zone_ft"][2]
    assert bogus > gs["dc_top_c"]
    assert gs["dc_top_c"] < 1.0, "compression fibre must not fail against f'c"


def test_tension_fibres_use_the_sense_that_tensions_them(model):
    m, _ = model
    gs = m["gov_section"]
    # bottom tension comes from SAGGING, top tension from HOGGING
    assert gs["sig_bot_t"] == pytest.approx(
        gs["M_sag_in_lb"] * gs["y_bar_in"] / gs["I_tr_in4"])
    n_top = gs["zone_n"][2]
    assert gs["sig_top_t"] == pytest.approx(
        n_top * gs["M_hog_in_lb"] * (gs["sheer_in"] - gs["y_bar_in"])
        / gs["I_tr_in4"])


def test_report_section_agrees_with_the_enforced_gate(model):
    m, _ = model
    gs = m["gov_section"]
    # THE reconciliation guard: the headline longitudinal D/C is the larger of
    # the two GATED (tension) utilisations -- never a compression fibre.
    assert m["gov_bend"]["dc"] == pytest.approx(
        max(gs["util_bot_t"], gs["util_top_t"]), rel=1e-9)


def test_gate_never_charges_compression_against_tensile_allowable(model):
    m, res = model
    # The sweep's own arrays: tension utilisation must track the tension
    # fibres only. If the gate had the sign bug, bot_t would spike wherever
    # hogging dominates (bottom in compression) -- it must not.
    bot_t = np.asarray(res["utilization"]["bot_t"])
    top_t = np.asarray(res["utilization"]["top_t"])
    assert np.all(bot_t >= 0) and np.all(top_t >= 0)
    # Feasible winner: every gated station is within the limit.
    assert np.max(bot_t) <= 1.0 + 1e-9
    assert np.max(top_t) <= 1.0 + 1e-9


def test_compression_is_advisory_not_gated(model):
    m, _ = model
    gs = m["gov_section"]
    # Compression D/C is reported but must not drive the headline number.
    assert gs["dc_top_c"] != pytest.approx(m["gov_bend"]["dc"])
    assert m["gov_bend"]["dc"] == pytest.approx(
        max(gs["util_bot_t"], gs["util_top_t"]), rel=1e-9)
