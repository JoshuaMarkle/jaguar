# tests/test_console_encoding.py
"""
THE CLI MUST NOT DIE ON ITS OWN BANNER.

The first run of the frozen headless build failed like this:

    print("\\U0001f6f6 ASCE CONCRETE CANOE: 5-DIMENSIONAL PARAMETRIC SWEEP")
    UnicodeEncodeError: 'charmap' codec can't encode character '\\U0001f6f6'

A frozen console build on Windows gets a **cp1252** stdout.  `main.py` prints
emoji, box-drawing and block characters throughout -- the executor banner, the
New Best lines, the phase markers, the winner header -- so the failure was not
one bad line; it was the first of many.  The run died before any work started,
on both executors, every time.

This had already been met once, in `PhaseClock.report()`, and solved there by
writing that one function in ASCII.  That fixed one function.  The stream is
the right place, and `errors="replace"` is the part that matters: UTF-8 alone
still raises if the console cannot render a glyph.  A '?' in a banner costs
nothing; a dead sweep costs hours.
"""
from __future__ import annotations

import io

import main


def _cp1252_stream():
    """A stream born with the encoding a frozen Windows console hands you."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_a_cp1252_stream_really_does_reject_the_banner():
    """
    POSITIVE CONTROL.  If the fixture's stream happily accepted an emoji, every
    assertion below would pass while proving nothing -- and this repo has
    shipped a scanner that could not fail before.
    """
    st = _cp1252_stream()
    try:
        st.write(main.BANNER_GLYPH if hasattr(main, "BANNER_GLYPH")
                 else "\U0001f6f6")
        st.flush()
    except UnicodeEncodeError:
        return                                   # expected
    raise AssertionError(
        "a cp1252 stream accepted an emoji; this fixture can no longer "
        "reproduce the failure it exists to guard against")


def test_harden_stdio_makes_the_banner_printable():
    st = _cp1252_stream()
    assert st.encoding.lower() in ("cp1252", "windows-1252")
    done = main.harden_stdio([st])
    assert done == [st], "the stream was not hardened"
    assert st.encoding.lower() == "utf-8"
    assert st.errors == "replace"
    st.write("\U0001f6f6 ASCE CONCRETE CANOE")   # must not raise
    st.flush()


def test_errors_replace_not_merely_utf8():
    """
    The distinction that decides whether a run survives.  A stream set to UTF-8
    with strict errors still raises on a glyph the target cannot encode; the
    fix is only worth anything because it also sets errors.
    """
    st = _cp1252_stream()
    main.harden_stdio([st])
    assert st.errors == "replace", (
        "encoding was fixed but errors= was left strict; a console that cannot "
        "render a character would still kill the run")


def test_a_stream_that_cannot_be_reconfigured_is_survivable():
    """
    Pipes, pytest captures and already-wrapped tees have no reconfigure().
    Refusing to run because the output stream is unusual would be a worse bug
    than the one being fixed.
    """
    class Unreconfigurable:
        encoding = "cp1252"

        def write(self, _s):
            return 0

    st = Unreconfigurable()
    assert main.harden_stdio([st]) == [], "it should report nothing hardened"
    # and, crucially, it must not have raised


def test_the_cli_entry_hardens_before_it_prints():
    """
    ORDERING IS THE WHOLE POINT.  Hardening after the first print is no fix at
    all, and the first print is a banner with an emoji in it.  Checked against
    the source because the __main__ block cannot be imported.
    """
    import ast
    import os
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py"), encoding="utf-8-sig").read()
    tree = ast.parse(src)
    mains = [n for n in tree.body
             if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
    assert mains, "no `if __name__ == '__main__'` block found"
    body = mains[0].body

    def _line_of_call(name):
        for n in ast.walk(ast.Module(body=body, type_ignores=[])):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == name):
                return n.lineno
        return None

    harden_at = _line_of_call("harden_stdio")
    assert harden_at is not None, (
        "the CLI entry no longer calls harden_stdio(); the frozen build will "
        "die on its own banner again")
    sweep_at = _line_of_call("run_grand_optimization")
    assert sweep_at is not None
    assert harden_at < sweep_at, (
        f"harden_stdio() is called at line {harden_at}, after the sweep starts "
        f"at {sweep_at} -- hardening after the first print fixes nothing")
