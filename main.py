import sys
import signal
import threading
import queue
import time
import os
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.state_machine  import StateMachine, SystemState
from core.database       import (
    create_tables, verify_database, get_active_session,
    mark_session_crashed, write_audit_log,
    open_session, close_session, update_checkpoint
)
from core.detection      import DetectionThread
from config              import ALERT_QUEUE_MAX, DB_QUEUE_MAX


# SHARED OBJECTS — created once, passed to every thread

state_machine    = StateMachine()
alert_queue      = queue.Queue(maxsize=ALERT_QUEUE_MAX)
db_queue         = queue.Queue(maxsize=DB_QUEUE_MAX)
frame_buffer     = {"frame": None, "annotated": None, "raw": None}
frame_lock       = threading.Lock()
ack_event        = threading.Event()
shutdown_event   = threading.Event()

session_state = {
    "session_id":       None,
    "operator_id":      None,
    "operator_name":    None,
    "start_time":       None,
    "demo_mode":        False,
    "baseline_ear":     0.30,
    "baseline_mar":     0.10,
    "baseline_pitch":   2.0,
    "glasses_mode":     False,
    "perclos_current":  0.0,
    "fatigue_score":    0.0,
    "face_detected":    True,
    "threshold_raised": 0.0,
    "brightness":       100.0,
    "current_ear":      0.0,
    "current_mar":      0.0,
    "current_pitch":    0.0,
    "fps":              0,
}
session_lock  = threading.Lock()
threads       = {}
SHUTDOWN_SENTINEL = "SHUTDOWN"


# STARTUP CRASH RECOVERY
def startup_check():
    print("[MAIN] Running startup check...")
    active = get_active_session()
    if active:
        print(f"[MAIN]  Crashed session found: {active['session_id']}")
        mark_session_crashed(active["session_id"])
        with session_lock:
            session_state["session_id"]       = active["session_id"]
            session_state["operator_id"]      = active["operator_id"]
            session_state["perclos_current"]  = active["perclos_checkpoint"]
            session_state["threshold_raised"] = active["threshold_raised"]
        write_audit_log("CRASH_RECOVERY", "SYSTEM", active["session_id"],
                        f"Restored PERCLOS={active['perclos_checkpoint']:.2f}%")
        print("[MAIN] Crash state restored.")
        state_machine.transition(SystemState.CRASH_RECOVERY)
        state_machine.transition(SystemState.MONITORING)
        return True
    print("[MAIN]  Clean start.")
    state_machine.transition(SystemState.WAITING_OPERATOR)
    return False


# SHUTDOWN

def shutdown(reason: str = "User requested"):
    if shutdown_event.is_set():
        return
    print(f"\n[MAIN] Shutting down: {reason}")
    shutdown_event.set()
    ack_event.set()
    for q in (alert_queue, db_queue):
        try:
            q.put_nowait(SHUTDOWN_SENTINEL)
        except queue.Full:
            pass
    for name, t in threads.items():
        if t and t.is_alive():
            t.join(timeout=2)
            status = "ok" if not t.is_alive() else " force"
            print(f"[MAIN] {status} Thread '{name}' exited")
    cv2.destroyAllWindows()
    print("[MAIN] Shutdown complete.")

def signal_handler(sig, frame):
    shutdown("Ctrl+C")
    sys.exit(0)


def simple_db_worker():
    """Temporary DB worker — drains db_queue so it never fills up."""
    while not shutdown_event.is_set():
        try:
            msg = db_queue.get(timeout=1)
            if msg == SHUTDOWN_SENTINEL:
                break
            action = msg.get("action", "")
            if action == "CHECKPOINT":
                update_checkpoint(
                    msg["session_id"],
                    msg["perclos"],
                    msg["threshold"]
                )
            # INSERT_EVENT handled properly in Week 2 StorageEngine
        except queue.Empty:
            continue
        except Exception as e:
            print(f"[DB_WORKER] Error: {e}")


def simple_alert_worker():
    """Temporary alert worker — prints alerts to console."""
    while not shutdown_event.is_set():
        try:
            msg = alert_queue.get(timeout=1)
            if msg == SHUTDOWN_SENTINEL:
                break
            if isinstance(msg, dict):
                level = msg.get("level", 0)
                ear   = msg.get("ear", 0)
                pc    = msg.get("perclos", 0)
                print(f"\n[ALERT]  LEVEL {level} | "
                      f"EAR={ear:.3f} | PERCLOS={pc:.1f}%")
                print(f"[ALERT] Press SPACE to acknowledge\n")
        except queue.Empty:
            continue
        except Exception as e:
            print(f"[ALERT_WORKER] Error: {e}")


