"""
Geometry Search Bounds panel: the buildable-grid override, the absolute depth
maximum, and the one-modal validation (FOLLOWUPS 9a-9d).

Two of these controls CHANGED MEANING rather than moving, which is the failure
mode the panel work exists to fix and the one a test suite is worst at seeing:

  * the thickness step used 0 as a "use the buildable grid" sentinel, so the
    field read as an inert control and was nearly deleted -- the grid it gates
    is CONSTRUCTED in the UI, one layer up from where anyone looked (9d);
  * the depth field was a SPAN above a computed floor while the column header
    said Max, so the shipped default looked inverted and any min<max rule
    would have rejected it (9b).

Instantiating the panel needs a QApplication and a QtWebEngine context that
will not start headless, so nothing here builds a widget.  What is tested is
what does not need one: the arithmetic that converts the old default into the
new field (real numpy grids, not assertions about intent), the preset
migration (real methods, bound to a stand-in that supplies only the hull draft
and the freeboard text), and SOURCE guards for the wiring, in the style of
tests/test_source_guards.py.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = (_ROOT / "ui" / "structural_tab.py").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
#  Source guards — true with or without a display
# ══════════════════════════════════════════════════════════════════════════

def test_the_thickness_branch_is_gated_by_the_checkbox_not_the_step():
    """
    A positive step must no longer BE the decision.  "Which grid" and "how
    fine" are separate questions; fusing them is what made 0.0000 look like a
    broken control and hid a working advanced feature behind it.
    """
    assert "self.thickness_override_checkbox.isChecked()" in _SRC, (
        "the override checkbox is gone; the thickness branch would be back on "
        "a zero sentinel")
    assert not re.search(r"if\s+_t_step\s*>\s*1e-9\s*:", _SRC), (
        "the custom-thickness branch still keys off a positive step, so the "
        "step field is once again both the value and the switch")


def test_the_step_field_cannot_hold_the_old_sentinel():
    """
    Zero must be unreachable, not merely unused: a spin box that still accepts
    0 lets a stale preset write the sentinel back in and re-create the
    ambiguity the checkbox removed.
    """
    m = re.search(r"self\.thickness_step_input\s*=\s*_spin\(([^,]+),", _SRC)
    assert m, "thickness_step_input is no longer built with _spin"
    assert m.group(1).strip() == "_IN_32", (
        f"the step's minimum is {m.group(1).strip()}, not one buildable "
        f"increment; 0 is reachable again")


def test_the_sweep_is_handed_a_freeboard_ceiling_only():
    """
    The row has held three meanings: floor+SPAN, then an absolute MOLD DEPTH,
    now a freeboard CEILING -- the same quantity the minimum bounds.  Exactly
    one form may travel; main.py raises on more than one.
    """
    assert "freeboard_max_in=self.freeboard_max_input.value()" in _SRC, (
        "the panel no longer passes a maximum freeboard")
    assert "depth_span_in=" not in _SRC, (
        "the panel still passes the DEPRECATED span form")
    assert "depth_max_in=" not in _SRC, (
        "the panel still passes an absolute mold depth; with freeboard_max_in "
        "also set, main.py refuses the pair and no sweep runs at all")


def test_the_panel_does_not_convert_freeboard_to_depth_itself():
    """
    The draft belongs to the hull, not to either field.  If the panel did the
    arithmetic it would use the draft IT scraped, and a sweep that parsed a
    different one would search a different range while the label said
    otherwise -- the confusion this row has already made twice.
    """
    call = _SRC[_SRC.index("freeboard_max_in=self.freeboard_max_input"):][:400]
    assert "_hull_draft_in" not in call, (
        "the panel is converting freeboard to mold depth at the call site; "
        "the conversion belongs in the sweep, against the draft it parsed")


def test_the_override_reaches_the_sweep_banner():
    """
    The grid is built HERE, so nothing downstream can discover that its
    thicknesses are not buildable ones.  Unreported, an overridden grid prints
    as "N buildable options" -- the banner asserting the opposite of the truth.
    """
    assert "thickness_grid_overridden=" in _SRC, (
        "the UI no longer tells the sweep its thickness grid was overridden")
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    # Adjacent-literal seams are an accident of line width, not of wording.
    joined = re.sub(r'"\s*\n\s*f?"', "", main_src)
    assert "BUILDABLE GRID OVERRIDDEN" in joined, (
        "the sweep banner no longer distinguishes an overridden grid")
    assert "thickness_grid_overridden else" in main_src, (
        "the banner's 'buildable options' wording is unconditional again")


def test_every_offending_pair_lands_in_one_modal():
    """
    One dialog per bad field makes the user re-run the gauntlet once per
    mistake.  The panel already had the collect-then-show shape; the pair
    checks must use it rather than warning inline.
    """
    val = _SRC[_SRC.index("    def _validate(self)"):]
    val = val[:val.index("\n    # ---")]
    warns = [ln for ln in val.splitlines() if "QMessageBox.warning" in ln]
    assert len(warns) == 1, (
        f"_validate shows {len(warns)} dialogs; every finding belongs in the "
        f"single modal so the user sees them all at once")
    assert "errors.append" in val


def test_every_inverted_range_names_both_of_its_numbers():
    """
    "Maximum must exceed the minimum" does not tell the user which number to
    change.  Both pairs -- freeboard and mix density -- state both values and
    say what the consequence is.
    """
    assert "maximum {_fb_max:g} in is at or below the" in _SRC
    assert "minimum {_d_lo:g} pcf is above maximum" in _SRC
    assert _SRC.count("nothing would be searched") >= 2, (
        "an inverted range no longer says it searches nothing, which is the "
        "half that stops it reading as 'no feasible design'")
    # The same failures, named the same way, on the headless path.
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "freeboard-required minimum mold depth" in main_src
    assert "the mix density range is EMPTY" in main_src, (
        "the sweep-side density guard is gone; the UI protects one path only")


# ══════════════════════════════════════════════════════════════════════════
#  The default depth grid — the searched-grid equivalence, UI side
# ══════════════════════════════════════════════════════════════════════════
#
# The sweep layer already proves (floor, max) reproduces (floor, span)
# exactly (tests/test_sweep_diagnostics.py).  What is NOT covered there is the
# number this panel now ships as its default: the old field held a span of
# 4.5 in, the new one holds an absolute depth, and converting between them
# means rounding a float into a spin box's precision.
#
# np.arange's interval is half-open, so a default even 0.004 in high admits
# one extra depth at fine steps -- a default GUI run would silently search a
# grid the old one did not.  Measured, not assumed.

def _ui_default_max(floor: float, span: float = 4.5) -> float:
    """The panel's conversion, restated: truncate to the field's 3 decimals."""
    return math.floor((floor + span) * 1000.0) / 1000.0


@pytest.mark.parametrize("floor", [10.576026, 12.57, 14.0, 20.0, 10.5])
def test_the_default_maximum_searches_the_old_span_exactly(floor):
    """Every step the field can hold, not just the shipped one."""
    new_max = _ui_default_max(floor)
    mismatched = []
    for k in range(125, 2001):                 # 0.125 - 2.000 in, the spin range
        step = k / 1000.0
        old = np.arange(floor, floor + 4.5, step)
        new = np.arange(floor, new_max, step)
        if not np.array_equal(old, new):
            mismatched.append(step)
    assert not mismatched, (
        f"floor {floor}: the default maximum {new_max} searches a different "
        f"grid from the old 4.5 in span at {len(mismatched)} step size(s), "
        f"first {mismatched[:5]}")


def test_rounding_the_default_up_would_widen_the_search():
    """
    Why the conversion truncates.  This is the bug the truncation prevents --
    if it ever stops failing, round() became safe and the comment is stale.
    """
    floor = 10.576026
    rounded_up = round(floor + 4.5, 2)         # 15.08, above floor + 4.5
    old = np.arange(floor, floor + 4.5, 0.125)
    widened = np.arange(floor, rounded_up, 0.125)
    assert len(widened) == len(old) + 1, (
        "rounding the default to 2 decimals no longer adds a depth; if the "
        "field's precision changed, re-derive the truncation")


def test_truncation_never_drops_a_depth_the_old_default_searched():
    """The other direction: truncating must not narrow the search either."""
    for floor in (10.576026, 12.57, 14.0, 20.0):
        new_max = _ui_default_max(floor)
        for k in range(125, 2001):
            step = k / 1000.0
            old = set(np.round(np.arange(floor, floor + 4.5, step), 9))
            new = set(np.round(np.arange(floor, new_max, step), 9))
            assert not (old - new), (
                f"floor {floor}, step {step}: truncation dropped "
                f"{sorted(old - new)} from the default search")


# ══════════════════════════════════════════════════════════════════════════
#  Preset migration — both controls changed MEANING, so old files must convert
# ══════════════════════════════════════════════════════════════════════════
#
# A preset that loads "successfully" into a renamed field is the quiet version
# of this whole defect class: depth_span 4.5 written into an absolute-maximum
# box asks for a 4.5 in mold, which is below every floor and searches nothing.

class _Text:
    """The one widget _migrate_preset touches, without a QApplication."""

    def __init__(self, txt): self._t = str(txt)
    def text(self): return self._t
    def setText(self, t): self._t = str(t)


def _migrator(draft_in=6.57, freeboard="6"):
    """`_migrate_preset` bound to a stand-in that keeps the REAL floor
    arithmetic -- only the hull draft and the text field are supplied."""
    from ui.structural_tab import StructuralTab

    class _Stub:
        freeboard_var = _Text(freeboard)
        _hull_draft_in = lambda self: draft_in
        _depth_floor_in = StructuralTab._depth_floor_in
        _migrate_preset = StructuralTab._migrate_preset

    return _Stub()


def test_a_saved_span_migrates_all_the_way_to_a_freeboard_ceiling():
    """
    Two hops in one load: span -> absolute depth -> freeboard.  A preset from
    the first generation must land on the current field, not halfway.
    """
    out = _migrator(draft_in=6.57).\
        _migrate_preset({"freeboard": "6", "depth_span": 4.5})
    assert "depth_span" not in out and "depth_max" not in out, (
        "a stale key survived; it would be silently ignored on apply")
    # floor 12.57 + span 4.5 = 17.07 in of depth, less 6.57 in of draft.
    assert out["freeboard_max"] == pytest.approx(10.5, abs=1e-3)


def test_migration_honours_the_presets_own_freeboard_not_the_screens():
    """
    The floor is draft + freeboard, and a preset carries its own freeboard.
    Converting against whatever is on screen would move the saved search.
    """
    m = _migrator(draft_in=6.57, freeboard="6")
    out = m._migrate_preset({"freeboard": "8", "depth_span": 4.5})
    # floor 6.57 + 8 = 14.57, + span 4.5 = 19.07 of depth, - 6.57 of draft.
    assert out["freeboard_max"] == pytest.approx(12.5, abs=1e-3), (
        "the span was converted against the on-screen freeboard, not the "
        "preset's own")
    assert m.freeboard_var.text() == "6", (
        "migration left the preset's freeboard on the widget; the real value "
        "is written by _apply_preset immediately after")


def test_a_saved_zero_step_means_the_buildable_grid_not_a_zero_step():
    """0 was the sentinel.  Writing it into the new field would clamp it to
    1/32 in and turn a buildable-grid preset into an override."""
    out = _migrator()._migrate_preset({"thickness_step": 0.0})
    assert out["thickness_override"] is False
    assert "thickness_step" not in out, (
        "the sentinel is being written into a field whose minimum is 1/32 in")


def test_a_saved_positive_step_keeps_meaning_the_override():
    out = _migrator()._migrate_preset({"thickness_step": 0.05})
    assert out["thickness_override"] is True
    assert out["thickness_step"] == 0.05


def test_a_current_preset_passes_through_untouched():
    cur = {"freeboard_max": 10.5, "thickness_override": True,
           "thickness_step": 0.05}
    assert _migrator()._migrate_preset(cur) == cur


def test_a_depth_era_preset_converts_against_the_hull_loaded_now():
    """
    The draft is the HULL's, so a preset saved against one hull converts
    against the hull in front of you.  That is the honest reading of "search
    up to this much freeboard"; carrying the absolute depth across a hull
    change would quietly mean a different amount of hull above the water.
    """
    shallow = _migrator(draft_in=4.576)._migrate_preset({"depth_max": 15.076})
    deep = _migrator(draft_in=6.57)._migrate_preset({"depth_max": 15.076})
    assert shallow["freeboard_max"] == pytest.approx(10.5, abs=1e-3)
    assert deep["freeboard_max"] == pytest.approx(8.506, abs=1e-3)


def test_the_registry_offers_only_the_current_keys():
    """A retired key that still existed would be written by _collect_preset
    and read back with the wrong meaning by the next version of this panel."""
    assert '"depth_span": self.' not in _SRC
    assert '"depth_max": self.' not in _SRC
    assert '"freeboard_max": self.freeboard_max_input' in _SRC
    assert '"mix_min": self.mix_min_var' in _SRC
    assert '"thickness_override": self.thickness_override_checkbox' in _SRC


def test_the_freeboard_step_can_go_finer_than_the_old_eighth_inch():
    """
    Freeboard is a shallower quantity than mold depth and 1/8 in is coarse
    against a ~4 in range.  The floor is the field's own precision now.
    """
    m = re.search(r"self\.depth_step_input\s*=\s*_spin\(([^,]+),", _SRC)
    assert m, "depth_step_input is no longer built with _spin"
    assert float(m.group(1)) <= 0.01 + 1e-9, (
        f"the step's minimum is {m.group(1)}, no finer than before")


# ══════════════════════════════════════════════════════════════════════════
#  The redesign: cards replace the settings accordion
# ══════════════════════════════════════════════════════════════════════════
#
# _CardStack wears QToolBox's addItem/setCurrentIndex on purpose. The five
# category blocks that build the settings are long and full of controls whose
# meaning has already moved once; carrying a presentation change and a control
# change in one diff is how the next one moves silently. Keeping the interface
# means the redesign could not touch them.

def test_the_card_stack_still_answers_the_toolbox_interface():
    from ui.structural_tab import _CardStack

    for name in ("addItem", "setCurrentIndex", "count"):
        assert hasattr(_CardStack, name), (
            f"_CardStack dropped {name}; the five category blocks would have "
            f"to be edited to match, which is the diff this shape avoids")


@pytest.mark.parametrize("raw,expect", [
    ("\U0001f4c1  1.  File Input", "FILE INPUT"),
    ("\U0001f4c1  3.  Geometry Search Bounds", "GEOMETRY SEARCH BOUNDS"),
    ("\U0001f4c1  5.  Solver Precision", "SOLVER PRECISION"),
])
def test_the_ordinal_prefix_is_dropped_from_card_titles(raw, expect):
    """
    "1." / "2." told the user to work top-to-bottom, which was only ever true
    of an accordion that showed one page at a time. Cards are independent, so
    the number is a false instruction -- the name alone survives.
    """
    from ui.structural_tab import _CardStack

    assert _CardStack._clean(raw) == expect


def test_setcurrentindex_does_not_collapse_the_others():
    """
    THE behavioural difference, and the reason for the change: a QToolBox
    shows one page, so checking a thickness bound against the depth bound
    that constrains it meant clicking back and forth holding a number in your
    head. Expanding one card must not close another.
    """
    import inspect
    from ui.structural_tab import _CardStack

    src = inspect.getsource(_CardStack.setCurrentIndex)
    assert "set_expanded(True)" in src
    assert "set_expanded(False)" not in src, (
        "setCurrentIndex collapses other cards; that is QToolBox behaviour "
        "and the accordion was replaced to be rid of it")


def test_the_palette_is_referenced_by_role_not_by_hex():
    """
    The accent moved from cyan to amber and every hard-coded #00d2ff had to be
    found by hand. Tokens are named for what they DO so the next move is one
    edit in ui/style.py.
    """
    from ui.style import C, DARK_STYLE

    assert C.ACCENT in DARK_STYLE and C.OK in DARK_STYLE
    assert "#00d2ff" not in DARK_STYLE, (
        "the retired cyan accent is back in the stylesheet")
    # Role names, not colour names -- ACCENT survives a hue change, ORANGE does not.
    for role in ("ACCENT", "OK", "WARN", "TEXT_DIM", "BG_CARD", "ON_ACCENT"):
        assert getattr(C, role).startswith("#") or role == "MONO"


def test_the_freeboard_default_needs_no_truncation_at_all():
    """
    The truncation in 9e-i exists because floor + 4.5 had to be rounded into a
    spin box, and rounding UP past it admitted a depth the old default never
    searched.  Bounding FREEBOARD removes the arithmetic instead of taming it:
    both ends convert through the same draft, so the span between them is
    exactly 4.5 in on every hull and np.arange's half-open interval lands
    where it always did.

    Measured over four floors and every step from 0.010 to 2.000 in -- four
    FLOORS rather than four drafts, because the band is anchored to the floor
    and the draft never enters the arithmetic.  The
    truncation is still live for the span-era preset hop, which does have to
    write a converted number into a field -- that is the only thing keeping it.
    """
    bad = []
    for floor in (10.576026, 12.57, 14.0, 20.0):   # wherever the floor lands
        by_freeboard_max = floor + (10.5 - 6.0)    # no rounding anywhere
        for k in range(10, 2001):
            step = k / 1000.0
            old = np.arange(floor, floor + 4.5, step)
            new = np.arange(floor, by_freeboard_max, step)
            if not np.array_equal(old, new):
                bad.append((floor, round(step, 3)))
    assert not bad, (
        f"the freeboard default searches a different grid from the original "
        f"4.5 in span at {len(bad)} (floor, step) pair(s), first {bad[:5]}")
