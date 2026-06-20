# USP Predictive fatigue warning
import threading
import time
import numpy as np
from collections import deque

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import (
    FATIGUE_L1_THRESH,
    PREDICTION_HORIZON_SEC,
    PREDICTION_SAMPLE_INTERVAL_SEC,
    PREDICTION_MIN_SAMPLES,
    PREDICTION_MIN_R2,
    PREDICTION_COOLDOWN_SEC,
)
from core.database import write_audit_log


class PredictiveEngine(threading.Thread):
    def __init__(self, session_state: dict, session_lock: threading.Lock,
                 alert_queue, db_queue,
                 shutdown_event: threading.Event,
                 state_machine):
        super().__init__(name="PredictiveEngine", daemon=True)

        self.session_state  = session_state
        self.session_lock   = session_lock
        self.alert_queue    = alert_queue
        self.db_queue       = db_queue
        self.shutdown_event = shutdown_event
        self.sm             = state_machine

        # Rolling trend buffer: (timestamp, fatigue_score) pairs
        # 10-minute window at 30-second samples = 20 data points max
        max_samples = int(600 / PREDICTION_SAMPLE_INTERVAL_SEC) + 5
        self._trend_buffer = deque(maxlen=max_samples)

        # State
        self._last_warning_time = 0.0
        self._last_prediction_sec = None   # last predicted ETA in seconds
        self._last_r2 = 0.0
        self._last_slope = 0.0

        print("[PREDICT] PredictiveEngine initialized.")

   
    # MAIN LOOP
    # Entry point for the current engine

    def run(self):
        from core.state_machine import SystemState
        print("[PREDICT] Predictive engine running.")

        while not self.shutdown_event.is_set():
            time.sleep(PREDICTION_SAMPLE_INTERVAL_SEC)

            # Only run during active monitoring with a real session
            if self.sm.state != SystemState.MONITORING:
                self._trend_buffer.clear()
                continue

            with self.session_lock:
                score      = self.session_state.get("fatigue_score",  0.0)
                alert_lvl  = self.session_state.get("alert_level",    0)
                session_id = self.session_state.get("session_id")
                start_time = self.session_state.get("start_time",     time.time())

            # Don't predict if an alert is already active
            if alert_lvl > 0:
                continue

            # Don't predict in first 5 minutes of shift (not enough trend data)
            shift_elapsed = time.time() - (start_time or time.time())
            if shift_elapsed < 300:
                self._trend_buffer.append((time.time(), score))
                continue

            # Add sample to trend buffer
            self._trend_buffer.append((time.time(), score))

            # Need minimum samples before predicting
            if len(self._trend_buffer) < PREDICTION_MIN_SAMPLES:
                continue

            # Run prediction
            prediction = self._compute_prediction()
            if prediction is None:
                continue

            eta_sec, slope, r2 = prediction
            self._last_prediction_sec = eta_sec
            self._last_r2    = r2
            self._last_slope = slope

            # Update session_state so dashboard can show trend arrow
            with self.session_lock:
                self.session_state["fatigue_trend_slope"]  = round(slope, 4)
                self.session_state["fatigue_trend_r2"]     = round(r2, 3)
                self.session_state["fatigue_eta_sec"]      = round(eta_sec) if eta_sec else None

            # Fire warning if ETA is within horizon and cooldown passed
            should_warn = (
                eta_sec is not None
                and eta_sec <= PREDICTION_HORIZON_SEC
                and eta_sec >= 60   # at least 1 minute away (not imminent)
                and r2 >= PREDICTION_MIN_R2
                and (time.time() - self._last_warning_time) > PREDICTION_COOLDOWN_SEC
            )

            if should_warn:
                self._fire_warning(eta_sec, slope, r2, score, session_id)

        print("[PREDICT] Predictive engine stopped.")

    