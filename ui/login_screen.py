# Basic login screen for operator to enter ID and start the fatigue detection system.
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QCheckBox
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from ui.theme import T, font


class LoginScreen(QWidget):
    login_requested = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("screen")
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(420)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 32, 36, 32)
        card_layout.setSpacing(10)

        # Title
        brand = QLabel("FATIGUEGUARD")
        brand.setProperty("caption", True)
        brand.setStyleSheet(f"color:{T.primary}; letter-spacing:3px;")
        card_layout.addWidget(brand)

        title = QLabel("Start your shift")
        title.setFont(font(24, QFont.Bold))
        card_layout.addWidget(title)

        subtitle = QLabel("Scan your RFID card or enter your operator ID.")
        subtitle.setProperty("muted", True)
        subtitle.setFont(font(13))
        subtitle.setWordWrap(True)
        card_layout.addWidget(subtitle)

        card_layout.addSpacing(14)

        # Operator ID field
        id_label = QLabel("OPERATOR ID")
        id_label.setProperty("caption", True)
        card_layout.addWidget(id_label)

        self.operator_input = QLineEdit()
        self.operator_input.setText("OP001")
        self.operator_input.setPlaceholderText("e.g. OP001")
        card_layout.addWidget(self.operator_input)

        card_layout.addSpacing(4)

        # Demo mode checkbox
        self.demo_checkbox = QCheckBox("Demo mode — fast alerts for presentations")
        card_layout.addWidget(self.demo_checkbox)

        card_layout.addSpacing(16)

        # Login button
        self.login_btn = QPushButton("Start Shift")
        self.login_btn.setObjectName("primary")
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.clicked.connect(self._on_login)
        card_layout.addWidget(self.login_btn)

        # Footer
        footer = QLabel("Fully offline · No internet required · Privacy by design")
        footer.setProperty("muted", True)
        footer.setFont(font(10))
        footer.setAlignment(Qt.AlignCenter)
        card_layout.addSpacing(10)
        card_layout.addWidget(footer)

        outer.addWidget(card, alignment=Qt.AlignCenter)

        # Enter key triggers login
        self.operator_input.returnPressed.connect(self._on_login)

    def _on_login(self):
        operator_id = self.operator_input.text().strip() or "OP001"
        demo_mode   = self.demo_checkbox.isChecked()
        self.login_requested.emit(operator_id, demo_mode)
