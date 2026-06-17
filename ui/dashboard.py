import sys
import os
import time
import threading
import queue

import cv2
import numpy as np

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QStackedWidget, QFrame, QPushButton, QShortcut
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QImage, QPixmap, QFont, QKeySequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ui.widgets import (
    EARGraphWidget, PERCLOSGauge, AlertStatusWidget,
    HealthPanel, MetricRow, make_card, label, C_CARD
)
from ui.login_screen import LoginScreen
from ui.calibration_screen import CalibrationScreen

from core.state_machine import SystemState
from core.database import open_session, write_audit_log


class DashboardWindow(QMainWindow):
   
    def __init__(self, frame_buffer, frame_lock,
                 session_state, session_lock,
                 ack_event, shutdown_event,
                 state_machine,
                 on_login_callback, on_shutdown_callback):
        super().__init__()

        self.frame_buffer    = frame_buffer
        self.frame_lock      = frame_lock
        self.session_state   = session_state
        self.session_lock    = session_lock
        self.ack_event       = ack_event
        self.shutdown_event  = shutdown_event
        self.sm              = state_machine
        self.on_login        = on_login_callback
        self.on_shutdown      = on_shutdown_callback

        self._calibration_manager = None   # set externally if needed

        self.setWindowTitle("Operator Fatigue Detection System — Phase 1")
        self.setMinimumSize(1180, 720)
        self.setStyleSheet("background-color: #0F172A;")

        self._build_ui()
        self._start_refresh_timer()

    
    # UI CONSTRUCTION
    

    def _build_ui(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Screen 0 — Login
        self.login_screen = LoginScreen()
        self.login_screen.login_requested.connect(self._handle_login)
        self.stack.addWidget(self.login_screen)

        # Screen 1 — Monitoring dashboard (built once, always present)
        self.monitor_widget = self._build_monitor_screen()
        self.stack.addWidget(self.monitor_widget)

        # Calibration overlay (shown ON TOP of monitor screen)
        self.calibration_screen = CalibrationScreen(self.monitor_widget)
        self.calibration_screen.setGeometry(0, 0,
                                            self.monitor_widget.width(),
                                            self.monitor_widget.height())
        self.calibration_screen.hide()

        self.stack.setCurrentIndex(0)

        # Keyboard shortcuts
        QShortcut(QKeySequence("Space"), self).activated.connect(self._on_ack)
        QShortcut(QKeySequence("D"), self).activated.connect(self._toggle_demo)
        QShortcut(QKeySequence("Q"), self).activated.connect(self.close)
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.close)

    def _build_monitor_screen(self) -> QWidget:
        root = QWidget()
        root.setStyleSheet("background-color: #0F172A;")
        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)

        # LEFT: Live video feed 
        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        video_card = make_card()
        video_layout = QVBoxLayout(video_card)
        video_layout.setContentsMargins(4, 4, 4, 4)

        self.video_label = QLabel()
        self.video_label.setMinimumSize(640, 480)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            "background-color: #000000; border-radius: 8px;"
        )
        self.video_label.setText("Initializing camera...")
        self.video_label.setStyleSheet(
            "background-color:#000; border-radius:8px; color:#64748B; font-size:13px;"
        )
        video_layout.addWidget(self.video_label)
        left_col.addWidget(video_card, stretch=3)

        # Alert status bar under video
        self.alert_status = AlertStatusWidget()
        left_col.addWidget(self.alert_status)

        # Metric row under alert bar
        self.metric_row = MetricRow()
        left_col.addWidget(self.metric_row)

        main_layout.addLayout(left_col, stretch=3)

        # RIGHT: Stats panels 
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        # Operator info card
        info_card = make_card()
        info_layout = QVBoxLayout(info_card)
        self.operator_name_lbl = label("Operator: —", size=14, bold=True)
        self.shift_timer_lbl   = label("Shift time: 00:00:00", size=11,
                                       color="#94A3B8")
        self.state_lbl         = label("State: WAITING", size=11,
                                       color="#3B82F6", bold=True)
        info_layout.addWidget(self.operator_name_lbl)
        info_layout.addWidget(self.shift_timer_lbl)
        info_layout.addWidget(self.state_lbl)
        right_col.addWidget(info_card)

        # EAR graph
        graph_card = make_card()
        graph_layout = QVBoxLayout(graph_card)
        graph_layout.setContentsMargins(2, 2, 2, 2)
        self.ear_graph = EARGraphWidget()
        graph_layout.addWidget(self.ear_graph)
        right_col.addWidget(graph_card)

        # PERCLOS gauge + glasses/drowsy flags
        gauge_row = QHBoxLayout()
        gauge_card = make_card()
        gauge_layout = QVBoxLayout(gauge_card)
        self.perclos_gauge = PERCLOSGauge()
        gauge_layout.addWidget(self.perclos_gauge, alignment=Qt.AlignCenter)
        gauge_row.addWidget(gauge_card)

        flags_card = make_card()
        flags_layout = QVBoxLayout(flags_card)
        self.glasses_lbl = label("👓 Glasses mode: No", size=10, color="#94A3B8")
        self.drowsy_lbl  = label("⚠️ Drowsy at start: No", size=10, color="#94A3B8")
        self.events_lbl  = label("Events this shift: 0", size=10, color="#94A3B8")
        flags_layout.addWidget(self.glasses_lbl)
        flags_layout.addWidget(self.drowsy_lbl)
        flags_layout.addWidget(self.events_lbl)
        flags_layout.addStretch()
        gauge_row.addWidget(flags_card)

        right_col.addLayout(gauge_row)

        # System health panel
        self.health_panel = HealthPanel()
        right_col.addWidget(self.health_panel)

        # End shift button
        self.end_shift_btn = QPushButton("⏹  End Shift")
        self.end_shift_btn.setCursor(Qt.PointingHandCursor)
        self.end_shift_btn.setStyleSheet("""
            QPushButton {
                background-color: #DC2626; color: white;
                border: none; border-radius: 8px;
                padding: 10px; font-size: 12px; font-weight: 700;
            }
            QPushButton:hover { background-color: #B91C1C; }
        """)
        self.end_shift_btn.clicked.connect(self._on_end_shift)
        right_col.addWidget(self.end_shift_btn)

        right_col.addStretch()

        right_widget = QWidget()
        right_widget.setLayout(right_col)
        right_widget.setFixedWidth(340)
        main_layout.addWidget(right_widget)

        return root

    # REFRESH TIMER — polls shared state, updates UI
    # Heavy lifting is done in _refresh() which runs every ~66ms (15 FPS) via QTimer.

    def _start_refresh_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(66)   # ~15 FPS UI refresh

    def _refresh(self):
        if self.shutdown_event.is_set():
            self.timer.stop()
            return

        # Update video frame
        with self.frame_lock:
            frame = self.frame_buffer.get("annotated") or \
                    self.frame_buffer.get("frame")

        if frame is not None:
            self._display_frame(frame)

        # Update session state widgets
        with self.session_lock:
            ear         = self.session_state.get("current_ear",     0.0)
            mar         = self.session_state.get("current_mar",     0.0)
            pitch       = self.session_state.get("current_pitch",   0.0)
            score       = self.session_state.get("fatigue_score",   0.0)
            perclos     = self.session_state.get("perclos_current", 0.0)
            baseline    = self.session_state.get("baseline_ear",    0.30)
            alert_level = self.session_state.get("alert_level",     0)
            fps         = self.session_state.get("fps",             0)
            brightness  = self.session_state.get("brightness",      100.0)
            free_mb     = self.session_state.get("storage_free_mb", 9999.0)
            face        = self.session_state.get("face_detected",   True)
            demo        = self.session_state.get("demo_mode",       False)
            thr_raised  = self.session_state.get("threshold_raised",0.0)
            op_name     = self.session_state.get("operator_name",   "—")
            start_time  = self.session_state.get("start_time",      None)
            glasses     = self.session_state.get("glasses_mode",    False)
            drowsy      = self.session_state.get("drowsy_at_start", False)

        self.ear_graph.update_value(ear, baseline)
        self.perclos_gauge.update_value(perclos)
        self.alert_status.update_level(alert_level)
        self.metric_row.update_metrics(ear, mar, pitch, score)
        self.health_panel.update_health(fps, brightness, free_mb,
                                        face, demo, thr_raised)

        self.operator_name_lbl.setText(f"Operator: {op_name}")
        self.state_lbl.setText(f"State: {self.sm.state.name}")

        self.glasses_lbl.setText(
            f"👓 Glasses mode: {'Yes' if glasses else 'No'}"
        )
        self.drowsy_lbl.setText(
            f"⚠️ Drowsy at start: {'Yes' if drowsy else 'No'}"
        )

        if start_time:
            elapsed = time.time() - start_time
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            self.shift_timer_lbl.setText(f"Shift time: {h:02d}:{m:02d}:{s:02d}")

        # Show / hide calibration overlay
        if self.sm.state == SystemState.CALIBRATING:
            if self.calibration_screen.isHidden():
                self.calibration_screen.setGeometry(
                    0, 0, self.monitor_widget.width(),
                    self.monitor_widget.height()
                )
                self.calibration_screen.set_operator(op_name)
                self.calibration_screen.show()
                self.calibration_screen.raise_()

            if self._calibration_manager:
                prog = self._calibration_manager.get_progress()
                self.calibration_screen.update_progress(
                    prog["progress_pct"], prog["face_pct"],
                    prog["status_message"]
                )
        else:
            if not self.calibration_screen.isHidden():
                self.calibration_screen.hide()

        # Switch to monitor screen once we leave login
        if self.sm.state != SystemState.WAITING_OPERATOR and \
           self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)

   