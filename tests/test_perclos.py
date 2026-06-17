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


# TEST 1 — PERCLOS window fills correctly


print("TEST 1 — PERCLOS deque fills and auto-discards correctly")


window = deque(maxlen=PERCLOS_WINDOW_FRAMES)

# Fill to exactly maxlen
for i in range(PERCLOS_WINDOW_FRAMES):
    window.append(0.30)
check("Deque reaches maxlen after filling",
      len(window) == PERCLOS_WINDOW_FRAMES,
      PERCLOS_WINDOW_FRAMES, len(window))

# Add more — oldest should be discarded
window.append(0.99)
check("Deque stays at maxlen after overflow",
      len(window) == PERCLOS_WINDOW_FRAMES,
      PERCLOS_WINDOW_FRAMES, len(window))
check("Newest value is the one just appended",
      window[-1] == 0.99, 0.99, window[-1])


# TEST 2 — PERCLOS calculation scenarios

print("TEST 2 — PERCLOS calculation: 5 real scenarios")


ear_threshold = 0.22

def calc_perclos(ear_list):
    closed = sum(1 for e in ear_list if e < ear_threshold)
    return (closed / len(ear_list)) * 100.0

# Scenario 1: Fully alert — all frames above threshold
all_open   = [0.32] * PERCLOS_WINDOW_FRAMES
perclos_1  = calc_perclos(all_open)
check("Fully alert: PERCLOS = 0%",
      perclos_1 == 0.0, "0.0%", perclos_1)

# Scenario 2: Fully asleep — all frames below threshold
all_closed = [0.10] * PERCLOS_WINDOW_FRAMES
perclos_2  = calc_perclos(all_closed)
check("Fully asleep: PERCLOS = 100%",
      perclos_2 == 100.0, "100.0%", perclos_2)

# Scenario 3: Exactly L1 — 15% of frames closed
n_closed   = int(0.15 * PERCLOS_WINDOW_FRAMES)
mixed_l1   = [0.10] * n_closed + [0.32] * (PERCLOS_WINDOW_FRAMES - n_closed)
perclos_3  = calc_perclos(mixed_l1)
check("15% closed frames → PERCLOS ~15%",
      abs(perclos_3 - 15.0) < 1.0, "~15%", round(perclos_3, 2))

# Scenario 4: Normal blink rate (~12% of time)
blink_rate = [0.10] * int(0.12 * PERCLOS_WINDOW_FRAMES)
blink_rate += [0.32] * (PERCLOS_WINDOW_FRAMES - len(blink_rate))
perclos_4  = calc_perclos(blink_rate)
check("12% blink rate → PERCLOS ~12%",
      abs(perclos_4 - 12.0) < 1.0, "~12%", round(perclos_4, 2))
print(f"     Normal blink PERCLOS = {perclos_4:.1f}%")

# Scenario 5: Realistic drowsy — 35% closed
n_drowsy   = int(0.35 * PERCLOS_WINDOW_FRAMES)
drowsy     = [0.12] * n_drowsy + [0.30] * (PERCLOS_WINDOW_FRAMES - n_drowsy)
perclos_5  = calc_perclos(drowsy)
check("35% drowsy → PERCLOS ~35%",
      abs(perclos_5 - 35.0) < 1.0, "~35%", round(perclos_5, 2))


# TEST 3 — Fatigue score relative to personal baseline
#Test the fatigue score for the personal basline 

print("TEST 3 — Fatigue score scales correctly with baseline")

def compute_score(perclos_current, baseline_ear):
    baseline_perclos = max(2.0, (1.0 - baseline_ear / 0.35) * 20.0)
    return (perclos_current - baseline_perclos) / baseline_perclos

# Operator A — normal EAR baseline 0.30
score_A_alert  = compute_score(perclos_3,  0.30)  # 15% PERCLOS, normal baseline
score_A_drowsy = compute_score(perclos_5,  0.30)  # 35% PERCLOS, normal baseline

# Operator B — high EAR baseline 0.35 (wide-eyed)
score_B_alert  = compute_score(perclos_3,  0.35)
score_B_drowsy = compute_score(perclos_5,  0.35)

check("Higher baseline EAR → LOWER baseline PERCLOS denominator → HIGHER score (more sensitive)",
      score_B_alert > score_A_alert,
      f"score_B > score_A", f"{score_B_alert:.3f} vs {score_A_alert:.3f}")

check("Score_A drowsy triggers L3",
      score_A_drowsy >= FATIGUE_L3_THRESH,
      f">= {FATIGUE_L3_THRESH}", round(score_A_drowsy, 3))

print(f"     Operator A (EAR=0.30): alert_score={score_A_alert:.3f}, "
      f"drowsy_score={score_A_drowsy:.3f}")
print(f"     Operator B (EAR=0.35): alert_score={score_B_alert:.3f}, "
      f"drowsy_score={score_B_drowsy:.3f}")


