"""
P0 unit tests for the ASCE compliance checker.

No Qt, no file I/O, no external binaries required.
"""
import pytest
from reporting.compliance import (
    ASCEComplianceChecker,
    ComplianceReport,
    CheckStatus,
)

# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def checker():
    return ASCEComplianceChecker()


def _passing_inputs():
    """Keyword arguments for a clearly compliant design."""
    return dict(
        loa_ft=20.0,
        freeboard_in=7.5,
        fos=1.5,
        thickness_in=0.5,
        freeboard_min_in=6.0,
        hull_weight_lbs=185.0,
        mix_densities=[78.0, 68.0, 62.0],
    )


# ── ComplianceReport aggregation ─────────────────────────────────────────────

class TestComplianceReport:

    def test_all_pass_on_good_design(self, checker):
        report = checker.check_all(**_passing_inputs())
        assert report.n_fail == 0
        assert report.is_compliant

    def test_returns_one_check_per_input(self, checker):
        report = checker.check_all(**_passing_inputs())
        # 8 base checks + 1 mix-density check = 9
        assert len(report.checks) == 9

    def test_n_pass_n_fail_n_warn_sum_to_total(self, checker):
        report = checker.check_all(**_passing_inputs())
        na_count = sum(1 for c in report.checks if c.status == CheckStatus.NA)
        assert report.n_pass + report.n_fail + report.n_warn + na_count == len(report.checks)

    def test_weight_na_when_not_provided(self, checker):
        inputs = _passing_inputs()
        inputs.pop("hull_weight_lbs")
        report = checker.check_all(**inputs)
        weight_check = next(c for c in report.checks if c.name == "Dry Hull Weight")
        assert weight_check.status == CheckStatus.NA

    def test_no_mix_check_when_densities_omitted(self, checker):
        inputs = _passing_inputs()
        inputs.pop("mix_densities")
        report = checker.check_all(**inputs)
        mix_checks = [c for c in report.checks if "Mix" in c.name]
        assert len(mix_checks) == 0


# ── LOA checks ───────────────────────────────────────────────────────────────

class TestLOAChecks:

    def test_over_max_loa_is_fail(self, checker):
        inputs = _passing_inputs()
        inputs["loa_ft"] = 41.0
        report = checker.check_all(**inputs)
        loa = next(c for c in report.checks if c.name == "Maximum LOA")
        assert loa.status == CheckStatus.FAIL

    def test_exactly_max_loa_is_pass(self, checker):
        inputs = _passing_inputs()
        inputs["loa_ft"] = ASCEComplianceChecker.MAX_LOA_FT
        report = checker.check_all(**inputs)
        loa = next(c for c in report.checks if c.name == "Maximum LOA")
        assert loa.status == CheckStatus.PASS

    def test_under_min_loa_is_warn_not_fail(self, checker):
        inputs = _passing_inputs()
        inputs["loa_ft"] = 12.0
        report = checker.check_all(**inputs)
        loa_min = next(c for c in report.checks if c.name == "Minimum LOA")
        assert loa_min.status == CheckStatus.WARN
        # Short canoe is a warning, not a disqualification
        assert report.is_compliant  # still compliant (no FAILs from this alone)


# ── Freeboard checks ─────────────────────────────────────────────────────────

class TestFreeboardChecks:

    def test_below_asce_minimum_is_fail(self, checker):
        inputs = _passing_inputs()
        inputs["freeboard_in"] = 2.0
        report = checker.check_all(**inputs)
        fb = next(c for c in report.checks if c.name == "ASCE Min Freeboard")
        assert fb.status == CheckStatus.FAIL
        assert not report.is_compliant

    def test_above_asce_min_but_below_config_is_warn(self, checker):
        inputs = _passing_inputs()
        inputs["freeboard_in"] = 4.0   # above 3 in ASCE min, below 6 in config
        inputs["freeboard_min_in"] = 6.0
        report = checker.check_all(**inputs)
        fb_asce   = next(c for c in report.checks if c.name == "ASCE Min Freeboard")
        fb_config = next(c for c in report.checks if c.name == "Configured Freeboard Target")
        assert fb_asce.status   == CheckStatus.PASS
        assert fb_config.status == CheckStatus.WARN

    def test_meeting_both_freeboard_targets_is_pass(self, checker):
        inputs = _passing_inputs()  # freeboard=7.5, min=6.0
        report = checker.check_all(**inputs)
        fb_asce   = next(c for c in report.checks if c.name == "ASCE Min Freeboard")
        fb_config = next(c for c in report.checks if c.name == "Configured Freeboard Target")
        assert fb_asce.status   == CheckStatus.PASS
        assert fb_config.status == CheckStatus.PASS


