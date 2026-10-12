# reporting/report_generator.py
"""
ASCE Concrete Canoe — Technical Submittal PDF.

Generates a complete PDF compiling:
  1. Cover page
  2. Hull geometry
  3. ASCE/ACI Compliance (reporting/compliance.py)
  4. Structural analysis — moment, shear, pour zones, stress (charts embedded)
  5. Sensitivity analysis (parameter table + key sensitivities)
  6. Pour schedule / manufacturing
  7. Concrete mix formulations
  8. Wave resistance validation
  9. Safety factor summary

Charts are rendered to in-memory PNG buffers (io.BytesIO) — no temp files.
The footer cites the mix-regression version used for the structural run.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from fpdf import FPDF

import config
from config import (
    FACTOR_OF_SAFETY, DYNAMIC_LOAD_FACTOR, MANEUVER_COUPLING_K,
    get_mix_db,
)
from reporting.compliance import ASCEComplianceChecker, CheckStatus
from errors import domain_only


# ── Figure → PNG helper ────────────────────────────────────────────────────────

def _fig_to_png(fig, dpi: int = 150) -> io.BytesIO:
    """Render a matplotlib Figure to a PNG BytesIO buffer (white background)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


def _embed(pdf: "SubmittalReport", buf: io.BytesIO, w_mm: float = 0,
           x_mm: Optional[float] = None) -> None:
    """
    Embed a PNG buffer into *pdf*.

    FPDF2 ≥ 2.5 accepts a file-like object directly via image().
    Falls back to a NamedTemporaryFile for older builds.
    """
    if x_mm is None:
        x_mm = pdf.l_margin
    if w_mm == 0:
        w_mm = pdf.w - pdf.l_margin - pdf.r_margin
    try:
        pdf.image(buf, x=x_mm, w=w_mm)
    except TypeError:
        # Older FPDF2 — save to temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(buf.read())
            tmp_name = tf.name
        try:
            pdf.image(tmp_name, x=x_mm, w=w_mm)
        finally:
            os.unlink(tmp_name)


# ── Print-friendly chart renderers ────────────────────────────────────────────

def _plot_hull_print(offsets, L: float, T: float) -> io.BytesIO:
    ns, _ = offsets.shape
    x = np.linspace(0, L, ns)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 3))

    wl = offsets[:, -1]
    ax1.fill_between(x, -wl, wl, color="#0077b6", alpha=0.25)
    ax1.plot(x, wl, color="#0077b6", lw=1.5)
    ax1.plot(x, -wl, color="#0077b6", lw=1.5)
    ax1.set_title("Plan View (Top)", fontsize=10, fontweight="bold")
    ax1.set_xlabel("Length (m)"); ax1.set_ylabel("Beam (m)")
    ax1.set_aspect("equal"); ax1.grid(True, ls="--", alpha=0.3)

    ax2.fill_between(x, 0, T, color="#0077b6", alpha=0.15)
    ax2.axhline(T, color="#f39c12", ls="--", lw=0.8, label=f"Draft={T:.3f}m")
    ax2.set_title("Profile View (Side)", fontsize=10, fontweight="bold")
    ax2.set_xlabel("Length (m)"); ax2.set_ylabel("Height (m)")
    ax2.legend(fontsize=7); ax2.grid(True, ls="--", alpha=0.3)

    fig.tight_layout()
    return _fig_to_png(fig)


