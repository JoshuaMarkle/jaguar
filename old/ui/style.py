# ui/style.py
"""
Dark theme for the ASCE Concrete Canoe Engineering Suite (JAGUAR).

The app is locked to dark mode -- there is no light theme -- so there are no
white flashes or unreadable (light-on-light) input boxes.

The palette comes from the Structural Sweep redesign, which specifies colours
in oklch.  Qt stylesheets have no oklch, so every token below is the sRGB hex
its oklch triple converts to; the source triple is kept in the comment so the
next person can re-derive rather than eyeball a match.  The character of the
scheme is a near-neutral blue-black ramp (chroma 0.004-0.014 at hue 250) with
a single warm amber accent at hue 40 -- so anything amber is a control the
user acts on, and anything grey is structure.

Usage:
    from ui.style import DARK_STYLE, apply_theme, C
"""
from __future__ import annotations

_current_theme: str = "dark"


class C:
    """Palette tokens, for widgets that must style themselves in code.

    Named by ROLE, not by colour: the redesign moved the accent from cyan to
    amber, and every call site that had hard-coded ``#00d2ff`` had to be found
    by hand.  Reference ``C.ACCENT`` and the next move is one edit here.
    """

    # Surfaces, darkest to lightest.
    BG_DEEP = "#0b0e11"          # oklch(0.16 0.008 250) -- app ground, inputs
    BG_PANEL = "#0e1114"         # oklch(0.175 0.009 250) -- settings column
    BG_BAR = "#101418"           # oklch(0.19 0.01 250) -- app bar, tab strip
    BG_CARD = "#12161b"          # oklch(0.20 0.011 250) -- section cards
    BG_HOVER = "#1d2227"         # oklch(0.25 0.012 250) -- hover / active tab

    # Lines, in the order you reach for them.
    BORDER = "#20252a"           # oklch(0.26 0.012 250)
    BORDER_CARD = "#22272d"      # oklch(0.27 0.013 250)
    BORDER_BAR = "#25292f"       # oklch(0.28 0.012 250)
    BORDER_INPUT = "#292e35"     # oklch(0.30 0.014 250)
    BORDER_STRONG = "#2e343a"    # oklch(0.32 0.014 250)
    DASH = "#33393f"             # oklch(0.34 0.014 250) -- placeholder frames

    # Text, brightest to faintest.
    TEXT = "#e9ebee"             # oklch(0.94 0.004 250)
    TEXT_MID = "#babec3"         # oklch(0.80 0.008 250)
    TEXT_DIM = "#95999d"         # oklch(0.68 0.008 250) -- field labels
    TEXT_FAINT = "#7d8185"       # oklch(0.60 0.008 250) -- inactive nav
    TEXT_GHOST = "#606468"       # oklch(0.50 0.008 250) -- hints, chevrons

    # The one accent.  Amber at hue 40 -- reserved for things the user acts on.
    ACCENT = "#f28058"           # oklch(0.72 0.15 40)
    ACCENT_HOVER = "#ff936a"     # oklch(0.78 0.15 40)
    ACCENT_BRIGHT = "#ff9a71"    # oklch(0.80 0.15 40)
    ACCENT_DEEP = "#381b10"      # oklch(0.26 0.05 40) -- current-step fill
    ON_ACCENT = "#120805"        # oklch(0.15 0.02 40) -- text ON amber

    # Status.  Green means done/loaded, yellow means "you are overriding
    # something", and there is no third status colour on purpose.
    OK = "#4eb068"               # oklch(0.68 0.14 150)
    ON_OK = "#001002"            # oklch(0.15 0.05 150)
    WARN = "#d3b63b"             # oklch(0.78 0.14 95)

    MONO = "'JetBrains Mono', 'Cascadia Mono', Consolas, 'Courier New', monospace"


def apply_theme(app, theme: str = "dark") -> None:
    """Apply the (dark) theme to a QApplication.

    The ``theme`` argument is retained for call-site compatibility but is
    ignored: only the dark stylesheet exists.
    """
    global _current_theme
    _current_theme = "dark"
    app.setStyleSheet(DARK_STYLE)


def toggle_theme(app) -> str:
    """No-op: the theme is locked to dark.  Kept so any remaining caller stays
    valid; always returns "dark" without changing the stylesheet."""
    apply_theme(app)
    return "dark"


