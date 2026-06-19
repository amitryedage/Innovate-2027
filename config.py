# config.py — Single source of truth for ALL constants
# Rule: Never hardcode any value anywhere else in the project
# If you need to change a threshold, change it HERE only
# We can update the any value here we don't hardcode any value in actual code 

import os


BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.path.join(BASE_DIR, "data")
CLIPS_DIR       = os.path.join(DATA_DIR, "clips")
REPORTS_DIR     = os.path.join(DATA_DIR, "reports")
ASSETS_DIR      = os.path.join(BASE_DIR, "assets")
AUDIO_DIR       = os.path.join(ASSETS_DIR, "audio")
DB_PATH         = os.path.join(DATA_DIR, "fatigue.db")


# CAMERA SETTINGS

CAMERA_INDEX    = 0          # 0 = default webcam
FRAME_WIDTH     = 480        # resize width before AI processing
FRAME_HEIGHT    = 360        # resize height before AI processing
FPS_TARGET      = 30         # target frames per second
CLAHE_CLIP      = 2.0        # CLAHE contrast limit
CLAHE_GRID      = (8, 8)     # CLAHE tile grid size
LOW_LIGHT_THRESH = 30        # mean pixel brightness below this = dark cab
CRITICAL_LIGHT_THRESH = 15   # below this = log LOW_LIGHT event


MAX_FACES           = 1
DETECTION_CONFIDENCE = 0.7
TRACKING_CONFIDENCE  = 0.7

# MediaPipe landmark indices — DO NOT CHANGE
# Left eye landmarks
LEFT_EYE  = [33, 160, 158, 133, 153, 144]
# Right eye landmarks
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
# Mouth landmarks
MOUTH     = [61, 291, 13, 14, 78, 95, 308, 324]
# Head pose reference landmarks
HEAD_POSE = [1, 33, 263, 61, 291, 199]


# EMA SMOOTHING

EMA_ALPHA = 0.15   # smoothing factor — do not change without testing
                   # 0.15 = heavy smoothing, kills cab vibration jitter
                   # lower = more smoothing, higher = more responsive


# =============================================================
# config.py — Single source of truth for ALL constants
# Rule: Never hardcode any value anywhere else in the project
# If you need to change a threshold, change it HERE only
# =============================================================

import os

# -------------------------------------------------------------
# PROJECT PATHS
# -------------------------------------------------------------
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.path.join(BASE_DIR, "data")
CLIPS_DIR       = os.path.join(DATA_DIR, "clips")
REPORTS_DIR     = os.path.join(DATA_DIR, "reports")
ASSETS_DIR      = os.path.join(BASE_DIR, "assets")
AUDIO_DIR       = os.path.join(ASSETS_DIR, "audio")
DB_PATH         = os.path.join(DATA_DIR, "fatigue.db")

# -------------------------------------------------------------
# CAMERA SETTINGS
# -------------------------------------------------------------
CAMERA_INDEX    = 0          # 0 = default webcam
FRAME_WIDTH     = 480        # resize width before AI processing
FRAME_HEIGHT    = 360        # resize height before AI processing
FPS_TARGET      = 30         # target frames per second
CLAHE_CLIP      = 2.0        # CLAHE contrast limit
CLAHE_GRID      = (8, 8)     # CLAHE tile grid size
LOW_LIGHT_THRESH = 30        # mean pixel brightness below this = dark cab
CRITICAL_LIGHT_THRESH = 15   # below this = log LOW_LIGHT event

# -------------------------------------------------------------
# MEDIAPIPE SETTINGS
# -------------------------------------------------------------
MAX_FACES           = 1
DETECTION_CONFIDENCE = 0.7
TRACKING_CONFIDENCE  = 0.7

# MediaPipe landmark indices — DO NOT CHANGE
# Left eye landmarks
LEFT_EYE  = [33, 160, 158, 133, 153, 144]
# Right eye landmarks
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
# Mouth landmarks
MOUTH     = [61, 291, 13, 14, 78, 95, 308, 324]
# Head pose reference landmarks
HEAD_POSE = [1, 33, 263, 61, 291, 199]

# -------------------------------------------------------------
# EMA SMOOTHING
# -------------------------------------------------------------
EMA_ALPHA = 0.15   # smoothing factor — do not change without testing
                   # 0.15 = heavy smoothing, kills cab vibration jitter
                   # lower = more smoothing, higher = more responsive

# -------------------------------------------------------------
# FEATURE THRESHOLDS (absolute — used during calibration only)
# -------------------------------------------------------------
EAR_OPEN_NORMAL     = 0.30   # population average for open eyes
EAR_GLASSES_THRESH  = 0.20   # below this during calibration = glasses mode
EAR_DROWSY_BASELINE = 0.255  # 15% below population avg = possibly tired at start

# MAR thresholds
MAR_RESTING_MAX     = 0.35   # normal resting mouth
MAR_YAWN_THRESH     = 0.60   # above this for YAWN_DURATION = yawn detected
YAWN_DURATION_SEC   = 3.0    # yawn must sustain this long

