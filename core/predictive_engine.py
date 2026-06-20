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

    
    # PREDICTION COMPUTATION
    # Done on the  linear regression  
   

    def _compute_prediction(self):
        
        if len(self._trend_buffer) < PREDICTION_MIN_SAMPLES:
            return None

        samples = list(self._trend_buffer)
        t0      = samples[0][0]

        # Relative timestamps in seconds from first sample
        t_arr = np.array([s[0] - t0 for s in samples])
        y_arr = np.array([s[1] for s in samples])

        # Linear regression: y = slope * t + intercept
        try:
            coeffs  = np.polyfit(t_arr, y_arr, 1)
            slope   = coeffs[0]      # fatigue score change per second
            intercept = coeffs[1]

            # R² — how well does the line fit the data?
            y_pred  = np.polyval(coeffs, t_arr)
            ss_res  = np.sum((y_arr - y_pred) ** 2)
            ss_tot  = np.sum((y_arr - np.mean(y_arr)) ** 2)
            r2      = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        except Exception:
            return None

        # If slope is flat or decreasing — fatigue is NOT building
        if slope <= 0:
            return None

        # Extrapolate: when does score hit L1 threshold?
        # score(t) = slope * t_elapsed + current_score
        # L1 = slope * eta + current_score
        # eta = (L1 - current_score) / slope
        current_score = y_arr[-1]
        if current_score >= FATIGUE_L1_THRESH:
            # Already at L1 — not a prediction, it's happening now
            return None

        t_elapsed_now = t_arr[-1]   # seconds since first sample
        eta_from_first = (FATIGUE_L1_THRESH - intercept) / slope
        eta_from_now   = eta_from_first - t_elapsed_now

        if eta_from_now < 0:
            # Regression says threshold already passed — score noise
            return None

        # Gate on R² — only return prediction if trend is sustained
        if r2 < PREDICTION_MIN_R2:
            return None

        return (eta_from_now, slope, r2)

    
    # WARNING EVENT
    # put a PREDICTIVE_WARNING event into alert_queue and db_queue.

    def _fire_warning(self, eta_sec: float, slope: float,
                      r2: float, current_score: float, session_id: str):
        
        self._last_warning_time = time.time()

        eta_min = eta_sec / 60.0
        print(f"\n[PREDICT] PREDICTIVE WARNING")
        print(f"          Fatigue predicted in ~{eta_min:.0f} minutes")
        print(f"          Trend slope={slope*60:.4f}/min  R²={r2:.2f}  "
              f"Current score={current_score:.3f}")

        event = {
            "type":          "PREDICTIVE_WARNING",
            "eta_sec":       round(eta_sec),
            "eta_min":       round(eta_min, 1),
            "slope_per_min": round(slope * 60, 4),
            "r2":            round(r2, 3),
            "current_score": round(current_score, 3),
            "session_id":    session_id,
            "timestamp":     time.time(),
        }

        # Into alert_queue — AlertEngine will play a soft chime
        try:
            self.alert_queue.put_nowait(event)
        except Exception:
            pass

        # Into db_queue — StorageEngine logs it
        try:
            self.db_queue.put_nowait({
                "action": "INSERT_EVENT",
                "data": {
                    "type":          "PREDICTIVE_WARNING",
                    "session_id":    session_id,
                    "fatigue_score": current_score,
                    "timestamp":     time.time(),
                    "alert_level":   0,   # not a fatigue alert level
                }
            })
        except Exception:
            pass

        write_audit_log(
            "PREDICTIVE_WARNING", "SYSTEM", session_id,
            f"ETA={eta_min:.0f}min slope={slope*60:.4f}/min R²={r2:.2f}"
        )

    
    # STATUS (polled by dashboard for trend arrow)
    # Based on the last 10 min data of the user 
   

    def get_trend_status(self) -> dict:
    
        slope = self._last_slope
        r2    = self._last_r2
        eta   = self._last_prediction_sec

        if r2 < 0.4 or len(self._trend_buffer) < PREDICTION_MIN_SAMPLES:
            direction = "stable"
        elif slope > 0.0005:
            direction = "rising"
        elif slope < -0.0005:
            direction = "falling"
        else:
            direction = "stable"

        return {
            "direction":       direction,
            "slope_per_min":   round(slope * 60, 4),
            "r2":              round(r2, 3),
            "eta_sec":         round(eta) if eta else None,
            "eta_min":         round(eta / 60, 1) if eta else None,
            "samples":         len(self._trend_buffer),
            "warning_active":  (
                eta is not None
                and eta <= PREDICTION_HORIZON_SEC
                and r2 >= PREDICTION_MIN_R2
                and slope > 0
            ),
        }

    def reset(self):
        """Call when a new operator session starts — clears trend buffer."""
        self._trend_buffer.clear()
        self._last_warning_time   = 0.0
        self._last_prediction_sec = None
        self._last_r2             = 0.0
        self._last_slope          = 0.0
        print("[PREDICT] Trend buffer reset for new session.")