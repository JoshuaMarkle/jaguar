# ui/optimizer_tab.py
"""
Tab 5 — NSGA-II Pareto Hull Optimisation (plan §9.5).

Runs a bi-objective optimisation (minimise drag, maximise stability)
using pymoo's NSGA-II algorithm and displays the evolving Pareto front.
The user can select any point on the Pareto front and apply it as the
seed hull for the Godzilla / structural-sweep pipeline.
"""
from __future__ import annotations

import threading

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox,
    QDoubleSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QSplitter, QSizePolicy,
)

from ui.widgets import NoScrollSpinBox, NoScrollDoubleSpinBox


class OptimizerTab(QWidget):
    """Self-contained NSGA-II Pareto optimisation tab."""

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self._pareto_F: np.ndarray | None = None   # (n, 2) current Pareto front
        self._pareto_X: np.ndarray | None = None   # (n, 9) decision variables
        self._pareto_hydro: list[dict] = []
        self._pareto_canvas = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ── Left: controls ──
        left = QWidget()
        left.setMaximumWidth(320)
        left_lay = QVBoxLayout(left)
        left_lay.setSpacing(6)

        heading = QLabel("NSGA-II Pareto Optimisation")
        heading.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        heading.setStyleSheet("color: #00d2ff;")
        left_lay.addWidget(heading)

        left_lay.addWidget(QLabel("Hull / Speed parameters", objectName="heading"))
        grid = QGridLayout()
        grid.setSpacing(4)

        def _dspin(lo, hi, val, dec=2, w=90, suffix=""):
            s = NoScrollDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setValue(val)
            s.setFixedWidth(w)
            if suffix:
                s.setSuffix(f" {suffix}")
            return s

        self._len_spin    = _dspin(4.0, 8.0, 6.1, suffix="m")
        self._beam_min    = _dspin(0.30, 1.0, 0.45, suffix="m")
        self._beam_max    = _dspin(0.35, 1.5, 0.76, suffix="m")
        self._draft_min   = _dspin(0.05, 0.5, 0.08, suffix="m")
        self._draft_max   = _dspin(0.10, 1.0, 0.30, suffix="m")
        self._speed_spin  = _dspin(0.5, 5.0, 2.0, suffix="m/s")
        self._disp_lo     = _dspin(0.01, 0.5, 0.04, dec=3, suffix="m³")
        self._disp_hi     = _dspin(0.02, 1.0, 0.12, dec=3, suffix="m³")

        rows = [
            ("Length:", self._len_spin),
            ("Beam min:", self._beam_min),
            ("Beam max:", self._beam_max),
            ("Draft min:", self._draft_min),
            ("Draft max:", self._draft_max),
            ("Design speed:", self._speed_spin),
            ("Disp. min:", self._disp_lo),
            ("Disp. max:", self._disp_hi),
        ]
        for r, (lbl, w) in enumerate(rows):
            grid.addWidget(QLabel(lbl), r, 0, Qt.AlignmentFlag.AlignRight)
            grid.addWidget(w, r, 1)
        left_lay.addLayout(grid)

        left_lay.addWidget(QLabel("Optimiser settings", objectName="heading"))
        opt_grid = QGridLayout()
        opt_grid.setSpacing(4)

        self._gen_spin = NoScrollSpinBox()
        self._gen_spin.setRange(5, 500)
        self._gen_spin.setValue(40)
        self._gen_spin.setFixedWidth(80)
        self._gen_spin.setToolTip("Number of NSGA-II generations")

        self._pop_spin = NoScrollSpinBox()
        self._pop_spin.setRange(10, 200)
        self._pop_spin.setValue(40)
        self._pop_spin.setFixedWidth(80)
        self._pop_spin.setToolTip("Population size per generation")

        opt_grid.addWidget(QLabel("Generations:"), 0, 0,
                           Qt.AlignmentFlag.AlignRight)
        opt_grid.addWidget(self._gen_spin, 0, 1)
        opt_grid.addWidget(QLabel("Population:"), 1, 0,
                           Qt.AlignmentFlag.AlignRight)
        opt_grid.addWidget(self._pop_spin, 1, 1)
        left_lay.addLayout(opt_grid)

        self._run_btn = QPushButton("▶  Run NSGA-II")
        self._run_btn.setFixedHeight(40)
        self._run_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._run_btn.setStyleSheet(
            "QPushButton { background-color: #0077aa; }"
            "QPushButton:hover { background-color: #005588; }")
        self._run_btn.setToolTip(
            "Run multi-objective NSGA-II to optimise drag vs. stability")
        self._run_btn.clicked.connect(self._start_nsga2)
        left_lay.addWidget(self._run_btn)

        self._apply_btn = QPushButton("✔  Apply Selected to Pipeline")
        self._apply_btn.setFixedHeight(32)
        self._apply_btn.setEnabled(False)
        self._apply_btn.setStyleSheet(
            "QPushButton { background-color: #1a6b3c; }"
            "QPushButton:hover { background-color: #145530; }")
        self._apply_btn.setToolTip(
            "Write the selected Pareto solution to off1.mlt and out.mlt "
            "so the Godzilla + structural sweep can use it.")
        self._apply_btn.clicked.connect(self._apply_selected)
        left_lay.addWidget(self._apply_btn)

        self._status_lbl = QLabel("Configure parameters and click ▶ Run NSGA-II.")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setObjectName("note")
        left_lay.addWidget(self._status_lbl)

        # Pareto table
        left_lay.addWidget(QLabel("Pareto front solutions:", objectName="heading"))
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["#", "Drag (N)", "GMT (m)", "Beam (m)"])
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMaximumHeight(200)
        self._table.itemSelectionChanged.connect(self._on_table_select)
        left_lay.addWidget(self._table)
        left_lay.addStretch()

        splitter.addWidget(left)

        # ── Right: Pareto front canvas ──
        self._plot_container = QWidget()
        QVBoxLayout(self._plot_container)
        splitter.addWidget(self._plot_container)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([310, 700])

    # ------------------------------------------------------------------
    # Running state
    # ------------------------------------------------------------------
    def _set_running(self, running: bool):
        self._run_btn.setEnabled(not running)
        if running:
            self._apply_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # NSGA-II launch
    # ------------------------------------------------------------------
    def _start_nsga2(self):
        self.mw.log("🧬 Starting NSGA-II Pareto optimisation…")
        self._pareto_F = None
        self._pareto_X = None
        self._pareto_hydro = []
        self._table.setRowCount(0)
        self.mw._launch_worker(self._nsga2_work)

    def _nsga2_work(self, worker=None):
        import config as _cfg

        cfg = {
            "length_m":     self._len_spin.value(),
            "design_speed": self._speed_spin.value(),
            "disp_lo":      self._disp_lo.value(),
            "disp_hi":      self._disp_hi.value(),
            "rho":          998.0,
            "g":            9.807,
            "nu":           1.004e-6,
            "n_stations":   41,
            "n_waterlines": 21,
            "ntheta":       64,
            "pop_size":     self._pop_spin.value(),
            "friction_method": 1,
            "constraints": {
                "beam_min":  self._beam_min.value(),
                "beam_max":  self._beam_max.value(),
                "draft_min": self._draft_min.value(),
                "draft_max": self._draft_max.value(),
                "disp_min":  self._disp_lo.value(),
                "disp_max":  self._disp_hi.value(),
                "lcb_min": -0.15,
                "lcb_max":  0.05,
                "gmt_min":   0.0,
            },
            "cwd": _cfg.__file__.replace("config.py", "").rstrip("/\\"),
        }

        n_gen     = self._gen_spin.value()
        pop_size  = self._pop_spin.value()
        stop_ev   = self.mw._stop

        # Accumulated Pareto front from latest generation
        _latest_F: list[np.ndarray] = []
        _latest_X: list[np.ndarray] = []

        def _ui_cb(gen, best_Rt, hydro, offsets):
            frac = gen / n_gen
            if worker:
                worker.progress_signal.emit(frac, f"Gen {gen}/{n_gen}  Rt={best_Rt:.2f} N")

        from optimizer import run_optimization
        try:
            offsets, hydro = run_optimization(
                cfg, mode="NSGA-II", max_gen=n_gen,
                ui_callback=_ui_cb,
                stop_event=stop_ev,
            )
        except Exception as exc:
            import traceback
            if worker:
                worker.log_signal.emit(f"NSGA-II error: {exc}\n")
            return

        # After the run, extract the full Pareto front from pymoo result
        # by re-running a quick evaluation on the final population.
        # We already have the best single solution — build a surrogate front
        # from the callback's accumulated data.  If pymoo exposes result.F,
        # use that directly.
        self._collect_pareto_after_run(cfg, offsets, hydro, n_gen, pop_size,
                                       stop_ev, worker)

    def _collect_pareto_after_run(self, cfg, best_offsets, best_hydro,
                                   n_gen, pop_size, stop_ev, worker):
        """Re-run a short NSGA-II to capture the final Pareto population."""
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.optimize import minimize as pymoo_minimize
        from pymoo.termination import get_termination
        from optimizer import HullProblem

        problem = HullProblem(cfg, multi_objective=True)
        algo    = NSGA2(pop_size=pop_size)
        term    = get_termination("n_gen", n_gen)

        result = pymoo_minimize(problem, algo, term,
                                seed=cfg.get("random_seed", 1),
                                verbose=False)

        if result.F is not None and len(result.F) > 0:
            F = result.F                    # (k, 2): [drag, -GMT]
            X = result.X                    # (k, 9)
            drag   = F[:, 0]
            gmt    = -F[:, 1]              # flip sign back

            order = np.argsort(drag)
            F_sorted = F[order]
            X_sorted = X[order]
            gmt_sorted = gmt[order]

            self._pareto_F = F_sorted
            self._pareto_X = X_sorted

            # Recompute hydrostatics for table display
            from hull_generator import generate_hull, compute_hydrostatics
            L = cfg["length_m"]
            hydros = []
            for i in range(len(X_sorted)):
                x = X_sorted[i]
                B, T = float(x[7]), float(x[8])
                offs = generate_hull(x[:7], L, B, T,
                                     cfg["n_stations"], cfg["n_waterlines"])
                h = compute_hydrostatics(offs, L, T, cfg["rho"], cfg["g"])
                hydros.append(h)
            self._pareto_hydro = hydros

            if worker:
                worker.log_signal.emit(
                    f"✅ NSGA-II complete — {len(X_sorted)} Pareto solutions\n")
            QTimer.singleShot(0, self._refresh_pareto_display)
        else:
            # Fallback: display the single best solution found
            from hull_generator import compute_hydrostatics
            self._pareto_F = np.array([[0.0, -best_hydro.get("gmt", 0)]])
            self._pareto_X = None
            self._pareto_hydro = [best_hydro]
            QTimer.singleShot(0, self._refresh_pareto_display)

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    def _refresh_pareto_display(self):
        if self._pareto_F is None:
            return
        self._update_table()
        self._plot_pareto()
        self._status_lbl.setText(
            f"Pareto front: {len(self._pareto_F)} solutions. "
            "Select a row, then click ✔ Apply.")
        self._apply_btn.setEnabled(self._pareto_X is not None)

    def _update_table(self):
        F = self._pareto_F
        hydros = self._pareto_hydro
        n = len(F)
        self._table.setRowCount(n)
        for i in range(n):
            drag = F[i, 0]
            gmt  = hydros[i].get("gmt", float(-F[i, 1])) if hydros else float(-F[i, 1])
            beam = hydros[i].get("beam", 0.0) if hydros else 0.0
            for col, val in enumerate([i + 1, drag, gmt, beam]):
                item = QTableWidgetItem(
                    str(val) if isinstance(val, int) else f"{val:.4f}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, col, item)
        if n > 0:
            self._table.selectRow(0)

    def _plot_pareto(self):
        import matplotlib.pyplot as plt

        F      = self._pareto_F
        drag   = F[:, 0]
        gmt    = np.array([h.get("gmt", 0) for h in self._pareto_hydro]) \
                 if self._pareto_hydro else -F[:, 1]

        fig, ax = plt.subplots(figsize=(5, 4), facecolor="#1a1a2e")
        ax.set_facecolor("#1a1a2e")
        ax.tick_params(colors="gray", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#2a2a4a")

        ax.scatter(drag, gmt, c="#00d2ff", s=40, zorder=5, label="Pareto solutions")

        # Highlight the selected solution
        sel_rows = self._table.selectedItems()
        if sel_rows:
            row = self._table.currentRow()
            if 0 <= row < len(drag):
                ax.scatter([drag[row]], [gmt[row]],
                           c="#ff6600", s=80, zorder=10, marker="*",
                           label=f"Selected #{row + 1}")

        ax.set_xlabel("Total Resistance (N)", color="gray", fontsize=8)
        ax.set_ylabel("GMT — Initial Stability (m)", color="gray", fontsize=8)
        ax.set_title("Pareto Front: Drag vs. Stability", color="white", fontsize=9)
        ax.legend(fontsize=7, facecolor="#1a1a2e", labelcolor="gray",
                  edgecolor="#2a2a4a")
        fig.tight_layout()

        self.mw._embed_figure(fig, self._plot_container, "_pareto_canvas")

    def _on_table_select(self):
        if self._pareto_F is not None:
            self._plot_pareto()

    # ------------------------------------------------------------------
    # Apply selected solution to the pipeline
    # ------------------------------------------------------------------
    def _apply_selected(self):
        if self._pareto_X is None or self._pareto_hydro is None:
            return

        row = self._table.currentRow()
        if row < 0 or row >= len(self._pareto_X):
            return

        import sys, os
        from hull_generator import generate_hull, compute_hydrostatics
        from optimizer import _write_off1, _write_out

        x    = self._pareto_X[row]
        hydro = self._pareto_hydro[row]
        L = float(self._len_spin.value())
        B, T = float(x[7]), float(x[8])

        offsets = generate_hull(
            x[:7], L, B, T,
            int(self._pop_spin.value()),     # reuse n_stations from pop_size? No — use fixed
            21,
        )
        offsets = generate_hull(x[:7], L, B, T, 41, 21)

        cwd = os.path.dirname(os.path.abspath(
            sys.modules["__main__"].__file__))

        try:
            _write_off1(offsets, cwd)
            _write_out(hydro, cwd)
            self.mw.log(
                f"✅ Applied Pareto solution #{row + 1}: "
                f"Drag={hydro.get('Rt', 0):.2f} N  GMT={hydro.get('gmt', 0):.3f} m  "
                f"→ off1.mlt + out.mlt written."
            )
            # Refresh structural tab's file status
            self.mw.struct_tab._refresh_file_status()
        except Exception as exc:
            self.mw.log(f"❌ Apply failed: {exc}")
