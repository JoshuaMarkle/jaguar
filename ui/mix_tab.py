# ui/mix_tab.py
"""
Tab — Concrete Mix Lab

Lets the mix team:
  - Log cylinder break test results directly into the database
  - See the density vs. tensile-strength scatter plot with regression overlay
  - Publish a named regression version for structural-sweep provenance
  - Browse the full lab-result history and all regression snapshots
"""
from __future__ import annotations

import sys
from datetime import date as _date, datetime as _datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit,
    QTabWidget, QTableWidget, QTableWidgetItem,
    QScrollArea, QDialog, QComboBox, QHeaderView,
    QSplitter, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import numpy as np


def _to_display_date(iso: str) -> str:
    """Convert YYYY-MM-DD (DB storage) → MM-DD-YYYY (display)."""
    try:
        return _datetime.strptime(iso, "%Y-%m-%d").strftime("%m-%d-%Y")
    except ValueError:
        return iso


def _to_storage_date(us: str) -> str:
    """Convert MM-DD-YYYY (user input) → YYYY-MM-DD (DB storage)."""
    try:
        return _datetime.strptime(us.strip(), "%m-%d-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return us.strip()   # store as-is if unparseable


class MixTab(QWidget):
    """Mix Lab tab — lab logging, regression visualisation, publish workflow."""

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self._scatter_canvas = None
        self._flex_canvas = None
        self._build_ui()
        QTimer.singleShot(400, self._refresh_all)

    # ──────────────────────  UI BUILD  ────────────────────────────────
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ── Left: data-entry panel ──────────────────────────────────
        left_outer = QWidget()
        left_layout = QVBoxLayout(left_outer)
        left_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        fg = QGridLayout(form_widget)
        fg.setSpacing(5)
        scroll.setWidget(form_widget)
        left_layout.addWidget(scroll, stretch=1)

        r = 0
        self._add_heading(fg, "Log New Lab Result", r); r += 1

        self._date_var    = self._add_field(fg, "Test Date (MM-DD-YYYY):",
                                            _date.today().strftime("%m-%d-%Y"), r); r += 1
        self._batch_var   = self._add_field(fg, "Mix Name / Batch:", "", r); r += 1
        self._cure_var    = self._add_field(fg, "Curing time (days):", "28", r); r += 1
        self._cure_var.setToolTip(
            "Age of the specimen at the break.  Breaks younger than the\n"
            "configured design age (mix.design_age_days, currently 28) are\n"
            "recorded and shown, but held OUT of the regression: a 7-day\n"
            "cylinder is the same mix at a fraction of its strength.\n"
            "Leave 0 if the age was not recorded.")
        self._density_var = self._add_field(fg, "Density (pcf):", "70.0", r); r += 1
        self._density_var.setToolTip(
            "The density the regression uses.  Leave 0 to have it taken from\n"
            "the dry weighing below (or the wet one, if that is all there is).")
        self._dry_rho_var = self._add_field(fg, "Dry Density (pcf):", "0", r); r += 1
        self._wet_rho_var = self._add_field(fg, "Wet Density (pcf):", "0", r); r += 1
        self._wet_rho_var.setToolTip(
            "Both weighings are kept, the way the team's sheet records them.\n"
            "Wet runs a few pcf heavier (absorbed water).")
        self._spec_type   = self._add_field(fg, "Specimen (cylinder/beam):",
                                            "cylinder", r); r += 1
        self._fc_var      = self._add_field(fg, "f'c (psi):",   "1500", r); r += 1
        self._ft_var      = self._add_field(fg, "ft (psi):",    "300",  r); r += 1
        self._fr_var      = self._add_field(fg, "fr (MOR, psi):", "0",  r); r += 1
        self._fr_var.setToolTip(
            "Flexural strength / Modulus of Rupture from a 3-point bending test\n"
            "(psi). Optional — leave 0 if not measured. Once the team has ≥2 fr\n"
            "points, the structural sweep's BENDING gate uses the f_r fit (the\n"
            "physically correct limit for σ=My/I); until then it falls back to\n"
            "the direct-tensile ft (conservative).")
        self._fiber_type  = self._add_field(fg, "Fiber Type:",  "PVA",  r); r += 1
        self._fiber_vol   = self._add_field(fg, "Fiber Vol (%):", "2.0", r); r += 1
        self._mass_var    = self._add_field(fg, "Specimen Mass (g):", "0.0", r); r += 1
        self._designer_var= self._add_field(fg, "Mix Designer:", "",    r); r += 1
        self._notes_var   = self._add_field(fg, "Notes:",       "",    r); r += 1

        log_btn = QPushButton("+ Log Result")
        log_btn.setFixedHeight(36)
        log_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        log_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; }"
            "QPushButton:hover { background-color: #1e8449; }")
        log_btn.clicked.connect(self._log_result)
        fg.addWidget(log_btn, r, 0, 1, 2); r += 1

        # Regression stats display
        self._add_heading(fg, "Current Regression", r); r += 1
        self._regression_label = QLabel("—")
        self._regression_label.setWordWrap(True)
        self._regression_label.setFont(QFont("Consolas", 9))
        self._regression_label.setObjectName("note")
        fg.addWidget(self._regression_label, r, 0, 1, 2); r += 1

        # Publish section
        self._add_heading(fg, "Publish Regression Version", r); r += 1
        _pub_info = QLabel(
            "What publishing does: snapshots the current density→strength "
            "fit as a named, dated version — the team's official record of "
            "which relationship a design was checked against (drawn dashed "
            "on the chart, cited in the PDF report).  The sweep normally "
            "uses the LIVE fit from lab data; a published MANUAL expression "
            "(below) is the exception — it stays pinned as the active "
            "relationship until another version is published.")
        _pub_info.setWordWrap(True)
        _pub_info.setStyleSheet("color: #999; font-size: 8pt;")
        fg.addWidget(_pub_info, r, 0, 1, 2); r += 1
        self._pub_tag_var = self._add_field(fg, "Version Tag:", "", r); r += 1
        self._pub_notes_var = self._add_field(fg, "Notes:", "", r); r += 1

        pub_btn = QPushButton("Publish New Relationship")
        pub_btn.setFixedHeight(36)
        pub_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        pub_btn.setToolTip(
            "Snapshot the current regression as a named version and mark it\n"
            "as the team's published relationship (provenance for reports).\n"
            "Sanity-check the fitted strengths on the chart first — a bad\n"
            "fit published here is what design reviews will cite.")
        pub_btn.setStyleSheet(
            "QPushButton { background-color: #2980b9; }"
            "QPushButton:hover { background-color: #1a5276; }")
        pub_btn.clicked.connect(self._publish_regression)
        fg.addWidget(pub_btn, r, 0, 1, 2); r += 1

        self._pub_status = QLabel("")
        self._pub_status.setFont(QFont("Consolas", 9))
        self._pub_status.setWordWrap(True)
        fg.addWidget(self._pub_status, r, 0, 1, 2); r += 1

        # Manual regression expression (fix plan Tab-4): for teams with an
        # equation (last season / reference) but no raw cylinder data
        self._add_heading(fg, "Manual Regression Expression", r); r += 1
        manual_btn = QPushButton("Enter Regression Manually…")
        manual_btn.setFixedHeight(30)
        manual_btn.setToolTip(
            "Type the linear coefficients of  f'c = a·ρ + b  and\n"
            "ft = c·ρ + d  directly (ρ in pcf, strengths in psi).\n"
            "Published manual expressions become the ACTIVE relationship\n"
            "used by the structural sweep.")
        manual_btn.setStyleSheet(
            "QPushButton { background-color: #6c3483; }"
            "QPushButton:hover { background-color: #4a235a; }")
        manual_btn.clicked.connect(self._enter_manual_regression)
        fg.addWidget(manual_btn, r, 0, 1, 2); r += 1

        fg.setRowStretch(r, 1)
        splitter.addWidget(left_outer)

        # ── Right: result tabs ─────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        right_layout.addWidget(self._tabs)

        # Scatter tab — canvas holder + note label
        self._scatter_tab = QWidget()
        scatter_vbox = QVBoxLayout(self._scatter_tab)
        scatter_vbox.setContentsMargins(0, 0, 0, 0)
        scatter_vbox.setSpacing(2)
        self._scatter_holder = QWidget()
        QVBoxLayout(self._scatter_holder)
        self._scatter_holder.layout().setContentsMargins(0, 0, 0, 0)
        scatter_vbox.addWidget(self._scatter_holder, stretch=1)
        self._scatter_note = QLabel(
            "★ Solid red = current ft regression  (used by Structural Sweep)"
            "     │     Orange dashed = published snapshot"
        )
        self._scatter_note.setStyleSheet("color: #999; font-size: 8pt; padding: 3px 0;")
        self._scatter_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scatter_vbox.addWidget(self._scatter_note)

        # Flexural tab — density vs. Modulus of Rupture (f_r) from 3-point bending
        self._flex_tab = QWidget()
        flex_vbox = QVBoxLayout(self._flex_tab)
        flex_vbox.setContentsMargins(0, 0, 0, 0)
        flex_vbox.setSpacing(2)
        self._flex_holder = QWidget()
        QVBoxLayout(self._flex_holder)
        self._flex_holder.layout().setContentsMargins(0, 0, 0, 0)
        flex_vbox.addWidget(self._flex_holder, stretch=1)
        self._flex_note = QLabel(
            "★ Solid blue = current f_r (MOR) fit — the bending gate's primary "
            "strength     │     Grey dashed = f_t (direct-tensile) fallback"
        )
        self._flex_note.setStyleSheet("color: #999; font-size: 8pt; padding: 3px 0;")
        self._flex_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        flex_vbox.addWidget(self._flex_note)

        self._history_tab  = QWidget(); QVBoxLayout(self._history_tab)
        self._versions_tab = QWidget(); QVBoxLayout(self._versions_tab)

        self._tabs.addTab(self._scatter_tab,  "Density vs. Tension")
        self._tabs.addTab(self._flex_tab,     "Density vs. Flexural")
        self._tabs.addTab(self._history_tab,  "Lab Results")
        self._tabs.addTab(self._versions_tab, "Regression Versions")

        # Column order follows the team's own test-results sheet, so a row here
        # reads the same way as the row it was imported from.
        self._history_table = self._make_table(
            ["ID", "Date tested", "Mix Name", "Cure (d)", "Specimen",
             "Dry ρ (pcf)", "Wet ρ (pcf)", "ρ used (pcf)",
             "f'c (psi)", "ft (psi)", "fr (psi)",
             "In fit", "Fiber", "Designer", "Notes"],
            self._history_tab,
        )
        self._history_table.setToolTip(
            "Every break on file.  'ρ used' is the density the regression "
            "reads — the dry weighing when there is one, else the wet.\n"
            "'In fit' says whether the row feeds the density → strength fit; "
            "early-age breaks (1-day, 7-day) are recorded but held out, since "
            "they are the same mix at a fraction of its strength.")

        # Multi-select (fix plan Tab-4): Shift/Ctrl-click ranges, delete once
        self._history_table.setSelectionMode(
            QTableWidget.SelectionMode.ExtendedSelection)

        # Import / Export / Delete buttons below the history table
        del_row = QHBoxLayout()
        self._export_csv_btn = QPushButton("Export CSV…")
        self._export_csv_btn.setFixedHeight(28)
        self._export_csv_btn.setToolTip(
            "Write every lab result to a CSV file (opens in Excel / Google\n"
            "Sheets) so the team can share mix data.  Includes the curing\n"
            "age, specimen type and both weighings, and imports back without\n"
            "duplicating anything.")
        self._export_csv_btn.clicked.connect(self._export_lab_csv)
        del_row.addWidget(self._export_csv_btn)

        self._import_csv_btn = QPushButton("Import CSV…")
        self._import_csv_btn.setFixedHeight(28)
        self._import_csv_btn.setToolTip(
            "Import the mix team's test-results sheets — the flexural tab,\n"
            "the compressive tab, or a CSV written by this suite.  Pick both\n"
            "tabs at once (Ctrl-click) and they merge in one pass.\n\n"
            "One test type per file: a sheet with both tables pasted side by\n"
            "side is refused, since its two halves share one set of mix-name\n"
            "and date columns without their rows lining up.\n\n"
            "Densities and flexural strengths the sheet left to a formula are\n"
            "computed from the weights and break loads it does record, using\n"
            "the specimen constants in the sheet.  Breaks already on file have\n"
            "their blanks filled instead of being logged twice; the regression\n"
            "refits once at the end.")
        self._import_csv_btn.clicked.connect(self._import_lab_csv)
        del_row.addWidget(self._import_csv_btn)

        del_row.addStretch()
        self._del_btn = QPushButton("Delete Selected Result(s)")
        self._del_btn.setFixedHeight(28)
        self._del_btn.setToolTip(
            "Deletes every selected row (Shift/Ctrl-click to select several)\n"
            "in one step, then refits the regression once.")
        self._del_btn.setStyleSheet(
            "QPushButton { background-color: #922b21; color: white; }"
            "QPushButton:hover { background-color: #e74c3c; }"
            "QPushButton:disabled { background-color: #555; color: #888; }")
        self._del_btn.clicked.connect(self._delete_selected)
        del_row.addWidget(self._del_btn)
        self._history_tab.layout().addLayout(del_row)

        self._versions_table = self._make_table(
            ["ID", "Tag", "Created", "Slope fc", "Int fc", "R² fc",
             "Slope ft", "Int ft", "R² ft", "n", "Source", "Published"],
            self._versions_tab,
        )

        # ── Recommendations tab: what mix density/strength to develop next ──
        self._rec_tab = QWidget()
        _rec_v = QVBoxLayout(self._rec_tab)
        _rec_v.setContentsMargins(6, 6, 6, 6)
        _rec_v.setSpacing(4)
        self._rec_header = QLabel(
            "Run a structural sweep for design-driven mix targets, or log lab "
            "data for coverage guidance.")
        self._rec_header.setWordWrap(True)
        self._rec_header.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._rec_header.setObjectName("heading")
        _rec_v.addWidget(self._rec_header)
        self._rec_table = self._make_table(
            ["Target ρ (pcf)", "Role", "Achieve f_t (psi)", "Achieve f_r (psi)",
             "Achieve f'c (psi)", "Governs", "Design D/C (Allowable)",
             "Data confidence"],
            self._rec_tab)
        self._rec_text = QTextEdit()
        self._rec_text.setReadOnly(True)
        self._rec_text.setFont(QFont("Segoe UI", 9))
        _rec_v.addWidget(self._rec_text, stretch=1)
        self._tabs.addTab(self._rec_tab, "Recommendations")
        # Recommendations depend on the latest sweep result (owned by the
        # Structural tab), which changes independently of mix data — so refresh
        # them whenever this tab is brought to the front.
        self._tabs.currentChanged.connect(self._on_tab_changed)

        splitter.addWidget(right)
        splitter.setSizes([320, 700])

    # ──────────────────────  HELPERS  ─────────────────────────────────
    @staticmethod
    def _add_heading(grid, text: str, row: int):
        lbl = QLabel(text)
        lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        lbl.setObjectName("heading")
        grid.addWidget(lbl, row, 0, 1, 2)

    @staticmethod
    def _add_field(grid, label: str, default: str, row: int) -> QLineEdit:
        grid.addWidget(QLabel(label), row, 0)
        entry = QLineEdit(default)
        entry.setMinimumWidth(140)
        grid.addWidget(entry, row, 1)
        return entry

    @staticmethod
    def _make_table(headers: list[str], parent: QWidget) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        parent.layout().addWidget(table)
        return table

    def _embed_scatter(self, fig):
        old = self._scatter_canvas
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        canvas = FigureCanvasQTAgg(fig)
        from ui.chart_export import enable_chart_export
        enable_chart_export(canvas)
        canvas.draw()
        lay = self._scatter_holder.layout()
        lay.addWidget(canvas)
        self._scatter_canvas = canvas

    def _embed_flex(self, fig):
        old = self._flex_canvas
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        canvas = FigureCanvasQTAgg(fig)
        from ui.chart_export import enable_chart_export
        enable_chart_export(canvas)
        canvas.draw()
        self._flex_holder.layout().addWidget(canvas)
        self._flex_canvas = canvas

    # ──────────────────────  DATA OPERATIONS  ────────────────────────
    def _get_db(self):
        from config import get_mix_db
        return get_mix_db()

    def _log_result(self):
        try:
            db = self._get_db()
            db.add_lab_result(
                test_date      = _to_storage_date(self._date_var.text()),
                density_pcf    = float(self._density_var.text() or 0.0),
                fc_psi         = float(self._fc_var.text() or 0.0),
                ft_psi         = float(self._ft_var.text() or 0.0),
                fr_psi         = float(self._fr_var.text() or 0.0),
                batch_id       = self._batch_var.text().strip(),
                curing_days    = int(float(self._cure_var.text() or 0)),
                specimen_type  = self._spec_type.text().strip().lower(),
                dry_density_pcf= float(self._dry_rho_var.text() or 0.0),
                wet_density_pcf= float(self._wet_rho_var.text() or 0.0),
                fiber_type     = self._fiber_type.text().strip() or "PVA",
                fiber_vol_pct  = float(self._fiber_vol.text()),
                specimen_mass_g= float(self._mass_var.text()),
                mix_designer   = self._designer_var.text().strip(),
                notes          = self._notes_var.text().strip(),
            )
            self.mw.log("Lab result logged. Regression refit automatically.")
            self._refresh_all()
        except Exception as exc:
            self.mw.log(f"Log result failed: {exc}")

    def _delete_selected(self):
        """Delete every selected row in one transaction (fix plan Tab-4:
        Shift/Ctrl multi-select used to still delete one at a time)."""
        rows = sorted({item.row() for item in self._history_table.selectedItems()})
        ids = []
        for row in rows:
            id_item = self._history_table.item(row, 0)
            if id_item is not None:
                ids.append(int(id_item.text()))
        if not ids:
            return
        label = (f"lab result ID {ids[0]}" if len(ids) == 1 else
                 f"{len(ids)} lab results (IDs {', '.join(map(str, ids))})")
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete {label}? This will refit the regression.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                n = self._get_db().delete_lab_results(ids)
                self.mw.log(f"{n} lab result(s) deleted. Regression refit.")
                self._refresh_all()
            except Exception as exc:
                self.mw.log(f"Delete failed: {exc}")

    def _export_lab_csv(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Lab Results", "mix_lab_results.csv",
            "CSV files (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            n = self._get_db().export_lab_results_csv(path)
            self.mw.log(f"Exported {n} lab result(s) → {path}")
        except Exception as exc:
            self.mw.log(f"CSV export failed: {exc}")

    def _import_lab_csv(self):
        """Import one or more lab-results CSVs in a single merge.

        The team keeps flexural and compressive results on separate tabs, so
        selecting both at once is the normal case; each file is read on its own
        terms and the regression refits once, at the end.
        """
        from PyQt6.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import Lab Results — select one or more sheets", "",
            "CSV files (*.csv);;All files (*.*)")
        if not paths:
            return
        try:
            reports = self._get_db().import_lab_csv_files(paths)
        except Exception as exc:
            self.mw.log(f"CSV import failed: {exc}")
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        for rep in reports:
            self.mw.log("CSV import — " + rep.summary())
        self._refresh_all()
        self._show_import_summary(reports)

    def _show_import_summary(self, reports):
        """Report what each file was read as, and everything worth checking.

        The warnings are the point of this dialog: a refused file, or a mix of
        dry and wet densities, is invisible in the numbers afterwards.
        """
        added   = sum(r.added for r in reports)
        updated = sum(r.updated for r in reports)
        skipped = sum(r.skipped for r in reports)
        invalid = sum(r.invalid for r in reports)
        refused = [r for r in reports if not r.imported]

        html = []
        if refused:
            html.append(
                f"<b>{len(refused)} file(s) not imported</b> — "
                "see below.<br><br>")
        html.append(f"<b>{added} result(s) added</b>")
        if updated:
            html.append(f"<br>{updated} existing break(s) completed with "
                        "measurements from these files")
        html.append(f"<br>{skipped} already on file")
        if invalid:
            html.append(f"<br>{invalid} row(s) unusable "
                        "(a strength with no weight or density)")
        # ADDED IS NOT ADMITTED.  Rows land in the table and can still be
        # excluded from every regression -- most often for a missing curing
        # age.  Without this line the dialog said "4 added" while zero rows
        # reached the fit and the strength curve quietly fell back to the
        # seeded mixes table, which is design intent rather than measurement.
        _adm = sum(getattr(rep, "fit_admitted", 0) for rep in reports)
        _exc = {}
        for rep in reports:
            for why, n in (getattr(rep, "fit_excluded", {}) or {}).items():
                _exc[why] = _exc.get(why, 0) + n
        _unusable = any(not getattr(rep, "fit_usable", True) for rep in reports)
        html.append(f"<br><br><b>Into the strength fit:</b> {_adm} row(s) "
                    f"admitted")
        if _exc:
            html.append(", excluded: " + ", ".join(
                f"{n} ({why})" for why, n in sorted(_exc.items())))
        if _unusable:
            html.append(
                "<br><br><b style='color:#b00'>NO ROW FROM THESE FILES "
                "REACHED ANY REGRESSION.</b> The strength curve is running on "
                "the seeded mix table &mdash; design intent, not measurement. "
                "Every density floor and predicted strength downstream is "
                "built on it. Fix the sheet and re-import before trusting any "
                "weight.")
        html.append("<br><br><b>Files</b><ul>")
        for rep in reports:
            html.append(f"<li>{rep.summary()}</li>")
        html.append("</ul>")

        # Two sheets out of one workbook raise the same complaint twice.
        notes    = list(dict.fromkeys(n for rep in reports for n in rep.notes))
        warnings = list(dict.fromkeys(w for rep in reports for w in rep.warnings))

        if notes:
            html.append("<b>How the files were read</b><ul>")
            html += [f"<li>{n}</li>" for n in notes]
            html.append("</ul>")

        if warnings:
            html.append("<b>Worth checking</b><ul>")
            html += [f"<li>{w}</li>" for w in warnings]
            html.append("</ul>")

        box = QMessageBox(self)
        box.setWindowTitle("Lab Results Imported")
        box.setWindowTitle("Lab Results Imported — NOT USABLE FOR FITTING"
                           if _unusable else "Lab Results Imported")
        box.setIcon(QMessageBox.Icon.Critical if _unusable
                    else QMessageBox.Icon.Warning if warnings
                    else QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText("".join(html))
        box.exec()

    def _enter_manual_regression(self):
        """Dialog for typing the linear strength relationship directly
        (fix plan Tab-4): f'c = a·ρ + b, ft = c·ρ + d."""
        from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                                     QCheckBox)
        dlg = QDialog(self)
        dlg.setWindowTitle("Manual Regression Expression")
        form = QFormLayout(dlg)
        info = QLabel(
            "Linear density→strength relationship (ρ in pcf, strengths in "
            "psi, BEFORE the factor of safety):\n"
            "    f'c = a·ρ + b        ft = c·ρ + d")
        info.setWordWrap(True)
        form.addRow(info)
        e_a = QLineEdit("40.0");  form.addRow("a  (f'c slope):",     e_a)
        e_b = QLineEdit("-1400"); form.addRow("b  (f'c intercept):", e_b)
        e_c = QLineEdit("8.0");   form.addRow("c  (ft slope):",      e_c)
        e_d = QLineEdit("-280");  form.addRow("d  (ft intercept):",  e_d)
        e_tag = QLineEdit("");    form.addRow("Version tag:",        e_tag)
        e_notes = QLineEdit("");  form.addRow("Notes:",              e_notes)
        cb_pub = QCheckBox("Publish (make this the ACTIVE relationship "
                           "used by the sweep)")
        cb_pub.setChecked(True)
        form.addRow(cb_pub)
        preview = QLabel("")
        preview.setStyleSheet("color: #999; font-size: 8pt;")
        preview.setWordWrap(True)
        form.addRow(preview)

        def _update_preview():
            try:
                a, b = float(e_a.text()), float(e_b.text())
                c, d = float(e_c.text()), float(e_d.text())
                preview.setText(
                    "Check: at 60 pcf → f'c "
                    f"{a*60+b:.0f} psi, ft {c*60+d:.0f} psi   |   80 pcf → "
                    f"f'c {a*80+b:.0f} psi, ft {c*80+d:.0f} psi")
            except ValueError:
                preview.setText("(enter numeric coefficients)")
        for e in (e_a, e_b, e_c, e_d):
            e.textChanged.connect(_update_preview)
        _update_preview()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            vid = self._get_db().set_manual_regression(
                float(e_a.text()), float(e_b.text()),
                float(e_c.text()), float(e_d.text()),
                version_tag=e_tag.text().strip(),
                notes=e_notes.text().strip(),
                publish=cb_pub.isChecked())
            state = ("published — ACTIVE for the sweep"
                     if cb_pub.isChecked() else "saved (not published)")
            self.mw.log(f"Manual regression stored (id={vid}, {state}).")
            self._refresh_all()
        except Exception as exc:
            self.mw.log(f"Manual regression failed: {exc}")

    def _publish_regression(self):
        try:
            db = self._get_db()
            tag   = self._pub_tag_var.text().strip()
            notes = self._pub_notes_var.text().strip()
            vid = db.publish_current_regression(version_tag=tag, notes=notes)
            if vid:
                pub = db.get_published_regression()
                vtag = pub["version_tag"] if pub else "—"
                self._pub_status.setText(f"Published: {vtag}  (id={vid})")
                self.mw.log(f"Regression published as '{vtag}' (id={vid}).")
            else:
                self._pub_status.setText("Nothing to publish — need >= 2 data points.")
            self._refresh_all()
        except Exception as exc:
            self.mw.log(f"Publish failed: {exc}")

    def _refresh_all(self):
        try:
            db = self._get_db()
            self._regression_label.setText(db.get_regression_stats())
            self._refresh_scatter(db)
            self._refresh_flexural(db)
            self._refresh_history_table(db)
            self._refresh_versions_table(db)
            self._refresh_recommendations(db)
        except Exception as exc:
            self.mw.log(f"Mix Lab refresh error: {exc}")

    def _on_tab_changed(self, idx):
        if self._tabs.widget(idx) is self._rec_tab:
            try:
                self._refresh_recommendations(self._get_db())
            except Exception as exc:
                self.mw.log(f"Recommendations refresh error: {exc}")

    # ──────────────────────  RECOMMENDATIONS  ────────────────────────
    def on_sweep_completed(self, result):
        """Slot — a structural sweep finished with a new winning design.

        Wired in MainWindow to ``StructuralTab.sweep_completed`` (emitted on the
        main thread after the worker's result is cached), so the Recommendations
        tab reflects the new design immediately, even from another sub-tab.
        """
        try:
            self._refresh_recommendations(self._get_db(), result=result)
            self.mw.log("[Mix Lab] Recommendations updated from the new "
                        "structural design.")
        except Exception as exc:
            self.mw.log(f"[Mix Lab] recommendations update failed: {exc}")

    def _refresh_recommendations(self, db, result=None):
        """Populate the mix-development targets.  Prefers a design pushed in
        (``result`` from the sweep-completed signal); otherwise pulls the
        Structural tab's cached result; absent any design, gives data-coverage
        guidance."""
        res = result
        if res is None:
            res = getattr(getattr(self.mw, "struct_tab", None),
                          "_last_sweep_result", None)
        zone_densities = bend_dc = hoop_dc = None
        at_floor = False
        if isinstance(res, dict):
            # Multi-zone winners carry "zone_densities"; single-zone (2-D) and
            # uniform-mix winners carry the mix set in "best_mixes" instead.
            zone_densities = res.get("zone_densities") or res.get("best_mixes")
            util = res.get("utilization") or {}
            bt, tt = util.get("bot_t"), util.get("top_t")
            if bt is not None and tt is not None:
                bend_dc = float(max(np.max(bt), np.max(tt)))
            hu = res.get("hoop_util")
            if hu is not None:
                hoop_dc = float(np.max(np.asarray(hu)))
            for b in (res.get("boundary_flags") or []):
                if b.get("variable") == "density" and b.get("edge") == "min":
                    at_floor = True

        import config as _cfg
        rec = db.recommend_mix_targets(
            zone_densities=zone_densities, bend_dc=bend_dc, hoop_dc=hoop_dc,
            density_at_floor=at_floor,
            fos=getattr(_cfg, "FACTOR_OF_SAFETY", 1.5))

        if rec["mode"] == "design":
            _gov = ("f_r (MOR)" if rec.get("governing_strength") == "fr"
                    else "f_t (direct tensile — no MOR fit yet)")
            self._rec_header.setText(
                "Targets for the current winning design — develop mixes at these "
                "densities and hit at least these raw (pre-FoS) strengths. The "
                f"bending gate currently governs on {_gov} (green column). "
                "Orange = the fit is extrapolating there; break a specimen to "
                "confirm before trusting the design.")
        else:
            self._rec_header.setText(
                "No structural design loaded — data-coverage guidance from the "
                "current fit. Run a Structural Sweep for design-driven targets.")

        tgts = rec["targets"]
        governing = rec.get("governing_strength", "ft")
        t = self._rec_table
        t.setRowCount(len(tgts))
        for i, g in enumerate(tgts):
            dc = g["utilization"]
            ft = g["ft_achieve_psi"]
            fr = g.get("fr_achieve_psi")
            ft_txt = f"{ft:.0f}" if ft > 5.0 else "negligible"
            fr_txt = ("—" if fr is None
                      else (f"{fr:.0f}" if fr > 5.0 else "negligible"))
            gov_txt = "f_r (MOR)" if governing == "fr" else "f_t (tensile)"
            vals = [f"{g['density']:.1f}", g["role"], ft_txt, fr_txt,
                    f"{g['fc_achieve_psi']:.0f}", gov_txt,
                    "—" if dc is None else f"{dc:.2f}", g["confidence"]]
            extrap = "EXTRAPOLATION" in g["confidence"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if extrap and c == 7:                         # confidence column
                    item.setForeground(QColor("#e67e22"))
                if (governing == "fr" and c == 3) or (governing == "ft" and c == 2):
                    item.setForeground(QColor("#2ecc71"))     # governing strength
                t.setItem(i, c, item)

        html = []
        if rec["improvements"]:
            html.append("<b>Next tests &amp; improvement levers</b><ul>")
            html += [f"<li>{im['text']}</li>" for im in rec["improvements"]]
            html.append("</ul>")
        if rec["notes"]:
            html.append("<b>Notes</b><ul>")
            html += [f"<li>{nt}</li>" for nt in rec["notes"]]
            html.append("</ul>")
        self._rec_text.setHtml("".join(html) or "<i>No recommendations yet.</i>")

    # ──────────────────────  SCATTER PLOT  ───────────────────────────
    def _refresh_scatter(self, db):
        lab_rows = db.get_lab_results()
        mix_rows = db.get_all_mixes()

        fig = Figure(figsize=(6, 4), facecolor="#1e1e1e")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#2a2a2a")
        ax.tick_params(colors="white")
        for spine in ax.spines.values():
            spine.set_edgecolor("#555")

        # Scatter: density vs. tensile strength.  Only specimens that actually
        # recorded a tensile break belong on this axis — a flexural-only row
        # carries ft = 0, which is missing data, not a measurement of zero.
        ft_rows  = [r for r in lab_rows if float(r.get("ft_psi") or 0.0) > 0.0]
        fit_rows = [r for r in ft_rows if not db.fit_eligibility(r)]
        held     = [r for r in ft_rows if db.fit_eligibility(r)]
        if fit_rows:
            ax.scatter([r["density_pcf"] for r in fit_rows],
                       [r["ft_psi"] for r in fit_rows],
                       color="#2ecc71", zorder=3, s=50,
                       label=f"Lab results (n={len(fit_rows)})")
        if held:
            ax.scatter([r["density_pcf"] for r in held],
                       [r["ft_psi"] for r in held],
                       facecolors="none", edgecolors="#e67e22", zorder=3, s=50,
                       label=f"Held out of the fit (n={len(held)})")

        if mix_rows:
            md  = np.array([r["density_pcf"] for r in mix_rows])
            mft = np.array([r["ft_psi"]      for r in mix_rows])
            ax.scatter(md, mft, color="#95a5a6", zorder=2, s=35, marker="s",
                       label=f"Mix designs (n={len(mix_rows)})")

        # Current ft regression — this is what the structural sweep uses
        if db.ft_regression is not None:
            all_dens = (
                [r["density_pcf"] for r in lab_rows]
                + [r["density_pcf"] for r in mix_rows]
            )
            x_min, x_max = (min(all_dens) - 2, max(all_dens) + 2) if all_dens else (55, 85)
            xs = np.linspace(x_min, x_max, 100)
            ys = np.polyval(db.ft_regression, xs)
            ax.plot(xs, ys, color="#e74c3c", linewidth=2, zorder=4,
                    label=f"Current ft fit  (R²={db.r_squared_ft:.3f})  ← sweep uses this")

            # Published regression overlay (snapshot)
            pub = db.get_published_regression()
            if pub:
                ys_pub = np.polyval([pub["slope_ft"], pub["intercept_ft"]], xs)
                ax.plot(xs, ys_pub, color="#f39c12", linewidth=1.5,
                        linestyle="--", zorder=3,
                        label=f"Published: {pub['version_tag']} (snapshot)")

        ax.set_xlabel("Density (pcf)", color="white")
        ax.set_ylabel("ft — Tensile Strength (psi)", color="white")
        ax.set_title("Density vs. Tensile Strength", color="white", pad=8)
        ax.legend(fontsize=8, facecolor="#333", labelcolor="white", framealpha=0.8)
        fig.tight_layout(pad=1.2)
        QTimer.singleShot(0, lambda: self._embed_scatter(fig))

    # ──────────────────────  FLEXURAL PLOT  ──────────────────────────
    def _refresh_flexural(self, db):
        """Density vs. flexural strength (Modulus of Rupture, fr) from the
        team's 3-point-bending points.  Shows the fr fit the bending gate uses
        as its PRIMARY strength when >= 2 bending points exist, plus the ft
        direct-tensile line as the FALLBACK the gate reverts to without an MOR
        fit (ft <= fr for concrete, so the fallback is conservative)."""
        lab_rows = db.get_lab_results()
        mix_rows = db.get_all_mixes()

        fig = Figure(figsize=(6, 4), facecolor="#1e1e1e")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#2a2a2a")
        ax.tick_params(colors="white")
        for spine in ax.spines.values():
            spine.set_edgecolor("#555")

        # Scatter only the rows that recorded a 3-point-bending result (fr > 0);
        # mix designs and tension-only lab points carry no fr.
        fr_rows = [r for r in lab_rows if float(r.get("fr_psi", 0.0)) > 0.0]
        fitted  = [r for r in fr_rows if not db.fit_eligibility(r)]
        held    = [r for r in fr_rows if db.fit_eligibility(r)]
        if fitted:
            ax.scatter([r["density_pcf"] for r in fitted],
                       [r["fr_psi"] for r in fitted],
                       color="#3498db", zorder=3, s=50,
                       label=f"3-pt bending fr  (n={len(fitted)})")
        if held:
            ax.scatter([r["density_pcf"] for r in held],
                       [r["fr_psi"] for r in held],
                       facecolors="none", edgecolors="#e67e22", zorder=3, s=50,
                       label=f"Held out of the fit (n={len(held)})")

        all_dens = ([r["density_pcf"] for r in lab_rows]
                    + [r["density_pcf"] for r in mix_rows])
        x_min, x_max = (min(all_dens) - 2, max(all_dens) + 2) if all_dens else (55, 85)
        xs = np.linspace(x_min, x_max, 100)

        # Primary: current fr (MOR) fit — what the bending gate uses when present.
        if db.fr_regression is not None:
            ax.plot(xs, np.polyval(db.fr_regression, xs),
                    color="#3498db", linewidth=2, zorder=4,
                    label=f"Current fr fit  (R²={db.r_squared_fr:.3f})  ← gate uses this")
        # Fallback: ft direct-tensile line the gate reverts to without an MOR fit.
        if db.ft_regression is not None:
            ax.plot(xs, np.polyval(db.ft_regression, xs),
                    color="#95a5a6", linewidth=1.5, linestyle="--", zorder=3,
                    label="ft (direct tensile) — fallback, no MOR fit")

        if db.fr_regression is None:
            ax.text(0.5, 0.5,
                    "No MOR fit yet.\nLog ≥ 2 three-point-bending results (fr)\n"
                    "to fit density → flexural strength.\n"
                    "Until then the bending gate uses ft (dashed).",
                    transform=ax.transAxes, ha="center", va="center",
                    color="#e67e22", fontsize=9, zorder=5)

        ax.set_xlabel("Density (pcf)", color="white")
        ax.set_ylabel("fr — Flexural Strength / MOR (psi)", color="white")
        ax.set_title("Density vs. Flexural Strength (Modulus of Rupture)",
                     color="white", pad=8)
        ax.legend(fontsize=8, facecolor="#333", labelcolor="white", framealpha=0.8)
        fig.tight_layout(pad=1.2)
        QTimer.singleShot(0, lambda: self._embed_flex(fig))

    # ──────────────────────  HISTORY TABLE  ──────────────────────────
    @staticmethod
    def _num_cell(value: float, digits: int = 0) -> QTableWidgetItem:
        """A measured number, or an em dash when the specimen never got one.

        Zero means "not measured" in every strength and density column here —
        a beam has no cylinder strength — and printing it as 0 would read as
        a measurement of zero.
        """
        v = float(value or 0.0)
        item = QTableWidgetItem(f"{v:.{digits}f}" if v > 0 else "—")
        if v <= 0:
            item.setForeground(QColor("#777"))
        return item

    def _refresh_history_table(self, db):
        rows = db.get_lab_results()
        t = self._history_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            held = db.fit_eligibility(r)
            t.setItem(i, 0,  QTableWidgetItem(str(r["id"])))
            t.setItem(i, 1,  QTableWidgetItem(_to_display_date(r["test_date"])))
            t.setItem(i, 2,  QTableWidgetItem(r["batch_id"] or ""))
            cure = int(r.get("curing_days") or 0)
            t.setItem(i, 3,  QTableWidgetItem(str(cure) if cure else "—"))
            t.setItem(i, 4,  QTableWidgetItem(r.get("specimen_type") or "—"))
            t.setItem(i, 5,  self._num_cell(r.get("dry_density_pcf"), 1))
            t.setItem(i, 6,  self._num_cell(r.get("wet_density_pcf"), 1))
            t.setItem(i, 7,  self._num_cell(r["density_pcf"], 1))
            t.setItem(i, 8,  self._num_cell(r["fc_psi"]))
            t.setItem(i, 9,  self._num_cell(r["ft_psi"]))
            t.setItem(i, 10, self._num_cell(r.get("fr_psi")))
            fit_item = QTableWidgetItem("yes" if not held else held)
            fit_item.setForeground(QColor("#2ecc71" if not held else "#e67e22"))
            if held:
                fit_item.setToolTip(
                    "Held out of the density → strength regression: "
                    + ("no density to place it on the density axis"
                       if held == "no density" else
                       f"an early-age break, below the "
                       f"{db.design_age_days()}-day design age"))
            t.setItem(i, 11, fit_item)
            t.setItem(i, 12, QTableWidgetItem(
                f"{r['fiber_type']} {r['fiber_vol_pct']:.1f}%"))
            t.setItem(i, 13, QTableWidgetItem(r["mix_designer"] or ""))
            notes_item = QTableWidgetItem(r["notes"] or "")
            notes_item.setToolTip(r["notes"] or "")
            t.setItem(i, 14, notes_item)

    # ──────────────────────  VERSIONS TABLE  ─────────────────────────
    def _refresh_versions_table(self, db):
        rows = db.get_regression_versions()
        t = self._versions_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pub_flag = "YES" if r["is_published"] else ""
            t.setItem(i, 0,  QTableWidgetItem(str(r["id"])))
            t.setItem(i, 1,  QTableWidgetItem(r["version_tag"]))
            t.setItem(i, 2,  QTableWidgetItem(r["created_at"][:16]))
            t.setItem(i, 3,  QTableWidgetItem(f"{r['slope_fc']:.3f}"))
            t.setItem(i, 4,  QTableWidgetItem(f"{r['intercept_fc']:.1f}"))
            t.setItem(i, 5,  QTableWidgetItem(f"{r['r2_fc']:.3f}"))
            t.setItem(i, 6,  QTableWidgetItem(f"{r['slope_ft']:.3f}"))
            t.setItem(i, 7,  QTableWidgetItem(f"{r['intercept_ft']:.1f}"))
            t.setItem(i, 8,  QTableWidgetItem(f"{r['r2_ft']:.3f}"))
            t.setItem(i, 9,  QTableWidgetItem(str(r["num_points"])))
            t.setItem(i, 10, QTableWidgetItem(r["data_source"]))
            t.setItem(i, 11, QTableWidgetItem(pub_flag))
            if r["is_published"]:
                for col in range(12):
                    item = t.item(i, col)
                    if item:
                        item.setBackground(QColor("#1a3a1a"))
