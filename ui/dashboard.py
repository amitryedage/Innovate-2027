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
    HealthPanel, MetricRow, make_card, label, caption, set_color,
    RiskScoreWidget, TrendIndicator
)
from ui.theme import T, RADIUS
from ui.login_screen import LoginScreen
from ui.calibration_screen import CalibrationScreen

from core.state_machine import SystemState
from core.database import open_session, write_audit_log
from config import (
    EAR_CLOSED_NORMAL, EAR_CLOSED_GLASSES, MAR_YAWN_THRESH,
    PITCH_DROOP_THRESH, FATIGUE_L1_THRESH, FATIGUE_L3_THRESH,
)

# State badge colour per system state
STATE_COLORS = {
    SystemState.MONITORING:       T.success,
    SystemState.CALIBRATING:      T.primary,
    SystemState.CRASH_RECOVERY:   T.primary,
    SystemState.ALERT_L1:         T.warning,
    SystemState.ALERT_L2:         T.danger,
    SystemState.ALERT_L3:         T.danger,
    SystemState.FACE_LOSS:        T.warning,
    SystemState.TAMPER_ALERT:     T.danger,
}


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

        self.setWindowTitle("FatigueGuard — Operator Fatigue Detection")
        self.setMinimumSize(1240, 760)

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
        root.setObjectName("screen")
        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(16)

        # LEFT: live video, alert banner, live metrics
        left_col = QVBoxLayout()
        left_col.setSpacing(12)

        video_card = make_card()
        video_layout = QVBoxLayout(video_card)
        video_layout.setContentsMargins(6, 6, 6, 6)
        self.video_label = QLabel("Initializing camera...")
        self.video_label.setMinimumSize(640, 480)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            f"background-color:{T.video_bg}; border-radius:{RADIUS}px;"
            f"color:{T.muted_fg};"
        )
        video_layout.addWidget(self.video_label)
        left_col.addWidget(video_card, stretch=1)

        self.alert_status = AlertStatusWidget()
        left_col.addWidget(self.alert_status)

        self.metric_row = MetricRow()
        left_col.addWidget(self.metric_row)

        main_layout.addLayout(left_col, stretch=1)

        # RIGHT: session info and trend panels
        right_col = QVBoxLayout()
        right_col.setSpacing(12)

        # Operator card
        info_card = make_card()
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(16, 14, 16, 14)
        info_layout.setSpacing(6)
        info_layout.addWidget(caption("Operator"))
        self.operator_name_lbl = label("—", size=18, bold=True)
        info_layout.addWidget(self.operator_name_lbl)
        status_row = QHBoxLayout()
        self.shift_timer_lbl = label("00:00:00", size=13, mono_font=True, muted=True)
        self.state_lbl = QLabel("WAITING")
        self.state_lbl.setAlignment(Qt.AlignCenter)
        self._state_color = None
        status_row.addWidget(self.shift_timer_lbl)
        status_row.addStretch()
        status_row.addWidget(self.state_lbl)
        info_layout.addLayout(status_row)
        right_col.addWidget(info_card)

        # EAR graph
        graph_card = make_card()
        graph_layout = QVBoxLayout(graph_card)
        graph_layout.setContentsMargins(4, 4, 4, 4)
        self.ear_graph = EARGraphWidget()
        graph_layout.addWidget(self.ear_graph)
        right_col.addWidget(graph_card)

        # PERCLOS gauge + session flags
        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(12)
        gauge_card = make_card()
        gauge_layout = QVBoxLayout(gauge_card)
        gauge_layout.setContentsMargins(8, 8, 8, 8)
        self.perclos_gauge = PERCLOSGauge()
        gauge_layout.addWidget(self.perclos_gauge, alignment=Qt.AlignCenter)
        gauge_row.addWidget(gauge_card)

        flags_card = make_card()
        flags_layout = QVBoxLayout(flags_card)
        flags_layout.setContentsMargins(14, 12, 14, 12)
        flags_layout.setSpacing(4)
        self.glasses_lbl = self._flag_row(flags_layout, "Glasses mode")
        self.drowsy_lbl  = self._flag_row(flags_layout, "Drowsy at start")
        self.events_lbl  = self._flag_row(flags_layout, "Alerts this shift")
        flags_layout.addStretch()
        gauge_row.addWidget(flags_card, stretch=1)
        right_col.addLayout(gauge_row)

        # Shift risk + fatigue trend
        usp_row = QHBoxLayout()
        usp_row.setSpacing(12)
        risk_card = make_card()
        risk_layout = QVBoxLayout(risk_card)
        risk_layout.setContentsMargins(8, 8, 8, 8)
        self.risk_widget = RiskScoreWidget()
        risk_layout.addWidget(self.risk_widget, alignment=Qt.AlignCenter)
        usp_row.addWidget(risk_card)
        trend_card = make_card()
        trend_layout = QVBoxLayout(trend_card)
        self.trend_widget = TrendIndicator()
        trend_layout.addWidget(self.trend_widget)
        usp_row.addWidget(trend_card, stretch=1)
        right_col.addLayout(usp_row)

        # System health tiles
        self.health_panel = HealthPanel()
        right_col.addWidget(self.health_panel)

        right_col.addStretch()

        # End shift button
        self.end_shift_btn = QPushButton("End Shift")
        self.end_shift_btn.setObjectName("destructive")
        self.end_shift_btn.setCursor(Qt.PointingHandCursor)
        self.end_shift_btn.clicked.connect(self._on_end_shift)
        right_col.addWidget(self.end_shift_btn)

        right_widget = QWidget()
        right_widget.setLayout(right_col)
        right_widget.setFixedWidth(400)
        main_layout.addWidget(right_widget)

        return root

    def _flag_row(self, layout, title: str) -> QLabel:
        row = QHBoxLayout()
        row.addWidget(label(title, size=12, muted=True))
        row.addStretch()
        value = label("—", size=12, bold=True, mono_font=True)
        row.addWidget(value)
        layout.addLayout(row)
        return value

    def _set_state_badge(self, state):
        color = STATE_COLORS.get(state, T.muted_fg)
        self.state_lbl.setText(state.name.replace("_", " "))
        if color != self._state_color:
            self._state_color = color
            self.state_lbl.setStyleSheet(
                f"color:{color}; border:1px solid {color}; border-radius:{RADIUS}px;"
                f"padding:2px 8px; font-size:11px; font-weight:700;"
            )

    
    # REFRESH TIMER — polls shared state, updates UI
   

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
            # numpy arrays can't be used with `or` — check None explicitly
            frame = self.frame_buffer.get("annotated")
            if frame is None:
                frame = self.frame_buffer.get("frame")

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
            alert_reason= self.session_state.get("alert_reason",    "")
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

        closed_thresh = EAR_CLOSED_GLASSES if glasses else EAR_CLOSED_NORMAL
        self.ear_graph.update_value(ear, baseline, closed_thresh)
        self.perclos_gauge.update_value(perclos)
        self.alert_status.update_level(alert_level, alert_reason)
        self.metric_row.update_metrics(ear, mar, pitch, score, closed_thresh,
                                       MAR_YAWN_THRESH, PITCH_DROOP_THRESH,
                                       FATIGUE_L1_THRESH, FATIGUE_L3_THRESH)
        self.health_panel.update_health(fps, brightness, free_mb,
                                        face, demo, thr_raised)

        # USP  Risk score widget
        with self.session_lock:
            risk_score = self.session_state.get('risk_score', 0.0)
            risk_band  = self.session_state.get('risk_band', 'GREEN')
            trend_dir  = self.session_state.get('fatigue_trend_slope', 0.0)
            trend_r2   = self.session_state.get('fatigue_trend_r2', 0.0)
            eta_sec    = self.session_state.get('fatigue_eta_sec', None)
            alerts     = self.session_state.get('alert_count', 0)
        self.risk_widget.update_score(risk_score, risk_band)
        eta_min = eta_sec / 60 if eta_sec else None
        direction = ('rising' if trend_dir > 0.0005 and trend_r2 > 0.4
                     else 'falling' if trend_dir < -0.0005
                     else 'stable')
        self.trend_widget.update_trend(direction, eta_min, trend_r2)

        self.operator_name_lbl.setText(op_name or "—")
        self._set_state_badge(self.sm.state)
        self.glasses_lbl.setText("Yes" if glasses else "No")
        self.drowsy_lbl.setText("Yes" if drowsy else "No")
        set_color(self.drowsy_lbl, T.warning if drowsy else T.foreground)
        self.events_lbl.setText(str(alerts))
        set_color(self.events_lbl, T.warning if alerts else T.foreground)

        if start_time:
            elapsed = time.time() - start_time
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            self.shift_timer_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}")

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
                prog = self._calibration_manager.get_calibration_progress()
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

    def _display_frame(self, frame: np.ndarray):
        """Convert OpenCV BGR frame to QPixmap and show it."""
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg)
            scaled = pixmap.scaled(
                self.video_label.width(), self.video_label.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.video_label.setPixmap(scaled)
        except Exception as e:
            print(f"[UI] Frame display error: {e}")

    
    # EVENT HANDLERS
    # Help to show case working of the USP 
    

    def _handle_login(self, operator_id: str, demo_mode: bool):
        """Called when LoginScreen emits login_requested."""
        print(f"[UI] Login requested: {operator_id} demo={demo_mode}")
        self.stack.setCurrentIndex(1)   # switch to monitor screen
        if self.on_login:
            self.on_login(operator_id, demo_mode)

    def _on_ack(self):
        self.ack_event.set()
        print("[UI] Acknowledged via spacebar")
        QTimer.singleShot(100, self.ack_event.clear)

    def _toggle_demo(self):
        with self.session_lock:
            dm = not self.session_state.get("demo_mode", False)
            self.session_state["demo_mode"] = dm
        print(f"[UI] Demo mode: {'ON' if dm else 'OFF'}")

    def _on_end_shift(self):
        print("[UI] End shift requested")
        self.close()

    def set_calibration_manager(self, manager):
        """Allow main.py to attach the SessionManager for calibration progress polling."""
        self._calibration_manager = manager

    def closeEvent(self, event):
        print("[UI] Window closing — triggering shutdown")
        if self.on_shutdown:
            self.on_shutdown("UI window closed")
        event.accept()