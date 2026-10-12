# ui/race_tab.py
"""
Tab 1 — Race Simulator.

Hosts the Tier 1 GA strategy evolution + Tier 2 hull matching controls
and all associated result charts and telemetry replay.
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget,
    QLabel, QLineEdit, QCheckBox, QPushButton,
    QScrollArea, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

class RaceTab(QWidget):
    """
    Self-contained widget for Tab 1 (Race Simulator).

    All Qt signals and heavy computations are bridged through ``self.mw``
    (the parent MainWindow), which owns the progress bar, console, worker
    thread, and stop event.
    """

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw    # reference to CanoeOptimizationSuite
        self._build_ui()
        self._apply_preset("combined")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # ── Left: Settings ──
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        settings = QWidget()
        sg = QGridLayout(settings)
        sg.setSpacing(4)
        scroll.setWidget(settings)
        left_layout.addWidget(scroll, stretch=1)

        r = 0
        self.mw._add_heading(sg, "Tournament Preset", r); r += 1
        _preset_row = QHBoxLayout()
        _preset_row.setContentsMargins(0, 2, 0, 4)
        self._btn_regionals = QPushButton("Regionals")
        self._btn_nationals = QPushButton("Nationals")
        self._btn_combined  = QPushButton("Combined")
        for _btn in (self._btn_regionals, self._btn_nationals, self._btn_combined):
            # Minimum (not fixed) height: a fixed 26 px clipped the button
            # text at the bottom on high-DPI / large-font systems.
            _btn.setMinimumHeight(30)
            _preset_row.addWidget(_btn)
        self._btn_regionals.clicked.connect(lambda: self._apply_preset("regionals"))
        self._btn_nationals.clicked.connect(lambda: self._apply_preset("nationals"))
        self._btn_combined.clicked.connect(lambda: self._apply_preset("combined"))
        sg.addLayout(_preset_row, r, 0, 1, 4)
        sg.setRowMinimumHeight(r, 38); r += 1

        self.mw._add_heading(sg, "Courses to Race", r); r += 1
        self.race_sprint_var = QCheckBox("Sprint (200 m)")
        self.race_sprint_var.setChecked(True)
        self.race_sprint_var.setToolTip("Include the 200 m straight sprint event in the optimisation fitness.")
        sg.addWidget(self.race_sprint_var, r, 0, 1, 4); r += 1
        self.race_slalom_var = QCheckBox("Slalom (buoy gates)")
        self.race_slalom_var.setChecked(True)
        self.race_slalom_var.setToolTip("Include the slalom buoy-gate event in the optimisation fitness.")
        sg.addWidget(self.race_slalom_var, r, 0, 1, 4); r += 1
        self.race_endure_var = QCheckBox("Endurance Slalom (500 m loop + slalom)")
        self.race_endure_var.setToolTip("Include the 500 m loop + slalom endurance event in the optimisation fitness.")
        sg.addWidget(self.race_endure_var, r, 0, 1, 4); r += 1
        self.race_endurance_sprint_var = QCheckBox("Endurance Sprint (400m double)")
        self.race_endurance_sprint_var.setToolTip("Include the 400 m double-sprint endurance event in the optimisation fitness.")
        sg.addWidget(self.race_endurance_sprint_var, r, 0, 1, 4); r += 1

        self.mw._add_heading(sg, "Course Weights", r); r += 1
        self.wt_sprint_var = self.mw._add_entry(sg, "Sprint:", "1.0", r, 0)
        self.wt_sprint_var.setToolTip(
            "Relative importance of the sprint score in the combined fitness.\n"
            "Higher = hull optimised more for straight-line speed.")
        self.wt_slalom_var = self.mw._add_entry(sg, "Slalom:", "1.0", r, 1)
        self.wt_slalom_var.setToolTip("Relative importance of the slalom score."); r += 1
        self.wt_endure_var = self.mw._add_entry(sg, "Endurance Slalom:", "0.5", r, 0)
        self.wt_endure_var.setToolTip("Relative importance of the endurance slalom score.")
        self.wt_endurance_sprint_var = self.mw._add_entry(sg, "End. Sprint:", "0.0", r, 1)
        self.wt_endurance_sprint_var.setToolTip("Relative importance of the endurance sprint score."); r += 1

        self.mw._add_heading(sg, "Hull Parameters", r); r += 1
        self.evo_hull_len = self.mw._add_entry(sg, "Hull L (m):", "6.1", r, 0)
        self.evo_hull_len.setToolTip(
            "Target hull waterline length in metres.\n"
            "ASCE rules cap at approximately 6.7 m (22 ft)."); r += 1

        self.mw._add_heading(sg, "Crew", r); r += 1
        # Seeded from canoe_suite.cfg (crew.paddler_power_w)
        import config as _cfg_pw
        self.power_var = self.mw._add_entry(
            sg, "Watts/paddler:",
            f"{float(getattr(_cfg_pw, 'PADDLER_POWER_W', 150.0)):g}", r, 0)
        self.power_var.setToolTip(
            "Average sustained paddler power output in watts.\n"
            "100 W = casual recreational, 150 W = trained club, 200 W = competitive elite.\n"
            "Starts from canoe_suite.cfg (crew.paddler_power_w).")
        self.paddler_efficiency_var = self.mw._add_entry(sg, "Efficiency:", "0.75", r, 1)
        self.paddler_efficiency_var.setToolTip(
            "Fraction of paddle power converted to forward thrust (0–1).\n"
            "0.75 = 75% efficiency; accounts for blade slip and stroke mechanics."); r += 1
        self.mw._add_note(sg, "Sustained: 100 W casual – 200 W elite", r); r += 1

        self.mw._add_heading(sg, "Maneuver Load", r); r += 1
        self.mlf_enabled = QCheckBox("Enable Maneuver Load Factor (for Structural Sweep)")
        self.mlf_enabled.setToolTip(
            "When enabled, the Structural Sweep applies a centripetal lateral load\n"
            "computed from the turning speed and radius below.")
        sg.addWidget(self.mlf_enabled, r, 0, 1, 4); r += 1
        self.mlf_speed_var  = self.mw._add_entry(sg, "Turn Speed (m/s):", "2.5", r, 0)
        self.mlf_speed_var.setToolTip(
            "Canoe speed during a tight turning manoeuvre (m/s).\n"
            "Used to compute the centripetal lateral load on the hull.")
        self.mlf_radius_var = self.mw._add_entry(sg, "Turn Radius (m):",  "3.0", r, 1)
        self.mlf_radius_var.setToolTip(
            "Turn radius during the manoeuvre (m).\n"
            "Smaller radius → larger centripetal load."); r += 1

        sg.setRowStretch(r, 1)

        help_btn = QPushButton("? Help")
        help_btn.setFixedHeight(26)
        help_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #555; color: #888; }")
        help_btn.setToolTip("Open the in-app help for this tab")
        help_btn.clicked.connect(lambda: self._open_help())
        left_layout.addWidget(help_btn)

        self.race_btn = QPushButton("Run Race Simulator")
        self.race_btn.setFixedHeight(50)
        self.race_btn.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.race_btn.setStyleSheet(
            "QPushButton { background-color: #8e44ad; }"
            "QPushButton:hover { background-color: #6c3483; }")
        self.race_btn.clicked.connect(self._start_race_sim)
        left_layout.addWidget(self.race_btn)

        # ── Right: Results ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.result_tabs = QTabWidget()
        right_layout.addWidget(self.result_tabs)

        self._drag_tab   = QWidget()
        self._path_tab   = QWidget()
        self._replay_tab = QWidget()
        self.result_tabs.addTab(self._drag_tab,   "Drag Curve")
        self.result_tabs.addTab(self._path_tab,   "Racing Line")
        self.result_tabs.addTab(self._replay_tab, "Replay")

        from race_replay_widget import RaceReplayWidget
        replay_split = QHBoxLayout(self._replay_tab)
        replay_split.setContentsMargins(0, 0, 0, 0)
        replay_split.setSpacing(4)
        self.replay_widget = RaceReplayWidget()
        replay_split.addWidget(self.replay_widget, stretch=3)
        self._telem_graph = _TelemetryGraphPanel()
        replay_split.addWidget(self._telem_graph, stretch=2)
        self.replay_widget.course_combo.currentIndexChanged.connect(
            self._on_replay_course_changed)

        # ── Drag Curve tab ───────────────────────────────────────────
        drag_layout = QVBoxLayout(self._drag_tab)
        drag_layout.setContentsMargins(0, 0, 0, 0)
        self._drag_canvas = None

        # ── Racing Line tab ──────────────────────────────────────────
        path_layout = QVBoxLayout(self._path_tab)
        self._path_metrics_label = QLabel(
            "Select courses and run the Race Simulator.\n"
            "The racing line and % straight / % turn will appear here."
        )
        self._path_metrics_label.setFont(QFont("Consolas", 9))
        self._path_metrics_label.setWordWrap(True)
        path_layout.addWidget(self._path_metrics_label)
        self._path_canvas = None

        layout.addWidget(left,  stretch=1)
        layout.addWidget(right, stretch=2)

    def _set_running(self, running: bool):
        self.race_btn.setEnabled(not running)

    def _on_replay_course_changed(self):
        cname = self.replay_widget.course_combo.currentText()
        telem = self.replay_widget.telemetry_data.get(cname)
        if telem is not None:
            self._telem_graph.load_telemetry(telem)

    def _open_help(self):
        from ui.help_panel import HelpPanel
        HelpPanel.show_help("Race Simulator", parent=self)

    # ------------------------------------------------------------------
    # Tournament presets
    # ------------------------------------------------------------------
    _PRESETS = {
        "regionals": {"sprint": 3.0, "slalom": 2.0, "endurance": 0.0, "endurance_sprint": 0.0},
        "nationals": {"sprint": 2.0, "slalom": 0.0, "endurance": 2.0, "endurance_sprint": 1.0},
        "combined":  {"sprint": 5.0, "slalom": 2.0, "endurance": 2.0, "endurance_sprint": 1.0},
    }

    def _apply_preset(self, preset_name: str):
        w = self._PRESETS[preset_name]
        self.race_sprint_var.setChecked(w["sprint"] > 0)
        self.wt_sprint_var.setText(str(w["sprint"]))
        self.race_slalom_var.setChecked(w["slalom"] > 0)
        self.wt_slalom_var.setText(str(w["slalom"]))
        self.race_endure_var.setChecked(w["endurance"] > 0)
        self.wt_endure_var.setText(str(w["endurance"]))
        self.race_endurance_sprint_var.setChecked(w["endurance_sprint"] > 0)
        self.wt_endurance_sprint_var.setText(str(w["endurance_sprint"]))

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

        _pos_float(self.power_var,              "Watts/paddler")
        _pos_float(self.paddler_efficiency_var, "Efficiency")
        _pos_float(self.evo_hull_len,           "Hull length")

        if self.race_sprint_var.isChecked():
            _pos_float(self.wt_sprint_var, "Sprint weight", lo=-0.001)
        if self.race_slalom_var.isChecked():
            _pos_float(self.wt_slalom_var, "Slalom weight", lo=-0.001)
        if self.race_endure_var.isChecked():
            _pos_float(self.wt_endure_var, "Endurance weight", lo=-0.001)
        if self.race_endurance_sprint_var.isChecked():
            _pos_float(self.wt_endurance_sprint_var, "Endurance Sprint weight", lo=-0.001)

        if not (self.race_sprint_var.isChecked() or
                self.race_slalom_var.isChecked() or
                self.race_endure_var.isChecked() or
                self.race_endurance_sprint_var.isChecked()):
            errors.append("Select at least one course")

        if errors:
            QMessageBox.warning(self, "Invalid Input",
                                "Please fix the following:\n\n• " + "\n• ".join(errors))
            return False
        return True

    # ------------------------------------------------------------------
    # Race simulation pipeline
    # ------------------------------------------------------------------
    def _start_race_sim(self):
        if not self._validate():
            return
        self.mw.log("Starting race simulator...")
        self.mw._launch_worker(self._race_sim_work)
        self.mw._worker.finished_signal.connect(self._on_race_sim_finished)

    def _race_sim_work(self, worker=None):
        from race_simulator import run_strategy_evolution
        from hull_matcher import match_hull
        import canoe_physics

        canoe_physics.PADDLER_POWER_W   = float(self.power_var.text())
        canoe_physics.PADDLE_EFFICIENCY = float(self.paddler_efficiency_var.text())

        course_names, course_weights = [], []
        if self.race_sprint_var.isChecked():
            course_names.append("sprint");    course_weights.append(float(self.wt_sprint_var.text()))
        if self.race_slalom_var.isChecked():
            course_names.append("slalom");    course_weights.append(float(self.wt_slalom_var.text()))
        if self.race_endure_var.isChecked():
            course_names.append("endurance"); course_weights.append(float(self.wt_endure_var.text()))
        if self.race_endurance_sprint_var.isChecked():
            course_names.append("endurance_sprint"); course_weights.append(float(self.wt_endurance_sprint_var.text()))

        if not course_names:
            print("Select at least one course!")
            return None

        n_gen = 30
        pop_size = 40

        def ui_cb(gen, best_time, best_genome):
            frac = min(gen / max(n_gen, 1), 1.0) * 0.5
            if worker:
                worker.progress_signal.emit(frac, f"Tier 1 Gen {gen}/{n_gen}  Best: {best_time:.1f}s")

        results = run_strategy_evolution(
            course_names=course_names,
            course_weights=course_weights,
            pop_size=pop_size,
            n_generations=n_gen,
            ui_callback=ui_cb,
            stop_event=self.mw._stop,
        )

        if self.mw._stop.is_set():
            return results

        if worker:
            worker.progress_signal.emit(0.6, "Tier 2: Physical Hull Matching...")

        ideal_Cs = results["ideal_Cs"]
        ideal_Ct = results["ideal_Ct"]
        hull_length = float(self.evo_hull_len.text())

        match_results = match_hull(ideal_Cs, ideal_Ct, L=hull_length, mass_target=420.0)
        results["best_hull"] = match_results

        from canoe_physics import CanoePerformance, simulate_race_with_telemetry
        from race_course import get_course

        telemetry = {}
        perf = CanoePerformance(
            factors=match_results["factors"],
            B=match_results["beam"],
            T=match_results["draft"],
            L=hull_length,
            n_paddlers=4,
        )
        for cname in course_names:
            course = get_course(cname)
            perf.n_paddlers = course.n_paddlers
            _, telem = simulate_race_with_telemetry(perf, course)
            telemetry[course.name] = telem

        results["telemetry"] = telemetry

        # Path analysis — driven racing line for each selected course
        from physics.race_path import analyse_course
        path_analyses = {}
        for cname in course_names:
            course = get_course(cname)
            path_analyses[cname] = analyse_course(course)
        results["path_analyses"] = path_analyses

        self.mw._race_results = results

        # Audit log
        try:
            from data.session import log_run
            log_run(
                "race_sim",
                {
                    "courses": course_names,
                    "hull_L_m": float(self.evo_hull_len.text()),
                },
                {
                    "ideal_Cs": results.get("ideal_Cs"),
                    "ideal_Ct": results.get("ideal_Ct"),
                    "fitness":  results.get("baseline_time"),
                },
            )
        except Exception:
            pass

        if worker:
            worker.progress_signal.emit(1.0, "Matching Complete!")

        return results

    def _on_race_sim_finished(self, results):
        if not results:
            return
        self.mw._set_progress(1.0, "Race Sim Complete")
        self._display_race_results(results)

    def _display_race_results(self, results):
        lines = []
        lines.append("═" * 60)
        lines.append("  NESTED OPTIMIZATION RESULTS")
        lines.append("═" * 60)
        lines.append("  TIER 1: ABSTRACT STRATEGY")
        lines.append(f"    Ideal Straight Capability (Cs): {results['ideal_Cs']:.3f}")
        lines.append(f"    Ideal Turning Capability  (Ct): {results['ideal_Ct']:.3f}")
        lines.append(f"    Best Weighted Race Time: {results.get('baseline_time', 0):.1f} s")
        lines.append("  TIER 2: PHYSICAL HULL MATCH")
        if "best_hull" in results:
            hull = results["best_hull"]
            lines.append(f"    Beam:    {hull.get('beam', 0):.3f} m")
            lines.append(f"    Draft:   {hull.get('draft', 0):.3f} m")
            lines.append(f"    Factors: {[round(f, 2) for f in hull.get('factors', [])]}")
        self.mw.log("\n".join(lines))

        if "telemetry" in results:
            self.replay_widget.load_telemetry(results["telemetry"])
            # Remap path_analyses from short keys ("sprint") to display names ("Sprint")
            # so the replay widget can match them against the telemetry course names.
            if "path_analyses" in results:
                from race_course import get_course as _gc
                path_by_name = {
                    _gc(cname).name: pa
                    for cname, pa in results["path_analyses"].items()
                }
                self.replay_widget.load_path_analyses(path_by_name)
            # Seed the telemetry graph with the first course (signal may not fire if
            # the combo index was already 0 before load_telemetry cleared it).
            first_telem = next(iter(results["telemetry"].values()), None)
            if first_telem is not None:
                self._telem_graph.load_telemetry(first_telem)
            self.result_tabs.setCurrentWidget(self._replay_tab)

        self._render_drag_tab(results)
        self._render_path_tab(results)

    def _render_drag_tab(self, results):
        """Plot total resistance R(V) for the best hull from the race sim."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        hull = results.get("best_hull")
        if hull is None:
            return

        try:
            L = float(self.evo_hull_len.text())
            B = hull.get("beam", 0.6)
            T = hull.get("draft", 0.20)
            factors = hull.get("factors", [1.0] * 7)

            from hull_generator import generate_hull, compute_hydrostatics
            offsets = generate_hull(factors, L, B, T, n_stations=41, n_waterlines=21)
            hydro   = compute_hydrostatics(offsets, L, T, rho=998.0, g=9.807)
            disp    = hydro["volume"] * 998.0 * 9.807

            from wave_resistance import total_resistance
            speeds = np.linspace(0.5, 4.5, 40)
            R_total, R_wave, R_fric = [], [], []
            for V in speeds:
                try:
                    rt = total_resistance(offsets, L, B, T, float(V), disp)
                    R_total.append(rt.get("Rt", 0))
                    R_wave.append(rt.get("Rw", 0))
                    R_fric.append(rt.get("Rf", 0))
                except Exception:
                    R_total.append(0); R_wave.append(0); R_fric.append(0)

            Fn = speeds / np.sqrt(9.807 * L)

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 4))
            fig.patch.set_facecolor("#1a1a2e")
            for ax in (ax1, ax2):
                ax.set_facecolor("#1a1a2e")
                ax.tick_params(colors="white")
                for sp in ax.spines.values():
                    sp.set_edgecolor("#555")

            ax1.plot(speeds, R_total, color="#00d2ff",  lw=2,   label="Total R")
            ax1.plot(speeds, R_wave,  color="#e74c3c",  lw=1.5, label="Wave R")
            ax1.plot(speeds, R_fric,  color="#2ecc71",  lw=1.5, label="Friction R")
            ax1.set_xlabel("Speed (m/s)", color="white")
            ax1.set_ylabel("Resistance (N)", color="white")
            ax1.set_title("R(V) — Total Drag", color="white")
            ax1.legend(fontsize=8, facecolor="#333", labelcolor="white")

            ax2.plot(Fn, R_wave, color="#e74c3c", lw=2)
            ax2.axvline(0.35, color="#f39c12", lw=1, ls="--", alpha=0.7, label="Fn = 0.35")
            ax2.set_xlabel("Froude Number Fn", color="white")
            ax2.set_ylabel("Wave Resistance (N)", color="white")
            ax2.set_title("Wave Resistance vs Fn", color="white")
            ax2.legend(fontsize=8, facecolor="#333", labelcolor="white")

            fig.suptitle(
                f"Hull  L={L:.2f}m  B={B:.3f}m  T={T:.3f}m",
                color="#aaa", fontsize=10)
            fig.tight_layout(rect=[0, 0, 1, 0.95])
            self.mw._embed_figure(fig, self._drag_tab, "_drag_canvas")
        except Exception as exc:
            self.mw.log(f"  ⚠ Drag curve: {exc}")

    def _render_path_tab(self, results):
        """Populate the Racing Line tab with driven-path charts and Cs/Ct metrics."""
        from race_course import get_course
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        path_analyses = results.get("path_analyses", {})
        if not path_analyses:
            self._path_metrics_label.setText("No course path data available.")
            return

        # Metrics summary text
        lines = []
        for cname, pa in path_analyses.items():
            lines.append("─" * 42)
            lines.append(f"  {cname.upper()}")
            lines.append(
                f"  Total: {pa['total_m']:.1f} m  |  "
                f"Straight: {pa['pct_straight']:.1f}%  |  "
                f"Turning: {pa['pct_turn']:.1f}%"
            )
            lines.append(f"  Cs = {pa['Cs']:.3f}   Ct = {pa['Ct']:.3f}")
        self._path_metrics_label.setText("\n".join(lines))

        # One subplot per course
        n = len(path_analyses)
        fig, axes = plt.subplots(1, n, figsize=(8 * n, 5))
        fig.patch.set_facecolor("#1a1a2e")
        if n == 1:
            axes = [axes]

        for ax, (cname, pa) in zip(axes, path_analyses.items()):
            course = get_course(cname)
            wp_x = [w.x for w in course.waypoints]
            wp_y = [w.y for w in course.waypoints]
            path_xy = pa["path_xy"]

            ax.set_facecolor("#1a1a2e")
            ax.tick_params(colors="white")
            for spine in ax.spines.values():
                spine.set_edgecolor("#555")

            ax.plot(wp_x, wp_y, "o", color="#555", linewidth=0,
                    markersize=5, alpha=0.6, label="Waypoints")
            ax.scatter([wp_x[0]], [wp_y[0]], color="#2ecc71", zorder=5, s=80, label="Start")
            ax.scatter([wp_x[-1]], [wp_y[-1]], color="#e74c3c", zorder=5, s=80, label="Finish")
            for wp in course.waypoints:
                if wp.turn_radius > 0:
                    plot_x = wp.x + wp.turn_radius if wp.turn_angle >= 180 else wp.x
                    ax.scatter([plot_x], [wp.y], color="#f39c12", zorder=4, s=40, marker="^")
            if len(path_xy) > 1:
                ax.plot(path_xy[:, 0], path_xy[:, 1],
                        color="#00d2ff", linewidth=2.5, label="Racing line")

            ax.set_title(
                f"{cname}  —  {pa['total_m']:.0f} m\n"
                f"{pa['pct_straight']:.1f}% straight / {pa['pct_turn']:.1f}% turning  "
                f"|  Cs={pa['Cs']:.3f}  Ct={pa['Ct']:.3f}",
                color="white", fontsize=9,
            )
            ax.set_xlabel("x (m)", color="white")
            ax.set_ylabel("y (m)", color="white")
            ax.set_aspect("equal")
            ax.legend(fontsize=7, facecolor="#333", labelcolor="white", framealpha=0.8)

        fig.tight_layout(pad=1.0)
        self.mw._embed_figure(fig, self._path_tab, "_path_canvas")

    # ------------------------------------------------------------------
    # Apply seed hull
    # ------------------------------------------------------------------
    def _prompt_apply_seed(self, results, hull_length_m):
        hull = results["best_hull"]
        msg = (
            f"The race simulator found an optimal hull:\n\n"
            f"  Beam:    {hull['beam']:.3f} m\n"
            f"  Draft:   {hull['draft']:.3f} m\n"
            f"  Factors: {[round(f, 2) for f in hull['factors']]}\n"
            f"  Race time: {results.get('baseline_time', 0):.1f} s\n\n"
            f"Apply this hull as the starting seed in in.mlt\n"
            f"for Godzilla to optimise from?"
        )
        answer = QMessageBox.question(self, "Apply Race Sim Results?", msg)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self._write_seed_to_in_mlt(results, hull_length_m)
                self.mw.log("✅ Optimal hull seed written to in.mlt")
                self.mw.log("   → Switching to Step ② Godzilla")
                self.mw.tabs.setCurrentIndex(1)
                QTimer.singleShot(100, self.mw.struct_tab._refresh_file_status)
            except Exception as e:
                self.mw.log(f"❌ Failed to write in.mlt: {e}")
        else:
            self.mw.log("ℹ️  Seed not applied. Switch to ② when ready.")

    def _write_seed_to_in_mlt(self, results, hull_length_m):
        from services.hull_service import HullService
        cwd = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
        hydro, offsets = HullService().write_seed_hull(
            results, hull_length_m, cwd, log_fn=self.mw.log
        )
        hull = results["best_hull"]
        L = hull_length_m
        T_opt = hull["draft"]
        beam = hydro["beam"]
        self.mw.struct_tab._render_hull_preview(
            offsets, L, T_opt,
            title=f"Seed Hull  (L={L:.2f}m  B={beam:.3f}m  T={T_opt:.3f}m)"
        )


