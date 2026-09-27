import threading
import time
import traceback
import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from collections import deque
from scipy.spatial import distance as dist

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import (
    CAMERA_INDEX, FRAME_WIDTH, FRAME_HEIGHT, FPS_TARGET,
    CLAHE_CLIP, CLAHE_GRID, LOW_LIGHT_THRESH, CRITICAL_LIGHT_THRESH,
    MAX_FACES, DETECTION_CONFIDENCE, TRACKING_CONFIDENCE,
    LEFT_EYE, RIGHT_EYE, MOUTH,
    EMA_ALPHA,
    EAR_GLASSES_THRESH, EAR_OPEN_NORMAL,
    EAR_CLOSED_NORMAL, EAR_CLOSED_GLASSES,
    MAR_YAWN_THRESH, YAWN_DURATION_SEC,
    PITCH_DROOP_THRESH, PITCH_DROOP_SEC,
    PERCLOS_WINDOW_FRAMES, PERCLOS_MIN_FRAMES,
    FATIGUE_L1_THRESH, FATIGUE_L2_THRESH, FATIGUE_L3_THRESH,
    SHIFT_HOUR_4_MULT, SHIFT_HOUR_6_MULT,
    DEBOUNCE_FRAMES,
    FACE_LOSS_LOG_SEC, FACE_LOSS_ALERT_SEC,
    CHECKPOINT_INTERVAL_SEC,
    DEMO_L1_THRESH,
)
from core.state_machine import StateMachine, SystemState

# Path to the downloaded face landmarker model
MODEL_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'assets', 'face_landmarker.task'
)


# 3D FACE MODEL for head pose solvePnP

FACE_3D_MODEL = np.array([
    [0.0,    0.0,    0.0   ],
    [0.0,   -330.0, -65.0  ],
    [-225.0, 170.0, -135.0 ],
    [225.0,  170.0, -135.0 ],
    [-150.0,-150.0, -125.0 ],
    [150.0, -150.0, -125.0 ],
], dtype=np.float64)

# Landmark indices for head pose (nose, chin, left eye, right eye, mouth corners)
HEAD_POSE_INDICES = [1, 152, 33, 263, 61, 291]


