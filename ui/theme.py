# Design tokens for the Qt UI.
# Ported from the tweakcn/shadcn theme (oklch values converted to sRGB hex,
# since Qt stylesheets can't parse oklch). Status colours (success / warning /
# danger) are not part of the theme — they were added in the same warm palette
# because green / amber / red carry safety meaning on this dashboard.
# Switch palettes with UI_THEME in config.py ("dark" or "light").

import os
from types import SimpleNamespace

from PyQt5.QtGui import QColor, QFont, QFontDatabase

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import UI_THEME


PALETTES = {
    "dark": dict(
        background   = "#1C1917",
        foreground   = "#F5F5F4",
        card         = "#292524",
        primary      = "#F97316",
        primary_fg   = "#FFFFFF",
        primary_hover= "#DF6100",
        secondary    = "#57534E",
        secondary_fg = "#E7E5E4",
        muted        = "#201D1A",
        muted_fg     = "#A8A29E",
        accent       = "#1E4252",
        border       = "#44403C",
        ring         = "#F97316",
        destructive  = "#DC2626",
        destructive_hover = "#BE1218",
        chart_blue   = "#0EA5E9",
        # status
        success      = "#53BE70",
        warning      = "#EAB308",
        danger       = "#FF655A",
        success_soft = "#1A3520",
        warning_soft = "#412805",
        danger_soft  = "#4F1A17",
        danger_blink = "#7F211D",
        video_bg     = "#0F0D0B",
        overlay      = "rgba(28, 25, 23, 235)",
    ),
    "light": dict(
        background   = "#FDFBF7",
        foreground   = "#4A3B33",
        card         = "#F8F4EE",
        primary      = "#B45309",
        primary_fg   = "#FFFFFF",
        primary_hover= "#9C4100",
        secondary    = "#E4C090",
        secondary_fg = "#57534E",
        muted        = "#F1E9DA",
        muted_fg     = "#78716C",
        accent       = "#F2DABA",
        border       = "#E4D9BC",
        ring         = "#B45309",
        destructive  = "#991B1B",
        destructive_hover = "#80070D",
        chart_blue   = "#006B9E",
        # status
        success      = "#21763C",
        warning      = "#A06108",
        danger       = "#991B1B",
        success_soft = "#D9F3DD",
        warning_soft = "#FDE8C6",
        danger_soft  = "#FFDEDA",
        danger_blink = "#FEBAB2",
        video_bg     = "#26201D",
        overlay      = "rgba(253, 251, 247, 235)",
    ),
}

T = SimpleNamespace(**PALETTES.get(UI_THEME, PALETTES["dark"]))

RADIUS    = 5            # --radius: 0.3rem
FONT_SANS = "Oxanium"
FONT_MONO = "Fira Code"

FONT_DIR = os.path.join(os.path.dirname(__file__), '..', 'assets', 'fonts')


def qc(hex_color: str) -> QColor:
    return QColor(hex_color)


def font(size: int, weight: int = QFont.Normal) -> QFont:
    f = QFont(FONT_SANS)
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


def mono(size: int, weight: int = QFont.Medium) -> QFont:
    f = QFont(FONT_MONO)
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


def load_fonts():
    """Register the bundled Oxanium / Fira Code fonts (OFL, see assets/fonts)."""
    if not os.path.isdir(FONT_DIR):
        return
    for name in sorted(os.listdir(FONT_DIR)):
        if name.endswith(".ttf"):
            path = os.path.abspath(os.path.join(FONT_DIR, name))
            if QFontDatabase.addApplicationFont(path) < 0:
                print(f"[UI] Could not load font {name}")


def stylesheet() -> str:
    return f"""
    QMainWindow, QWidget#screen {{
        background-color: {T.background};
    }}
    QLabel {{
        color: {T.foreground};
        font-family: "{FONT_SANS}";
        background: transparent;
        border: none;
    }}
    QLabel[muted="true"] {{ color: {T.muted_fg}; }}
    QLabel[caption="true"] {{
        color: {T.muted_fg};
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1px;
    }}
    QFrame#card {{
        background-color: {T.card};
        border: 1px solid {T.border};
        border-radius: {RADIUS}px;
    }}
    QPushButton {{
        font-family: "{FONT_SANS}";
        font-size: 13px;
        font-weight: 700;
        border: none;
        border-radius: {RADIUS}px;
        padding: 10px 14px;
    }}
    QPushButton#primary {{ background-color: {T.primary}; color: {T.primary_fg}; }}
    QPushButton#primary:hover {{ background-color: {T.primary_hover}; }}
    QPushButton#destructive {{ background-color: {T.destructive}; color: #FFFFFF; }}
    QPushButton#destructive:hover {{ background-color: {T.destructive_hover}; }}
    QLineEdit {{
        font-family: "{FONT_MONO}";
        font-size: 14px;
        color: {T.foreground};
        background-color: {T.background};
        border: 1px solid {T.border};
        border-radius: {RADIUS}px;
        padding: 9px 12px;
        selection-background-color: {T.primary};
    }}
    QLineEdit:focus {{ border: 1px solid {T.ring}; }}
    QCheckBox {{
        font-family: "{FONT_SANS}";
        font-size: 12px;
        color: {T.foreground};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {T.border};
        border-radius: 3px;
        background: {T.background};
    }}
    QCheckBox::indicator:checked {{
        background: {T.primary};
        border: 1px solid {T.primary};
    }}
    QProgressBar {{
        font-family: "{FONT_MONO}";
        font-size: 11px;
        font-weight: 700;
        color: {T.foreground};
        background-color: {T.muted};
        border: 1px solid {T.border};
        border-radius: {RADIUS}px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background-color: {T.primary};
        border-radius: {RADIUS - 1}px;
    }}
    QToolTip {{
        color: {T.foreground};
        background-color: {T.card};
        border: 1px solid {T.border};
    }}
    """


def apply(app):
    """Load fonts and install the global stylesheet on the QApplication."""
    load_fonts()
    app.setFont(font(12))
    app.setStyleSheet(stylesheet())
