# ui/help_panel.py
"""
Floating "What does this do?" help panel.

Reads Markdown files from docs/help/ and renders them as formatted HTML
inside a non-modal QDialog.  One panel instance is shared across all tabs
(call show_help(topic) to switch topic without closing and reopening).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextBrowser, QComboBox,
)

_HELP_DIR = Path(__file__).parent.parent / "docs" / "help"

_TOPICS: dict[str, str] = {
    "Race Simulator":        "race_simulator.md",
    "Structural Sweep":      "structural_sweep.md",
    "ASCE Compliance":       "compliance.md",
    "Mix Lab":               "mix_lab.md",
}


def _md_to_html(md: str) -> str:
    """Minimal Markdown → HTML converter (headings, bold, tables, lists)."""
    lines = md.splitlines()
    html_parts: list[str] = ["<html><body style='font-family:Segoe UI,sans-serif;font-size:11pt;'>"]
    in_table = False
    in_list = False

    for line in lines:
        # Heading
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            if in_table: html_parts.append("</table>"); in_table = False
            if in_list:  html_parts.append("</ul>"); in_list = False
            lvl = len(m.group(1))
            sizes = {1: "16pt", 2: "13pt", 3: "11pt"}
            color = {1: "#00d2ff", 2: "#0077aa", 3: "#555"}
            html_parts.append(
                f"<p style='font-size:{sizes[lvl]};font-weight:bold;"
                f"color:{color[lvl]};margin-top:12px'>{m.group(2)}</p>")
            continue

        # Table row
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.match(r"^[-: ]+$", c) for c in cells):
                continue  # separator row
            if not in_table:
                html_parts.append("<table border='0' cellpadding='4' cellspacing='0' "
                                  "style='border-collapse:collapse;width:100%'>")
                in_table = True
            row = "".join(f"<td style='border-bottom:1px solid #ccc'>{c}</td>"
                          for c in cells)
            html_parts.append(f"<tr>{row}</tr>")
            continue

        if in_table:
            html_parts.append("</table>")
            in_table = False

        # List item
        if re.match(r"^[-*]\s", line):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            item = line[2:]
            item = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", item)
            html_parts.append(f"<li>{item}</li>")
            continue

        if in_list:
            html_parts.append("</ul>")
            in_list = False

        # Blank line
        if not line.strip():
            html_parts.append("<br/>")
            continue

        # Normal paragraph
        para = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", line)
        para = re.sub(r"`(.*?)`", r"<code style='background:#f0f0f0;padding:1px 3px'>\1</code>", para)
        html_parts.append(f"<p style='margin:2px 0'>{para}</p>")

    if in_table: html_parts.append("</table>")
    if in_list:  html_parts.append("</ul>")
    html_parts.append("</body></html>")
    return "\n".join(html_parts)


class HelpPanel(QDialog):
    """Non-modal help window shared across all tabs."""

    _instance: "HelpPanel | None" = None

    @classmethod
    def show_help(cls, topic: str, parent=None) -> "HelpPanel":
        """Show or raise the help panel, switching to the requested topic."""
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent)
        panel = cls._instance
        panel.set_topic(topic)
        panel.show()
        panel.raise_()
        panel.activateWindow()
        return panel

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Help")
        self.resize(540, 580)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Topic selector ──
        top = QHBoxLayout()
        top.addWidget(QLabel("Topic:"))
        self._combo = QComboBox()
        self._combo.addItems(list(_TOPICS.keys()))
        self._combo.currentTextChanged.connect(self.set_topic)
        top.addWidget(self._combo, stretch=1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.hide)
        top.addWidget(close_btn)
        root.addLayout(top)

        # ── Content browser ──
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setFont(QFont("Segoe UI", 10))
        root.addWidget(self._browser, stretch=1)

    def set_topic(self, topic: str):
        """Switch the displayed topic."""
        idx = self._combo.findText(topic)
        if idx >= 0:
            self._combo.blockSignals(True)
            self._combo.setCurrentIndex(idx)
            self._combo.blockSignals(False)

        fname = _TOPICS.get(topic)
        if fname is None:
            self._browser.setHtml("<p>Topic not found.</p>")
            return
        path = _HELP_DIR / fname
        if not path.exists():
            self._browser.setHtml(f"<p>Help file not found: {fname}</p>")
            return
        md = path.read_text(encoding="utf-8")
        self._browser.setHtml(_md_to_html(md))
        self._browser.moveCursor(self._browser.textCursor().MoveOperation.Start)
