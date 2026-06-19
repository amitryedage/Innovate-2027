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


