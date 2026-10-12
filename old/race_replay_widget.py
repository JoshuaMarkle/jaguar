import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox,
    QSlider, QLabel
)
from PyQt6.QtCore import Qt, QTimer
from race_course import get_course

class RaceReplayWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.telemetry_data = {}  # dict of course_name -> np.ndarray
        self.path_analyses = {}   # dict of course_name -> analyse_course() result (has path_xy)
        self.current_course = None
        self.current_telem = None
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_timer)
        
        # Playback state
        self.frame_idx = 0
        self.is_playing = False
        
        self._init_ui()
        
    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # ── Toolbar ──
        toolbar = QHBoxLayout()
        
        self.course_combo = QComboBox()
        self.course_combo.currentIndexChanged.connect(self._on_course_changed)
        toolbar.addWidget(QLabel("Course:"))
        toolbar.addWidget(self.course_combo)
        
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._toggle_play)
        toolbar.addWidget(self.play_btn)
        
        self.reset_btn = QPushButton("⏪ Reset")
        self.reset_btn.clicked.connect(self._reset)
        toolbar.addWidget(self.reset_btn)
        
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1.0x", "2.0x", "5.0x", "10.0x", "20.0x", "50.0x"])
        self.speed_combo.setCurrentIndex(2)  # 5.0x default
        toolbar.addWidget(QLabel("Speed:"))
        toolbar.addWidget(self.speed_combo)
        
        toolbar.addStretch()
        layout.addLayout(toolbar)
        
        # ── Matplotlib Canvas (course overhead only) ──
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        self.fig = plt.figure(figsize=(9, 4), facecolor="#1a1a2e")
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.fig)
        try:
            from ui.chart_export import enable_chart_export
            enable_chart_export(self.canvas)
        except ImportError:
            pass    # standalone use without the ui package
        layout.addWidget(self.canvas, stretch=1)

        self.ax.set_facecolor("#1a1a2e")
        self.ax.tick_params(colors="gray", labelsize=7)
        for spine in self.ax.spines.values():
            spine.set_color("#2a2a4a")
        self.ax.set_aspect('equal')

        self.trail_line, = self.ax.plot([], [], 'c-', alpha=0.6, linewidth=1.5)
        from matplotlib.patches import Polygon as MplPolygon
        self.canoe_poly = MplPolygon(np.zeros((3, 2)), color='#00d2ff', zorder=10)
        self.ax.add_patch(self.canoe_poly)
        
        # ── Timeline & Status ──
        bottom_bar = QHBoxLayout()
        
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.sliderPressed.connect(self._pause)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        bottom_bar.addWidget(self.slider)
        
        self.status_lbl = QLabel("T: 0.0s | V: 0.0 m/s")
        self.status_lbl.setFixedWidth(150)
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bottom_bar.addWidget(self.status_lbl)
        
        layout.addLayout(bottom_bar)
        
    def load_telemetry(self, telemetry_dict):
        """Load telemetry dictionary {course_name: telemetry_array}."""
        self.telemetry_data = telemetry_dict or {}

        self.course_combo.blockSignals(True)
        self.course_combo.clear()
        for cname in self.telemetry_data.keys():
            self.course_combo.addItem(cname)
        self.course_combo.blockSignals(False)

        if self.telemetry_data:
            # Force call since index might already be 0 and setCurrentIndex(0) wouldn't emit
            self.course_combo.setCurrentIndex(0)
            self._on_course_changed()
        else:
            self.status_lbl.setText("No telemetry data")
            
    def load_path_analyses(self, path_analyses_dict):
        """Load racing-line data keyed by the same course display names as telemetry."""
        self.path_analyses = path_analyses_dict or {}
        if self.current_course:
            self._draw_arena()
            self._update_frame()

    def _on_course_changed(self):
        cname = self.course_combo.currentText()
        if not cname or cname not in self.telemetry_data:
            return

        self._pause()
        self.current_course = cname
        self.current_telem = self.telemetry_data[cname]

        self.slider.setRange(0, max(0, len(self.current_telem) - 1))
        self.frame_idx = 0
        self.slider.setValue(0)

        self._draw_arena()
        self._update_frame()
        
    def _draw_arena(self):
        self.ax.clear()
        self.ax.set_facecolor("#1a1a2e")
        self.ax.set_aspect('equal')
        self.ax.tick_params(colors="gray", labelsize=7)
        for spine in self.ax.spines.values():
            spine.set_color('#2a2a4a')

        # Re-initialize dynamic course elements
        self.trail_line, = self.ax.plot([], [], 'c-', alpha=0.6, linewidth=1.5)
        from matplotlib.patches import Polygon as MplPolygon
        self.canoe_poly = MplPolygon(np.zeros((3, 2)), color='#00d2ff', zorder=10)
        self.ax.add_patch(self.canoe_poly)

        if not self.current_course:
            self.canvas.draw_idle()
            return

        try:
            course = get_course(self.current_course)
            xs, ys = [], []
            max_x = max((wp.x for wp in course.waypoints), default=1.0)
            for wp in course.waypoints:
                xs.append(wp.x)
                ys.append(wp.y)
                if wp.turn_radius > 0 and wp.x > max_x * 0.15:
                    from matplotlib.patches import Circle
                    plot_x = wp.x + wp.turn_radius if wp.turn_angle >= 180 else wp.x
                    circle = Circle((plot_x, wp.y), 1.0, color='red', alpha=0.7, zorder=5)
                    self.ax.add_patch(circle)

            # waypoint connector line intentionally hidden
            margin = 10.0
            self.ax.set_xlim(min(xs) - margin, max(xs) + margin)
            self.ax.set_ylim(min(ys) - margin, max(ys) + margin)

            # Overlay the computed racing line if available
            pa = self.path_analyses.get(self.current_course)
            if pa is not None:
                path_xy = pa.get("path_xy")
                if path_xy is not None and len(path_xy) > 1:
                    self.ax.plot(
                        path_xy[:, 0], path_xy[:, 1],
                        color='#f39c12', linewidth=2.0, alpha=0.75,
                        zorder=3, label="Racing line",
                    )
                pct_s = pa.get("pct_straight", 0)
                pct_t = pa.get("pct_turn", 0)
                title = (
                    f"{course.name}  —  "
                    f"{pct_s:.0f}% straight / {pct_t:.0f}% turn"
                )
            else:
                title = f"{course.name} Course"

            self.ax.set_title(title, color="white", fontsize=8)
            _handles, _labels = self.ax.get_legend_handles_labels()
            if _handles:
                self.ax.legend(_handles, _labels, fontsize=6, facecolor="#222",
                               labelcolor="white", framealpha=0.7, loc="upper right")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("_draw_arena error: %s", e)

        self.canvas.draw_idle()
        
    def _update_frame(self):
        if self.current_telem is None or len(self.current_telem) == 0:
            return
            
        idx = self.frame_idx
        row = self.current_telem[idx]
        t, x, y, heading, v = row
        
        # Update trail
        # Show trail from start to current
        self.trail_line.set_data(self.current_telem[:idx+1, 1], self.current_telem[:idx+1, 2])
        
        # Update canoe shape (a triangle representing the canoe)
        length = 6.0
        width = 1.0
        
        # Canoe coordinates in local frame
        # Nose at (length/2, 0), back corners at (-length/2, width/2), (-length/2, -width/2)
        local_pts = np.array([
            [length/2, 0],
            [-length/2, width/2],
            [-length/2, -width/2]
        ])
        
        # Rotate by heading
        c, s = np.cos(heading), np.sin(heading)
        rot_matrix = np.array([[c, -s], [s, c]])
        
        world_pts = np.dot(local_pts, rot_matrix.T) + np.array([x, y])
        self.canoe_poly.set_xy(world_pts)
        
        # Update status
        self.status_lbl.setText(f"T: {t:.1f}s | V: {v:.1f} m/s")

        # Update slider without triggering signals
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)

        self.canvas.draw_idle()
        
    def _toggle_play(self):
        if self.is_playing:
            self._pause()
        else:
            self._play()
            
    def _play(self):
        if self.current_telem is None:
            return
        if self.frame_idx >= len(self.current_telem) - 1:
            self.frame_idx = 0
            
        self.is_playing = True
        self.play_btn.setText("⏸ Pause")
        self.timer.start(33) # ~30fps
        
    def _pause(self):
        self.is_playing = False
        self.play_btn.setText("▶ Play")
        self.timer.stop()
        
    def _reset(self):
        self._pause()
        self.frame_idx = 0
        self._update_frame()
        
    def _on_slider_moved(self, value):
        self.frame_idx = value
        self._update_frame()
        
    def _on_timer(self):
        if self.current_telem is None:
            return
            
        # Get speed multiplier
        speed_str = self.speed_combo.currentText()
        speed_mult = float(speed_str.replace("x", ""))
        
        # Telemetry is stored at 0.05s intervals (dt=0.05).
        # Timer fires every 33ms (~0.033s).
        # So frames to jump = (0.033 * speed_mult) / 0.05
        frames_to_jump = max(1, int((0.033 * speed_mult) / 0.05))
        
        self.frame_idx += frames_to_jump
        if self.frame_idx >= len(self.current_telem):
            self.frame_idx = len(self.current_telem) - 1
            self._update_frame()
            self._pause()
        else:
            self._update_frame()
