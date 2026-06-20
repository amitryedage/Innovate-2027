# risk_scorer.py — USP  Shift-level fatigue risk scoring
# Converts all the raw fatigue signals into a single number (0-100)
# that a site manager can read at a glance.
# A manager with 20 operators doesn't read 20 PDFs.
# They need: green / amber / red per operator, ranked by risk.
# This is the feature that makes your system a management tool,
# not just an alert device — a completely different budget category.
# SCORING COMPONENTS (weighted):
#   30% — Alert frequency    (how many L1/L2/L3 events per hour)
#   30% — Alert severity     (weighted by level: L1=1, L2=3, L3=10)
#   20% — Acknowledgement    (slow/no ack = higher risk)
#   10% — Predictive trend   (rising slope adds to score)
#   10% — Shift time factor  (hour 6+ of shift = multiplier)
#
# OUTPUT:
#   0-25   → GREEN  — Low risk, operator alert
#   26-50  → AMBER  — Moderate risk, monitor closely
#   51-75  → ORANGE — High risk, consider break
#   76-100 → RED    — Critical risk, stop work
import time
import threading
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.database import get_session_events, write_audit_log


class RiskScorer:
    # Score thresholds(Chnage as per requirement)
    GREEN  = 25
    AMBER  = 50
    ORANGE = 75
    RED    = 100

    # Alert severity weights
    LEVEL_WEIGHTS = {1: 1.0, 2: 3.0, 3: 10.0}

    def __init__(self, session_state: dict, session_lock: threading.Lock,
                 predictive_engine=None):
        self.session_state    = session_state
        self.session_lock     = session_lock
        self.predictive_engine = predictive_engine

        self._current_score      = 0.0
        self._current_band       = "GREEN"
        self._component_scores   = {}
        self._last_computed_time = 0.0
        self._score_history      = []   # (timestamp, score) for trend in PDF

    
    # COMPUTE SCORE
    # Compute score calculated here 
    def compute(self) -> dict:
        """
        Compute current shift risk score. Returns full breakdown dict.
        Cached for 60 seconds — call freely from UI refresh timer.
        """
        now = time.time()
        if now - self._last_computed_time < 60:
            return self._get_result()

        with self.session_lock:
            session_id  = self.session_state.get("session_id")
            start_time  = self.session_state.get("start_time",  now)
            alert_level = self.session_state.get("alert_level", 0)
            threshold_r = self.session_state.get("threshold_raised", 0.0)

        if not session_id:
            return self._zero_result()

        # Fetch all events for this session
        try:
            events = get_session_events(session_id)
        except Exception:
            return self._zero_result()

        fatigue_events = [e for e in events
                          if e.get("event_type", "").startswith("FATIGUE")]

        shift_elapsed_hours = (now - start_time) / 3600.0

        #  Component 1: Alert frequency (30%) 
        freq_score = self._score_frequency(fatigue_events, shift_elapsed_hours)

        #  Component 2: Alert severity (30%) ---
        sev_score = self._score_severity(fatigue_events, shift_elapsed_hours)

        #  Component 3: Acknowledgement quality (20%) ---
        ack_score = self._score_acknowledgement(fatigue_events)

        #  Component 4: Predictive trend (10%) 
        trend_score = self._score_trend()

        #  Component 5: Shift time factor (10%) 
        time_score = self._score_shift_time(shift_elapsed_hours)

        # Weighted sum → 0-100
        raw = (
            0.30 * freq_score +
            0.30 * sev_score  +
            0.20 * ack_score  +
            0.10 * trend_score +
            0.10 * time_score
        )

        # Clamp to 0-100
        final_score = min(100.0, max(0.0, raw))

        self._current_score    = final_score
        self._current_band     = self._band(final_score)
        self._last_computed_time = now
        self._component_scores = {
            "frequency":   round(freq_score,  1),
            "severity":    round(sev_score,   1),
            "ack_quality": round(ack_score,   1),
            "trend":       round(trend_score, 1),
            "shift_time":  round(time_score,  1),
        }
        self._score_history.append((now, final_score))

        # Write to audit log if band changed significantly
        if len(self._score_history) >= 2:
            prev = self._score_history[-2][1]
            if abs(final_score - prev) >= 15:
                write_audit_log(
                    "RISK_SCORE_CHANGE", "SYSTEM", session_id,
                    f"{prev:.0f}→{final_score:.0f} band={self._current_band}"
                )

        return self._get_result()

   
    # COMPONENT SCORERS (each returns 0-100)
    

    def _score_frequency(self, events: list, hours: float) -> float:
        """Alerts per hour → 0-100. More than 6/hour = 100."""
        """Avg of shift will also calculated"""
        if hours < 0.1:
            return 0.0
        alerts_per_hour = len(events) / hours
        # 0/hr=0, 2/hr=33, 4/hr=67, 6+/hr=100
        return min(100.0, (alerts_per_hour / 6.0) * 100.0)

    def _score_severity(self, events: list, hours: float) -> float:
        """Weighted severity score. L3 weighs 10x L1."""
        if not events:
            return 0.0

        total_weight = sum(
            self.LEVEL_WEIGHTS.get(e.get("alert_level", 0), 0)
            for e in events
        )

        # Normalize: 10 L1 events/hr = 50, 1 L3 event/hr = 50
        hours_clamped = max(hours, 0.1)
        weight_per_hour = total_weight / hours_clamped

        # Scale: 20 weight units/hr = 100
        return min(100.0, (weight_per_hour / 20.0) * 100.0)

    def _score_acknowledgement(self, events: list) -> float:
        """
        Poor ack behaviour increases risk score.
        Factors: slow acks (>20s), no acks (escalations), fast-ack pattern.
        """
        if not events:
            return 0.0

        total  = len(events)
        unacked = sum(1 for e in events if not e.get("acknowledged"))
        slow_acks = sum(
            1 for e in events
            if e.get("ack_time_sec") and e["ack_time_sec"] > 20
        )

        # Unacknowledged events = highest risk
        unack_pct = unacked / total if total > 0 else 0
        slow_pct  = slow_acks / total if total > 0 else 0

        score = (unack_pct * 80) + (slow_pct * 20)
        return min(100.0, score)

    def _score_trend(self) -> float:
        """
        Rising fatigue trend from PredictiveEngine contributes to risk.
        High slope + high R² = higher risk.
        """
        if not self.predictive_engine:
            return 0.0

        trend = self.predictive_engine.get_trend_status()
        slope = trend.get("slope_per_min", 0.0)
        r2    = trend.get("r2", 0.0)
        eta   = trend.get("eta_sec")

        if slope <= 0 or r2 < 0.4:
            return 0.0

        # Higher score if warning is imminent
        if eta and eta < 300:    # < 5 minutes
            return 80.0
        elif eta and eta < 600:  # < 10 minutes
            return 50.0
        else:
            return min(30.0, slope * 1000 * r2)

    