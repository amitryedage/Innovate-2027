
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

    
    