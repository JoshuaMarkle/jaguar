# data/session.py
"""
Audit log for sweep runs.

Every call to `log_run()` appends one JSON line to  sweep_audit.log
(next to the project file or in the project root).  The log is append-only
so it survives partial writes and can be grep-d by any team member.

No Qt dependencies.  Safe to call from a background thread.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

_LOG_FILE = Path(__file__).parent.parent / "sweep_audit.log"


def _hash_inputs(inputs: dict) -> str:
    """SHA-256 of the JSON-serialised inputs (truncated to 12 chars)."""
    raw = json.dumps(inputs, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def log_run(
    run_type: str,
    inputs: dict[str, Any],
    result_summary: dict[str, Any],
    *,
    log_path: str | None = None,
) -> str:
    """
    Append a structured audit record to sweep_audit.log.

    Parameters
    ----------
    run_type       : "structural_sweep" | "race_sim" | "report_gen" | …
    inputs         : Dict of user-supplied parameters (will be hashed)
    result_summary : Dict of scalar result metrics (weight, Fn, fitness, …)
    log_path       : Override the default log file location

    Returns
    -------
    str — the input hash (12 hex chars) that identifies this run
    """
    path = Path(log_path) if log_path else _LOG_FILE
    input_hash = _hash_inputs(inputs)

    record = {
        "ts":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host":       socket.gethostname(),
        "run_type":   run_type,
        "input_hash": input_hash,
        "inputs":     inputs,
        "result":     result_summary,
    }

    line = json.dumps(record, default=str) + "\n"
    with _LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    return input_hash


def read_recent(n: int = 20, *, log_path: str | None = None) -> list[dict]:
    """Return the last *n* audit records (newest last), or [] if log absent."""
    path = Path(log_path) if log_path else _LOG_FILE
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records[-n:]


# ═══════════════════════════════════════════════════════════════════
#  DESIGN STATE PERSISTENCE  (pipeline: seed → godzilla → structural)
# ═══════════════════════════════════════════════════════════════════

_DESIGN_STATE_FILE = Path(__file__).parent.parent / "design_state.json"


def save_design_state(state, *, path: str | None = None) -> str:
    """
    Serialise a domain.models.CanoeDesignState to design_state.json.
    Atomic write (tmp + replace) so a crash never leaves a truncated file.
    Returns the path written.
    """
    target = Path(path) if path else _DESIGN_STATE_FILE
    payload = json.dumps(state.to_dict(), indent=2, default=str)
    tmp = target.with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    return str(target)


def load_design_state(*, path: str | None = None):
    """
    Load the persisted CanoeDesignState, or return a fresh empty one when
    the file is absent or unreadable (never raises).
    """
    from domain.models import CanoeDesignState
    target = Path(path) if path else _DESIGN_STATE_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            return CanoeDesignState.from_dict(json.load(fh))
    except (OSError, json.JSONDecodeError, ValueError):
        return CanoeDesignState()


def format_recent(n: int = 10, *, log_path: str | None = None) -> str:
    """Return a human-readable table of the last *n* runs."""
    records = read_recent(n, log_path=log_path)
    if not records:
        return "(no audit log entries yet)"
    lines = [f"{'Timestamp':<22} {'Type':<20} {'Hash':<14} {'Summary'}"]
    lines.append("-" * 90)
    for r in records:
        ts   = r.get("ts", "?")[:19]
        rt   = r.get("run_type", "?")[:18]
        h    = r.get("input_hash", "?")
        res  = r.get("result", {})
        summary = "  ".join(f"{k}={v}" for k, v in list(res.items())[:4])
        lines.append(f"{ts:<22} {rt:<20} {h:<14} {summary}")
    return "\n".join(lines)
