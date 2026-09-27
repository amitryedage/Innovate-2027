# Dashboard widgets — all colours / fonts come from ui/theme.py
from collections import deque

from PyQt5.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QSizePolicy
)
from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import QPainter, QPen, QFont, QPainterPath, QBrush

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ui.theme import T, qc, font, mono, RADIUS


# HELPERS

def make_card(parent=None) -> QFrame:
    """Themed card frame (styled by QFrame#card in the app stylesheet)."""
    card = QFrame(parent)
    card.setObjectName("card")
    return card


def label(text: str, size: int = 12, muted: bool = False,
          bold: bool = False, mono_font: bool = False, parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    f = mono(size) if mono_font else font(size)
    if bold:
        f.setWeight(QFont.Bold)
    lbl.setFont(f)
    if muted:
        lbl.setProperty("muted", True)
    return lbl


def caption(text: str, parent=None) -> QLabel:
    """Small uppercase section title, e.g. 'EAR' above a value."""
    lbl = QLabel(text.upper(), parent)
    lbl.setProperty("caption", True)
    return lbl


def set_color(lbl: QLabel, color: str):
    # Called at 15 fps — setStyleSheet forces a style recompute, so skip no-ops
    if lbl.property("_color") != color:
        lbl.setProperty("_color", color)
        lbl.setStyleSheet(f"color:{color};")


def status_color(value: float, warn: float, danger: float, higher_is_worse=True) -> str:
    if higher_is_worse:
        return T.danger if value >= danger else T.warning if value >= warn else T.success
    return T.danger if value < danger else T.warning if value < warn else T.success



# EAR SCROLLING GRAPH


class EARGraphWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Rolling buffer — 30 seconds at 15 updates/sec = 450 points
        self._values    = deque([0.30] * 30, maxlen=450)
        self._baseline  = 0.30
        self._threshold = 0.22   # EAR below this counts as closed (PERCLOS)

    def update_value(self, ear: float, baseline: float = 0.30,
                     threshold: float = 0.22):
        self._values.append(ear)
        self._baseline  = baseline
        self._threshold = threshold
        self.update()

    def _line_color(self, v: float):
        if v < self._threshold:
            return qc(T.danger)
        if v < self._threshold + 0.03:
            return qc(T.warning)
        return qc(T.success)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        W, H = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 40, 12, 32, 14
        vals = list(self._values)

        # Title + current value
        p.setFont(font(10, QFont.DemiBold))
        p.setPen(qc(T.muted_fg))
        p.drawText(12, 20, "EYE ASPECT RATIO")
        latest = vals[-1] if vals else 0.0
        p.setFont(mono(13, QFont.Bold))
        p.setPen(self._line_color(latest))
        text = f"{latest:.3f}"
        p.drawText(W - pad_r - p.fontMetrics().width(text), 21, text)

        graph_w = W - pad_l - pad_r
        graph_h = H - pad_t - pad_b
        y_min, y_max = 0.05, max(0.50, max(vals, default=0) + 0.02)

        def to_y(val):
            frac = (min(max(val, y_min), y_max) - y_min) / (y_max - y_min)
            return pad_t + int((1 - frac) * graph_h)

        def to_x(i, total):
            return pad_l + int(i / max(total - 1, 1) * graph_w)

        # Grid + Y labels
        p.setFont(mono(9))
        for yv in (0.10, 0.20, 0.30, 0.40, 0.50):
            if yv > y_max:
                continue
            py = to_y(yv)
            p.setPen(QPen(qc(T.border), 1, Qt.DotLine))
            p.drawLine(pad_l, py, W - pad_r, py)
            p.setPen(qc(T.muted_fg))
            p.drawText(6, py + 4, f"{yv:.2f}")

        # Closed-eye threshold (same value PERCLOS uses)
        ty = to_y(self._threshold)
        p.setPen(QPen(qc(T.danger), 1, Qt.DashLine))
        p.drawLine(pad_l, ty, W - pad_r, ty)
        p.setFont(font(9))
        p.drawText(W - pad_r - 44, ty - 4, "closed")

        # Personal baseline
        by = to_y(self._baseline)
        p.setPen(QPen(qc(T.chart_blue), 1, Qt.DashLine))
        p.drawLine(pad_l, by, W - pad_r, by)
        p.setPen(qc(T.chart_blue))
        p.drawText(pad_l + 4, by - 4, "baseline")

        n = len(vals)
        if n < 2:
            p.end()
            return

        path = QPainterPath()
        for i, v in enumerate(vals):
            x, y = to_x(i, n), to_y(v)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        color = self._line_color(latest)
        p.setPen(QPen(color, 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        # Current point
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(to_x(n - 1, n) - 4, to_y(latest) - 4, 8, 8)
        p.end()



# PERCLOS GAUGE


class PERCLOSGauge(QWidget):
    """Semicircular gauge showing current PERCLOS %."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 104)
        self._value = 0.0

    def update_value(self, value: float):
        self._value = min(100.0, max(0.0, value))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        W, H = self.width(), self.height()
        cx, cy = W // 2, H - 22
        r      = min(cx - 12, cy - 8)
        rect   = QRectF(cx - r, cy - r, 2 * r, 2 * r)

        # Track
        p.setPen(QPen(qc(T.border), 10, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 180 * 16, -180 * 16)

        # Value arc
        color = qc(status_color(self._value, 15, 25))
        p.setPen(QPen(color, 10, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 180 * 16, -int(self._value / 100.0 * 180 * 16))

        # Value
        p.setFont(mono(20, QFont.Bold))
        p.setPen(color)
        text = f"{self._value:.1f}%"
        p.drawText(cx - p.fontMetrics().width(text) // 2, cy - 6, text)

        # Label
        p.setFont(font(10, QFont.DemiBold))
        p.setPen(qc(T.muted_fg))
        text = "PERCLOS"
        p.drawText(cx - p.fontMetrics().width(text) // 2, cy + 16, text)
        p.end()



# ALERT STATUS BANNER


class AlertStatusWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._level   = 0
        self._reason  = ""
        self._title   = "Operator alert"
        self._detail  = "Monitoring active"
        self._blink   = False

        # Blink timer for critical alerts
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)

    def update_level(self, level: int, reason: str = ""):
        # Called every UI refresh — only act on change, otherwise the
        # L3 blink timer is restarted every frame and never fires
        if level == self._level and reason == self._reason:
            return
        self._level  = level
        self._reason = reason
        cause = f"{reason}  ·  " if reason else ""
        if level == 0:
            self._title, self._detail = "Operator alert", "Monitoring active"
            self._blink_timer.stop()
            self._blink = False
        elif level == 1:
            self._title  = "Level 1 · Mild fatigue"
            self._detail = f"{cause}Press SPACE to acknowledge"
            self._blink_timer.stop()
        elif level == 2:
            self._title  = "Level 2 · Drowsy"
            self._detail = f"{cause}Press SPACE to acknowledge"
            self._blink_timer.stop()
        else:
            self._title  = "Level 3 · Critical — stop machine"
            self._detail = f"{cause}Press SPACE to acknowledge"
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
            bg, fg = T.success_soft, T.success
        elif self._level == 1:
            bg, fg = T.warning_soft, T.warning
        elif self._level == 2:
            bg, fg = T.danger_soft, T.danger
        else:
            bg, fg = (T.danger_blink if self._blink else T.danger_soft), T.danger

        rect = QRectF(1, 1, W - 2, H - 2)
        p.setPen(QPen(qc(fg), 1.5))
        p.setBrush(QBrush(qc(bg)))
        p.drawRoundedRect(rect, RADIUS, RADIUS)

        # Status dot
        p.setPen(Qt.NoPen)
        p.setBrush(qc(fg))
        p.drawEllipse(18, H // 2 - 6, 12, 12)

        # Title
        p.setFont(font(15, QFont.Bold))
        p.setPen(qc(fg))
        p.drawText(42, H // 2 + 6, self._title)
        title_w = p.fontMetrics().width(self._title)

        # Detail (right-aligned)
        p.setFont(font(12))
        p.setPen(qc(T.foreground))
        dw = p.fontMetrics().width(self._detail)
        x  = max(42 + title_w + 24, W - dw - 18)
        p.drawText(x, H // 2 + 5, self._detail)
        p.end()



# STAT TILE — caption over a value (used by metric row and health panel)


class StatTile(QFrame):
    def __init__(self, title: str, value: str, value_size: int = 15, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        vl = QVBoxLayout(self)
        vl.setContentsMargins(6, 6, 6, 6)
        vl.setSpacing(2)
        cap = caption(title)
        cap.setAlignment(Qt.AlignCenter)
        self.value = QLabel(value)
        self.value.setFont(mono(value_size, QFont.Bold))
        self.value.setAlignment(Qt.AlignCenter)
        vl.addWidget(cap)
        vl.addWidget(self.value)

    def set(self, text: str, color: str):
        self.value.setText(text)
        set_color(self.value, color)



# SYSTEM HEALTH PANEL


class HealthPanel(QWidget):
    """FPS, light level, storage, face, mode and threshold raise tiles."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._fps   = StatTile("FPS",     "0",     12)
        self._light = StatTile("Light",   "OK",    12)
        self._stor  = StatTile("Storage", "OK",    12)
        self._face  = StatTile("Face",    "—",     12)
        self._mode  = StatTile("Mode",    "NORM",  12)
        self._thr   = StatTile("Thresh",  "+0.00", 12)
        for w in (self._fps, self._light, self._stor,
                  self._face, self._mode, self._thr):
            layout.addWidget(w)

    def update_health(self, fps: int, brightness: float,
                      free_mb: float, face: bool,
                      demo: bool, thr_raised: float):
        self._fps.set(str(fps), status_color(fps, 20, 10, higher_is_worse=False))

        self._light.set("OK" if brightness >= 40 else "DIM" if brightness >= 20 else "DARK",
                        status_color(brightness, 40, 20, higher_is_worse=False))

        s_col = T.success if free_mb > 1000 else T.warning if free_mb > 500 else T.danger
        self._stor.set(f"{int(free_mb)}MB" if free_mb < 9000 else "OK", s_col)

        self._face.set("YES" if face else "LOST", T.success if face else T.danger)

        self._mode.set("DEMO" if demo else "NORM", T.warning if demo else T.muted_fg)

        self._thr.set(f"+{thr_raised:.2f}",
                      T.warning if thr_raised > 0 else T.muted_fg)



# METRIC ROW — EAR / MAR / PITCH / SCORE


class MetricRow(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._ear   = StatTile("EAR",   "0.300", 17)
        self._mar   = StatTile("MAR",   "0.000", 17)
        self._pitch = StatTile("Pitch", "0.0°",  17)
        self._score = StatTile("Score", "0.00",  17)
        for w in (self._ear, self._mar, self._pitch, self._score):
            layout.addWidget(w)

    def update_metrics(self, ear: float, mar: float, pitch: float,
                       score: float, ear_threshold: float = 0.22,
                       mar_thresh: float = 0.60, pitch_thresh: float = 15.0,
                       l1: float = 0.15, l3: float = 0.40):
        self._ear.set(f"{ear:.3f}",
                      status_color(ear, ear_threshold + 0.03, ear_threshold,
                                   higher_is_worse=False))
        self._mar.set(f"{mar:.3f}", T.danger if mar > mar_thresh else T.foreground)
        self._pitch.set(f"{pitch:.1f}°", T.danger if pitch > pitch_thresh else T.foreground)
        # Score is relative to the personal baseline and goes negative while
        # eyes are more open than baseline — show 0 instead of confusing -1.0
        shown = max(0.0, score)
        self._score.set(f"{shown:.2f}",
                        T.danger if score >= l3 else T.warning if score >= l1 else T.foreground)



# RISK SCORE RING


class RiskScoreWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 132)
        self._score = 0.0
        self._band  = "GREEN"

    def update_score(self, score: float, band: str):
        self._score = min(100.0, max(0.0, score))
        self._band  = band
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        color = qc({
            "GREEN":  T.success,
            "AMBER":  T.warning,
            "ORANGE": T.primary,
            "RED":    T.danger,
        }.get(self._band, T.muted_fg))

        p.setFont(font(10, QFont.DemiBold))
        p.setPen(qc(T.muted_fg))
        title = "SHIFT RISK"
        p.drawText((W - p.fontMetrics().width(title)) // 2, 14, title)

        # Ring sits below the title and inside the widget with room for the pen
        top    = 24
        r      = min(W // 2, (H - top) // 2) - 8
        cx, cy = W // 2, top + (H - top) // 2
        rect   = QRectF(cx - r, cy - r, 2 * r, 2 * r)

        p.setPen(QPen(qc(T.border), 8, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(rect)
        p.setPen(QPen(color, 8, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 90 * 16, -int(self._score / 100.0 * 360 * 16))

        p.setFont(mono(22, QFont.Bold))
        p.setPen(color)
        text = f"{self._score:.0f}"
        p.drawText(cx - p.fontMetrics().width(text) // 2, cy + 6, text)

        p.setFont(font(9, QFont.Bold))
        p.drawText(cx - p.fontMetrics().width(self._band) // 2, cy + 22, self._band)
        p.end()



# FATIGUE TREND INDICATOR


class TrendIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignCenter)

        title = caption("Fatigue trend")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self._arrow = QLabel("→  STABLE")
        self._arrow.setFont(font(15, QFont.Bold))
        self._arrow.setAlignment(Qt.AlignCenter)
        set_color(self._arrow, T.muted_fg)
        layout.addWidget(self._arrow)

        self._eta_lbl = QLabel("")
        self._eta_lbl.setFont(mono(10))
        self._eta_lbl.setAlignment(Qt.AlignCenter)
        set_color(self._eta_lbl, T.warning)
        layout.addWidget(self._eta_lbl)

    def update_trend(self, direction: str, eta_min=None, r2: float = 0.0):
        if direction == "rising":
            text, color = "↑  RISING", T.danger
        elif direction == "falling":
            text, color = "↓  FALLING", T.success
        else:
            text, color = "→  STABLE", T.muted_fg
        self._arrow.setText(text)
        set_color(self._arrow, color)

        if eta_min and direction == "rising":
            self._eta_lbl.setText(f"ETA ~{eta_min:.0f} min · R² {r2:.2f}")
        else:
            self._eta_lbl.setText("")