def demo_login():
    print("\n" + "="*55)
    print("  FATIGUE DETECTION — DAY 2 TEST")
    print("="*55)
    print("  Operator : OP001 — Demo Operator")
    print("  Controls : Q = quit | SPACE = acknowledge alert")
    print("="*55)
    input("\n  Press ENTER to start monitoring...\n")

    session_id = open_session("OP001", demo_mode=False)
    with session_lock:
        session_state["session_id"]    = session_id
        session_state["operator_id"]   = "OP001"
        session_state["operator_name"] = "Demo Operator"
        session_state["start_time"]    = time.time()

    print(f"[MAIN] Session opened: {session_id}")
    state_machine.transition(SystemState.CALIBRATING)
    # Skip calibration for Day 2 — go straight to monitoring
    state_machine.transition(SystemState.MONITORING)
    print("[MAIN] State: MONITORING — detection active\n")
    return session_id


def display_loop():
    last_print = time.time()
    print("[MAIN] Video window open. Press Q to quit.\n")

    while not shutdown_event.is_set():
        # Get latest annotated frame
        with frame_lock:
            frame = frame_buffer.get("annotated") or frame_buffer.get("frame")

        if frame is not None:
            cv2.imshow("Fatigue Detection ", frame)

        # Print metrics every 2 seconds
        now = time.time()
        if now - last_print >= 2.0:
            with session_lock:
                ear   = session_state.get("current_ear",    0.0)
                mar   = session_state.get("current_mar",    0.0)
                pitch = session_state.get("current_pitch",  0.0)
                pc    = session_state.get("perclos_current",0.0)
                fs    = session_state.get("fatigue_score",  0.0)
                fps   = session_state.get("fps",            0)
                face  = session_state.get("face_detected",  False)
                bri   = session_state.get("brightness",     0.0)

            face_icon = "Ok" if face else "Not ok"
            print(f"[METRICS] {face_icon} Face | "
                  f"EAR={ear:.3f} | MAR={mar:.3f} | "
                  f"PITCH={pitch:.1f}° | PERCLOS={pc:.1f}% | "
                  f"Score={fs:.3f} | FPS={fps} | Bright={bri:.0f}")
            last_print = now

        # Key handling
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:
            print("[MAIN] Q pressed — shutting down.")
            shutdown("User pressed Q")
            break
        elif key == ord(' '):
            # Spacebar = acknowledge alert
            ack_event.set()
            print("[MAIN]  Alert acknowledged by operator.")
            ack_event.clear()

    cv2.destroyAllWindows()


# MAIN

def main():
    print("\n" + "="*55)
    print("  OPERATOR FATIGUE DETECTION SYSTEM")
    print("  Phase 1 — Day 2 — Camera + MediaPipe")
    print("="*55)

    signal.signal(signal.SIGINT, signal_handler)

    # Step 1 — Database
    print("\n[MAIN] Initializing database...")
    create_tables()
    verify_database()
    print("[MAIN]  Database ready.\n")

    #  Crash check
    crashed = startup_check()

    # Audit log
    write_audit_log("SYSTEM_START", "SYSTEM",
                    detail=f"Day 2 start. Crash recovery: {crashed}")

    #  Login
    if not crashed:
        demo_login()

    #  Start temporary worker threads
    db_worker_thread = threading.Thread(
        target=simple_db_worker,
        name="TempDBWorker",
        daemon=True
    )
    db_worker_thread.start()
    threads["db_worker"] = db_worker_thread

    alert_worker_thread = threading.Thread(
        target=simple_alert_worker,
        name="TempAlertWorker",
        daemon=True
    )
    alert_worker_thread.start()
    threads["alert_worker"] = alert_worker_thread

    # Start Thread 1 (Detection)
    print("[MAIN] Starting Thread 1 — Detection...")
    detection_thread = DetectionThread(
        state_machine, frame_buffer, frame_lock,
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock
    )
    detection_thread.start()
    threads["detection"] = detection_thread

    # Give Thread 1 time to initialize camera and MediaPipe
    print("[MAIN] Waiting for camera to initialize...")
    time.sleep(2)

    #  Display loop on main thread
    display_loop()

    #  Clean shutdown
    shutdown("Display loop ended")

if __name__ == "__main__":
    main()