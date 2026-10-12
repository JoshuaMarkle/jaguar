# reporting/compliance.py
"""
ASCE Concrete Canoe Competition compliance checker.

Completely Qt-free — callable from tests or a background thread.

Limits are based on the 2026 ASCE Concrete Canoe Competition rules and
ACI 318 structural practice.  Override any limit by subclassing and
redefining the class constants before calling check_all().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ═══════════════════════════════════════════════════════════════════
#  RESULT TYPES
# ═══════════════════════════════════════════════════════════════════

class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    NA   = "N/A"


@dataclass
class ComplianceCheck:
    """Result of a single compliance evaluation."""
    name:     str          # short display name
    status:   CheckStatus
    actual:   str          # formatted measured/configured value
    limit:    str          # formatted rule limit
    detail:   str          # one-line human-readable explanation
    rule_ref: str = ""     # optional: "ASCE 2026 §3.2" etc.


@dataclass
class ComplianceReport:
    """Aggregate result of check_all()."""
    checks: List[ComplianceCheck] = field(default_factory=list)

    @property
    def n_pass(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def n_warn(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)

    @property
    def is_compliant(self) -> bool:
        """True only if zero FAIL results."""
        return self.n_fail == 0

    @property
    def summary(self) -> str:
        if self.n_fail == 0 and self.n_warn == 0:
            return "ALL CHECKS PASSED — Ready for competition submission"
        if self.n_fail == 0:
            return f"PASSED with {self.n_warn} warning(s) — Review before submission"
        return (f"{self.n_fail} DISQUALIFICATION RISK(S) — "
                f"Must be resolved before competition")


# ═══════════════════════════════════════════════════════════════════
#  COMPLIANCE CHECKER
# ═══════════════════════════════════════════════════════════════════

class ASCEComplianceChecker:
    """
    Evaluates a canoe design against ASCE competition rules.

    All numeric limits are class constants so they can be overridden
    for future competition years without changing call sites.

    Usage
    -----
        checker = ASCEComplianceChecker()
        report  = checker.check_all(
            loa_ft=20.0, freeboard_in=7.5, hull_weight_lbs=185.0,
            fos=1.5, thickness_in=0.5, freeboard_min_in=6.0,
            mix_densities=[78.0, 68.0, 62.0],
        )
        print(report.summary)
        for c in report.checks:
            print(c.status, c.name, c.detail)
    """

    # ── ASCE 2026 rule limits ────────────────────────────────────────
    MAX_LOA_FT        = 40.0    # §3.1  — canoe length overall
    MIN_LOA_FT        = 14.0    # practical minimum for competitive paddling
    ASCE_FREEBOARD_IN =  3.0    # §4.2  — absolute freeboard when loaded
    MAX_DRY_WEIGHT_LBS = 250.0  # §3.3  — dry hull weight
    TARGET_WEIGHT_LBS  = 200.0  # aspirational target for competitive weight

    # ── ACI 318 / structural practice ───────────────────────────────
    ACI_FOS_MIN       =  1.0    # FoS < 1 → guaranteed structural failure
    ACI_FOS_RECOMMENDED = 1.5   # ACI 318 allowable-stress baseline
    MIN_THICKNESS_IN  =  0.375  # 3/8 in — minimum trowel-able shell

    # ── Mix chemistry ────────────────────────────────────────────────
    MIX_DENSITY_MIN_PCF = 60.0  # empirical regression lower bound
    MIX_DENSITY_MAX_PCF = 80.0  # practical upper limit for lightweight concrete

    # ------------------------------------------------------------------
    def check_all(
        self,
        loa_ft: float,
        freeboard_in: float,
        fos: float,
        thickness_in: float,
        freeboard_min_in: float,
        hull_weight_lbs: Optional[float] = None,
        mix_densities: Optional[List[float]] = None,
    ) -> ComplianceReport:
        """
        Run all compliance checks and return a ComplianceReport.

        Parameters
        ----------
        loa_ft           : Length overall in feet
        freeboard_in     : Minimum freeboard achieved (loaded condition), inches
        fos              : Configured Factor of Safety
        thickness_in     : Shell trowel thickness, inches
        freeboard_min_in : Minimum freeboard required by the sweep config (inches)
        hull_weight_lbs  : Dry hull weight from sweep result (None → check skipped)
        mix_densities    : List of mix densities used [heavy, mid, light] pcf
        """
        checks: List[ComplianceCheck] = []

        checks.append(self._check_loa(loa_ft))
        checks.append(self._check_loa_min(loa_ft))
        checks.append(self._check_freeboard_asce(freeboard_in))
        checks.append(self._check_freeboard_config(freeboard_in, freeboard_min_in))
        checks.append(self._check_weight(hull_weight_lbs))
        checks.append(self._check_fos_absolute(fos))
        checks.append(self._check_fos_aci(fos))
        checks.append(self._check_thickness(thickness_in))

        if mix_densities:
            checks.append(self._check_mix_densities(mix_densities))

        return ComplianceReport(checks=checks)

    # ------------------------------------------------------------------
    # Individual check methods — each returns one ComplianceCheck
    # ------------------------------------------------------------------

    def _check_loa(self, loa_ft: float) -> ComplianceCheck:
        ok = loa_ft <= self.MAX_LOA_FT
        return ComplianceCheck(
            name="Maximum LOA",
            status=CheckStatus.PASS if ok else CheckStatus.FAIL,
            actual=f"{loa_ft:.2f} ft",
            limit=f"≤ {self.MAX_LOA_FT:.0f} ft",
            detail=(
                "Length overall is within the competition limit."
                if ok else
                f"Canoe is {loa_ft - self.MAX_LOA_FT:.2f} ft over the maximum — "
                "DISQUALIFICATION RISK."
            ),
            rule_ref="ASCE 2026 §3.1",
        )

    def _check_loa_min(self, loa_ft: float) -> ComplianceCheck:
        ok = loa_ft >= self.MIN_LOA_FT
        return ComplianceCheck(
            name="Minimum LOA",
            status=CheckStatus.PASS if ok else CheckStatus.WARN,
            actual=f"{loa_ft:.2f} ft",
            limit=f"≥ {self.MIN_LOA_FT:.0f} ft",
            detail=(
                "Length is adequate for competitive paddling."
                if ok else
                f"Canoe may be too short for 4-paddler events (< {self.MIN_LOA_FT} ft)."
            ),
            rule_ref="Competition practice",
        )

    def _check_freeboard_asce(self, freeboard_in: float) -> ComplianceCheck:
        ok = freeboard_in >= self.ASCE_FREEBOARD_IN
        return ComplianceCheck(
            name="ASCE Min Freeboard",
            status=CheckStatus.PASS if ok else CheckStatus.FAIL,
            actual=f"{freeboard_in:.2f} in",
            limit=f"≥ {self.ASCE_FREEBOARD_IN:.0f} in",
            detail=(
                "Freeboard meets the ASCE minimum loaded condition."
                if ok else
                f"Freeboard {freeboard_in:.2f} in is below the {self.ASCE_FREEBOARD_IN} in "
                "ASCE minimum — DISQUALIFICATION RISK."
            ),
            rule_ref="ASCE 2026 §4.2",
        )

    def _check_freeboard_config(
        self, freeboard_in: float, freeboard_min_in: float
    ) -> ComplianceCheck:
        ok = freeboard_in >= freeboard_min_in
        return ComplianceCheck(
            name="Configured Freeboard Target",
            status=CheckStatus.PASS if ok else CheckStatus.WARN,
            actual=f"{freeboard_in:.2f} in",
            limit=f"≥ {freeboard_min_in:.1f} in",
            detail=(
                f"Freeboard meets the team's {freeboard_min_in:.1f} in structural target."
                if ok else
                f"Freeboard {freeboard_in:.2f} in is below team target {freeboard_min_in:.1f} in — "
                "structural sweep may be overly optimistic."
            ),
            rule_ref="Team structural spec",
        )

    def _check_weight(self, weight_lbs: Optional[float]) -> ComplianceCheck:
        if weight_lbs is None:
            return ComplianceCheck(
                name="Dry Hull Weight",
                status=CheckStatus.NA,
                actual="(no sweep result)",
                limit=f"≤ {self.MAX_DRY_WEIGHT_LBS:.0f} lbs",
                detail="Run the structural sweep to get a weight estimate.",
                rule_ref="ASCE 2026 §3.3",
            )
        if weight_lbs > self.MAX_DRY_WEIGHT_LBS:
            return ComplianceCheck(
                name="Dry Hull Weight",
                status=CheckStatus.FAIL,
                actual=f"{weight_lbs:.1f} lbs",
                limit=f"≤ {self.MAX_DRY_WEIGHT_LBS:.0f} lbs",
                detail=(
                    f"Hull is {weight_lbs - self.MAX_DRY_WEIGHT_LBS:.1f} lbs over the "
                    "weight limit — DISQUALIFICATION RISK."
                ),
                rule_ref="ASCE 2026 §3.3",
            )
        if weight_lbs > self.TARGET_WEIGHT_LBS:
            return ComplianceCheck(
                name="Dry Hull Weight",
                status=CheckStatus.WARN,
                actual=f"{weight_lbs:.1f} lbs",
                limit=f"≤ {self.TARGET_WEIGHT_LBS:.0f} lbs (target)",
                detail=(
                    f"Hull weight {weight_lbs:.1f} lbs is legal but above the "
                    f"{self.TARGET_WEIGHT_LBS:.0f} lb competitive target."
                ),
                rule_ref="Competition practice",
            )
        return ComplianceCheck(
            name="Dry Hull Weight",
            status=CheckStatus.PASS,
            actual=f"{weight_lbs:.1f} lbs",
            limit=f"≤ {self.MAX_DRY_WEIGHT_LBS:.0f} lbs",
            detail=f"Hull weight is within the {self.MAX_DRY_WEIGHT_LBS:.0f} lb limit.",
            rule_ref="ASCE 2026 §3.3",
        )

    def _check_fos_absolute(self, fos: float) -> ComplianceCheck:
        ok = fos >= self.ACI_FOS_MIN
        return ComplianceCheck(
            name="Factor of Safety ≥ 1.0",
            status=CheckStatus.PASS if ok else CheckStatus.FAIL,
            actual=f"{fos:.2f}",
            limit=f"≥ {self.ACI_FOS_MIN:.1f}",
            detail=(
                "FoS is above the structural failure floor."
                if ok else
                f"FoS {fos:.2f} < 1.0 — structural failure is GUARANTEED."
            ),
            rule_ref="ACI 318-19 §5.3",
        )

    def _check_fos_aci(self, fos: float) -> ComplianceCheck:
        ok = fos >= self.ACI_FOS_RECOMMENDED
        return ComplianceCheck(
            name="Factor of Safety ≥ 1.5",
            status=CheckStatus.PASS if ok else CheckStatus.WARN,
            actual=f"{fos:.2f}",
            limit=f"≥ {self.ACI_FOS_RECOMMENDED:.1f}",
            detail=(
                "FoS meets the ACI 318 allowable-stress recommendation."
                if ok else
                f"FoS {fos:.2f} is below the ACI 318 recommended 1.5. "
                "Design is legal but has reduced safety margin."
            ),
            rule_ref="ACI 318-19 §5.3",
        )

    def _check_thickness(self, thickness_in: float) -> ComplianceCheck:
        ok = thickness_in >= self.MIN_THICKNESS_IN
        return ComplianceCheck(
            name="Shell Thickness",
            status=CheckStatus.PASS if ok else CheckStatus.FAIL,
            actual=f"{thickness_in:.3f} in",
            limit=f"≥ {self.MIN_THICKNESS_IN:.3f} in",
            detail=(
                f"Shell thickness {thickness_in:.3f} in is trowel-able."
                if ok else
                f"Thickness {thickness_in:.3f} in is below the {self.MIN_THICKNESS_IN} in "
                "casting minimum — structural integrity cannot be guaranteed."
            ),
            rule_ref="ASCE 2026 §3.4 / casting practice",
        )

    def _check_mix_densities(self, densities: List[float]) -> ComplianceCheck:
        out_of_range = [
            d for d in densities
            if not (self.MIX_DENSITY_MIN_PCF <= d <= self.MIX_DENSITY_MAX_PCF)
        ]
        if not out_of_range:
            return ComplianceCheck(
                name="Mix Density Range",
                status=CheckStatus.PASS,
                actual=f"{min(densities):.1f}–{max(densities):.1f} pcf",
                limit=f"{self.MIX_DENSITY_MIN_PCF:.0f}–{self.MIX_DENSITY_MAX_PCF:.0f} pcf",
                detail="All mix densities are within the regression calibration range.",
                rule_ref="Team regression model",
            )
        return ComplianceCheck(
            name="Mix Density Range",
            status=CheckStatus.WARN,
            actual=", ".join(f"{d:.1f}" for d in out_of_range) + " pcf",
            limit=f"{self.MIX_DENSITY_MIN_PCF:.0f}–{self.MIX_DENSITY_MAX_PCF:.0f} pcf",
            detail=(
                f"{len(out_of_range)} mix(es) outside the regression range — "
                "strength predictions may be extrapolated."
            ),
            rule_ref="Team regression model",
        )
