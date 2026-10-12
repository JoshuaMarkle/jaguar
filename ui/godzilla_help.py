# ui/godzilla_help.py
"""Popup help dialog for the Godzilla physics engine keyboard shortcuts."""
from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QPushButton
from PyQt6.QtCore import Qt


class GodzillaHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Godzilla Physics Engine — Help & Shortcuts")
        self.setMinimumSize(550, 500)

        layout = QVBoxLayout(self)

        browser = QTextBrowser()
        browser.setHtml('''
        <div style="font-family: 'Segoe UI', Arial, sans-serif; padding: 10px;">
            <h2 style="color: #3498db; border-bottom: 1px solid #555; padding-bottom: 5px;">Godzilla Keyboard Shortcuts</h2>
            <p>Ensure the legacy Godzilla DOS window is selected/focused before using these shortcuts.</p>

            <h3 style="color: #2ecc71; margin-top: 20px;">&#9654; Execution Controls</h3>
            <ul style="line-height: 1.5;">
                <li><b>g</b> &nbsp;&#8212; Run Godzilla for a single step (or to initialize). The panel cycles through <i>INITIALISING</i>, <i>GODZILLA STEPPING</i>, and <i>GODZILLA IDLE</i>.</li>
                <li><b>G</b> &nbsp;&#8212; Run Godzilla continuously.</li>
                <li><b>[Any Key]</b> (e.g., Space) &#8212; Interrupt and pause Godzilla when running continuously. It will finish evaluating the current population before idling.</li>
                <li><b>Ctrl + Alt + Delete</b> &#8212; Immediately abort and force-quit the engine.</li>
            </ul>

            <h3 style="color: #2ecc71; margin-top: 20px;">&#128065; Views &amp; Visualizations</h3>
            <ul style="line-height: 1.5;">
                <li><b>p</b> &nbsp;&#8212; Open the population screen. Displays fitness of all hulls, highlights the best hull in <span style="color: #2ecc71;">green</span>, and flags constrained hulls with <i>Violated</i>.</li>
                <li><b>h</b> &nbsp;&#8212; View the waterlines for <b>all</b> hulls in the population.</li>
                <li><b>H</b> &nbsp;&#8212; View the waterlines for the <b>current</b> hull only.</li>
                <li><b>v</b> &nbsp;&#8212; View the sections for <b>all</b> hulls in the population.</li>
                <li><b>V</b> &nbsp;&#8212; View the sections for the <b>current</b> hull only.</li>
            </ul>

            <h3 style="color: #2ecc71; margin-top: 20px;">&#129517; General Navigation</h3>
            <ul style="line-height: 1.5;">
                <li><b>?</b> &nbsp;&#8212; View the help screen showing options available in GODZILLA mode.</li>
                <li><b>Esc</b> &#8212; Return to the main Lines Drawing screen from the help menu.</li>
            </ul>
        </div>
        ''')
        layout.addWidget(browser)

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(30)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
