# ui/fit_guard_dialog.py
"""
The strength-fit sanity guard's one interactive moment.

``MixDatabase`` blocks a physically implausible fit by default — everywhere,
including headless runs, the CLI and the tests.  This dialog is the ONLY thing
that can release one, and it exists because the person in front of the screen
may know something the data does not (a mis-typed row they are about to fix, a
specimen geometry the sheet records wrongly).

Three rules shape it:

* REJECT is the default.  It is the focused button, Enter takes it, Escape
  takes it, and closing the window takes it.  Overriding has to be a
  deliberate act of clicking the other button.
* The findings are stated with THIS run's numbers in them.  "Implausible fit"
  tells nobody anything; "f_r at 64 pcf is 6.9x f'c" lets them judge.
* An override leaves a trace.  ``set_fit_override`` records who released it,
  and the banner, the report and the saved design state all carry it
  afterwards, so a design built on an overridden fit can never be mistaken for
  one built on a clean fit.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QFrame,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont


_FALLBACK = {
    "fr": ("the bending gate falls back to the direct-tensile f_t, which is "
           "conservative (f_t &le; f_r for concrete)"),
    "fc": ("f'c falls back to the suite's empirical density relation"),
    "ft": ("f_t falls back to the empirical relation (0.20 &middot; f'c)"),
}


def ask_fit_override(parent, db, *, context: str = "this run") -> bool:
    """Ask whether to release the blocked strength fit(s).

    Returns True if the user chose OVERRIDE (and the override has been recorded
    on ``db``), False otherwise.  Either way the caller should proceed: REJECT
    blocks the FIT, not the run — the tier simply falls back.

    With no blocking findings this returns False without showing anything.
    """
    findings = db.blocking_findings()
    if not findings:
        return False

    dlg = QDialog(parent)
    dlg.setWindowTitle("Strength fit rejected — physically implausible")
    dlg.setModal(True)
    dlg.setMinimumWidth(680)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(10)

    head = QLabel("The Mix Lab regression contains a fit that contradicts how "
                  "concrete behaves.\nIt has been held out of force.")
    head.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
    head.setWordWrap(True)
    lay.addWidget(head)

    body = QTextEdit()
    body.setReadOnly(True)
    body.setFont(QFont("Segoe UI", 9))
    body.setMinimumHeight(200)
    html = []
    for f in findings:
        html.append(f"<p><b>{f.headline}</b><br>{f.detail}")
        if f.row_ids:
            ids = ", ".join(str(i) for i in f.row_ids)
            html.append(f"<br><i>Lab result id(s): {ids}</i> — find these rows "
                        f"in the Lab Results tab and in the team's sheet.")
        html.append("</p>")
    tiers = sorted({f.tier for f in findings})
    html.append("<p><b>If you reject:</b> "
                + "; ".join(_FALLBACK.get(t, "that tier falls back") for t in tiers)
                + ". The run proceeds on the fallback.</p>")
    html.append("<p><b>If you override:</b> the fit becomes the tier the sweep "
                "gates on, and the override is recorded — the sweep banner, "
                "the report and the saved design state will all say this design "
                "was built on a rejected fit.</p>")
    body.setHtml("".join(html))
    lay.addWidget(body)

    rule = QFrame(); rule.setFrameShape(QFrame.Shape.HLine)
    rule.setStyleSheet("color:#444;")
    lay.addWidget(rule)

    row = QHBoxLayout()
    override_btn = QPushButton("Override — use the fit anyway")
    override_btn.setFixedHeight(30)
    override_btn.setToolTip(
        "Release the fit and gate the design on it.\n"
        "Recorded in the banner, the report and the design state.")
    # Secondary on purpose: flat, muted, not the default and not focused.
    override_btn.setStyleSheet(
        "QPushButton { background-color: #3a3a3a; color: #bbb;"
        " border: 1px solid #555; padding: 4px 12px; }"
        "QPushButton:hover { background-color: #4a4a4a; color: #ddd; }")
    override_btn.setAutoDefault(False)
    override_btn.setDefault(False)
    row.addWidget(override_btn)

    row.addStretch()

    reject_btn = QPushButton("Reject the fit  (recommended)")
    reject_btn.setFixedHeight(34)
    reject_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    reject_btn.setToolTip("Keep the fit out of force and continue on the "
                          "fallback tier. This is what Enter does.")
    reject_btn.setStyleSheet(
        "QPushButton { background-color: #27ae60; color: white;"
        " padding: 4px 18px; }"
        "QPushButton:hover { background-color: #1e8449; }")
    reject_btn.setAutoDefault(True)
    reject_btn.setDefault(True)
    row.addWidget(reject_btn)
    lay.addLayout(row)

    outcome = {"override": False}

    def _do_override():
        outcome["override"] = True
        dlg.accept()

    override_btn.clicked.connect(_do_override)
    reject_btn.clicked.connect(dlg.reject)      # Escape and the X land here too
    # Focus once the dialog is actually shown: setFocus() on an unshown widget
    # is discarded, and "Enter takes REJECT" is the whole point of the layout.
    QTimer.singleShot(0, reject_btn.setFocus)

    dlg.exec()

    if outcome["override"]:
        db.set_fit_override(
            findings,
            source=f"interactive override in the Canoe Suite UI ({context})")
        return True
    return False
