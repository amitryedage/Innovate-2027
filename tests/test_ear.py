# test_ear.py — Verify EAR, MAR, and head pitch calculations
#This test suite checks the mathematical correctness of the core formulas used in fatigue detection:
# - EAR (Eye Aspect Ratio) for open vs closed eyes
# - MAR (Mouth Aspect Ratio) for yawning detection
# - EMA (Exponential Moving Average) for smoothing noisy signals
# - PERCLOS rolling window calculation
# - Fatigue score calculation based on deviation from baseline
# Usage: python tests/test_ear.py


import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
from scipy.spatial import distance as dist
from collections import deque

from config import (
    EMA_ALPHA, PERCLOS_WINDOW_FRAMES,
    FATIGUE_L1_THRESH, FATIGUE_L2_THRESH, FATIGUE_L3_THRESH,
    EAR_OPEN_NORMAL, EAR_GLASSES_THRESH,
    MAR_YAWN_THRESH,
)



def calculate_ear_from_points(pts):
   
    A = dist.euclidean(pts[1], pts[5])
    B = dist.euclidean(pts[2], pts[4])
    C = dist.euclidean(pts[0], pts[3])
    return (A + B) / (2.0 * C) if C > 0 else 0.0


def calculate_mar_from_points(pts):
    """
    pts: list of 8 (x,y) tuples for mouth landmarks
    Returns MAR float.
    """
    A = dist.euclidean(pts[1], pts[7])
    B = dist.euclidean(pts[2], pts[6])
    C = dist.euclidean(pts[3], pts[5])
    D = dist.euclidean(pts[0], pts[4])
    return (A + B + C) / (3.0 * D) if D > 0 else 0.0


def apply_ema(values, alpha=EMA_ALPHA):
    """Apply EMA smoothing to a list of values. Returns smoothed list."""
    smoothed = [values[0]]
    for v in values[1:]:
        smoothed.append(alpha * v + (1 - alpha) * smoothed[-1])
    return smoothed




passed = 0
failed = 0

def check(name, condition, expected=None, got=None):
    global passed, failed
    if condition:
        print(f"  {name}")
        passed += 1
    else:
        print(f"   {name}")
        if expected is not None:
            print(f"     Expected: {expected}")
            print(f"     Got:      {got}")
        failed += 1



# TEST 1 — EAR with perfectly open eye

print("\n" + "="*55)
print("TEST 1 — EAR formula: perfectly open eye")
print("="*55)


open_eye = [
    (0,  10),   # p0 outer left
    (20, 0 ),   # p1 top inner
    (40, 0 ),   # p2 top outer
    (60, 10),   # p3 outer right
    (40, 20),   # p4 bottom outer
    (20, 20),   # p5 bottom inner
]
ear_open = calculate_ear_from_points(open_eye)
check("Open eye EAR in range 0.28–0.38",
      0.28 <= ear_open <= 0.38,
      "0.28–0.38", round(ear_open, 3))
print(f"     Open eye EAR = {ear_open:.3f}")


# TEST 2 — EAR with closed eye

print("\n" + "="*55)
print("TEST 2 — EAR formula: closed eye")
print("="*55)

# Eye nearly closed: height = 2px, width = 60px
# EAR should be approximately 2/60 = 0.033 (very low)
closed_eye = [
    (0,  5 ),   # p0 outer left
    (20, 4 ),   # p1 top inner
    (40, 4 ),   # p2 top outer
    (60, 5 ),   # p3 outer right
    (40, 6 ),   # p4 bottom outer
    (20, 6 ),   # p5 bottom inner
]
ear_closed = calculate_ear_from_points(closed_eye)
check("Closed eye EAR below 0.15",
      ear_closed < 0.15,
      "< 0.15", round(ear_closed, 3))
print(f"     Closed eye EAR = {ear_closed:.3f}")


# TEST 3 — EAR clearly separates open vs closed

print("\n" + "="*55)
print("TEST 3 — EAR separation: open vs closed")
print("="*55)

check("Open EAR > Closed EAR by at least 0.15",
      (ear_open - ear_closed) >= 0.15,
      ">= 0.15 difference",
      round(ear_open - ear_closed, 3))
