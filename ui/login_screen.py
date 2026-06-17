# Basic login screen for operator to enter ID and start the fatigue detection system.
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QCheckBox
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont


class LoginScreen(QWidget):
    login_requested = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #0F172A;")
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setFixedWidth(420)
        card.setStyleSheet("""
            QFrame {
                background-color: #1E293B;
                border: 1px solid #334155;
                border-radius: 16px;
            }
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 36, 36, 36)
        card_layout.setSpacing(14)

        # Title
        title = QLabel("🛡️  Fatigue Detection System")
        title.setStyleSheet("color:#F1F5F9; font-size:20px; font-weight:700; "
                            "background:transparent; border:none;")
        title.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(title)

        subtitle = QLabel("Operator Login")
        subtitle.setStyleSheet("color:#94A3B8; font-size:13px; "
                               "background:transparent; border:none;")
        subtitle.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(subtitle)

        card_layout.addSpacing(10)

        # Operator ID field
        id_label = QLabel("Operator ID / RFID Card")
        id_label.setStyleSheet("color:#CBD5E1; font-size:11px; font-weight:600; "
                               "background:transparent; border:none;")
        card_layout.addWidget(id_label)

        self.operator_input = QLineEdit()
        self.operator_input.setText("OP001")
        self.operator_input.setPlaceholderText("e.g. OP001")
        self.operator_input.setStyleSheet("""
            QLineEdit {
                background-color: #0F172A;
                color: #F1F5F9;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 10px 12px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border: 1px solid #3B82F6;
            }
        """)
        card_layout.addWidget(self.operator_input)

        card_layout.addSpacing(6)

        # Demo mode checkbox
        self.demo_checkbox = QCheckBox("Enable Demo Mode (fast alerts for presentation)")
        self.demo_checkbox.setStyleSheet("""
            QCheckBox {
                color: #F59E0B;
                font-size: 11px;
                font-weight: 600;
                background: transparent;
                border: none;
            }
            QCheckBox::indicator {
                width: 16px; height: 16px;
            }
        """)
        card_layout.addWidget(self.demo_checkbox)

        card_layout.addSpacing(14)

        # Login button
        self.login_btn = QPushButton("Start Shift")
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.setStyleSheet("""
            QPushButton {
                background-color: #3B82F6;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #2563EB; }
            QPushButton:pressed { background-color: #1D4ED8; }
        """)
        self.login_btn.clicked.connect(self._on_login)
        card_layout.addWidget(self.login_btn)

        # Footer
        footer = QLabel("Fully offline · No internet required · Privacy by design")
        footer.setStyleSheet("color:#64748B; font-size:9px; "
                             "background:transparent; border:none;")
        footer.setAlignment(Qt.AlignCenter)
        card_layout.addSpacing(8)
        card_layout.addWidget(footer)

        outer.addWidget(card, alignment=Qt.AlignCenter)

        # Enter key triggers login
        self.operator_input.returnPressed.connect(self._on_login)

    def _on_login(self):
        operator_id = self.operator_input.text().strip() or "OP001"
        demo_mode   = self.demo_checkbox.isChecked()
        self.login_requested.emit(operator_id, demo_mode)