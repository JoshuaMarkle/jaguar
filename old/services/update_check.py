# services/update_check.py
"""
Optional auto-update check against a GitHub releases JSON endpoint.

Fetches the latest release tag asynchronously and compares to
APP_VERSION in config.py (or a fallback constant).  Shows a
non-blocking notification if a newer release is available.

No Qt imports in the network fetch — only in the optional notify helper.
"""
from __future__ import annotations

import threading
from typing import Callable

APP_VERSION = "1.0.0"          # updated at release time
_RELEASES_URL = (
    "https://api.github.com/repos/uvaconcretecanoe/"
    "canoe-optimizer/releases/latest"
)
_TIMEOUT_S = 5


def _parse_semver(tag: str) -> tuple[int, ...]:
    """Convert 'v1.2.3' or '1.2.3' to (1, 2, 3).  Non-numeric parts → 0."""
    tag = tag.lstrip("v")
    parts = tag.split(".")
    result = []
    for p in parts[:3]:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    while len(result) < 3:
        result.append(0)
    return tuple(result)


def check_for_update(
    callback: Callable[[str, str], None],
    *,
    releases_url: str = _RELEASES_URL,
) -> None:
    """
    Spawn a background thread that fetches the latest release tag.

    If a newer version is available, *callback(current, latest)* is invoked
    from the worker thread.  On any error the function returns silently.

    Parameters
    ----------
    callback       : called with (current_version, latest_version) when an
                     update is available.  Caller must use QTimer.singleShot
                     if it needs to update the UI.
    releases_url   : GitHub API URL (overridable for testing)
    """
    def _fetch():
        try:
            import urllib.request
            import json
            req = urllib.request.Request(
                releases_url,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "canoe-optimizer-update-check/1.0"},
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            latest_tag = data.get("tag_name", "")
            if not latest_tag:
                return
            if _parse_semver(latest_tag) > _parse_semver(APP_VERSION):
                callback(APP_VERSION, latest_tag.lstrip("v"))
        except Exception:                    # noqa: BLE001
            # Offline, rate-limited, repo private/missing (404): the check is
            # advisory, so any failure is silent rather than a thread traceback.
            pass

    t = threading.Thread(target=_fetch, daemon=True, name="update-check")
    t.start()