# ── Weight checks ────────────────────────────────────────────────────────────

class TestWeightChecks:

    def test_over_limit_is_fail(self, checker):
        inputs = _passing_inputs()
        inputs["hull_weight_lbs"] = 260.0
        report = checker.check_all(**inputs)
        w = next(c for c in report.checks if c.name == "Dry Hull Weight")
        assert w.status == CheckStatus.FAIL

    def test_between_target_and_limit_is_warn(self, checker):
        inputs = _passing_inputs()
        inputs["hull_weight_lbs"] = 220.0   # legal but above 200 target
        report = checker.check_all(**inputs)
        w = next(c for c in report.checks if c.name == "Dry Hull Weight")
        assert w.status == CheckStatus.WARN
        assert report.is_compliant

    def test_under_target_is_pass(self, checker):
        inputs = _passing_inputs()
        inputs["hull_weight_lbs"] = 185.0
        report = checker.check_all(**inputs)
        w = next(c for c in report.checks if c.name == "Dry Hull Weight")
        assert w.status == CheckStatus.PASS


# ── Factor of Safety checks ──────────────────────────────────────────────────

class TestFoSChecks:

    def test_below_one_is_fail(self, checker):
        inputs = _passing_inputs()
        inputs["fos"] = 0.9
        report = checker.check_all(**inputs)
        fos_abs = next(c for c in report.checks if "1.0" in c.name)
        assert fos_abs.status == CheckStatus.FAIL

    def test_between_one_and_1_5_is_warn(self, checker):
        inputs = _passing_inputs()
        inputs["fos"] = 1.2
        report = checker.check_all(**inputs)
        fos_abs = next(c for c in report.checks if "1.0" in c.name)
        fos_aci = next(c for c in report.checks if "1.5" in c.name)
        assert fos_abs.status == CheckStatus.PASS
        assert fos_aci.status == CheckStatus.WARN
        assert report.is_compliant

    def test_exactly_1_5_is_fully_passing(self, checker):
        inputs = _passing_inputs()
        inputs["fos"] = 1.5
        report = checker.check_all(**inputs)
        fos_aci = next(c for c in report.checks if "1.5" in c.name)
        assert fos_aci.status == CheckStatus.PASS


# ── Shell thickness checks ───────────────────────────────────────────────────

class TestThicknessChecks:

    def test_below_minimum_is_fail(self, checker):
        inputs = _passing_inputs()
        inputs["thickness_in"] = 0.25
        report = checker.check_all(**inputs)
        t = next(c for c in report.checks if c.name == "Shell Thickness")
        assert t.status == CheckStatus.FAIL

    def test_exactly_minimum_is_pass(self, checker):
        inputs = _passing_inputs()
        inputs["thickness_in"] = ASCEComplianceChecker.MIN_THICKNESS_IN
        report = checker.check_all(**inputs)
        t = next(c for c in report.checks if c.name == "Shell Thickness")
        assert t.status == CheckStatus.PASS


# ── Mix density checks ───────────────────────────────────────────────────────

class TestMixDensityChecks:

    def test_in_range_is_pass(self, checker):
        inputs = _passing_inputs()
        inputs["mix_densities"] = [78.0, 68.0, 62.0]
        report = checker.check_all(**inputs)
        m = next(c for c in report.checks if "Mix" in c.name)
        assert m.status == CheckStatus.PASS

    def test_any_out_of_range_is_warn(self, checker):
        inputs = _passing_inputs()
        inputs["mix_densities"] = [85.0, 68.0, 50.0]   # both extremes out of range
        report = checker.check_all(**inputs)
        m = next(c for c in report.checks if "Mix" in c.name)
        assert m.status == CheckStatus.WARN
        # Warn — not a disqualification
        assert report.is_compliant


# ── Summary / is_compliant ───────────────────────────────────────────────────

class TestSummaryStrings:

    def test_zero_fail_zero_warn_gives_all_passed(self, checker):
        report = checker.check_all(**_passing_inputs())
        assert "ALL CHECKS PASSED" in report.summary

    def test_fail_gives_disqualification_risk_in_summary(self, checker):
        inputs = _passing_inputs()
        inputs["loa_ft"] = 45.0
        report = checker.check_all(**inputs)
        assert "DISQUALIFICATION" in report.summary

    def test_is_compliant_false_when_any_fail(self, checker):
        inputs = _passing_inputs()
        inputs["freeboard_in"] = 1.0   # below ASCE minimum
        report = checker.check_all(**inputs)
        assert not report.is_compliant