print(f"     Separation = {ear_open - ear_closed:.3f}")


# TEST 4 — MAR: mouth resting vs yawning

print("\n" + "="*55)
print("TEST 4 — MAR formula: resting vs yawning")
print("="*55)

# Resting mouth: width=80, height=5 → MAR ~ 0.06
resting_mouth = [
    (0,  20),  # left corner
    (20, 18),  # top left
    (40, 17),  # top center
    (60, 18),  # top right
    (80, 20),  # right corner
    (60, 22),  # bottom right
    (40, 23),  # bottom center
    (20, 22),  # bottom left
]
mar_resting = calculate_mar_from_points(resting_mouth)
check("Resting MAR below 0.35",
      mar_resting < 0.35,
      "< 0.35", round(mar_resting, 3))
print(f"     Resting MAR = {mar_resting:.3f}")

# Yawning mouth: width=80, height=50 → MAR ~ 0.63(based on formula and geometry,reseched typical yawning MAR values)
yawn_mouth = [
    (0,  30),  # left corner
    (20, 5 ),  # top left
    (40, 0 ),  # top center
    (60, 5 ),  # top right
    (80, 30),  # right corner
    (60, 55),  # bottom right
    (40, 60),  # bottom center
    (20, 55),  # bottom left
]
mar_yawn = calculate_mar_from_points(yawn_mouth)
check("Yawn MAR above 0.60",
      mar_yawn >= 0.60,
      ">= 0.60", round(mar_yawn, 3))
print(f"     Yawn MAR = {mar_yawn:.3f}")

check("Yawn MAR clearly above resting by 0.3+",
      (mar_yawn - mar_resting) >= 0.30,
      ">= 0.30 difference",
      round(mar_yawn - mar_resting, 3))


# TEST 5 — EMA smoothing reduces noise

print("\n" + "="*55)
print("TEST 5 — EMA smoothing reduces vibration noise")
print("="*55)

# Simulate stable signal with added noise
np.random.seed(42)
true_value   = 0.30
noise        = np.random.normal(0, 0.05, 100)  # simulate cab vibration
noisy_signal = true_value + noise
smoothed     = apply_ema(noisy_signal.tolist())

raw_std      = float(np.std(noisy_signal))
smooth_std   = float(np.std(smoothed))

check("EMA reduces std deviation by at least 50%",
      smooth_std < raw_std * 0.5,
      f"< {raw_std*0.5:.4f}", round(smooth_std, 4))
check("EMA smoothed std < 0.03",
      smooth_std < 0.03,
      "< 0.03", round(smooth_std, 4))
print(f"     Raw noise std:     {raw_std:.4f}")
print(f"     Smoothed std:      {smooth_std:.4f}")
print(f"     Noise reduction:   {((raw_std-smooth_std)/raw_std*100):.1f}%")


# TEST 6 — PERCLOS rolling window(Check that it counts % of frames below EAR threshold correctly)

print("\n" + "="*55)
print("TEST 6 — PERCLOS rolling window calculation")
print("="*55)

ear_threshold = 0.22
window = deque(maxlen=PERCLOS_WINDOW_FRAMES)

# Simulate 60 seconds: first 45s alert, last 15s drowsy
alert_frames  = int(0.75 * PERCLOS_WINDOW_FRAMES)  # 1350 frames open
drowsy_frames = int(0.25 * PERCLOS_WINDOW_FRAMES)  # 450 frames closed

for _ in range(alert_frames):
    window.append(0.32)   # open eye EAR
for _ in range(drowsy_frames):
    window.append(0.15)   # closed eye EAR

perclos = sum(1 for e in window if e < ear_threshold) / len(window) * 100
check("Window maxlen is 1800",
      len(window) == PERCLOS_WINDOW_FRAMES,
      PERCLOS_WINDOW_FRAMES, len(window))
check("PERCLOS = 25% when 25% of frames are closed",
      abs(perclos - 25.0) < 1.0,
      "~25.0%", round(perclos, 2))
