# test_alert_triggers.py — Verify what fires an alert and how fast it reaches the operator
# - MAR landmark order gives sane values (closed mouth ~0, yawn > threshold)
# - Yawn / head droop only alert once sustained, and carry a reason
# - Eye-closure alerts are labelled "Eyes closing"
# - A higher-level alert supersedes a pending L1/L2 ack wait immediately
# - Unacked L1/L2 clears when fatigue signs are gone, escalates only if they persist
# Usage: uv run tests/test_alert_triggers.py


import sys, os, time, queue, threading
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.detection import DetectionThread
from core.alert_engine import AlertEngine
from core.state_machine import StateMachine
from config import (
    MOUTH, MAR_YAWN_THRESH, YAWN_DURATION_SEC,
    PITCH_DROOP_THRESH, PITCH_DROOP_SEC, FATIGUE_L1_THRESH, DEBOUNCE_FRAMES,
)



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


def make_detector():
    state = {"session_id": "S1", "demo_mode": False, "perclos_current": 0.0}
    det = DetectionThread(StateMachine(), {}, threading.Lock(),
                          queue.Queue(maxsize=10), queue.Queue(maxsize=50),
                          threading.Event(), threading.Event(),
                          state, threading.Lock())
    return det


def run_frames(det, seconds, score=-1.0, mar=0.05, pitch=3.0, fps=30):
    det.smooth_mar, det.smooth_pitch = mar, pitch
    for _ in range(int(seconds * fps)):
        det._alert_decision(score, 0.30, mar, pitch)


def fired(det):
    out = []
    while not det.alert_queue.empty():
        out.append(det.alert_queue.get_nowait())
    return out



# TEST 1 — MAR from correctly ordered landmarks

print("\n" + "="*55)
print("TEST 1 — MAR landmark order")
print("="*55)

def mouth_landmarks(open_px):
    # inner lips: corners 100px apart, upper/lower lip split by open_px
    pos = {
        MOUTH[0]: (0, 0),    MOUTH[4]: (100, 0),
        MOUTH[1]: (25, -open_px/2), MOUTH[2]: (50, -open_px/2), MOUTH[3]: (75, -open_px/2),
        MOUTH[7]: (25,  open_px/2), MOUTH[6]: (50,  open_px/2), MOUTH[5]: (75,  open_px/2),
    }
    lm = [SimpleNamespace(x=0.0, y=0.0) for _ in range(478)]
    for idx, (x, y) in pos.items():
        lm[idx] = SimpleNamespace(x=(x + 200) / 480, y=(y + 200) / 360)
    return lm

det = make_detector()
closed = det._calculate_mar(mouth_landmarks(2), 480, 360)
yawn   = det._calculate_mar(mouth_landmarks(80), 480, 360)
check("Closed mouth MAR < 0.1", closed < 0.1, "< 0.1", round(closed, 3))
check("Wide yawn MAR above yawn threshold", yawn > MAR_YAWN_THRESH,
      f"> {MAR_YAWN_THRESH}", round(yawn, 3))


# TEST 2 — Brief mouth opening / glance down does not alert

print("\n" + "="*55)
print("TEST 2 — Brief yawn / droop ignored")
print("="*55)

det = make_detector()
run_frames(det, YAWN_DURATION_SEC - 1, mar=MAR_YAWN_THRESH + 0.2)
check("Short mouth opening: no alert", fired(det) == [])
run_frames(det, 1)   # back to normal
run_frames(det, PITCH_DROOP_SEC - 1, pitch=PITCH_DROOP_THRESH + 10)
check("Short glance down: no alert", fired(det) == [])


# TEST 3 — Sustained yawn fires L1 with reason

print("\n" + "="*55)
print("TEST 3 — Sustained yawn")
print("="*55)

det = make_detector()
run_frames(det, YAWN_DURATION_SEC + 0.5, mar=MAR_YAWN_THRESH + 0.2)
alerts = fired(det)
check("One alert fired", len(alerts) == 1, 1, len(alerts))
check("Alert is L1", alerts and alerts[0]["level"] == 1, 1, alerts and alerts[0]["level"])
check("Reason is Yawning", alerts and alerts[0]["reason"] == "Yawning",
      "Yawning", alerts and alerts[0]["reason"])


