# Thread 1: Detection   Thread 3: AlertEngine
# Thread 4: Storage     Thread 5: PredictiveEngine
# SessionManager, RiskScorer, AnalyticsEngine, CrashRecovery


import sys, signal, threading, queue, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore    import QTimer

from core.state_machine    import StateMachine, SystemState
from core.database         import create_tables, verify_database, write_audit_log
from core.detection        import DetectionThread
from core.alert_engine     import AlertEngine
from core.storage_engine   import StorageEngine
from core.session_manager  import SessionManager
from core.crash_recovery   import (run_startup_recovery, DetectionWatchdog,
                                    refire_unacknowledged_alerts)
from core.predictive_engine import PredictiveEngine
from core.risk_scorer       import RiskScorer
from core.analytics_engine  import AnalyticsEngine
from ui.dashboard           import DashboardWindow
from config                 import ALERT_QUEUE_MAX, DB_QUEUE_MAX


# SHARED OBJECTS
# Across the multiple USP
state_machine  = StateMachine()
alert_queue    = queue.Queue(maxsize=ALERT_QUEUE_MAX)
db_queue       = queue.Queue(maxsize=DB_QUEUE_MAX)
frame_buffer   = {"frame": None, "annotated": None, "raw": None}
frame_lock     = threading.Lock()
ack_event      = threading.Event()
shutdown_event = threading.Event()

session_state = {
    # Identity
    "session_id":        None,
    "operator_id":       None,
    "operator_name":     None,
    "start_time":        None,
    "demo_mode":         False,
    # Calibration
    "baseline_ear":      0.30,
    "baseline_mar":      0.10,
    "baseline_pitch":    2.0,
    "glasses_mode":      False,
    "drowsy_at_start":   False,
    # Live metrics
    "current_ear":       0.0,
    "current_mar":       0.0,
    "current_pitch":     0.0,
    "fps":               0,
    "brightness":        100.0,
    "face_detected":     True,
    "perclos_current":   0.0,
    "fatigue_score":     0.0,
    # Alerts
    "alert_level":       0,
    "alert_active":      False,
    "alert_fired_time":  0.0,
    "threshold_raised":  0.0,
    "ack_times":         [],
    # USP 4 — Predictive engine
    "fatigue_trend_slope": 0.0,
    "fatigue_trend_r2":    0.0,
    "fatigue_eta_sec":     None,
    # USP 1 — Risk score
    "risk_score":        0.0,
    "risk_band":         "GREEN",
    # Storage
    "storage_free_mb":   9999.0,
    "storage_warning":   False,
    "storage_critical":  False,
    "last_report_path":  None,
    "camera_stalled":    False,
}
session_lock      = threading.Lock()
threads           = {}
SHUTDOWN_SENTINEL = "SHUTDOWN"

# USP instances (created once, reused across sessions)
window             = None
session_manager    = None
predictive_engine  = None
risk_scorer        = None
analytics_engine   = AnalyticsEngine()   # stateless — no reset needed
watchdog           = None



# DETECTION THREAD — start / restart 

def start_detection_thread():
    old = threads.get("detection")
    if old and old.is_alive():
        print("[MAIN] Stopping old DetectionThread...")
        old_shutdown = threading.Event()
        old.shutdown_event = old_shutdown
        old_shutdown.set()
        old.join(timeout=3)

    det = DetectionThread(
        state_machine, frame_buffer, frame_lock,
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock
    )
    det.start()
    threads["detection"] = det
    print("[MAIN] DetectionThread started.")
    return det



# SHUTDOWN
# End of the shift 
def shutdown(reason: str = "User requested"):
    if shutdown_event.is_set():
        return
    print(f"\n[MAIN] Shutting down: {reason}")

    if session_manager:
        with session_lock:
            sid = session_state.get("session_id")
        if sid:
            session_manager.end_session(reason=f"System shutdown: {reason}")
            time.sleep(1.2)

    shutdown_event.set()
    ack_event.set()

    for q in (alert_queue, db_queue):
        try: q.put_nowait(SHUTDOWN_SENTINEL)
        except queue.Full: pass

    for name, t in threads.items():
        if t and t.is_alive():
            t.join(timeout=3)
            print(f"[MAIN] {'OK' if not t.is_alive() else 'NO'} "
                  f"Thread '{name}' exited")

    write_audit_log("SHUTDOWN", "SYSTEM", detail=reason)
    print("[MAIN]  Shutdown complete.")
    app = QApplication.instance()
    if app:
        QTimer.singleShot(200, app.quit)


def signal_handler(sig, frame):
    shutdown("Ctrl+C")



# UI CALLBACKS

def handle_login(operator_id: str, demo_mode: bool):
    # Start Thread 1 if needed
    if "detection" not in threads or not threads["detection"].is_alive():
        start_detection_thread()
        time.sleep(2.0)

    # Reset USP engines for new session
    if predictive_engine:
        predictive_engine.reset()
    if risk_scorer:
        risk_scorer.reset()

    session_manager.start_session(operator_id, demo_mode=demo_mode)

    if window:
        window.set_calibration_manager(session_manager)


