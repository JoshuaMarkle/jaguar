# services/export_service.py
"""
Design package export — zips all deliverables into one archive.

Produces:  <timestamp>_canoe_design_package.zip
Contents:
  - session.json          : structural + race result JSON
  - report.pdf            : PDF report (if generated)
  - stress_envelope.csv   : per-station stress data
  - sweep_audit.log       : last 50 audit entries
  - regression_version.txt: mix regression provenance

No Qt imports.  Returns the zip path so the caller can open it.
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


def export_design_package(
    output_dir: str,
    structural_result: dict[str, Any] | None = None,
    race_result: dict[str, Any] | None = None,
    pdf_path: str | None = None,
    csv_paths: list[str] | None = None,
    regression_tag: str | None = None,
) -> str:
    """
    Bundle all design deliverables into a timestamped zip file.

    Parameters
    ----------
    output_dir         : Directory where the zip is written
    structural_result  : Dict from StructuralService.run_sweep()
    race_result        : Dict from the race simulator
    pdf_path           : Path to an already-generated PDF (optional)
    csv_paths          : List of CSV file paths to include (optional)
    regression_tag     : Mix regression version string

    Returns
    -------
    str — absolute path to the created zip file
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"{ts}_canoe_design_package.zip"
    zip_path = os.path.join(output_dir, zip_name)

    project_root = Path(__file__).parent.parent

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:

        # 1 — session JSON
        session: dict[str, Any] = {
            "exported_at": datetime.now().isoformat(),
            "regression_version": regression_tag or "unknown",
        }
        if structural_result:
            sr = dict(structural_result)
            # Remove non-serialisable numpy arrays — keep scalars
            for key in list(sr.keys()):
                v = sr[key]
                if hasattr(v, "tolist"):
                    sr[key] = v.tolist()
                elif not isinstance(v, (int, float, str, bool, list, dict, type(None))):
                    sr[key] = str(v)
            session["structural"] = sr
        if race_result:
            rr = {}
            for k, v in race_result.items():
                if hasattr(v, "tolist"):
                    rr[k] = v.tolist()
                elif not isinstance(v, (int, float, str, bool, list, dict, type(None))):
                    rr[k] = str(v)
                else:
                    rr[k] = v
            session["race"] = rr
        zf.writestr("session.json", json.dumps(session, indent=2, default=str))

        # 2 — PDF report
        if pdf_path and os.path.exists(pdf_path):
            zf.write(pdf_path, arcname=os.path.basename(pdf_path))

        # 3 — CSV files
        for csv_p in (csv_paths or []):
            if os.path.exists(csv_p):
                zf.write(csv_p, arcname=os.path.basename(csv_p))

        # 4 — Last 50 audit log entries
        audit_log = project_root / "sweep_audit.log"
        if audit_log.exists():
            from data.session import format_recent
            recent = format_recent(50)
            zf.writestr("sweep_audit_recent.txt", recent)

        # 5 — Regression provenance
        prov = f"Regression version: {regression_tag or 'unknown'}\nExported: {datetime.now().isoformat()}\n"
        if not regression_tag:
            try:
                from mix_database import get_mix_db
                prov = f"Regression version: {get_mix_db().get_active_version_tag()}\n"
            except ImportError:
                pass
        zf.writestr("regression_version.txt", prov)

        # 6 — Pipeline design state + any exported CAD geometry it references
        state_file = project_root / "design_state.json"
        if state_file.exists():
            zf.write(state_file, arcname="design_state.json")
            try:
                exports = json.loads(state_file.read_text(encoding="utf-8")
                                     ).get("exports") or {}
                for label, geo_path in exports.items():
                    if geo_path and os.path.exists(geo_path):
                        zf.write(geo_path,
                                 arcname=f"geometry/{os.path.basename(geo_path)}")
            except (json.JSONDecodeError, OSError):
                pass

    return str(zip_path)