# Head pose thresholds
PITCH_DROOP_THRESH  = 15.0   # degrees — head drooping forward
PITCH_DROOP_SEC     = 3.0    # must sustain this long

# -------------------------------------------------------------
# FEATURE WEIGHTS — normal mode vs glasses mode
# -------------------------------------------------------------
WEIGHT_EAR_NORMAL   = 1.0    # EAR is primary signal in normal mode
WEIGHT_MAR_NORMAL   = 0.3    # MAR is secondary
WEIGHT_PITCH_NORMAL = 0.3    # pitch is secondary

WEIGHT_EAR_GLASSES  = 0.40   # EAR less reliable with glasses
WEIGHT_MAR_GLASSES  = 0.35   # MAR becomes primary
WEIGHT_PITCH_GLASSES= 0.35   # pitch becomes primary

# -------------------------------------------------------------
# PERCLOS ENGINE
# -------------------------------------------------------------
PERCLOS_WINDOW_SEC  = 60     # rolling window duration in seconds
PERCLOS_WINDOW_FRAMES = PERCLOS_WINDOW_SEC * FPS_TARGET  # = 1800 frames

# Fatigue score thresholds — percentage ABOVE personal baseline
# Example: baseline PERCLOS = 8%, L1 fires when current PERCLOS >= 8 * 1.15 = 9.2%
FATIGUE_L1_THRESH   = 0.15   # +15% above baseline = mild
FATIGUE_L2_THRESH   = 0.25   # +25% above baseline = moderate
FATIGUE_L3_THRESH   = 0.40   # +40% above baseline = critical

# Shift-time threshold tightening multipliers
# Same drowsiness is MORE dangerous late in shift
SHIFT_HOUR_4_MULT   = 0.90   # thresholds tighten 10% after hour 4
SHIFT_HOUR_6_MULT   = 0.80   # thresholds tighten 20% after hour 6

# -------------------------------------------------------------
# ALERT SYSTEM
# -------------------------------------------------------------
DEBOUNCE_FRAMES     = 3      # consecutive frames needed to confirm fatigue
                              # at 30fps = 100ms — eliminates single-frame noise

# Alert cooldown — prevent alert spam
COOLDOWN_L1_SEC     = 60     # minimum 60s between L1 alerts
COOLDOWN_L2_SEC     = 120    # minimum 120s between L2 alerts
COOLDOWN_L3_SEC     = 30     # L3 repeats every 30s until acked — dangerous!

ACK_TIMEOUT_SEC     = 30     # operator must ack within 30s or alert escalates

# False alert learning — safety ceiling
FAST_ACK_TIME_SEC   = 2.0    # ack faster than this = possibly false alert
FAST_ACK_COUNT      = 3      # 3 fast acks in a row triggers threshold raise
FAST_ACK_RAISE      = 0.02   # raise EAR threshold by this amount
FAST_ACK_MAX_RAISE  = 0.05   # HARD CEILING — never raise more than this total
                              # Safety: prevents system becoming too lenient

# -------------------------------------------------------------
# FACE LOSS POLICY
# -------------------------------------------------------------
FACE_LOSS_PAUSE_SEC     = 3.0    # < 3s = pause PERCLOS, no action
FACE_LOSS_LOG_SEC       = 10.0   # 3-10s = log FACE_LOSS event
FACE_LOSS_ALERT_SEC     = 30.0   # > 30s = camera obstruction alert

# -------------------------------------------------------------
# CALIBRATION
# -------------------------------------------------------------
CALIBRATION_DURATION_SEC    = 120    # 2 minutes = 3600 frames
RECALIBRATE_AFTER_DAYS      = 7      # re-run calibration if last baseline >7 days old
CALIBRATION_MIN_FACE_PCT    = 0.80   # minimum 80% face detection for valid calibration
CALIBRATION_RETRY_LIMIT     = 1      # retry once before aborting
CALIBRATION_PROMPT_FACE_PCT = 0.60   # below this = voice prompt to look at camera

# Demo mode overrides — DO NOT USE ON REAL SITE
DEMO_CALIBRATION_SEC        = 10     # calibration completes in 10s in demo mode
DEMO_L1_THRESH              = 0.05   # much tighter threshold for fast demo alerts

# -------------------------------------------------------------
# STORAGE
# -------------------------------------------------------------
CLIP_DURATION_PRE_SEC   = 5      # seconds before event to include in clip
CLIP_DURATION_POST_SEC  = 5      # seconds after event to include in clip
CLIP_WIDTH              = 640    # clip resolution width
CLIP_HEIGHT             = 480    # clip resolution height
CLIP_FPS                = 15     # clip frame rate (lower = smaller file)

STORAGE_MIN_MB          = 500    # skip clip save if less than this free
STORAGE_WARN_MB         = 100    # stop all clips if less than this free

CLIP_RETENTION_DAYS     = 7      # auto-delete clips older than this
EVENT_RETENTION_DAYS    = 30     # auto-delete event records older than this
AUDIT_RETENTION_DAYS    = 90     # auto-delete audit records older than this