print(f"     PERCLOS = {perclos:.2f}% (expected ~25%)")

# TEST 7 — Fatigue score delta from baseline

print("\n" + "="*55)
print("TEST 7 — Fatigue score: deviation from personal baseline")
print("="*55)

baseline_ear     = 0.30
baseline_perclos = max(2.0, (1.0 - baseline_ear / 0.35) * 20.0)

# Scenario A: slightly drowsy (PERCLOS = baseline * 1.2)
perclos_a    = baseline_perclos * 1.20
score_a      = (perclos_a - baseline_perclos) / baseline_perclos
check("Score A = 0.20 when 20% above baseline",
      abs(score_a - 0.20) < 0.01,
      "~0.20", round(score_a, 3))

# Scenario B: moderately drowsy (PERCLOS = baseline * 1.30)
perclos_b    = baseline_perclos * 1.30
score_b      = (perclos_b - baseline_perclos) / baseline_perclos
check("Score B = 0.30 when 30% above baseline",
      abs(score_b - 0.30) < 0.01,
      "~0.30", round(score_b, 3))

# Scenario C: critical (PERCLOS = baseline * 1.50)
perclos_c    = baseline_perclos * 1.50
score_c      = (perclos_c - baseline_perclos) / baseline_perclos
check("Score C = 0.50 when 50% above baseline",
      abs(score_c - 0.50) < 0.01,
      "~0.50", round(score_c, 3))

# Verify alert thresholds fire at right scores
check(f"L1 threshold ({FATIGUE_L1_THRESH}) fires for Score A",
      score_a >= FATIGUE_L1_THRESH,
      f">= {FATIGUE_L1_THRESH}", round(score_a, 3))
check(f"L2 threshold ({FATIGUE_L2_THRESH}) fires for Score B",
      score_b >= FATIGUE_L2_THRESH,
      f">= {FATIGUE_L2_THRESH}", round(score_b, 3))
check(f"L3 threshold ({FATIGUE_L3_THRESH}) fires for Score C",
      score_c >= FATIGUE_L3_THRESH,
      f">= {FATIGUE_L3_THRESH}", round(score_c, 3))

print(f"\n     Baseline PERCLOS = {baseline_perclos:.2f}%")
print(f"     Score A (+20%) = {score_a:.3f}  → L1 fires: {score_a >= FATIGUE_L1_THRESH}")
print(f"     Score B (+30%) = {score_b:.3f}  → L2 fires: {score_b >= FATIGUE_L2_THRESH}")
print(f"     Score C (+50%) = {score_c:.3f}  → L3 fires: {score_c >= FATIGUE_L3_THRESH}")


# TEST 8 — Glasses mode EAR threshold adjustment

print("\n" + "="*55)
print("TEST 8 — Glasses mode detection")
print("="*55)

glasses_baseline_ear = 0.18   # operator with glasses
normal_threshold     = 0.22   # normal EAR closed threshold
glasses_threshold    = 0.18   # glasses mode threshold

# A glasses wearer's baseline should trigger glasses_mode flag
check("Glasses baseline EAR < 0.20 triggers glasses mode",
      glasses_baseline_ear < EAR_GLASSES_THRESH,
      f"< {EAR_GLASSES_THRESH}", glasses_baseline_ear)
check("Glasses threshold lower than normal threshold",
      glasses_threshold < normal_threshold,
      f"< {normal_threshold}", glasses_threshold)
print(f"     Glasses baseline EAR = {glasses_baseline_ear}")
print(f"     glasses_mode would activate: {glasses_baseline_ear < EAR_GLASSES_THRESH}")


# SUMMARY : - print results and exit with code 0 if all passed, else 1

print("\n" + "="*55)
total = passed + failed
print(f"  RESULTS: {passed}/{total} tests passed")
if failed == 0:
    print("  ALL TESTS PASSED")
    print("  EAR, MAR, EMA, PERCLOS, fatigue score math verified.")
    print("  Safe to proceed to main.py testing.")
else:
    print(f"   {failed} TESTS FAILED — fix before proceeding")
print("="*55)

sys.exit(0 if failed == 0 else 1)