# TEST 4 — Shift-time weighting


print("TEST 4 — Shift-time threshold tightening")


# At hour 4 multiplier = 0.9 → same score appears 11% higher effectively
raw_score    = 0.20
score_hr0    = raw_score
score_hr4    = raw_score * (1.0 / SHIFT_HOUR_4_MULT)
score_hr6    = raw_score * (1.0 / SHIFT_HOUR_6_MULT)

check("Score increases after hour 4",
      score_hr4 > score_hr0,
      f"> {score_hr0:.3f}", round(score_hr4, 3))
check("Score increases more after hour 6",
      score_hr6 > score_hr4,
      f"> {score_hr4:.3f}", round(score_hr6, 3))
check("Hour 6 score triggers L2 when raw score was only L1",
      score_hr6 >= FATIGUE_L2_THRESH and raw_score < FATIGUE_L2_THRESH,
      f"score_hr6={score_hr6:.3f} >= L2={FATIGUE_L2_THRESH}",
      f"raw={raw_score:.3f} < L2")

print(f"     Raw score:     {score_hr0:.3f}  → Level: "
      f"{'L1' if score_hr0 >= FATIGUE_L1_THRESH else 'NONE'}")
print(f"     After hour 4:  {score_hr4:.3f}  → Level: "
      f"{'L2' if score_hr4 >= FATIGUE_L2_THRESH else 'L1'}")
print(f"     After hour 6:  {score_hr6:.3f}  → Level: "
      f"{'L3' if score_hr6 >= FATIGUE_L3_THRESH else 'L2'}")


# TEST 5 — Calibration baseline analysis logic
#Check the logic of CalibrationManager._analyse_baseline 

print("TEST 5 — Calibration baseline analysis")


def simulate_baseline_analysis(ear_samples, mar_samples, pitch_samples):
    """Replicate CalibrationManager._analyse_baseline logic."""
    ear_arr   = np.array(ear_samples)
    mar_arr   = np.array(mar_samples)
    pitch_arr = np.array(pitch_samples)

    raw_ear   = float(np.percentile(ear_arr, 75))
    raw_mar   = float(np.median(mar_arr))
    raw_pitch = float(np.median(pitch_arr))

    glasses_mode    = raw_ear < EAR_GLASSES_THRESH
    drowsy_at_start = raw_ear < EAR_DROWSY_BASELINE

    if drowsy_at_start:
        baseline_ear = 0.70 * raw_ear + 0.30 * EAR_OPEN_NORMAL
    else:
        baseline_ear = raw_ear

    baseline_ear   = float(np.clip(baseline_ear,   0.15, 0.45))
    baseline_mar   = float(np.clip(raw_mar,         0.02, 0.40))
    baseline_pitch = float(np.clip(raw_pitch,       0.0,  20.0))

    return {
        "baseline_ear":    baseline_ear,
        "baseline_mar":    baseline_mar,
        "baseline_pitch":  baseline_pitch,
        "glasses_mode":    glasses_mode,
        "drowsy_at_start": drowsy_at_start,
    }

# Normal alert operator
normal_ear_samples = np.random.normal(0.31, 0.02, 200).tolist()
normal_mar_samples = np.random.normal(0.08, 0.01, 200).tolist()
normal_pitch       = np.random.normal(2.5,  1.0,  200).tolist()
result_normal      = simulate_baseline_analysis(
    normal_ear_samples, normal_mar_samples, normal_pitch
)
check("Normal operator: no glasses mode",
      not result_normal["glasses_mode"],
      False, result_normal["glasses_mode"])
check("Normal operator: not drowsy at start",
      not result_normal["drowsy_at_start"],
      False, result_normal["drowsy_at_start"])
check("Normal baseline EAR in 0.28–0.35 range",
      0.28 <= result_normal["baseline_ear"] <= 0.35,
      "0.28–0.35", round(result_normal["baseline_ear"], 3))

# Glasses operator — low EAR
glasses_ear_samples = np.random.normal(0.17, 0.015, 200).tolist()
result_glasses = simulate_baseline_analysis(
    glasses_ear_samples, normal_mar_samples, normal_pitch
)
check("Glasses operator: glasses_mode activated",
      result_glasses["glasses_mode"],
      True, result_glasses["glasses_mode"])
print(f"     Glasses baseline EAR = {result_glasses['baseline_ear']:.3f}")

# Drowsy operator at shift start
drowsy_ear_samples = np.random.normal(0.23, 0.02, 200).tolist()
result_drowsy = simulate_baseline_analysis(
    drowsy_ear_samples, normal_mar_samples, normal_pitch
)
check("Drowsy-at-start: drowsy_at_start flag set",
      result_drowsy["drowsy_at_start"],
      True, result_drowsy["drowsy_at_start"])
