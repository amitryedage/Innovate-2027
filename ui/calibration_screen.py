# Basic calibration screen shown during CALIBRATING state, with progress bar and status updates.
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QFrame, QProgressBar
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont


class CalibrationScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: rgba(15, 23, 42, 235);")
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setFixedWidth(460)
        card.setStyleSheet("""
            QFrame {
                background-color: #1E293B;
                border: 2px solid #10B981;
                border-radius: 16px;
            }
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 32, 36, 32)
        layout.setSpacing(12)

        icon = QLabel("📐")
        icon.setStyleSheet("font-size:36px; background:transparent; border:none;")
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon)

        title = QLabel("Calibrating Your Personal Baseline")
        title.setStyleSheet("color:#F1F5F9; font-size:17px; font-weight:700; "
                            "background:transparent; border:none;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.operator_label = QLabel("Operator: —")
        self.operator_label.setStyleSheet("color:#94A3B8; font-size:11px; "
                                          "background:transparent; border:none;")
        self.operator_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.operator_label)

        layout.addSpacing(12)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedHeight(24)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #0F172A;
                border: 1px solid #334155;
                border-radius: 8px;
                text-align: center;
                color: #F1F5F9;
                font-weight: 700;
                font-size: 11px;
            }
            QProgressBar::chunk {
                background-color: #10B981;
                border-radius: 7px;
            }
        """)
        layout.addWidget(self.progress_bar)

        layout.addSpacing(8)

        # Status message
        self.status_label = QLabel("Please look straight at the camera and stay relaxed...")
        self.status_label.setStyleSheet("color:#CBD5E1; font-size:12px; "
                                        "background:transparent; border:none;")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addSpacing(6)

        # Face detection rate indicator
        self.face_label = QLabel("Face detection: —")
        self.face_label.setStyleSheet("color:#10B981; font-size:11px; font-weight:600; "
                                      "background:transparent; border:none;")
        self.face_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.face_label)

        layout.addSpacing(10)

        info = QLabel(
            "This personal baseline ensures fair, accurate fatigue detection — "
            "your alerts are based on YOUR normal behaviour, not a generic average."
        )
        info.setStyleSheet("color:#64748B; font-size:9px; "
                           "background:transparent; border:none;")
        info.setAlignment(Qt.AlignCenter)
        info.setWordWrap(True)
        layout.addWidget(info)

        outer.addWidget(card, alignment=Qt.AlignCenter)

    def set_operator(self, name: str):
        self.operator_label.setText(f"Operator: {name}")

    def update_progress(self, progress_pct: float, face_pct: float,
                        status_message: str):
        self.progress_bar.setValue(int(progress_pct))
        self.status_label.setText(status_message)

        if face_pct >= 80:
            color, icon = "#10B981", "✅"
        elif face_pct >= 60:
            color, icon = "#F59E0B", "⚠️"
        else:
            color, icon = "#EF4444", "❌"

        self.face_label.setText(f"{icon} Face detection: {face_pct:.0f}%")
        self.face_label.setStyleSheet(
            f"color:{color}; font-size:11px; font-weight:600; "
            "background:transparent; border:none;"
        )