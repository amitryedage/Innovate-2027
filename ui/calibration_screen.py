# Basic calibration screen shown during CALIBRATING state, with progress bar and status updates.
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QFrame, QProgressBar
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from ui.theme import T, font, mono


class CalibrationScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Translucent overlay on top of the monitor screen
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"CalibrationScreen {{ background-color: {T.overlay}; }}")
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(460)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 30, 36, 30)
        layout.setSpacing(10)

        step = QLabel("CALIBRATION")
        step.setProperty("caption", True)
        step.setStyleSheet(f"color:{T.primary}; letter-spacing:3px;")
        layout.addWidget(step)

        title = QLabel("Learning your personal baseline")
        title.setFont(font(20, QFont.Bold))
        layout.addWidget(title)

        self.operator_label = QLabel("Operator: —")
        self.operator_label.setProperty("muted", True)
        self.operator_label.setFont(font(12))
        layout.addWidget(self.operator_label)

        layout.addSpacing(10)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedHeight(22)
        layout.addWidget(self.progress_bar)

        # Status message
        self.status_label = QLabel("Please look straight at the camera and stay relaxed...")
        self.status_label.setFont(font(13))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Face detection rate indicator
        self.face_label = QLabel("Face detection: —")
        self.face_label.setFont(mono(12, QFont.Bold))
        layout.addWidget(self.face_label)

        layout.addSpacing(8)

        info = QLabel(
            "Your alerts are based on YOUR normal behaviour, not a generic "
            "average — keep looking at the camera as you normally would."
        )
        info.setProperty("muted", True)
        info.setFont(font(11))
        info.setWordWrap(True)
        layout.addWidget(info)

        outer.addWidget(card, alignment=Qt.AlignCenter)

    def set_operator(self, name: str):
        self.operator_label.setText(f"Operator: {name}")

    def update_progress(self, progress_pct: float, face_pct: float,
                        status_message: str):
        self.progress_bar.setValue(int(progress_pct))
        self.status_label.setText(status_message)

        color = T.success if face_pct >= 80 else T.warning if face_pct >= 60 else T.danger
        self.face_label.setText(f"Face detection: {face_pct:.0f}%")
        if self.face_label.property("_color") != color:
            self.face_label.setProperty("_color", color)
            self.face_label.setStyleSheet(f"color:{color};")
