# test_crash_recovery.py — Verify startup crash recovery and the detection watchdog
# Covers the 4 startup scenarios handled by core/crash_recovery.py:
# - Clean start (fresh DB and no ACTIVE session)
# - Recent crash -> session resumed (with and without calibration)
# - Stale crash -> session marked CRASHED
# - Corrupted DB -> quarantined
# Plus unacked-alert re-fire and DetectionWatchdog stall/restart logic.
# Runs against a temporary database — data/fatigue.db is never touched.
# Usage: uv run tests/test_crash_recovery.py


import sys, os, time, queue, threading, tempfile, uuid
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core import database
from core.state_machine import StateMachine, SystemState
from core.crash_recovery import (
    run_startup_recovery, refire_unacknowledged_alerts, DetectionWatchdog
)
from config import WATCHDOG_TIMEOUT_SEC, CRASH_RESUME_MAX_GAP_SEC

# Point the database module at a throwaway DB
TMP_DIR = tempfile.mkdtemp(prefix="fatigue_test_")
database.DATA_DIR = TMP_DIR
database.DB_PATH  = os.path.join(TMP_DIR, "fatigue.db")



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


def fresh_state():
    return StateMachine(), {"session_id": None, "operator_id": None}, threading.Lock()


def reset_db():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(database.DB_PATH + suffix):
            os.remove(database.DB_PATH + suffix)
    database.create_tables()


def add_session(operator_id="OP001", age_sec=30, checkpoint_age_sec=None,
                perclos=12.5):
    sid   = str(uuid.uuid4())
    start = (datetime.now() - timedelta(seconds=age_sec)).isoformat()
    ckpt  = None
    if checkpoint_age_sec is not None:
        ckpt = (datetime.now() - timedelta(seconds=checkpoint_age_sec)).isoformat()
    conn = database.get_connection()
    conn.execute("""
        INSERT INTO sessions (session_id, operator_id, start_time, status,
                              last_checkpoint, perclos_checkpoint)
        VALUES (?, ?, ?, 'ACTIVE', ?, ?)
    """, (sid, operator_id, start, ckpt, perclos))
    conn.commit()
    conn.close()
    return sid


def run(sm, state, lock):
    return run_startup_recovery(state, lock, sm, queue.Queue(maxsize=3))



# TEST 1 — Fresh install, no DB file

print("\n" + "="*55)
print("TEST 1 — Fresh install (no database file)")
print("="*55)

sm, state, lock = fresh_state()
res = run(sm, state, lock)
check("Scenario is CLEAN_START", res["scenario"] == "CLEAN_START",
      "CLEAN_START", res["scenario"])
check("Nothing recovered", res["recovered"] is False)
check("State moved to WAITING_OPERATOR",
      sm.state == SystemState.WAITING_OPERATOR, "WAITING_OPERATOR", sm.state.name)


# TEST 2 — Existing DB, previous shift closed cleanly

print("\n" + "="*55)
print("TEST 2 — Clean start (no ACTIVE session)")
print("="*55)

reset_db()
sm, state, lock = fresh_state()
res = run(sm, state, lock)
check("Scenario is CLEAN_START", res["scenario"] == "CLEAN_START",
      "CLEAN_START", res["scenario"])
check("Login possible afterwards (WAITING_OPERATOR -> CALIBRATING)",
      sm.can_transition(SystemState.CALIBRATING))


# TEST 3 — Recent crash, stored baseline -> resume monitoring

print("\n" + "="*55)
print("TEST 3 — Recent crash resumed with stored baseline")
print("="*55)

reset_db()
sid = add_session(age_sec=120, checkpoint_age_sec=20, perclos=12.5)
database.insert_event(sid, "FATIGUE_L2", ear_value=0.18, perclos_value=22.0,
                      fatigue_score=0.3, alert_level=2)
sm, state, lock = fresh_state()
res = run(sm, state, lock)
check("Scenario is SESSION_RESUMED", res["scenario"] == "SESSION_RESUMED",
      "SESSION_RESUMED", res["scenario"])
check("recovered flag set", res["recovered"] is True)
check("No calibration needed (OP001 has baseline)",
      res["needs_calibration"] is False)
check("State is MONITORING", sm.state == SystemState.MONITORING,
      "MONITORING", sm.state.name)
check("session_id restored", state.get("session_id") == sid, sid, state.get("session_id"))
check("PERCLOS checkpoint restored", state.get("perclos_current") == 12.5,
      12.5, state.get("perclos_current"))
check("Session kept ACTIVE for resume",
      database.get_active_session()["session_id"] == sid)
check("One unacked alert found", len(res["unacked_alerts"]) == 1,
      1, len(res["unacked_alerts"]))

aq = queue.Queue(maxsize=3)
n  = refire_unacknowledged_alerts(res["unacked_alerts"], aq, state, lock)
msg = aq.get_nowait() if not aq.empty() else {}
check("Unacked alert re-fired", n == 1, 1, n)
check("Re-fired as FATIGUE_L2 for the resumed session",
      msg.get("type") == "FATIGUE_L2" and msg.get("session_id") == sid,
      "FATIGUE_L2", msg.get("type"))
check("Nothing re-fired for empty list",
      refire_unacknowledged_alerts([], aq, state, lock) == 0)


# TEST 4 — Alert acknowledged before crash is not re-fired

print("\n" + "="*55)
print("TEST 4 — Acknowledged alert not treated as unacked")
print("="*55)

