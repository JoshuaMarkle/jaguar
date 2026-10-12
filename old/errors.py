"""Shared exception policy.

ONE RULE: a programming error is never a skippable condition.

`NameError`, `AttributeError` and `TypeError` mean the code is wrong -- a name
that was never bound, a method that does not exist, an argument of the wrong
shape.  Catching them alongside genuine environmental failures turns a bug into
a routine fallback, and the caller then reports a plausible result built on
nothing.

This project has paid for that four times:

  * `fit_override` read a name that did not exist in that function; report
    generation had been dead for over a month behind a broad handler.
  * the refinement gate raised `NameError: name 'L_ft' is not defined` and it
    printed as a one-line "gate skipped", so a full sweep reported a winner the
    gate had never touched.
  * `transverse_gmt_in` returned `0.0` on any exception -- and 0.0 is a
    stability CLAIM, which the Min-GMT gate then skipped on rather than failed.
  * `_settings_source` returned `{"loaded": True}` from an exception, which
    suppressed the "CONFIG DEFAULTED" banner that would have said otherwise.

Usage, as the FIRST statement of a broad handler:

    try:
        ...
    except Exception as exc:
        domain_only(exc)          # re-raises a programming error
        ...fallback...

See CLAUDE.md, "Sentinels and Absence", for the companion rule about what a
fallback VALUE is allowed to be.
"""
from __future__ import annotations

#: Errors that always mean "this code is wrong", never "this input was bad".
PROGRAMMING_ERRORS = (NameError, AttributeError, TypeError)


def domain_only(exc: BaseException) -> None:
    """
    Re-raise ``exc`` when it is a programming error; return otherwise.

    Call it as the first statement of a broad ``except`` so the handler keeps
    covering genuine environmental failures (a missing file, an unparsable
    line, a subprocess that would not start) and stops covering bugs.
    """
    if isinstance(exc, PROGRAMMING_ERRORS):
        raise exc