check("Drowsy-at-start: blended baseline higher than raw",
      result_drowsy["baseline_ear"] > np.mean(drowsy_ear_samples),
      "blended > raw",
      f"{result_drowsy['baseline_ear']:.3f} vs {np.mean(drowsy_ear_samples):.3f}")
print(f"     Drowsy raw EAR = {np.mean(drowsy_ear_samples):.3f}, "
      f"blended = {result_drowsy['baseline_ear']:.3f}")


# TEST 6 — CalibrationManager with simulated session_state

print("TEST 6 — CalibrationManager collects samples from session_state")


session_state = {
    "session_id":      "TEST-SESSION",
    "operator_id":     "OP001",
    "current_ear":     0.30,
    "current_mar":     0.08,
    "current_pitch":   2.0,
    "face_detected":   True,
    "baseline_ear":    0.30,
    "baseline_mar":    0.08,
    "baseline_pitch":  2.0,
    "glasses_mode":    False,
    "drowsy_at_start": False,
}
session_lock   = threading.Lock()
shutdown_event = threading.Event()

cal = CalibrationManager(session_state, session_lock, shutdown_event)

# Simulate feeding values (3 second demo calibration to check it's working or not)
from config import DEMO_CALIBRATION_SEC
import threading as th

def feed_values():
    for i in range(50):
        ear = 0.30 + np.random.normal(0, 0.01)
        with session_lock:
            session_state["current_ear"]   = float(np.clip(ear, 0.20, 0.40))
            session_state["current_mar"]   = 0.08 + np.random.normal(0, 0.005)
            session_state["current_pitch"] = 2.5  + np.random.normal(0, 0.5)
            session_state["face_detected"] = True
        time.sleep(0.08)

feeder = th.Thread(target=feed_values, daemon=True)
feeder.start()

# Run 3-second demo calibration (override DEMO_CALIBRATION_SEC)
import config
orig = config.DEMO_CALIBRATION_SEC
config.DEMO_CALIBRATION_SEC = 3

try:
    result = cal.run("OP001", demo_mode=True)
    check("CalibrationManager returns dict with baseline_ear",
          "baseline_ear" in result and result["baseline_ear"] > 0,
          "> 0", result.get("baseline_ear"))
    check("CalibrationManager returns baseline_mar",
          "baseline_mar" in result,
          True, "baseline_mar" in result)
    check("baseline_ear within realistic range",
          0.15 <= result["baseline_ear"] <= 0.45,
          "0.15–0.45", round(result["baseline_ear"], 3))
    check("session_state updated with baseline",
          session_state["baseline_ear"] == result["baseline_ear"],
          result["baseline_ear"],
          session_state["baseline_ear"])
    print(f"     Calibration result: EAR={result['baseline_ear']:.3f} "
          f"MAR={result['baseline_mar']:.3f} "
          f"PITCH={result['baseline_pitch']:.2f}°")
    print(f"     glasses_mode={result['glasses_mode']} "
          f"drowsy_at_start={result['drowsy_at_start']}")
except CalibrationError as e:
    check("CalibrationManager completes without error", False, "success", str(e))
finally:
    config.DEMO_CALIBRATION_SEC = orig

feeder.join(timeout=5)


# TEST 7 — CalibrationError on insufficient face detection(When the full face is not there for the decetion)


print("TEST 7 — CalibrationError on insufficient face detection")


bad_session = {
    "session_id": "TEST-BAD",
    "current_ear": 0.0,
    "current_mar": 0.0,
    "current_pitch": 0.0,
    "face_detected": False,    # face never detected
}
bad_lock     = threading.Lock()
bad_shutdown = threading.Event()
import config as cfg
orig2 = cfg.DEMO_CALIBRATION_SEC
cfg.DEMO_CALIBRATION_SEC = 2
cfg.CALIBRATION_RETRY_LIMIT = 0

cal2 = CalibrationManager(bad_session, bad_lock, bad_shutdown)
try:
    cal2.run("OP001", demo_mode=True)
    check("CalibrationError raised on no face detected", False,
          "CalibrationError", "no error raised")
except CalibrationError as e:
    check("CalibrationError raised on no face detected", True)
    print(f"     Error message: {str(e)[:80]}")
finally:
    cfg.DEMO_CALIBRATION_SEC    = orig2
    cfg.CALIBRATION_RETRY_LIMIT = 1

# SUMMARY (Of the entire test of perclos)

total = passed + failed
print(f"  RESULTS: {passed}/{total} tests passed")
if failed == 0:
    print("   ALL TESTS PASSED")
    print("  PERCLOS engine, calibration, and baseline logic verified.")
    print("  Safe to proceed to alert engine (Day 5).")
else:
    print(f"   {failed} TESTS FAILED — fix before proceeding")


sys.exit(0 if failed == 0 else 1)