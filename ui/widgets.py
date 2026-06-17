# light weight custom widgets for fatigue detection UI
import time
from collections import deque

from PyQt5.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QProgressBar, QSizePolicy
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QFont, QBrush,
    QLinearGradient, QPainterPath
)

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))



# COLOUR PALETTE
# Tailwind-inspired dark palette for a modern look.
C_BG        = QColor("#0F172A")
C_CARD      = QColor("#1E293B")
C_BORDER    = QColor("#334155")
C_TEXT      = QColor("#F1F5F9")
C_MUTED     = QColor("#94A3B8")
C_GREEN     = QColor("#10B981")
C_AMBER     = QColor("#F59E0B")
C_RED       = QColor("#EF4444")
C_BLUE      = QColor("#3B82F6")
C_PURPLE    = QColor("#8B5CF6")
C_TEAL      = QColor("#06B6D4")


def make_card(parent=None) -> QFrame:
    """Returns a styled dark card frame."""
    card = QFrame(parent)
    card.setStyleSheet("""
        QFrame {
            background-color: #1E293B;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 8px;
        }
    """)
    return card


def label(text: str, size: int = 11, color: str = "#F1F5F9",
          bold: bool = False, parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    weight = "700" if bold else "400"
    lbl.setStyleSheet(f"""
        QLabel {{
            color: {color};
            font-size: {size}px;
            font-weight: {weight};
            background: transparent;
            border: none;
            padding: 0;
        }}
    """)
    return lbl



# EAR SCROLLING GRAPH
# Line graph for EAR values over last 30 seconds, with colour coding and alert thresholds.

class EARGraphWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Rolling buffer — 30 seconds at 15 updates/sec = 450 points
        self._values   = deque(maxlen=450)
        self._baseline = 0.30
        self._title    = "Eye Aspect Ratio (EAR)"

        # Seed with baseline
        for _ in range(30):
            self._values.append(0.30)

    def update_value(self, ear: float, baseline: float = 0.30):
        self._values.append(ear)
        self._baseline = baseline
        self.update()

    def paintEvent(self, event):
        if not self._values:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        W, H = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 44, 12, 28, 28

        # Background
        p.fillRect(0, 0, W, H, C_CARD)

        # Title
        p.setPen(C_MUTED)
        p.setFont(QFont("Arial", 9))
        p.drawText(pad_l, 16, self._title)

        graph_w = W - pad_l - pad_r
        graph_h = H - pad_t - pad_b

        # Y range: 0.05 to 0.45
        y_min, y_max = 0.05, 0.45

        def to_px(val):
            frac = (val - y_min) / (y_max - y_min)
            return pad_t + int((1 - frac) * graph_h)

        def to_x(i, total):
            return pad_l + int(i / max(total - 1, 1) * graph_w)

        # Grid lines + Y labels
        p.setFont(QFont("Arial", 8))
        for yv in [0.10, 0.20, 0.25, 0.30, 0.35, 0.40]:
            py = to_px(yv)
            p.setPen(QPen(QColor("#1E3A5F"), 1, Qt.DotLine))
            p.drawLine(pad_l, py, W - pad_r, py)
            p.setPen(C_MUTED)
            p.drawText(2, py + 4, f"{yv:.2f}")

        # Alert threshold lines
        p.setPen(QPen(C_AMBER, 1, Qt.DashLine))
        p.drawLine(pad_l, to_px(0.25), W - pad_r, to_px(0.25))
        p.setFont(QFont("Arial", 7))
        p.setPen(C_AMBER)
        p.drawText(W - pad_r - 30, to_px(0.25) - 3, "drowsy")

        # Baseline line
        p.setPen(QPen(C_TEAL, 1, Qt.DashLine))
        p.drawLine(pad_l, to_px(self._baseline),
                   W - pad_r, to_px(self._baseline))

        # Draw EAR line — colour segments by value
        vals = list(self._values)
        n    = len(vals)
        if n < 2:
            return

        path = QPainterPath()
        for i, v in enumerate(vals):
            x = to_x(i, n)
            y = to_px(v)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

            # Draw dot at current value
            if i == n - 1:
                if v < 0.20:
                    dot_color = C_RED
                elif v < 0.25:
                    dot_color = C_AMBER
                else:
                    dot_color = C_GREEN
                p.setPen(Qt.NoPen)
                p.setBrush(dot_color)
                p.drawEllipse(x - 4, y - 4, 8, 8)

        # Colour line based on latest value
        latest = vals[-1]
        if latest < 0.20:
            line_color = C_RED
        elif latest < 0.25:
            line_color = C_AMBER
        else:
            line_color = C_GREEN

        p.setPen(QPen(line_color, 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        # Current value label
        p.setFont(QFont("Arial", 10, QFont.Bold))
        p.setPen(line_color)
        p.drawText(W - 65, 18, f"EAR {latest:.3f}")

        p.end()



# PERCLOS GAUGE
# Semi-circular gauge showing PERCLOS % over last minute.


