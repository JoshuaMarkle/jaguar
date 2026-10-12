# asset_paths.py
"""
ONE resolver for the data files this application reads.

WHY THIS MODULE EXISTS. Five shipped bugs, all the same defect: an asset
resolved relative to something a frozen build does not have.

    canoe_suite.cfg   -> every frozen run used built-in defaults
    concrete_mixes.db -> sqlite CREATED an empty one; 0 of 20 lab results
    off1.mlt          -> a DEFAULT hull was generated; a generic 20.0 ft boat
    out.mlt           -> a 35-byte stub supplied length and draft, which set
                         the waterline, the depth floor and the whole grid
    reports_dir       -> PDFs written INSIDE the application bundle

Every one was silent. Every one produced numbers that looked entirely
plausible. Four survived for months, and each was found by running the shipped
artifact rather than the source -- because from source the working directory
IS the repo and every file is simply there.

The proximate cause was a variable named `cwd` that held
`os.path.dirname(os.path.abspath(__file__))` -- the MODULE's directory, not the
working directory. Under PyInstaller that is `_internal`, a folder no user
would think to look in and that a reinstall replaces. The name invited the
mistake and the mistake was made four times.

THE ORDER, and why it is this order:

  1. beside the executable  -- for a frozen build this is the folder the team
     actually has, so it is the copy they can edit or replace. In the
     interpreter it is wherever python.exe lives, which harmlessly does not
     exist, so (3) wins.
  2. the working directory  -- how a CLI run picks up files sitting next to
     whatever it was pointed at.
  3. beside this module     -- the repo root, or the bundled copy inside
     `_internal`. LAST, deliberately: a bundled default that outranked a
     user's own file would be the same defect in new clothes -- settings and
     geometry that look applied and are not.
"""
from __future__ import annotations

import os
import sys

#: The assets a run needs. Anything in this list is checked by
#: `audit_assets()` at startup, so a missing one is loud immediately rather
#: than inferred from a strange weight three hours later.
EXPECTED_ASSETS = (
    ("canoe_suite.cfg", "run settings (factor of safety, densities, crews)"),
    ("concrete_mixes.db", "mix database (lab results and the strength fits)"),
    ("off1.mlt", "hull offsets (the shape every section is cut from)"),
    ("out.mlt", "hull dimensions (length and draft -> waterline, depth floor)"),
)


def asset_search_paths(name):
    """Absolute candidates for ``name``, most-preferred first, deduplicated."""
    out = []
    if getattr(sys, "frozen", False):
        out.append(os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), name))
    out.append(os.path.join(os.path.abspath(os.getcwd()), name))
    out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    seen, uniq = set(), []
    for c in out:
        k = os.path.normcase(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def resolve_asset(name):
    """
    The first candidate that EXISTS, or None.

    Returning None rather than a guessed path is deliberate: callers that
    fabricate a fallback must do so knowingly and say so. A path that merely
    looks plausible is how a 35-byte invented `out.mlt` came to define a
    hull's length.
    """
    return next((c for c in asset_search_paths(name) if os.path.exists(c)),
                None)


def audit_assets(names=None):
    """
    [(name, description, resolved_path_or_None, candidates)] for every asset.

    The data behind the startup self-check. Separated from the printing so a
    test can assert on it without parsing console output.
    """
    rows = []
    wanted = (EXPECTED_ASSETS if names is None
              else tuple((n, "") for n in names))
    for name, why in wanted:
        rows.append((name, why, resolve_asset(name), asset_search_paths(name)))
    return rows


def print_asset_audit(names=None, stream=None):
    """
    Say where every asset came from, at startup, every run.

    NOT conditional on something looking wrong: the failure mode here is a
    number that looks RIGHT. A default hull, an empty database and a defaulted
    config all produce clean, reproducible, plausible output. The only way to
    catch them is to state the provenance unprompted.

    Returns the count of MISSING assets so a caller can decide how loud to be.
    """
    out = stream if stream is not None else sys.stdout
    rows = audit_assets(names)
    missing = [r for r in rows if r[2] is None]
    print("  " + "-" * 68, file=out)
    print("  DATA FILES IN USE", file=out)
    for name, why, path, cands in rows:
        if path is None:
            print(f"    !! {name:<18s} NOT FOUND -- looked in:", file=out)
            for c in cands:
                print(f"         {c}", file=out)
            if why:
                print(f"       this supplies: {why}", file=out)
        else:
            print(f"    {name:<18s} {path}", file=out)
    if missing:
        print("", file=out)
        print(f"  !! {len(missing)} FILE(S) MISSING. Whatever the run does "
              f"next, it is", file=out)
        print("  !! not describing the inputs you think it is. Put the file(s) "
              "beside", file=out)
        print("  !! the executable and run again.", file=out)
    print("  " + "-" * 68, file=out)
    return len(missing)
