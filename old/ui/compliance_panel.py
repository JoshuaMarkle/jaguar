# ui/compliance_panel.py
"""
Tab 3 — ASCE Competition Compliance Checker.

Provides editable input fields for hull parameters, runs
ASCEComplianceChecker (no Qt dependencies), and renders results
as a color-coded checklist table.  Values can be populated manually
or loaded from the most recent structural sweep result.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont


# Status badge colors (foreground text, background)
_STATUS_COLORS = {
    "PASS": ("#1a1a2e", "#2ecc71"),
    "FAIL": ("#ffffff", "#e74c3c"),
    "WARN": ("#1a1a2e", "#f39c12"),
    "N/A":  ("#888888", "#2a2a4a"),
}


class CompliancePanel(QWidget):
    """
    Self-contained widget for Tab 3 (ASCE Compliance Checker).

    Accesses ``self.mw`` (CanoeOptimizationSuite) only for console
    logging — no other shared state is required.
    """

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self._last_report = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # ── Title ──
        title = QLabel("ASCE Competition Compliance Checker")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00d2ff; padding-bottom: 4px;")
        root.addWidget(title)

        subtitle = QLabel(
            "Verify your hull design against ASCE 2026 Concrete Canoe "
            "Competition rules before submitting."
        )
        subtitle.setObjectName("note")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: #2a2a4a;")
        root.addWidget(divider)

        # ── Two-column layout: inputs left, results right ──
        body = QHBoxLayout()
        root.addLayout(body, stretch=1)

        body.addWidget(self._build_input_panel(), stretch=0)
        body.addWidget(self._build_results_panel(), stretch=1)

    # ------------------------------------------------------------------
    # Input panel (left column)
    # ------------------------------------------------------------------
    def _build_input_panel(self):
        container = QWidget()
        container.setFixedWidth(280)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(6)

        def heading(text):
            lbl = QLabel(text)
            lbl.setObjectName("heading")
            layout.addWidget(lbl)

        def field(label, default):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label)
            lbl.setMinimumWidth(140)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            entry = QLineEdit(default)
            entry.setFixedWidth(90)
            rl.addWidget(lbl)
            rl.addWidget(entry)
            layout.addWidget(row)
            return entry

        # Hull geometry
        heading("Hull Geometry")
        self._loa_var       = field("Length Overall (ft):", "20.0")
        self._freeboard_var = field("Min Freeboard (in):", "7.5")

        # Structural result
        heading("Structural Sweep Result")
        self._weight_var    = field("Dry Hull Weight (lbs):", "")
        self._thick_var     = field("Shell Thickness (in):", "0.5")
        self._mix_var       = field("Mix Densities (pcf):", "78, 68, 62")

        # Safety settings
        heading("Safety Settings")
        self._fos_var       = field("Factor of Safety:", "1.5")
        self._fb_min_var    = field("Freeboard Target (in):", "6.0")

        layout.addSpacing(8)

        # Load-from-sweep button
        load_btn = QPushButton("⬇  Load from Last Sweep")
        load_btn.setFixedHeight(30)
        load_btn.setStyleSheet(
            "QPushButton { background-color: #0f3460; border: 1px solid #00d2ff; "
            "color: #00d2ff; font-size: 10px; }"
            "QPushButton:hover { background-color: #1a5276; }"
        )
        load_btn.clicked.connect(self._load_from_sweep)
        layout.addWidget(load_btn)

        layout.addSpacing(8)

        # Run button
        self._run_btn = QPushButton("▶  Run Compliance Check")
        self._run_btn.setFixedHeight(44)
        self._run_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._run_btn.setStyleSheet(
            "QPushButton { background-color: #2980b9; }"
            "QPushButton:hover { background-color: #1a5276; }"
        )
        self._run_btn.clicked.connect(self._run_check)
        layout.addWidget(self._run_btn)

        layout.addStretch()
        return container

    # ------------------------------------------------------------------
    # Results panel (right column)
    # ------------------------------------------------------------------
    def _build_results_panel(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Summary banner
        self._summary_label = QLabel("Enter hull parameters and click Run.")
        self._summary_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._summary_label.setWordWrap(True)
        self._summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._summary_label.setMinimumHeight(44)
        self._summary_label.setStyleSheet(
            "QLabel { background-color: #0f3460; border-radius: 6px; "
            "color: #aaa; padding: 8px; }"
        )
        layout.addWidget(self._summary_label)

        # Column headers
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(4, 2, 4, 2)
        for col_text, col_width in [
            ("Status", 64), ("Check", 0), ("Actual", 100), ("Limit", 120)
        ]:
            lbl = QLabel(col_text)
            lbl.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            lbl.setStyleSheet("color: #aaa;")
            if col_width:
                lbl.setFixedWidth(col_width)
            header_layout.addWidget(lbl)
        layout.addWidget(header_widget)

        # Scrollable results area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(2)
        self._results_layout.addStretch()

        scroll.setWidget(self._results_container)
        layout.addWidget(scroll, stretch=1)

        return container

    # ------------------------------------------------------------------
    # Populate results rows
    # ------------------------------------------------------------------
    def _clear_results(self):
        # Remove all widgets except the trailing stretch
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_result_row(self, check):
        from reporting.compliance import CheckStatus

        row = QWidget()
        row.setFixedHeight(36)

        # Alternating background
        rl = QHBoxLayout(row)
        rl.setContentsMargins(4, 2, 4, 2)
        rl.setSpacing(8)

        # Status badge
        fg, bg = _STATUS_COLORS.get(check.status.value, ("#fff", "#555"))
        badge = QLabel(check.status.value)
        badge.setFixedSize(56, 24)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        badge.setStyleSheet(
            f"QLabel {{ background-color: {bg}; color: {fg}; "
            f"border-radius: 4px; padding: 2px 4px; }}"
        )
        rl.addWidget(badge)

        # Check name + detail (expands)
        name_col = QWidget()
        nc_layout = QVBoxLayout(name_col)
        nc_layout.setContentsMargins(0, 0, 0, 0)
        nc_layout.setSpacing(0)

        name_lbl = QLabel(check.name)
        name_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        name_lbl.setStyleSheet("color: #e0e0e0;")
        nc_layout.addWidget(name_lbl)

        detail_lbl = QLabel(check.detail)
        detail_lbl.setFont(QFont("Segoe UI", 8))
        detail_lbl.setStyleSheet("color: #888;")
        detail_lbl.setWordWrap(True)
        nc_layout.addWidget(detail_lbl)

        rl.addWidget(name_col, stretch=1)

        # Actual value
        actual_lbl = QLabel(check.actual)
        actual_lbl.setFixedWidth(100)
        actual_lbl.setFont(QFont("Consolas", 9))
        actual_lbl.setStyleSheet("color: #00d2ff;")
        actual_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rl.addWidget(actual_lbl)

        # Limit
        limit_lbl = QLabel(check.limit)
        limit_lbl.setFixedWidth(120)
        limit_lbl.setFont(QFont("Consolas", 9))
        limit_lbl.setStyleSheet("color: #aaa;")
        limit_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rl.addWidget(limit_lbl)

        # Insert before the trailing stretch
        self._results_layout.insertWidget(
            self._results_layout.count() - 1, row
        )

    # ------------------------------------------------------------------
    # Load from sweep
    # ------------------------------------------------------------------
    def _load_from_sweep(self):
        """
        Auto-fill input fields from the most recent structural sweep result
        stored on the StructuralTab (if any) and from the structural settings.
        """
        try:
            st = self.mw.struct_tab

            # FoS from slider
            self._fos_var.setText(f"{st._get_fos_value():.2f}")

            # Freeboard target from input field
            self._fb_min_var.setText(st.freeboard_var.text())

            # LOA from length field
            self._loa_var.setText(st.length_var.text())

            # Shell thickness from last sweep result (stored as attribute if available)
            last = getattr(st, '_last_sweep_result', None)
            if last is not None:
                weight = last.get('weight_lbs') or last.get('W')
                if weight:
                    self._weight_var.setText(f"{weight:.1f}")

                thickness = last.get('thickness_in')
                if thickness:
                    self._thick_var.setText(f"{thickness:.3f}")

                mixes = last.get('mix_densities') or last.get('mixes')
                if mixes:
                    self._mix_var.setText(", ".join(f"{d:.1f}" for d in mixes))

            self.mw.log("✅ Compliance fields loaded from current settings.")
        except Exception as e:
            self.mw.log(f"⚠ Could not load from sweep: {e}")

    # ------------------------------------------------------------------
    # Run check
    # ------------------------------------------------------------------
    def _run_check(self):
        from reporting.compliance import ASCEComplianceChecker, CheckStatus

        errors = []

        def _float(widget, label, allow_empty=False):
            txt = widget.text().strip()
            if not txt and allow_empty:
                return None
            try:
                return float(txt)
            except ValueError:
                widget.setStyleSheet("border: 1px solid #e74c3c;")
                errors.append(f"{label} is not a valid number")
                return None

        def _float_list(widget, label):
            txt = widget.text().strip()
            if not txt:
                return None
            try:
                vals = [float(v.strip()) for v in txt.split(",") if v.strip()]
                if not vals:
                    raise ValueError
                widget.setStyleSheet("")
                return vals
            except ValueError:
                widget.setStyleSheet("border: 1px solid #e74c3c;")
                errors.append(f"{label} must be comma-separated numbers")
                return None

        for w in [self._loa_var, self._freeboard_var, self._fos_var,
                  self._thick_var, self._fb_min_var]:
            w.setStyleSheet("")

        loa_ft       = _float(self._loa_var,       "Length Overall")
        freeboard_in = _float(self._freeboard_var,  "Freeboard")
        fos          = _float(self._fos_var,         "Factor of Safety")
        thickness_in = _float(self._thick_var,       "Shell Thickness")
        fb_min_in    = _float(self._fb_min_var,      "Freeboard Target")
        weight_lbs   = _float(self._weight_var,      "Hull Weight", allow_empty=True)
        mix_densities = _float_list(self._mix_var,   "Mix Densities")

        if errors:
            self._summary_label.setText("⚠  Fix input errors before running.")
            self._summary_label.setStyleSheet(
                "QLabel { background-color: #5a2d0a; border-radius: 6px; "
                "color: #f39c12; padding: 8px; }"
            )
            self.mw.log("⚠ Compliance check: " + "; ".join(errors))
            return

        checker = ASCEComplianceChecker()
        report = checker.check_all(
            loa_ft=loa_ft,
            freeboard_in=freeboard_in,
            fos=fos,
            thickness_in=thickness_in,
            freeboard_min_in=fb_min_in,
            hull_weight_lbs=weight_lbs,
            mix_densities=mix_densities,
        )

        self._last_report = report
        self._clear_results()
        for check in report.checks:
            self._add_result_row(check)

        # Update summary banner
        if report.n_fail > 0:
            color, bg = "#ffffff", "#922b21"
            icon = "❌"
        elif report.n_warn > 0:
            color, bg = "#1a1a2e", "#d68910"
            icon = "⚠"
        else:
            color, bg = "#1a1a2e", "#1e8449"
            icon = "✅"

        self._summary_label.setText(f"{icon}  {report.summary}")
        self._summary_label.setStyleSheet(
            f"QLabel {{ background-color: {bg}; border-radius: 6px; "
            f"color: {color}; padding: 8px; font-weight: bold; }}"
        )

        self.mw.log(
            f"[Compliance] {report.n_pass} passed, {report.n_warn} warnings, "
            f"{report.n_fail} failures. {report.summary}"
        )