def _plot_envelope_print(x, M_data, title: str, ylabel: str) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(7, 2.5))
    if isinstance(M_data, tuple) and len(M_data) in (3, 4):
        M_env, M4, M2 = M_data[0], M_data[1], M_data[2]
        M3 = M_data[3] if len(M_data) == 4 else None
        ax.plot(x, M4,  color="#c0392b", lw=1.5, label="4-Person")
        if M3 is not None:
            ax.plot(x, M3, color="#27ae60", lw=1.5, label="3-Person")
        ax.plot(x, M2,  color="#e67e22", lw=1.5, label="2-Person")
        ax.plot(x, M_env, color="#2980b9", lw=1, ls="--", alpha=0.6, label="Envelope")
    elif isinstance(M_data, dict):
        if "race_4" in M_data:
            ax.plot(x, M_data["race_4"], color="#c0392b", lw=1.5, label="4-Person")
        if M_data.get("race_3") is not None:
            ax.plot(x, M_data["race_3"], color="#27ae60", lw=1.5, label="3-Person")
        if "race_2" in M_data:
            ax.plot(x, M_data["race_2"], color="#e67e22", lw=1.5, label="2-Person")
        if "max_pos" in M_data:
            ax.fill_between(x, M_data["max_pos"], M_data.get("max_neg", 0),
                            color="#2980b9", alpha=0.15, label="Envelope")
    else:
        ax.plot(x, M_data, color="#2980b9", lw=1.5)
    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_xlabel("Distance from Bow (ft)"); ax.set_ylabel(ylabel)
    ax.legend(fontsize=7); ax.grid(True, ls="--", alpha=0.3)
    fig.tight_layout()
    return _fig_to_png(fig)


def _plot_pour_zones_print(x, layout, na_line=None) -> io.BytesIO:
    from visualization import plot_pour_zones
    fig = plot_pour_zones(x, layout, filename=None, na_line=na_line)
    # Convert dark figure to white background for print
    fig.patch.set_facecolor("white")
    for ax in fig.axes:
        ax.set_facecolor("white")
        ax.tick_params(colors="black")
        for spine in ax.spines.values():
            spine.set_edgecolor("black")
        ax.title.set_color("black")
        ax.xaxis.label.set_color("black")
        ax.yaxis.label.set_color("black")
    return _fig_to_png(fig)


def _plot_stress_print(x, M_envelope, I_in4, c_top, c_bot,
                       allow_c, allow_t) -> io.BytesIO:
    from visualization import plot_stress_envelope
    fig = plot_stress_envelope(x, M_envelope, I_in4, c_top, c_bot, allow_c, allow_t)
    fig.patch.set_facecolor("white")
    for ax in fig.axes:
        ax.set_facecolor("white")
        ax.tick_params(colors="black")
        for spine in ax.spines.values():
            spine.set_edgecolor("black")
    return _fig_to_png(fig)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN REPORT CLASS
# ══════════════════════════════════════════════════════════════════════════════

