# tests/test_reference_hull_integrity.py
"""
The tracked reference hull (in.mlt, off1.mlt, out.mlt) is the geometry every
sweep validates against.  Running Godzilla (mlt933w.exe) or a sweep/render
script IN THE REPO ROOT overwrites these files in place, and a crashed or
partial run leaves them corrupted -- off1.mlt and out.mlt have been observed
byte-identical, which silently breaks every downstream offset parse.

This guard pins their SHA-256 (tests/fixtures/reference_hull.sha256) so any
such corruption fails CI before it can reach main, instead of being caught by
eye.  The only legitimate way the hashes change is an intentional hull
redesign, which regenerates the manifest in the same commit -- so the manifest
doubles as the explicit "yes, I meant to change the hull" gate.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).parent / "fixtures" / "reference_hull.sha256"


def _load_manifest():
    entries = []
    for line in MANIFEST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split()
        entries.append((name.lstrip("*"), digest))
    return entries


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_lists_all_three_reference_files():
    names = {n for n, _ in _load_manifest()}
    assert names == {"in.mlt", "off1.mlt", "out.mlt"}, (
        f"reference-hull manifest should pin exactly the three tracked files, "
        f"got {names}")


@pytest.mark.parametrize("name,expected", _load_manifest())
def test_reference_hull_unchanged(name, expected):
    path = ROOT / name
    assert path.exists(), f"reference hull file missing: {name}"
    actual = _sha256(path)
    assert actual == expected, (
        f"\nReference hull '{name}' does not match its pinned SHA-256.\n"
        f"  expected {expected}\n  actual   {actual}\n\n"
        "If a sweep or Godzilla run corrupted it (these overwrite the file in\n"
        "place when run in the repo root), restore it:\n"
        f"    git checkout -- {name}\n\n"
        "If you INTENTIONALLY re-designed the reference hull, regenerate the\n"
        "manifest in the SAME commit:\n"
        "    sha256sum in.mlt off1.mlt out.mlt "
        "> tests/fixtures/reference_hull.sha256\n")
