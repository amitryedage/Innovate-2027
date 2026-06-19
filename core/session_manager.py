
# session_manager.py — Multi-operator session lifecycle manager
# RESPONSIBILITY:
# Owns everything between "operator swipes card" and "PDF generated".
# Designed to loop indefinitely — when one operator ends their shift,
# this manager closes that session and prepares for the next operator
# without restarting the process or any thread.



import threading
import time
import queue
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.state_machine import StateMachine, SystemState
from core.database import (
    get_operator, open_session, close_session,
    update_operator_baseline, write_audit_log,
    get_connection, get_all_operators
)
from core.calibration import CalibrationManager, CalibrationError
from config import RECALIBRATE_AFTER_DAYS


class SessionManager:
    """
    Manages the full operator session lifecycle.

    Usage:
        mgr = SessionManager(session_state, session_lock,
                             state_machine, db_queue,
                             shutdown_event, ack_event)

        # When operator logs in (called from UI callback):
        mgr.start_session("OP001", demo_mode=False)

        # When operator ends shift (called from UI button):
        mgr.end_session()

        # Poll this for calibration progress (called from UI refresh timer):
        progress = mgr.get_calibration_progress()
    """

    def __init__(self, session_state: dict, session_lock: threading.Lock,
                 state_machine: StateMachine,
                 db_queue: queue.Queue,
                 shutdown_event: threading.Event,
                 ack_event: threading.Event):

        self.session_state  = session_state
        self.session_lock   = session_lock
        self.sm             = state_machine
        self.db_queue       = db_queue
        self.shutdown_event = shutdown_event
        self.ack_event      = ack_event

        # Active calibration manager — polled by UI for progress bar
        self._cal_manager: CalibrationManager = None
        self._cal_thread:  threading.Thread   = None

        # Handover callback — UI sets this to show LoginScreen again
        self.on_session_ended_callback = None

        print("[MGR] SessionManager initialized.")

    
    # OPEN SESSION
    # Validates operator exists or not 

    def start_session(self, operator_id: str, demo_mode: bool = False):
        """
        Called when an operator logs in (RFID swipe or PIN entry).
        1. Validates operator exists (registers if first time)
        2. Opens a new session in SQLite
        3. Loads stored baseline OR starts fresh calibration
        4. Transitions state machine to CALIBRATING or MONITORING
        """
        print(f"\n[MGR] Starting session for operator: {operator_id}")

        # Ensure operator exists in DB
        operator = self._ensure_operator(operator_id)
        if not operator:
            print(f"[MGR]  Could not register operator {operator_id}")
            return

        # Open session in DB
        session_id = open_session(operator_id, demo_mode=demo_mode)
        print(f"[MGR] Session opened: {session_id}")

        # Update shared session_state
        with self.session_lock:
            self.session_state.update({
                "session_id":        session_id,
                "operator_id":       operator_id,
                "operator_name":     operator["name"],
                "start_time":        time.time(),
                "demo_mode":         demo_mode,
                "glasses_mode":      bool(operator["glasses_mode"]),
                "alert_level":       0,
                "alert_active":      False,
                "threshold_raised":  0.0,
                "perclos_current":   0.0,
                "fatigue_score":     0.0,
                "ack_times":         [],
                "last_report_path":  None,
                "storage_warning":   False,
                "storage_critical":  False,
            })

        write_audit_log("SESSION_START", operator_id, session_id,
                        f"demo={demo_mode}")

        # Decide: use stored baseline OR run calibration
        needs_calibration = self._needs_calibration(operator, demo_mode)

        if not needs_calibration:
            # Load stored baseline directly — handover under 5 seconds.
            # Still route through CALIBRATING briefly so the state machine
            # transition graph is respected (WAITING_OPERATOR → CALIBRATING
            # → MONITORING). This takes <1ms — no calibration actually runs.
            self._load_stored_baseline(operator)
            self.sm.transition(SystemState.CALIBRATING)
            self.sm.transition(SystemState.MONITORING)
            print(f"[MGR] Stored baseline loaded for {operator_id} "
                  f"— monitoring active immediately")
        else:
            # Fresh calibration needed
            self.sm.transition(SystemState.CALIBRATING)
            self._start_calibration(operator_id, demo_mode)

    
    # CLOSE SESSION
    # Run when at the end of session 

    def end_session(self, reason: str = "Operator ended shift"):
        """
        Called when operator presses 'End Shift' or new operator logs in.
        Closes the current session, triggers PDF generation, resets state.
        """
        with self.session_lock:
            session_id  = self.session_state.get("session_id")
            op_id       = self.session_state.get("operator_id")
            op_name     = self.session_state.get("operator_name", "Unknown")

        if not session_id:
            print("[MGR] No active session to close.")
            return

        print(f"\n[MGR] Closing session {session_id} — {reason}")

        # Cancel any running calibration
        if self._cal_thread and self._cal_thread.is_alive():
            print("[MGR] Cancelling active calibration...")
            # CalibrationManager checks shutdown_event — signal it temporarily
            # using a local event rather than the global one
            if self._cal_manager:
                self._cal_manager._interrupted = True
            self._cal_thread.join(timeout=3)

        # Tell Thread 4 to close session and generate PDF
        try:
            self.db_queue.put_nowait({
                "action":          "SESSION_CLOSE",
                "session_id":      session_id,
                "operator_name":   op_name,
                "generate_report": True,
            })
        except queue.Full:
            # Queue full — close directly
            close_session(session_id)

        write_audit_log("SESSION_END", op_id or "UNKNOWN",
                        session_id, reason)

        # Reset session_state to neutral (ready for next operator)
        with self.session_lock:
            self.session_state.update({
                "session_id":        None,
                "operator_id":       None,
                "operator_name":     None,
                "start_time":        None,
                "demo_mode":         False,
                "baseline_ear":      0.30,
                "baseline_mar":      0.10,
                "baseline_pitch":    2.0,
                "glasses_mode":      False,
                "drowsy_at_start":   False,
                "perclos_current":   0.0,
                "fatigue_score":     0.0,
                "alert_level":       0,
                "alert_active":      False,
                "threshold_raised":  0.0,
                "ack_times":         [],
            })

        # Route through SESSION_CLOSING before WAITING_OPERATOR
        # so the state machine graph is respected.
        # From any active state → SESSION_CLOSING → WAITING_OPERATOR
        try:
            if self.sm.state not in (SystemState.WAITING_OPERATOR,
                                     SystemState.STARTUP,
                                     SystemState.SHUTTING_DOWN):
                self.sm.transition(SystemState.SESSION_CLOSING)
            self.sm.transition(SystemState.WAITING_OPERATOR)
        except Exception as e:
            print(f"[MGR] State transition warning: {e}")
            # Force state reset if transition path is unexpected
            self.sm._state = SystemState.WAITING_OPERATOR

        self._cal_manager = None

        print(f"[MGR]  Session closed. System ready for next operator.")

        # Notify UI to show login screen again
        if self.on_session_ended_callback:
            self.on_session_ended_callback()

    
   