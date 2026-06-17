import sys, signal, threading, queue, time, os, cv2
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.state_machine  import StateMachine, SystemState
from core.database       import (
    create_tables, verify_database, get_active_session,
    mark_session_crashed, write_audit_log,
    open_session, close_session
)
from core.detection      import DetectionThread
from core.alert_engine   import AlertEngine
from core.storage_engine import StorageEngine
from core.calibration    import CalibrationManager, CalibrationError
from config              import ALERT_QUEUE_MAX, DB_QUEUE_MAX, DEMO_CALIBRATION_SEC


# SHARED OBJECTS — created once, passed to every thread

state_machine  = StateMachine()
alert_queue    = queue.Queue(maxsize=ALERT_QUEUE_MAX)
db_queue       = queue.Queue(maxsize=DB_QUEUE_MAX)
frame_buffer   = {"frame": None, "annotated": None, "raw": None}
frame_lock     = threading.Lock()
ack_event      = threading.Event()
shutdown_event = threading.Event()

session_state = {
    "session_id":        None,
    "operator_id":       None,
    "operator_name":     None,
    "start_time":        None,
    "demo_mode":         False,
    "baseline_ear":      0.30,
    "baseline_mar":      0.10,
    "baseline_pitch":    2.0,
    "glasses_mode":      False,
    "drowsy_at_start":   False,
    "perclos_current":   0.0,
    "fatigue_score":     0.0,
    "face_detected":     True,
    "threshold_raised":  0.0,
    "brightness":        100.0,
    "current_ear":       0.0,
    "current_mar":       0.0,
    "current_pitch":     0.0,
    "fps":               0,
    "alert_level":       0,
    "alert_active":      False,
    "alert_fired_time":  0.0,
    "ack_times":         [],
    "storage_free_mb":   9999.0,
    "storage_warning":   False,
    "storage_critical":  False,
    "last_report_path":  None,
}
session_lock      = threading.Lock()
threads           = {}
SHUTDOWN_SENTINEL = "SHUTDOWN"


# STARTUP — CRASH RECOVERY
# Keep the logic simple — if we find an active session in the DB, we assume the system crashed during that session and we restore the state from the last checkpoint. We then mark that session as "crashed" in the DB for audit purposes. The system will then continue to run as normal, allowing the operator to finish their shift and generate a report at the end. This way we don't lose all data from that session and we can still analyze it in the report, while also keeping a clear record of the crash in the audit logs.
# We could potentially add more sophisticated recovery logic in the future, such as trying to estimate how long the system was down based on the last checkpoint time and adjusting the fatigue score accordingly, but for now we keep it simple and just restore the last known state.
def startup_check():
    print("[MAIN] Running startup check...")
    active = get_active_session()
    if active:
        print(f"[MAIN]  Crashed session: {active['session_id']}")
        mark_session_crashed(active["session_id"])
        try:
            start_ts = datetime.fromisoformat(active["start_time"]).timestamp()
        except Exception:
            start_ts = time.time()
        with session_lock:
            session_state.update({
                "session_id":       active["session_id"],
                "operator_id":      active["operator_id"],
                "start_time":       start_ts,
                "perclos_current":  active["perclos_checkpoint"],
                "threshold_raised": active["threshold_raised"],
            })
        write_audit_log("CRASH_RECOVERY", "SYSTEM", active["session_id"],
                        f"Restored PERCLOS={active['perclos_checkpoint']:.2f}%")
        print("[MAIN]  Crash state restored.")
        state_machine.transition(SystemState.CRASH_RECOVERY)
        state_machine.transition(SystemState.MONITORING)
        return True
    print("[MAIN]  Clean start.")
    state_machine.transition(SystemState.WAITING_OPERATOR)
    return False

# SHUTDOWN — clean 4-thread exit

def shutdown(reason: str = "User requested"):
    if shutdown_event.is_set():
        return
    print(f"\n[MAIN] Shutting down: {reason}")

    # Close active session — triggers PDF generation in Thread 4 (To avoid race conditions, we set session_id to None before putting the close action in the queue, so that no new data is written to the DB while we're closing.)
    with session_lock:
        sid   = session_state.get("session_id")
        oname = session_state.get("operator_name", "Unknown")

    if sid:
        try:
            db_queue.put_nowait({
                "action":          "SESSION_CLOSE",
                "session_id":      sid,
                "operator_name":   oname,
                "generate_report": True,
            })
        except queue.Full:
            pass
        time.sleep(1.5)   # give Thread 4 time to generate PDF

    # Signal all threads
    shutdown_event.set()
    ack_event.set()

    # Put SHUTDOWN sentinels in queues
    for q in (alert_queue, db_queue):
        try:
            q.put_nowait(SHUTDOWN_SENTINEL)
        except queue.Full:
            pass

    # Join all threads gracefully
    for name, t in threads.items():
        if t and t.is_alive():
            t.join(timeout=3)
            status = "Okay" if not t.is_alive() else " force"
            print(f"[MAIN] {status} Thread '{name}' exited")

    cv2.destroyAllWindows()
    print("[MAIN]  Shutdown complete.")