# ---------------------------------------------------------------------------
# Telemetry graph panel — distance / speed / acceleration over time
# ---------------------------------------------------------------------------
class _TelemetryGraphPanel(QWidget):
    """Triple-axis time-series graph for the Replay tab right panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._telem      = None
        self._line_speed = None
        self._line_dist  = None
        self._line_accel = None
        self.ax_speed    = None
        self.ax_dist     = None
        self.ax_accel    = None
        self._build()

    def _build(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(4)

        # ── Checkboxes ──
        cb_row = QHBoxLayout()
        lbl = QLabel("Show:")
        lbl.setStyleSheet("color: #aaa; font-size: 9pt;")
        cb_row.addWidget(lbl)

        self.chk_speed = QCheckBox("Speed (m/s)")
        self.chk_dist  = QCheckBox("Distance (m)")
        self.chk_accel = QCheckBox("Acceleration (m/s²)")
        self.chk_speed.setChecked(True)
        self.chk_dist.setChecked(False)
        self.chk_accel.setChecked(False)
        self.chk_speed.setStyleSheet("color: #3498db;")
        self.chk_dist.setStyleSheet("color: #2ecc71;")
        self.chk_accel.setStyleSheet("color: #e74c3c;")

        for cb in (self.chk_speed, self.chk_dist, self.chk_accel):
            cb.toggled.connect(self._update_graph_visibility)
            cb_row.addWidget(cb)
        cb_row.addStretch()
        vbox.addLayout(cb_row)

        # ── Matplotlib canvas ──
        self.fig = plt.figure(figsize=(4, 3.5), facecolor="#1a1a2e")
        self.replay_canvas = FigureCanvasQTAgg(self.fig)
        from ui.chart_export import enable_chart_export
        enable_chart_export(self.replay_canvas)
        vbox.addWidget(self.replay_canvas, stretch=1)

    def load_telemetry(self, telem: np.ndarray):
        self._telem = telem
        self._plot_telemetry()

    def _plot_telemetry(self):
        self.fig.clf()

        if self._telem is None or len(self._telem) == 0:
            self.replay_canvas.draw_idle()
            return

        t = self._telem[:, 0]
        v = self._telem[:, 4]

        # Cumulative trapezoidal distance
        dt = np.diff(t)
        distance = np.concatenate([[0.0], np.cumsum(0.5 * (v[:-1] + v[1:]) * dt)])

        # Acceleration via finite differences
        accel = np.gradient(v, t)

        # ── Triple-axis setup ──
        self.ax_speed = self.fig.add_subplot(111)
        self.ax_speed.set_facecolor("#1a1a2e")
        self.ax_dist  = self.ax_speed.twinx()
        self.ax_accel = self.ax_speed.twinx()

        # Offset third axis so it doesn't overlap the second
        self.ax_accel.spines['right'].set_position(('axes', 1.15))
        self.ax_accel.patch.set_alpha(0)
        self.ax_dist.patch.set_alpha(0)

        for ax in (self.ax_speed, self.ax_dist, self.ax_accel):
            ax.set_facecolor("#1a1a2e")
            ax.tick_params(colors="gray", labelsize=7)
            for spine in ax.spines.values():
                spine.set_color("#2a2a4a")

        # Plot and store line handles
        self._line_speed = self.ax_speed.plot(t, v,        color='#3498db', linewidth=1.2, label='Speed')
        self._line_dist  = self.ax_dist.plot( t, distance, color='#2ecc71', linewidth=1.2, label='Distance')
        self._line_accel = self.ax_accel.plot(t, accel,    color='#e74c3c', linewidth=1.2, label='Accel')
        self.ax_accel.axhline(0, color="#444", linewidth=0.5, linestyle="--", alpha=0.6)

        # Color-coded axis labels and ticks
        self.ax_speed.set_xlabel("Time (s)",       color="gray",    fontsize=7)
        self.ax_speed.set_ylabel("Speed (m/s)",    color="#3498db", fontsize=7)
        self.ax_dist.set_ylabel( "Distance (m)",   color="#2ecc71", fontsize=7)
        self.ax_accel.set_ylabel("Accel (m/s²)",   color="#e74c3c", fontsize=7)
        self.ax_speed.tick_params(axis='y', colors='#3498db', labelsize=7)
        self.ax_dist.tick_params( axis='y', colors='#2ecc71', labelsize=7)
        self.ax_accel.tick_params(axis='y', colors='#e74c3c', labelsize=7)
        self.ax_speed.tick_params(axis='x', colors='gray',    labelsize=7)

        self.fig.subplots_adjust(right=0.78)
        self._update_graph_visibility()

    def _update_graph_visibility(self):
        if self._line_speed is None:
            return

        is_speed = self.chk_speed.isChecked()
        is_dist  = self.chk_dist.isChecked()
        is_accel = self.chk_accel.isChecked()

        self._line_speed[0].set_visible(is_speed)
        self.ax_speed.get_yaxis().set_visible(is_speed)

        self._line_dist[0].set_visible(is_dist)
        self.ax_dist.get_yaxis().set_visible(is_dist)

        self._line_accel[0].set_visible(is_accel)
        self.ax_accel.get_yaxis().set_visible(is_accel)

        self.replay_canvas.draw_idle()
