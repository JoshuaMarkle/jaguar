"""
Tests for services/hull_service.py — template validation and write_seed_hull.

Golden-file behaviour:
  - First run (no tests/fixtures/in_mlt_golden.mlt) auto-generates the snapshot
    and skips with an informational message.
  - Subsequent runs compare byte-for-byte against the snapshot.
  - To accept a known-good change: delete tests/fixtures/in_mlt_golden.mlt and
    re-run; the new output becomes the new golden.

Set REGENERATE_GOLDEN=1 in the environment to force regeneration.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "in_mlt_golden.mlt"
REGENERATE = os.environ.get("REGENERATE_GOLDEN", "0") == "1"

# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def hull_svc():
    from services.hull_service import HullService
    return HullService()


@pytest.fixture
def workdir(tmp_path):
    """Copy in_temp.mlt into a temp directory and return the path."""
    src = PROJECT_ROOT / "in_temp.mlt"
    if not src.exists():
        pytest.skip("in_temp.mlt not present in project root")
    shutil.copy(src, tmp_path / "in_temp.mlt")
    return tmp_path


@pytest.fixture
def canonical_results():
    return {
        "best_hull": {
            "factors": [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05],
            "beam": 0.61,
            "draft": 0.20,
        }
    }


CANONICAL_L_M = 18.0 * 0.3048   # 18 ft canoe → metres


# ── validate_template tests ───────────────────────────────────────────────────

class TestValidateTemplate:

    def test_real_template_passes(self):
        """The committed in_temp.mlt must pass validation with zero errors."""
        from services.hull_service import HullService
        tpl = str(PROJECT_ROOT / "in_temp.mlt")
        if not Path(tpl).exists():
            pytest.skip("in_temp.mlt not present")
        errors = HullService.validate_template(tpl)
        assert errors == [], f"Template validation failed:\n" + "\n".join(errors)

    def test_missing_file_returns_error(self, tmp_path):
        from services.hull_service import HullService
        errors = HullService.validate_template(str(tmp_path / "nonexistent.mlt"))
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_truncated_template_reports_line_count(self, tmp_path):
        """A template with wrong line count must report a line-count error."""
        from services.hull_service import HullService, _EXPECTED_LINE_COUNT

        src = PROJECT_ROOT / "in_temp.mlt"
        if not src.exists():
            pytest.skip("in_temp.mlt not present")

        with open(src, "r", newline="") as _fh:
            content = _fh.read()
        lines = content.splitlines(keepends=True)
        truncated = "".join(lines[:50])   # keep only first 50 lines
        bad_tpl = tmp_path / "bad_temp.mlt"
        bad_tpl.write_text(truncated, newline="")

        errors = HullService.validate_template(str(bad_tpl))
        line_count_err = [e for e in errors if "lines" in e.lower()]
        assert line_count_err, (
            f"Expected a line-count error for a 50-line template, got: {errors}"
        )

    def test_missing_marker_reported(self, tmp_path):
        """Removing a required comment marker must produce a specific error."""
        from services.hull_service import HullService

        src = PROJECT_ROOT / "in_temp.mlt"
        if not src.exists():
            pytest.skip("in_temp.mlt not present")

        with open(src, "r", newline="") as _fh:
            content = _fh.read()
        # Remove one required marker (replace with a different comment so line count stays same)
        modified = content.replace(
            "# Displacement Volume (cubic metres)",
            "# REMOVED FOR TEST PURPOSES ONLY",
            1,
        )
        bad_tpl = tmp_path / "bad_temp.mlt"
        bad_tpl.write_text(modified, newline="")

        errors = HullService.validate_template(str(bad_tpl))
        disp_err = [e for e in errors if "Displacement Volume" in e]
        assert disp_err, f"Expected marker-missing error, got: {errors}"


# ── write_seed_hull tests ─────────────────────────────────────────────────────

class TestWriteSeedHull:

    def test_output_file_created(self, hull_svc, canonical_results, workdir):
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        assert (workdir / "in.mlt").exists(), "in.mlt was not created"

    def test_line_count_unchanged(self, hull_svc, canonical_results, workdir):
        """Output must have the exact same number of lines as the template."""
        from services.hull_service import _EXPECTED_LINE_COUNT
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        out_bytes = (workdir / "in.mlt").read_bytes()
        # CRLF-terminated lines: split on \r\n; last element will be empty b""
        crlf_lines = out_bytes.split(b"\r\n")
        # Subtract trailing empty element from final \r\n
        n_out = len(crlf_lines) - 1 if crlf_lines[-1] == b"" else len(crlf_lines)
        assert n_out == _EXPECTED_LINE_COUNT, (
            f"Output has {n_out} CRLF lines; expected {_EXPECTED_LINE_COUNT}"
        )

    def test_crlf_line_endings(self, hull_svc, canonical_results, workdir):
        """Every line in the output must end with CRLF (Godzilla requirement)."""
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        content = (workdir / "in.mlt").read_bytes()
        # No bare LF (i.e. no \n not preceded by \r)
        bare_lf = re.search(rb"(?<!\r)\n", content)
        assert not bare_lf, "Output contains bare LF line endings (expected CRLF only)"

    def test_length_patched_correctly(self, hull_svc, canonical_results, workdir):
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        with open(workdir / "in.mlt", "r", newline="") as _fh:
            lines = _fh.read().splitlines()
        stripped = [ln.strip() for ln in lines]
        idx = stripped.index("# Length (metres)")
        written_L = float(stripped[idx + 1])
        assert written_L == pytest.approx(CANONICAL_L_M, rel=1e-4)

    def test_draft_patched_correctly(self, hull_svc, canonical_results, workdir):
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        with open(workdir / "in.mlt", "r", newline="") as _fh:
            lines = _fh.read().splitlines()
        stripped = [ln.strip() for ln in lines]
        idx = stripped.index("# Draft (metres)")
        written_T = float(stripped[idx + 1])
        assert written_T == pytest.approx(canonical_results["best_hull"]["draft"], rel=1e-4)

    def test_shape_factors_patched(self, hull_svc, canonical_results, workdir):
        """All 7 shape factors must appear in the output with the right values."""
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        with open(workdir / "in.mlt", "r", newline="") as _fh:
            lines = _fh.read().splitlines()
        stripped = [ln.strip() for ln in lines]
        idx = stripped.index("# Shape Factors: Minimum, Maximum, Seed.")
        factors = canonical_results["best_hull"]["factors"]
        for j, expected_f in enumerate(factors):
            written = stripped[idx + 1 + j]
            seed_val = float(written.split(",")[2])
            assert seed_val == pytest.approx(expected_f, rel=1e-4), (
                f"Shape factor {j}: expected {expected_f}, got {seed_val}"
            )

    def test_no_template_needed(self, hull_svc, canonical_results, tmp_path):
        """
        write_seed_hull SYNTHESISES in.mlt; it does not patch a template.

        This replaces an obsolete test that expected a ValueError when
        inTemplate.mlt was absent.  write_seed_hull used to patch that template
        in place, but now generates the whole file via generate_godzilla_input,
        so an empty working directory is a perfectly valid input -- and one
        fewer external file to go missing at run time.
        """
        assert not (tmp_path / "inTemplate.mlt").exists()
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(tmp_path))
        out = tmp_path / "in.mlt"
        assert out.exists(), "in.mlt must be written without any template"
        n = len(out.read_bytes().split(b"\r\n")) - 1
        from services.hull_service import _EXPECTED_LINE_COUNT
        assert n == _EXPECTED_LINE_COUNT

    def test_golden_file(self, hull_svc, canonical_results, workdir):
        """
        Snapshot regression test.

        On first run (or when REGENERATE_GOLDEN=1), the golden file is created
        and the test is skipped.  Subsequent runs compare byte-for-byte.
        """
        hull_svc.write_seed_hull(canonical_results, CANONICAL_L_M, str(workdir))
        with open(workdir / "in.mlt", "r", newline="") as _fh:
            actual = _fh.read()

        if REGENERATE or not GOLDEN_PATH.exists():
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(GOLDEN_PATH, "w", newline="") as _fh:
                _fh.write(actual)
            pytest.skip(
                "Golden file written to tests/fixtures/in_mlt_golden.mlt — "
                "re-run to validate"
            )

        with open(GOLDEN_PATH, "r", newline="") as _fh:
            expected = _fh.read()
        assert actual == expected, (
            "in.mlt output changed unexpectedly. "
            "If the change is intentional, delete tests/fixtures/in_mlt_golden.mlt "
            "and re-run (or set REGENERATE_GOLDEN=1)."
        )
