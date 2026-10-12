# ui/structural_tab.py
"""
Tab 2 — Godzilla + 5-D Structural Sweep.

Hosts hull file management, Godzilla launch, structural sweep settings,
and all result charts (hull preview, moment/shear envelopes, pour zones,
validation).
"""
from __future__ import annotations

import os
import re
import sys

import math

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget,
    QLabel, QLineEdit, QCheckBox, QPushButton, QTextEdit,
    QComboBox, QSlider, QScrollArea, QDialog, QFileDialog,
    QDoubleSpinBox, QSpinBox, QSizePolicy, QSplitter, QGroupBox, QFormLayout,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from ui.widgets import NoScrollSpinBox, NoScrollDoubleSpinBox

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg


class _CardStack(QWidget):
    """A column of CollapsibleCards wearing QToolBox's interface.

    The redesign replaces the settings accordion with independent cards, and
    the reason is not decorative: a QToolBox shows ONE page at a time, so
    checking a thickness bound against the depth bound that constrains it
    meant clicking back and forth holding a number in your head.

    It deliberately keeps ``addItem(widget, title)`` and ``setCurrentIndex``
    so the five category blocks that build the settings are not touched by a
    layout change.  Mixing a presentation change into the same diff as the
    controls themselves is exactly how a control quietly changes meaning --
    the defect this panel's own history is made of.
    """

    def __init__(self, default_expanded=(0,), parent=None):
        super().__init__(parent)
        self._default_expanded = set(default_expanded)
        self.cards = []
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 8)
        self._lay.setSpacing(8)
        self._lay.addStretch(1)

    @staticmethod
    def _clean(title: str) -> str:
        """Strip the toolbox's "📁  3.  " ordinal prefix.

        The numbering told the user to work top-to-bottom, which was only ever
        true of the accordion.  Cards are independent, so the ordinal is a
        false instruction; the name alone survives.
        """
        return re.sub(r"^\s*\S*\s*\d+\.\s*", "", title).strip().upper()

    def addItem(self, widget, title: str) -> int:
        from ui.widgets import CollapsibleCard
        idx = len(self.cards)
        card = CollapsibleCard(self._clean(title), widget,
                               expanded=idx in self._default_expanded,
                               warn=title.strip().startswith("⚠"))
        self.cards.append(card)
        self._lay.insertWidget(idx, card)
        return idx

    def setCurrentIndex(self, index: int) -> None:
        """Expand one card.  Unlike a toolbox this does NOT collapse the
        others — that is the whole point of the change."""
        if 0 <= index < len(self.cards):
            self.cards[index].set_expanded(True)

    def count(self) -> int:
        return len(self.cards)


