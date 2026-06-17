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


# =============================================================
# widgets.py — Custom PyQt5 widgets for the dashboard
# EAR graph, PERCLOS gauge, alert indicator, health panel
# =============================================================

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


# =============================================================
# COLOUR PALETTE
# =============================================================
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


# =============================================================
# EAR SCROLLING GRAPH
# =============================================================

class EARGraphWidget(QWidget):
    """
    Scrolling line graph showing EAR value over last 30 seconds.
    Green = alert, Orange = drowsy, Red = closing/closed.
    Dashed line shows personal baseline.
    """

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


# =============================================================
# PERCLOS GAUGE
# =============================================================

class PERCLOSGauge(QWidget):
    """
    Semicircular gauge showing current PERCLOS % with
    colour zones: green → amber → red.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(160, 110)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._value   = 0.0
        self._label   = "PERCLOS"

    def update_value(self, value: float):
        self._value = min(100.0, max(0.0, value))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, C_CARD)

        cx   = W // 2
        cy   = H - 20
        r    = min(cx - 10, cy - 10)

        # Background arc (grey)
        p.setPen(QPen(QColor("#334155"), 12, Qt.SolidLine,
                      Qt.RoundCap, Qt.RoundJoin))
        from PyQt5.QtCore import QRectF
        rect = QRectF(cx - r, cy - r, 2*r, 2*r)
        p.drawArc(rect, 180 * 16, -180 * 16)

        # Value arc — colour by severity
        pct   = self._value / 100.0
        angle = int(pct * 180 * 16)

        if self._value < 15:
            arc_color = C_GREEN
        elif self._value < 25:
            arc_color = C_AMBER
        else:
            arc_color = C_RED

        p.setPen(QPen(arc_color, 12, Qt.SolidLine,
                      Qt.RoundCap, Qt.RoundJoin))
        p.drawArc(rect, 180 * 16, -angle)

        # Value text
        p.setFont(QFont("Arial", 18, QFont.Bold))
        p.setPen(arc_color)
        text = f"{self._value:.0f}%"
        fm   = p.fontMetrics()
        p.drawText(cx - fm.width(text)//2, cy - 10, text)

        # Label
        p.setFont(QFont("Arial", 8))
        p.setPen(C_MUTED)
        p.drawText(cx - 25, cy + 12, self._label)

        p.end()


# =============================================================
# ALERT STATUS PANEL
# =============================================================

class AlertStatusWidget(QWidget):
    """
    Large coloured panel showing current alert state.
    Changes colour and text based on alert level.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._level   = 0
        self._message = "✅  MONITORING — OPERATOR ALERT"
        self._blink   = False

        # Blink timer for critical alerts
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)

    def update_level(self, level: int):
        self._level = level
        if level == 0:
            self._message = "✅  MONITORING — OPERATOR ALERT"
            self._blink_timer.stop()
            self._blink = False
        elif level == 1:
            self._message = "⚠️  MILD FATIGUE DETECTED — Level 1"
            self._blink_timer.stop()
        elif level == 2:
            self._message = "🚨  DROWSY — PRESS SPACE TO ACKNOWLEDGE — Level 2"
            self._blink_timer.stop()
        else:
            self._message = "🔴  CRITICAL FATIGUE — STOP MACHINE — Level 3"
            self._blink_timer.start(400)
        self.update()

    def _toggle_blink(self):
        self._blink = not self._blink
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        if self._level == 0:
            bg = QColor("#064E3B")
            tc = C_GREEN
        elif self._level == 1:
            bg = QColor("#451A03")
            tc = C_AMBER
        elif self._level == 2:
            bg = QColor("#450A0A")
            tc = C_RED
        else:
            bg = QColor("#450A0A") if not self._blink else QColor("#7F1D1D")
            tc = QColor("#FCA5A5") if not self._blink else C_RED

        p.fillRect(0, 0, W, H, bg)

        # Border
        p.setPen(QPen(tc, 2))
        p.drawRoundedRect(1, 1, W-2, H-2, 8, 8)

        # Text
        p.setFont(QFont("Arial", 12, QFont.Bold))
        p.setPen(tc)
        fm   = p.fontMetrics()
        tw   = fm.width(self._message)
        p.drawText((W - tw)//2, H//2 + 5, self._message)
        p.end()


# =============================================================
# SYSTEM HEALTH PANEL
# =============================================================

class HealthPanel(QWidget):
    """
    Shows CPU%, free storage, camera status, FPS, brightness.
    Green / Amber / Red based on thresholds.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._fps        = 0
        self._brightness = 100.0
        self._free_mb    = 9999.0
        self._face       = True
        self._demo       = False
        self._thr_raised = 0.0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(16)

        self._fps_lbl   = self._make_metric("FPS", "0",  "#3B82F6")
        self._light_lbl = self._make_metric("LIGHT", "100", "#10B981")
        self._stor_lbl  = self._make_metric("STORAGE", "OK", "#10B981")
        self._face_lbl  = self._make_metric("FACE", "✅", "#10B981")
        self._demo_lbl  = self._make_metric("MODE", "NORMAL", "#94A3B8")
        self._thr_lbl   = self._make_metric("THRESH", "+0.00", "#94A3B8")

        for w in [self._fps_lbl, self._light_lbl, self._stor_lbl,
                  self._face_lbl, self._demo_lbl, self._thr_lbl]:
            layout.addWidget(w)

    def _make_metric(self, title: str, value: str, color: str) -> QWidget:
        container = QWidget()
        container.setStyleSheet(
            "background:#1E293B; border:1px solid #334155; border-radius:6px;"
        )
        vl = QVBoxLayout(container)
        vl.setContentsMargins(8, 4, 8, 4)
        vl.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("color:#94A3B8;font-size:8px;font-weight:600;"
                        "background:transparent;border:none;")
        t.setAlignment(Qt.AlignCenter)
        v = QLabel(value)
        v.setStyleSheet(f"color:{color};font-size:13px;font-weight:700;"
                        "background:transparent;border:none;")
        v.setAlignment(Qt.AlignCenter)
        v.setObjectName("val")
        vl.addWidget(t)
        vl.addWidget(v)
        return container

    def _set_val(self, widget, text: str, color: str):
        lbl = widget.findChild(QLabel, "val")
        if lbl:
            lbl.setText(text)
            lbl.setStyleSheet(f"color:{color};font-size:13px;font-weight:700;"
                              "background:transparent;border:none;")

    def update_health(self, fps: int, brightness: float,
                      free_mb: float, face: bool,
                      demo: bool, thr_raised: float):
        # FPS
        fps_col = "#10B981" if fps >= 20 else "#F59E0B" if fps >= 10 else "#EF4444"
        self._set_val(self._fps_lbl, str(fps), fps_col)

        # Brightness
        b_col = "#10B981" if brightness >= 40 else "#F59E0B" if brightness >= 20 else "#EF4444"
        b_str = "OK" if brightness >= 40 else "DIM" if brightness >= 20 else "DARK"
        self._set_val(self._light_lbl, b_str, b_col)

        # Storage
        s_col = "#10B981" if free_mb > 1000 else "#F59E0B" if free_mb > 500 else "#EF4444"
        s_str = f"{int(free_mb)}MB" if free_mb < 9000 else "OK"
        self._set_val(self._stor_lbl, s_str, s_col)

        # Face
        f_col = "#10B981" if face else "#EF4444"
        f_str = "✅" if face else "❌"
        self._set_val(self._face_lbl, f_str, f_col)

        # Demo mode
        d_col = "#F59E0B" if demo else "#94A3B8"
        d_str = "DEMO 🔴" if demo else "NORMAL"
        self._set_val(self._demo_lbl, d_str, d_col)

        # Threshold raised
        t_col = "#F59E0B" if thr_raised > 0 else "#94A3B8"
        t_str = f"+{thr_raised:.2f}" if thr_raised > 0 else "+0.00"
        self._set_val(self._thr_lbl, t_str, t_col)


# =============================================================
# METRIC ROW — small EAR / MAR / PITCH numbers
# =============================================================

class MetricRow(QWidget):
    """Horizontal row of EAR / MAR / PITCH current values."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(52)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._ear_w   = self._metric("EAR",   "0.300", "#10B981")
        self._mar_w   = self._metric("MAR",   "0.100", "#3B82F6")
        self._pitch_w = self._metric("PITCH", "0.0°",  "#8B5CF6")
        self._score_w = self._metric("SCORE", "0.000", "#94A3B8")

        for w in [self._ear_w, self._mar_w, self._pitch_w, self._score_w]:
            layout.addWidget(w)

    def _metric(self, title, value, color) -> QWidget:
        f = QFrame()
        f.setStyleSheet("background:#1E293B;border:1px solid #334155;border-radius:6px;")
        vl = QVBoxLayout(f)
        vl.setContentsMargins(6, 3, 6, 3)
        vl.setSpacing(1)
        tl = QLabel(title)
        tl.setStyleSheet("color:#94A3B8;font-size:8px;font-weight:600;"
                         "background:transparent;border:none;")
        tl.setAlignment(Qt.AlignCenter)
        vl.addWidget(tl)
        vl_val = QLabel(value)
        vl_val.setStyleSheet(f"color:{color};font-size:14px;font-weight:700;"
                             "background:transparent;border:none;")
        vl_val.setAlignment(Qt.AlignCenter)
        vl_val.setObjectName("val")
        vl.addWidget(vl_val)
        return f

    def _set(self, widget, text, color):
        lbl = widget.findChild(QLabel, "val")
        if lbl:
            lbl.setText(text)
            lbl.setStyleSheet(f"color:{color};font-size:14px;font-weight:700;"
                              "background:transparent;border:none;")

    def update_metrics(self, ear: float, mar: float,
                       pitch: float, score: float):
        ear_col = "#10B981" if ear >= 0.25 else "#F59E0B" if ear >= 0.20 else "#EF4444"
        mar_col = "#EF4444" if mar > 0.60 else "#3B82F6"
        pit_col = "#EF4444" if pitch > 15 else "#8B5CF6"
        sc_col  = "#EF4444" if score >= 0.40 else \
                  "#F59E0B" if score >= 0.15 else "#94A3B8"

        self._set(self._ear_w,   f"{ear:.3f}",   ear_col)
        self._set(self._mar_w,   f"{mar:.3f}",   mar_col)
        self._set(self._pitch_w, f"{pitch:.1f}°", pit_col)
        self._set(self._score_w, f"{score:.3f}",  sc_col)