import time
import threading
import numpy as np
from collections import deque

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import (
    FPS_TARGET,
    CALIBRATION_DURATION_SEC, CALIBRATION_MIN_FACE_PCT,
    CALIBRATION_RETRY_LIMIT, CALIBRATION_PROMPT_FACE_PCT,
    EAR_GLASSES_THRESH, EAR_OPEN_NORMAL, EAR_DROWSY_BASELINE,
    DEMO_CALIBRATION_SEC,
)
from core.database import update_operator_baseline, write_audit_log


class CalibrationError(Exception):
    """Raised when calibration cannot complete successfully."""
    pass


class CalibrationManager:


    def __init__(self, session_state: dict, session_lock: threading.Lock,
                 shutdown_event: threading.Event):
        self.session_state  = session_state
        self.session_lock   = session_lock
        self.shutdown_event = shutdown_event

        # Collected samples during calibration
        self.ear_samples   = []
        self.mar_samples   = []
        self.pitch_samples = []

        # Status tracking
        self.progress_pct   = 0.0      # 0.0 to 100.0
        self.face_pct       = 0.0      # face detection rate this calibration
        self.status_message = "Starting calibration..."
        self.is_running     = False

        # Results (set after successful calibration)
        self.baseline_ear    = EAR_OPEN_NORMAL
        self.baseline_mar    = 0.10
        self.baseline_pitch  = 2.0
        self.glasses_mode    = False
        self.drowsy_at_start = False

    # MAIN ENTRY POINT ( Run the full calibration sequence.)
    

    def run(self, operator_id: str, demo_mode: bool = False) -> dict:
       
        duration = DEMO_CALIBRATION_SEC if demo_mode else CALIBRATION_DURATION_SEC
        retries  = 0

        print(f"\n[CAL] Starting {'DEMO ' if demo_mode else ''}calibration "
              f"for operator {operator_id}")
        print(f"[CAL] Duration: {duration}s | "
              f"Min face detection: {CALIBRATION_MIN_FACE_PCT*100:.0f}%")

        while retries <= CALIBRATION_RETRY_LIMIT:
            try:
                result = self._collect_baseline(duration)
                self._analyse_baseline(result)
                self._store_baseline(operator_id)
                self._update_session_state()
                print(f"[CAL]  Calibration complete for {operator_id}")
                print(f"[CAL]    baseline_EAR   = {self.baseline_ear:.3f}"
                      f"  {'(GLASSES MODE)' if self.glasses_mode else ''}")
                print(f"[CAL]    baseline_MAR   = {self.baseline_mar:.3f}")
                print(f"[CAL]    baseline_PITCH = {self.baseline_pitch:.2f}°")
                print(f"[CAL]    drowsy_at_start= {self.drowsy_at_start}")
                write_audit_log(
                    "CALIBRATION_COMPLETE", "SYSTEM",
                    self.session_state.get("session_id"),
                    f"EAR={self.baseline_ear:.3f} glasses={self.glasses_mode}"
                )
                return {
                    "baseline_ear":    self.baseline_ear,
                    "baseline_mar":    self.baseline_mar,
                    "baseline_pitch":  self.baseline_pitch,
                    "glasses_mode":    self.glasses_mode,
                    "drowsy_at_start": self.drowsy_at_start,
                }

            except CalibrationError as e:
                retries += 1
                print(f"[CAL] ⚠️  Calibration attempt {retries} failed: {e}")
                if retries <= CALIBRATION_RETRY_LIMIT:
                    self.status_message = (
                        "Calibration failed — please look at the camera. Retrying..."
                    )
                    print(f"[CAL] Retrying ({retries}/{CALIBRATION_RETRY_LIMIT})...")
                    self._clear_samples()
                    time.sleep(2)
                else:
                    raise CalibrationError(
                        f"Calibration failed after {CALIBRATION_RETRY_LIMIT+1} "
                        f"attempts. Please ensure your face is clearly visible."
                    )

    
    # SAMPLE COLLECTION
   

    def _collect_baseline(self, duration: float) -> dict:
       
        self._clear_samples()
        self.is_running      = True
        self.status_message  = "Look straight at the camera and stay relaxed..."

        start_time    = time.time()
        sample_count  = 0
        face_detected = 0
        total_samples = 0

        print(f"[CAL] Collecting samples for {duration}s...")

        while (time.time() - start_time) < duration:
            if self.shutdown_event.is_set():
                raise CalibrationError("Shutdown requested during calibration")

            elapsed   = time.time() - start_time
            self.progress_pct = min(100.0, (elapsed / duration) * 100.0)

            # Read current values from session_state
            with self.session_lock:
                ear          = self.session_state.get("current_ear",    0.0)
                mar          = self.session_state.get("current_mar",    0.0)
                pitch        = self.session_state.get("current_pitch",  0.0)
                face_present = self.session_state.get("face_detected",  False)

            total_samples += 1

            if face_present and ear > 0.05:
                # Valid sample — face detected and EAR looks real
                self.ear_samples.append(ear)
                self.mar_samples.append(mar)
                self.pitch_samples.append(pitch)
                face_detected += 1
                sample_count  += 1

            # Compute current face detection rate
            if total_samples > 0:
                self.face_pct = face_detected / total_samples

            # Prompt operator if face detection drops too low
            if (total_samples > 30 and
                    self.face_pct < CALIBRATION_PROMPT_FACE_PCT and
                    total_samples % 30 == 0):
                self.status_message = (
                    "Please look directly at the camera..."
                )
                print(f"[CAL]   Low face detection "
                      f"({self.face_pct*100:.0f}%) — prompting operator")

            # Sample at ~10Hz (enough for good statistics)
            time.sleep(0.1)

        self.is_running = False

        # Validate we have enough samples
        if self.face_pct < CALIBRATION_MIN_FACE_PCT:
            raise CalibrationError(
                f"Face detected only {self.face_pct*100:.0f}% of calibration time. "
                f"Need {CALIBRATION_MIN_FACE_PCT*100:.0f}% minimum."
            )

        if len(self.ear_samples) < 20:
            raise CalibrationError(
                f"Too few valid samples ({len(self.ear_samples)}). "
                f"Ensure good lighting and face visibility."
            )

        return {
            "ear_samples":   self.ear_samples,
            "mar_samples":   self.mar_samples,
            "pitch_samples": self.pitch_samples,
            "face_pct":      self.face_pct,
            "sample_count":  sample_count,
        }

    
    # BASELINE ANALYSIS
    

    def _analyse_baseline(self, result: dict):
       
        ear_arr   = np.array(result["ear_samples"])
        mar_arr   = np.array(result["mar_samples"])
        pitch_arr = np.array(result["pitch_samples"])

        
        raw_ear_baseline   = float(np.percentile(ear_arr,   75))
        raw_mar_baseline   = float(np.median(mar_arr))
        raw_pitch_baseline = float(np.median(pitch_arr))

        print(f"[CAL] Raw baseline:  EAR={raw_ear_baseline:.3f}  "
              f"MAR={raw_mar_baseline:.3f}  PITCH={raw_pitch_baseline:.2f}°")
        print(f"[CAL] Sample stats:  "
              f"EAR min={ear_arr.min():.3f} max={ear_arr.max():.3f} "
              f"std={ear_arr.std():.3f}")

        
        # GLASSES DETECTION
        # If baseline EAR is abnormally low → operator wears glasses
        # Reflective lenses cause MediaPipe to under-read eye openness
        # In glasses mode, we rely more on MAR and head pitch for fatigue detection
        self.glasses_mode = raw_ear_baseline < EAR_GLASSES_THRESH
        if self.glasses_mode:
            print(f"[CAL]  GLASSES MODE activated "
                  f"(baseline EAR {raw_ear_baseline:.3f} < {EAR_GLASSES_THRESH})")
            # In glasses mode, MAR and pitch become primary signals
            # Do not adjust baseline — detection.py handles the weighting

        
        # DROWSY BASELINE DETECTION(Reserch insight from real-world testing)
        # If operator starts shift already fatigued, their baseline EAR
        # will be lower than population average.
        # We blend their personal baseline with population average
        # to avoid the system being too lenient from the start.
    
        population_avg_ear = EAR_OPEN_NORMAL   # 0.30

        if raw_ear_baseline < EAR_DROWSY_BASELINE:  # < 0.255
            self.drowsy_at_start = True
            blend_weight = 0.70   # 70% personal, 30% population
            blended_ear  = (blend_weight * raw_ear_baseline +
                            (1 - blend_weight) * population_avg_ear)
            print(f"[CAL]  DROWSY BASELINE detected "
                  f"(EAR {raw_ear_baseline:.3f} < {EAR_DROWSY_BASELINE})")
            print(f"[CAL]    Blending: {raw_ear_baseline:.3f} * 0.7 + "
                  f"{population_avg_ear} * 0.3 = {blended_ear:.3f}")
            self.baseline_ear = blended_ear
        else:
            self.drowsy_at_start = False
            self.baseline_ear    = raw_ear_baseline

        self.baseline_mar   = raw_mar_baseline
        self.baseline_pitch = raw_pitch_baseline

        # Sanity bounds — clamp to realistic ranges(Calibration should never produce values outside these or else something went very wrong)
        self.baseline_ear   = float(np.clip(self.baseline_ear,   0.15, 0.45))
        self.baseline_mar   = float(np.clip(self.baseline_mar,   0.02, 0.40))
        self.baseline_pitch = float(np.clip(self.baseline_pitch, 0.0,  20.0))

 
    # STORE TO DATABASE
    #Store the computed baseline values in the operators table for this operator_id.

    def _store_baseline(self, operator_id: str):
        
        update_operator_baseline(
            operator_id    = operator_id,
            baseline_ear   = self.baseline_ear,
            baseline_mar   = self.baseline_mar,
            baseline_pitch = self.baseline_pitch,
            glasses_mode   = self.glasses_mode,
        )
        print(f"[CAL] Baseline stored to DB for operator {operator_id}")

    
    # UPDATE SESSION STATE
 
    # Push baseline values into shared session_state.
    #Thread 1 (detection) reads these to compute fatigue score.
    def _update_session_state(self):
        
        with self.session_lock:
            self.session_state["baseline_ear"]    = self.baseline_ear
            self.session_state["baseline_mar"]    = self.baseline_mar
            self.session_state["baseline_pitch"]  = self.baseline_pitch
            self.session_state["glasses_mode"]    = self.glasses_mode
            self.session_state["drowsy_at_start"] = self.drowsy_at_start

        print("[CAL] Session state updated with personal baseline.")

    # HELPERS functions to reset samples and get progress for UI display
    def _clear_samples(self):
        self.ear_samples   = []
        self.mar_samples   = []
        self.pitch_samples = []
        self.progress_pct  = 0.0
        self.face_pct      = 0.0

    def get_progress(self) -> dict:
        return {
            "progress_pct":   self.progress_pct,
            "face_pct":       self.face_pct * 100,
            "status_message": self.status_message,
            "is_running":     self.is_running,
            "sample_count":   len(self.ear_samples),
        }