class StructuralTab(QWidget):
    """
    Self-contained widget for Tab 2 (Godzilla + 5-D Structural Sweep).

    All Qt signals and heavy computations are bridged through ``self.mw``
    (the parent MainWindow), which owns the progress bar, console, worker
    thread, and stop event.
    """

    # Emitted by the background sweep worker; connected (queued) to
    # _render_sweep_results which runs in the main GUI thread.
    _sweep_render_signal = pyqtSignal(object)
    # Emitted (main thread) once a sweep's winner is cached, so other tabs —
    # e.g. the Mix Lab's design-driven recommendations — can react without
    # reaching into this tab's internals.
    sweep_completed = pyqtSignal(object)

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self._last_sweep_result: dict = {}
        self._active_canvases: list = []
        self._sweep_render_signal.connect(self._render_sweep_results)
        self._build_ui()

    def _sync_fos_from_slider(self, value):
        """Updates the typed box when the slider is dragged."""
        self.fos_spinbox.blockSignals(True)
        self.fos_spinbox.setValue(value / 100.0)
        self.fos_spinbox.blockSignals(False)

    def _sync_fos_from_spinbox(self, value):
        """Updates the slider when a number is typed."""
        self.fos_slider.blockSignals(True)
        self.fos_slider.setValue(int(value * 100))
        self.fos_slider.blockSignals(False)

    # Maneuver load controls live in RaceTab (Tab 1); delegate here so
    # downstream sweep and validation code needs no changes.
    @property
    def mlf_enabled(self):
        return self.mw.race_tab.mlf_enabled

    @property
    def mlf_speed_var(self):
        return self.mw.race_tab.mlf_speed_var

    @property
    def mlf_radius_var(self):
        return self.mw.race_tab.mlf_radius_var

    def stem_rocker_in(self) -> float | None:
        """
        Bow/stern stem-rocker sweep for CAD export, in inches (side-view
        rounding of the ends; plan view stays pointed).
        None → auto mode (geometry.suggest_stem_rocker_m decides);
        0.0  → legacy full-depth vertical stem ends.
        """
        if self.stem_rocker_auto_checkbox.isChecked():
            return None
        return float(self.stem_rocker_input.value())

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _add_entry_left(self, grid, label: str, default: str, row: int, col: int,
                        width: int = 100) -> QLineEdit:
        """Left-aligned variant of MainWindow._add_entry for a cleaner column look."""
        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(lbl, row, col * 2)
        entry = QLineEdit(default)
        entry.setFixedWidth(width)
        grid.addWidget(entry, row, col * 2 + 1)
        return entry

    # ------------------------------------------------------------------
    # Depth-search floor (draft + min freeboard)
    # ------------------------------------------------------------------
    def _hull_draft_in(self) -> float:
        """True draft of the current hull, inches, scraped from out.mlt.

        The SAME call main.py makes, deliberately: its own missing-file
        fallback is what the sweep will use, so borrowing it keeps the number
        shown here and the number that gates the run from disagreeing.  A
        private fallback of our own would drift the moment out.mlt went away —
        which is exactly when a user reads this label.
        """
        from geometry import parse_godzilla_dimensions
        try:
            cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        except (KeyError, AttributeError):
            cwd = os.getcwd()
        _len_ft, draft_in = parse_godzilla_dimensions(
            os.path.join(cwd, "out.mlt"))
        return float(draft_in)

    def _depth_floor_in(self) -> float:
        """The freeboard-required minimum mold depth, inches.

        This is main.py's ``MIN_MOLD_DEPTH`` before the 4-person displacement
        check, which can only RAISE it.  A UI-side LOWER BOUND on the real
        floor, then: a Max above this may still be rejected by the sweep, but
        a Max at or below it is certainly an empty search.
        """
        try:
            fb = float(self.freeboard_var.text())
        except (AttributeError, ValueError):
            import config as _cfg_fb
            fb = float(getattr(_cfg_fb, "FREEBOARD_MIN_IN", 6.0))
        return self._hull_draft_in() + fb

    def _refresh_depth_floor_hint(self) -> None:
        """Say how wide the searched band is, how many steps that is, and
        where it will sit.

        The BAND WIDTH leads, because it is the part that is certain.  Where
        the band sits is not: the sweep measures both bounds from the
        governing draft, and the 4-person check can move that from the
        2-person design draft to the heaviest crew's waterline.  Both ends
        move together, so the width is fixed and the position is indicative.

        Quoting a mold-depth range off the design draft as though it were
        settled is exactly the error this row shipped once: the minimum came
        from the 4-person waterline and the maximum from the 2-person draft,
        so "min 6.0 / max 6.5" was rejected as though the maximum were 7.99.

        The step count is here because the depth axis multiplies the
        configuration total -- a step made ten times finer is a sweep ten
        times longer, worth knowing before the run rather than after.
        """
        try:
            fb_min = float(self.freeboard_var.text())
            fb_max = float(self.freeboard_max_input.value())
            step = float(self.depth_step_input.value())
        except ValueError:
            self._depth_floor_hint.setText(
                "Freeboard range: — (minimum freeboard is not a number)")
            return
        if fb_max <= fb_min:
            self._depth_floor_hint.setText(
                f"⚠  maximum freeboard {fb_max:g} in is at or below the "
                f"minimum {fb_min:g} in — nothing would be searched.")
            return
        draft = self._hull_draft_in()
        n = max(int(math.ceil((fb_max - fb_min) / step - 1e-9)), 0)
        self._depth_floor_hint.setText(
            f"Searches {fb_max - fb_min:.2f} in of freeboard "
            f"({fb_min:.2f}–{fb_max:.2f}) in {n} step"
            f"{'' if n == 1 else 's'} of {step:g} in — mold depth "
            f"≈{draft + fb_min:.2f}–{draft + fb_max:.2f} in at the "
            f"{draft:.2f} in design draft. The heaviest crew sits deeper, "
            f"which slides the whole band down the hull without changing "
            f"its width or the number of steps.")

    # ------------------------------------------------------------------
    # Sweep-configuration presets (save / reload named search boundaries)
    # ------------------------------------------------------------------
    def _preset_registry(self) -> dict:
        """Map preset key -> widget for every control that defines a sweep
        configuration.  Serialisation is generic on the widget type, so adding a
        control to a preset is a one-line entry here."""
        return {
            # Mix & Pour
            "mix_max": self.mix_max_var, "mix_min": self.mix_min_var,
            "n_mixes": self.n_mixes_spin,
            "n_layers": self.n_layers_spin, "density_step": self.density_step_input,
            "existing_mixes_only": self.existing_mixes_only_var,
            "price_mix_development": self.price_mix_dev_var,
            # Geometry search bounds
            "min_thickness": self.min_thickness_input,
            "max_thickness": self.max_thickness_input,
            "thickness_step": self.thickness_step_input,
            "thickness_override": self.thickness_override_checkbox,
            # "freeboard_max" bounds the SAME quantity as "freeboard".  Presets
            # written earlier carry "depth_max" (an absolute mold depth) or
            # "depth_span" (a span above the floor); both are converted on load.
            "freeboard": self.freeboard_var,
            "freeboard_max": self.freeboard_max_input,
            "depth_step": self.depth_step_input,
            "min_gw_width": self.min_gw_width_input, "max_gw_width": self.max_gw_width_input,
            "gw_steps": self.gw_steps_input,
            "min_gw_height": self.min_gw_height_input, "max_gw_height": self.max_gw_height_input,
            "flare_min": self.flare_min_input, "flare_max": self.flare_max_input,
            "stem_rocker": self.stem_rocker_input,
            "stem_rocker_auto": self.stem_rocker_auto_checkbox,
            "enable_gunwales": self.enable_gunwales_checkbox,
            "enable_mesh": self.enable_mesh_checkbox, "fillet_ratio": self.fillet_ratio_input,
            # Load cases & safety
            "fos": self.fos_spinbox,
            "kupfer_accept": self.kupfer_accept_input,
            "design_method": self.design_method_combo,
            "gamma_dead": self.gamma_dead_input,
            "gamma_live": self.gamma_live_input,
            "phi_resistance": self.phi_input, "omega_safety": self.omega_input,
            "maneuver_speed": self.maneuver_speed_input,
            "maneuver_radius": self.maneuver_radius_input,
            "advanced_loading": self.advanced_loading_var,
            "crew4": self.crew4_var, "crew3": self.crew3_var, "crew2": self.crew2_var,
            "optimize_seating": self.optimize_seating_checkbox,
            "pos_variance": self.pos_variance_var,
            "pos4": self.pos4_var, "pos3": self.pos3_var, "pos2": self.pos2_var,
            # Solver precision
            "precision": self.precision_slider, "stations": self.stations_input,
        }

    def _collect_preset(self) -> dict:
        """Snapshot the current settings into a JSON-friendly dict."""
        out = {}
        for key, w in self._preset_registry().items():
            if isinstance(w, QCheckBox):
                out[key] = bool(w.isChecked())
            elif isinstance(w, QLineEdit):
                out[key] = w.text()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            else:                                    # QSpinBox / QDoubleSpinBox / QSlider
                out[key] = w.value()
        return out

    def _migrate_preset(self, settings: dict) -> dict:
        """Translate presets written against the OLD control semantics.

        Controls that changed MEANING rather than merely moving, so a stale
        preset that loaded 'successfully' would silently search a different
        grid.  Old keys are converted, never dropped:

        * ``depth_span`` was a span ABOVE the computed floor; ``depth_max`` was
          an absolute mold depth; ``freeboard_max`` bounds freeboard itself.
          The depth row has been all three, so the chain runs in order --
          span → depth → freeboard -- and a preset from any generation lands
          on the current field.
        * ``thickness_step`` used 0 as the "buildable grid" sentinel; the
          override checkbox carries that meaning now, so a positive saved step
          means the preset wanted the override on.
        """
        out = dict(settings or {})
        if "depth_span" in out and "depth_max" not in out:
            try:
                span = float(out["depth_span"])
                # Honour this preset's own freeboard, not the one on screen.
                _fb_now = self.freeboard_var.text()
                if "freeboard" in out:
                    self.freeboard_var.setText(str(out["freeboard"]))
                floor = self._depth_floor_in()
                self.freeboard_var.setText(_fb_now)
                # Truncate, do not round — rounding UP past floor+span admits
                # a depth the saved preset never searched (see the field's
                # default, same half-open-interval reason).
                out["depth_max"] = math.floor((floor + span) * 1000.0) / 1000.0
                print(f"  Preset: depth span {span:g} in above the "
                      f"{floor:.2f} in floor → maximum depth "
                      f"{out['depth_max']:.3f} in.")
            except (TypeError, ValueError):
                pass
            out.pop("depth_span", None)

        # Second hop: an absolute mold depth becomes a freeboard ceiling.  The
        # draft is what separates them, and it is the HULL's, not the preset's
        # -- so a preset saved against one hull converts against the hull
        # loaded now.  That is the honest reading of "search up to this much
        # freeboard"; carrying the old absolute depth across a hull change
        # would silently mean a different amount of hull above the water.
        if "depth_max" in out and "freeboard_max" not in out:
            try:
                depth_max = float(out["depth_max"])
                draft = self._hull_draft_in()
                out["freeboard_max"] = round(max(depth_max - draft, 0.5), 3)
                print(f"  Preset: maximum mold depth {depth_max:.2f} in "
                      f"→ maximum freeboard {out['freeboard_max']:.3f} in "
                      f"(draft {draft:.2f} in).")
            except (TypeError, ValueError):
                pass
            out.pop("depth_max", None)

        if "thickness_step" in out and "thickness_override" not in out:
            try:
                out["thickness_override"] = float(out["thickness_step"]) > 1e-9
            except (TypeError, ValueError):
                out["thickness_override"] = False
            if not out["thickness_override"]:
                # 0 was the sentinel, never a real step — do not write it into
                # a spin box whose minimum is now 1/32 in.
                out.pop("thickness_step", None)
        return out

    def _apply_preset(self, settings: dict) -> None:
        """Write a saved settings dict back into the widgets.  Signals stay live
        so the dependent enable-states/labels (gunwale rows, seating mode, rocker
        auto, FoS slider, precision label) re-sync through their own handlers."""
        settings = self._migrate_preset(settings)
        reg = self._preset_registry()
        for key, val in (settings or {}).items():
            w = reg.get(key)
            if w is None:
                continue
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                elif isinstance(w, QLineEdit):
                    w.setText(str(val))
                elif isinstance(w, QComboBox):
                    w.setCurrentText(str(val))
                elif isinstance(w, QDoubleSpinBox):
                    w.setValue(float(val))
                else:                                # QSpinBox / QSlider (integer)
                    w.setValue(int(round(float(val))))
            except (TypeError, ValueError):
                pass

    def _refresh_preset_dropdown(self, select: str | None = None) -> None:
        """Repopulate the dropdown from disk (signals blocked so it does not
        fire a load), optionally selecting ``select``."""
        from data import sweep_presets
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— Load preset —")
        for nm in sweep_presets.names():
            self.preset_combo.addItem(nm)
        if select:
            idx = self.preset_combo.findText(select)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)

    def _on_preset_selected(self, idx: int) -> None:
        if idx <= 0:                                 # the "— Load preset —" placeholder
            return
        from data import sweep_presets
        name = self.preset_combo.currentText()
        settings = sweep_presets.get(name)
        if settings is None:
            self._refresh_preset_dropdown()
            return
        self._apply_preset(settings)
        print(f"  Loaded sweep preset '{name}'.")

    def _on_save_preset(self) -> None:
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        from data import sweep_presets
        default = (self.preset_combo.currentText()
                   if self.preset_combo.currentIndex() > 0 else "")
        name, ok = QInputDialog.getText(self, "Save Sweep Preset",
                                        "Preset name:", text=default)
        name = (name or "").strip()
        if not ok or not name:
            return
        if sweep_presets.get(name) is not None:
            if QMessageBox.question(
                    self, "Overwrite preset?",
                    f"A preset named '{name}' already exists. Overwrite it?"
            ) != QMessageBox.StandardButton.Yes:
                return
        try:
            sweep_presets.save(name, self._collect_preset())
        except Exception as exc:                     # disk/permission issues
            QMessageBox.warning(self, "Save failed", f"Could not save preset:\n{exc}")
            return
        self._refresh_preset_dropdown(select=name)
        print(f"  Saved sweep preset '{name}'.")

    def _on_delete_preset(self) -> None:
        from PyQt6.QtWidgets import QMessageBox
        from data import sweep_presets
        if self.preset_combo.currentIndex() <= 0:
            QMessageBox.information(self, "No preset selected",
                                    "Pick a preset from the dropdown to delete.")
            return
        name = self.preset_combo.currentText()
        if QMessageBox.question(self, "Delete preset?",
                                f"Delete the preset '{name}'?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        sweep_presets.delete(name)
        self._refresh_preset_dropdown()
        print(f"  Deleted sweep preset '{name}'.")

    def _build_ui(self):
        from ui.style import C
        from ui.widgets import PipelineStepper

        # The tab is a column now, not a bare splitter: the pipeline stepper
        # spans the full width above the workspace, because "which stage am I
        # at" is a question about the whole run and not about either pane.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.pipeline = PipelineStepper(active="structural")
        self.pipeline.setStyleSheet(
            f"PipelineStepper {{ background:{C.BG_PANEL}; "
            f"border-bottom:1px solid {C.BORDER}; }}")
        layout.addWidget(self.pipeline)

        workspace = QHBoxLayout()
        workspace.setContentsMargins(10, 10, 10, 8)
        layout.addLayout(workspace, stretch=1)

        # ── Left: Settings — a pinned run block over independent section cards ──
        left = QWidget()
        left.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        # ── Pinned run block ──────────────────────────────────────────────
        # Hull status and the Run button used to sit at opposite ends of a
        # scrolling column: the two facts that decide whether you CAN run, and
        # the control that runs it, could not be seen at once.  Both are
        # pinned here, above the scroll, and neither moves.
        self._run_block = QWidget()
        _rb = QVBoxLayout(self._run_block)
        _rb.setContentsMargins(14, 12, 14, 12)
        _rb.setSpacing(8)

        self._pinned_status = {}
        for _fname in ("off1.mlt", "out.mlt"):
            _row = QHBoxLayout()
            _row.setSpacing(7)
            _dot = QLabel("●")
            _dot.setStyleSheet(f"color:{C.TEXT_GHOST}; font-size:11px;")
            _nm = QLabel(_fname)
            _nm.setStyleSheet(f"color:{C.TEXT_DIM}; font-size:11px;")
            _state = QLabel("missing")
            _state.setStyleSheet(
                f"color:{C.TEXT_GHOST}; font-family:{C.MONO}; font-size:10px;")
            _row.addWidget(_dot); _row.addWidget(_nm)
            _row.addStretch(1); _row.addWidget(_state)
            _rb.addLayout(_row)
            self._pinned_status[_fname] = (_dot, _state)

        self._pinned_hull_label = QLabel("no hull loaded")
        self._pinned_hull_label.setWordWrap(True)
        self._pinned_hull_label.setStyleSheet(
            f"color:{C.TEXT_GHOST}; font-family:{C.MONO}; font-size:10px;")
        _rb.addWidget(self._pinned_hull_label)

        self.sweep_only_btn = QPushButton("▶  Run Structural Sweep")
        self.sweep_only_btn.setObjectName("primary")
        self.sweep_only_btn.setFixedHeight(44)
        self.sweep_only_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.sweep_only_btn.setToolTip(
            "Run the sweep against the uploaded off1.mlt + out.mlt.\n"
            "Godzilla is launched from the Hull Configurator tab.")
        self.sweep_only_btn.setEnabled(False)
        self.sweep_only_btn.clicked.connect(self._start_structural_only)
        _rb.addWidget(self.sweep_only_btn)

        _acts = QHBoxLayout()
        _acts.setSpacing(8)
        self.report_btn = QPushButton("Generate Report")
        self.report_btn.setFixedHeight(30)
        self.report_btn.clicked.connect(self._start_report)
        _acts.addWidget(self.report_btn, 1)
        self.export_btn = QPushButton("Export Package (.zip)")
        self.export_btn.setFixedHeight(30)
        self.export_btn.setToolTip(
            "Bundle session JSON, PDF, CSV exports, and audit log into one zip file.")
        self.export_btn.clicked.connect(self._export_design_package)
        _acts.addWidget(self.export_btn, 1)
        _rb.addLayout(_acts)

        self._run_block.setStyleSheet(
            f"QWidget#runblock {{ background:{C.BG_PANEL}; "
            f"border:1px solid {C.BORDER}; border-radius:10px; }}")
        self._run_block.setObjectName("runblock")
        left_layout.addWidget(self._run_block)

        # ── Preset bar: save / reload named sweep configurations (persisted) ──
        _preset_bar = QHBoxLayout()
        _preset_bar.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip(
            "Load a saved sweep configuration (search bounds, steps, load cases,\n"
            "solver precision). Presets persist across sessions.")
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        _preset_bar.addWidget(self.preset_combo, stretch=1)
        self.save_preset_btn = QPushButton("Save Current Settings")
        self.save_preset_btn.setToolTip(
            "Save every current sweep setting as a named preset you can reload later.")
        self.save_preset_btn.clicked.connect(self._on_save_preset)
        _preset_bar.addWidget(self.save_preset_btn)
        self.delete_preset_btn = QPushButton("Delete")
        self.delete_preset_btn.setToolTip("Delete the selected preset.")
        self.delete_preset_btn.clicked.connect(self._on_delete_preset)
        _preset_bar.addWidget(self.delete_preset_btn)
        left_layout.addLayout(_preset_bar)
        self._refresh_preset_dropdown()      # list any presets saved previously

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # _CardStack keeps QToolBox's addItem/setCurrentIndex signature, so the
        # five category blocks below are untouched by the redesign — only how
        # they are PRESENTED changed, and a settings change and a layout change
        # in one diff is how a control quietly loses its meaning.
        self._settings_toolbox = _CardStack(default_expanded=(0, 1))
        scroll.setWidget(self._settings_toolbox)
        left_layout.addWidget(scroll, stretch=1)

        # Hidden compatibility widgets: hull geometry/environment are owned by
        # ManualHullTab; the mix/engine widgets are kept so legacy backend
        # methods (_refresh_mix_display, _start_godzilla, …) don't AttributeError.
        self.weight_var     = QLineEdit("730")
        self.disp_tol_var   = QLineEdit("1.0")
        self.length_var     = QLineEdit("20.0")
        self.min_length_var = QLineEdit("18.0")
        self.draft_var      = QLineEdit("0.20")
        self.water_rho_var  = QLineEdit("998")
        self.water_visc_var = QLineEdit("1.139")
        self.gravity_var    = QLineEdit("9.807")
        self._mix_display = QTextEdit()
        self._regression_label = QLabel("")
        self.engine_var = QComboBox()
        self.engine_var.addItems(["Legacy Michlet (DOS visualizer)",
                                  "Python (pymoo / CMA-ES)"])

        # small helpers used across the pages
        def _spin(lo, hi, step, dec, val, tip="", w=90):
            s = NoScrollDoubleSpinBox()
            s.setRange(lo, hi); s.setSingleStep(step); s.setDecimals(dec)
            s.setValue(val); s.setMaximumWidth(w)
            # Hide the up/down arrows so numeric inputs read as clean text boxes.
            s.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            if tip:
                s.setToolTip(tip)
            return s

        def _hint(text, tip):
            lbl = QLabel(text); lbl.setEnabled(False)
            lbl.setStyleSheet("color:#888; font-style:italic;")
            lbl.setToolTip(tip)
            return lbl

        # ══════════════════════════════════════════════════════════════════
        #  Category 1 — File Input  (step 1 for the user)
        # ══════════════════════════════════════════════════════════════════
        p_file = QWidget(); vf = QVBoxLayout(p_file); vf.setSpacing(6)
        _fi_note = QLabel("Upload the off1.mlt + out.mlt from a previous "
                          "Godzilla run (or the Hull Configurator).")
        _fi_note.setWordWrap(True); _fi_note.setStyleSheet("color:#999;")
        vf.addWidget(_fi_note)

        _r1 = QHBoxLayout()
        self._off1_label = QLabel("❌  off1.mlt  not found")
        self._off1_label.setFont(QFont("Consolas", 10))
        _r1.addWidget(self._off1_label, 1)
        _off1_btn = QPushButton("Upload"); _off1_btn.setFixedSize(72, 24)
        _off1_btn.setStyleSheet("QPushButton { background-color: #555; }")
        _off1_btn.clicked.connect(lambda: self._upload_godzilla_file("off1.mlt"))
        _r1.addWidget(_off1_btn); vf.addLayout(_r1)

        _r2 = QHBoxLayout()
        self._out_label = QLabel("❌  out.mlt   not found")
        self._out_label.setFont(QFont("Consolas", 10))
        _r2.addWidget(self._out_label, 1)
        _out_btn = QPushButton("Upload"); _out_btn.setFixedSize(72, 24)
        _out_btn.setStyleSheet("QPushButton { background-color: #555; }")
        _out_btn.clicked.connect(lambda: self._upload_godzilla_file("out.mlt"))
        _r2.addWidget(_out_btn); vf.addLayout(_r2)

        _refresh_fs_btn = QPushButton("↻ Refresh file status")
        _refresh_fs_btn.setFixedSize(160, 22)
        _refresh_fs_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #555; "
            "color: gray; font-size: 10px; }")
        _refresh_fs_btn.clicked.connect(self._refresh_file_status)
        vf.addWidget(_refresh_fs_btn)

        self._active_hull_path = None
        self._active_hull_label = QLabel(
            "Active Hull File: (none — upload or run Hull Configurator)")
        self._active_hull_label.setFont(QFont("Consolas", 9))
        self._active_hull_label.setWordWrap(True)
        self._active_hull_label.setStyleSheet("color: #aaaaaa;")
        vf.addWidget(self._active_hull_label)
        vf.addStretch(1)
        self._settings_toolbox.addItem(p_file, "📁  1.  File Input")

        # ══════════════════════════════════════════════════════════════════
        #  Category 2 — Mix & Pour Strategy
        # ══════════════════════════════════════════════════════════════════
        p_mix = QWidget(); mf = QFormLayout(p_mix); mf.setSpacing(6)

        # Seeded from canoe_suite.cfg (mix.max_density_pcf) — the file sets what
        # the field starts at, this field then bounds the run.  Read through the
        # settings singleton, the same way the stem-rocker default is.
        from config.settings import get_settings as _get_settings_mix
        self.mix_max_var = QLineEdit(
            f"{float(_get_settings_mix().mix.max_density_pcf):g}")
        self.mix_max_var.setMaximumWidth(90)
        self.mix_max_var.setToolTip(
            "Density ceiling for the concrete mix sweep (pcf).\n"
            "Higher density = stronger but heavier hull.\n"
            "Starts from canoe_suite.cfg (mix.max_density_pcf); whatever is\n"
            "in THIS field is what bounds the run.")
        mf.addRow("Max Mix (pcf):", self.mix_max_var)

        # The team's density FLOOR.  It existed in canoe_suite.cfg and in
        # config.MIN_DENSITY_PCF all along, documented as "the team's stated
        # floor, NOT the sweep's -- nothing gates on it today": a number the
        # user could set that changed nothing.  It bounds the grid now, and
        # this field is where it is set.
        #
        # It can only RAISE the Mix Lab regression's viability floor.  Below
        # that floor the regression predicts no strength, so those densities
        # are configs that get built, evaluated and rejected -- sweep time
        # spent to reach a foregone conclusion.
        self.mix_min_var = QLineEdit(
            f"{float(_get_settings_mix().mix.min_density_pcf):g}")
        self.mix_min_var.setMaximumWidth(90)
        self.mix_min_var.setToolTip(
            "Density FLOOR for the concrete mix sweep (pcf).\n"
            "A manufacturing constraint — 'we will not batch below this' —\n"
            "not a claim about strength.\n\n"
            "The Mix Lab regression has its own floor (the lightest mix it\n"
            "still predicts strength for). Whichever is HIGHER wins, so a\n"
            "value below it changes nothing; the range below shows what is\n"
            "actually searched.\n"
            "Starts from canoe_suite.cfg (mix.min_density_pcf).")
        mf.addRow("Min Mix (pcf):", self.mix_min_var)

        # The range the sweep will ACTUALLY search, shown where it is set.  Both
        # ends can now come from a field OR from the regression, and a sweep
        # bounded into a range with no viable design looks exactly like a
        # physics failure unless you can see which bound did it.
        self._mix_range_label = QLabel()
        self._mix_range_label.setWordWrap(True)
        self._mix_range_label.setStyleSheet("color:#8899aa; font-size:11px;")
        mf.addRow("", self._mix_range_label)
        self.mix_max_var.textChanged.connect(self._refresh_mix_range_label)
        self.mix_min_var.textChanged.connect(self._refresh_mix_range_label)

        self.n_mixes_spin = NoScrollSpinBox(); self.n_mixes_spin.setRange(1, 3)
        self.n_mixes_spin.setValue(2); self.n_mixes_spin.setMaximumWidth(90)
        self.n_mixes_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.n_mixes_spin.setToolTip(
            "Distinct concrete mixes the sweep evaluates.\n"
            "1 = uniform hull.  2 = heavy midbody + light tips (default).\n"
            "3 = heavy + medium + light.\n"
            "More than one mix automatically pours a composite I-beam\n"
            "(3-zone Structural/Light/Structural stack).")
        mf.addRow("Number of Mixes:", self.n_mixes_spin)

        import config as _cfg_nl
        self.n_layers_spin = NoScrollSpinBox(); self.n_layers_spin.setRange(1, 8)
        self.n_layers_spin.setValue(
            int(getattr(_cfg_nl, "N_CONSTRUCTION_LAYERS", 3)))
        self.n_layers_spin.setMaximumWidth(90)
        self.n_layers_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.n_layers_spin.setToolTip(
            "Through-thickness construction layers: successive equal troweled\n"
            "passes that build the wall up to its thickness (default 3 →\n"
            "0.375\" wall = 3 × 0.125\").  ORTHOGONAL to the pour zones and the\n"
            "mix count — a derived manufacturing output that drives the report's\n"
            "layer schedule, the trowel-ability check and the punching mesh\n"
            "depth.  It is NOT a sweep input: the structural winner is\n"
            "independent of this value.")
        mf.addRow("Construction Layers:", self.n_layers_spin)

        # Minimum 0.1 pcf.  The sweep PAIRS densities, so the combo count
        # grows QUADRATICALLY as the step shrinks: over a ~20 pcf viable
        # range, 0.5 -> ~861 pairs, 0.25 -> ~3.4k, 0.1 -> ~21k.  The tooltip
        # states it because the cost is not visible from the control itself.
        self.density_step_input = _spin(0.1, 5.0, 0.05, 2, 0.5,
            "Mix-density search increment (pcf). Finer resolves lighter optima but multiplies the grid QUADRATICALLY: the sweep PAIRS densities, so halving the step roughly quadruples the combo count.\nOver a ~20 pcf viable range: 0.5 -> ~861 pairs, 0.25 -> ~3.4k, 0.1 -> ~21k.\nRange is min-viable .. Max Mix.")
        mf.addRow("Density step (pcf):", self.density_step_input)

        self.existing_mixes_only_var = QCheckBox(
            "Evaluate using existing mixes only (disable interpolation)")
        self.existing_mixes_only_var.setChecked(False)
        self.existing_mixes_only_var.setToolTip(
            "MANUFACTURING CONSTRAINT, like the distinct-mix cap.\n\n"
            "Off (default): the sweep searches a continuous density spectrum\n"
            "and the Mix Lab regression predicts strengths for densities the\n"
            "team has never batched — the winner may be an interpolated mix.\n\n"
            "On: the sweep searches ONLY densities that exist in the Mix Lab\n"
            "(mix library + tested batches), so the winner is pourable as-is.\n"
            "This is a real restriction and usually costs weight — the range\n"
            "below shows exactly what is searched either way.")
        mf.addRow(self.existing_mixes_only_var)

        self.price_mix_dev_var = QCheckBox(
            "Price mix development (slow — runs the search several more times)")
        self.price_mix_dev_var.setChecked(False)
        self.price_mix_dev_var.setToolTip(
            "Answers: what would developing each missing mix actually save?"
            "\n\n"
            "It re-runs the ENTIRE search once for the mixes you already"
            " have, then once per target mix, then cumulatively --\n"
            "five extra searches for two targets. Measured at 93% of a"
            " run's total time.\n\n"
            "Off (default): the sweep still TELLS you which target mixes"
            " it wants; it just does not price them.\n"
            "A missing payoff table means NOT PRICED, never 'worth"
            " nothing'.\n\n"
            "On: use it when you are deciding what to batch next, not on"
            " every design iteration.")

        mf.addRow(self.price_mix_dev_var)

        # The effective range depends on the ceiling, the step, the mix count
        # and the existing-mixes constraint — refresh from all four (and once
        # now that they all exist).
        self.n_mixes_spin.valueChanged.connect(self._refresh_mix_range_label)
        self.density_step_input.valueChanged.connect(self._refresh_mix_range_label)
        self.existing_mixes_only_var.toggled.connect(self._refresh_mix_range_label)
        self._refresh_mix_range_label()

        # >1 mix ⇒ composite I-beam pour is automatic (fixed 3-zone stack); there
        # is no user-facing zone count any more.
        self._settings_toolbox.addItem(p_mix, "📁  2.  Mix & Pour Strategy")

        # ══════════════════════════════════════════════════════════════════
        #  Category 3 — Geometry Search Bounds  (unified Variable|Min|Max|Step)
        # ══════════════════════════════════════════════════════════════════
        p_geom = QWidget(); vg = QVBoxLayout(p_geom); vg.setSpacing(6)
        grid = QGridLayout(); grid.setSpacing(4)
        grid.setColumnStretch(0, 1)
        for c, h in enumerate(["Variable", "Min", "Max", "Step"]):
            _h = QLabel(h); _h.setStyleSheet("font-weight: bold;")
            grid.addWidget(_h, 0, c)
        _IN_32 = 1.0 / 32.0
        gr = 1

        # Wall thickness — Min/Max free; step is the fixed buildable grid.
        # Seeded from canoe_suite.cfg (structural.thickness_options_in): the
        # file supplies the RANGE, these fields override it per run.
        import config as _cfg_t
        self.min_thickness_input = _spin(0.125, 2.0, _IN_32, 4,
            float(getattr(_cfg_t, "THICKNESS_MIN_IN", 0.375)),
            "Lowest wall thickness the sweep tries (in).\n"
            "Starts from canoe_suite.cfg (structural.thickness_options_in).")
        self.max_thickness_input = _spin(0.125, 3.0, _IN_32, 4,
            float(getattr(_cfg_t, "THICKNESS_MAX_IN", 0.625)),
            "Highest wall thickness the sweep tries (in).\n"
            "Starts from canoe_suite.cfg (structural.thickness_options_in).")
        # Thickness step: a CUSTOM uniform step that replaces the buildable
        # grid.  It is gated by the override checkbox below, not by a zero
        # sentinel — 0.0000 used to mean "use the buildable grid", which read
        # as a broken control and hid a working advanced feature.  The step
        # therefore cannot be zero any more; whether it applies is the
        # checkbox's business, and the value survives hide/show untouched.
        self.thickness_step_input = _spin(_IN_32, 1.0, _IN_32, 4, _IN_32,
            "Custom wall-thickness search step (in) — only when the override\n"
            "below is checked.\n\n"
            "Steps uniformly from Min to Max, ignoring the buildable grid, so\n"
            "the winner may not fall on a thickness the team can trowel.")
        # The step shares its cell with the hint that stands in for it while
        # the buildable grid is in force, so the row keeps its height and the
        # Step column does not jump when the override is toggled.
        self._t_step_cell = QWidget()
        _t_step_box = QHBoxLayout(self._t_step_cell)
        _t_step_box.setContentsMargins(0, 0, 0, 0); _t_step_box.setSpacing(0)
        self._t_grid_hint = _hint("buildable grid",
            "Thickness snaps to buildable trowel increments (1/32\" & 0.5 mm),\n"
            "divisible by the mix count — there is no step to set.\n"
            "Check 'Override buildable thickness grid' below to set one.")
        _t_step_box.addWidget(self._t_grid_hint)
        _t_step_box.addWidget(self.thickness_step_input)
        self.thickness_step_input.setVisible(False)
        grid.addWidget(QLabel("Wall Thickness (in)"), gr, 0)
        grid.addWidget(self.min_thickness_input, gr, 1)
        grid.addWidget(self.max_thickness_input, gr, 2)
        grid.addWidget(self._t_step_cell, gr, 3)
        gr += 1

        def _sync_thickness_bounds(*_):
            if self.max_thickness_input.value() < self.min_thickness_input.value():
                self.max_thickness_input.setValue(self.min_thickness_input.value())
        self.min_thickness_input.valueChanged.connect(_sync_thickness_bounds)

        # Freeboard — Min and Max of ONE quantity: how much hull stands above
        # the water.  Mold depth is the derived number (draft + freeboard), and
        # the conversion happens in the sweep against the draft that run
        # parsed, so this panel and the sweep cannot convert against different
        # drafts.
        #
        # The row has now carried three meanings.  It began as floor + SPAN
        # while the header said Max, so "min 6 / max 4.5" looked inverted but
        # was valid.  Then Max became an absolute MOLD DEPTH, which made the
        # min<max validation safe but left the two columns holding different
        # quantities — a minimum freeboard beside a maximum depth, with the
        # draft silently between them.  They are the same quantity now, which
        # is what the Variable|Min|Max|Step header claimed all along.
        self.freeboard_var = QLineEdit(
            f"{float(getattr(_cfg_t, 'FREEBOARD_MIN_IN', 6.0)):g}")
        self.freeboard_var.setMaximumWidth(90)
        self.freeboard_var.setToolTip(
            "MINIMUM freeboard (in) — the least hull that may stand above the\n"
            "waterline. The ASCE rule; the sweep rejects hulls violating it.\n\n"
            "The 4-person crew can force this HIGHER: if the heaviest crew\n"
            "sinks the hull past this, the sweep raises the floor and says so.\n"
            "Starts from canoe_suite.cfg (structural.freeboard_min_in).")
        # Default 10.5 in = the old shipped search (a 4.5 in span above a 6 in
        # freeboard floor) restated as a ceiling on the same quantity.  The
        # depths searched are unchanged on any hull: both ends convert through
        # the same draft, so the span between them is still 4.5 in.
        self.freeboard_max_input = _spin(0.5, 24.0, 0.5, 3, 10.5,
            "MAXIMUM freeboard (in) — the most hull that may stand above the\n"
            "waterline.\n\n"
            "The sweep searches freeboard from Min to Max; mold depth is\n"
            "derived (draft + freeboard) and shown below. A maximum at or\n"
            "below the minimum searches nothing and is rejected before the\n"
            "sweep starts.")
        # A finer floor than the old 0.125 in: freeboard is a shallower
        # quantity than mold depth and a 1/8 in grid is coarse against a ~4 in
        # range.  0.01 in is the limit of the field's own precision.
        #
        # It buys resolution at a real price -- the depth axis is a MULTIPLIER
        # on the whole config count, so 0.5 -> 0.05 in is ten times the sweep,
        # not ten more evaluations.  The searched-range banner prints the depth
        # count so the cost is visible before the run rather than after.
        self.depth_step_input = _spin(0.01, 2.0, 0.0625, 3, 0.5,
            "Freeboard search increment (in). Finer tries more designs.\n\n"
            "This axis MULTIPLIES the configuration count: halving the step\n"
            "doubles the whole sweep. The range note below shows how many\n"
            "steps the current setting produces.")
        grid.addWidget(QLabel("Freeboard (in)"), gr, 0)
        grid.addWidget(self.freeboard_var, gr, 1)
        grid.addWidget(self.freeboard_max_input, gr, 2)
        grid.addWidget(self.depth_step_input, gr, 3)
        gr += 1

        # Gunwale width — full Min/Max/Step (step = shared linspace count).
        self.min_gw_width_input = _spin(0.25, 6.0, 0.25, 2, 0.5)
        self.max_gw_width_input = _spin(0.25, 8.0, 0.25, 2, 2.5)
        self.gw_steps_input = NoScrollSpinBox(); self.gw_steps_input.setRange(2, 20)
        self.gw_steps_input.setValue(5); self.gw_steps_input.setMaximumWidth(90)
        self.gw_steps_input.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.gw_steps_input.setToolTip(
            "Number of evenly-spaced steps from Min to Max — shared by gunwale\n"
            "width and height.")
        grid.addWidget(QLabel("Gunwale Width (in)"), gr, 0)
        grid.addWidget(self.min_gw_width_input, gr, 1)
        grid.addWidget(self.max_gw_width_input, gr, 2)
        grid.addWidget(self.gw_steps_input, gr, 3)
        gr += 1

        # Gunwale height — shares the width's step count.
        self.min_gw_height_input = _spin(0.25, 6.0, 0.25, 2, 0.5)
        self.max_gw_height_input = _spin(0.25, 8.0, 0.25, 2, 3.0)
        grid.addWidget(QLabel("Gunwale Height (in)"), gr, 0)
        grid.addWidget(self.min_gw_height_input, gr, 1)
        grid.addWidget(self.max_gw_height_input, gr, 2)
        grid.addWidget(_hint("shared count",
            "Gunwale width & height share the one step count (Width row)."), gr, 3)
        gr += 1

        # Flare — Min/Max bound the search; the angle itself is auto-found.
        self.flare_min_input = _spin(0.0, 30.0, 1.0, 1, 0.0,
            "Lower bound of the flare-angle search (deg).")
        self.flare_max_input = _spin(0.0, 60.0, 1.0, 1, 15.0,
            "Upper bound of the flare-angle search (deg).")
        grid.addWidget(QLabel("Flare Angle (deg)"), gr, 0)
        grid.addWidget(self.flare_min_input, gr, 1)
        grid.addWidget(self.flare_max_input, gr, 2)
        grid.addWidget(_hint("auto-found",
            "The minimum feasible flare angle is found by a feasibility gate,\n"
            "not stepped — only Min/Max bound the search."), gr, 3)
        gr += 1

        # Stem overhang — a measured geometric property, never swept.
        self.stem_rocker_input = _spin(0.0, 36.0, 0.5, 2, 1.0,
            "STEM OVERHANG (tip cantilever extent) at each end, in.\n"
            "0 = vertical stem ends.\n\n"
            "A truthful geometric INPUT, not a design variable: its un-buoyed\n"
            "cantilever mass raises tip hogging and RELIEVES midship sagging,\n"
            "so an optimiser allowed to choose it would lengthen it to buy\n"
            "margin — a very poor weight-per-unit-margin trade next to mix\n"
            "density or a thickness step.  Set it to what the hull IS.")
        grid.addWidget(QLabel("Stem Overhang (in/end)"), gr, 0)
        grid.addWidget(self.stem_rocker_input, gr, 1)
        grid.addWidget(_hint("measured, not swept",
            "One value (derived or assumed) describing the hull as built --\n"
            "never a search variable."), gr, 2)
        grid.addWidget(_hint("Un-buoyed tip mass",
            "The overhang extends the beam past the supported span; its\n"
            "cantilevered dead weight is in the beam self-weight (not CAD-only).\n"
            "It raises tip hogging, which OPPOSES midship sagging and so lowers\n"
            "the governing D/C -- which is exactly why it must be truthful."), gr, 3)
        gr += 1
        vg.addLayout(grid)

        # The depth floor is COMPUTED (draft + min freeboard) and enforced no
        # matter what the user types, so a Max below it is an empty search
        # rather than a tight one.  Show the number that decides it, and keep
        # it live as the freeboard is edited.
        self._depth_floor_hint = QLabel("")
        self._depth_floor_hint.setWordWrap(True)
        self._depth_floor_hint.setStyleSheet("color:#888; font-style:italic;")
        vg.addWidget(self._depth_floor_hint)
        # All three inputs feed the note, so all three refresh it — a hint that
        # only tracks one of them is worse than none, because it looks current.
        self.freeboard_var.textChanged.connect(
            lambda *_: self._refresh_depth_floor_hint())
        self.freeboard_max_input.valueChanged.connect(
            lambda *_: self._refresh_depth_floor_hint())
        self.depth_step_input.valueChanged.connect(
            lambda *_: self._refresh_depth_floor_hint())
        self._refresh_depth_floor_hint()

        # Buildable-grid override (advanced).  Unchecked, thickness snaps to
        # the trowel increments and there is no step to set; checked, the
        # custom step above replaces that grid.
        self.thickness_override_checkbox = QCheckBox(
            "Override buildable thickness grid")
        self.thickness_override_checkbox.setChecked(False)
        self.thickness_override_checkbox.setToolTip(
            "ADVANCED.  Off (default): wall thickness snaps to buildable\n"
            "trowel increments (1/32\" & 0.5 mm), divisible by the mix count,\n"
            "so every candidate is one the team can actually pour.\n\n"
            "On: reveals a custom uniform step that replaces that grid.  The\n"
            "winner may then land on a thickness nobody can trowel, so the\n"
            "sweep banner says 'buildable grid overridden' when it does.")
        vg.addWidget(self.thickness_override_checkbox)

        def _on_thickness_override_toggled(checked):
            # The step keeps its value while hidden — this only swaps which of
            # the two widgets in the Step cell is showing.
            self.thickness_step_input.setVisible(bool(checked))
            self._t_grid_hint.setVisible(not checked)
        self.thickness_override_checkbox.toggled.connect(
            _on_thickness_override_toggled)
        _on_thickness_override_toggled(
            self.thickness_override_checkbox.isChecked())

        # Gunwale enable + mesh + fillet (extras beyond min/max/step)
        self.enable_gunwales_checkbox = QCheckBox("Evaluate Thickened Gunwales")
        self.enable_gunwales_checkbox.setChecked(False)
        self.enable_gunwales_checkbox.setToolTip(
            "Adds a rectangular gunwale strip at the top of each section and\n"
            "enables the Gunwale Width/Height rows above.")
        vg.addWidget(self.enable_gunwales_checkbox)

        self.enable_mesh_checkbox = QCheckBox("Include Composite Mesh Reinforcement")
        self.enable_mesh_checkbox.setChecked(False)
        self.enable_mesh_checkbox.setEnabled(False)
        self.enable_mesh_checkbox.setToolTip(
            "Raises the top-fibre tensile allowable to 800 psi (hogging check).\n"
            "Only active when Evaluate Thickened Gunwales is checked.")
        vg.addWidget(self.enable_mesh_checkbox)

        _fillet_row = QHBoxLayout()
        _fillet_row.addWidget(QLabel("Target Fillet r/t Ratio:"))
        self.fillet_ratio_input = _spin(0.0, 10.0, 0.25, 2, 1.0,
            "Fillet radius as a multiple of wall thickness (r = ratio × t).\n"
            "Larger values reduce the hull-to-gunwale stress concentration Kt.")
        self.fillet_ratio_input.setEnabled(False)
        _fillet_row.addWidget(self.fillet_ratio_input); _fillet_row.addStretch()
        vg.addLayout(_fillet_row)

        # gunwale rows/extras are disabled until gunwales are enabled
        for _w in (self.min_gw_width_input, self.max_gw_width_input,
                   self.min_gw_height_input, self.max_gw_height_input,
                   self.gw_steps_input):
            _w.setEnabled(False)

        def _on_gunwales_toggled(checked):
            for _w in (self.enable_mesh_checkbox, self.min_gw_width_input,
                       self.max_gw_width_input, self.min_gw_height_input,
                       self.max_gw_height_input, self.gw_steps_input,
                       self.fillet_ratio_input):
                _w.setEnabled(checked)
            if not checked:
                self.enable_mesh_checkbox.setChecked(False)
        self.enable_gunwales_checkbox.toggled.connect(_on_gunwales_toggled)

        # Stem-overhang auto/manual toggle (drives stem_rocker_input enabled)
        from config.settings import get_settings
        _cfg_rocker = get_settings().structural.bow_stern_rocker_in
        self.stem_rocker_auto_checkbox = QCheckBox(
            "Auto stem overhang (derived from hull geometry)")
        self.stem_rocker_auto_checkbox.setChecked(_cfg_rocker is None)
        self.stem_rocker_auto_checkbox.setToolTip(
            "STEM OVERHANG (tip cantilever extent), inches per end.\n\n"
            "The fore-aft run at each end where the hull is too narrow to host\n"
            "a shell section — max half-breadth below 1.5 × wall thickness.\n"
            "Structurally it is un-buoyed cantilever mass beyond the supported\n"
            "span: it raises hogging at the tips and RELIEVES midship sagging.\n\n"
            "Auto interpolates where the half-breadth crosses 1.5t, so the\n"
            "value is set by the hull and not by the offset file's row count.\n"
            "≈1.05 in/end on the current hull.  Unchecking makes it an explicit\n"
            "assumption instead of a derivation.\n\n"
            "This is a TRUTHFUL GEOMETRIC INPUT, not a design lever — tip mass\n"
            "is a very poor way to buy structural margin.")
        if _cfg_rocker is not None:
            self.stem_rocker_input.setValue(float(_cfg_rocker))
        vg.addWidget(self.stem_rocker_auto_checkbox)
        self.stem_rocker_auto_checkbox.toggled.connect(
            lambda checked: self.stem_rocker_input.setEnabled(not checked))
        self.stem_rocker_input.setEnabled(
            not self.stem_rocker_auto_checkbox.isChecked())
        vg.addStretch(1)
        self._settings_toolbox.addItem(p_geom, "📁  3.  Geometry Search Bounds")

        # ══════════════════════════════════════════════════════════════════
        #  Category 4 — Load Cases & Safety
        # ══════════════════════════════════════════════════════════════════
        p_loads = QWidget(); vl = QVBoxLayout(p_loads); vl.setSpacing(6)

        # Factor of Safety
        _fos_row = QHBoxLayout()
        _fos_lbl = QLabel("Factor of Safety:")
        _fos_lbl.setToolTip(
            "Divides the measured strength before the stress check.\n"
            "FOS = 1.5 caps design stress at 67% of measured. ASCE ≥ 1.5.")
        _fos_row.addWidget(_fos_lbl)
        self.fos_slider = QSlider(Qt.Orientation.Horizontal)
        self.fos_slider.setRange(100, 300); self.fos_slider.setValue(150)
        self.fos_slider.setTickInterval(10); self.fos_slider.setFixedWidth(140)
        _fos_row.addWidget(self.fos_slider)
        self.fos_spinbox = NoScrollDoubleSpinBox()
        self.fos_spinbox.setRange(1.00, 3.00); self.fos_spinbox.setSingleStep(0.01)
        # Seeded from canoe_suite.cfg (structural.factor_of_safety)
        self.fos_spinbox.setValue(float(getattr(_cfg_t, "FACTOR_OF_SAFETY", 1.5)))
        self.fos_spinbox.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        self.fos_spinbox.setFixedWidth(70)
        self.fos_spinbox.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        _fos_row.addWidget(self.fos_spinbox); _fos_row.addStretch()
        self.fos_slider.valueChanged.connect(self._sync_fos_from_slider)
        self.fos_spinbox.valueChanged.connect(self._sync_fos_from_spinbox)
        vl.addLayout(_fos_row)

        # Acceptance limit on the governing Kupfer D/C.  Distinct from the FoS
        # above: this is a margin against the utilisation's NUMERICAL drift with
        # station count, not against the physics.
        _acc_form = QFormLayout(); _acc_form.setSpacing(4)
        self.kupfer_accept_input = _spin(
            0.50, 1.00, 0.01, 2,
            float(getattr(_cfg_t, "KUPFER_ACCEPTANCE_MAX", 0.95)),
            "Highest Kupfer D/C the sweep will ACCEPT for a winner.\n\n"
            "Not 1.00 by default.  The gating utilisation is not converged at\n"
            "the search resolution — it drifts ~1–2% every time the station\n"
            "count doubles, with no plateau out to 641 stations — so a design\n"
            "accepted at 1.00 on the 161-station grid fails one refinement\n"
            "step (measured: 0.9957 at 161 → 1.0040 at 321).\n\n"
            "0.95 buys ~2.4 doublings at the worst observed drift, i.e. the\n"
            "verdict holds to ~830 stations.  Set 1.00 to disable and accept\n"
            "whatever passes at the search resolution.\n"
            "Starts from canoe_suite.cfg (structural.kupfer_acceptance_max).")
        _acc_form.addRow("Max accepted D/C:", self.kupfer_accept_input)
        vl.addLayout(_acc_form)

        # ── Design methodology ──────────────────────────────────────────────
        # The FAILURE CRITERION (Kupfer-Gerstle biaxial) is identical under
        # both methods; only the load combination and the capacity divisor
        # change.  Everything here is seeded from canoe_suite.cfg.
        _dm_grp = QGroupBox("Design Methodology")
        _dm_form = QFormLayout(_dm_grp); _dm_form.setSpacing(4)
        self.design_method_combo = QComboBox()
        self.design_method_combo.addItems(["LRFD", "ASD"])
        self.design_method_combo.setCurrentText(
            str(getattr(_cfg_t, "DESIGN_METHOD", "LRFD")).upper())
        self.design_method_combo.setToolTip(
            "How loads and capacities are factored.  The Kupfer-Gerstle\n"
            "biaxial failure criterion is used under BOTH — this changes\n"
            "only where the safety sits.\n\n"
            "LRFD:  factored loads (γD·D + γL·L)  vs  φ·f_nominal\n"
            "ASD:   service loads  (D + L)        vs  f_nominal / Ω\n\n"
            "This hull is live-dominated (≈111 lb hull vs 640 lb crew), so\n"
            "LRFD is the more demanding of the two.")
        _dm_form.addRow("Method:", self.design_method_combo)

        self.gamma_dead_input = _spin(0.0, 2.0, 0.05, 2,
            float(getattr(_cfg_t, "GAMMA_DEAD", 1.2)),
            "LRFD dead-load factor γD (ASCE 7-22 §2.3.1).\n"
            "Applies to hull self-weight INCLUDING the stem-rocker tip mass.\n"
            "Where dead COUNTERACTS live — which it does at this hull's\n"
            "governing station — the 0.9 alternative is enveloped as well,\n"
            "since factoring a relieving load up would understate demand.")
        _dm_form.addRow("γ dead:", self.gamma_dead_input)

        self.gamma_live_input = _spin(0.0, 3.0, 0.05, 2,
            float(getattr(_cfg_t, "GAMMA_LIVE", 1.6)),
            "LRFD live-load factor γL (ASCE 7-22 §2.3.1), applied to the\n"
            "paddler loads.  The dynamic load factor multiplies these FIRST\n"
            "(impact, not safety), so the effective factor on nominal crew\n"
            "weight is DLF × γL — 1.2 × 1.6 = 1.92 at the defaults.")
        _dm_form.addRow("γ live:", self.gamma_live_input)

        self.phi_input = _spin(0.30, 1.00, 0.05, 2,
            float(getattr(_cfg_t, "PHI_RESISTANCE", 0.60)),
            "LRFD resistance factor φ.\n\n"
            "0.60 = ACI 318-19 Table 21.2.1(g), PLAIN concrete — the\n"
            "defensible value here.  This gate checks the CONCRETE's tensile\n"
            "capacity directly, which is the plain-concrete limit state.\n\n"
            "0.90 is ACI's tension-controlled flexure factor and does NOT\n"
            "apply: it presumes a reinforced section where steel carries the\n"
            "tension and concrete tensile strength is ignored entirely.  The\n"
            "hull's mesh is not conventional reinforcement and contributes\n"
            "nothing before the first cracking this check gates on.")
        _dm_form.addRow("φ (LRFD):", self.phi_input)

        self.omega_input = _spin(1.00, 4.00, 0.05, 2,
            float(getattr(_cfg_t, "OMEGA_SAFETY", 1.5)),
            "ASD factor of safety Ω: allowable = f_nominal / Ω.\n"
            "Kept at 1.5 so the ASD path reproduces the pre-LRFD engine.")
        _dm_form.addRow("Ω (ASD):", self.omega_input)

        def _on_method(*_):
            lrfd = self.design_method_combo.currentText().upper() == "LRFD"
            for w in (self.gamma_dead_input, self.gamma_live_input, self.phi_input):
                w.setEnabled(lrfd)
            self.omega_input.setEnabled(not lrfd)
        self.design_method_combo.currentTextChanged.connect(_on_method)
        _on_method()
        vl.addWidget(_dm_grp)

        # Maneuver load (turn)
        _man_grp = QGroupBox("Maneuver Load (turn)")
        _man_form = QFormLayout(_man_grp); _man_form.setSpacing(4)
        self.maneuver_speed_input = _spin(0.5, 8.0, 0.25, 2, 2.5,
            "Design speed through a slalom turn (m/s). Hoop load ∝ speed².")
        _man_form.addRow("Turn Speed (m/s):", self.maneuver_speed_input)
        self.maneuver_radius_input = _spin(1.0, 20.0, 0.5, 1, 4.0,
            "Design minimum turn radius (m). Hoop load ∝ 1/radius; ~4.0 m is\n"
            "realistic for a 6.1 m hull.")
        _man_form.addRow("Min Turn Radius (m):", self.maneuver_radius_input)
        vl.addWidget(_man_grp)

        # Paddler load model
        self.advanced_loading_var = QCheckBox(
            "Use Advanced Paddler Loading (Knees & Toes split)")
        self.advanced_loading_var.setChecked(False)
        self.advanced_loading_var.setToolTip(
            "Unchecked (default): each paddler is a single point load.\n"
            "Checked: 80% knees (3-in patch) + 20% toes (17 in aft).")
        vl.addWidget(self.advanced_loading_var)

        # Paddler seating — per load case
        _seat_grp = QGroupBox("Paddler Seating (per load case)")
        _seat_v = QVBoxLayout(_seat_grp); _seat_v.setSpacing(4)
        _w_form = QFormLayout(); _w_form.setSpacing(3)
        # Seeded from canoe_suite.cfg (crew.race_*_person_lbs)
        def _csv(vals):
            return ", ".join(f"{float(v):g}" for v in vals)
        self.crew4_var = QLineEdit(_csv(getattr(_cfg_t, "RACE_4_PERSON",
                                                [160, 150, 170, 160])))
        self.crew3_var = QLineEdit(_csv(getattr(_cfg_t, "RACE_3_PERSON",
                                                [160, 160, 160])))
        self.crew2_var = QLineEdit(_csv(getattr(_cfg_t, "RACE_2_PERSON",
                                                [170, 160])))
        for _w, _n in ((self.crew4_var, 4), (self.crew3_var, 3), (self.crew2_var, 2)):
            _w.setToolTip(f"{_n}-person crew static weights (lb), comma-separated.")
        _w_form.addRow("4-Person Weights (lb):", self.crew4_var)
        _w_form.addRow("3-Person Weights (lb):", self.crew3_var)
        _w_form.addRow("2-Person Weights (lb):", self.crew2_var)
        _seat_v.addLayout(_w_form)

        _opt_row = QHBoxLayout()
        self.optimize_seating_checkbox = QCheckBox("Optimize Seating Placement")
        self.optimize_seating_checkbox.setChecked(True)
        self.optimize_seating_checkbox.setToolTip(
            "Checked: SLSQP finds the best seat stations.\n"
            "Unchecked: use the manual positions below.")
        _opt_row.addWidget(self.optimize_seating_checkbox)
        _opt_row.addWidget(QLabel("variance:"))
        self.pos_variance_var = QLineEdit("0.0"); self.pos_variance_var.setMaximumWidth(46)
        self.pos_variance_var.setToolTip(
            "Expected paddler drift from the optimized station (ft); the\n"
            "envelopes cover shifted seatings. Optimizer mode only.")
        _opt_row.addWidget(self.pos_variance_var)
        _opt_row.addWidget(QLabel("ft")); _opt_row.addStretch()
        _seat_v.addLayout(_opt_row)

        _p_form = QFormLayout(); _p_form.setSpacing(3)
        self.pos4_var = QLineEdit("3.0, 7.5, 12.0, 15.0")
        self.pos3_var = QLineEdit("5.0, 9.0, 13.0")
        self.pos2_var = QLineEdit("4.0, 14.0")
        for _w in (self.pos4_var, self.pos3_var, self.pos2_var):
            _w.setToolTip("Comma-separated seat positions measured from the bow (ft).")
        _p_form.addRow("4-Person Seats (ft):", self.pos4_var)
        _p_form.addRow("3-Person Seats (ft):", self.pos3_var)
        _p_form.addRow("2-Person Seats (ft):", self.pos2_var)
        _seat_v.addLayout(_p_form)

        def _on_optimize_toggle(optimize: bool):
            for _w in (self.pos4_var, self.pos3_var, self.pos2_var):
                _w.setEnabled(not optimize)
            self.pos_variance_var.setEnabled(optimize)
        self.optimize_seating_checkbox.toggled.connect(_on_optimize_toggle)
        _on_optimize_toggle(True)
        vl.addWidget(_seat_grp)

        # last-sweep seating readout
        self._seats_label = QLabel("Paddler seating: run a sweep to see "
                                   "optimized seat stations.")
        self._seats_label.setFont(QFont("Consolas", 9))
        self._seats_label.setWordWrap(True)
        self._seats_label.setStyleSheet("color: #aaaaaa;")
        vl.addWidget(self._seats_label)
        vl.addStretch(1)
        self._settings_toolbox.addItem(p_loads, "📁  4.  Load Cases & Safety")

        # ══════════════════════════════════════════════════════════════════
        #  Category 5 — Solver Precision
        # ══════════════════════════════════════════════════════════════════
        p_solver = QWidget(); vs = QVBoxLayout(p_solver); vs.setSpacing(6)

        # Sweep Precision preset (Coarse/Normal/Fine) — the primary, most
        # prominent control on this page, kept at the very top.
        _prec_row = QHBoxLayout()
        _prec_lbl = QLabel("Sweep Precision:")
        _prec_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        _prec_lbl.setToolTip(
            "Density of the design search grid.\n"
            "Coarse (1): fast preview.  Normal (2): balanced.  Fine (3): full.")
        _prec_row.addWidget(_prec_lbl)
        self.precision_slider = QSlider(Qt.Orientation.Horizontal)
        self.precision_slider.setRange(1, 3); self.precision_slider.setValue(2)
        self.precision_slider.setTickInterval(1)
        self.precision_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.precision_slider.setFixedWidth(120)
        self.precision_slider.setToolTip("1 = Coarse  |  2 = Normal  |  3 = Fine")
        self.precision_slider.valueChanged.connect(self._on_precision_change)
        _prec_row.addWidget(self.precision_slider)
        self.precision_label = QLabel("Normal")
        self.precision_label.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        _prec_row.addWidget(self.precision_label); _prec_row.addStretch()
        vs.addLayout(_prec_row)

        self._NATIVE_STATIONS = 41       # off1.mlt native station rows (real data)
        self._NOMINAL_LOA_FT = 20.0      # for the inch/station display estimate
        _long_grp = QGroupBox("Longitudinal Resolution (integration accuracy)")
        _long_form = QFormLayout(_long_grp); _long_form.setSpacing(4)
        self.stations_input = NoScrollSpinBox()
        self.stations_input.setRange(self._NATIVE_STATIONS, 321)
        self.stations_input.setSingleStep(20); self.stations_input.setValue(161)
        self.stations_input.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.stations_input.setToolTip(
            "Number of longitudinal stations the shear / moment / weight\n"
            "integrals use. This is NUMERICAL ACCURACY, not a design search\n"
            f"dimension. The offsets have {self._NATIVE_STATIONS} NATIVE stations; the\n"
            "integrals converge only well above that, so the default (161)\n"
            "oversamples. Values above native INTERPOLATE between real offsets.")
        _long_form.addRow("Stations:", self.stations_input)
        self.stations_note = QLabel()
        self.stations_note.setWordWrap(True)
        self.stations_note.setObjectName("note")
        _long_form.addRow("", self.stations_note)

        def _on_stations_change(*_):
            n = self.stations_input.value()
            dx_in = self._NOMINAL_LOA_FT * 12.0 / max(n - 1, 1)
            msg = f"≈ {dx_in:.2f} in/station."
            if n > self._NATIVE_STATIONS:
                msg += (f"  ⚠ above native ({self._NATIVE_STATIONS}): intermediate "
                        "stations are INTERPOLATED, not measured hull data.")
            elif n == self._NATIVE_STATIONS:
                msg += "  Native — real offsets only, fastest (under-reads weight ~5%)."
            self.stations_note.setText(msg)
        self.stations_input.valueChanged.connect(_on_stations_change)
        _on_stations_change()
        vs.addWidget(_long_grp)
        vs.addStretch(1)
        self._settings_toolbox.addItem(p_solver, "📁  5.  Solver Precision")

        self._settings_toolbox.setCurrentIndex(0)

        # Run / Report / Export moved into the pinned block at the top of this
        # column; only the help link and the fallback banner stay at the foot.
        struct_help_btn = QPushButton("? Help")
        struct_help_btn.setFixedHeight(26)
        struct_help_btn.setToolTip("Open the in-app help for this tab")
        struct_help_btn.clicked.connect(lambda: self._open_help())
        left_layout.addWidget(struct_help_btn)

        # ── Godzilla fallback banner (hidden until needed) ──
        self._godzilla_banner = QLabel("")
        self._godzilla_banner.setWordWrap(True)
        self._godzilla_banner.setVisible(False)
        self._godzilla_banner.setStyleSheet(
            f"QLabel {{ background-color: {C.WARN}; color: {C.BG_DEEP}; "
            f"padding: 7px 9px; font-weight: bold; border-radius: 6px; }}")
        left_layout.addWidget(self._godzilla_banner)

        # ── Right: Result Tabs ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.struct_tabs = QTabWidget()
        right_layout.addWidget(self.struct_tabs)

        from ui.hull3d_view import Hull3DView
        self._hull3d_view    = Hull3DView()          # Three.js WebGL viewport
        self._moment_tab     = QWidget(); QVBoxLayout(self._moment_tab)
        self._shear_tab      = QWidget(); QVBoxLayout(self._shear_tab)
        self._pour_tab       = QWidget(); QVBoxLayout(self._pour_tab)
        self._util_tab       = QWidget(); QVBoxLayout(self._util_tab)
        self._stress_tab     = QWidget(); QVBoxLayout(self._stress_tab)

        # Load-case selection bars — persist above the embedded charts
        self._moment_case_checks = self._make_case_bar(self._moment_tab)
        self._shear_case_checks  = self._make_case_bar(self._shear_tab)
        # Mould-orientation bar on the Pour Zones tab (female = keel down;
        # male = keel up / inverted, matching a plug the hull is laid over).
        self._make_pour_bar(self._pour_tab)

        self.struct_tabs.addTab(self._hull3d_view,    "Optimized Hull 3D")
        self.struct_tabs.addTab(self._moment_tab,     "Moment Envelope")
        self.struct_tabs.addTab(self._shear_tab,      "Shear Envelope")
        self.struct_tabs.addTab(self._pour_tab,       "Pour Zones")
        self.struct_tabs.addTab(self._util_tab,       "Utilization")
        self.struct_tabs.addTab(self._stress_tab,     "Peak Stress")
        self._moment_canvas     = None
        self._shear_canvas      = None
        self._pour_canvas       = None
        self._util_canvas       = None
        self._stress_canvas     = None

        # ── Worst-station summary card inside the stress tab ──
        self._stress_card = QLabel("Run a sweep to see peak-stress results.")
        self._stress_card.setFont(QFont("Consolas", 9))
        self._stress_card.setWordWrap(True)
        self._stress_card.setObjectName("note")
        self._stress_tab.layout().addWidget(self._stress_card)

        # ── Torsion / lateral summary card (relocated from the removed
        #    Validation tab — it is core structural output, not validation) ──
        self._torsion_card = QLabel("Run a sweep to see torsion & lateral load results.")
        self._torsion_card.setFont(QFont("Consolas", 9))
        self._torsion_card.setWordWrap(True)
        self._torsion_card.setObjectName("note")
        self._stress_tab.layout().addWidget(self._torsion_card)

        self._export_stress_btn = QPushButton("Export Stress Table (CSV)")
        self._export_stress_btn.setFixedHeight(26)
        self._export_stress_btn.setEnabled(False)
        self._export_stress_btn.setStyleSheet(
            "QPushButton { background-color: #555; font-size: 10px; }")
        self._export_stress_btn.clicked.connect(self._export_stress_csv)
        self._stress_tab.layout().addWidget(self._export_stress_btn)

        # Cache last stress arrays for CSV export
        self._last_stress_data: dict = {}
        self._last_sweep_result: dict = {}

        right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # A horizontal splitter lets the window expand gracefully on large
        # monitors — the result panel absorbs the extra width, and the user
        # can drag the divider to rebalance.
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        workspace.addWidget(splitter, stretch=1)

    # ------------------------------------------------------------------
    # Load-case selection for the moment / shear charts
    # ------------------------------------------------------------------
    def _make_case_bar(self, tab) -> dict:
        """Checkbox row selecting which load cases the chart displays."""
        from PyQt6.QtWidgets import QHBoxLayout, QCheckBox, QLabel, QWidget
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 2, 6, 0)
        lay.addWidget(QLabel("Load cases:"))
        checks = {}
        for key, label in (("envelope", "Envelope"), ("race_4", "4-Person"),
                           ("race_3", "3-Person"), ("race_2", "2-Person")):
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.toggled.connect(self._refresh_envelope_charts)
            lay.addWidget(cb)
            checks[key] = cb
        lay.addStretch(1)
        tab.layout().addWidget(bar)
        return checks

    def _make_pour_bar(self, tab):
        """Mould-orientation + outline controls above the Pour Zones chart."""
        from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QWidget, QComboBox,
                                     QCheckBox)
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 2, 6, 0)
        lay.addWidget(QLabel("Mould:"))
        self._pour_mold_combo = QComboBox()
        # userData is the value plot_pour_zones expects.
        self._pour_mold_combo.addItem("Female (keel down)", "female")
        self._pour_mold_combo.addItem("Male / plug (keel up)", "male")
        self._pour_mold_combo.setToolTip(
            "Female mould: concrete troweled into a concave cavity, keel down.\n"
            "Male mould / plug: hull laid up inverted over a convex form, so the\n"
            "chart flips vertically to match how the zones stack during the pour.")
        self._pour_mold_combo.currentIndexChanged.connect(self._refresh_pour_chart)
        lay.addWidget(self._pour_mold_combo)
        self._pour_outline_check = QCheckBox("Hull outline")
        self._pour_outline_check.setChecked(True)
        self._pour_outline_check.setToolTip(
            "Draw the canoe's side-profile silhouette (gunwale + keel).")
        self._pour_outline_check.toggled.connect(self._refresh_pour_chart)
        lay.addWidget(self._pour_outline_check)
        lay.addStretch(1)
        tab.layout().addWidget(bar)

    def _pour_mold(self) -> str:
        combo = getattr(self, "_pour_mold_combo", None)
        return combo.currentData() if combo is not None else "female"

    def _pour_show_outline(self) -> bool:
        cb = getattr(self, "_pour_outline_check", None)
        return cb.isChecked() if cb is not None else True

    def _refresh_pour_chart(self, *_):
        """Re-render the Pour Zones chart from the cached sweep result."""
        result = getattr(self, "_last_sweep_result", None)
        if not isinstance(result, dict) or "x" not in result or "layout" not in result:
            return
        from visualization import plot_pour_zones
        fig_p = plot_pour_zones(
            result["x"], result["layout"],
            na_line=result.get("na_line"),
            pour_line=result.get("pour_line_in"),
            sheer_line=result.get("sheer_line_in"),
            mixes_pcf=result.get("best_mixes"),
            zone_pour_lines=result.get("zone_pour_lines"),
            zone_densities=result.get("zone_densities"),
            mold=self._pour_mold(),
            show_outline=self._pour_show_outline())
        self._embed(fig_p, self._pour_tab, "_pour_canvas")

    @staticmethod
    def _selected_cases(checks: dict) -> set:
        return {k for k, cb in checks.items() if cb.isChecked()}

    def _refresh_envelope_charts(self, *_):
        """Re-render moment/shear charts from the cached sweep result."""
        result = getattr(self, "_last_sweep_result", None)
        if not isinstance(result, dict):
            return
        from visualization import plot_structural_envelope, plot_shear_envelope
        if "x" in result and "M_envelope" in result:
            fig_m = plot_structural_envelope(
                result["x"], result["M_envelope"],
                cases=self._selected_cases(self._moment_case_checks),
                sag_hog=(result.get("M_sag"), result.get("M_hog"))
                        if result.get("M_sag") is not None else None,
                seats=result.get("paddler_positions"))
            self._embed(fig_m, self._moment_tab, "_moment_canvas")
        if "x" in result and "V_envelope" in result:
            fig_v = plot_shear_envelope(
                result["x"], result["V_envelope"],
                cases=self._selected_cases(self._shear_case_checks))
            self._embed(fig_v, self._shear_tab, "_shear_canvas")

    def _set_running(self, running: bool):
        for btn in [self.report_btn, self.export_btn]:
            btn.setEnabled(not running)
        # Sweep button: disable while running; on completion restore the
        # file-presence gating handled by _refresh_file_status.
        if running:
            self.sweep_only_btn.setEnabled(False)
        else:
            self._refresh_file_status()

    def _open_help(self):
        from ui.help_panel import HelpPanel
        HelpPanel.show_help("Structural Sweep", parent=self)

    def _show_godzilla_banner(self, msg: str):
        """Show the orange fallback banner on the main thread."""
        QTimer.singleShot(0, lambda: (
            self._godzilla_banner.setText(f"Godzilla Fallback: {msg}"),
            self._godzilla_banner.setVisible(True),
        ))

    def _hide_godzilla_banner(self):
        QTimer.singleShot(0, lambda: self._godzilla_banner.setVisible(False))

    # ------------------------------------------------------------------
    # Embed helper — stores canvases on self (StructuralTab)
    # ------------------------------------------------------------------
    def _embed(self, fig, parent_tab, canvas_attr: str):
        lay = parent_tab.layout()
        if lay is None:
            lay = QVBoxLayout(parent_tab)
        old = getattr(self, canvas_attr, None)
        if old is not None:
            lay.removeWidget(old)
            if old in self._active_canvases:
                self._active_canvases.remove(old)
            old.deleteLater()
        canvas = FigureCanvasQTAgg(fig)
        from ui.chart_export import enable_chart_export
        enable_chart_export(canvas)
        canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        canvas.updateGeometry()
        lay.addWidget(canvas)
        canvas.show()
        canvas.draw()
        self._active_canvases.append(canvas)
        setattr(self, canvas_attr, canvas)
        plt.close(fig)

    # ------------------------------------------------------------------
    # FoS slider
    # ------------------------------------------------------------------

    def _get_fos_value(self) -> float:
        return self.fos_slider.value() / 100.0

    def _on_precision_change(self, value: int):
        self.precision_label.setText({1: "Coarse", 2: "Normal", 3: "Fine"}[value])

    # ------------------------------------------------------------------
    # Mix database UI
    # ------------------------------------------------------------------
    def _refresh_mix_display(self):
        try:
            from config import get_mix_db
            db = get_mix_db()
            mixes = db.get_all_mixes()

            self._mix_display.clear()
            if not mixes:
                self._mix_display.setText(
                    "No mixes registered. Click 'Seed Defaults' to start.")
            else:
                fc_label = "f'c"
                header = (f"{'Name':<30s}  {'pcf':>5s}  "
                          f"{fc_label:>7s}  {'ft':>7s}  {'Fiber':>6s}")
                sep = "─" * 65
                lines = [header, sep]
                for m in mixes:
                    fiber = m.get('fiber_type', 'PVA')
                    lines.append(
                        f"{m['name']:<30s}  {m['density_pcf']:5.1f}  "
                        f"{m['fc_psi']:7.0f}  {m['ft_psi']:7.0f}  {fiber:>6s}")
                self._mix_display.setText("\n".join(lines))

            try:
                self._regression_label.setText(db.get_regression_stats())
            except Exception:
                pass
        except Exception as e:
            self.mw.log(f"⚠ Mix DB refresh error: {e}")

    def _seed_default_mixes(self):
        try:
            from config import get_mix_db
            db = get_mix_db()
            db.seed_defaults()
            self._refresh_mix_display()
            self.mw.log("✅ Default mixes seeded into database.")
        except Exception as e:
            self.mw.log(f"❌ Seeding failed: {e}")

    def _open_add_mix_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Concrete Mix")
        dialog.setFixedSize(400, 360)
        dlg_layout = QGridLayout(dialog)

        fields = {}
        specs = [
            ("Name:",            "Mix X – Description"),
            ("Density (pcf):",   "70.0"),
            ("f'c (psi):",       "1724"),
            ("ft (psi):",        "345"),
            ("Fiber Type:",      "PVA"),
            ("Fiber Vol (%):",   "2.0"),
        ]
        for i, (lbl_text, default) in enumerate(specs):
            dlg_layout.addWidget(QLabel(lbl_text), i, 0)
            entry = QLineEdit(default)
            entry.setFixedWidth(200)
            dlg_layout.addWidget(entry, i, 1)
            fields[lbl_text] = entry

        def _submit():
            try:
                from config import get_mix_db
                db = get_mix_db()
                db.add_mix(
                    name=fields["Name:"].text(),
                    density_pcf=float(fields["Density (pcf):"].text()),
                    fc_psi=float(fields["f'c (psi):"].text()),
                    ft_psi=float(fields["ft (psi):"].text()),
                    fiber_type=fields["Fiber Type:"].text(),
                    fiber_vol_pct=float(fields["Fiber Vol (%):"].text()),
                )
                self.mw.log(f"✅ Mix '{fields['Name:'].text()}' added to database.")
                self._refresh_mix_display()
                dialog.accept()
            except Exception as e:
                self.mw.log(f"❌ Add mix failed: {e}")

        submit_btn = QPushButton("Add Mix")
        submit_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; font-weight: bold; font-size: 13px; }")
        submit_btn.setFixedHeight(36)
        submit_btn.clicked.connect(_submit)
        dlg_layout.addWidget(submit_btn, len(specs), 0, 1, 2)

        dialog.exec()

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_hull_file(path: str) -> str:
        """'folder/name.mlt  (modified 2026-07-11 14:03)' display string."""
        import datetime as _dt
        display = (os.path.basename(os.path.dirname(path)) + "/"
                   + os.path.basename(path))
        try:
            ts = _dt.datetime.fromtimestamp(os.path.getmtime(path))
            return f"{display}  (modified {ts:%Y-%m-%d %H:%M})"
        except OSError:
            return display

    def set_active_hull_file(self, path: str, source: str = "Hull Configurator"):
        """Point the tab at the hull file the sweep will consume.  Called by
        ManualHullTab on Godzilla completion and by the upload buttons."""
        self._active_hull_path = path
        self._active_hull_label.setText(
            f"Active Hull File: {self._fmt_hull_file(path)}")
        self._active_hull_label.setStyleSheet("color: #2ecc71;")
        self._refresh_file_status()
        self.mw.log(f"[Structural] Active hull file set ({source}): {path}")

    def _render_active_hull_3d(self):
        """
        Show the ACTIVE hull (the one the sweep will consume, parsed from
        off1.mlt) in the "Optimized Hull 3D" tab as soon as the files exist.
        Tab ② keeps the seed-authoring preview; this view shows the real
        Godzilla output — the two previews are no longer duplicates.
        A completed sweep's render owns this view and is never clobbered.
        """
        if getattr(self, "_last_sweep_result", None):
            return
        try:
            offsets, L_m, T_m, source = self.mw._resolve_underwater_offsets()
            if source != "godzilla":
                return          # seed hulls live in the Hull Configurator
            self._hull3d_view.set_hull(
                offsets, L_m, T_m,
                title="Active Hull — off1.mlt  (pre-sweep, underwater only)")
        except Exception as exc:
            print(f"[StructuralTab] active-hull preview skipped: {exc}")

    def _refresh_mix_range_label(self, *_):
        """Show the density range the sweep will really search.

        The ceiling is the Max Mix field.  The floor is whichever is HIGHER of
        the Min Mix field and the Mix Lab regression's own viability floor (the
        lightest density it still predicts any strength for) -- so the label
        must say WHICH one bound it, or a user who lowers Min Mix and sees the
        range refuse to move has no way to learn why.

        Both belong next to the input, because a range that contains no
        feasible design fails identically to bad physics.
        """
        try:
            import config as _c
            top = float(self.mix_max_var.text())
            floor = float(self.mix_min_var.text())
            step = float(self.density_step_input.value())
            only = self.existing_mixes_only_var.isChecked()
            if floor > top:
                self._mix_range_label.setText(
                    f"⚠  minimum {floor:g} pcf is above maximum {top:g} pcf — "
                    f"nothing would be searched")
                return
            _db = _c.get_mix_db()
            grid = _db.get_structural_sweep_grid(
                fos=1.5, max_density=top,
                n_mixes=int(self.n_mixes_spin.value()), step=step,
                existing_only=only, min_density=floor or None)
            dens = sorted({d for combo in grid for d in combo})
            try:
                _viable = float(_db.tensile_viability_floor()[0])
            except Exception:
                _viable = None
            if only:
                _why = "Mix Lab entries only — no interpolation"
            elif _viable is not None and floor >= _viable - 1e-9:
                _why = f"floor set by Min Mix (regression allows {_viable:.1f})"
            elif _viable is not None:
                _why = (f"floor set by the Mix Lab regression — Min Mix "
                        f"{floor:g} is below what it predicts strength for")
            else:
                _why = "floor set by the Mix Lab regression"
            self._mix_range_label.setText(
                f"searches {dens[0]:.1f} – {dens[-1]:.1f} pcf  "
                f"({len(dens)} densities, {len(grid):,} combos) · {_why}")
        except Exception:
            self._mix_range_label.setText("")

    def _refresh_file_status(self):
        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        off1_ok = os.path.exists(os.path.join(cwd, "off1.mlt"))
        out_ok  = os.path.exists(os.path.join(cwd, "out.mlt"))

        def _label(fname, found):
            if found:
                path = os.path.join(cwd, fname)
                kb = os.path.getsize(path) / 1024
                return f"✅  {fname:<12s}  {kb:.1f} kB"
            return f"❌  {fname:<12s}  not found"

        self._off1_label.setText(_label("off1.mlt", off1_ok))
        self._out_label.setText(_label("out.mlt",  out_ok))

        # "Active Hull file: none" fix — when nothing has explicitly claimed
        # the active slot but the working-directory pair exists, that IS the
        # hull the sweep will use; say so instead of "none".
        if not getattr(self, "_active_hull_path", None) and out_ok:
            self._active_hull_label.setText(
                "Active Hull File: "
                + self._fmt_hull_file(os.path.join(cwd, "out.mlt")))
            self._active_hull_label.setStyleSheet("color: #aaddaa;")

        # ── Pinned block + pipeline stepper ──────────────────────────────
        # The same two facts, restated where the Run button is: the section
        # they live in can be collapsed, and "why is Run greyed out" must be
        # answerable without opening anything.
        from ui.style import C
        for _fname, _found in (("off1.mlt", off1_ok), ("out.mlt", out_ok)):
            _dot, _state = self._pinned_status[_fname]
            _dot.setStyleSheet(
                f"color:{C.OK if _found else C.TEXT_GHOST}; font-size:11px;")
            if _found:
                _kb = os.path.getsize(os.path.join(cwd, _fname)) / 1024
                _state.setText(f"loaded · {_kb:.1f} kB")
            else:
                _state.setText("missing")
            _state.setStyleSheet(
                f"color:{C.OK if _found else C.TEXT_GHOST}; "
                f"font-family:{C.MONO}; font-size:10px;")

        both = off1_ok and out_ok
        self._pinned_hull_label.setText(
            self._fmt_hull_file(getattr(self, "_active_hull_path", None)
                                or os.path.join(cwd, "out.mlt"))
            if both else "no hull loaded — upload both files, or run the "
                         "Hull Configurator")
        self._pinned_hull_label.setStyleSheet(
            f"color:{C.TEXT_DIM if both else C.TEXT_GHOST}; "
            f"font-family:{C.MONO}; font-size:10px;")

        # The stepper reports the same readiness rather than deriving its own:
        # two sources for one fact is how a banner starts disagreeing with the
        # control it describes.
        if hasattr(self, "pipeline"):
            self.pipeline.set_active("structural" if both else "godzilla")
            self.pipeline.set_status(
                "godzilla", "off1 + out loaded" if both else "hull files missing")

        self.sweep_only_btn.setEnabled(both)
        if both:
            self._render_active_hull_3d()

    def _upload_godzilla_file(self, filename: str):
        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {filename}", "",
            "Michlet files (*.mlt *.txt *.csv);;All files (*.*)")
        if not path:
            return
        dest = os.path.join(cwd, filename)
        try:
            import shutil
            shutil.copy2(path, dest)
            self.mw.log(f"✅  Copied  {os.path.basename(path)}  →  {filename}")
        except Exception as ex:
            self.mw.log(f"❌  Could not copy file: {ex}")
            return
        # An upload claims the active-hull slot (out.mlt names the run;
        # off1.mlt uploads refresh the same display)
        self.set_active_hull_file(dest, source=f"uploaded {filename}")

    # ------------------------------------------------------------------
    # Hull preview
    # ------------------------------------------------------------------
    def _render_hull_preview(self, offsets, L, T, title="Hull Geometry"):
        """Forward to the Hull Configurator tab, which now owns the 3-D preview.

        Kept as a thin shim so existing callers (e.g. RaceTab seed application)
        need no changes after the preview migration.
        """
        hull_tab = self.mw.hull_config_tab
        QTimer.singleShot(0, lambda: hull_tab.show_hull_preview(offsets, L, T, title=title))
        QTimer.singleShot(0, lambda: self.mw.tabs.setCurrentWidget(hull_tab))

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    def _export_design_package(self):
        """Bundle session JSON, PDF, CSVs, and audit log into a zip file."""
        from PyQt6.QtWidgets import QFileDialog
        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose export folder",
            os.path.expanduser("~\\Desktop"))
        if not out_dir:
            return
        try:
            from services.export_service import export_design_package
            sr = getattr(self, "_last_sweep_result", None)
            rr = getattr(self.mw, "_race_results", None)
            # Collect existing PDFs and CSVs
            cwd = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
            pdfs = [os.path.join(cwd, f) for f in os.listdir(cwd) if f.endswith(".pdf")]
            csvs = [os.path.join(cwd, f) for f in os.listdir(cwd) if f.endswith(".csv")]
            zip_path = export_design_package(
                output_dir=out_dir,
                structural_result=sr if isinstance(sr, dict) else None,
                race_result=rr,
                pdf_path=pdfs[0] if pdfs else None,
                csv_paths=csvs,
            )
            self.mw.log(f"✅ Design package exported: {zip_path}")
        except Exception as exc:
            self.mw.log(f"❌ Export failed: {exc}")

    def _start_report(self):
        self.mw.log("Generating technical submittal report...")
        self.mw._launch_worker(self._report_work)

    def _report_work(self, worker=None):
        # Rigorous LaTeX technical report.  EVERY number comes from the live
        # sweep result object (single source of truth); no section is emitted
        # without real data.  Compiles to PDF when a LaTeX engine (tectonic /
        # pdflatex) is on PATH, otherwise writes the .tex source.
        from reporting.latex_report import generate_latex_report
        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        sr = getattr(self, "_last_sweep_result", None)
        if not sr or "weight_lbs" not in sr:
            self.mw.log("Run the structural sweep before generating the report.")
            return None
        out_dir = os.path.join(cwd, "reports")
        output_path = generate_latex_report(sr, out_dir)
        print(f"\n✅ Report saved: {os.path.basename(output_path)}")
        if worker:
            worker.progress_signal.emit(1.0, "Report Generated")
        if output_path.lower().endswith(".pdf"):
            from services.report_service import open_pdf
            open_pdf(output_path)
        else:
            self.mw.log(f"No LaTeX engine found; wrote source: {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    def _validate(self) -> bool:
        from PyQt6.QtWidgets import QMessageBox
        errors = []

        def _pos_float(widget, label, lo=0.0):
            try:
                v = float(widget.text())
                if v <= lo:
                    raise ValueError
                widget.setStyleSheet("")
                return v
            except ValueError:
                widget.setStyleSheet("border: 1px solid #e74c3c;")
                errors.append(f"{label} must be a number > {lo}")
                return None

        _pos_float(self.length_var,    "Max length (ft)")
        _pos_float(self.min_length_var,"Min length (ft)")
        _pos_float(self.draft_var,     "Seed draft (m)")
        _pos_float(self.water_rho_var, "Water density")
        _pos_float(self.water_visc_var,"Kinematic viscosity")
        _pos_float(self.gravity_var,   "Gravity")
        _pos_float(self.mix_max_var,   "Max mix density")

        # Freeboard allows 0 (technically marginal but not disqualifying at 0)
        try:
            fb = float(self.freeboard_var.text())
            if fb < 0:
                raise ValueError
            self.freeboard_var.setStyleSheet("")
        except ValueError:
            self.freeboard_var.setStyleSheet("border: 1px solid #e74c3c;")
            errors.append("Min freeboard must be >= 0")

        if self.mlf_enabled.isChecked():
            _pos_float(self.mlf_speed_var,  "Turn speed")
            _pos_float(self.mlf_radius_var, "Turn radius")

        # Sanity: min < max length
        try:
            if float(self.min_length_var.text()) >= float(self.length_var.text()):
                errors.append("Min length must be less than max length")
        except ValueError:
            pass

        # ── Search-bound pairs ───────────────────────────────────────────
        # EVERY offending pair goes into the one modal, named with both of its
        # numbers.  One dialog per bad field would make the user re-run the
        # gauntlet once per mistake, and the numbers are what identify which
        # field is wrong — "max must exceed min" does not.
        #
        # Only safe to check at all because Max now means Max: while the depth
        # field was a span above the floor, a min<max rule would have rejected
        # the valid default (floor 12.57 in, "max" 4.5 in).
        def _pair(lo_w, hi_w, label, unit="in"):
            lo, hi = lo_w.value(), hi_w.value()
            if hi < lo:
                lo_w.setStyleSheet("border: 1px solid #e74c3c;")
                hi_w.setStyleSheet("border: 1px solid #e74c3c;")
                errors.append(f"{label}: maximum {hi:g} {unit} is below "
                              f"minimum {lo:g} {unit}")
            else:
                lo_w.setStyleSheet(""); hi_w.setStyleSheet("")

        _pair(self.min_thickness_input, self.max_thickness_input,
              "Wall thickness")
        _pair(self.flare_min_input, self.flare_max_input, "Flare angle", "deg")
        if self.enable_gunwales_checkbox.isChecked():
            _pair(self.min_gw_width_input, self.max_gw_width_input,
                  "Gunwale width")
            _pair(self.min_gw_height_input, self.max_gw_height_input,
                  "Gunwale height")

        # Freeboard IS a plain pair now, which is the point of the change: both
        # columns hold the same quantity, so the rule is min < max and the
        # draft never enters it.  The 4-person crew can still raise the
        # effective minimum at run time — that is the sweep's to report, and it
        # does, because only the sweep knows the crew's displacement.
        try:
            _fb = float(self.freeboard_var.text())
            _fb_max = self.freeboard_max_input.value()
            if _fb_max <= _fb:
                self.freeboard_var.setStyleSheet("border: 1px solid #e74c3c;")
                self.freeboard_max_input.setStyleSheet(
                    "border: 1px solid #e74c3c;")
                errors.append(
                    f"Freeboard: maximum {_fb_max:g} in is at or below the "
                    f"minimum {_fb:g} in, so no freeboard satisfies both and "
                    f"nothing would be searched")
            else:
                self.freeboard_max_input.setStyleSheet("")
        except ValueError:
            pass                       # the freeboard error above already fired

        # Mix density: the same shape, and the same reason it must be caught
        # here — an inverted range yields zero configurations, and zero
        # configurations must never reach the user as "no feasible design".
        try:
            _d_lo = float(self.mix_min_var.text())
            _d_hi = float(self.mix_max_var.text())
            if _d_lo > _d_hi:
                self.mix_min_var.setStyleSheet("border: 1px solid #e74c3c;")
                self.mix_max_var.setStyleSheet("border: 1px solid #e74c3c;")
                errors.append(
                    f"Mix density: minimum {_d_lo:g} pcf is above maximum "
                    f"{_d_hi:g} pcf, so no mix satisfies both and nothing "
                    f"would be searched")
            else:
                self.mix_min_var.setStyleSheet("")
        except ValueError:
            self.mix_min_var.setStyleSheet("border: 1px solid #e74c3c;")
            errors.append("Min mix density must be a number")

        if errors:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Invalid Input",
                                "Please fix the following:\n\n• " + "\n• ".join(errors))
            return False
        return True

    # ------------------------------------------------------------------
    # Godzilla workflow
    # ------------------------------------------------------------------
    def _start_godzilla(self):
        if not self._validate():
            return
        self._check_strength_fits()
        if self.engine_var.currentText() == "Legacy Michlet (DOS visualizer)":
            self.mw._launch_worker(self._godzilla_work)
        else:
            self.mw._launch_worker(self._python_optimizer_work)

    def _check_strength_fits(self) -> None:
        """Put a blocked strength fit in front of the user before it gates a design.

        The block is already in force — this only offers the override, and only
        on the main thread, before any worker starts.  Rejecting (the default)
        lets the sweep run on the fallback tier, so this is never a dead end.
        """
        try:
            from config import get_mix_db
            from ui.fit_guard_dialog import ask_fit_override
            db = get_mix_db()
            if not db.blocking_findings():
                return
            for line in db.fit_guard_banner():
                self.mw.log("  " + line)
            if ask_fit_override(self, db, context="this structural sweep"):
                self.mw.log("⚠  STRENGTH FIT OVERRIDDEN — this sweep is gated "
                            "on a fit the sanity guard rejected. The report "
                            "and the saved design will say so.")
            else:
                self.mw.log("   Fit kept out of force; the sweep runs on the "
                            "fallback tier.")
        except Exception as exc:                   # never block a run on this
            self.mw.log(f"   [fit guard] check skipped: {exc}")

    def _start_structural_only(self):
        if not self._validate():
            return
        self._check_strength_fits()
        self.mw.log("📂  Starting Structural Sweep Only (Godzilla skipped)...")
        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        self.mw._launch_worker(self._structural_sweep_work, args=(cwd,))

    def _generate_default_in_mlt(self, cwd: str):
        from main import _generate_fallback_off1, _generate_fallback_out

        L_ft    = float(self.length_var.text())
        draft_m = float(self.draft_var.text())
        L_m     = L_ft * 0.3048

        off1    = os.path.join(cwd, "off1.mlt")
        out_mlt = os.path.join(cwd, "out.mlt")
        if not os.path.exists(off1):
            _generate_fallback_off1(off1, L_ft, draft_m * 39.3701)
        if not os.path.exists(out_mlt):
            _generate_fallback_out(out_mlt, L_ft, draft_m * 39.3701)

        tpl = os.path.join(cwd, "inTemplate.mlt")
        if os.path.exists(tpl):
            factors = [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05]
            proxy_results = {
                "best_hull": {"factors": factors, "beam": 0.61, "draft": draft_m},
                "baseline_time": 0.0,
            }
            from services.hull_service import HullService
            HullService().write_seed_hull(proxy_results, L_m, cwd, log_fn=self.mw.log)
        else:
            print("   ℹ  inTemplate.mlt not found — "
                  "structural sweep will use Python-generated offsets.")

    def _godzilla_work(self, worker=None):
        print("=" * 55)
        print("  STEP 2: GODZILLA + STRUCTURAL SWEEP")
        print("=" * 55)

        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        self._hide_godzilla_banner()

        in_mlt = os.path.join(cwd, "in.mlt")
        if not os.path.exists(in_mlt):
            print("   in.mlt not found — generating default hull seed...")
            try:
                self._generate_default_in_mlt(cwd)
                print("   Default in.mlt written.")
            except Exception as ex:
                print(f"   Could not write in.mlt: {ex}")
                print("   Skipping Godzilla; running structural sweep only.")
                self._run_structural_sweep_core(cwd, worker)
                return

        print("\n[2a] Launching Godzilla Physics Engine (mlt933w -godz)...")
        if worker:
            worker.progress_signal.emit(0.1, "Running Godzilla...")

        from services.hull_service import HullService

        def _progress(p, msg):
            if worker:
                worker.progress_signal.emit(p, msg)

        success = HullService().launch_godzilla(
            cwd=cwd,
            stop_event=self.mw._stop,
            log_fn=self.mw.log,
            progress_fn=_progress,
        )

        if self.mw._stop.is_set():
            return

        if success:
            QTimer.singleShot(0, self._refresh_file_status)
            if worker:
                worker.progress_signal.emit(0.5, "Structural sweep...")
            self._run_structural_sweep_core(cwd, worker)
        else:
            # Godzilla missing or crashed — show banner and fall back to Python optimizer
            exe = os.path.join(cwd, "mlt933w.exe")
            if not os.path.exists(exe):
                reason = "mlt933w.exe not found on this machine."
            else:
                reason = "Godzilla exited with an error (see log for details)."
            banner_msg = f"{reason} Using Python optimizer as fallback."
            print(f"\n   Godzilla fallback: {banner_msg}")
            self._show_godzilla_banner(banner_msg)
            if worker:
                worker.progress_signal.emit(0.3, "Python optimizer (fallback)...")
            self._python_optimizer_work(worker)

    def _python_optimizer_work(self, worker=None):
        from optimizer import run_optimization

        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        print("=" * 55)
        print("  STEP 2: PYTHON OPTIMIZER (CMA-ES)")
        print("=" * 55)
        if worker:
            worker.progress_signal.emit(0.1, "Running pymoo...")

        disp_target = (float(self.weight_var.text()) * 0.453592
                       / float(self.water_rho_var.text()))
        cfg = {
            "length_m":     float(self.length_var.text()) * 0.3048,
            "disp_target":  disp_target,
            "disp_lo":      disp_target * 0.95,
            "disp_hi":      disp_target * 1.05,
            "rho":          float(self.water_rho_var.text()),
            "g":            float(self.gravity_var.text()),
            "nu":           float(self.water_visc_var.text()) * 1e-6,
            "design_speed": 4.0,
            "cwd":          cwd,
            "constraints":  {
                "beam_min": 0.4, "beam_max": 2.0,
                "draft_min": 0.1, "draft_max": 1.0,
            },
        }

        def ui_cb(gen, best_Rt, hydro, offsets):
            print(f"Gen {gen}: Rt={best_Rt:.2f}N | Vol={hydro['volume']:.3f}m3")

        try:
            run_optimization(cfg, ui_callback=ui_cb, stop_event=self.mw._stop)
            print("   ✅ Python Optimization done.")
        except Exception as e:
            print(f"   ❌ {e}")
            return

        if worker:
            worker.progress_signal.emit(0.5, "Structural sweep...")
        self._run_structural_sweep_core(cwd, worker)

    def _structural_sweep_work(self, cwd, worker=None):
        self._run_structural_sweep_core(cwd, worker)

    def _run_structural_sweep_core(self, cwd, worker=None):
        print("\n[2b] 5-D Structural Sweep...")
        if worker:
            worker.progress_signal.emit(0.0, "Preparing sweep…")

        try:
            from main import _generate_fallback_off1, _generate_fallback_out
            off1_path = os.path.join(cwd, "off1.mlt")
            out_path  = os.path.join(cwd, "out.mlt")

            L_ft    = float(self.length_var.text())
            draft_m = float(self.draft_var.text())
            T_in    = draft_m * 39.3701

            if not os.path.exists(out_path):
                print("   ⚠  out.mlt missing — writing fallback report.")
                _generate_fallback_out(out_path, L_ft, T_in)

            if not os.path.exists(off1_path):
                print("   ⚠  off1.mlt missing — generating default hull offsets.")
                _generate_fallback_off1(off1_path, L_ft, T_in)

            import config as _cfg
            from domain.models import OptimizationConfig
            from services.structural_service import StructuralService

            base_dlf = _cfg.DYNAMIC_LOAD_FACTOR
            if self.mlf_enabled.isChecked():
                from structural import compute_maneuver_load_factor
                mlf = compute_maneuver_load_factor(
                    avg_speed=float(self.mlf_speed_var.text()),
                    min_turn_radius=float(self.mlf_radius_var.text()),
                    g=float(self.gravity_var.text()),
                )
                base_dlf = base_dlf * mlf
                print(f"   Maneuver Load Factor: {mlf:.3f} "
                      f"(effective DLF: {base_dlf:.3f})")

            from structural import buildable_thickness_grid
            _t_min  = self.min_thickness_input.value()
            _t_max  = self.max_thickness_input.value()
            _t_step = self.thickness_step_input.value()
            _t_override = self.thickness_override_checkbox.isChecked()
            if _t_override:
                # Custom uniform step (advanced): full manual control of the
                # thickness grid; the winner may not land on a buildable value.
                # Gated by the checkbox now, not by a positive step — the step
                # is always positive, and "which grid" is a separate question
                # from "how fine".
                _n = int(math.floor((_t_max - _t_min) / _t_step + 1e-9)) + 1
                _t_options = [round(_t_min + i * _t_step, 6) for i in range(max(_n, 1))]
                print(f"  Thickness sweep: {len(_t_options)} custom steps "
                      f"({_t_min:.4f}\"–{_t_max:.4f}\", step {_t_step:.4f}\" — "
                      f"buildable grid overridden)")
            else:
                # Default: buildable 1/32\" & 0.5 mm grid.  The divisor stays the
                # MIX count (winner bit-identical); the construction layer count
                # (n_layers_spin) is a separate derived descriptor, not this grid.
                _grid_div  = self.n_mixes_spin.value()
                _t_options = buildable_thickness_grid(_t_min, _t_max,
                                                      n_layers=_grid_div)
                print(f"  Thickness sweep: {len(_t_options)} buildable steps "
                      f"({_t_min:.3f}\"–{_t_max:.3f}\", 1/32\" & 0.5 mm grid, "
                      f"{_grid_div}-way divisible)")

            # ── Paddler seating configuration ─────────────────────────────
            def _parse_floats(widget, n, label):
                vals = [float(v) for v in widget.text().split(",") if v.strip()]
                if len(vals) != n:
                    raise ValueError(
                        f"{label} needs exactly {n} comma-separated values "
                        f"(got {len(vals)}).")
                return vals

            crew4 = _parse_floats(self.crew4_var, 4, "4-Person Crew weights")
            crew3 = _parse_floats(self.crew3_var, 3, "3-Person Crew weights")
            crew2 = _parse_floats(self.crew2_var, 2, "2-Person Crew weights")

            fixed_positions = None
            pos_variance = 0.0
            if not self.optimize_seating_checkbox.isChecked():
                fixed_positions = {
                    "4": _parse_floats(self.pos4_var, 4, "4-Person Seats"),
                    "2": _parse_floats(self.pos2_var, 2, "2-Person Seats"),
                    "3": _parse_floats(self.pos3_var, 3, "3-Person Seats"),
                }
                print("  Seating: user-specified positions (ft from bow) — "
                      f"4P={fixed_positions['4']}  3P={fixed_positions['3']}  "
                      f"2P={fixed_positions['2']}")
            else:
                pos_variance = float(self.pos_variance_var.text() or 0.0)
                if pos_variance > 0:
                    print(f"  Seating: optimizer-placed with ±{pos_variance:.2f} ft "
                          "drift envelope")

            opt_config = OptimizationConfig(
                factor_of_safety=self._get_fos_value(),
                kupfer_acceptance_max=float(self.kupfer_accept_input.value()),
                design_method=self.design_method_combo.currentText().upper(),
                gamma_dead=float(self.gamma_dead_input.value()),
                gamma_live=float(self.gamma_live_input.value()),
                phi_resistance=float(self.phi_input.value()),
                omega_safety=float(self.omega_input.value()),
                dynamic_load_factor=base_dlf,
                max_density_pcf=float(self.mix_max_var.text()),
                min_density_pcf=float(self.mix_min_var.text()),
                thickness_options_in=_t_options,
                freeboard_min_in=float(self.freeboard_var.text()),
                race_4_person_lbs=crew4,
                race_2_person_lbs=crew2,
                race_3_person_lbs=crew3,
                fixed_paddler_positions=fixed_positions,
                paddler_pos_variance_ft=pos_variance,
                n_mixes=self.n_mixes_spin.value(),
                n_construction_layers=self.n_layers_spin.value(),
                enable_gunwales=self.enable_gunwales_checkbox.isChecked(),
                enable_mesh=self.enable_mesh_checkbox.isChecked(),
                gw_min_width_in=self.min_gw_width_input.value(),
                gw_max_width_in=self.max_gw_width_input.value(),
                gw_min_height_in=self.min_gw_height_input.value(),
                gw_max_height_in=self.max_gw_height_input.value(),
                gw_steps=self.gw_steps_input.value(),
                gw_fillet_ratio=self.fillet_ratio_input.value(),
                flare_max_deg=self.flare_max_input.value(),
                # >1 mix ⇒ composite I-beam pour automatically (no separate toggle):
                # a fixed 3-zone Structural/Light/Structural stack. Single mix →
                # None (uniform section; the zone count is irrelevant there).
                multizone=(self.n_mixes_spin.value() > 1),
                num_zones=(3 if self.n_mixes_spin.value() > 1 else None),
                maneuver_speed_mps=self.maneuver_speed_input.value(),
                maneuver_turn_radius_m=self.maneuver_radius_input.value(),
                # Longitudinal integration resolution (Solver Precision page).
                # Single rocker input (None = auto); its overhang mass enters the
                # beam self-weight, so it is a real structural input now.
                bow_stern_rocker_in=self.stem_rocker_in(),
                longitudinal_stations=self.stations_input.value(),
                # Search increments come straight from the Geometry / Mix grids
                # now (the old Advanced-Precision override panel is gone).
                depth_step_in=self.depth_step_input.value(),
                # A maximum FREEBOARD -- the same quantity freeboard_min_in
                # bounds from below.  Mold depth is derived inside the sweep,
                # against the draft that run parsed, so this panel cannot
                # convert against a different one.  depth_max_in and
                # depth_span_in stay unset; main.py rejects more than one.
                freeboard_max_in=self.freeboard_max_input.value(),
                density_step_pcf=self.density_step_input.value(),
                # Only the UI knows the thickness grid was overridden — it is
                # built here, and main.py sees nothing but the finished list.
                thickness_grid_overridden=_t_override,
            )

            def progress_cb(p, m):
                if worker:
                    worker.progress_signal.emit(p, m)

            # Thread the worker's stop_event through so the Stop button
            # actually cancels the sweep mid-run.
            _stop_evt = worker.stop_event if worker else None
            result = StructuralService().run_sweep(
                opt_config, progress_cb,
                stop_event=_stop_evt,
                precision_level=self.precision_slider.value(),
                existing_mixes_only=self.existing_mixes_only_var.isChecked(),
                price_mix_development=self.price_mix_dev_var.isChecked(),
                advanced_loading=self.advanced_loading_var.isChecked())

            # Cache for report generation (5.1 / 5.5)
            if result is not None:
                self._last_sweep_result = result

            # ── Search-boundary warning ──────────────────────────────────────
            # A winner on an exposed range edge means the SEARCH SPACE, not the
            # design, may be the binding constraint.  Physical floors (min
            # trowel, freeboard depth, Max-Density cap) are called out as such so
            # they aren't mistaken for a fence that should be widened.
            if isinstance(result, dict) and result.get("boundary_flags"):
                print("\n⚠  WINNER AT SEARCH BOUNDARY — the searched range may be "
                      "the constraint, not the design:")
                for b in result["boundary_flags"]:
                    if b.get("physical"):
                        print(f"    • {b['variable']} at its {b['edge']} "
                              f"({b['value']:.3f}) — physical limit ({b['why']}); "
                              "widening the range would not help.")
                    else:
                        print(f"    • {b['variable']} at its {b['edge']} "
                              f"({b['value']:.3f}) — SEARCH-RANGE EDGE; widen the "
                              "range and re-run to confirm the true optimum is "
                              "inside the searched space.")

            if result is None:
                print("\n⚠ Structural analysis returned no results. "
                      "Check if valid mixes exist.")
                # THE BANNER MATTERS MORE ON FAILURE, not less.  This early
                # return used to skip the emit, so a failed sweep showed no
                # run-critical warnings at all -- and a two-point strength
                # fit or a defaulted config is very often WHY everything came
                # back infeasible.  Synthesise the fit provenance so the
                # caveat still reaches whoever is staring at "0 of 100".
                try:
                    import config as _c
                    _db = _c.get_mix_db()
                    self._show_run_critical_warnings({
                        "mix_regression": {
                            "n": getattr(_db, "_regression_n", None),
                            "source": getattr(_db, "_regression_source", None),
                        },
                        "gate_records": None,   # no sweep ran; not "ungated"
                    })
                except Exception as _wexc:
                    print(f"[warnings] could not render on failure: {_wexc}")
                return

            # ── Flare Angle Optimization ──────────────────────────────────────
            # Thickness is fixed (uniform wall) — only the flare angle varies.
            # Runs silently; result is tagged into the result dict for reports.
            #
            # The physics lives in structural.freeboard_flare_gate, which the
            # sweep itself used to accept this design.  This panel used to carry
            # its own copy of that maths (with its own mock lower-hull
            # constants), so the two could — and did — disagree.  Now it re-runs
            # the ONE shared function against the ONE real lower-hull section
            # the sweep exported, purely to report the angle and stress.
            if isinstance(result, dict) and "M_envelope" in result and "t_in" in result:
                import config as _cfg_fl
                from structural import freeboard_flare_gate

                flare_min_deg = self.flare_min_input.value()
                flare_max_deg = self.flare_max_input.value()
                fos_val       = self.fos_spinbox.value()

                # 2-person race moment [ft·lb], PER STATION — the gate checks
                # every station, not just the peak-moment one.
                M_race_2  = result["M_envelope"][2]

                # Freeboard-wall material = the mix physically poured into the
                # TOP zone, NOT the lightest mix in the batch.  The freeboard
                # wall is a structural cantilever built from the heavy mix;
                # using min() collapsed the allowable to the lightweight value
                # (the 0.023 MPa bug).  zone_densities is stored strictly
                # bottom→top, so [-1] is the true top zone regardless of any
                # density ranking elsewhere.  The 2-zone path pours the
                # freeboard from the strongest mix → max().
                # config.get_mix_properties already divides by FACTOR_OF_SAFETY.
                best_mixes = result.get("best_mixes", [80.0, 60.0])
                _zone_dens = result.get("zone_densities")
                fb_pcf = (float(_zone_dens[-1]) if _zone_dens
                          else float(max(best_mixes)))
                fb_props    = _cfg_fl.get_mix_properties(fb_pcf)
                allow_psi   = fb_props.get("strength_t", 50.0)
                if self.enable_mesh_checkbox.isChecked():
                    # Composite mesh raises the wall allowable — must match
                    # the sweep's flare gate or feasible results get rejected
                    allow_psi = max(allow_psi, 800.0)
                allow_MPa = allow_psi * 6894.76 / 1e6

                # Real lower-hull section + real freeboard for the winning
                # config, computed by the sweep from the Godzilla offsets.
                _lh = result.get("lower_hull")
                _fb_in = result.get("freeboard_in")
                if _lh is None or _fb_in is None:
                    self.mw.log(
                        "⚠ Flare report skipped: the sweep did not export the "
                        "lower-hull section (multi-zone path or stale result).")
                else:
                    # All-stations gate: per-station moment vs the full
                    # per-station section, worst station governs.
                    ok, best_deg, stress_psi, gov_st = freeboard_flare_gate(
                        M_race_2, result["t_in"], _fb_in, flare_max_deg,
                        allow_psi, _lh, flare_min_deg=flare_min_deg)

                    if not ok:
                        # The sweep already accepted this design through the
                        # same function, so a failure here means the UI's flare
                        # range is narrower than the sweep's — not a bad design.
                        self.mw.log(
                            f"⚠ No flare angle between {flare_min_deg:.0f}° and "
                            f"{flare_max_deg:.0f}° satisfies the gunwale stress "
                            f"limit ({allow_MPa:.3f} MPa) for the winning design. "
                            "Widen the flare range to see the angle it needs.")
                    else:
                        stress_MPa = stress_psi * 6894.76 / 1e6
                        result["flare_angle_deg"]  = best_deg
                        result["flare_stress_MPa"] = stress_MPa
                        _x_ft = result.get("x")
                        _gov_txt = (f" at x={float(_x_ft[gov_st]):.1f} ft"
                                    if _x_ft is not None
                                    and gov_st < len(_x_ft) else "")
                        self.mw.log(
                            f"✅ FLARE OPTIMIZED: {best_deg:.0f}° selected — "
                            f"worst gunwale stress = {stress_MPa:.3f} MPa"
                            f"{_gov_txt}  "
                            f"(allow = {allow_MPa:.3f} MPa, "
                            f"D/C = {stress_psi / max(allow_psi, 1e-9):.2f}, "
                            f"FoS = {fos_val:.2f})")

            if isinstance(result, dict):
                # Persist the winning configuration so the complete-canoe CAD
                # export can rebuild the final geometry in a later session.
                try:
                    from data.session import load_design_state, save_design_state
                    _ds = load_design_state()
                    _ds.mark_structural(result)
                    save_design_state(_ds)
                except Exception as _ds_exc:
                    print(f"[DesignState] save skipped: {_ds_exc}")

                # Emit to main thread via queued connection — _render_sweep_results
                # always runs in the GUI thread regardless of which thread emits.
                self._sweep_render_signal.emit(result)

            print("\n✅ COMPLETE.")

            # Audit log
            try:
                from data.session import log_run
                # The hash is taken over THIS dict, so anything missing here is
                # a dimension two runs can differ on while logging as the same
                # run.  That happened: every entry in the file carried hash
                # eda79cfa41a0 because the six original fields never varied,
                # and neither the station count nor the density grid was
                # recorded at all -- so a 41-station run and a 161-station run
                # were indistinguishable in the audit trail.  Anything the
                # search actually depends on belongs in here.
                import config as _cfg_log
                from version_stamp import code_version as _code_version
                inputs_for_log = {
                    "run_type":     "structural_sweep",
                    "length_ft":    float(self.length_var.text()),
                    "draft_m":      float(self.draft_var.text()),
                    "fos":          self._get_fos_value(),
                    "freeboard_in": float(self.freeboard_var.text()),
                    "mix_max_pcf":  float(self.mix_max_var.text()),
                    # Longitudinal resolution: NOT a design variable, but it
                    # decides both the reported weight and the governing D/C.
                    "longitudinal_stations": opt_config.longitudinal_stations,
                    # The density grid, both ends and its step.
                    "mix_min_pcf":      opt_config.min_density_pcf,
                    "density_step_pcf": opt_config.density_step_pcf,
                    "n_mixes":          opt_config.n_mixes,
                    "num_zones":        opt_config.num_zones,
                    "multizone":        bool(opt_config.multizone),
                    "existing_mixes_only":
                        bool(self.existing_mixes_only_var.isChecked()),
                    "precision_level":  self.precision_slider.value(),
                    "executor":         getattr(_cfg_log, "SWEEP_EXECUTOR",
                                                "thread"),
                    "code_version":     _code_version(),
                }
                # ONE definition, shared with main._audit_run.  This
                # block used to build its own copy, and the two drifted: the
                # numpy-truthiness crash and the permanently-None "feasible"
                # both lived here and not there.
                from main import build_audit_summary
                summary = build_audit_summary(result)
                _h = log_run("structural_sweep", inputs_for_log, summary)
                print(f"  audit: logged run {_h} "
                      f"({inputs_for_log['longitudinal_stations']} stations, "
                      f"{inputs_for_log['mix_min_pcf']:g}–"
                      f"{inputs_for_log['mix_max_pcf']:g} pcf, "
                      f"{inputs_for_log['executor']} executor)")
            except Exception as _audit_exc:
                # A silent pass here is how the audit trail goes missing
                # without anyone noticing it has.  Say so.
                print(f"  ⚠  audit log NOT written: "
                      f"{type(_audit_exc).__name__}: {_audit_exc}")

        except Exception as e:
            import traceback
            print(f"\n❌ Structural error: {e}\n{traceback.format_exc()}")

        if worker:
            worker.progress_signal.emit(1.0, "Done")

    # ------------------------------------------------------------------
    # Peak Stress tab helpers (C3 / 4.A7 + 4.A8)
    # ------------------------------------------------------------------
    def _try_render_stress(self, result: dict):
        """Render the per-station stress chart if the result has the needed data."""
        import numpy as np
        required = {"x", "M_envelope", "I_in4", "c_top", "c_bot", "best_mixes", "layout"}
        if not required.issubset(result.keys()):
            return

        try:
            from config import get_mix_db
            from visualization import plot_stress_envelope

            x        = result["x"]
            M_env    = result["M_envelope"][0]   # absolute envelope is index 0
            I_in4    = result["I_in4"]
            c_top    = result["c_top"]
            c_bot    = result["c_bot"]
            layout   = result["layout"]          # (n_stations × 2) mix-index array
            mixes    = result["best_mixes"]      # [heavy_pcf, mid_pcf, light_pcf]
            fos      = self._get_fos_value()

            db = get_mix_db()

            # Per-station allowable from zone assignment
            allow_c = np.array([
                db.get_mix_properties(mixes[int(layout[i, 1])], fos)["strength_c"]
                for i in range(len(x))
            ])
            allow_t = np.array([
                db.get_mix_properties(mixes[int(layout[i, 0])], fos)["strength_t"]
                for i in range(len(x))
            ])

            # Worst-station summary
            I_safe    = np.maximum(I_in4, 1e-6)
            M_lb_in   = M_env * 12.0
            sig_top   = M_lb_in * c_top / I_safe
            sig_bot   = M_lb_in * c_bot / I_safe
            util      = np.maximum(sig_top / np.maximum(allow_c, 1e-3),
                                   sig_bot / np.maximum(allow_t, 1e-3))
            worst     = int(np.argmax(util))
            fos_margin = 1.0 / util[worst] if util[worst] > 0 else float("inf")

            card = (
                f"WORST STATION — x = {x[worst]:.2f} ft\n"
                f"  σ_top  = {sig_top[worst]:7.1f} psi  (allow {allow_c[worst]:.1f} psi)\n"
                f"  σ_bot  = {sig_bot[worst]:7.1f} psi  (allow {allow_t[worst]:.1f} psi)\n"
                f"  Utilization = {util[worst]:.4f}   FoS margin = {fos_margin:.3f}\n"
                f"  Mix (bot/top): {mixes[int(layout[worst,0])]:.1f} / "
                f"{mixes[int(layout[worst,1])]:.1f} pcf"
            )
            QTimer.singleShot(0, lambda c=card: self._stress_card.setText(c))

            # Cache for CSV export
            self._last_stress_data = {
                "x": x, "sig_top": sig_top, "sig_bot": sig_bot,
                "allow_c": allow_c, "allow_t": allow_t,
                "util": util, "mixes": mixes, "layout": layout,
            }
            QTimer.singleShot(0, lambda: self._export_stress_btn.setEnabled(True))

            fig_s = plot_stress_envelope(
                x, M_env, I_in4, c_top, c_bot, allow_c, allow_t
            )
            QTimer.singleShot(0, lambda f=fig_s: self._embed(
                f, self._stress_tab, "_stress_canvas"))
            QTimer.singleShot(0, lambda: self.struct_tabs.setCurrentWidget(
                self._stress_tab))

        except Exception as exc:
            import traceback
            print(f"[PeakStress] {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------
    # Main-thread rendering slot (connected to _sweep_render_signal)
    # ------------------------------------------------------------------
    def _show_run_critical_warnings(self, result: dict):
        """
        Put the handful of run-critical warnings IN THE GUI.

        The frozen build is windowed (console=False), so everything the sweep
        prints goes to a log a teammate will never open.  Four warnings decide
        whether a result can be trusted at all -- a boundary-pinned winner, a
        two-point strength fit, defaulted config, an unverified gate -- so
        those come to the front. Everything else stays in the log.
        """
        try:
            from PyQt6.QtWidgets import QLabel, QMessageBox
            from PyQt6.QtCore import Qt
            import ui.warning_banner as _wb
            import run_log as _rl

            warns = _wb.collect(result, "")
            log_path = _rl.current_log_path()
            if not warns:
                if getattr(self, "_warn_banner", None) is not None:
                    self._warn_banner.setVisible(False)
                return

            html = _wb.as_html(warns, log_path)
            if getattr(self, "_warn_banner", None) is None:
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(True)
                lbl.setOpenExternalLinks(True)   # one click opens the log
                self._warn_banner = lbl
                try:
                    self.layout().insertWidget(0, lbl)
                except Exception:
                    pass
            self._warn_banner.setText(html)
            self._warn_banner.setVisible(True)
        except Exception as _e:
            # A banner failure must never cost the RESULT; but say so rather
            # than swallowing the one thing that reports problems.
            import logging
            logging.getLogger(__name__).error(
                "run-critical warning banner failed: %s", _e, exc_info=True)
            print("!! run-critical warning banner failed: %s -- check the log "
                  "for boundary/fit warnings by hand" % _e)

    def _render_sweep_results(self, result: dict):
        """
        Called in the main GUI thread via Qt's queued-connection mechanism
        whenever the background sweep worker emits _sweep_render_signal.
        Direct widget calls are safe here — no QTimer indirection needed.
        """
        self._show_run_critical_warnings(result)
        try:
            from visualization import (
                plot_structural_envelope,
                plot_shear_envelope,
                plot_pour_zones,
                plot_utilization,
            )

            # ── Tab 0: Optimised Hull 3D (Three.js WebGL) ────────────────
            _off = result.get("hull_offsets_m")
            _L   = result.get("hull_L_m")
            _T   = result.get("hull_T_m")
            if _off is not None and _L is not None and _T is not None:
                _t_lbl = result.get("t_in", "?")
                self._hull3d_view.set_hull(
                    _off, _L, _T,
                    title=f"Optimised Hull  —  t={_t_lbl}\"  "
                          f"mixes={result.get('best_mixes', [])}",
                )
                self.struct_tabs.setCurrentWidget(self._hull3d_view)

            # ── Tab 1: Moment Envelope ────────────────────────────────────
            if "x" in result and "M_envelope" in result:
                fig_m = plot_structural_envelope(
                    result["x"], result["M_envelope"],
                    cases=self._selected_cases(self._moment_case_checks),
                    sag_hog=(result.get("M_sag"), result.get("M_hog"))
                            if result.get("M_sag") is not None else None,
                    seats=result.get("paddler_positions"))
                self._embed(fig_m, self._moment_tab, "_moment_canvas")

            # ── Tab 2: Shear Envelope ─────────────────────────────────────
            if "x" in result and "V_envelope" in result:
                fig_v = plot_shear_envelope(
                    result["x"], result["V_envelope"],
                    cases=self._selected_cases(self._shear_case_checks))
                self._embed(fig_v, self._shear_tab, "_shear_canvas")

            # ── Tab 3: Pour Zones ─────────────────────────────────────────
            # Pass the multi-zone (I-beam) bands so the tab renders N zones /
            # N-1 hand-off lines — same result object and same plot_pour_zones
            # the PDF export uses.  Without these the tab silently fell back to
            # the legacy 2-mix hand-off rendering even for a 3-zone winner.
            if "x" in result and "layout" in result:
                fig_p = plot_pour_zones(
                    result["x"], result["layout"],
                    na_line=result.get("na_line"),
                    pour_line=result.get("pour_line_in"),
                    sheer_line=result.get("sheer_line_in"),
                    mixes_pcf=result.get("best_mixes"),
                    zone_pour_lines=result.get("zone_pour_lines"),
                    zone_densities=result.get("zone_densities"),
                    mold=self._pour_mold(),
                    show_outline=self._pour_show_outline())
                self._embed(fig_p, self._pour_tab, "_pour_canvas")

            # ── Tab 4: Utilization (D/C) ──────────────────────────────────
            _util = result.get("utilization")
            if "x" in result and _util and _util.get("bot_t") is not None:
                fig_u = plot_utilization(result["x"], _util)
                self._embed(fig_u, self._util_tab, "_util_canvas")

            # ── Peak Stress tab (uses QTimer internally — fine from main thread)
            self._try_render_stress(result)

            # ── Post-render bookkeeping ───────────────────────────────────
            self._on_sweep_loaded(result)
            self._update_torsion_card(result)

        except Exception as viz_err:
            import traceback
            print(f"❌ Visualization error: {viz_err}\n{traceback.format_exc()}")

    def _export_stress_csv(self):
        import csv
        from PyQt6.QtWidgets import QFileDialog

        d = self._last_stress_data
        if not d:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Stress Table", "stress_table.csv",
            "CSV files (*.csv);;All files (*.*)")
        if not path:
            return

        try:
            import numpy as np
            with open(path, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow([
                    "x_ft", "sigma_top_psi", "sigma_bot_psi",
                    "allow_c_psi", "allow_t_psi",
                    "util_c", "util_t", "util_max",
                    "mix_bot_pcf", "mix_top_pcf",
                ])
                x = d["x"]
                for i in range(len(x)):
                    util_c_i = d["sig_top"][i] / max(d["allow_c"][i], 1e-3)
                    util_t_i = d["sig_bot"][i] / max(d["allow_t"][i], 1e-3)
                    writer.writerow([
                        f"{x[i]:.3f}",
                        f"{d['sig_top'][i]:.2f}", f"{d['sig_bot'][i]:.2f}",
                        f"{d['allow_c'][i]:.2f}", f"{d['allow_t'][i]:.2f}",
                        f"{util_c_i:.4f}", f"{util_t_i:.4f}",
                        f"{d['util'][i]:.4f}",
                        f"{d['mixes'][int(d['layout'][i,0])]:.1f}",
                        f"{d['mixes'][int(d['layout'][i,1])]:.1f}",
                    ])
            self.mw.log(f"Stress table saved: {path}")
        except Exception as exc:
            self.mw.log(f"Export failed: {exc}")

    # ------------------------------------------------------------------
    # Torsion + lateral check display
    # ------------------------------------------------------------------
    def _update_torsion_card(self, result: dict):
        tl = result.get("torsion_lateral")
        if tl is None:
            self._torsion_card.setText("Torsion/lateral check not available for this result.")
            return

        lines = [
            "TORSION + LATERAL LOAD CHECKS",
            "─" * 52,
        ]
        for line in tl.summary_lines():
            lines.append(line)
        lines += [
            "─" * 52,
            f"Design torque     T = {tl.T_design_lb_in:8.1f} lb·in",
            f"Lateral moment    M = {tl.M_lat_lb_in:8.1f} lb·in  "
            f"(F_lat = {tl.F_lat_lb:.1f} lb)",
            f"Critical torsion at x = {tl.critical_torsion_x_ft:.2f} ft  "
            f"(J_min = {tl.J_min_in4:.4f} in⁴)",
            f"Critical lateral at x = {tl.critical_lateral_x_ft:.2f} ft  "
            f"(I_lat_min = {tl.I_lat_min_in4:.4f} in⁴)",
            "─" * 52,
            f"Overall: {'✅ Within limits' if tl.is_feasible else '⚠ Secondary warnings — non-blocking, review for report'}",
        ]
        self._torsion_card.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # Post-sweep hook
    # ------------------------------------------------------------------
    def _on_sweep_loaded(self, result: dict):
        """Called (on main thread via QTimer) after a successful sweep.

        Caches the result and surfaces the paddler seating the winning
        evaluation actually used.
        """
        self._last_sweep_result = result

        # Notify listeners (Mix Lab recommendations, etc.) on the main thread
        # that a new winning design is available.  Guarded so a downstream slot
        # error never breaks the sweep-results rendering.
        try:
            self.sweep_completed.emit(result)
        except Exception as exc:
            self.mw.log(f"[Structural] sweep_completed notify failed: {exc}")

        # ── Paddler seating display (fix plan Phase 2) ────────────────────
        seats = result.get("paddler_positions")
        if isinstance(seats, dict) and seats.get("race_4"):
            mode = result.get("seating_mode", "optimized")
            var = float(result.get("position_variance_ft", 0.0) or 0.0)
            lines = [f"Paddler seating ({mode}), ft from bow"
                     + (f", ±{var:g} ft drift covered:" if var > 0 else ":")]
            names = {"race_4": "4-person", "race_2": "2-person",
                     "race_3": "3-person"}
            for key in ("race_4", "race_2", "race_3"):
                pos = seats.get(key) or []
                if pos:
                    lines.append(f"  {names[key]}:  "
                                 + ",  ".join(f"{p:.1f}" for p in pos))
            self._seats_label.setText("\n".join(lines))
            self._seats_label.setStyleSheet("color: #2ecc71;")
            self.mw.log("[Structural] " + "  |  ".join(lines))