class SubmittalReport(FPDF):
    """
    ASCE Concrete Canoe — Technical Submittal PDF.

    Usage
    -----
        rpt = SubmittalReport(project_dir="path/to/project",
                              regression_version="v3 (2026-04-15)")
        path = rpt.generate(structural_result=sweep_result_dict)
    """

    def __init__(self, project_dir=None, regression_version: str = ""):
        super().__init__(orientation="P", unit="mm", format="letter")
        self.project_dir = project_dir or os.path.dirname(
            os.path.abspath(__file__)
        )
        self._regression_version = regression_version or (
            get_mix_db().get_active_version_tag()
            if hasattr(get_mix_db(), "get_active_version_tag") else ""
        )
        self.set_auto_page_break(auto=True, margin=20)

    @staticmethod
    def _sanitize(text: str) -> str:
        """Map non-latin-1 characters to ASCII equivalents for core fonts."""
        replacements = {
            "–": "-", "—": "--",
            "‘": "'", "’": "'",
            "“": '"', "”": '"',
            "…": "...", "≥": ">=", "≤": "<=",
        }
        for k, v in replacements.items():
            text = text.replace(k, v)
        return text.encode("latin-1", errors="replace").decode("latin-1")

    # ── Page chrome ───────────────────────────────────────────────────────────

    def header(self):
        if self.page_no() > 1:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(120, 120, 120)
            self.cell(0, 8, "ASCE Concrete Canoe - Technical Submittal", 0, 0, "L")
            self.cell(0, 8, f"Page {self.page_no()}", 0, 1, "R")
            self.line(10, 14, self.w - 10, 14)
            self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        ver_tag = (f"  |  Regression: {self._regression_version}"
                   if self._regression_version else "")
        self.cell(
            0, 10,
            self._sanitize(
                f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                f"  |  Canoe Optimizer Suite v2.0{ver_tag}"
            ),
            0, 0, "C",
        )

    # ── Heading helpers ───────────────────────────────────────────────────────

    def _section(self, title: str):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(0, 51, 102)
        self.cell(0, 10, self._sanitize(title), 0, 1, "L")
        self.set_draw_color(0, 119, 182)
        self.line(10, self.get_y(), 100, self.get_y())
        self.ln(4)

    def _subsection(self, title: str):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(40, 40, 40)
        self.cell(0, 8, self._sanitize(title), 0, 1, "L")
        self.ln(1)

    def _body(self, text: str):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(50, 50, 50)
        self.multi_cell(0, 5, self._sanitize(text))
        self.ln(2)

    def _kv(self, key: str, value: str, ref: str = ""):
        """Render a single key: value [rule ref] line in a definition-list style."""
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(30, 30, 30)
        self.cell(65, 5, self._sanitize(key + ":"), 0, 0, "L")
        self.set_font("Helvetica", "", 9)
        self.cell(60, 5, self._sanitize(value), 0, 0, "L")
        if ref:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(120, 120, 120)
            self.cell(0, 5, self._sanitize(ref), 0, 1, "R")
        else:
            self.ln()

    # ── Individual sections ───────────────────────────────────────────────────

    def _add_cover(self):
        self.add_page()
        self.ln(50)
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(0, 51, 102)
        self.cell(0, 15, "ASCE Concrete Canoe", 0, 1, "C")
        self.set_font("Helvetica", "", 18)
        self.set_text_color(80, 80, 80)
        self.cell(0, 12, "Technical Submittal Report", 0, 1, "C")
        self.ln(10)
        self.set_draw_color(0, 119, 182)
        self.set_line_width(0.8)
        self.line(60, self.get_y(), self.w - 60, self.get_y())
        self.ln(10)
        self.set_font("Helvetica", "", 14)
        self.set_text_color(60, 60, 60)
        self.cell(0, 10, "University of Virginia", 0, 1, "C")
        self.cell(0, 10,
                  f"Competition Year: {datetime.now().year}-{datetime.now().year+1}",
                  0, 1, "C")
        self.ln(8)
        self.set_font("Helvetica", "I", 11)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Report Date: {datetime.now().strftime('%B %d, %Y')}", 0, 1, "C")
        self.cell(0, 8, "Generated by Canoe Optimizer Suite v2.0", 0, 1, "C")
        if self._regression_version:
            self.ln(4)
            self.set_font("Helvetica", "I", 9)
            self.set_text_color(100, 100, 100)
            self.cell(0, 6,
                      self._sanitize(
                          f"Structural results based on mix regression: {self._regression_version}"
                      ),
                      0, 1, "C")

    def _add_hull_section(self):
        self.add_page()
        self._section("Hull Geometry")
        try:
            from hull_generator import generate_hull, compute_hydrostatics
            factors = [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05]
            L, B, T = 6.1, 0.61, 0.20
            offsets = generate_hull(factors, L, B, T, 81, 51)
            hydro = compute_hydrostatics(offsets, L, T, 998.0, 9.807)
            self._body(
                f"Hull Dimensions: L = {L:.2f} m  |  B = {hydro['beam']:.3f} m  |  "
                f"T = {T:.3f} m\n"
                f"Displaced Volume: {hydro['volume']:.4f} m³\n"
                f"Grid Resolution: {offsets.shape[0]} stations × "
                f"{offsets.shape[1]} waterlines\n"
                f"Shape Factors: {[round(f, 2) for f in factors]}"
            )
            buf = _plot_hull_print(offsets, L, T)
            _embed(self, buf)
        except Exception as exc:
            # DELIBERATELY BROAD, same reasoning as `_solid_keel_audit`.  This
            # is section 2 of 9 and the page is already open; an exception here
            # discards the seven sections after it and the sweep that fed them.
            # The section states its own absence in the PDF, so a reader is
            # never shown a hull page that quietly went missing.
            self._body(f"Hull geometry not available: "
                       f"{type(exc).__name__}: {exc}")

    def _add_compliance_section(self, structural_result=None):
        self.add_page()
        self._section("ASCE 2026 / ACI 318 Compliance")
        self._body(
            "All limits are evaluated against ASCE Concrete Canoe Competition "
            "2026 rules and ACI 318-19 allowable-stress practice."
        )

        # Gather parameters from sweep result if available
        loa_ft = 20.0
        t_in = 0.5
        hull_weight = None
        mix_densities = None
        freeboard_in = 7.0

        if structural_result:
            t_in = structural_result.get("t_in", t_in)
            hull_weight = structural_result.get("best_weight")
            mixes = structural_result.get("best_mixes")
            if mixes:
                mix_densities = mixes

        checker = ASCEComplianceChecker()
        report = checker.check_all(
            loa_ft=loa_ft,
            freeboard_in=freeboard_in,
            fos=FACTOR_OF_SAFETY,
            thickness_in=t_in,
            freeboard_min_in=config.FREEBOARD_MIN_IN,
            hull_weight_lbs=hull_weight,
            mix_densities=mix_densities,
        )

        # Summary banner
        self.set_font("Helvetica", "B", 11)
        if report.is_compliant:
            self.set_fill_color(220, 255, 220)
            self.set_text_color(0, 100, 0)
        else:
            self.set_fill_color(255, 220, 220)
            self.set_text_color(180, 0, 0)
        self.cell(0, 8, self._sanitize(report.summary), 1, 1, "C", fill=True)
        self.ln(3)

        # Results table — columns: Name / Status / Actual / Limit / Detail / Rule ref
        col_w = [48, 16, 26, 20, 46, 34]
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(0, 51, 102)
        self.set_text_color(255, 255, 255)
        for w, h in zip(col_w, ["Check", "Status", "Actual", "Limit", "Detail", "Rule Ref"]):
            self.cell(w, 6, h, 1, 0, "C", fill=True)
        self.ln()

        status_colors = {
            CheckStatus.PASS: (220, 255, 220),
            CheckStatus.FAIL: (255, 200, 200),
            CheckStatus.WARN: (255, 245, 200),
            CheckStatus.NA:   (235, 235, 235),
        }

        self.set_font("Helvetica", "", 7)
        for chk in report.checks:
            fill_rgb = status_colors.get(chk.status, (255, 255, 255))
            self.set_fill_color(*fill_rgb)
            self.set_text_color(40, 40, 40)
            row = [
                self._sanitize(chk.name[:28]),
                chk.status.value,
                self._sanitize(chk.actual[:16]),
                self._sanitize(chk.limit[:16]),
                self._sanitize(chk.detail[:50]),
                self._sanitize(chk.rule_ref[:22]),
            ]
            for w, val in zip(col_w, row):
                self.cell(w, 5, val, 1, 0, "L", fill=True)
            self.ln()
        self.ln(3)

    def _add_structural_section(self, structural_result=None):
        self.add_page()
        self._section("Structural Analysis")
        self._body(
            "The 5-dimensional parametric sweep finds the lightest concrete "
            "shell that satisfies the bending moment envelope across all race "
            f"conditions.\n\nFactor of Safety: {FACTOR_OF_SAFETY:.2f} (ACI 318)  |  "
            f"Dynamic Load Factor: {DYNAMIC_LOAD_FACTOR:.2f}  |  "
            f"Maneuver Coupling K: {MANEUVER_COUPLING_K}"
        )

        if structural_result is None:
            self._body(
                "No structural sweep result available. "
                "Run the sweep from the Godzilla + Structures tab."
            )
            return

        sr = structural_result
        x      = sr.get("x")
        M_env  = sr.get("M_envelope")
        V_env  = sr.get("V_envelope")
        layout = sr.get("layout")
        na     = sr.get("na_line")
        I      = sr.get("I_in4")
        c_top  = sr.get("c_top")
        c_bot  = sr.get("c_bot")
        mixes  = sr.get("best_mixes", [])
        t_in   = sr.get("t_in", 0.5)

        if x is not None and M_env is not None:
            self._subsection("Bending Moment Envelope (ft·lb)")
            buf = _plot_envelope_print(x, M_env, "Bending Moment Envelope", "Moment (ft·lb)")
            _embed(self, buf)

        if x is not None and V_env is not None:
            self._subsection("Shear Force Envelope (lb)")
            buf = _plot_envelope_print(x, V_env, "Shear Force Envelope", "Shear (lb)")
            _embed(self, buf)

        if x is not None and layout is not None:
            self._subsection("Pour Zone Layout")
            buf = _plot_pour_zones_print(x, layout, na)
            _embed(self, buf)

        # Stress chart when we have enough data
        if (x is not None and M_env is not None and I is not None
                and c_top is not None and c_bot is not None and mixes):
            try:
                db = get_mix_db()
                # Strongest mix in the design sets the capacity envelope.  Use
                # max() not mixes[0]: a multi-zone stack is stored bottom→top
                # (e.g. [57.5, 57.5, 69.5]) so mixes[0] is NOT the heaviest.
                props_heavy = db.get_mix_properties(max(mixes), fos=FACTOR_OF_SAFETY)
                allow_c = props_heavy["strength_c"] * np.ones_like(x)
                allow_t = props_heavy["strength_t"] * np.ones_like(x)
                self._subsection("Peak Stress vs Allowable")
                buf = _plot_stress_print(x, M_env, I, c_top, c_bot, allow_c, allow_t)
                _embed(self, buf)
            except Exception as _exc:
                domain_only(_exc)   # a bug is not a skippable condition
                pass

    def _add_sensitivity_section(self, structural_result=None):
        self.add_page()
        self._section("Sensitivity Analysis")
        self._body(
            "This section shows how key design parameters affect the structural "
            "performance. Sensitivities are derived from the parametric sweep "
            "grid without re-running the full optimisation."
        )

        # Parameter table
        rows = [
            ("Shell thickness", "in", "0.375-0.750",
             "Primary weight driver; +/-0.125 in changes hull weight ~+/-8-15%",
             "ASCE S3.4"),
            ("Mold depth (freeboard)", "in", ">= draft + 6",
             "Freeboard constraint; increasing depth adds dead weight",
             "ASCE S4.2"),
            ("Factor of Safety", "-", "1.0-2.0",
             "FoS 1.5->1.0 reduces required concrete area by ~33%; rule sets floor",
             "ACI 318 S5.3"),
            ("Dynamic Load Factor", "-", "1.0-1.5",
             "DLF 1.2->1.0 reduces design loads ~17%; use 1.2 for ASCE events",
             "Structural practice"),
            ("Mix density (tension zone)", "pcf", "60-80",
             "Heavier mix -> higher f'c -> thinner zone; weight trade-off non-linear",
             "Team regression"),
            ("Mix density (compression zone)", "pcf", "60-80",
             "Compression zone is lighter than tension due to lower stress demand",
             "Team regression"),
            ("Paddler position spacing", "ft", ">= 3.5",
             "Minimum spacing sets CoM travel range; tighter spacing raises peak moment",
             "Biomechanics model"),
        ]

        col_w = [42, 10, 26, self.w - 118, 30]
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(0, 51, 102)
        self.set_text_color(255, 255, 255)
        for w, h in zip(col_w, ["Parameter", "Unit", "Range", "Effect on Design", "Rule Ref"]):
            self.cell(w, 6, h, 1, 0, "C", fill=True)
        self.ln()

        self.set_font("Helvetica", "", 8)
        fill = False
        for name, unit, rng, effect, ref in rows:
            self.set_fill_color(245, 245, 250 if fill else 255)
            self.set_text_color(40, 40, 40)
            self.cell(col_w[0], 5, self._sanitize(name),   1, 0, "L", fill=True)
            self.cell(col_w[1], 5, unit,                   1, 0, "C", fill=True)
            self.cell(col_w[2], 5, rng,                    1, 0, "C", fill=True)
            self.cell(col_w[3], 5, self._sanitize(effect[:70]), 1, 0, "L", fill=True)
            self.cell(col_w[4], 5, self._sanitize(ref),    1, 0, "L", fill=True)
            self.ln()
            fill = not fill

        self.ln(3)
        if structural_result:
            mixes = structural_result.get("best_mixes", [])
            t_in  = structural_result.get("t_in", "—")
            self._body(
                f"Optimised configuration: shell thickness = {t_in} in  |  "
                f"tension-zone density = {mixes[0]:.1f} pcf  |  "
                f"compression-zone density = {mixes[1]:.1f} pcf  |  "
                f"end-zone density = {mixes[2]:.1f} pcf"
                if len(mixes) >= 3 else
                f"Optimised shell thickness: {t_in} in"
            )

    def _add_pour_schedule(self, structural_result=None):
        self.add_page()
        self._section("Pour Schedule & Manufacturing")
        self._body(
            "The three-zone pour strategy is derived from the neutral-axis "
            "position along the canoe length. Placing the densest mix in the "
            "tension zone and the lightest mix at the zero-moment ends minimises "
            "total hull weight while meeting structural requirements at every station."
        )

        mixes = (structural_result or {}).get("best_mixes", [])
        t_in  = (structural_result or {}).get("t_in", 0.5)

        zone_rows = [
            ("Zone 1 — Tension (bottom)",
             f"{mixes[0]:.1f} pcf" if mixes else "heavy mix",
             "Below neutral axis in mid-span",
             "1st",
             "Maximum bending demand; highest f'c required."),
            ("Zone 2 — Compression (top)",
             f"{mixes[1]:.1f} pcf" if len(mixes) > 1 else "mid mix",
             "Above neutral axis in mid-span",
             "2nd",
             "Lower stress demand allows lighter mix; reduces dead weight."),
            ("Zone 3 — Low Moment (ends)",
             f"{mixes[2]:.1f} pcf" if len(mixes) > 2 else "light mix",
             "Bow and stern where M ≈ 0",
             "3rd",
             "Lightest mix where structural demand is minimal."),
        ]

        col_w = [48, 22, 38, 12, self.w - 140]
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(0, 51, 102)
        self.set_text_color(255, 255, 255)
        for w, h in zip(col_w, ["Zone", "Mix Density", "Location", "Seq.", "Notes"]):
            self.cell(w, 6, h, 1, 0, "C", fill=True)
        self.ln()

        fill_c = [(255, 230, 220), (220, 230, 255), (220, 255, 220)]
        self.set_font("Helvetica", "", 8)
        for i, (zone, density, loc, seq, notes) in enumerate(zone_rows):
            self.set_fill_color(*fill_c[i])
            self.set_text_color(40, 40, 40)
            for w, val in zip(col_w,
                              [zone, density, loc, seq, self._sanitize(notes[:65])]):
                self.cell(w, 5, self._sanitize(val), 1, 0, "L", fill=True)
            self.ln()

        self.ln(4)
        self._subsection("General Manufacturing Notes")
        self._body(
            f"1. Target trowel thickness: {t_in:.3f} in ({t_in * 25.4:.1f} mm). "
            "Maintain consistent thickness with gauge cards during layup.\n"
            "2. Apply Zone 1 (tension) first to the bottom of the mold so it "
            "can cure and stiffen before flipping.\n"
            "3. After flip, apply Zone 2 (compression) to the top. Keep the "
            "neutral-axis boundary within ± 0.5 in of the design line.\n"
            "4. Zone 3 (ends) can be cast simultaneously with Zone 1 if the "
            "mold is sealed at the bow and stern bulkheads.\n"
            "5. Allow 28-day cure before testing; wet-cure for the first 7 days "
            "to prevent shrinkage cracking.\n"
            "6. Cylinder specimens: cast at least 3 per zone per pour day. "
            "Test at 7, 14, and 28 days. Log results in the Mix Lab tab."
        )

    def _add_mix_table(self):
        self.add_page()
        self._section("Concrete Mix Formulations")
        self._body(
            "Allowable stresses are derived from the team's empirical "
            f"regression divided by FoS = {FACTOR_OF_SAFETY:.2f}. "
            + (f"Regression version in use: {self._regression_version}."
               if self._regression_version else "")
        )
        try:
            mixes = get_mix_db().get_all_mixes()
        except Exception as _exc:
            domain_only(_exc)   # a bug is not a skippable condition
            mixes = []

        if not mixes:
            self._body("No mixes registered in the database.")
            return

        col_w = [55, 22, 28, 28, 22, 22]
        headers = ["Mix Name", "pcf", "f'c (psi)", "ft (psi)", "Fiber", "Vol %"]
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(0, 51, 102)
        self.set_text_color(255, 255, 255)
        for w, h in zip(col_w, headers):
            self.cell(w, 7, h, 1, 0, "C", fill=True)
        self.ln()

        self.set_font("Helvetica", "", 9)
        self.set_text_color(40, 40, 40)
        fill = False
        for m in mixes:
            self.set_fill_color(240, 240, 245 if fill else 255)
            row = [
                self._sanitize(m["name"][:28]),
                f"{m['density_pcf']:.1f}",
                f"{m['fc_psi']:.0f}",
                f"{m['ft_psi']:.0f}",
                m.get("fiber_type", "PVA"),
                f"{m.get('fiber_vol_pct', 2.0):.1f}",
            ]
            for w, val in zip(col_w, row):
                self.cell(w, 6, val, 1, 0, "C", fill=True)
            self.ln()
            fill = not fill

    def _add_validation_section(self):
        self._section("Wave Resistance Validation")
        val_path = os.path.join(self.project_dir, "validation_results.json")
        if not os.path.exists(val_path):
            self._body(
                "No validation data available. Run validation_suite.py to compare "
                "the Python Michell engine against Godzilla."
            )
            return
        try:
            with open(val_path, "r") as fh:
                data = json.load(fh)
            latest = data[-1] if isinstance(data, list) else data
            hull   = latest.get("hull", {})
            deltas = latest.get("deltas")
            py_t   = latest.get("python_elapsed_s", "N/A")
            self._body(
                f"Validation hull: L={hull.get('L','N/A')} m  "
                f"B={hull.get('B','N/A')} m  T={hull.get('T','N/A')} m\n"
                f"Python engine time: {py_t} s"
            )
            if deltas:
                self._subsection("Error Metrics (Python vs. Godzilla)")
                self._body(
                    f"RMSE Wave Resistance: {deltas['rmse_Rw']:.3f} N\n"
                    f"RMSE Total Resistance: {deltas['rmse_Rt']:.3f} N\n"
                    f"Max Absolute Error: {deltas['max_abs_err_Rw']:.3f} N "
                    f"@ {deltas['max_err_speed']:.1f} m/s\n"
                    f"Mean Percentage Error: {deltas['mean_pct_err_Rw']:.1f}%"
                )
            else:
                self._body(
                    "Godzilla not available for comparison. "
                    "Python-only results logged."
                )
        except Exception as exc:
            domain_only(exc)   # a bug is not a skippable condition
            self._body(f"Error reading validation data: {exc}")

    def _add_safety_summary(self):
        self._section("Safety Factor Summary")
        rows = [
            ("Factor of Safety (FoS)", f"{FACTOR_OF_SAFETY:.2f}",
             "Allowable stress = ultimate / FoS", "ACI 318-19 §5.3"),
            ("Dynamic Load Factor (DLF)", f"{DYNAMIC_LOAD_FACTOR:.2f}",
             "Multiplier on paddler loads for race dynamics", "Structural practice"),
            ("Maneuver Coupling K", f"{MANEUVER_COUPLING_K}",
             "Lateral centripetal load fraction in bending", "Structural practice"),
            ("Min Shell Thickness", "0.375 in",
             "Minimum trowel-able shell thickness", "ASCE 2026 §3.4"),
            ("ASCE Freeboard Minimum", "3.0 in",
             "Loaded condition", "ASCE 2026 §4.2"),
        ]
        col_w = [52, 18, self.w - 100, 28]
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(0, 51, 102)
        self.set_text_color(255, 255, 255)
        for w, h in zip(col_w, ["Parameter", "Value", "Description", "Rule Ref"]):
            self.cell(w, 7, h, 1, 0, "C", fill=True)
        self.ln()
        self.set_font("Helvetica", "", 9)
        self.set_text_color(40, 40, 40)
        fill = False
        for name, val, desc, ref in rows:
            self.set_fill_color(245, 245, 250 if fill else 255)
            self.cell(col_w[0], 6, self._sanitize(name),    1, 0, "L", fill=True)
            self.cell(col_w[1], 6, val,                      1, 0, "C", fill=True)
            self.cell(col_w[2], 6, self._sanitize(desc[:60]), 1, 0, "L", fill=True)
            self.cell(col_w[3], 6, self._sanitize(ref),      1, 0, "L", fill=True)
            self.ln()
            fill = not fill

    # ── Top-level generate ────────────────────────────────────────────────────

    def generate(
        self,
        output_name: str = "ASCE_Technical_Submittal.pdf",
        structural_result: Optional[dict] = None,
        race_result: Optional[dict] = None,
    ) -> str:
        """
        Build the complete PDF.

        Parameters
        ----------
        structural_result : dict returned by main.run_grand_optimization()
            Keys used: x, M_envelope, V_envelope, layout, na_line,
                       I_in4, c_top, c_bot, best_mixes, t_in,
                       regression_version (optional override)
        race_result : dict returned by race_simulator (reserved for future use)

        Returns the absolute path to the saved PDF.
        """
        if structural_result and "regression_version" in structural_result:
            self._regression_version = structural_result["regression_version"]

        print("=" * 55)
        print("  ASCE CONCRETE CANOE — TECHNICAL SUBMITTAL REPORT")
        print("=" * 55)

        print("[1/9] Cover page...")
        self._add_cover()

        print("[2/9] Hull geometry...")
        self._add_hull_section()

        print("[3/9] ASCE/ACI compliance...")
        self._add_compliance_section(structural_result)

        print("[4/9] Structural analysis (embedded charts)...")
        self._add_structural_section(structural_result)

        print("[5/9] Sensitivity analysis...")
        self._add_sensitivity_section(structural_result)

        print("[6/9] Pour schedule...")
        self._add_pour_schedule(structural_result)

        print("[7/9] Mix formulations...")
        self._add_mix_table()

        print("[8/9] Wave resistance validation...")
        self.add_page()
        self._add_validation_section()

        print("[9/9] Safety factor summary...")
        self._add_safety_summary()

        output_path = os.path.join(self.project_dir, output_name)
        self.output(output_path)
        size_kb = os.path.getsize(output_path) / 1024
        print(f"\nReport saved: {output_path} ({size_kb:.1f} kB)")
        return output_path


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    rpt = SubmittalReport()
    print(rpt.generate())
