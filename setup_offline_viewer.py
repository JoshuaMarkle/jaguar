#!/usr/bin/env python3
"""
setup_offline_viewer.py
=======================
One-time setup script: downloads Three.js r134 and OrbitControls.js from their
respective CDNs and saves them into ui/static/ so the hull 3-D viewer works
completely offline.

Run once from the project root while connected to the internet:

    python setup_offline_viewer.py

After that the app never needs network access for the 3-D viewer.
"""
import pathlib
import sys
import urllib.request

_STATIC = pathlib.Path(__file__).parent / "ui" / "static"

_DOWNLOADS = {
    "three.min.js": (
        "https://cdn.jsdelivr.net/npm/three@0.134.0/build/three.min.js"
    ),
    "OrbitControls.js": (
        "https://cdn.jsdelivr.net/npm/three@0.134.0/examples/js/controls/OrbitControls.js"
    ),
}


def _sizeof(path: pathlib.Path) -> str:
    kb = path.stat().st_size / 1024
    return f"{kb:.0f} KB" if kb < 1024 else f"{kb/1024:.1f} MB"


def main() -> int:
    print(f"Target directory: {_STATIC}\n")
    _STATIC.mkdir(parents=True, exist_ok=True)

    all_ok = True
    for filename, url in _DOWNLOADS.items():
        dest = _STATIC / filename
        if dest.exists():
            print(f"  OK   {filename} already present  ({_sizeof(dest)})")
            continue

        print(f"  DL   {filename}")
        print(f"       {url}")
        tmp = dest.with_suffix(".tmp")
        try:
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(dest)
            print(f"       saved  {_sizeof(dest)}")
        except Exception as exc:
            print(f"       ERROR: {exc}")
            if tmp.exists():
                tmp.unlink()
            all_ok = False

    if all_ok:
        print("\nDone -- Three.js is ready for offline use.")
        print("You can delete this script or keep it for future updates.")
        return 0
    else:
        print("\nOne or more downloads failed. Check your internet connection.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
