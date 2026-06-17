import sys, signal, threading, queue, time, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

from core.state_machine  import StateMachine, SystemState
from core.database       import (
    create_tables, verify_database, get_active_session,
    mark_session_crashed, write_audit_log, open_session, close_session
)
from core.detection      import DetectionThread
from core.alert_engine   import AlertEngine
from core.storage_engine import StorageEngine
from core.calibration    import CalibrationManager, CalibrationError
from ui.dashboard        import DashboardWindow
from config              import ALERT_QUEUE_MAX, DB_QUEUE_MAX


# SHARED OBJECTS

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
threads            = {}
SHUTDOWN_SENTINEL  = "SHUTDOWN"

window = None   # set in main(), referenced by callbacks


# STARTUP — CRASH RECOVERY
def startup_check():
    print("[MAIN] Running startup check...")
    active = get_active_session()
    if active:
        print(f"[MAIN]  Crashed session: {active['session_id']}")
        mark_session_crashed(active["session_id"])
        with session_lock:
            session_state.update({
                "session_id":       active["session_id"],
                "operator_id":      active["operator_id"],
                "perclos_current":  active["perclos_checkpoint"],
                "threshold_raised": active["threshold_raised"],
            })
        write_audit_log("CRASH_RECOVERY", "SYSTEM", active["session_id"],
                        f"Restored PERCLOS={active['perclos_checkpoint']:.2f}%")
        state_machine.transition(SystemState.CRASH_RECOVERY)
        state_machine.transition(SystemState.MONITORING)
        return True
    state_machine.transition(SystemState.WAITING_OPERATOR)
    return False


# Shutdown procedure — called from signal handler or Qt exit

def shutdown(reason: str = "User requested"):
    if shutdown_event.is_set():
        return
    print(f"\n[MAIN] Shutting down: {reason}")

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
        time.sleep(1.0)

    shutdown_event.set()
    ack_event.set()

    for q in (alert_queue, db_queue):
        try:
            q.put_nowait(SHUTDOWN_SENTINEL)
        except queue.Full:
            pass

    for name, t in threads.items():
        if t and t.is_alive():
            t.join(timeout=3)
            status = "✅" if not t.is_alive() else "⚠️ force"
            print(f"[MAIN] {status} Thread '{name}' exited")

    print("[MAIN]  Shutdown complete.")
    app = QApplication.instance()
    if app:
        QTimer.singleShot(200, app.quit)


def signal_handler(sig, frame):
    shutdown("Ctrl+C")


# help to provide the better user experience, we can add a calibration screen that shows the progress of the calibration process. This screen will be displayed during the CALIBRATING state and will poll the CalibrationManager for updates. The screen will include a progress bar, status message, and face detection rate indicator.
# CALIBRATION (runs in background thread)

calibration_manager_holder = {"manager": None}

def run_calibration(operator_id: str, demo_mode: bool):
    state_machine.transition(SystemState.CALIBRATING)
    print(f"\n[MAIN] Starting calibration "
          f"({'DEMO — 10s' if demo_mode else '2 minutes'})...")

    cal = CalibrationManager(session_state, session_lock, shutdown_event)
    calibration_manager_holder["manager"] = cal
    if window:
        window.set_calibration_manager(cal)

    try:
        result = cal.run(operator_id=operator_id, demo_mode=demo_mode)
        print(f"[MAIN]  Calibration complete: "
              f"EAR={result['baseline_ear']:.3f} "
              f"glasses={result['glasses_mode']}")
        state_machine.transition(SystemState.MONITORING)
    except CalibrationError as e:
        print(f"[MAIN]  Calibration failed: {e}")
        state_machine.transition(SystemState.MONITORING)


# =============================================================
# LOGIN CALLBACK — invoked by DashboardWindow on login
# =============================================================
def handle_login(operator_id: str, demo_mode: bool):
    sid = open_session(operator_id, demo_mode=demo_mode)
    with session_lock:
        session_state.update({
            "session_id":    sid,
            "operator_id":   operator_id,
            "operator_name": f"Operator {operator_id}",
            "start_time":    time.time(),
            "demo_mode":     demo_mode,
        })
    print(f"[MAIN] Session opened: {sid}")

    # Start detection thread now (after login, not before)
    if "detection" not in threads:
        print("[MAIN] Starting Thread 1 — DetectionThread...")
        det = DetectionThread(
            state_machine, frame_buffer, frame_lock,
            alert_queue, db_queue, ack_event, shutdown_event,
            session_state, session_lock
        )
        det.start()
        threads["detection"] = det
        time.sleep(2.0)   # let camera + model initialize

    # Start calibration in background
    cal_thread = threading.Thread(
        target=run_calibration, args=(operator_id, demo_mode),
        name="CalibrationThread", daemon=True
    )
    cal_thread.start()
    threads["calibration"] = cal_thread



# MAIN
# Entry point for the application. Initializes database, starts threads, and launches PyQt5 dashboard.
def main():
    global window

    print("\n" + "="*55)
    print("  OPERATOR FATIGUE DETECTION SYSTEM")
    print("  Phase 1 — Day 6 — PyQt5 Dashboard")
    print("="*55)

    signal.signal(signal.SIGINT, signal_handler)

    print("\n[MAIN] Initializing database...")
    create_tables()
    verify_database()
    print("[MAIN]  Database ready.\n")

    crashed = startup_check()
    write_audit_log("SYSTEM_START", "SYSTEM",
                    detail=f"PyQt5 dashboard. Crash recovery: {crashed}")

    # Start Thread 4 (storage) — always needed
    print("[MAIN] Starting Thread 4 — StorageEngine...")
    storage_thread = StorageEngine(
        db_queue, shutdown_event, session_state, session_lock
    )
    storage_thread.start()
    threads["storage"] = storage_thread
    time.sleep(0.2)

    # Start Thread 3 (alerts) — always needed
    print("[MAIN] Starting Thread 3 — AlertEngine...")
    alert_thread = AlertEngine(
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock, language="marathi"
    )
    alert_thread.start()
    threads["alert"] = alert_thread
    time.sleep(0.2)

    # Create Qt application
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = DashboardWindow(
        frame_buffer, frame_lock,
        session_state, session_lock,
        ack_event, shutdown_event,
        state_machine,
        on_login_callback    = handle_login,
        on_shutdown_callback = shutdown,
    )

    if crashed:
        # Already monitoring — start detection thread immediately
        print("[MAIN] Crash recovery — starting detection immediately...")
        det = DetectionThread(
            state_machine, frame_buffer, frame_lock,
            alert_queue, db_queue, ack_event, shutdown_event,
            session_state, session_lock
        )
        det.start()
        threads["detection"] = det
        window.stack.setCurrentIndex(1)

    window.show()
    print("\n[MAIN]  Dashboard window open.\n")

    exit_code = app.exec_()

    if not shutdown_event.is_set():
        shutdown("Qt event loop ended")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()