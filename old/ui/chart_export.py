# ui/chart_export.py
"""
Shared "Save chart…" support for every embedded matplotlib canvas
(fix plan Phase 4: make all graphs exportable).

Usage — one call wherever a FigureCanvasQTAgg is created:

    from ui.chart_export import enable_chart_export
    canvas = FigureCanvasQTAgg(fig)
    enable_chart_export(canvas)

Adds a right-click context menu with a "Save chart as…" action offering
PNG (raster, 300 dpi), SVG (vector) and PDF output.
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QMenu

_FILTERS = ("PNG image (*.png);;SVG vector (*.svg);;"
            "PDF document (*.pdf);;All files (*.*)")


def enable_chart_export(canvas):
    """Attach the right-click export menu to a matplotlib Qt canvas."""
    canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def _show_menu(pos):
        menu = QMenu(canvas)
        act = menu.addAction("Save chart as…  (PNG / SVG / PDF)")
        if menu.exec(canvas.mapToGlobal(pos)) is act:
            _save(canvas)

    canvas.customContextMenuRequested.connect(_show_menu)
    return canvas


def _save(canvas):
    path, _ = QFileDialog.getSaveFileName(
        canvas, "Save Chart", "chart.png", _FILTERS)
    if not path:
        return
    try:
        ext = os.path.splitext(path)[1].lower()
        kwargs = {"bbox_inches": "tight",
                  "facecolor": canvas.figure.get_facecolor()}
        if ext in ("", ".png"):
            kwargs["dpi"] = 300
            if ext == "":
                path += ".png"
        canvas.figure.savefig(path, **kwargs)
    except Exception as exc:                      # keep the UI alive
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(canvas, "Save Chart",
                            f"Could not save the chart:\n{exc}")