class DetectionThread(threading.Thread):
    """
    Thread 1 — Full detection pipeline.
    Camera → preprocess → FaceLandmarker → EAR/MAR/pitch
    → EMA → PERCLOS → alert decision → queues
    """

    def __init__(self, state_machine, frame_buffer, frame_lock,
                 alert_queue, db_queue, ack_event, shutdown_event,
                 session_state, session_lock):
        super().__init__(name="DetectionThread", daemon=True)

        self.sm             = state_machine
        self.frame_buffer   = frame_buffer
        self.frame_lock     = frame_lock
        self.alert_queue    = alert_queue
        self.db_queue       = db_queue
        self.ack_event      = ack_event
        self.shutdown_event = shutdown_event
        self.session_state  = session_state
        self.session_lock   = session_lock

        # Camera + CLAHE
        self.cap   = None
        self.clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP,
                                      tileGridSize=CLAHE_GRID)
        # MediaPipe landmarker
        self.landmarker = None

        # EMA smoothed values
        self.smooth_ear   = EAR_OPEN_NORMAL
        self.smooth_mar   = 0.10
        self.smooth_pitch = 0.0

        # PERCLOS rolling window
        self.ear_deque = deque(maxlen=PERCLOS_WINDOW_FRAMES)

        # Alert state
        self.confirm_counter     = 0
        self.current_alert_level = 0

        # Consecutive frames of open mouth / drooped head
        self.yawn_frames  = 0
        self.droop_frames = 0

        # Face loss tracking
        self.face_loss_frames  = 0
        self.face_loss_logged  = False
        self.face_loss_alerted = False

        # Pre-event clip buffer (last 5s of frames)
        self.clip_buffer = deque(maxlen=150)

        # Checkpoint timer
        self.last_checkpoint = time.time()

        # Per-frame error counts (so one bad frame can't stop monitoring)
        self._frame_errors  = {}

        # FPS tracking
        self.fps            = 0
        self.frame_count    = 0
        self.fps_start_time = time.time()

        print("[T1] DetectionThread initialized.")

    
    # MAIN RUN LOOP
    # Entry point 
    def run(self):
        print("[T1] Starting...")
        # Initial heartbeat — lets the watchdog catch a camera that never opens
        with self.session_lock:
            self.session_state["last_frame_time"] = time.time()
        if not self._init_camera():
            print("[T1]  Camera failed. Exiting.")
            return
        if not self._init_landmarker():
            print("[T1]  FaceLandmarker failed. Exiting.")
            return
        print("[T1]  Ready. Detection loop running.")
        try:
            while not self.shutdown_event.is_set():
                try:
                    self._process_frame()
                except Exception as e:
                    # One bad frame must not leave the operator unmonitored
                    self._log_frame_error(e)
                    time.sleep(0.033)
        finally:
            self._cleanup()

    def _log_frame_error(self, e: Exception):
        key = f"{type(e).__name__}: {e}"
        n   = self._frame_errors.get(key, 0) + 1
        self._frame_errors[key] = n
        if n == 1:
            print(f"[T1]  Frame error (continuing): {key}")
            traceback.print_exc()
        elif n % 300 == 0:
            print(f"[T1]  Frame error repeated {n}x: {key}")

    
    # INIT
    
    def _init_camera(self) -> bool:
        print(f"[T1] Opening camera {CAMERA_INDEX}...")
        self.cap = cv2.VideoCapture(CAMERA_INDEX)
        if not self.cap.isOpened():
            return False
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap.set(cv2.CAP_PROP_FPS, FPS_TARGET)
        ret, _ = self.cap.read()
        if not ret:
            return False
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[T1]  Camera {w}x{h} @ {FPS_TARGET}fps")
        return True

    def _init_landmarker(self) -> bool:
        """
        Initialize MediaPipe FaceLandmarker using new Tasks API.
        Requires face_landmarker.task model file in assets/ folder.
        Download it once by running:
            uv run scripts/download_model.py
        """
        model_path = os.path.abspath(MODEL_PATH)
        if not os.path.exists(model_path):
            print(f"[T1]  Model file not found: {model_path}")
            print("[T1]    Run:  uv run scripts/download_model.py")
            return False

        print(f"[T1] Loading FaceLandmarker model from {model_path}...")
        try:
            base_opts = mp_python.BaseOptions(model_asset_path=model_path)
            opts = mp_vision.FaceLandmarkerOptions(
                base_options                  = base_opts,
                num_faces                     = MAX_FACES,
                min_face_detection_confidence = DETECTION_CONFIDENCE,
                min_face_presence_confidence  = DETECTION_CONFIDENCE,
                min_tracking_confidence       = TRACKING_CONFIDENCE,
            )
            self.landmarker = mp_vision.FaceLandmarker.create_from_options(opts)
            print("[T1]  FaceLandmarker loaded.")
            return True
        except Exception as e:
            print(f"[T1] FaceLandmarker init error: {e}")
            return False

    
    # FRAME PROCESSING
    # Process each frame one by one 
    def _process_frame(self):
        ret, frame = self.cap.read()
        if not ret or frame is None:
            time.sleep(0.033)
            return

        # Heartbeat for DetectionWatchdog
        with self.session_lock:
            self.session_state["last_frame_time"] = time.time()

        # Preprocess
        processed = self._preprocess(frame)

        # Save to clip buffer
        self.clip_buffer.append(frame.copy())

        # Write to frame_buffer for UI
        with self.frame_lock:
            self.frame_buffer["frame"] = processed.copy()
            self.frame_buffer["raw"]   = frame.copy()

        # Only run detection when monitoring or calibrating
        if not self.sm.is_monitoring_active() and \
           self.sm.state != SystemState.CALIBRATING:
            self._clear_annotated()
            self._update_fps()
            return

        # Run FaceLandmarker
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB,
                            data=cv2.cvtColor(processed, cv2.COLOR_BGR2RGB))
        result = self.landmarker.detect(mp_image)

        # No face detected
        if not result.face_landmarks:
            self._clear_annotated()
            self._handle_face_loss()
            self._update_fps()
            return

        # Face found
        self._reset_face_loss()
        landmarks = result.face_landmarks[0]   # list of NormalizedLandmark
        h, w = processed.shape[:2]

        # Extract features
        raw_ear   = self._calculate_ear(landmarks, w, h)
        raw_mar   = self._calculate_mar(landmarks, w, h)
        raw_pitch = self._calculate_head_pitch(landmarks, w, h)

        # EMA smoothing
        self.smooth_ear   = EMA_ALPHA * raw_ear   + (1-EMA_ALPHA) * self.smooth_ear
        self.smooth_mar   = EMA_ALPHA * raw_mar   + (1-EMA_ALPHA) * self.smooth_mar
        self.smooth_pitch = EMA_ALPHA * raw_pitch + (1-EMA_ALPHA) * self.smooth_pitch

        # Draw landmarks on frame
        annotated = self._draw_landmarks(processed, landmarks, w, h)
        with self.frame_lock:
            self.frame_buffer["annotated"] = annotated

        # Update session_state metrics for UI
        with self.session_lock:
            self.session_state["current_ear"]   = round(self.smooth_ear,   3)
            self.session_state["current_mar"]   = round(self.smooth_mar,   3)
            self.session_state["current_pitch"] = round(self.smooth_pitch, 2)
            self.session_state["fps"]           = self.fps
            self.session_state["face_detected"] = True

        # Skip PERCLOS during calibration
        if self.sm.state == SystemState.CALIBRATING:
            self._update_fps()
            return

        # PERCLOS + alert
        glasses_mode  = self.session_state.get("glasses_mode", False)
        fatigue_score = self._update_perclos(glasses_mode)
        self._alert_decision(fatigue_score, raw_ear, raw_mar, raw_pitch)

        # 60s checkpoint
        if time.time() - self.last_checkpoint >= CHECKPOINT_INTERVAL_SEC:
            self._write_checkpoint()
            self.last_checkpoint = time.time()

        self._update_fps()

    
    # PREPROCESSING
    # preprocess every frame 
    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))
        gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_brightness = float(np.mean(gray))

        if mean_brightness < LOW_LIGHT_THRESH:
            lab      = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l, a, b  = cv2.split(lab)
            l        = self.clahe.apply(l)
            frame    = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

        with self.session_lock:
            self.session_state["brightness"] = round(mean_brightness, 1)
        return frame

   
    # FEATURE EXTRACTION
    def _calculate_ear(self, landmarks, w: int, h: int) -> float:
        """Eye Aspect Ratio from 6 landmarks per eye.""" # Facemesh algorithum
        def eye_ear(indices):
            pts = [(landmarks[i].x * w, landmarks[i].y * h) for i in indices]
            A = dist.euclidean(pts[1], pts[5])
            B = dist.euclidean(pts[2], pts[4])
            C = dist.euclidean(pts[0], pts[3])
            return (A + B) / (2.0 * C) if C > 0 else 0.30

        return (eye_ear(LEFT_EYE) + eye_ear(RIGHT_EYE)) / 2.0

    def _calculate_mar(self, landmarks, w: int, h: int) -> float:
        """Mouth Aspect Ratio — yawn detection."""
        pts = [(landmarks[i].x * w, landmarks[i].y * h) for i in MOUTH]
        A = dist.euclidean(pts[1], pts[7])
        B = dist.euclidean(pts[2], pts[6])
        C = dist.euclidean(pts[3], pts[5])
        D = dist.euclidean(pts[0], pts[4])
        return (A + B + C) / (3.0 * D) if D > 0 else 0.10

    def _calculate_head_pitch(self, landmarks, w: int, h: int) -> float:
        """Head pitch angle using solvePnP. Returns degrees (positive = droop)."""
        face_2d = np.array([
            [landmarks[HEAD_POSE_INDICES[i]].x * w,
             landmarks[HEAD_POSE_INDICES[i]].y * h]
            for i in range(6)
        ], dtype=np.float64)

        focal   = float(w)
        cam_mat = np.array([[focal, 0, w/2],
                             [0, focal, h/2],
                             [0, 0, 1     ]], dtype=np.float64)
        dist_c  = np.zeros((4,1), dtype=np.float64)

        ok, rvec, _ = cv2.solvePnP(FACE_3D_MODEL, face_2d,
                                    cam_mat, dist_c,
                                    flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            return 0.0

        rmat, _ = cv2.Rodrigues(rvec)
        angles, *_ = cv2.RQDecomp3x3(rmat)   # already in degrees
        pitch = angles[0]
        # FACE_3D_MODEL is y-up but image coords are y-down, so a level
        # head decomposes to ~±180°. Fold it back around 0.
        if pitch > 90:
            pitch -= 180
        elif pitch < -90:
            pitch += 180
        return abs(pitch)

   
    # PERCLOS ENGINE
    # PERCLOS is the percentage of time eyes are closed over a rolling window.
    # Glasses-mode glare frames are filtered out so they don't count as closed.

    def _update_perclos(self, glasses_mode: bool) -> float:
        ear_threshold = EAR_CLOSED_GLASSES if glasses_mode else EAR_CLOSED_NORMAL

        # Glasses-mode glare gate:
        # If EAR drops below threshold BUT MAR and pitch are resting,
        # this is almost certainly a lens-reflection artifact, not a
        # real eye-closure event. Do NOT count this frame as closed.
        # Glare signature: EAR low + MAR < 0.35 (not yawning) + pitch < 8deg
        if glasses_mode and self.smooth_ear < ear_threshold:
            is_glare = (self.smooth_mar < 0.35 and self.smooth_pitch < 8.0)
            # Use baseline_ear as the appended value so glare frames are
            # treated as "eyes open" in the PERCLOS window
            if is_glare:
                with self.session_lock:
                    baseline = self.session_state.get("baseline_ear", 0.18)
                self.ear_deque.append(baseline)
            else:
                self.ear_deque.append(self.smooth_ear)
        else:
            self.ear_deque.append(self.smooth_ear)

        if len(self.ear_deque) < 30:
            return 0.0

        closed      = sum(1 for e in self.ear_deque if e < ear_threshold)
        perclos     = (closed / max(len(self.ear_deque), PERCLOS_MIN_FRAMES)) * 100.0

        with self.session_lock:
            baseline_ear = self.session_state.get("baseline_ear", 0.30)
            # start_time is None between sessions (key present, value None)
            shift_start  = self.session_state.get("start_time") or time.time()

        baseline_perclos = max(2.0, (1.0 - baseline_ear / 0.35) * 20.0)
        fatigue_score    = (perclos - baseline_perclos) / baseline_perclos \
                           if baseline_perclos > 0 else 0.0

        elapsed_hours = (time.time() - shift_start) / 3600.0
        if elapsed_hours >= 6:
            fatigue_score *= (1.0 / SHIFT_HOUR_6_MULT)
        elif elapsed_hours >= 4:
            fatigue_score *= (1.0 / SHIFT_HOUR_4_MULT)

        with self.session_lock:
            self.session_state["perclos_current"] = round(perclos, 2)
            self.session_state["fatigue_score"]   = round(fatigue_score, 3)

        return fatigue_score

    
    # ALERT DECISION
    # Alert levels are determined by PERCLOS, EAR, MAR, and pitch.
    def _alert_decision(self, fatigue_score: float,
                        ear: float, mar: float, pitch: float):
        demo = self.session_state.get("demo_mode", False)
        l1   = DEMO_L1_THRESH if demo else FATIGUE_L1_THRESH

        # Yawn / droop only count once sustained, so talking or a quick
        # glance down does not fire an alert (frames, like PERCLOS window)
        self.yawn_frames  = self.yawn_frames + 1  \
            if self.smooth_mar   > MAR_YAWN_THRESH    else 0
        self.droop_frames = self.droop_frames + 1 \
            if self.smooth_pitch > PITCH_DROOP_THRESH else 0

        yawn_active  = self.yawn_frames  >= YAWN_DURATION_SEC * FPS_TARGET
        droop_active = self.droop_frames >= PITCH_DROOP_SEC   * FPS_TARGET

        if   fatigue_score >= FATIGUE_L3_THRESH or \
             (fatigue_score >= FATIGUE_L2_THRESH and yawn_active):
            required = 3
        elif fatigue_score >= FATIGUE_L2_THRESH or \
             (fatigue_score >= l1 and droop_active):
            required = 2
        elif fatigue_score >= l1 or yawn_active or droop_active:
            required = 1
        else:
            required = 0

        reasons = []
        if fatigue_score >= l1: reasons.append("Eyes closing")
        if yawn_active:         reasons.append("Yawning")
        if droop_active:        reasons.append("Head drooping")
        reason = " + ".join(reasons)

        # Live condition — AlertEngine uses this to clear or escalate
        with self.session_lock:
            self.session_state["fatigue_level"]  = required
            self.session_state["fatigue_reason"] = reason

        if required == 0:
            self.confirm_counter     = 0
            self.current_alert_level = 0
            return

        self.confirm_counter += 1
        if self.confirm_counter < DEBOUNCE_FRAMES:
            return

        if required > self.current_alert_level:
            self._fire_alert(required, ear, mar, pitch, fatigue_score,
                             reason=reason)

    def _fire_alert(self, level: int, ear: float, mar: float,
                    pitch: float, fatigue_score: float, reason: str = ""):
        with self.session_lock:
            session_id = self.session_state.get("session_id")

        event = {
            "type":          f"FATIGUE_L{level}",
            "level":         level,
            "reason":        reason,
            "ear":           round(ear, 3),
            "mar":           round(mar, 3),
            "pitch":         round(pitch, 2),
            "perclos":       self.session_state.get("perclos_current", 0),
            "fatigue_score": round(fatigue_score, 3),
            "session_id":    session_id,
            "timestamp":     time.time(),
            "clip_frames":   list(self.clip_buffer),
        }
        try:
            # AlertEngine writes the event to the DB when it handles it
            self.alert_queue.put_nowait(event)
            self.current_alert_level = level
            self.confirm_counter     = 0
            print(f"[T1] 🚨 ALERT L{level} ({reason}) | "
                  f"EAR={ear:.3f} MAR={mar:.2f} PITCH={pitch:.1f} "
                  f"SCORE={fatigue_score:.2f}")
        except Exception:
            pass

    
    # FACE LOSS
    # Face not detected for >30s triggers CAMERA_OBSTRUCTION alert.
    def _handle_face_loss(self):
        self.face_loss_frames += 1
        elapsed = self.face_loss_frames / FPS_TARGET
        with self.session_lock:
            self.session_state["face_detected"] = False
            sid = self.session_state.get("session_id")

        if elapsed > FACE_LOSS_ALERT_SEC and not self.face_loss_alerted:
            try:
                self.alert_queue.put_nowait({
                    "type": "CAMERA_OBSTRUCTION", "session_id": sid
                })
            except Exception:
                pass
            self.face_loss_alerted = True
            print("[T1]  Camera obstruction >30s")

        elif elapsed > FACE_LOSS_LOG_SEC and not self.face_loss_logged:
            try:
                self.db_queue.put_nowait({
                    "action": "INSERT_EVENT",
                    "data": {"type": "FACE_LOSS",
                             "session_id": sid,
                             "timestamp": time.time()}
                })
            except Exception:
                pass
            self.face_loss_logged = True

    def _reset_face_loss(self):
        self.face_loss_frames  = 0
        self.face_loss_logged  = False
        self.face_loss_alerted = False
        with self.session_lock:
            self.session_state["face_detected"] = True

    
    # LANDMARK DRAWING
    # landmarks are drawn on the annotated frame for UI display, with EAR/MAR/pitch metrics overlaid.
    def _draw_landmarks(self, frame, landmarks, w: int, h: int) -> np.ndarray:
        out = frame.copy()
        # Eyes — green dots
        for idx in LEFT_EYE + RIGHT_EYE:
            x = int(landmarks[idx].x * w)
            y = int(landmarks[idx].y * h)
            cv2.circle(out, (x, y), 2, (0, 255, 0), -1)
        # Mouth — blue dots
        for idx in MOUTH:
            x = int(landmarks[idx].x * w)
            y = int(landmarks[idx].y * h)
            cv2.circle(out, (x, y), 2, (255, 100, 0), -1)

        # Colour by EAR value
        ear_col = (0, 0, 255) if self.smooth_ear < 0.20 else \
                  (0, 165, 255) if self.smooth_ear < 0.25 else \
                  (0, 255, 0)

        perclos = self.session_state.get("perclos_current", 0.0)
        score   = self.session_state.get("fatigue_score", 0.0)

        cv2.putText(out, f"EAR:     {self.smooth_ear:.3f}",
                    (10, 28),  cv2.FONT_HERSHEY_SIMPLEX, 0.6, ear_col, 2)
        cv2.putText(out, f"MAR:     {self.smooth_mar:.3f}",
                    (10, 54),  cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200,200,0), 2)
        cv2.putText(out, f"PITCH:   {self.smooth_pitch:.1f}deg",
                    (10, 80),  cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180,180,255), 2)
        cv2.putText(out, f"PERCLOS: {perclos:.1f}%",
                    (10, 106), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
        cv2.putText(out, f"SCORE:   {score:.3f}",
                    (10, 132), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,200,100), 2)
        cv2.putText(out, f"FPS:     {self.fps}",
                    (10, 154), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150,150,150), 1)
        return out

   
    # CHECKPOINT

    def _write_checkpoint(self):
        try:
            with self.session_lock:
                sid = self.session_state.get("session_id")
                pc  = self.session_state.get("perclos_current", 0)
                thr = self.session_state.get("threshold_raised", 0)
            if sid:
                self.db_queue.put_nowait({
                    "action":     "CHECKPOINT",
                    "session_id": sid,
                    "perclos":    pc,
                    "threshold":  thr,
                })
        except Exception:
            pass

    
    # Drop the stale landmark overlay so the UI falls back to the live frame
    def _clear_annotated(self):
        with self.frame_lock:
            self.frame_buffer["annotated"] = None

    # FPS
    
    def _update_fps(self):
        self.frame_count += 1
        if self.frame_count >= 30:
            elapsed = time.time() - self.fps_start_time
            self.fps = int(self.frame_count / elapsed) if elapsed > 0 else 0
            self.frame_count    = 0
            self.fps_start_time = time.time()

  
    # CLEANUP
    # Clean up process is applied once the work is completed
    def _cleanup(self):
        print("[T1] Releasing resources...")
        if self.cap and self.cap.isOpened():
            self.cap.release()
        if self.landmarker:
            self.landmarker.close()
        print("[T1] Thread 1 cleaned up.")