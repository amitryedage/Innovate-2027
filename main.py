
# main.py — Application entry point
# Creates all shared objects, starts all 4 threads,
# handles clean shutdown sequence.
#Core working start from here 
import sys
import signal
import threading
import queue
import time
import os


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.state_machine import StateMachine, SystemState, InvalidTransitionError
from core.database import (
    create_tables, verify_database, get_active_session,
    mark_session_crashed, write_audit_log, open_session, close_session
)
from config import ALERT_QUEUE_MAX, DB_QUEUE_MAX




# State machine — single source of truth for system state
state_machine = StateMachine()

# Thread communication queues
alert_queue = queue.Queue(maxsize=ALERT_QUEUE_MAX)
db_queue    = queue.Queue(maxsize=DB_QUEUE_MAX)

# Shared frame buffer for UI — protected by lock
frame_buffer      = {"frame": None}
frame_buffer_lock = threading.Lock()

# Events
ack_event      = threading.Event()   # operator presses ack button
shutdown_event = threading.Event()   # signals all threads to exit cleanly

# Session state — shared across threads
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
    "threshold_raised": 0.0,
}
session_lock = threading.Lock()

# Thread references — stored so we can join them on shutdown
threads = {}


SHUTDOWN_SENTINEL = "SHUTDOWN"




def startup_check():
    print("[MAIN] Running startup check...")

    active = get_active_session()

    if active:
        print(f"[MAIN] Crashed session detected: {active['session_id']}")
        print(f"[MAIN] Operator: {active['operator_id']}")
        print(f"[MAIN] Last checkpoint: {active['last_checkpoint']}")
        print(f"[MAIN] PERCLOS at crash: {active['perclos_checkpoint']:.2f}%")

        
        mark_session_crashed(active["session_id"])

        # Restore session state from checkpoint
        with session_lock:
            session_state["session_id"]      = active["session_id"]
            session_state["operator_id"]     = active["operator_id"]
            session_state["perclos_current"] = active["perclos_checkpoint"]
            session_state["threshold_raised"]= active["threshold_raised"]

        write_audit_log("CRASH_RECOVERY", "SYSTEM",
                        active["session_id"],
                        f"Restored PERCLOS={active['perclos_checkpoint']:.2f}%")

        print("[MAIN]  Crash recovery state restored.")
        state_machine.transition(SystemState.CRASH_RECOVERY)
        state_machine.transition(SystemState.MONITORING)
        return True  # crash recovery path

    else:
        print("[MAIN] Clean start — no crashed sessions.")
        state_machine.transition(SystemState.WAITING_OPERATOR)
        return False  # clean start path


def shutdown(reason: str = "User requested"):
    if shutdown_event.is_set():
        return  # already shutting down — ignore duplicate calls

    print(f"\n[MAIN] Initiating shutdown: {reason}")
    write_audit_log("SHUTDOWN", "SYSTEM", detail=reason)

    # Step 1 — Signal all threads to stop
    shutdown_event.set()

    # Step 2 — Unblock ack_event if alert thread is waiting
    ack_event.set()

    # Step 3 — Put SHUTDOWN sentinel in queues
    # Threads listening on these will see sentinel and exit loop
    try:
        alert_queue.put_nowait(SHUTDOWN_SENTINEL)
    except queue.Full:
        pass  # queue full — thread will see shutdown_event anyway

    try:
        db_queue.put_nowait(SHUTDOWN_SENTINEL)
    except queue.Full:
        pass

    # Step 4 — Join all threads with 2 second timeout each
    print("[MAIN] Waiting for threads to exit...")
    for name, thread in threads.items():
        if thread and thread.is_alive():
            thread.join(timeout=2)
            if thread.is_alive():
                print(f"[MAIN]   Thread {name} did not exit cleanly — force continuing")
            else:
                print(f"[MAIN]  Thread {name} exited cleanly")

    print("[MAIN] Shutdown complete.")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    print("\n[MAIN] Ctrl+C received.")
    shutdown("Ctrl+C signal")
    sys.exit(0)



def demo_login():
   
    print("\n" + "="*50)
    print("  FATIGUE DETECTION SYSTEM — DEMO LOGIN")
    print("="*50)
    print("  Operator ID: OP001 (Demo Operator)")
    print("  Press ENTER to login, or type 'quit' to exit")
    print("="*50)

    user_input = input("\n> ").strip().lower()
    if user_input == 'quit':
        shutdown("User quit at login")
        sys.exit(0)

    # Open session
    session_id = open_session("OP001", demo_mode=False)
    with session_lock:
        session_state["session_id"]   = session_id
        session_state["operator_id"]  = "OP001"
        session_state["operator_name"]= "Demo Operator"
        session_state["start_time"]   = time.time()

    print(f"\n[MAIN] Session opened: {session_id}")
    state_machine.transition(SystemState.CALIBRATING)
    return session_id



def main():
    print("\n" + "="*60)
    print("  OPERATOR FATIGUE DETECTION SYSTEM")
    print("  Phase 1 — Laptop Prototype")
    print("="*60)

    # Register Ctrl+C handler
    signal.signal(signal.SIGINT, signal_handler)

    # Step 1 — Initialize database
    print("\n[MAIN] Initializing database...")
    create_tables()
    verify_database()
    print("[MAIN]  Database ready.")

    # Step 2 — Startup crash check
    print("\n[MAIN] Running crash recovery check...")
    crashed = startup_check()

    # Step 3 — Write system start to audit log
    write_audit_log("SYSTEM_START", "SYSTEM",
                    detail=f"System started. Crash recovery: {crashed}")

    # Step 4 — Temporary demo login 
    if not crashed:
        session_id = demo_login()

    # Step 5 — Placeholder for thread starts (Days 2-6)
    print("\n[MAIN]   Thread start points ready.")
    print("[MAIN] Threads will be added here in Days 2-6.")
    print("[MAIN] Current state:", state_machine.state.name)

    # Step 6 — Keep main thread alive until shutdown
    print("\n[MAIN] System running. Press Ctrl+C to exit cleanly.\n")
    try:
        while not shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    shutdown("Main loop exited")


if __name__ == "__main__":
    main()