reset_db()
sid = add_session(age_sec=120, checkpoint_age_sec=20)
database.insert_event(sid, "FATIGUE_L1", alert_level=1)
time.sleep(0.01)
database.write_audit_log("EVENT_ACKNOWLEDGED", "OPERATOR", sid, "Level 1 ack in 2.0s")
sm, state, lock = fresh_state()
res = run(sm, state, lock)
check("No unacked alerts", res["unacked_alerts"] == [], [], res["unacked_alerts"])


# TEST 5 — Recent crash, operator never calibrated

print("\n" + "="*55)
print("TEST 5 — Recent crash, operator needs calibration")
print("="*55)

reset_db()
conn = database.get_connection()
conn.execute("""
    INSERT INTO operators (operator_id, name, pin_hash, baseline_ear,
                           baseline_mar, baseline_pitch, glasses_mode, created_at)
    VALUES ('OP777', 'New Operator', '0000', 0.0, 0.0, 0.0, 0, ?)
""", (datetime.now().isoformat(),))
conn.commit()
conn.close()
add_session(operator_id="OP777", age_sec=40)
sm, state, lock = fresh_state()
res = run(sm, state, lock)
check("Scenario is SESSION_RESUMED", res["scenario"] == "SESSION_RESUMED",
      "SESSION_RESUMED", res["scenario"])
check("needs_calibration is True", res["needs_calibration"] is True)
check("State is CALIBRATING", sm.state == SystemState.CALIBRATING,
      "CALIBRATING", sm.state.name)


# TEST 6 — Stale crash -> session closed as CRASHED

print("\n" + "="*55)
print("TEST 6 — Stale crash marked CRASHED")
print("="*55)

reset_db()
old = CRASH_RESUME_MAX_GAP_SEC + 600
sid = add_session(age_sec=old, checkpoint_age_sec=old - 60)
sm, state, lock = fresh_state()
res = run(sm, state, lock)
conn = database.get_connection()
row  = dict(conn.execute("SELECT status, end_time FROM sessions WHERE session_id=?",
                         (sid,)).fetchone())
conn.close()
check("Scenario is STALE_SESSION_CLOSED",
      res["scenario"] == "STALE_SESSION_CLOSED",
      "STALE_SESSION_CLOSED", res["scenario"])
check("Session status CRASHED", row["status"] == "CRASHED", "CRASHED", row["status"])
check("Session end_time set", row["end_time"] is not None)
check("State is WAITING_OPERATOR", sm.state == SystemState.WAITING_OPERATOR,
      "WAITING_OPERATOR", sm.state.name)
check("session_state untouched", state.get("session_id") is None)


# TEST 7 — Corrupted database quarantined

print("\n" + "="*55)
print("TEST 7 — Corrupted database quarantined")
print("="*55)

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(database.DB_PATH + suffix):
        os.remove(database.DB_PATH + suffix)
with open(database.DB_PATH, "wb") as f:
    f.write(b"this is not a sqlite database" * 100)
sm, state, lock = fresh_state()
res = run(sm, state, lock)
quarantined = [f for f in os.listdir(TMP_DIR) if ".corrupt-" in f]
check("Scenario is DB_CORRUPTED", res["scenario"] == "DB_CORRUPTED",
      "DB_CORRUPTED", res["scenario"])
check("Corrupt file moved aside", len(quarantined) == 1, 1, quarantined)
check("Original path freed for a fresh DB", not os.path.exists(database.DB_PATH))
database.create_tables()
check("Fresh DB verifies after quarantine", database.verify_database())


# TEST 8 — Watchdog stall detection and restart

print("\n" + "="*55)
print("TEST 8 — DetectionWatchdog")
print("="*55)

reset_db()
restarts = []
sm = StateMachine()
sm.transition(SystemState.WAITING_OPERATOR)
sm.transition(SystemState.CALIBRATING)
sm.transition(SystemState.MONITORING)
state = {"session_id": "S1", "last_frame_time": None, "camera_stalled": False}
lock  = threading.Lock()
aq    = queue.Queue(maxsize=3)
wd    = DetectionWatchdog(state, lock, sm, aq, threading.Event(),
                          restart_callback=lambda: restarts.append(1))

wd._check()
check("No restart before Thread 1 has started", restarts == [])

state["last_frame_time"] = time.time()
wd._check()
check("No restart while frames are flowing", restarts == [])

state["last_frame_time"] = time.time() - (WATCHDOG_TIMEOUT_SEC + 5)
wd._check()
check("Restart triggered after timeout", len(restarts) == 1, 1, len(restarts))
check("camera_stalled flag set", state["camera_stalled"] is True)
alert = aq.get_nowait() if not aq.empty() else {}
check("Camera check alert queued during session",
      alert.get("type") == "CAMERA_OBSTRUCTION", "CAMERA_OBSTRUCTION", alert.get("type"))

wd._check()
check("Second restart attempt while still stalled", len(restarts) == 2, 2, len(restarts))
check("Camera check alert not repeated in same episode", aq.empty())

state["last_frame_time"] = time.time()
wd._check()
check("camera_stalled cleared once frames resume", state["camera_stalled"] is False)

stop = threading.Event()
wd2  = DetectionWatchdog(state, lock, sm, aq, stop, restart_callback=lambda: None)
wd2.start()
stop.set()
wd2.join(timeout=2)
check("Watchdog thread exits on shutdown", not wd2.is_alive())


# SUMMARY : - print results and exit with code 0 if all passed, else 1

print("\n" + "="*55)
total = passed + failed
print(f"  RESULTS: {passed}/{total} tests passed")
if failed == 0:
    print("  ALL TESTS PASSED")
    print("  Crash recovery scenarios and watchdog verified.")
else:
    print(f"   {failed} TESTS FAILED — fix before proceeding")
print("="*55)

sys.exit(0 if failed == 0 else 1)