DARK_STYLE = f"""
QMainWindow, QWidget {{
    background-color: {C.BG_DEEP};
    color: {C.TEXT};
    font-family: 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif;
    font-size: 11px;
}}
QTabWidget::pane {{
    border: 1px solid {C.BORDER};
    background-color: {C.BG_PANEL};
    border-radius: 8px;
}}
/* Tab strip: the redesign underlines the active tab in amber instead of
   filling it, so the strip reads as navigation rather than as buttons. */
QTabBar::tab {{
    background-color: transparent;
    color: {C.TEXT_DIM};
    padding: 9px 16px;
    margin-right: 2px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: bold;
}}
QTabBar::tab:selected {{
    color: {C.TEXT};
    border-bottom: 2px solid {C.ACCENT};
}}
QTabBar::tab:hover {{
    color: {C.TEXT};
    background-color: {C.BG_HOVER};
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QGroupBox {{
    border: 1px solid {C.BORDER_CARD};
    border-radius: 10px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: {C.TEXT};
    background-color: {C.BG_CARD};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {C.TEXT};
}}
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {C.BG_DEEP};
    border: 1px solid {C.BORDER_INPUT};
    border-radius: 6px;
    padding: 5px 8px;
    color: {C.TEXT};
    font-family: {C.MONO};
    selection-background-color: {C.ACCENT};
    selection-color: {C.ON_ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {C.ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {C.TEXT_GHOST};
    border-color: {C.BORDER};
}}
QPushButton {{
    background-color: transparent;
    border: 1px solid {C.BORDER_STRONG};
    border-radius: 7px;
    padding: 7px 14px;
    color: {C.TEXT_MID};
    font-weight: bold;
}}
QPushButton:hover {{
    border-color: {C.ACCENT};
    color: {C.TEXT};
}}
QPushButton:pressed {{
    background-color: {C.ACCENT};
    color: {C.ON_ACCENT};
    border-color: {C.ACCENT};
}}
QPushButton:disabled {{
    background-color: transparent;
    color: {C.TEXT_GHOST};
    border-color: {C.BORDER};
}}
/* The one filled button per screen: the primary action. */
QPushButton#primary {{
    background-color: {C.ACCENT};
    color: {C.ON_ACCENT};
    border: none;
    border-radius: 8px;
    font-size: 13px;
}}
QPushButton#primary:hover {{ background-color: {C.ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background-color: {C.ACCENT_BRIGHT}; }}
QPushButton#primary:disabled {{
    background-color: {C.BG_HOVER};
    color: {C.TEXT_GHOST};
}}
QTextEdit {{
    background-color: {C.BG_DEEP};
    border: 1px solid {C.BORDER};
    border-radius: 8px;
    color: {C.OK};
    font-family: {C.MONO};
    font-size: 10px;
    padding: 6px;
}}
QCheckBox {{
    spacing: 7px;
    color: {C.TEXT};
}}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid {C.BORDER_STRONG};
    background-color: {C.BG_DEEP};
}}
QCheckBox::indicator:checked {{
    background-color: {C.ACCENT};
    border-color: {C.ACCENT};
}}
QCheckBox::indicator:disabled {{
    border-color: {C.BORDER};
    background-color: {C.BG_CARD};
}}
QComboBox {{
    background-color: {C.BG_DEEP};
    border: 1px solid {C.BORDER_INPUT};
    border-radius: 6px;
    padding: 5px 8px;
    color: {C.TEXT};
}}
QComboBox:focus {{ border-color: {C.ACCENT}; }}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background-color: {C.BG_CARD};
    color: {C.TEXT};
    border: 1px solid {C.BORDER_INPUT};
    selection-background-color: {C.ACCENT};
    selection-color: {C.ON_ACCENT};
}}
QProgressBar {{
    background-color: {C.BG_DEEP};
    border: 1px solid {C.BORDER_INPUT};
    border-radius: 6px;
    text-align: center;
    color: {C.TEXT};
    font-size: 10px;
}}
QProgressBar::chunk {{
    background-color: {C.ACCENT};
    border-radius: 5px;
    margin: 0px;   /* solid fill -- no segmented/striped chunk rendering */
}}
/* Scrollbars -- high-contrast handles (the default ones vanish on the dark
   palette) */
QScrollBar:vertical {{
    background: {C.BG_BAR}; width: 10px; margin: 0; border: none;
}}
QScrollBar::handle:vertical {{
    background: {C.BORDER_STRONG}; min-height: 24px; border-radius: 5px; margin: 1px;
}}
QScrollBar::handle:vertical:hover {{ background: {C.ACCENT}; }}
QScrollBar:horizontal {{
    background: {C.BG_BAR}; height: 10px; margin: 0; border: none;
}}
QScrollBar::handle:horizontal {{
    background: {C.BORDER_STRONG}; min-width: 24px; border-radius: 5px; margin: 1px;
}}
QScrollBar::handle:horizontal:hover {{ background: {C.ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSlider::groove:horizontal {{
    height: 6px;
    background: {C.BG_DEEP};
    border: 1px solid {C.BORDER_INPUT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px;
    margin: -6px 0;
    background: {C.ACCENT};
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{
    background: {C.ACCENT};
    border-radius: 3px;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QSplitter::handle {{
    background-color: {C.BORDER};
    height: 6px;
}}
QSplitter::handle:hover {{
    background-color: {C.ACCENT};
}}
QToolTip {{
    background-color: {C.BG_CARD};
    color: {C.TEXT};
    border: 1px solid {C.BORDER_STRONG};
    padding: 6px 8px;
}}
QLabel#heading {{
    color: {C.TEXT};
    font-size: 13px;
    font-weight: bold;
    padding-top: 8px;
}}
QLabel#note {{
    color: {C.TEXT_GHOST};
    font-size: 10px;
}}
QLabel#regression {{
    color: {C.OK};
    font-family: {C.MONO};
    font-size: 9px;
}}
"""
