# ui/manual_hull_tab.py
"""Godzilla Configurator — manual hull parameter entry, live seed preview, and optimization launch."""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QScrollArea, QDoubleSpinBox, QSpinBox, QPushButton, QLabel,
    QSizePolicy, QAbstractSpinBox, QComboBox, QToolTip, QCheckBox,
    QFileDialog, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QCursor

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

if TYPE_CHECKING:
    from ui.main_window import CanoeOptimizationSuite

# The module's parent -- used only as the default DIRECTORY for the save/load
# dialogs, where it is merely a starting point a user can navigate away from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# in.mlt is DATA and goes through the shared search, so a user's own seed hull
# beside the executable wins over the bundled copy.  `asset_search_paths`
# rather than `resolve_asset`: this file is WRITTEN as well as read, so a
# not-yet-existing path is a legitimate answer.
from asset_paths import asset_search_paths as _asset_search_paths
_IN_MLT_CANDIDATES = _asset_search_paths("in.mlt")
_IN_MLT = next((c for c in _IN_MLT_CANDIDATES if os.path.exists(c)),
               _IN_MLT_CANDIDATES[0])

# Multiply SI value by these to get US Customary display values
_M_TO_FT   = 3.28084
_M_TO_IN   = 39.3701   # beam and draft displayed in inches (US)
_KG_TO_LBS = 2.20462
_MS_TO_KN  = 1.94384   # m/s → knots


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that ignores wheel events unless focused; no up/down arrows."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollSpinBox(QSpinBox):
    """QSpinBox variant of NoScrollDoubleSpinBox."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def _dsb(val: float, lo: float, hi: float, step: float, decimals: int) -> NoScrollDoubleSpinBox:
    sb = NoScrollDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setSingleStep(step)
    sb.setDecimals(decimals)
    sb.setValue(val)
    return sb


class ManualHullTab(QWidget):
    """Tab for manually configuring hull parameters and running Godzilla."""

    def __init__(self, mw: "CanoeOptimizationSuite"):
        super().__init__(mw)
        self._mw = mw
        self._preview_canvas = None
        # Widgets are built with their SI literal defaults, then converted in
        # one pass below — US Customary is what the team actually designs in.
        self._use_metric = True
        self._build_ui()
        self.unit_combo.setCurrentIndex(1)      # → _on_unit_changed converts
        # Publish Min GMT to config so the structural sweep can gate transverse
        # stability on it (task 7); keep it live as the user edits the field.
        self.sb_min_gmt.valueChanged.connect(self._publish_min_gmt)
        self._publish_min_gmt()
        self._connect_preview_signals()
        QTimer.singleShot(0, self._render_seed_preview)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        root.addLayout(top, stretch=1)

        # ── Left: scrollable input column ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        top.addWidget(scroll, stretch=2)

        content = QWidget()
        scroll.setWidget(content)
        vbox = QVBoxLayout(content)
        vbox.setSpacing(8)

        # ── Save / Load toolbar ──
        profile_bar = QHBoxLayout()
        self.btn_save = QPushButton("💾 Save Profile")
        self.btn_save.setFixedHeight(28)
        self.btn_save.setToolTip("Save all hull parameters to a JSON profile file.")
        self.btn_save.clicked.connect(self._save_profile)
        self.btn_load = QPushButton("📂 Load Profile")
        self.btn_load.setFixedHeight(28)
        self.btn_load.setToolTip("Load hull parameters from a previously saved JSON profile.")
        self.btn_load.clicked.connect(self._load_profile)
        profile_bar.addWidget(self.btn_save)
        profile_bar.addWidget(self.btn_load)
        profile_bar.addStretch()
        vbox.addLayout(profile_bar)

        # ── Unit system selector ──
        unit_bar = QHBoxLayout()
        unit_bar.addWidget(QLabel("Units:"))
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["Metric (SI)", "US Customary"])
        self.unit_combo.setFixedWidth(140)
        self.unit_combo.currentIndexChanged.connect(self._on_unit_changed)
        unit_bar.addWidget(self.unit_combo)
        unit_bar.addStretch()
        vbox.addLayout(unit_bar)

        # ── Primary group ──
        primary_grp = QGroupBox("Primary Hull Dimensions")
        primary_form = QFormLayout(primary_grp)

        # Default values in SI; baseline = proven 20-ft (6.10 m) design
        self.sb_L    = _dsb(6.10,  1.0,   10.0,  0.1,   2)
        self.sb_beam = _dsb(0.737, 0.1,    2.0,  0.01,  3)
        self.sb_T    = _dsb(0.150, 0.01,   1.0,  0.005, 3)
        self.sb_mass    = _dsb(258.5, 1.0, 5000.0,  0.5,   1)
        self.sb_min_gmt = _dsb(0.05, -10.0, 10.0, 0.01,  3)
        self.sb_max_gmt = _dsb(10.0,   0.0, 100.0, 0.1,   2)

        self.sb_L_min = _dsb(5.00, 1.0,  15.0, 0.1,  2)
        self.sb_L_max = _dsb(6.50, 1.0,  15.0, 0.1,  2)
        self.sb_B_min = _dsb(0.500, 0.05,  3.0, 0.01, 3)
        self.sb_B_max = _dsb(0.900, 0.05,  3.0, 0.01, 3)
        self.sb_T_min = _dsb(0.050, 0.01,  1.0, 0.01, 3)
        self.sb_T_max = _dsb(0.300, 0.01,  1.0, 0.01, 3)

        self.bow_rocker_input    = _dsb(0.051, 0.000, 0.300, 0.001, 3)
        self.stern_rocker_input  = _dsb(0.068, 0.000, 0.300, 0.001, 3)
        self.sb_bow_rocker_min   = _dsb(0.000, 0.000, 0.300, 0.001, 3)
        self.sb_bow_rocker_max   = _dsb(0.150, 0.000, 0.300, 0.001, 3)
        self.sb_stern_rocker_min = _dsb(0.000, 0.000, 0.300, 0.001, 3)
        self.sb_stern_rocker_max = _dsb(0.150, 0.000, 0.300, 0.001, 3)

        _TIP_L    = ("The overall waterline length of the hull. Longer hulls typically have a "
                     "higher theoretical top speed (lower wave-making drag) but turn slower.")
        _TIP_LBND = "The allowable waterline length bounds for the evolutionary algorithm to explore."
        _TIP_BEAM = ("The maximum transverse width of the hull at the waterline. A wider beam "
                     "increases transverse stability but increases frontal area and form drag.")
        _TIP_BBND = ("The allowable width bounds. Constrain the minimum beam tightly to ensure "
                     "paddlers physically fit and the hull remains stable.")
        _TIP_T    = ("The vertical distance from the waterline to the lowest point of the keel. "
                     "Represents exactly how deep the canoe sits in the water at design weight.")
        _TIP_TBND = ("The allowable depth bounds. A shallower maximum draft forces the algorithm "
                     "to generate wider, more stable shapes to support the displacement mass.")
        _TIP_MASS    = ("Total combined mass of the concrete canoe, paddlers, and gear. The solver "
                        "uses this to calculate the required submerged volume.")
        _TIP_MIN_GMT = ("Minimum Transverse Metacentric Height. A higher number ensures greater roll "
                        "stability and prevents the optimizer from generating pencil-thin, "
                        "un-paddleable hulls.")
        _TIP_MAX_GMT = ("Maximum Transverse Metacentric Height. Usually left very high unless you "
                        "specifically want to prevent snap-rolling.")
        _TIP_BOW_ROCKER   = ("Vertical rise of the keel at the bow above the lowest keel point "
                              "(units per the field label). Higher rocker lifts the bow for easier "
                              "turning but reduces straight-line tracking efficiency.")
        _TIP_STERN_ROCKER = ("Vertical rise of the keel at the stern above the lowest keel point "
                              "(units per the field label). Stern rocker affects water separation "
                              "and course stability.")
        _TIP_ROCKER_BND   = "Allowable rocker height bounds for the optimizer search."

        self._lbl_L    = QLabel("Length (m):")
        self._lbl_beam = QLabel("Beam (m):")
        self._lbl_T    = QLabel("Draft (m):")
        self._lbl_mass = QLabel("Displacement Mass (kg):")
        self._lbl_L_min = QLabel("Min Length (m):")
        self._lbl_L_max = QLabel("Max Length (m):")
        self._lbl_B_min = QLabel("Min Beam (m):")
        self._lbl_B_max = QLabel("Max Beam (m):")
        self._lbl_T_min = QLabel("Min Draft (m):")
        self._lbl_T_max = QLabel("Max Draft (m):")
        self._lbl_bow_rocker      = QLabel("Bow Rocker (m):")
        self._lbl_bow_rocker_min  = QLabel("Min Bow Rocker (m):")
        self._lbl_bow_rocker_max  = QLabel("Max Bow Rocker (m):")
        self._lbl_stern_rocker     = QLabel("Stern Rocker (m):")
        self._lbl_stern_rocker_min = QLabel("Min Stern Rocker (m):")
        self._lbl_stern_rocker_max = QLabel("Max Stern Rocker (m):")
        self._lbl_min_gmt = QLabel("Min GMT (m):")
        self._lbl_max_gmt = QLabel("Max GMT (m):")

        primary_form.addRow(self._lbl_L,     self._with_info(self.sb_L,     _TIP_L))
        primary_form.addRow(self._lbl_L_min, self._with_info(self.sb_L_min, _TIP_LBND))
        primary_form.addRow(self._lbl_L_max, self._with_info(self.sb_L_max, _TIP_LBND))
        primary_form.addRow(QLabel(""), QLabel(""))

        primary_form.addRow(self._lbl_beam,  self._with_info(self.sb_beam,  _TIP_BEAM))
        primary_form.addRow(self._lbl_B_min, self._with_info(self.sb_B_min, _TIP_BBND))
        primary_form.addRow(self._lbl_B_max, self._with_info(self.sb_B_max, _TIP_BBND))
        primary_form.addRow(QLabel(""), QLabel(""))

        primary_form.addRow(self._lbl_T,     self._with_info(self.sb_T,     _TIP_T))
        primary_form.addRow(self._lbl_T_min, self._with_info(self.sb_T_min, _TIP_TBND))
        primary_form.addRow(self._lbl_T_max, self._with_info(self.sb_T_max, _TIP_TBND))
        primary_form.addRow(QLabel(""), QLabel(""))

        primary_form.addRow(self._lbl_bow_rocker,     self._with_info(self.bow_rocker_input,   _TIP_BOW_ROCKER))
        primary_form.addRow(self._lbl_bow_rocker_min, self._with_info(self.sb_bow_rocker_min,  _TIP_ROCKER_BND))
        primary_form.addRow(self._lbl_bow_rocker_max, self._with_info(self.sb_bow_rocker_max,  _TIP_ROCKER_BND))
        primary_form.addRow(QLabel(""), QLabel(""))

        primary_form.addRow(self._lbl_stern_rocker,     self._with_info(self.stern_rocker_input,  _TIP_STERN_ROCKER))
        primary_form.addRow(self._lbl_stern_rocker_min, self._with_info(self.sb_stern_rocker_min, _TIP_ROCKER_BND))
        primary_form.addRow(self._lbl_stern_rocker_max, self._with_info(self.sb_stern_rocker_max, _TIP_ROCKER_BND))
        primary_form.addRow(QLabel(""), QLabel(""))

        primary_form.addRow(self._lbl_mass,  self._with_info(self.sb_mass,  _TIP_MASS))
        primary_form.addRow(QLabel(""), QLabel(""))
        primary_form.addRow(self._lbl_min_gmt, self._with_info(self.sb_min_gmt, _TIP_MIN_GMT))
        primary_form.addRow(self._lbl_max_gmt, self._with_info(self.sb_max_gmt, _TIP_MAX_GMT))
        vbox.addWidget(primary_grp)

        # ── Toggle button ──
        self.toggle_btn = QPushButton("▶  Show Advanced Settings")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.clicked.connect(self._toggle_advanced)
        vbox.addWidget(self.toggle_btn)

        # ── Advanced container ──
        self.advanced_container = QWidget()
        self.advanced_container.setVisible(False)
        adv_vbox = QVBoxLayout(self.advanced_container)
        adv_vbox.setContentsMargins(0, 0, 0, 0)
        adv_vbox.setSpacing(8)

        # A. Shape Factor Seeds
        sf_grp = QGroupBox("Shape Factor Seeds")
        sf_form = QFormLayout(sf_grp)
        self.sb_factors: list[QDoubleSpinBox] = []
        _sf_rows = [
            ("f0 — Fore waterplane:", 3.0,
             "Controls the fullness of the bow at the waterline. Lower values create a finer, "
             "sharper entry; higher values create a blunter bow."),
            ("f1 — Section fullness:", 3.0,
             "Controls the midship cross-section. Lower values create a sharper V-shape; "
             "higher values create a fuller U-shape or flat bottom."),
            ("f2 — Fore rocker:", 3.0,
             "Controls the upward curve of the keel line at the bow. Higher values lift the bow "
             "out of the water faster, improving turning at the cost of tracking."),
            ("f3 — Aft waterplane:", 3.0,
             "Controls the fullness of the stern at the waterline. Lower values taper the stern "
             "sharply to minimize wake separation drag."),
            ("f4 — Aft rocker:", 3.0,
             "Controls the upward curve of the keel line at the stern. Affects water flow "
             "separation and tracking at the rear of the hull."),
            ("f5 — Flare (0–0.75):", 0.75,
             "Controls the outward angle of the hull sides. Helps deflect waves and increases "
             "reserve buoyancy as the canoe rolls."),
            ("f6 — Tuck (0–0.75):", 0.75,
             "Controls the inward curvature of the hull at the stern near the keel. Affects "
             "how smoothly water converges behind the canoe."),
        ]
        for label, hi, tip in _sf_rows:
            sb = _dsb(0.5, 0.0, hi, 0.05, 3)
            self.sb_factors.append(sb)
            sf_form.addRow(label, self._with_info(sb, tip))
        adv_vbox.addWidget(sf_grp)

        # B. Environment & Speed
        env_grp = QGroupBox("Environment & Speed")
        env_form = QFormLayout(env_grp)
        self.sb_density   = _dsb(1025.9,  995.0, 1030.0, 0.1,   2)
        self.sb_viscosity = _dsb(1.18831, 0.5,   2.0,    0.001, 5)
        self.sb_n_ships   = NoScrollSpinBox()
        self.sb_n_ships.setRange(3, 120)
        self.sb_n_ships.setValue(40)
        self.sb_rng_seed  = NoScrollSpinBox()
        self.sb_rng_seed.setRange(0, 32000)
        self.sb_rng_seed.setValue(12345)
        # Three weighted design speed pairs
        self.sb_speed1 = _dsb(3.0, 0.0, 52.0, 0.1, 1)
        self.sb_wt1    = _dsb(1.0, 0.0, 10.0, 0.1, 2)
        self.sb_speed2 = _dsb(4.5, 0.0, 52.0, 0.1, 1)
        self.sb_wt2    = _dsb(0.5, 0.0, 10.0, 0.1, 2)
        self.sb_speed3 = _dsb(0.0, 0.0, 52.0, 0.1, 1)
        self.sb_wt3    = _dsb(0.0, 0.0, 10.0, 0.1, 2)

        _TIP_DENSITY = ("Density of the water at the race location. Default is ~998 kg/m³ for "
                        "fresh water. Determines buoyancy calculations.")
        _TIP_VISC    = ("The water's internal resistance to flow. The physics engine uses this "
                        "to calculate skin friction drag along the wetted surface.")
        _TIP_SPD     = "Target design speed for the optimizer."
        _TIP_WT      = "Importance of this speed. Set to 0 to ignore this speed tier."
        _TIP_NSHIPS  = ("The number of candidate hull variants Godzilla generates and evaluates "
                        "per evolutionary generation. Higher means a broader search, but slower "
                        "computation.")
        _TIP_RNG     = ("Determines the random number sequence used to generate new populations. "
                        "Change this to force the algorithm to explore different evolutionary paths.")

        # Density and viscosity stay in SI regardless of unit system
        env_form.addRow("Water Density (kg/m³):",        self._with_info(self.sb_density,   _TIP_DENSITY))
        env_form.addRow("Kin. Viscosity (×10⁻⁶ m²/s):", self._with_info(self.sb_viscosity, _TIP_VISC))
        env_form.addRow(QLabel(""), QLabel(""))
        self._lbl_speed1 = QLabel("Speed 1 (m/s):")
        self._lbl_speed2 = QLabel("Speed 2 (m/s):")
        self._lbl_speed3 = QLabel("Speed 3 (m/s):")
        env_form.addRow(self._lbl_speed1,    self._with_info(self.sb_speed1, _TIP_SPD))
        env_form.addRow("Weight 1:",         self._with_info(self.sb_wt1,    _TIP_WT))
        env_form.addRow(self._lbl_speed2,    self._with_info(self.sb_speed2, _TIP_SPD))
        env_form.addRow("Weight 2:",         self._with_info(self.sb_wt2,    _TIP_WT))
        env_form.addRow(self._lbl_speed3,    self._with_info(self.sb_speed3, _TIP_SPD))
        env_form.addRow("Weight 3:",         self._with_info(self.sb_wt3,    _TIP_WT))
        env_form.addRow(QLabel(""), QLabel(""))
        env_form.addRow("Population Ships:", self._with_info(self.sb_n_ships,  _TIP_NSHIPS))
        env_form.addRow("RNG Seed:",         self._with_info(self.sb_rng_seed, _TIP_RNG))

        self.objective_combo = QComboBox()
        self.objective_combo.addItem("Minimise Total Resistance (Code 3)",       3)
        self.objective_combo.addItem("Minimise Required Power (Code 13)",        13)
        self.objective_combo.addItem("Minimise Surface Area (Code 0)",           0)
        self.objective_combo.addItem("Minimise Viscous Resistance (Code 1)",     1)
        self.objective_combo.addItem("Minimise Wave Resistance (Code 2)",        2)
        self.objective_combo.addItem("Match Target Section Areas (Code 100)",    100)
        self.objective_combo.setCurrentIndex(0)
        _TIP_OBJ = ("Determines what physical property the evolutionary algorithm "
                    "will attempt to minimize.")
        env_form.addRow("Objective Function:", self._with_info(self.objective_combo, _TIP_OBJ))
        adv_vbox.addWidget(env_grp)

        # C. Internal Clearance (Paddler Box)
        box_grp = QGroupBox("Internal Clearance (Paddler Box)")
        box_form = QFormLayout(box_grp)
        self.chk_box_constraint = QCheckBox("Enable Internal Box Constraint")
        self.chk_box_constraint.setChecked(True)
        box_form.addRow(self.chk_box_constraint)
        self.sb_box_L   = _dsb(1.5, 0.0, 10.0, 0.1,  2)
        self.sb_box_B   = _dsb(0.5, 0.0,  5.0, 0.01, 3)
        self.sb_box_T   = _dsb(0.3, 0.0,  2.0, 0.01, 3)
        self.cb_box_loc = QComboBox()
        self.cb_box_loc.addItems(["Midships", "LCB", "Stern"])
        box_form.addRow("Box Length (m):", self._with_info(self.sb_box_L,
            "Minimum internal longitudinal clearance required."))
        box_form.addRow("Box Beam (m):",   self._with_info(self.sb_box_B,
            "Minimum internal width required to fit paddlers."))
        box_form.addRow("Box Draft (m):",  self._with_info(self.sb_box_T,
            "Minimum internal depth required."))
        box_form.addRow("Box Location:",   self._with_info(self.cb_box_loc,
            "Where the algorithm should attempt to fit the box."))
        adv_vbox.addWidget(box_grp)

        vbox.addWidget(self.advanced_container)
        vbox.addStretch()

        # ── Right: live seed-hull preview ──
        preview_panel = QWidget()
        preview_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        pv = QVBoxLayout(preview_panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(4)

        preview_header = QLabel("Seed Hull Preview")
        preview_header.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        preview_header.setStyleSheet("color: #00d2ff;")
        pv.addWidget(preview_header)

        self._preview_holder = QWidget()
        self._preview_holder.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        holder_lay = QVBoxLayout(self._preview_holder)
        holder_lay.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(self._preview_holder, stretch=1)

        # Orbitable Three.js viewport — same renderer as the structural
        # sweep's "Optimized Hull 3D" tab.  Falls back to the static
        # Matplotlib figure if QtWebEngine or the offline JS assets are
        # unavailable (see setup_offline_viewer.py).
        self._hull3d_view = None
        try:
            from ui.hull3d_view import Hull3DView
            self._hull3d_view = Hull3DView()
            self._hull3d_view.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._hull3d_view.setMinimumHeight(260)
            holder_lay.addWidget(self._hull3d_view)
        except Exception as exc:
            self._mw.log(
                f"⚠ 3-D seed preview unavailable ({exc}); "
                f"falling back to the static plot.")

        self.refresh_preview_btn = QPushButton("↻ Refresh Preview")
        self.refresh_preview_btn.setFixedHeight(28)
        self.refresh_preview_btn.setToolTip(
            "Re-render the seed hull from the current dimensions and shape factors.")
        self.refresh_preview_btn.clicked.connect(self._render_seed_preview)
        pv.addWidget(self.refresh_preview_btn)

        top.addWidget(preview_panel, stretch=3)

        # ── Help button — pinned above the run button ──
        self.help_btn = QPushButton("? Godzilla Help & Shortcuts")
        self.help_btn.setFixedHeight(30)
        self.help_btn.setStyleSheet(
            "QPushButton { background-color: #2c3e50; font-size: 9pt; }"
            "QPushButton:hover { background-color: #34495e; }"
        )
        self.help_btn.clicked.connect(self._show_godzilla_help)
        root.addWidget(self.help_btn)

        # ── Run button — pinned below scroll area ──
        self.run_btn = QPushButton("Run Godzilla Optimization")
        self.run_btn.setMinimumHeight(56)
        self.run_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; font-size: 14pt; font-weight: bold; }"
            "QPushButton:hover { background-color: #2ecc71; }"
            "QPushButton:disabled { background-color: #555; color: #888; }"
        )
        self.run_btn.clicked.connect(self._start_godzilla)
        root.addWidget(self.run_btn)

    # ------------------------------------------------------------------
    # Info icon helpers
    # ------------------------------------------------------------------
    def _info_icon(self, tooltip_text: str) -> QPushButton:
        icon = QPushButton('i')
        icon.setFixedSize(20, 20)
        icon.setStyleSheet('''
            QPushButton {
                background-color: #3498db;
                color: white;
                border-radius: 10px;
                font-weight: bold;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-style: italic;
                font-size: 13px;
                border: none;
                padding: 0px;
                margin: 0px;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #2980b9;
            }
        ''')
        icon.setCursor(Qt.CursorShape.PointingHandCursor)
        _html = f'<span style="font-size:9pt;">{tooltip_text}</span>'
        icon.setToolTip(_html)
        icon.clicked.connect(lambda: QToolTip.showText(QCursor.pos(), _html, icon))
        return icon

    def _with_info(self, widget: QWidget, tooltip_text: str) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(widget, stretch=1)
        row.addWidget(self._info_icon(tooltip_text))
        return container

    # ------------------------------------------------------------------
    # Unit system
    # ------------------------------------------------------------------
    def _read_si(self) -> dict:
        """Read spinbox display values and return them all in SI units."""
        f   = 1.0 if self._use_metric else 1.0 / _M_TO_FT
        fi  = 1.0 if self._use_metric else 1.0 / _M_TO_IN   # beam/draft in inches (US)
        fv  = 1.0 if self._use_metric else 1.0 / _MS_TO_KN
        fm  = 1.0 if self._use_metric else 1.0 / _KG_TO_LBS
        return {
            "L":        self.sb_L.value()           * f,
            "beam":     self.sb_beam.value()         * fi,
            "T":        self.sb_T.value()            * fi,
            "mass":     self.sb_mass.value()         * fm,
            "L_min":    self.sb_L_min.value()        * f,
            "L_max":    self.sb_L_max.value()        * f,
            "B_min":    self.sb_B_min.value()        * fi,
            "B_max":    self.sb_B_max.value()        * fi,
            "T_min":    self.sb_T_min.value()        * fi,
            "T_max":    self.sb_T_max.value()        * fi,
            "speed1":   self.sb_speed1.value() * fv,
            "wt1":      self.sb_wt1.value(),
            "speed2":   self.sb_speed2.value() * fv,
            "wt2":      self.sb_wt2.value(),
            "speed3":   self.sb_speed3.value() * fv,
            "wt3":      self.sb_wt3.value(),
            # Rocker heights + bounds and GMT bounds display in inches (US) —
            # stored back to SI metres here so the optimizer/handoff stay SI.
            "bow_rocker":       self.bow_rocker_input.value()   * fi,
            "stern_rocker":     self.stern_rocker_input.value() * fi,
            "bow_rocker_min":   self.sb_bow_rocker_min.value()   * fi,
            "bow_rocker_max":   self.sb_bow_rocker_max.value()   * fi,
            "stern_rocker_min": self.sb_stern_rocker_min.value() * fi,
            "stern_rocker_max": self.sb_stern_rocker_max.value() * fi,
            "min_gmt":          self.sb_min_gmt.value() * fi,
            "max_gmt":          self.sb_max_gmt.value() * fi,
        }

    def _apply_unit_ranges(self):
        """Update spinbox ranges, steps, and decimal places for the current unit system."""
        if self._use_metric:
            self.sb_L.setRange(1.0, 10.0);          self.sb_L.setSingleStep(0.1);    self.sb_L.setDecimals(2)
            self.sb_beam.setRange(0.1, 2.0);         self.sb_beam.setSingleStep(0.01); self.sb_beam.setDecimals(3)
            self.sb_T.setRange(0.01, 1.0);           self.sb_T.setSingleStep(0.005);  self.sb_T.setDecimals(3)
            self.sb_mass.setRange(1.0, 5000.0);      self.sb_mass.setSingleStep(0.5); self.sb_mass.setDecimals(1)
            self.sb_L_min.setRange(1.0, 15.0);       self.sb_L_min.setSingleStep(0.1); self.sb_L_min.setDecimals(2)
            self.sb_L_max.setRange(1.0, 15.0);       self.sb_L_max.setSingleStep(0.1); self.sb_L_max.setDecimals(2)
            self.sb_B_min.setRange(0.05, 3.0);       self.sb_B_min.setSingleStep(0.01); self.sb_B_min.setDecimals(3)
            self.sb_B_max.setRange(0.05, 3.0);       self.sb_B_max.setSingleStep(0.01); self.sb_B_max.setDecimals(3)
            self.sb_T_min.setRange(0.01, 1.0);       self.sb_T_min.setSingleStep(0.01); self.sb_T_min.setDecimals(3)
            self.sb_T_max.setRange(0.01, 1.0);       self.sb_T_max.setSingleStep(0.01); self.sb_T_max.setDecimals(3)
            for _sb in (self.sb_speed1, self.sb_speed2, self.sb_speed3):
                _sb.setRange(0.0, 52.0); _sb.setSingleStep(0.1); _sb.setDecimals(1)
            for _sb in (self.bow_rocker_input, self.stern_rocker_input,
                        self.sb_bow_rocker_min, self.sb_bow_rocker_max,
                        self.sb_stern_rocker_min, self.sb_stern_rocker_max):
                _sb.setRange(0.0, 0.30); _sb.setSingleStep(0.001); _sb.setDecimals(3)   # m
            self.sb_min_gmt.setRange(-10.0, 10.0);  self.sb_min_gmt.setSingleStep(0.01); self.sb_min_gmt.setDecimals(3)   # m
            self.sb_max_gmt.setRange(0.0, 100.0);   self.sb_max_gmt.setSingleStep(0.1);  self.sb_max_gmt.setDecimals(2)   # m
        else:
            self.sb_L.setRange(3.28, 32.8);           self.sb_L.setSingleStep(0.1);    self.sb_L.setDecimals(2)
            self.sb_beam.setRange(2.0, 78.8);         self.sb_beam.setSingleStep(0.25); self.sb_beam.setDecimals(2)   # inches
            self.sb_T.setRange(0.4, 39.4);            self.sb_T.setSingleStep(0.1);    self.sb_T.setDecimals(2)       # inches
            self.sb_mass.setRange(2.2, 11020.0);      self.sb_mass.setSingleStep(1.0); self.sb_mass.setDecimals(1)
            self.sb_L_min.setRange(3.28, 49.2);       self.sb_L_min.setSingleStep(0.5); self.sb_L_min.setDecimals(2)
            self.sb_L_max.setRange(3.28, 49.2);       self.sb_L_max.setSingleStep(0.5); self.sb_L_max.setDecimals(2)
            self.sb_B_min.setRange(2.0, 118.1);       self.sb_B_min.setSingleStep(0.25); self.sb_B_min.setDecimals(2) # inches
            self.sb_B_max.setRange(2.0, 118.1);       self.sb_B_max.setSingleStep(0.25); self.sb_B_max.setDecimals(2) # inches
            self.sb_T_min.setRange(0.4, 39.4);        self.sb_T_min.setSingleStep(0.1);  self.sb_T_min.setDecimals(2) # inches
            self.sb_T_max.setRange(0.4, 39.4);        self.sb_T_max.setSingleStep(0.1);  self.sb_T_max.setDecimals(2) # inches
            for _sb in (self.sb_speed1, self.sb_speed2, self.sb_speed3):
                _sb.setRange(0.0, 101.0); _sb.setSingleStep(0.5); _sb.setDecimals(1)
            for _sb in (self.bow_rocker_input, self.stern_rocker_input,
                        self.sb_bow_rocker_min, self.sb_bow_rocker_max,
                        self.sb_stern_rocker_min, self.sb_stern_rocker_max):
                _sb.setRange(0.0, 11.81); _sb.setSingleStep(0.05); _sb.setDecimals(2)   # inches
            self.sb_min_gmt.setRange(-394.0, 394.0); self.sb_min_gmt.setSingleStep(0.1); self.sb_min_gmt.setDecimals(2)   # inches
            self.sb_max_gmt.setRange(0.0, 3937.0);   self.sb_max_gmt.setSingleStep(1.0); self.sb_max_gmt.setDecimals(1)   # inches

    def _populate_from_si(self, si: dict):
        """Set spinbox display values from a dict of SI values."""
        f   = 1.0 if self._use_metric else _M_TO_FT
        fi  = 1.0 if self._use_metric else _M_TO_IN   # beam/draft in inches (US)
        fv  = 1.0 if self._use_metric else _MS_TO_KN
        fm  = 1.0 if self._use_metric else _KG_TO_LBS
        self.sb_L.setValue(si["L"]        * f)
        self.sb_beam.setValue(si["beam"]  * fi)
        self.sb_T.setValue(si["T"]        * fi)
        self.sb_mass.setValue(si["mass"]  * fm)
        self.sb_L_min.setValue(si["L_min"] * f)
        self.sb_L_max.setValue(si["L_max"] * f)
        self.sb_B_min.setValue(si["B_min"] * fi)
        self.sb_B_max.setValue(si["B_max"] * fi)
        self.sb_T_min.setValue(si["T_min"] * fi)
        self.sb_T_max.setValue(si["T_max"] * fi)
        self.sb_speed1.setValue(si["speed1"] * fv)
        self.sb_speed2.setValue(si["speed2"] * fv)
        self.sb_speed3.setValue(si["speed3"] * fv)
        # Rocker + GMT (SI metres → inches in US, metres in Metric)
        if "bow_rocker" in si:
            self.bow_rocker_input.setValue(si["bow_rocker"]     * fi)
            self.stern_rocker_input.setValue(si["stern_rocker"] * fi)
            self.sb_bow_rocker_min.setValue(si["bow_rocker_min"]     * fi)
            self.sb_bow_rocker_max.setValue(si["bow_rocker_max"]     * fi)
            self.sb_stern_rocker_min.setValue(si["stern_rocker_min"] * fi)
            self.sb_stern_rocker_max.setValue(si["stern_rocker_max"] * fi)
            self.sb_min_gmt.setValue(si["min_gmt"] * fi)
            self.sb_max_gmt.setValue(si["max_gmt"] * fi)

    def _refresh_labels(self):
        """Update form row labels to match the active unit system."""
        if self._use_metric:
            self._lbl_L.setText("Length (m):")
            self._lbl_beam.setText("Beam (m):")
            self._lbl_T.setText("Draft (m):")
            self._lbl_mass.setText("Displacement Mass (kg):")
            self._lbl_speed1.setText("Speed 1 (m/s):")
            self._lbl_speed2.setText("Speed 2 (m/s):")
            self._lbl_speed3.setText("Speed 3 (m/s):")
            self._lbl_L_min.setText("Min Length (m):")
            self._lbl_L_max.setText("Max Length (m):")
            self._lbl_B_min.setText("Min Beam (m):")
            self._lbl_B_max.setText("Max Beam (m):")
            self._lbl_T_min.setText("Min Draft (m):")
            self._lbl_T_max.setText("Max Draft (m):")
            self._lbl_bow_rocker.setText("Bow Rocker (m):")
            self._lbl_bow_rocker_min.setText("Min Bow Rocker (m):")
            self._lbl_bow_rocker_max.setText("Max Bow Rocker (m):")
            self._lbl_stern_rocker.setText("Stern Rocker (m):")
            self._lbl_stern_rocker_min.setText("Min Stern Rocker (m):")
            self._lbl_stern_rocker_max.setText("Max Stern Rocker (m):")
            self._lbl_min_gmt.setText("Min GMT (m):")
            self._lbl_max_gmt.setText("Max GMT (m):")
        else:
            self._lbl_L.setText("Length (ft):")
            self._lbl_beam.setText("Beam (in):")
            self._lbl_T.setText("Draft (in):")
            self._lbl_mass.setText("Displacement Mass (lbs):")
            self._lbl_speed1.setText("Speed 1 (kn):")
            self._lbl_speed2.setText("Speed 2 (kn):")
            self._lbl_speed3.setText("Speed 3 (kn):")
            self._lbl_L_min.setText("Min Length (ft):")
            self._lbl_L_max.setText("Max Length (ft):")
            self._lbl_B_min.setText("Min Beam (in):")
            self._lbl_B_max.setText("Max Beam (in):")
            self._lbl_T_min.setText("Min Draft (in):")
            self._lbl_T_max.setText("Max Draft (in):")
            self._lbl_bow_rocker.setText("Bow Rocker (in):")
            self._lbl_bow_rocker_min.setText("Min Bow Rocker (in):")
            self._lbl_bow_rocker_max.setText("Max Bow Rocker (in):")
            self._lbl_stern_rocker.setText("Stern Rocker (in):")
            self._lbl_stern_rocker_min.setText("Min Stern Rocker (in):")
            self._lbl_stern_rocker_max.setText("Max Stern Rocker (in):")
            self._lbl_min_gmt.setText("Min GMT (in):")
            self._lbl_max_gmt.setText("Max GMT (in):")

    def _publish_min_gmt(self, *_):
        """Mirror the Min-GMT input onto the config singleton (in inches) so the
        structural sweep gates transverse stability against it.  Read through
        _read_si() so the value is correct in either unit system; 0 disables the
        gate."""
        try:
            import config
            config.MIN_GMT_IN = float(self._read_si().get("min_gmt", 0.0)) * _M_TO_IN
        except Exception:
            pass

    def _on_unit_changed(self, idx: int):
        new_metric = (idx == 0)
        if new_metric == self._use_metric:
            return
        try:
            si = self._read_si()          # capture before changing flag
            self._use_metric = new_metric
            self._apply_unit_ranges()
            self._populate_from_si(si)
            self._refresh_labels()
            self.update()
        except Exception:
            import traceback
            self._mw.log(f"⚠ Unit switch error:\n{traceback.format_exc()}")

    # ------------------------------------------------------------------
    # Seed preview
    # ------------------------------------------------------------------
    def _connect_preview_signals(self):
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(400)
        self._preview_timer.timeout.connect(self._render_seed_preview)

        shape_inputs = [self.sb_L, self.sb_beam, self.sb_T, *self.sb_factors]
        for sb in shape_inputs:
            sb.valueChanged.connect(lambda *_: self._preview_timer.start())

    def _embed_preview(self, fig):
        lay = self._preview_holder.layout()
        if self._preview_canvas is not None:
            lay.removeWidget(self._preview_canvas)
            self._preview_canvas.deleteLater()
        canvas = FigureCanvasQTAgg(fig)
        from ui.chart_export import enable_chart_export
        enable_chart_export(canvas)
        canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(canvas)
        canvas.draw()
        self._preview_canvas = canvas

    def _render_hull(self, offsets, L, T, title):
        """Render a hull into the preview panel, 3-D if available."""
        if self._hull3d_view is not None:
            self._hull3d_view.set_hull(offsets, L, T, title=title)
        else:
            from visualization import plot_hull_3d
            self._embed_preview(plot_hull_3d(offsets, L, T, title=title))

    def _render_seed_preview(self):
        try:
            from hull_generator import generate_hull

            si = self._read_si()
            L, B, T = si["L"], si["beam"], si["T"]
            factors = [sb.value() for sb in self.sb_factors]
            offsets = generate_hull(factors, L, B, T)
            self._render_hull(
                offsets, L, T,
                title=f"Seed Hull  (L={L:.2f}m  B={B:.3f}m  T={T:.3f}m)")
        except Exception as exc:
            self._mw.log(f"⚠ Hull preview error: {exc}")

    def show_hull_preview(self, offsets, L, T, title="Hull Geometry"):
        """Public hook: push an external hull render into this tab's preview panel."""
        self._render_hull(offsets, L, T, title)

    def _toggle_advanced(self, checked: bool):
        self.advanced_container.setVisible(checked)
        self.toggle_btn.setText(
            "▼  Hide Advanced Settings" if checked else "▶  Show Advanced Settings"
        )

    # ------------------------------------------------------------------
    # Save / Load profile
    # ------------------------------------------------------------------
    def _get_widget_map(self) -> dict:
        """Map JSON keys to their UI widgets (unit-independent inputs only).

        Dimensional spinboxes (L, beam, T, bounds, speeds) are handled
        separately via _read_si() / _populate_from_si() so that profiles
        remain valid regardless of the active unit system.
        """
        return {
            # Shape factor seeds
            "f0": self.sb_factors[0],
            "f1": self.sb_factors[1],
            "f2": self.sb_factors[2],
            "f3": self.sb_factors[3],
            "f4": self.sb_factors[4],
            "f5": self.sb_factors[5],
            "f6": self.sb_factors[6],
            # Speed weightings (dimensionless — no unit conversion needed)
            "wt1": self.sb_wt1,
            "wt2": self.sb_wt2,
            "wt3": self.sb_wt3,
            # Environment (always SI)
            "water_density":       self.sb_density,
            "kinematic_viscosity": self.sb_viscosity,
            "n_ships":             self.sb_n_ships,
            "rng_seed":            self.sb_rng_seed,
            # Rocker heights/bounds and GMT bounds are DIMENSIONAL: handled via
            # _read_si()/_populate_from_si() (inches in US, metres in Metric),
            # not here, so they convert with the unit toggle like L/beam/draft.
            # Internal paddler box
            "box_enabled": self.chk_box_constraint,
            "box_L":       self.sb_box_L,
            "box_B":       self.sb_box_B,
            "box_T":       self.sb_box_T,
            # Objective function selector (integer code stored as userData)
            "objective_code": self.objective_combo,
        }

    def _get_ui_state(self) -> dict:
        """Snapshot the full UI into a serialisable dict (all dimensions in SI)."""
        # Dimensional values via _read_si() so they are always in SI
        state = dict(self._read_si())
        # Unit-independent widgets
        for key, widget in self._get_widget_map().items():
            if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                state[key] = widget.value()
            elif isinstance(widget, QCheckBox):
                state[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                state[key] = widget.currentData()
        # Box location uses display text (no integer userData on this combo)
        state["box_location"] = self.cb_box_loc.currentText()
        return state

    def _set_ui_state(self, state: dict):
        """Restore the UI from a previously saved state dict."""
        # Dimensional values — route through _populate_from_si() so the active
        # unit system is respected (SI → current display units on the way in)
        _si_keys = {
            "L", "beam", "T", "mass",
            "L_min", "L_max", "B_min", "B_max", "T_min", "T_max",
            "speed1", "speed2", "speed3",
            "bow_rocker", "stern_rocker",
            "bow_rocker_min", "bow_rocker_max",
            "stern_rocker_min", "stern_rocker_max",
            "min_gmt", "max_gmt",
        }
        si = {k: state[k] for k in _si_keys if k in state}
        if si:
            self._populate_from_si(si)
        # Unit-independent widgets
        widget_map = self._get_widget_map()
        for key, value in state.items():
            if key not in widget_map:
                continue
            widget = widget_map[key]
            if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                widget.setValue(value)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                idx = widget.findData(value)
                if idx >= 0:
                    widget.setCurrentIndex(idx)
        # Box location uses text matching
        if "box_location" in state:
            idx = self.cb_box_loc.findText(state["box_location"])
            if idx >= 0:
                self.cb_box_loc.setCurrentIndex(idx)

    def _save_profile(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Hull Profile", _PROJECT_ROOT,
            "Hull Profile (*.json);;All Files (*.*)")
        if not path:
            return
        try:
            state = self._get_ui_state()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            self._mw.log(f"✅ Hull profile saved: {os.path.basename(path)}")
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))

    def _load_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Hull Profile", _PROJECT_ROOT,
            "Hull Profile (*.json);;All Files (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self._set_ui_state(state)
            self._render_seed_preview()
            self._mw.log(f"✅ Hull profile loaded: {os.path.basename(path)}")
        except Exception as exc:
            QMessageBox.critical(self, "Load Failed", str(exc))

    # ------------------------------------------------------------------
    # Godzilla launch
    # ------------------------------------------------------------------
    def _collect_params(self) -> dict:
        si      = self._read_si()
        density = self.sb_density.value()       # always kg/m³
        vol_m3  = si["mass"] / density          # mass (kg) / density → volume (m³) for Godzilla
        return {
            "filename":            _IN_MLT,
            "L":                   si["L"],
            "beam":                si["beam"],
            "T":                   si["T"],
            "vol":                 vol_m3,
            "factors":             [sb.value() for sb in self.sb_factors],
            "water_density":       density,
            "kinematic_viscosity": self.sb_viscosity.value(),
            "design_speeds":       [si["speed1"], si["speed2"], si["speed3"]],
            "design_weights":      [si["wt1"],    si["wt2"],    si["wt3"]],
            "n_ships":             self.sb_n_ships.value(),
            "rng_seed":            self.sb_rng_seed.value(),
            "box_enabled":         self.chk_box_constraint.isChecked(),
            "box_L":               self.sb_box_L.value(),
            "box_B":               self.sb_box_B.value(),
            "box_T":               self.sb_box_T.value(),
            "box_location":        self.cb_box_loc.currentText(),
            "L_min":               si["L_min"],
            "L_max":               si["L_max"],
            "beam_min":            si["B_min"],
            "beam_max":            si["B_max"],
            "T_min":               si["T_min"],
            "T_max":               si["T_max"],
            "total_mass_kg":       si["mass"],
            "min_gmt":             si["min_gmt"],   # SI metres (display may be inches)
            "max_gmt":             si["max_gmt"],
            "objective_code":      self.objective_combo.currentData(),
        }

    def _start_godzilla(self):
        from services.godzilla_io import generate_godzilla_input
        from services.hull_service import HullService

        params = self._collect_params()
        mw = self._mw
        mw.log(
            f"[Hull Configurator] Writing in.mlt — "
            f"L={params['L']:.2f} m, B={params['beam']:.3f} m, "
            f"T={params['T']:.3f} m, Mass≈{params['vol']*params['water_density']:.1f} kg "
            f"(Vol={params['vol']:.4f} m³)"
        )

        def _task(worker=None, **_):
            generate_godzilla_input(**params)
            stop_ev = worker.stop_event if worker else None
            ok = HullService().launch_godzilla(cwd=_PROJECT_ROOT, stop_event=stop_ev)
            out_path = os.path.join(_PROJECT_ROOT, "out.mlt")
            if ok:
                # Record seed + Godzilla run in the persistent design state so
                # the structural and export steps know their inputs' provenance.
                try:
                    from data.session import load_design_state, save_design_state
                    from geometry import parse_godzilla_dimensions
                    ds = load_design_state()
                    ds.mark_seed(params["factors"],
                                 params["L"], params["beam"], params["T"])
                    g_L_ft, g_draft_in = parse_godzilla_dimensions(out_path)
                    ds.mark_godzilla(
                        g_L_ft, g_draft_in,
                        os.path.join(_PROJECT_ROOT, "off1.mlt"), out_path)
                    save_design_state(ds)
                except Exception as exc:
                    print(f"[DesignState] save skipped: {exc}")
            if os.path.exists(out_path):
                QTimer.singleShot(
                    0, lambda p=out_path: mw.struct_tab.set_active_hull_file(p))

        mw._launch_worker(_task)

    def _show_godzilla_help(self):
        from ui.godzilla_help import GodzillaHelpDialog
        GodzillaHelpDialog(self).exec()

    def _set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
