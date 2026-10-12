# tests/test_report.py
"""
The LaTeX technical report must be a faithful view of the live sweep result.

Guards the sync bug where the old report showed a stale/hardcoded weight
(117.0 lb) that disagreed with the model winner: the report's headline weight
MUST equal the sweep result weight, and none of the killed fabricated sections
(longitudinal "zero-moment ends" pour zoning that was never built) may return.
"""
from __future__ import annotations

import os
import re

import pytest


def _real_env(monkeypatch):
    """Production-like sweep config (real mix DB, 2-mix cap, no fixed seating)."""
    import config
    monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", None)
    monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 0.0)
    monkeypatch.setattr(config, "MAX_MIXES", 2)
    monkeypatch.setattr(config, "N_MIXES_MAX", 2)
    monkeypatch.setattr(config, "MAX_DENSITY_PCF", 80.0)


def test_report_headline_weight_equals_sweep_weight(small_sweep_model):
    """
    The headline weight must BE the sweep weight, never a cached, hardcoded or
    independently recomputed value.  That identity holds at any resolution, so
    the shared coarse sweep tests it exactly as a production one did.
    """
    from reporting.latex_report import render_latex
    m, res = small_sweep_model

    # THE guard: the report headline weight is the sweep weight itself, never a
    # cached, hardcoded, or independently recomputed value.
    assert m["weight_lbs"] == pytest.approx(res["weight_lbs"], abs=1e-9)

    tex = render_latex(m, {"env": "env.png", "dc": "dc.png", "pour": "pour.png"})
    assert f"{res['weight_lbs']:.2f}~lb" in tex

    # the killed fabricated sections must not come back
    low = tex.lower()
    assert "zero-moment ends" not in low
    assert "low moment (ends)" not in low
    assert "lightest mix at" not in low


def test_report_model_rejects_empty_result():
    from reporting.latex_report import build_report_model
    with pytest.raises(ValueError):
        build_report_model({})


# ── portability ──────────────────────────────────────────────────────────────
# Figure paths must be RELATIVE to the .tex. Absolute paths bake one machine's
# directory layout into the document, so the report directory cannot be moved,
# shared, or compiled anywhere else -- and this project's absolute path also
# contains spaces, which breaks \includegraphics on its own.

def _graphics_paths(tex):
    return re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", tex)


def test_figure_paths_are_relative_not_absolute(tmp_path, small_sweep_model):
    r"""\includegraphics targets must be relative and drive-free -- a property of
    render_latex's path handling, independent of the sweep that fed it."""
    from reporting.latex_report import render_latex
    m, _res = small_sweep_model
    out = str(tmp_path)
    charts = {k: os.path.join(out, f"{k}.png") for k in ("env", "dc", "pour")}
    tex = render_latex(m, charts, out_dir=out)

    paths = _graphics_paths(tex)
    assert paths, "report emitted no figures"
    for p in paths:
        assert not os.path.isabs(p), f"absolute figure path: {p}"
        assert ":" not in p, f"drive-qualified figure path: {p}"
        assert "\\" not in p, f"backslash in TeX path: {p}"
        # must resolve relative to the .tex directory
        assert os.path.basename(p) == p or not p.startswith(".."), p


def test_figure_paths_survive_a_moved_output_directory(tmp_path, small_sweep_model):
    # The same model rendered for two different output dirs must produce
    # IDENTICAL figure references -- that is what makes the directory movable.
    from reporting.latex_report import render_latex
    m, _res = small_sweep_model

    def _render(base):
        charts = {k: os.path.join(base, f"{k}.png") for k in ("env", "dc", "pour")}
        return _graphics_paths(render_latex(m, charts, out_dir=base))

    assert _render(str(tmp_path)) == _render(r"/somewhere/else/entirely")


# ══════════════════════════════════════════════════════════════════════════
#  The overridden-fit caveat must actually REACH the page
# ══════════════════════════════════════════════════════════════════════════

def test_fit_override_caveat_reaches_the_rendered_report(small_sweep_model):
    """
    The loudest warning the report can carry is the one saying the design is
    gated on a regression the suite judged physically impossible.

    It is rendered from the report MODEL, not from the raw sweep result --
    ``render_latex`` never sees the result dict.  Reading it off ``result``
    there raised NameError on EVERY render, which took the whole report down;
    reading it off an ``m`` that never carried it would have been quieter and
    worse, printing a clean-looking report for an overridden design.  This
    pins both halves: the key crosses into the model, and the prose renders.
    """
    from reporting.latex_report import build_report_model, render_latex
    _m, res = small_sweep_model

    rec = {"source": "interactive override (test)",
           "codes": ["fr_exceeds_fc"],
           "tiers": ["fr"],
           "row_ids": [18, 22, 24],
           "findings": ["flexural f_r (MOR) fit reaches 6.9x the compressive "
                        "strength at 64.0 pcf"]}
    m = build_report_model({**res, "fit_override": rec})
    assert m["fit_override"] == rec, "build_report_model dropped fit_override"

    tex = render_latex(m, {"env": "env.png", "dc": "dc.png", "pour": "pour.png"})
    assert "OVERRIDDEN STRENGTH FIT" in tex
    assert "interactive override (test)" in tex
    assert "18, 22, 24" in tex

    # And a clean run must NOT carry the caveat.
    clean = build_report_model({**res, "fit_override": None})
    assert "OVERRIDDEN STRENGTH FIT" not in render_latex(
        clean, {"env": "env.png", "dc": "dc.png", "pour": "pour.png"})