def signal_handler(sig, frame):
    shutdown("Ctrl+C")
    sys.exit(0)


# LOGIN

def demo_login(demo_mode: bool = False) -> str:
    
    print("  OPERATOR FATIGUE DETECTION SYSTEM")
    
    print("  Operator : OP001 — Demo Operator")
    print("  Controls : SPACE=ack  D=demo mode  Q=quit")
    
    inp = input("\n  Press ENTER to start (or type 'demo' for demo mode): ").strip().lower()
    if inp == "demo":
        demo_mode = True
        print("  [Demo mode ON — fast alerts enabled]")

    sid = open_session("OP001", demo_mode=demo_mode)
    with session_lock:
        session_state.update({
            "session_id":    sid,
            "operator_id":   "OP001",
            "operator_name": "Demo Operator",
            "start_time":    time.time(),
            "demo_mode":     demo_mode,
        })
    print(f"\n[MAIN] Session: {sid}")
    return sid, demo_mode


# CALIBRATION (runs in a background thread during startup)
# 2 minutes of calibration data is ideal for good baselines, but for demo purposes we can do a quick 10-second calibration with fixed thresholds. The CalibrationManager handles both modes internally based on the demo_mode flag.
def run_calibration(operator_id: str, demo_mode: bool):
    
    state_machine.transition(SystemState.CALIBRATING)
    print(f"\n[MAIN] Starting calibration "
          f"({'DEMO — 10s' if demo_mode else '2 minutes'})...")
    print("[MAIN] Look straight at the camera and stay relaxed.\n")

    cal = CalibrationManager(session_state, session_lock, shutdown_event)

    try:
        result = cal.run(
            operator_id = operator_id,
            demo_mode   = demo_mode
        )
        print(f"\n[MAIN] Calibration complete:")
        print(f"         EAR baseline   = {result['baseline_ear']:.3f}"
              f"  {'(GLASSES)' if result['glasses_mode'] else ''}")
        print(f"         MAR baseline   = {result['baseline_mar']:.3f}")
        print(f"         PITCH baseline = {result['baseline_pitch']:.1f}°")
        if result.get("drowsy_at_start"):
            print(f"           Operator appears fatigued at shift start")

        state_machine.transition(SystemState.MONITORING)
        print("[MAIN]  MONITORING active — fatigue detection running\n")

    except CalibrationError as e:
        print(f"\n[MAIN] Calibration failed: {e}")
        print("[MAIN] Using default thresholds — monitoring active")
        state_machine.transition(SystemState.MONITORING)


# DISPLAY LOOP 
# Need to change when migrating to PyQt5 in progress

