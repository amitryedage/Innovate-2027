import sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
 
import numpy as np
from collections import deque
from config import (
    PERCLOS_WINDOW_FRAMES, FPS_TARGET,
    FATIGUE_L1_THRESH, FATIGUE_L2_THRESH, FATIGUE_L3_THRESH,
    SHIFT_HOUR_4_MULT, SHIFT_HOUR_6_MULT,
    EAR_OPEN_NORMAL, EAR_DROWSY_BASELINE, EAR_GLASSES_THRESH,
    CALIBRATION_MIN_FACE_PCT,
)
from core.calibration import CalibrationManager, CalibrationError
 
passed = 0
failed = 0
 
def check(name, condition, expected=None, got=None):
    global passed, failed
    if condition:
        print(f"   {name}")
        passed += 1
    else:
        print(f"   {name}")
        if expected is not None:
            print(f"     Expected: {expected}")
            print(f"     Got:      {got}")
        failed += 1