# TEST 4 — Sustained head droop fires L1 with reason

print("\n" + "="*55)
print("TEST 4 — Sustained head droop")
print("="*55)

det = make_detector()
run_frames(det, PITCH_DROOP_SEC + 0.5, pitch=PITCH_DROOP_THRESH + 10)
alerts = fired(det)
check("One alert fired", len(alerts) == 1, 1, len(alerts))
check("Reason is Head drooping", alerts and alerts[0]["reason"] == "Head drooping",
      "Head drooping", alerts and alerts[0]["reason"])


# TEST 5 — Eye closure labelled, and droop + eyes escalates to L2

print("\n" + "="*55)
print("TEST 5 — Eye closure and combined signals")
print("="*55)

det = make_detector()
run_frames(det, (DEBOUNCE_FRAMES + 1) / 30, score=FATIGUE_L1_THRESH + 0.01)
alerts = fired(det)
check("Eye-closure alert fired", len(alerts) == 1, 1, len(alerts))
check("Reason is Eyes closing", alerts and alerts[0]["reason"] == "Eyes closing",
      "Eyes closing", alerts and alerts[0]["reason"])

det = make_detector()
run_frames(det, PITCH_DROOP_SEC + 0.5, score=FATIGUE_L1_THRESH + 0.01,
           pitch=PITCH_DROOP_THRESH + 10)
levels = [a["level"] for a in fired(det)]
check("Eyes closing + head droop reaches L2", 2 in levels, "contains 2", levels)


# TEST 6 — Higher alert supersedes a pending ack wait

print("\n" + "="*55)
print("TEST 6 — L2 not delayed by pending L1 ack")
print("="*55)

aq, dq  = queue.Queue(maxsize=3), queue.Queue(maxsize=50)
ack, sd = threading.Event(), threading.Event()
state   = {"session_id": "S1", "alert_level": 0}
lock    = threading.Lock()
engine  = AlertEngine(aq, dq, ack, sd, state, lock, language="marathi")
engine._play_audio = lambda *a, **k: None    # keep the test silent
engine.start()

aq.put({"type": "FATIGUE_L1", "level": 1, "reason": "Yawning", "session_id": "S1"})
time.sleep(0.5)
t0 = time.time()
aq.put({"type": "FATIGUE_L2", "level": 2, "reason": "Eyes closing", "session_id": "S1"})
while time.time() - t0 < 5 and state.get("alert_level") != 2:
    time.sleep(0.05)
delay = time.time() - t0
check("L2 shown within 1s (not after 30s ack timeout)", delay < 1.0,
      "< 1.0s", f"{delay:.2f}s")
check("Dashboard reason updated", state.get("alert_reason") == "Eyes closing",
      "Eyes closing", state.get("alert_reason"))

ack.set()
time.sleep(0.5)
check("Ack clears alert level and reason",
      state.get("alert_level") == 0 and state.get("alert_reason") == "",
      "0 / ''", (state.get("alert_level"), state.get("alert_reason")))
sd.set()
aq.put("SHUTDOWN")
engine.join(timeout=3)


# TEST 7 — Unacked alert clears when fatigue is gone, escalates only if not

print("\n" + "="*55)
print("TEST 7 — Auto-clear vs escalation")
print("="*55)

import core.alert_engine as alert_engine
alert_engine.ALERT_CLEAR_SEC = 0.5    # shorten timers for the test
alert_engine.ACK_TIMEOUT_SEC = 1.5

def start_engine(state):
    aq, sd = queue.Queue(maxsize=3), threading.Event()
    eng = AlertEngine(aq, queue.Queue(maxsize=50), threading.Event(), sd,
                      state, threading.Lock(), language="marathi")
    eng._play_audio = lambda *a, **k: None
    eng.start()
    return eng, aq, sd

def stop_engine(eng, aq, sd):
    sd.set(); aq.put("SHUTDOWN"); eng.join(timeout=3)

