import sys, signal, threading, queue, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore    import QTimer

from core.state_machine   import StateMachine, SystemState
from core.database        import (
    create_tables, verify_database, get_active_session,
    mark_session_crashed, write_audit_log
)
from core.detection       import DetectionThread
from core.alert_engine    import AlertEngine
from core.storage_engine  import StorageEngine
from core.session_manager import SessionManager
from ui.dashboard         import DashboardWindow
from config               import ALERT_QUEUE_MAX, DB_QUEUE_MAX


# SHARED OBJECTS — live for the full process lifetime

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
    "current_ear":       0.0,
    "current_mar":       0.0,
    "current_pitch":     0.0,
    "fps":               0,
    "brightness":        100.0,
    "face_detected":     True,
    "perclos_current":   0.0,
    "fatigue_score":     0.0,
    "alert_level":       0,
    "alert_active":      False,
    "alert_fired_time":  0.0,
    "threshold_raised":  0.0,
    "ack_times":         [],
    "storage_free_mb":   9999.0,
    "storage_warning":   False,
    "storage_critical":  False,
    "last_report_path":  None,
}
session_lock      = threading.Lock()
threads           = {}
SHUTDOWN_SENTINEL = "SHUTDOWN"
window            = None
session_manager   = None



# CRASH RECOVERY
# If any kind of crash happen during the exection

def startup_check() -> bool:
    print("[MAIN] Running startup check...")
    active = get_active_session()
    if not active:
        print("[MAIN] Clean start.")
        state_machine.transition(SystemState.WAITING_OPERATOR)
        return False

    print(f"[MAIN] Crashed session found: {active['session_id']}")
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



# SHUTDOWN
# when user want to shutdown the after completing the shift

def shutdown(reason: str = "User requested"):
    if shutdown_event.is_set():
        return
    print(f"\n[MAIN] Shutting down: {reason}")

    if session_manager:
        with session_lock:
            sid = session_state.get("session_id")
        if sid:
            session_manager.end_session(reason=f"System shutdown: {reason}")
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
            print(f"[MAIN] {'OK' if not t.is_alive() else 'FORCE'} "
                  f"Thread '{name}' exited")

    write_audit_log("SHUTDOWN", "SYSTEM", detail=reason)
    print("[MAIN] Shutdown complete.")
    app = QApplication.instance()
    if app:
        QTimer.singleShot(200, app.quit)


def signal_handler(sig, frame):
    shutdown("Ctrl+C")



# UI CALLBACKS
# Current is static mode

def handle_login(operator_id: str, demo_mode: bool):
    if "detection" not in threads or not threads["detection"].is_alive():
        print("[MAIN] Starting Thread 1 — DetectionThread...")
        det = DetectionThread(
            state_machine, frame_buffer, frame_lock,
            alert_queue, db_queue, ack_event, shutdown_event,
            session_state, session_lock
        )
        det.start()
        threads["detection"] = det
        time.sleep(2.0)

    session_manager.start_session(operator_id, demo_mode=demo_mode)

    if window:
        window.set_calibration_manager(session_manager)


def handle_end_shift():
    if session_manager:
        session_manager.end_session(reason="Operator ended shift")



# MAIN
# Entry point of the program
def main():
    global window, session_manager


    signal.signal(signal.SIGINT, signal_handler)

    print("\n[MAIN] Initializing database...")
    create_tables()
    verify_database()

    crashed = startup_check()
    write_audit_log("SYSTEM_START", "SYSTEM",
                    detail=f"Day 8 start. Crash recovery: {crashed}")

    # Thread 4 first
    print("[MAIN] Starting Thread 4 — StorageEngine...")
    storage = StorageEngine(db_queue, shutdown_event, session_state, session_lock)
    storage.start()
    threads["storage"] = storage
    time.sleep(0.2)

    # Thread 3
    print("[MAIN] Starting Thread 3 — AlertEngine...")
    alert = AlertEngine(
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock, language="marathi"
    )
    alert.start()
    threads["alert"] = alert
    time.sleep(0.2)

    # Session manager
    session_manager = SessionManager(
        session_state, session_lock,
        state_machine, db_queue,
        shutdown_event, ack_event,
    )

    # Qt app + dashboard
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

    # Wire end shift button to session manager
    window.end_shift_btn.clicked.disconnect()
    window.end_shift_btn.clicked.connect(handle_end_shift)

    # When session ends, switch UI back to login screen
    def _on_session_ended():
        if window:
            QTimer.singleShot(0, lambda: window.stack.setCurrentIndex(0))
    session_manager.on_session_ended_callback = _on_session_ended

    # Crash recovery path
    if crashed:
        print("[MAIN] Starting Thread 1 for crash recovery...")
        det = DetectionThread(
            state_machine, frame_buffer, frame_lock,
            alert_queue, db_queue, ack_event, shutdown_event,
            session_state, session_lock
        )
        det.start()
        threads["detection"] = det
        window.stack.setCurrentIndex(1)

    window.show()
    print("\n[MAIN] Dashboard ready. Waiting for operator login.\n")

    exit_code = app.exec_()
    if not shutdown_event.is_set():
        shutdown("Qt event loop ended")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()