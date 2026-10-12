# data/sweep_presets.py
"""
Named sweep-configuration presets, persisted across sessions.

A preset is just a flat dict of the Structural-sweep UI's widget values (search
bounds, step sizes, load cases, solver precision, ...).  They are stored as a
single JSON object ``{name: settings}`` in ``sweep_presets.json`` next to the
project root, so a user's custom search boundaries survive app restarts.

No Qt dependencies -- the UI layer serialises/deserialises its own widgets and
hands plain dicts in and out here.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_PRESET_FILE = Path(__file__).parent.parent / "sweep_presets.json"


def _path() -> Path:
    return _PRESET_FILE


def load_all() -> dict[str, dict[str, Any]]:
    """Return every saved preset as ``{name: settings}`` (empty on any error)."""
    p = _path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        # keep only well-formed {name: {settings}} entries
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save_all(presets: dict[str, dict[str, Any]]) -> None:
    """Persist the whole preset map atomically (temp file + replace)."""
    p = _path()
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(presets, fh, indent=2, sort_keys=True, default=str)
        os.replace(tmp, p)


def names() -> list[str]:
    """Preset names, case-insensitively sorted for a stable dropdown order."""
    return sorted(load_all().keys(), key=str.lower)


def get(name: str) -> dict[str, Any] | None:
    """Settings for ``name``, or None if it does not exist."""
    return load_all().get(name)


def save(name: str, settings: dict[str, Any]) -> None:
    """Create or overwrite the preset ``name`` with ``settings``."""
    name = (name or "").strip()
    if not name:
        raise ValueError("preset name cannot be empty")
    presets = load_all()
    presets[name] = dict(settings)
    save_all(presets)


def delete(name: str) -> bool:
    """Remove ``name``; return True if it existed."""
    presets = load_all()
    if name in presets:
        del presets[name]
        save_all(presets)
        return True
    return False
