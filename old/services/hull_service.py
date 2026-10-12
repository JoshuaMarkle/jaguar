# services/hull_service.py
"""
Manages hull file I/O and the Godzilla/Michlet Fortran subprocess.

No Qt imports — callable from tests or a background thread.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

from domain.models import HullHydrostatics
from services.godzilla_io import generate_godzilla_input
from errors import domain_only

# ── Template validation constants ────────────────────────────────────────────

_EXPECTED_LINE_COUNT = 299

# Comment markers that write_seed_hull patches (exact-match, stripped)
_REQUIRED_MARKERS: list[str] = [
    "# Offsets",
    "# Displacement Volume (cubic metres)",
    "# Length (metres)",
    "# Draft (metres)",
    "# Number of Ships (integer: min 3, max 120)",
    "# Design Speeds and Weightings (one pair per design speed)",
    "# Minimum Displacement Overall",
    "# Maximum Displacement Overall",
    "# Minimum Length Overall",
    "# Maximum Length Overall",
    "# Minimum Width Overall",
    "# Maximum Width Overall",
    "# Minimum Displacement",
    "# Maximum Displacement",
    "# Minimum Length (refer also to Min LOA and Max LOA)",
    "# Maximum Length (refer also to Min LOA and Max LOA)",
    "# Minimum Draft",
    "# Maximum Draft",
    "# Minimum Beam (refer also to Min WOA and Max WOA)",
    "# Maximum Beam (refer also to Min WOA and Max WOA)",
    "# Shape Factors: Minimum, Maximum, Seed.",
]

# Markers matched with startswith (Michlet adds parenthetical limits that vary)
_REQUIRED_PREFIXES: list[str] = [
    "# Number of Offset Stations",
    "# Number of Offset Waterlines",
]

# Physical sanity bounds for a concrete canoe hull
_BOUNDS = {
    "L_m":    (1.0, 15.0),
    "T_m":    (0.01, 1.0),
    "B_m":    (0.1, 3.0),
    "vol_m3": (0.01, 5.0),
}


class HullService:
    """
    Handles:
    - Launching mlt933w.exe (Godzilla) and waiting for completion
    - Writing an optimised seed hull into in.mlt from CanoePerformance results
    - Generating fallback hull files when Godzilla output is unavailable
    """

    # ── Template validation ───────────────────────────────────────────────────

    @staticmethod
    def validate_template(tpl_path: str) -> list[str]:
        """
        Validate that *tpl_path* is a well-formed Michlet template.

        Returns a list of human-readable error strings; an empty list means
        the template is good to use.
        """
        errors: list[str] = []

        if not os.path.exists(tpl_path):
            return [f"Template not found: {tpl_path}"]

        with open(tpl_path, "r", newline="") as fh:
            lines = fh.readlines()

        n = len(lines)
        if n != _EXPECTED_LINE_COUNT:
            errors.append(
                f"Template has {n} lines; expected {_EXPECTED_LINE_COUNT}. "
                "The file may be truncated, corrupted, or from a different Michlet version."
            )

        stripped = [ln.strip() for ln in lines]

        for marker in _REQUIRED_MARKERS:
            if marker not in stripped:
                errors.append(f"Required section missing from template: {marker!r}")

        for prefix in _REQUIRED_PREFIXES:
            if not any(s.startswith(prefix) for s in stripped):
                errors.append(f"Required section prefix missing from template: {prefix!r}")

        # Each required marker must have a non-empty data line immediately after it
        for i, s in enumerate(stripped):
            if s in _REQUIRED_MARKERS and i + 1 < n:
                next_line = stripped[i + 1]
                if not next_line or next_line.startswith("#"):
                    errors.append(
                        f"Section {s!r} at line {i+1} has no data line immediately after it."
                    )

        return errors

    # ── Godzilla launcher ─────────────────────────────────────────────────────

    # Files Godzilla needs beside it to start.  alleg42.dll is the Allegro GUI
    # library — its presence is why mlt933w.exe is a windowed program, not a
    # headless batch tool.
    _RUNTIME_FILES = ("mlt933w.exe", "alleg42.dll", "libstdc++.dll",
                      "inTemplate.mlt")
    # The only Godzilla outputs any app code reads.  Everything else it drops
    # (off2-5.mlt, drag.mlt, godzout.mlt, *.log, ...) is scratch.
    _CONSUMED_OUTPUTS = ("off1.mlt", "out.mlt")
    SCRATCH_DIRNAME = "godzilla_run"

    def launch_godzilla(
        self,
        cwd: str,
        stop_event: Optional[threading.Event] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        progress_fn: Optional[Callable[[float, str], None]] = None,
        scratch: bool = True,
    ) -> bool:
        """
        Launch ``mlt933w.exe -godz`` and block until it finishes.

        Returns True on success, False on failure or stop.
        stdout and stderr from the process are captured and forwarded to *log_fn*.

        ISOLATION (scratch=True, the default): Godzilla runs in a scratch
        subdirectory and its outputs are promoted back into *cwd* ONLY on a
        clean exit.  It litters its working directory with a dozen files and
        overwrites off1.mlt / out.mlt in place -- which are the TRACKED
        reference hull.  Running it directly in the project root means any
        crash, timeout, or user-cancelled run leaves the reference geometry
        half-written: observed in practice as off1.mlt and out.mlt ending up
        byte-identical, which silently breaks every downstream sweep.
        Promoting only on success makes a failed run a no-op instead.

        Pass scratch=False to run directly in *cwd* (the legacy behaviour).
        """
        _log = log_fn or print

        exe = os.path.join(cwd, "mlt933w.exe")
        if not os.path.exists(exe):
            _log("mlt933w.exe not found — skipping Godzilla.")
            return False

        _log("[HullService] Launching Godzilla physics engine...")
        if progress_fn:
            progress_fn(0.1, "Running Godzilla...")

        run_dir = cwd
        if scratch:
            run_dir = os.path.join(cwd, self.SCRATCH_DIRNAME)
            os.makedirs(run_dir, exist_ok=True)
            for _f in self._RUNTIME_FILES:
                _src = os.path.join(cwd, _f)
                if os.path.exists(_src):
                    shutil.copy2(_src, os.path.join(run_dir, _f))
            # Carry the prepared in.mlt in; it is the actual design input.
            _src_in = os.path.join(cwd, "in.mlt")
            if os.path.exists(_src_in):
                shutil.copy2(_src_in, os.path.join(run_dir, "in.mlt"))
            exe = os.path.join(run_dir, "mlt933w.exe")
            _log(f"[HullService] Godzilla scratch dir: {run_dir}")

        try:
            # Only write a default in.mlt if none exists yet; write_seed_hull
            # already wrote a parametric one when the race-sim path was used.
            _in_mlt = os.path.join(run_dir, 'in.mlt')
            if not os.path.exists(_in_mlt):
                generate_godzilla_input(_in_mlt)
            proc = subprocess.Popen(
                [exe, "-godz"],
                cwd=run_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            while proc.poll() is None:
                if stop_event and stop_event.is_set():
                    proc.terminate()
                    _log("Godzilla stopped by user.")
                    return False
                time.sleep(0.5)

            stdout_bytes, stderr_bytes = proc.communicate()
            captured_out = stdout_bytes.decode("ascii", errors="replace").strip()
            captured_err = stderr_bytes.decode("ascii", errors="replace").strip()
            if captured_out:
                _log(f"[Godzilla stdout] {captured_out}")
            if captured_err:
                _log(f"[Godzilla stderr] {captured_err}")

            if proc.returncode == 0:
                # Promote outputs ONLY on a clean exit. A crashed or cancelled
                # run therefore cannot leave a half-written reference hull.
                if scratch:
                    promoted = []
                    for _f in self._CONSUMED_OUTPUTS:
                        _out = os.path.join(run_dir, _f)
                        if os.path.exists(_out):
                            shutil.copy2(_out, os.path.join(cwd, _f))
                            promoted.append(_f)
                    if promoted:
                        _log("[HullService] Promoted from scratch: "
                             + ", ".join(promoted))
                    else:
                        _log("[HullService] Godzilla exited 0 but produced no "
                             f"{'/'.join(self._CONSUMED_OUTPUTS)}; reference "
                             "hull left untouched.")
                        return False
                _log("Godzilla complete.")
                return True

            # Collect any Michlet log files for additional context
            err_msgs: list[str] = []
            for log_file in ["firm.log", "michlet.err", "error.log"]:
                log_path = os.path.join(run_dir, log_file)
                if os.path.exists(log_path):
                    with open(log_path, "r") as lf:
                        content = lf.read().strip()
                    if content:
                        err_msgs.append(f"[{log_file}] {content}")
            if err_msgs:
                _log("Godzilla log files: " + " | ".join(err_msgs))

            _log(
                f"Godzilla exited with code {proc.returncode}. "
                + (f"Details: {' | '.join(err_msgs)}" if err_msgs else "No log details found.")
            )
            return False

        except Exception as exc:
            domain_only(exc)   # a bug is not a skippable condition
            _log(f"Godzilla exception: {exc}")
            return False

    # ── Seed-hull writer ──────────────────────────────────────────────────────

    def write_seed_hull(
        self,
        results: dict,
        hull_length_m: float,
        cwd: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> HullHydrostatics:
        """
        Write an optimised Michlet seed hull to ``in.mlt``.

        ``results`` must contain ``"best_hull"`` with keys ``factors``,
        ``beam``, and ``draft`` (as returned by hull_matcher.match_hull).

        Returns the HullHydrostatics for the final scaled hull.

        Raises ValueError if the computed hull values are outside physically
        sensible bounds (see ``_BOUNDS``).

        NOTE: this does NOT read or validate ``inTemplate.mlt``.  It used to
        patch that template in place; it now synthesises the whole file via
        ``generate_godzilla_input``, so there is no template to be missing or
        malformed and no template validation to perform.  ``validate_template``
        remains as a standalone check for callers that do consume a template.
        """
        from hull_generator import generate_hull, compute_hydrostatics
        from physics.constants import WATER_RHO, GRAVITY

        _log = log_fn or print

        out_path = os.path.join(cwd, "in.mlt")

        hull = results["best_hull"]
        factors = hull["factors"]
        L = hull_length_m
        T_opt = hull["draft"]

        # ── 1. Binary-search beam to hit target displacement volume ──────────
        target_vol = 0.496
        B_lo, B_hi = 0.5, 1.2
        for _ in range(30):
            B_mid = (B_lo + B_hi) / 2.0
            test_offsets = generate_hull(factors, L, B_mid, T_opt, 51, 31)
            hydro = compute_hydrostatics(test_offsets, L, T_opt, WATER_RHO, GRAVITY)
            if hydro["volume"] < target_vol:
                B_lo = B_mid
            else:
                B_hi = B_mid

        B_scaled = (B_lo + B_hi) / 2.0
        offsets = generate_hull(factors, L, B_scaled, T_opt, 51, 31)
        hydro = compute_hydrostatics(offsets, L, T_opt, WATER_RHO, GRAVITY)

        vol, beam = hydro["volume"], hydro["beam"]

        # ── 2. Validate physical bounds ──────────────────────────────────────
        bound_errors: list[str] = []
        for name, value, (lo, hi) in [
            ("L_m", L, _BOUNDS["L_m"]),
            ("T_m", T_opt, _BOUNDS["T_m"]),
            ("B_m", beam, _BOUNDS["B_m"]),
            ("vol_m3", vol, _BOUNDS["vol_m3"]),
        ]:
            if not (lo <= value <= hi):
                bound_errors.append(
                    f"{name} = {value:.4f} is outside allowed range [{lo}, {hi}]"
                )
        if bound_errors:
            msg = "Hull values out of physical range:\n  " + "\n  ".join(bound_errors)
            _log(f"[HullService] {msg}")
            raise ValueError(msg)

        # ── 3. Write in.mlt via the IO service (guaranteed clean CRLF) ───────
        # design_speeds/-weights are PARALLEL LISTS (up to 3 pairs), not scalars.
        # A single pair at the 3.0 m/s race speed, weight 1.0, is what the
        # seed hull is optimised for -- matching the golden in.mlt:
        #     # Number of Design Speeds ... 1
        #     # Design Speeds and Weightings ... 3.0,1.0
        generate_godzilla_input(
            filename=out_path,
            L=L,
            beam=beam,
            T=T_opt,
            vol=vol,
            factors=factors,
            design_speeds=[3.0],
            design_weights=[1.0],
            n_ships=40,
        )

        _log(f"   Vol={vol:.4f} m³ | L={L:.3f} m  T={T_opt:.3f} m  B={beam:.3f} m")
        return hydro, offsets
