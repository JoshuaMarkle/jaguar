"""
What code is this, actually?

A run once failed with a NameError on a symbol that does not exist anywhere in
the worker path, alongside a diagnostic message this tree had already replaced.
Both pointed at the run being on a DIFFERENT commit -- a stale checkout, or a
long-lived GUI process still executing what it imported at startup -- but
nothing in the output said so, and establishing it took reading three files.

So every sweep now stamps its own code version next to the run id.  One glance
answers "is this stale?", which is the cheapest possible fix for the most
expensive kind of confusion: debugging physics that was never running.

Resolution order, and why:
  1. A written build stamp (build_stamp.txt / _BUILD_SHA).  A frozen
     PyInstaller build has no .git, so packaging writes this.
  2. `git rev-parse` in the source tree.  Authoritative for a checkout, and it
     also reports whether the tree is DIRTY -- uncommitted edits mean the SHA
     alone does not identify what ran.
  3. "unknown".  Never raise: a version stamp must not be able to break a
     sweep it only annotates.
"""
from __future__ import annotations

import os
import subprocess

_CACHED: str | None = None
_ROOT = os.path.dirname(os.path.abspath(__file__))

#: Set by the packaging step for frozen builds, which have no .git to consult.
_BUILD_SHA = ""


def _from_build_file() -> str:
    for name in ("build_stamp.txt", "BUILD_SHA"):
        path = os.path.join(_ROOT, name)
        try:
            with open(path, encoding="utf-8") as fh:
                txt = fh.read().strip()
            if txt:
                return txt.splitlines()[0][:64]
        except OSError:
            continue
    return ""


def _from_git() -> str:
    def _git(*args):
        return subprocess.run(("git",) + args, cwd=_ROOT, capture_output=True,
                              text=True, timeout=10)

    try:
        r = _git("rev-parse", "--short", "HEAD")
        if r.returncode != 0:
            return ""
        sha = r.stdout.strip()
        if not sha:
            return ""
        # A dirty tree means the SHA does NOT identify what is running, which is
        # exactly the ambiguity this stamp exists to remove.  Say so.
        st = _git("status", "--porcelain")
        if st.returncode == 0:
            dirty = [ln[3:].strip() for ln in st.stdout.splitlines() if ln.strip()]
            # Sweep artefacts and local settings are always dirty here and say
            # nothing about the CODE; only source changes make the SHA a lie.
            code = [f for f in dirty
                    if f.endswith((".py", ".cfg", ".yaml", ".yml"))]
            if code:
                sha += f"+dirty({len(code)})"
        br = _git("rev-parse", "--abbrev-ref", "HEAD")
        if br.returncode == 0 and br.stdout.strip() not in ("", "HEAD"):
            sha += f" {br.stdout.strip()}"
        return sha
    except (OSError, subprocess.SubprocessError):
        return ""


def code_version(refresh: bool = False) -> str:
    """A short string identifying the running code.  Never raises."""
    global _CACHED
    if _CACHED is not None and not refresh:
        return _CACHED
    try:
        _CACHED = _BUILD_SHA or _from_build_file() or _from_git() or "unknown"
    except Exception:
        _CACHED = "unknown"
    return _CACHED
