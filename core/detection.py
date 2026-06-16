import threading
import time
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
    MAR_YAWN_THRESH,
    PITCH_DROOP_THRESH,
    PERCLOS_WINDOW_FRAMES,
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
HEAD_POSE_INDICES = [1, 152, 33, 263, 61, 291] #As per the facemesh model need to adjust if model is changed 


class DetectionThread(threading.Thread):
    

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

        # Face loss tracking
        self.face_loss_frames  = 0
        self.face_loss_logged  = False
        self.face_loss_alerted = False

        # Pre-event clip buffer (last 5s of frames)
        self.clip_buffer = deque(maxlen=150)

        # Checkpoint timer
        self.last_checkpoint = time.time()

        # FPS tracking
        self.fps            = 0
        self.frame_count    = 0
        self.fps_start_time = time.time()

        print("[T1] DetectionThread initialized.")

    
    # MAIN RUN LOOP
    #Entry point for the detection thread. Initializes camera and landmarker, then enters main loop to process frames until shutdown.
    def run(self):
        print("[T1] Starting...")
        if not self._init_camera():
            print("[T1]  Camera failed. Exiting.")
            return
        if not self._init_landmarker():
            print("[T1]  FaceLandmarker failed. Exiting.")
            return
        print("[T1] Ready. Detection loop running.")
        try:
            while not self.shutdown_event.is_set():
                self._process_frame()
        except Exception as e:
            import traceback
            print(f"[T1] Error: {e}")
            traceback.print_exc()
        finally:
            self._cleanup()

    
    # INIT (Camera, Landmarker)

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
        print(f"[T1] Camera {w}x{h} @ {FPS_TARGET}fps")
        return True

    def _init_landmarker(self) -> bool:
        model_path = os.path.abspath(MODEL_PATH)
        if not os.path.exists(model_path):
            print(f"[T1] Model file not found: {model_path}")
            print("[T1]    Run:  python scripts/download_model.py")
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
            print(f"[T1]  FaceLandmarker init error: {e}")
            return False

    
    # FRAME PROCESSING(Preprocess → Landmarker → Features → EMA → PERCLOS → Alert Decision)
   
    def _process_frame(self):
        ret, frame = self.cap.read()
        if not ret or frame is None:
            time.sleep(0.033)
            return

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
            self._update_fps()
            return

        # Run FaceLandmarker
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB,
                            data=cv2.cvtColor(processed, cv2.COLOR_BGR2RGB))
        result = self.landmarker.detect(mp_image)

        # No face detected
        if not result.face_landmarks:
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

    
    # PREPROCESSING (Resize, CLAHE for low light, brightness tracking,need to make the improvement for real world env)
    
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
        """Eye Aspect Ratio from 6 landmarks per eye."""
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
        angles, *_ = cv2.RQDecomp3x3(rmat)
        return abs(angles[0] * 360)

   
    # PERCLOS ENGINE(Calculates PERCLOS fatigue score based on rolling window of EAR values.)
    def _update_perclos(self, glasses_mode: bool) -> float:
        ear_threshold = 0.22 if not glasses_mode else 0.18
        self.ear_deque.append(self.smooth_ear)

        if len(self.ear_deque) < 30:
            return 0.0

        closed      = sum(1 for e in self.ear_deque if e < ear_threshold)
        perclos     = (closed / len(self.ear_deque)) * 100.0

        with self.session_lock:
            baseline_ear = self.session_state.get("baseline_ear", 0.30)
            shift_start  = self.session_state.get("start_time", time.time())

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

   
    # ALERT DECISION (Determines when to fire L1/L2/L3 alerts based on fatigue score and feature thresholds.
    # Uses a confirm_counter to require DEBOUNCE_FRAMES consecutive frames before escalating alert)

    def _alert_decision(self, fatigue_score: float,
                        ear: float, mar: float, pitch: float):
        demo = self.session_state.get("demo_mode", False)
        l1   = DEMO_L1_THRESH if demo else FATIGUE_L1_THRESH

        yawn_active  = self.smooth_mar   > MAR_YAWN_THRESH
        droop_active = self.smooth_pitch > PITCH_DROOP_THRESH

        if   fatigue_score >= FATIGUE_L3_THRESH or \
             (fatigue_score >= FATIGUE_L2_THRESH and yawn_active):
            required = 3
        elif fatigue_score >= FATIGUE_L2_THRESH or \
             (fatigue_score >= l1 and droop_active):
            required = 2
        elif fatigue_score >= l1 or yawn_active:
            required = 1
        else:
            self.confirm_counter     = 0
            self.current_alert_level = 0
            return

        self.confirm_counter += 1
        if self.confirm_counter < DEBOUNCE_FRAMES:
            return

        if required > self.current_alert_level:
            self._fire_alert(required, ear, mar, pitch, fatigue_score)

    def _fire_alert(self, level: int, ear: float, mar: float,
                    pitch: float, fatigue_score: float):
        with self.session_lock:
            session_id = self.session_state.get("session_id")

        event = {
            "type":          f"FATIGUE_L{level}",
            "level":         level,
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
            self.alert_queue.put_nowait(event)
            self.db_queue.put_nowait({"action": "INSERT_EVENT", "data": event})
            self.current_alert_level = level
            self.confirm_counter     = 0
            print(f"[T1]  ALERT L{level} | "
                  f"EAR={ear:.3f} PERCLOS={fatigue_score:.2f}")
        except Exception:
            pass

    
    # FACE LOSS (Adjust time if required )
   
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
            print("[T1]   Camera obstruction >30s")

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

    
    # LANDMARK DRAWING(Adjustable we can ajust if required according to camera position and angle)
    
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

  
    # CHECKPOINT(save intermediate state to DB every 60s — useful for crash recovery and analytics)

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

    
    # FPS(Frames Per Second) tracking for now it's 30 which is good enough for the edge device
    
    def _update_fps(self):
        self.frame_count += 1
        if self.frame_count >= 30:
            elapsed = time.time() - self.fps_start_time
            self.fps = int(self.frame_count / elapsed) if elapsed > 0 else 0
            self.frame_count    = 0
            self.fps_start_time = time.time()

    # ----------------------------------------------------------
    # CLEANUP
    # ----------------------------------------------------------
    def _cleanup(self):
        print("[T1] Releasing resources...")
        if self.cap and self.cap.isOpened():
            self.cap.release()
        if self.landmarker:
            self.landmarker.close()
        print("[T1] Thread 1 cleaned up.")