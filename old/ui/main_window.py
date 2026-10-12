# ui/main_window.py
"""
CanoeOptimizationSuite — top-level QMainWindow shell.

Owns:
- The two main tab widgets (RaceTab, StructuralTab)
- The console output pane
- The progress bar + stop button
- Shared grid helpers used by both tab widgets
- The background worker lifecycle
"""
from __future__ import annotations

import sys
import threading

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QPushButton,
    QTextEdit, QProgressBar, QSplitter, QMessageBox, QFileDialog,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QAction

from services.worker import WorkerThread


class CanoeOptimizationSuite(QMainWindow):
    """Top-level application window for the ASCE Concrete Canoe Engineering Suite."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("JAGUAR")
        self.resize(1200, 920)
        self.setMinimumSize(1050, 820)

        self._stop         = threading.Event()
        self._race_results = None
        self._worker       = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 8, 12, 6)
        root.setSpacing(6)

        # ── Splitter: Tabs above, Console below ──
        splitter = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(splitter, stretch=1)

        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)

        # Tab widgets import here so self is fully constructed before mw=self is passed
        from ui.race_tab import RaceTab
        from ui.structural_tab import StructuralTab
        from ui.compliance_panel import CompliancePanel
        from ui.mix_tab import MixTab
        from ui.optimizer_tab import OptimizerTab
        from ui.manual_hull_tab import ManualHullTab

        self.race_tab        = RaceTab(self)
        self.struct_tab      = StructuralTab(self)
        self.compliance_tab  = CompliancePanel(self)
        self.mix_tab         = MixTab(self)
        self.optimizer_tab   = OptimizerTab(self)
        self.hull_config_tab = ManualHullTab(self)

        # Route the structural-sweep completion to the Mix Lab so its
        # Recommendations tab refreshes with the new winner's design-driven
        # mix targets (the sweep runs in a worker thread; the tab re-emits on
        # the main thread, so this queued connection is main-thread safe).
        self.struct_tab.sweep_completed.connect(self.mix_tab.on_sweep_completed)
        self.tabs.addTab(self.race_tab,        "Race Simulator")
        self.tabs.addTab(self.hull_config_tab, "Hull Configurator")
        self.tabs.addTab(self.struct_tab,      "Structural Sweep")
        self.tabs.addTab(self.mix_tab,         "Mix Lab")
        # self.tabs.addTab(self.compliance_tab,  "⑤ ASCE Compliance")   # hidden — preserved for future use
        # self.tabs.addTab(self.optimizer_tab,   "⑥ NSGA-II Optimizer") # hidden — preserved for future use

        # ── Console ──
        console_container = QWidget()
        cc_layout = QVBoxLayout(console_container)
        cc_layout.setContentsMargins(0, 0, 0, 0)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(80)
        self.console.setMaximumHeight(2000)
        self.console.append("Ready. Start with Step ① Race Simulator.")
        cc_layout.addWidget(self.console)
        splitter.addWidget(console_container)

        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 150])

        # ── Bottom bar ──
        bottom = QHBoxLayout()
        root.addLayout(bottom)

        self.progress_label = QLabel("Idle")
        self.progress_label.setMinimumWidth(180)
        self.progress_label.setVisible(False)
        bottom.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        bottom.addWidget(self.progress_bar, stretch=1)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setFixedWidth(80)
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #e74c3c; }"
            "QPushButton:hover { background-color: #c0392b; }")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._request_stop)
        bottom.addWidget(self.stop_btn)

        # ── File menu ──
        menubar = self.menuBar()

        file_menu   = menubar.addMenu("File")
        export_menu = file_menu.addMenu("Export Geometry...")

        action_underwater = QAction("Export Underwater Hull (Godzilla Base)", self)
        action_underwater.triggered.connect(self._export_underwater)
        export_menu.addAction(action_underwater)

        action_extruded = QAction("Export Extruded Freeboard & Flare", self)
        action_extruded.triggered.connect(self._export_extruded)
        export_menu.addAction(action_extruded)

        action_full = QAction("Export Complete Canoe", self)
        action_full.triggered.connect(self._export_complete)
        export_menu.addAction(action_full)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # ── Settings menu ──
        # Theme is locked to dark (no light mode) to avoid white flashes and
        # unreadable inputs, so there is no theme toggle here.
        settings_menu = menubar.addMenu("Settings")

        units_action = QAction("Toggle Units (Metric / US Customary)", self)
        units_action.setShortcut(QKeySequence("Ctrl+U"))
        units_action.triggered.connect(self._toggle_units)
        settings_menu.addAction(units_action)

        QTimer.singleShot(300, self.struct_tab._refresh_file_status)
        QTimer.singleShot(300, self.struct_tab._refresh_mix_display)

        self._setup_shortcuts()

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------
    def _setup_shortcuts(self):
        def _sc(key, slot):
            s = QShortcut(QKeySequence(key), self)
            s.activated.connect(slot)

        # Tab switching
        _sc("Ctrl+1", lambda: self.tabs.setCurrentIndex(0))
        _sc("Ctrl+2", lambda: self.tabs.setCurrentIndex(1))
        _sc("Ctrl+3", lambda: self.tabs.setCurrentIndex(2))
        _sc("Ctrl+4", lambda: self.tabs.setCurrentIndex(3))
        _sc("Ctrl+5", lambda: self.tabs.setCurrentIndex(4))
        _sc("Ctrl+6", lambda: self.tabs.setCurrentIndex(5))

        # Run actions
        _sc("Ctrl+R", self.race_tab._start_race_sim)
        # Godzilla now launches from the Hull Configurator (decoupled workflow)
        _sc("Ctrl+G", self.hull_config_tab._start_godzilla)

        # Stop
        _sc("Ctrl+.", self._request_stop)
        _sc("Escape",  self._request_stop)

        # Export (structural tab)
        _sc("Ctrl+E", self.struct_tab._export_design_package)

        # Help for current tab
        _sc("F1", self._open_help_for_current_tab)

    def _open_help_for_current_tab(self):
        idx = self.tabs.currentIndex()
        topics = ["Race Simulator", "Hull Configurator", "Structural Sweep", "Mix Lab"]
        if 0 <= idx < len(topics):
            from ui.help_panel import HelpPanel
            HelpPanel.show_help(topics[idx], self)

    # ------------------------------------------------------------------
    # Grid helpers shared by RaceTab and StructuralTab
    # ------------------------------------------------------------------
    def _add_entry(self, grid, label: str, default: str, row: int, col: int,
                   width: int = 100) -> QLineEdit:
        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(lbl, row, col * 2)
        entry = QLineEdit(default)
        entry.setFixedWidth(width)
        grid.addWidget(entry, row, col * 2 + 1)
        return entry

    def _add_heading(self, grid, text: str, row: int, span: int = 4):
        lbl = QLabel(text)
        lbl.setObjectName("heading")
        grid.addWidget(lbl, row, 0, 1, span)

    def _add_note(self, grid, text: str, row: int, span: int = 4):
        lbl = QLabel(text)
        lbl.setObjectName("note")
        grid.addWidget(lbl, row, 0, 1, span)

    # ------------------------------------------------------------------
    # Console + progress
    # ------------------------------------------------------------------
    def log(self, msg: str):
        self.console.append(msg.rstrip("\n"))

    def _set_progress(self, fraction: float, label: str = ""):
        self.progress_bar.setValue(int(fraction * 1000))
        if label:
            self.progress_label.setText(label)

    # ------------------------------------------------------------------
    # Figure embedding — canvas attrs stored on self (mw)
    # ------------------------------------------------------------------
    def _embed_figure(self, fig, parent, canvas_attr: str):
        """Embed a matplotlib figure into a QWidget tab, replacing any old canvas."""
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

        old = getattr(self, canvas_attr, None)
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        canvas = FigureCanvasQTAgg(fig)
        from ui.chart_export import enable_chart_export
        enable_chart_export(canvas)
        canvas.draw()
        lay = parent.layout()
        if lay is None:
            lay = QVBoxLayout(parent)
        lay.addWidget(canvas)
        setattr(self, canvas_attr, canvas)

    # ------------------------------------------------------------------
    # Unit system toggle (Metric ↔ US Customary)
    # ------------------------------------------------------------------
    def _toggle_units(self):
        combo = self.hull_config_tab.unit_combo
        combo.setCurrentIndex(1 - combo.currentIndex())

    # ------------------------------------------------------------------
    # Stop button
    # ------------------------------------------------------------------
    def _request_stop(self):
        self._stop.set()
        self.log("  ■ Stopping...")

    # ------------------------------------------------------------------
    # Running state — disables action buttons across both tabs
    # ------------------------------------------------------------------
    def _set_running(self, running: bool):
        self.race_tab._set_running(running)
        self.struct_tab._set_running(running)
        self.hull_config_tab._set_running(running)
        self.progress_bar.setVisible(running)
        self.progress_label.setVisible(running)
        self.stop_btn.setVisible(running)
        if running:
            self.progress_bar.setValue(0)

    # ------------------------------------------------------------------
    # Worker thread lifecycle
    # ------------------------------------------------------------------
    def _launch_worker(self, target, args=()):
        """Start a background WorkerThread with proper signal wiring."""
        self._stop.clear()
        self._set_running(True)

        worker = WorkerThread(target, args, parent=self)
        # Share the single app-wide stop event so the Stop button (which sets
        # self._stop) reaches code that checks worker.stop_event.  Without
        # this the worker had its own disconnected event and Stop did nothing.
        worker.stop_event = self._stop
        worker.log_signal.connect(self._on_worker_log)
        worker.finished_signal.connect(self._on_worker_finished)
        worker.error_signal.connect(self._on_worker_error)
        worker.progress_signal.connect(self._set_progress)
        worker.finished.connect(lambda: self._set_running(False))
        self._worker = worker
        worker.start()

    # ------------------------------------------------------------------
    # Geometry export
    # ------------------------------------------------------------------
    def _export_underwater(self):
        """Ask the user which export format they want, then dispatch."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPushButton, QLabel

        dlg = QDialog(self)
        dlg.setWindowTitle("Export Underwater Hull")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "<b>Choose export format:</b><br><br>"
            "<b>Wireframe STEP</b> — cross-section curves for SolidWorks lofting<br>"
            "<b>Direct STL</b> — triangulated mesh for Bambu Studio / slicer"
        ))
        btn_step = QPushButton("Export Wireframe STEP  (SolidWorks)")
        btn_stl  = QPushButton("Export Direct STL  (Bambu Studio / 3-D print)")
        btn_cancel = QPushButton("Cancel")
        lay.addWidget(btn_step)
        lay.addWidget(btn_stl)
        lay.addWidget(btn_cancel)

        btn_step.clicked.connect(lambda: (dlg.done(1)))
        btn_stl.clicked.connect(lambda: (dlg.done(2)))
        btn_cancel.clicked.connect(dlg.reject)

        choice = dlg.exec()
        if choice == 1:
            self._export_underwater_step()
        elif choice == 2:
            self._export_underwater_stl()

    # ------------------------------------------------------------------
    # Geometry sources — prefer the real Godzilla output over the seed
    # ------------------------------------------------------------------
    def _resolve_underwater_offsets(self):
        """
        Return (offsets_m, L_m, T_m, source) for the best available
        underwater hull.

        Prefers the actual Godzilla-optimized geometry (off1.mlt + out.mlt)
        so exports reflect the optimized hull, not the seed.  Falls back to
        the Hull Configurator's seed factors when Godzilla has not run.
        """
        import os
        from geometry import parse_godzilla_dimensions, parse_godzilla_offsets

        # Shared resolver: a user's own hull beside the executable must beat
        # the bundled copy.  This used to be the module's parent folder, which
        # in a frozen build is `_internal`.
        from asset_paths import resolve_asset
        off1 = resolve_asset("off1.mlt")
        outp = resolve_asset("out.mlt")
        if off1 and outp:
            L_ft, draft_in = parse_godzilla_dimensions(outp)
            # mold_depth == draft → parser returns the underwater grid only
            _x, _z, y_in = parse_godzilla_offsets(
                off1, L_ft=L_ft, draft_in=draft_in, mold_depth_in=draft_in)
            if y_in is not None:
                return (y_in * 0.0254, L_ft * 0.3048, draft_in * 0.0254,
                        "godzilla")

        si      = self.hull_config_tab._read_si()
        factors = [sb.value() for sb in self.hull_config_tab.sb_factors]
        from hull_generator import generate_hull
        offsets = generate_hull(factors, si["L"], si["beam"], si["T"])
        return offsets, si["L"], si["T"], "seed"

    def _resolve_structural_params(self):
        """
        Winning upper-section parameters as a dict, or None if the structural
        sweep has not produced one (neither in this session nor on disk).
        """
        sr = getattr(self.struct_tab, "_last_sweep_result", None)
        if isinstance(sr, dict) and sr.get("depth_in"):
            return {
                "t_in":         float(sr.get("t_in", 0.5)),
                "depth_in":     float(sr["depth_in"]),
                "flare_deg":    float(sr.get("flare_angle_deg",
                                     sr.get("flare_req_deg", 0.0))),
                "gw_width_in":  float(sr.get("gw_width_in", 0.0)),
                "gw_height_in": float(sr.get("gw_height_in", 0.0)),
            }
        from data.session import load_design_state
        st = load_design_state().structural
        if isinstance(st, dict) and st.get("depth_in"):
            return {
                "t_in":         float(st.get("t_in", 0.5)),
                "depth_in":     float(st["depth_in"]),
                "flare_deg":    float(st.get("flare_deg", 0.0)),
                "gw_width_in":  float(st.get("gw_width_in", 0.0)),
                "gw_height_in": float(st.get("gw_height_in", 0.0)),
            }
        return None

    def _export_underwater_step(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Underwater Hull — Wireframe STEP",
            "canoe_wireframe.step",
            "STEP Files (*.step *.stp);;All Files (*.*)")
        if not path:
            return

        from services.step_export_service import export_wireframe_step

        try:
            offsets, L, T, source = self._resolve_underwater_offsets()
            self.log(f"Exporting underwater hull ({source} geometry)...")
            export_wireframe_step(offsets, L, T, path)
            QMessageBox.information(
                self, "Export Complete",
                f"Wireframe STEP exported:\n{path}\n\n"
                "Open in SolidWorks → Insert → Surface → Loft to skin the hull.")
        except ImportError as exc:
            QMessageBox.critical(
                self, "Missing Dependency",
                f"CadQuery is not installed.\n\nRun:  pip install cadquery\n\nDetails: {exc}")
        except RuntimeError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
        except Exception:
            import traceback
            QMessageBox.critical(self, "Export Failed", traceback.format_exc())

    def _export_underwater_stl(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Underwater Hull — Direct STL",
            "canoe_mesh.stl",
            "STL Files (*.stl);;All Files (*.*)")
        if not path:
            return

        from services.step_export_service import export_direct_stl

        try:
            offsets, L, T, source = self._resolve_underwater_offsets()
            self.log(f"Exporting underwater hull ({source} geometry)...")
            export_direct_stl(offsets, L, T, path)
            QMessageBox.information(
                self, "Export Complete",
                f"Direct STL exported:\n{path}\n\n"
                "Drop this file directly into Bambu Studio.")
        except RuntimeError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
        except Exception:
            import traceback
            QMessageBox.critical(self, "Export Failed", traceback.format_exc())

    def _export_extruded(self):
        """Export the hull + flared freeboard shell, without gunwale rails."""
        self._export_built_canoe(include_gunwales=False,
                                 title="Export Extruded Freeboard & Flare",
                                 default_stem="canoe_freeboard")

    def _export_complete(self):
        """Export the complete as-built canoe (freeboard + gunwale rails)."""
        self._export_built_canoe(include_gunwales=True,
                                 title="Export Complete Canoe",
                                 default_stem="canoe_complete")

    def _export_built_canoe(self, include_gunwales: bool,
                            title: str, default_stem: str):
        """
        Shared driver for the two upper-section exports: resolve geometry
        and winning sweep parameters, build the complete canoe, then write
        the format the user picks (watertight STL or wireframe STEP).
        """
        params = self._resolve_structural_params()
        if params is None:
            QMessageBox.warning(
                self, title,
                "No structural sweep result found.\n\n"
                "Run the Structural Sweep (tab ③) first — the export needs "
                "the winning wall thickness, mold depth, flare angle and "
                "gunwale dimensions.")
            return

        # Format choice (mirrors the underwater export dialog)
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPushButton, QLabel
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        lay = QVBoxLayout(dlg)
        gw_note = ("with gunwale rails" if include_gunwales
                   else "without gunwale rails")
        lay.addWidget(QLabel(
            f"<b>{title}</b><br>"
            f"t = {params['t_in']:.3f}\"  depth = {params['depth_in']:.2f}\"  "
            f"flare = {params['flare_deg']:.0f}°  ({gw_note})<br><br>"
            "<b>Watertight STL</b> — solid shell for OnShape viewing / slicing<br>"
            "<b>Wireframe STEP</b> — closed section curves for CAD lofting"))
        btn_stl  = QPushButton("Export Watertight STL  (OnShape / slicer)")
        btn_step = QPushButton("Export Wireframe STEP  (OnShape / SolidWorks loft)")
        btn_cancel = QPushButton("Cancel")
        lay.addWidget(btn_stl); lay.addWidget(btn_step); lay.addWidget(btn_cancel)
        btn_stl.clicked.connect(lambda: dlg.done(1))
        btn_step.clicked.connect(lambda: dlg.done(2))
        btn_cancel.clicked.connect(dlg.reject)
        choice = dlg.exec()
        if choice not in (1, 2):
            return

        ext, filt = ((".stl", "STL Files (*.stl);;All Files (*.*)")
                     if choice == 1 else
                     (".step", "STEP Files (*.step *.stp);;All Files (*.*)"))
        path, _ = QFileDialog.getSaveFileName(
            self, title, default_stem + ext, filt)
        if not path:
            return

        try:
            offsets, L_m, T_m, source = self._resolve_underwater_offsets()
            self.log(f"Building complete canoe from {source} geometry...")

            from geometry import build_complete_canoe
            IN = 0.0254
            rocker_in = self.struct_tab.stem_rocker_in()   # None = auto
            canoe = build_complete_canoe(
                offsets, L_m, T_m,
                depth_m=params["depth_in"] * IN,
                flare_deg=params["flare_deg"],
                gw_width_m=(params["gw_width_in"] * IN if include_gunwales else 0.0),
                gw_height_m=(params["gw_height_in"] * IN if include_gunwales else 0.0),
                t_m=params["t_in"] * IN,
                stem_rocker_m=(rocker_in * IN if rocker_in is not None else None),
            )
            r_bow = canoe.get("stem_rocker_bow_m", 0.0) / IN
            r_stern = canoe.get("stem_rocker_stern_m", 0.0) / IN
            if r_bow > 0 or r_stern > 0:
                self.log(f"  Stem rocker sweep: {r_bow:.1f}\" bow / "
                         f"{r_stern:.1f}\" stern "
                         + ("(auto)" if rocker_in is None else "(manual)"))
            else:
                self.log("  Bow/stern ends: vertical stems (rocker disabled)")

            from services.step_export_service import (
                export_complete_stl, export_complete_wireframe_step)
            if choice == 1:
                export_complete_stl(canoe, path)
            else:
                export_complete_wireframe_step(canoe, path)

            # Record the export in the persistent design state
            try:
                from data.session import load_design_state, save_design_state
                ds = load_design_state()
                label = ("complete_canoe" if include_gunwales else "freeboard_shell")
                ds.mark_export(f"{label}_{'stl' if choice == 1 else 'step'}", path)
                save_design_state(ds)
            except Exception:
                pass

            QMessageBox.information(
                self, "Export Complete",
                f"Exported ({source} underwater geometry):\n{path}\n\n"
                + ("Import directly into OnShape or a slicer."
                   if choice == 1 else
                   "Import into OnShape/SolidWorks and loft the closed "
                   "section curves to obtain the solid shell."))
        except ImportError as exc:
            QMessageBox.critical(
                self, "Missing Dependency",
                f"CadQuery is not installed.\n\nRun:  pip install cadquery\n\nDetails: {exc}")
        except (RuntimeError, ValueError) as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
        except Exception:
            import traceback
            QMessageBox.critical(self, "Export Failed", traceback.format_exc())

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop.set()
            self._worker.wait(3000)
        event.accept()

    def _on_worker_log(self, text: str):
        self.console.moveCursor(self.console.textCursor().MoveOperation.End)
        self.console.insertPlainText(text)
        self.console.moveCursor(self.console.textCursor().MoveOperation.End)

    def _on_worker_finished(self, result):
        pass  # per-tab callbacks handle their own results

    def _on_worker_error(self, tb: str):
        self.log(f"\n❌ Error:\n{tb}")
