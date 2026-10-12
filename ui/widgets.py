# ui/widgets.py
"""
Shared custom widgets for the ASCE Concrete Canoe Engineering Suite.

NoScrollSpinBox / NoScrollDoubleSpinBox: spin boxes that only respond to the
mouse wheel when they already hold keyboard focus (i.e. the user clicked into
them).  This stops the wheel from silently changing a value while the user is
just scrolling the settings panel past it.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QSpinBox, QDoubleSpinBox, QWidget, QFrame, QLabel,
    QHBoxLayout, QVBoxLayout, QSizePolicy,
)


class _NoWheelScrollMixin:
    """
    Mixin: swallow wheel events unless the box is focused.

    * ``StrongFocus`` removes ``WheelFocus`` from the default policy, so
      scrolling can no longer *give* the box focus.
    * An unfocused ``wheelEvent`` is ignored (not consumed), so the event
      bubbles up to the enclosing scroll area and the panel scrolls normally
      instead of the value changing by accident.  Once the user clicks in
      (focus), the wheel adjusts the value as usual.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollSpinBox(_NoWheelScrollMixin, QSpinBox):
    pass


class NoScrollDoubleSpinBox(_NoWheelScrollMixin, QDoubleSpinBox):
    pass


# ══════════════════════════════════════════════════════════════════════════
#  Redesign widgets — the two shapes the Structural Sweep mockup introduces
# ══════════════════════════════════════════════════════════════════════════

class PipelineStepper(QWidget):
    """Seed → Godzilla → Structural → Export, with each step's state.

    The pipeline was only ever implied by the tab order, so nothing on screen
    said which stage you were at or what was still owed.  Steps before the
    current one read Complete in green, the current one is filled amber, and
    later ones stay outlined and dim.

    ``set_active(key)`` moves the marker; ``set_status(key, text)`` overrides
    one step's caption when a stage knows something more specific than
    Complete/In progress/Pending (e.g. "off1 + out loaded").
    """

    STEPS = (("seed", "Seed"), ("godzilla", "Godzilla"),
             ("structural", "Structural"), ("export", "Export"))

    def __init__(self, active: str = "structural", parent=None):
        super().__init__(parent)
        self._active = active
        self._overrides: dict = {}
        self._rows: dict = {}

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(0)

        for i, (key, label) in enumerate(self.STEPS):
            dot = QLabel(str(i + 1))
            dot.setFixedSize(24, 24)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)

            name = QLabel(label)
            status = QLabel("Pending")

            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(0)
            text_col.addWidget(name)
            text_col.addWidget(status)

            chip_inner = QHBoxLayout()
            chip_inner.setContentsMargins(14, 7, 16, 7)
            chip_inner.setSpacing(10)
            chip_inner.addWidget(dot)
            chip_inner.addLayout(text_col)

            chip = QFrame()
            chip.setLayout(chip_inner)
            lay.addWidget(chip)

            connector = None
            if i < len(self.STEPS) - 1:
                connector = QFrame()
                connector.setFixedHeight(2)
                connector.setMinimumWidth(24)
                connector.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
                lay.addSpacing(6)
                lay.addWidget(connector, 1)
                lay.addSpacing(6)

            self._rows[key] = (chip, dot, name, status, connector)

        self._repaint()

    # ---- public API -------------------------------------------------
    def set_active(self, key: str) -> None:
        if key in self._rows and key != self._active:
            self._active = key
            self._repaint()

    def set_status(self, key: str, text) -> None:
        """Override (or clear, with None) one step's caption."""
        if key not in self._rows:
            return
        if text is None:
            self._overrides.pop(key, None)
        else:
            self._overrides[key] = text
        self._repaint()

    # ---- painting ---------------------------------------------------
    def _repaint(self) -> None:
        from ui.style import C
        order = [k for k, _ in self.STEPS]
        active_i = order.index(self._active) if self._active in order else 0

        for i, (key, _label) in enumerate(self.STEPS):
            chip, dot, name, status, connector = self._rows[key]
            done, current = i < active_i, i == active_i

            if current:
                chip_bg, chip_border = C.ACCENT_DEEP, C.ACCENT
                dot_bg, dot_fg = C.ACCENT, C.ON_ACCENT
                name_col, status_col = C.TEXT, C.ACCENT
            elif done:
                chip_bg, chip_border = C.BG_CARD, C.OK
                dot_bg, dot_fg = C.OK, C.ON_OK
                name_col, status_col = C.TEXT, C.OK
            else:
                chip_bg, chip_border = "transparent", C.BORDER_INPUT
                dot_bg, dot_fg = C.BORDER, C.TEXT_DIM
                name_col, status_col = C.TEXT_DIM, C.TEXT_GHOST

            chip.setStyleSheet(
                f"background:{chip_bg}; border:1px solid {chip_border}; "
                f"border-radius:8px;")
            dot.setText("✓" if done else str(i + 1))
            dot.setStyleSheet(
                f"background:{dot_bg}; color:{dot_fg}; border:none; "
                f"border-radius:12px; font-family:{C.MONO}; font-size:11px; "
                f"font-weight:700;")
            name.setStyleSheet(
                f"background:transparent; border:none; color:{name_col}; "
                f"font-size:12px; font-weight:600;")
            status.setText(self._overrides.get(
                key,
                "Complete" if done else "In progress" if current else "Pending"))
            status.setStyleSheet(
                f"background:transparent; border:none; color:{status_col}; "
                f"font-family:{C.MONO}; font-size:10px;")
            if connector is not None:
                connector.setStyleSheet(
                    f"background:{C.OK if done else C.BORDER_INPUT}; border:none;")


