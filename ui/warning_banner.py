"""
The run-critical warnings, in the GUI, where a teammate will actually see them.

The log (run_log.py) is the full record.  This is the short list that must
reach someone who never opens it -- the test being whether a teammate still
learns their winner is boundary-pinned, or that their strength fit has two
points.

FOUR, deliberately.  A panel that lists everything trains the reader to skip
it, which is how the silent import failure survived: "added=4, skipped=0" read
as success while zero rows reached the fit.
"""
from __future__ import annotations

from typing import List

#: (key, matcher, headline) -- matched against the run's captured stdout and
#: against the result dict.  Kept small on purpose.
RUN_CRITICAL = (
    ("boundary",
     "WINNER AT SEARCH BOUNDARY",
     "The winner sits on the EDGE of the search range, so it is not a free "
     "optimum — a wider range may well find something better. Widen the "
     "range and re-run before treating this as the answer."),
    ("thin_fit",
     "PREDICTIONS",
     "The strength curve is fitted to very few lab points. Predicted "
     "strengths are an EXTRAPOLATION, not a measurement: treat weights as "
     "RELATIVE comparisons until more lab data lands."),
    ("config_defaulted",
     "CONFIG DEFAULTED",
     "canoe_suite.cfg was NOT loaded. Every parameter in this run is a "
     "built-in default, not what the file on disk says."),
    ("ungated",
     "NOT refinement-verified",
     "The refinement gate did not confirm this winner at higher resolution. "
     "Its reported utilisation may understate the converged value."),
)


def describe_boundaries(flags) -> str:
    """
    Render boundary flags as PROSE, the way the console does.

    Each flag is a dict -- {'variable', 'value', 'edge', 'physical', 'why'} --
    and str()-ing it put a raw Python dict literal in front of the user:

        (at: {'variable': 'thickness', 'value': 0.75, 'edge': 'min',
              'physical': False, 'why': 'min buildable trowel'}, {...})

    The console already printed the same information readably; the banner is
    the copy the team actually reads, so it must not be the worse one.

    A PHYSICAL limit and a SEARCH-RANGE edge mean different things and the
    distinction is the actionable part: one cannot be widened, the other is
    the whole reason to re-run.
    """
    if isinstance(flags, dict):
        flags = [flags]
    if not isinstance(flags, (list, tuple)):
        return "at: %s" % (flags,)

    movable, physical = [], []
    for f in flags:
        if not isinstance(f, dict):
            movable.append(str(f))
            continue
        try:
            where = "%s = %.3f at its %s" % (f["variable"], float(f["value"]),
                                             f.get("edge", "limit"))
        except (KeyError, TypeError, ValueError):
            movable.append(str(f))
            continue
        if f.get("physical"):
            why = f.get("why")
            physical.append(where + (" (%s)" % why if why else ""))
        else:
            movable.append(where)

    parts = []
    if movable:
        parts.append("Pinned to a range YOU set: " + "; ".join(movable)
                     + " — widen and re-run to see if something better exists.")
    if physical:
        parts.append("At a physical limit (cannot be widened): "
                     + "; ".join(physical) + ".")
    return " ".join(parts)


def scan_text(text: str) -> List[dict]:
    """Run-critical warnings present in a captured run transcript."""
    out = []
    for key, needle, headline in RUN_CRITICAL:
        if needle and needle in (text or ""):
            out.append({"key": key, "headline": headline})
    return out


def scan_result(result: dict | None) -> List[dict]:
    """
    The same warnings, read from the result dict rather than the transcript.

    Two sources because neither is complete: the transcript catches anything
    printed, and the dict catches state that was never printed at all.
    """
    out: List[dict] = []
    if not isinstance(result, dict):
        return out

    flags = result.get("boundary_flags") or result.get("boundary") or []
    if flags:
        out.append({"key": "boundary",
                    "headline": RUN_CRITICAL[0][2] + "  " + describe_boundaries(flags)})

    reg = result.get("mix_regression") or {}
    n = reg.get("n")
    if isinstance(n, int) and n <= 3:
        out.append({"key": "thin_fit",
                    "headline": RUN_CRITICAL[1][2] + f"  (fitted to n = {n})"})

    recs = result.get("gate_records")
    notes = " ".join(str(x) for x in (result.get("gate_notes") or []))
    if recs is not None and len(recs) == 0:
        out.append({"key": "ungated", "headline": RUN_CRITICAL[3][2]})
    elif "NO candidate survived" in notes:
        out.append({"key": "ungated",
                    "headline": "The refinement gate ACCEPTED NOTHING. The "
                                "winner shown did not survive refinement."})
    return out


def collect(result: dict | None = None, transcript: str = "") -> List[dict]:
    """De-duplicated run-critical warnings from both sources."""
    seen, out = set(), []
    for w in list(scan_result(result)) + list(scan_text(transcript)):
        if w["key"] in seen:
            continue
        seen.add(w["key"])
        out.append(w)
    return out


def as_html(warnings: List[dict], log_path: str | None = None) -> str:
    """Render for a Qt rich-text label. Empty string when there is nothing."""
    if not warnings and not log_path:
        return ""
    parts = []
    if warnings:
        parts.append(
            "<div style='background:#4a1c1c;border:1px solid #a33;"
            "padding:8px;'><b style='color:#ffb3b3;'>"
            "READ BEFORE USING THIS RESULT</b><ul style='margin:6px 0 0 0;'>")
        parts += [f"<li style='color:#ffd9d9;'>{w['headline']}</li>"
                  for w in warnings]
        parts.append("</ul></div>")
    if log_path:
        parts.append(
            f"<div style='margin-top:6px;color:#9ab;'>Full run log: "
            f"<a href='file:///{log_path}'>{log_path}</a></div>")
    return "".join(parts)
