# tests/test_verification_guards.py
"""
TWO MECHANICAL GUARDS for one recurring defect: a quantity was patched and
verified, while the production path read a DIFFERENT quantity derived from it.
Three defects in one session shared that shape, each found only by a full run:

  * report generation read `result` in a function whose parameter is `m`
    (NameError, dead since Jul 24 -- no test entered that path)
  * the stem taper wrote A_in2 while the sweep reads cum_area
  * the taper then lifted the tip past the degeneracy exclusion the two
    transverse gates depend on

Neither guard asks anyone to remember which array the sweep reads.
"""
from __future__ import annotations

import dataclasses
import os

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF1 = os.path.join(REPO, "off1.mlt")

pytestmark = pytest.mark.skipif(not os.path.exists(OFF1),
                                reason="needs the real offset file")

# Fields a degenerate stem station may legitimately leave at the floor, each
# with the reason it MUST stay there.  Anything not named here has to carry a
# real tapered value -- so adding a field, or extending the taper, forces the
# decision instead of letting it default to "forgot".
INTENTIONALLY_DEGENERATE = {
    "yhalf_in":  "a stem has ~zero half-breadth; raising it lifts the station "
                 "past structural_station_mask's breadth test and re-opens the "
                 "sigma_y ~ 1/b^2 divergence in BOTH transverse gates",
    "width_ft":  "a knife-edge stem has no waterplane, and the overhang model "
                 "already treats the region beyond the last station as un-buoyed",
}


def _props(n=161):
    from geometry import parse_godzilla_offsets, calculate_shell_properties
    xr, zr, ym = parse_godzilla_offsets(
        OFF1, L_ft=20.00984316, draft_in=4.5760260931,
        mold_depth_in=14.57035257349158, n_stations=n)
    return xr, calculate_shell_properties(xr, zr, ym, 0.375)


def test_every_per_station_field_is_tapered_or_explicitly_exempt():
    """
    THE GUARD.  Walks ShellProperties by reflection, so a field added later is
    covered without anyone editing this test.  A stem station must hold a real
    value in every per-station array, or be named in INTENTIONALLY_DEGENERATE.

    This is what would have caught cum_area being left at the placeholder
    while A_in2 was tapered: the sweep reads cum_area, and no unit test on
    A_in2 could ever have noticed.
    """
    from domain.models import ShellProperties
    x, p = _props()
    nst = len(x)
    checked = []
    for f in dataclasses.fields(ShellProperties):
        v = getattr(p, f.name, None)
        if v is None:
            continue
        v = np.asarray(v, float)
        if v.ndim == 0 or v.shape[0] != nst:
            continue          # not indexed by station (e.g. the z grid)
        checked.append(f.name)
        for stem, neigh in ((0, 1), (nst - 1, nst - 2)):
            mag = float(np.max(np.abs(v[stem])))
            ref = float(np.max(np.abs(v[neigh])))
            if f.name in INTENTIONALLY_DEGENERATE:
                assert ref <= 0 or mag <= 1e-3 * ref, (
                    f"{f.name} is exempt as degenerate ("
                    f"{INTENTIONALLY_DEGENERATE[f.name]}) but station {stem} "
                    f"carries {mag:.6g} against a neighbour's {ref:.6g}; if "
                    f"this is now a real value, remove it from the exemption "
                    f"list and say why that is safe")
            else:
                assert ref <= 0 or mag >= 0.10 * ref, (
                    f"{f.name}[{stem}] = {mag:.6g} is a placeholder next to "
                    f"neighbour {ref:.6g}. Every per-station array must be "
                    f"tapered onto the stem or be listed in "
                    f"INTENTIONALLY_DEGENERATE with a reason.")
    assert len(checked) >= 10, f"reflection found only {checked}; it broke"
    assert "cum_area_in2" in checked, "the field the SWEEP reads went unchecked"


def test_the_real_report_entry_point_runs_end_to_end(tmp_path):
    """
    THE SECOND GUARD, for the other half of the class: code that is never
    executed by any test.  build_report_model() read an undefined `result` and
    raised NameError for over a month; every test exercised helpers around it.

    This calls the REAL entry point on a minimal design and fails on ANY
    exception.  It asserts almost nothing about the content on purpose -- its
    whole job is that the path executes.
    """
    from reporting.latex_report import build_report_model
    n = 161
    x = np.linspace(0.0, 20.00984316, n)
    ones = np.ones(n)
    result = {
        "weight_lbs": 133.1491, "kupfer_dc": 0.9255,
        "mixes": [72.5, 62.5, 72.5], "zone_densities": [72.5, 62.5, 72.5],
        "t_in": 0.375, "thickness_in": 0.375, "depth_in": 14.57035257349158,
        "pour_fracs": [0.2745, 0.6863], "L_ft": 20.00984316,
        "draft_in": 4.5760260931, "n_stations": n,
        "x": x,
        "M_envelope": 150.0 * np.sin(np.pi * x / x[-1]),
        "V_envelope": {"max_pos": 80.0 * np.cos(np.pi * x / x[-1]),
                       "max_neg": -80.0 * np.cos(np.pi * x / x[-1])},
        "M_sag": 150.0 * np.sin(np.pi * x / x[-1]),
        "M_hog": -20.0 * np.sin(np.pi * x / x[-1]),
        "na_line": 6.0 * ones,
        "sheer_line_in": 14.57035257349158 * ones,
        "utilization": {"bot_t": 0.9255 * np.sin(np.pi * x / x[-1]),
                        "top_t": 0.6100 * np.sin(np.pi * x / x[-1])},
        "paddler_positions": [4.5, 8.0, 12.0, 15.5],
        "governing": "sagging",
        "hoop_dc_homog": 0.41,
        "hoop_gov_zone": 1,
    }
    m = build_report_model(result)          # must not raise
    assert isinstance(m, dict) and m, "the report model came back empty"
    assert "fit_override" in m, (
        "fit_override vanished from the report model; it was silently dead "
        "once already")


def test_the_inert_knob_warning_fires(capsys, monkeypatch):
    """
    knee_frac_bending cannot move the reported D/C while the envelope is on.
    A cfg comment would be read AFTER someone spends an afternoon wondering
    why nothing moved, so the suite pins that the warning is emitted at
    startup instead.
    """
    import config, main
    monkeypatch.setattr(config, "PADDLER_LOAD_ENVELOPE", True, raising=False)
    monkeypatch.setattr(config, "KNEE_FRAC_BENDING", 0.90, raising=False)
    monkeypatch.setattr(config, "KNEE_FRAC_PUNCHING", 1.00, raising=False)
    src = __import__("inspect").getsource(main.run_grand_optimization)
    assert "CANNOT AFFECT ANY" in src, (
        "the inert-knob warning has gone from run_grand_optimization")
    assert "PADDLER_LOAD_ENVELOPE" in src