class CollapsibleCard(QWidget):
    """A titled card whose body folds away, with a chevron in the header.

    Replaces a QToolBox page.  The difference that matters is not visual: a
    QToolBox shows exactly ONE page at a time, so checking a thickness bound
    against the depth bound that constrains it meant clicking back and forth
    holding a number in your head.  These sections are independent -- open as
    many as the work needs.

    ``body`` is any widget; the card takes ownership and shows/hides it.
    """

    def __init__(self, title: str, body, expanded: bool = False,
                 warn: bool = False, parent=None):
        super().__init__(parent)
        from ui.style import C

        self._body = body
        self._expanded = bool(expanded)

        self._chevron = QLabel()
        self._chevron.setStyleSheet(
            f"background:transparent; border:none; color:{C.TEXT_GHOST}; "
            f"font-size:10px;")

        self._title = QLabel(title)
        self._title.setStyleSheet(
            f"background:transparent; border:none; "
            f"color:{C.WARN if warn else C.TEXT}; "
            f"font-size:12px; font-weight:700; letter-spacing:0.3px;")

        head_lay = QHBoxLayout()
        head_lay.setContentsMargins(14, 11, 14, 11)
        head_lay.addWidget(self._title)
        head_lay.addStretch(1)
        head_lay.addWidget(self._chevron)

        self._header = QFrame()
        self._header.setLayout(head_lay)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.mouseReleaseEvent = lambda _e: self.toggle()

        body_wrap = QVBoxLayout()
        body_wrap.setContentsMargins(14, 0, 14, 14)
        body_wrap.addWidget(body)
        self._body_host = QWidget()
        self._body_host.setLayout(body_wrap)
        self._body_host.setStyleSheet("background:transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._header)
        outer.addWidget(self._body_host)

        self.setStyleSheet(
            f"CollapsibleCard {{ background:{C.BG_CARD}; "
            f"border:1px solid {C.WARN if warn else C.BORDER_CARD}; "
            f"border-radius:10px; }}")
        self._apply()

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, on: bool) -> None:
        self._expanded = bool(on)
        self._apply()

    def is_expanded(self) -> bool:
        return self._expanded

    def _apply(self) -> None:
        self._body_host.setVisible(self._expanded)
        self._chevron.setText("▲" if self._expanded else "▼")