def _draw_alert_overlay(frame):
    import numpy as np
    with session_lock:
        level = session_state.get("alert_level", 0)
        demo  = session_state.get("demo_mode",   False)
        cal   = state_machine.state == SystemState.CALIBRATING

    h, w = frame.shape[:2]

    # Calibration overlay
    if cal:
        cv2.rectangle(frame, (0, h-42), (w, h), (40, 120, 40), -1)
        cv2.putText(frame, "CALIBRATING — look straight at camera",
                    (10, h-14), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255,255,255), 2)
        return frame

    # Alert overlay
    colors_map = {1:(0,165,255), 2:(0,0,255), 3:(0,0,180)}
    texts_map  = {
        1: "L1 — MILD FATIGUE",
        2: "L2 — DROWSY — PRESS SPACE TO ACK",
        3: "L3 — CRITICAL — STOP MACHINE NOW!"
    }
    if level in colors_map:
        cv2.rectangle(frame, (0, h-42), (w, h), colors_map[level], -1)
        cv2.putText(frame, texts_map[level], (10, h-14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255,255,255), 2)

    if demo:
        cv2.putText(frame, "DEMO MODE", (w-130, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)
    return frame


def display_loop():
    last_print = time.time()
    print("[MAIN] Video window open. Q=quit SPACE=ack D=demo mode\n")

    while not shutdown_event.is_set():
        with frame_lock:
            frame = frame_buffer.get("annotated")
            if frame is None:
                frame = frame_buffer.get("frame")

        if frame is not None:
            frame = _draw_alert_overlay(frame)
            cv2.imshow("Fatigue Detection — Phase 1", frame)

        now = time.time()
        if now - last_print >= 2.0:
            with session_lock:
                ear   = session_state.get("current_ear",    0.0)
                mar   = session_state.get("current_mar",    0.0)
                pitch = session_state.get("current_pitch",  0.0)
                pc    = session_state.get("perclos_current",0.0)
                fs    = session_state.get("fatigue_score",  0.0)
                fps   = session_state.get("fps",            0)
                alv   = session_state.get("alert_level",    0)
                thr   = session_state.get("threshold_raised",0.0)
                demo  = session_state.get("demo_mode",      False)
                rpt   = session_state.get("last_report_path", None)

            st = state_machine.state.name
            alert_icon = f" L{alv}" if alv > 0 else "Corrective"
            print(f"[{st}] {alert_icon} "
                  f"EAR={ear:.3f} MAR={mar:.3f} PITCH={pitch:.1f}° "
                  f"PERCLOS={pc:.1f}% Score={fs:.3f} FPS={fps}"
                  f"{' [DEMO]' if demo else ''}"
                  f"{' [+'+str(round(thr,2))+']' if thr>0 else ''}")
            # Print report path if available
            if rpt:
                print(f"[MAIN]  Last report: {os.path.basename(rpt)}")
                with session_lock:
                    session_state["last_report_path"] = None

            last_print = now

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:
            shutdown("User pressed Q")
            break
        elif key == ord(' '):
            ack_event.set()
            print("[MAIN] Alert acknowledged")
            time.sleep(0.1)
            ack_event.clear()
        elif key == ord('d'):
            with session_lock:
                dm = not session_state.get("demo_mode", False)
                session_state["demo_mode"] = dm
            print(f"[MAIN] Demo mode: {'ON ' if dm else 'OFF'}")

    cv2.destroyAllWindows()


# MAIN
# Entry point of the application — initializes DB, starts threads, runs display loop
def main():
    print("\n" + "="*55)
    print("  OPERATOR FATIGUE DETECTION SYSTEM")
    print("  Phase 1 — Full pipeline with calibration + reports")
    print("="*55)

    signal.signal(signal.SIGINT, signal_handler)

    # Step 1 — DB
    print("\n[MAIN] Initializing database...")
    create_tables()
    verify_database()
    print("[MAIN]  Database ready.\n")

    # Step 2 — Crash check
    crashed = startup_check()
    write_audit_log("SYSTEM_START", "SYSTEM",
                    detail=f"Full pipeline. Crash recovery: {crashed}")

    # Step 3 — Login
    if not crashed:
        sid, demo_mode = demo_login()
    else:
        demo_mode = False

    # Step 4 — Thread 4: StorageEngine (start first — must be ready before alerts)
    print("[MAIN] Starting Thread 4 — StorageEngine...")
    storage_thread = StorageEngine(
        db_queue, shutdown_event, session_state, session_lock
    )
    storage_thread.start()
    threads["storage"] = storage_thread
    time.sleep(0.2)

    # Step 5 — Thread 3: AlertEngine
    print("[MAIN] Starting Thread 3 — AlertEngine...")
    alert_thread = AlertEngine(
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock, language="marathi"
    )
    alert_thread.start()
    threads["alert"] = alert_thread
    time.sleep(0.2)

    # Step 6 — Thread 1: DetectionThread
    print("[MAIN] Starting Thread 1 — DetectionThread...")
    detection_thread = DetectionThread(
        state_machine, frame_buffer, frame_lock,
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock
    )
    detection_thread.start()
    threads["detection"] = detection_thread

    print("[MAIN] Waiting for camera to initialize (2s)...")
    time.sleep(2.5)

    # Step 7 — Calibration in background thread
    if not crashed:
        cal_thread = threading.Thread(
            target = run_calibration,
            args   = ("OP001", demo_mode),
            name   = "CalibrationThread",
            daemon = True
        )
        cal_thread.start()
        threads["calibration"] = cal_thread

    # Step 8 — Display loop on main thread
    display_loop()

    # Step 9 — Shutdown
    shutdown("Display loop ended")


if __name__ == "__main__":
    main()