# a) eyes open again (fatigue_level 0) -> L1 clears, no escalation
state = {"session_id": "S1", "fatigue_level": 0, "fatigue_reason": ""}
eng, aq, sd = start_engine(state)
aq.put({"type": "FATIGUE_L1", "level": 1, "reason": "Eyes closing", "session_id": "S1"})
time.sleep(1.2)
check("L1 cleared once fatigue signs gone", state.get("alert_level") == 0,
      0, state.get("alert_level"))
time.sleep(1.0)
check("No escalation after clearing", state.get("alert_level") == 0,
      0, state.get("alert_level"))
stop_engine(eng, aq, sd)

# b) still fatigued, no ack -> escalates to L2 with the live reason
state = {"session_id": "S1", "fatigue_level": 1, "fatigue_reason": "Head drooping"}
eng, aq, sd = start_engine(state)
aq.put({"type": "FATIGUE_L1", "level": 1, "reason": "Eyes closing", "session_id": "S1"})
t0 = time.time()
while time.time() - t0 < 4 and state.get("alert_level") != 2:
    time.sleep(0.05)
check("Persistent fatigue escalates L1 -> L2", state.get("alert_level") == 2,
      2, state.get("alert_level"))
check("Escalated alert shows live reason",
      state.get("alert_reason") == "Head drooping",
      "Head drooping", state.get("alert_reason"))
stop_engine(eng, aq, sd)


# TEST 8 — Ending a shift mid-monitoring doesn't break Thread 1

print("\n" + "="*55)
print("TEST 8 — End of shift race")
print("="*55)

import tempfile
from core import database
from core.session_manager import SessionManager
from core.state_machine import SystemState
database.DATA_DIR = tempfile.mkdtemp(prefix="fatigue_test_")
database.DB_PATH  = os.path.join(database.DATA_DIR, "fatigue.db")
database.create_tables()

class RecordingState(dict):
    """Records the state machine state at the moment start_time is cleared."""
    def update(self, *a, **kw):
        new = dict(*a, **kw)
        if "start_time" in new and new["start_time"] is None:
            self.state_when_cleared = sm.state
        super().update(new)

sm = StateMachine()
sm.transition(SystemState.WAITING_OPERATOR)
sm.transition(SystemState.CALIBRATING)
sm.transition(SystemState.MONITORING)
state = RecordingState(session_id="S-END", operator_id="OP001",
                       operator_name="Demo", start_time=time.time())
mgr = SessionManager(state, threading.Lock(), sm, queue.Queue(maxsize=5),
                     threading.Event(), threading.Event())
mgr.end_session(reason="test")
check("Left MONITORING before session data was cleared",
      getattr(state, "state_when_cleared", None) == SystemState.SESSION_CLOSING,
      "SESSION_CLOSING", getattr(state, "state_when_cleared", None))
check("Ends in WAITING_OPERATOR", sm.state == SystemState.WAITING_OPERATOR,
      "WAITING_OPERATOR", sm.state.name)

det = make_detector()
det.session_state["start_time"] = None       # between sessions
for _ in range(40):
    det.ear_deque.append(0.30)
try:
    det._update_perclos(False)
    ok = True
except TypeError:
    ok = False
check("PERCLOS update tolerates start_time=None", ok)

# A frame that raises must not end the detection loop
det   = make_detector()
calls = []
def flaky_frame():
    calls.append(1)
    if len(calls) <= 2:
        raise ValueError("simulated bad frame")
    det.shutdown_event.set()
det._init_camera     = lambda: True
det._init_landmarker = lambda: True
det._cleanup         = lambda: None
det._process_frame   = flaky_frame
det.run()
check("Detection loop survives frame errors", len(calls) == 3, 3, len(calls))


# SUMMARY : - print results and exit with code 0 if all passed, else 1

print("\n" + "="*55)
total = passed + failed
print(f"  RESULTS: {passed}/{total} tests passed")
if failed == 0:
    print("  ALL TESTS PASSED")
    print("  Alert triggers, reasons and escalation timing verified.")
else:
    print(f"   {failed} TESTS FAILED — fix before proceeding")
print("="*55)

sys.exit(0 if failed == 0 else 1)