def handle_end_shift():
    if session_manager:
        session_manager.end_session(reason="Operator ended shift")



# RISK SCORE REFRESH (called by Qt timer every 60s)(We can change the time )
# Help for the new usp
def refresh_risk_score():
    if not risk_scorer:
        return
    with session_lock:
        sid = session_state.get("session_id")
    if not sid:
        return
    try:
        result = risk_scorer.compute()
        with session_lock:
            session_state["risk_score"] = result["score"]
            session_state["risk_band"]  = result["band"]
    except Exception as e:
        print(f"[MAIN] Risk score error: {e}")


# MAIN
# Entry Point for the main file 

def main():
    global window, session_manager, predictive_engine, risk_scorer, watchdog

    print("\n" + "="*60)
    print("  OPERATOR FATIGUE DETECTION SYSTEM")
    print("  Full USP build: Predict · Score · Analyse · Comply")
    print("="*60)

    signal.signal(signal.SIGINT, signal_handler)

    # Step 1 — DB integrity check + crash recovery
    print("\n[MAIN] Running startup recovery...")
    recovery = run_startup_recovery(
        session_state, session_lock, state_machine, alert_queue
    )
    create_tables()
    verify_database()
    print("[MAIN]  Database ready.\n")
    write_audit_log("SYSTEM_START", "SYSTEM",
                    detail=f"USP build. Recovered={recovery['recovered']}")

    # Step 2 — Thread 4: StorageEngine
    print("[MAIN] Starting Thread 4 — StorageEngine...")
    storage = StorageEngine(db_queue, shutdown_event, session_state, session_lock)
    storage.start()
    threads["storage"] = storage
    time.sleep(0.2)

    # Step 3 — Thread 3: AlertEngine
    print("[MAIN] Starting Thread 3 — AlertEngine...")
    alert = AlertEngine(
        alert_queue, db_queue, ack_event, shutdown_event,
        session_state, session_lock, language="marathi"
    )
    alert.start()
    threads["alert"] = alert
    time.sleep(0.2)

    # Step 4 — Thread 5: PredictiveEngine 
    print("[MAIN] Starting Thread 5 — PredictiveEngine...")
    predictive_engine = PredictiveEngine(
        session_state, session_lock,
        alert_queue, db_queue,
        shutdown_event, state_machine
    )
    predictive_engine.start()
    threads["predictive"] = predictive_engine
    time.sleep(0.1)

    # Step 5 — Risk scorer not a thread, called on demand
    risk_scorer = RiskScorer(
        session_state, session_lock,
        predictive_engine=predictive_engine
    )
    print("[MAIN] Risk scorer ready.")

    # Step 6 — Session manager
    session_manager = SessionManager(
        session_state, session_lock,
        state_machine, db_queue,
        shutdown_event, ack_event,
    )

    # Step 7 — Qt app + dashboard
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

    window.end_shift_btn.clicked.disconnect()
    window.end_shift_btn.clicked.connect(handle_end_shift)

    def _on_session_ended():
        if window:
            QTimer.singleShot(0, lambda: window.stack.setCurrentIndex(0))
    session_manager.on_session_ended_callback = _on_session_ended

    # Risk score refresh timer (every 60 seconds on Qt thread)
    risk_timer = QTimer()
    risk_timer.timeout.connect(refresh_risk_score)
    risk_timer.start(60_000)

    # Step 8 — Watchdog (Thread 1 freeze detection)
    print("[MAIN] Starting DetectionWatchdog...")
    watchdog = DetectionWatchdog(
        session_state, session_lock,
        state_machine, alert_queue,
        shutdown_event,
        restart_callback=start_detection_thread
    )
    watchdog.start()
    threads["watchdog"] = watchdog

    # Step 9 — Crash recovery path
    if recovery["recovered"]:
        crashed_session = recovery["session"]
        op_id = crashed_session.get("operator_id", "UNKNOWN")
        print(f"\n[MAIN] Resuming crashed session for operator {op_id}...")

        with session_lock:
            session_state["operator_name"] = f"Operator {op_id}"
            session_state["start_time"]    = time.time()

        start_detection_thread()
        time.sleep(2.5)

        if recovery["needs_calibration"]:
            session_manager._start_calibration(op_id, demo_mode=False)
        else:
            from core.database import get_operator
            op = get_operator(op_id)
            if op:
                session_manager._load_stored_baseline(dict(op))

        if recovery["unacked_alerts"]:
            refire_unacknowledged_alerts(
                recovery["unacked_alerts"],
                alert_queue, session_state, session_lock
            )

        window.stack.setCurrentIndex(1)

    # Step 10 — Launch
    window.show()
    print("\n[MAIN] System ready.\n")
    print("  Threads running:")
    print("    T1 — DetectionThread (starts on first login)")
    print("    T3 — AlertEngine      ok")
    print("    T4 — StorageEngine    ok")
    print("    T5 — PredictiveEngine  ok")
    print("    Watchdog               ok")
    print("    RiskScorer              ok(on-demand)")
    print("    AnalyticsEngine        ok(on-demand)")
    print()

    exit_code = app.exec_()
    if not shutdown_event.is_set():
        shutdown("Qt event loop ended")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()