import os
import queue
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core import database
from core.database import (
    get_active_session, get_operator, mark_session_crashed, write_audit_log
)
from core.state_machine import StateMachine, SystemState
from config import (
    WATCHDOG_TIMEOUT_SEC, WATCHDOG_CHECK_SEC,
    CRASH_RESUME_MAX_GAP_SEC, CRASH_UNACKED_WINDOW_SEC,
    RECALIBRATE_AFTER_DAYS,
)


# STARTUP RECOVERY
# 4 scenarios handled on every boot:
#   1. CLEAN_START          — no ACTIVE session left behind
#   2. SESSION_RESUMED      — recent crash, operator's session is resumed
#   3. STALE_SESSION_CLOSED — crash too long ago, session marked CRASHED
#   4. DB_CORRUPTED         — database quarantined, fresh one created

def run_startup_recovery(session_state: dict, session_lock: threading.Lock,
                         state_machine: StateMachine,
                         alert_queue: queue.Queue) -> dict:
    """
    Called once from main.py before create_tables().
    Moves the state machine out of STARTUP and returns:
        recovered          True if a crashed session is being resumed
        session            the crashed session row (dict) or None
        needs_calibration  True if the operator has no usable baseline
        unacked_alerts     fatigue events not acknowledged before the crash
        scenario           which of the 4 scenarios applied
    alert_queue is accepted for API symmetry — re-firing is done
    separately by refire_unacknowledged_alerts() once threads are up.
    """
    result = {
        "recovered":         False,
        "session":           None,
        "needs_calibration": False,
        "unacked_alerts":    [],
        "scenario":          "CLEAN_START",
    }

    # Scenario 4 — corrupted database
    if not _check_db_integrity():
        _quarantine_db()
        result["scenario"] = "DB_CORRUPTED"
        state_machine.transition(SystemState.WAITING_OPERATOR)
        return result

    # First ever boot — tables not created yet, nothing to recover
    if not _sessions_table_exists():
        print("[REC] Fresh database — nothing to recover.")
        state_machine.transition(SystemState.WAITING_OPERATOR)
        return result

    session = get_active_session()

    # Scenario 1 — clean start
    if not session:
        print("[REC] Clean start — no crashed session found.")
        state_machine.transition(SystemState.WAITING_OPERATOR)
        return result

    session_id  = session["session_id"]
    operator_id = session["operator_id"]
    crash_time  = _last_activity(session)
    gap_sec     = (datetime.now() - crash_time).total_seconds()
    operator    = get_operator(operator_id)

    print(f"[REC]  Unclean shutdown detected — session {session_id} "
          f"(operator {operator_id}, last activity {gap_sec:.0f}s ago)")

    # Scenario 3 — crash too old to resume (or operator record gone)
    if gap_sec > CRASH_RESUME_MAX_GAP_SEC or not operator:
        mark_session_crashed(session_id)
        _set_end_time(session_id, crash_time)
        print(f"[REC] Session too old to resume "
              f"(>{CRASH_RESUME_MAX_GAP_SEC}s) — marked CRASHED.")
        result["scenario"] = "STALE_SESSION_CLOSED"
        state_machine.transition(SystemState.WAITING_OPERATOR)
        return result

    # Scenario 2 — resume the crashed session
    state_machine.transition(SystemState.CRASH_RECOVERY)

    needs_calibration = _needs_recalibration(operator)
    unacked           = _find_unacked_alerts(session_id, crash_time)

    with session_lock:
        session_state.update({
            "session_id":       session_id,
            "operator_id":      operator_id,
            "operator_name":    operator.get("name"),
            "demo_mode":        bool(session.get("demo_mode")),
            "glasses_mode":     bool(operator.get("glasses_mode")),
            "perclos_current":  session.get("perclos_checkpoint") or 0.0,
            "threshold_raised": session.get("threshold_raised") or 0.0,
            "alert_level":      0,
            "alert_reason":     "",
            "fatigue_level":    0,
            "fatigue_reason":   "",
            "alert_count":      _count_fatigue_events(session_id),
            "alert_active":     False,
            "ack_times":        [],
        })

    write_audit_log("CRASH", "SYSTEM", session_id,
                    f"Unclean shutdown detected — resuming after "
                    f"{gap_sec:.0f}s gap, {len(unacked)} unacked alert(s)")

    if needs_calibration:
        # CRASH_RECOVERY cannot go straight to CALIBRATING
        state_machine.transition(SystemState.WAITING_OPERATOR)
        state_machine.transition(SystemState.CALIBRATING)
    else:
        state_machine.transition(SystemState.MONITORING)

    print(f"[REC]  Session restored — PERCLOS checkpoint "
          f"{session_state['perclos_current']:.1f}%, "
          f"calibration {'required' if needs_calibration else 'skipped'}")

    result.update({
        "recovered":         True,
        "session":           session,
        "needs_calibration": needs_calibration,
        "unacked_alerts":    unacked,
        "scenario":          "SESSION_RESUMED",
    })
    return result


def refire_unacknowledged_alerts(unacked_alerts: list,
                                 alert_queue: queue.Queue,
                                 session_state: dict,
                                 session_lock: threading.Lock) -> int:
    """
    Re-fire the alert the operator never acknowledged before the crash.
    Only the highest-level (then most recent) one is re-fired — AlertEngine
    escalates from there, so replaying the whole chain would just repeat it.
    Returns number of alerts queued (0 or 1).
    """
    if not unacked_alerts:
        return 0

    top   = max(unacked_alerts,
                key=lambda e: (e.get("alert_level") or 0, e.get("timestamp", "")))
    level = min(max(int(top.get("alert_level") or 1), 1), 3)

    with session_lock:
        session_id = session_state.get("session_id")

    event = {
        "type":          f"FATIGUE_L{level}",
        "level":         level,
        "ear":           top.get("ear_value")     or 0.0,
        "mar":           top.get("mar_value")     or 0.0,
        "pitch":         top.get("pitch_value")   or 0.0,
        "perclos":       top.get("perclos_value") or 0.0,
        "fatigue_score": top.get("fatigue_score") or 0.0,
        "session_id":    session_id,
        "timestamp":     time.time(),
        "recovered":     True,
    }
    try:
        alert_queue.put_nowait(event)
    except queue.Full:
        print("[REC] Alert queue full — could not re-fire unacked alert.")
        return 0

    write_audit_log("ALERT_REFIRED", "SYSTEM", session_id,
                    f"Level {level} alert unacknowledged at crash — re-fired")
    print(f"[REC]  Re-fired unacknowledged L{level} alert.")
    return 1


# WATCHDOG
# Restarts Thread 1 if it stops delivering frames (camera freeze,
# driver hang, or the thread dying on an exception).

class DetectionWatchdog(threading.Thread):

    def __init__(self, session_state: dict, session_lock: threading.Lock,
                 state_machine: StateMachine, alert_queue: queue.Queue,
                 shutdown_event: threading.Event, restart_callback):
        super().__init__(name="DetectionWatchdog", daemon=True)

        self.session_state    = session_state
        self.session_lock     = session_lock
        self.sm               = state_machine
        self.alert_queue      = alert_queue
        self.shutdown_event   = shutdown_event
        self.restart_callback = restart_callback
        self.restart_count    = 0

    def run(self):
        print(f"[WD] Watchdog running — timeout {WATCHDOG_TIMEOUT_SEC}s, "
              f"check every {WATCHDOG_CHECK_SEC}s")
        while not self.shutdown_event.wait(WATCHDOG_CHECK_SEC):
            try:
                self._check()
            except Exception as e:
                print(f"[WD] Check error: {e}")
        print("[WD] Watchdog exiting.")

    def _check(self):
        with self.session_lock:
            last_frame = self.session_state.get("last_frame_time")
            session_id = self.session_state.get("session_id")
            stalled    = self.session_state.get("camera_stalled", False)

        # Thread 1 not started yet (it starts on first login)
        if last_frame is None:
            return

        silent_sec = time.time() - last_frame
        if silent_sec < WATCHDOG_TIMEOUT_SEC:
            if stalled:
                with self.session_lock:
                    self.session_state["camera_stalled"] = False
                print("[WD]  Frames flowing again — detection recovered.")
                write_audit_log("DETECTION_RECOVERED", "SYSTEM", session_id,
                                f"After {self.restart_count} restart(s)")
            return

        self._handle_stall(silent_sec, session_id, stalled)

    def _handle_stall(self, silent_sec: float, session_id, already_stalled: bool):
        if self.shutdown_event.is_set():
            return

        self.restart_count += 1
        print(f"[WD]  No frames for {silent_sec:.0f}s — restarting "
              f"DetectionThread (restart #{self.restart_count})")

        with self.session_lock:
            self.session_state["camera_stalled"] = True

        write_audit_log("DETECTION_RESTART", "SYSTEM", session_id,
                        f"No frames for {silent_sec:.0f}s "
                        f"(restart #{self.restart_count})")

        # Operator is unmonitored — play camera check once per stall episode
        if session_id and not already_stalled and \
           self.sm.state not in (SystemState.WAITING_OPERATOR,
                                 SystemState.SESSION_CLOSING,
                                 SystemState.SHUTTING_DOWN):
            try:
                self.alert_queue.put_nowait({
                    "type": "CAMERA_OBSTRUCTION", "session_id": session_id
                })
            except queue.Full:
                pass

        self.restart_callback()


# HELPERS

def _check_db_integrity() -> bool:
    """True if the DB is missing (fresh install) or passes quick_check."""
    path = database.DB_PATH
    if not os.path.exists(path):
        return True
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if row and row[0] == "ok":
            return True
        print(f"[REC]  Database integrity check failed: {row[0] if row else '?'}")
        return False
    except sqlite3.DatabaseError as e:
        print(f"[REC]  Database unreadable: {e}")
        return False


def _quarantine_db():
    """Move the corrupt DB (and its WAL files) aside so a fresh one is created."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = database.DB_PATH + suffix
        if os.path.exists(src):
            dst = f"{database.DB_PATH}.corrupt-{stamp}{suffix}"
            shutil.move(src, dst)
            print(f"[REC] Quarantined {os.path.basename(src)} -> {os.path.basename(dst)}")


def _sessions_table_exists() -> bool:
    conn = database.get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone() is not None
    finally:
        conn.close()


def _last_activity(session: dict) -> datetime:
    """Best estimate of crash time: latest of start, checkpoint, last event."""
    conn = database.get_connection()
    try:
        last_event = conn.execute(
            "SELECT MAX(timestamp) FROM events WHERE session_id=?",
            (session["session_id"],)
        ).fetchone()[0]
    finally:
        conn.close()

    stamps = []
    for ts in (session.get("start_time"), session.get("last_checkpoint"), last_event):
        try:
            if ts:
                stamps.append(datetime.fromisoformat(ts))
        except ValueError:
            pass
    return max(stamps) if stamps else datetime.now()


def _find_unacked_alerts(session_id: str, crash_time: datetime) -> list:
    """
    Fatigue events raised shortly before the crash with no acknowledgement.
    Acks are recorded in audit_log (EVENT_ACKNOWLEDGED), so any fatigue event
    after the last ack in the session counts as unacknowledged.
    """
    window_start = (crash_time - timedelta(seconds=CRASH_UNACKED_WINDOW_SEC)).isoformat()

    conn = database.get_connection()
    try:
        last_ack = conn.execute("""
            SELECT MAX(timestamp) FROM audit_log
            WHERE session_id=? AND action='EVENT_ACKNOWLEDGED'
        """, (session_id,)).fetchone()[0]

        since = max(window_start, last_ack) if last_ack else window_start
        rows = conn.execute("""
            SELECT * FROM events
            WHERE session_id=? AND event_type LIKE 'FATIGUE_L%'
              AND acknowledged=0 AND timestamp > ?
            ORDER BY timestamp DESC
        """, (session_id, since)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _needs_recalibration(operator: dict) -> bool:
    """Same rule as SessionManager._needs_calibration: no baseline yet,
    or last calibration older than RECALIBRATE_AFTER_DAYS."""
    if (operator.get("baseline_ear") or 0.0) < 0.05:
        return True
    last_seen = operator.get("last_seen")
    if last_seen:
        try:
            age = datetime.now() - datetime.fromisoformat(last_seen)
            return age.days > RECALIBRATE_AFTER_DAYS
        except ValueError:
            pass
    return False


def _count_fatigue_events(session_id: str) -> int:
    conn = database.get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id=? AND event_type LIKE 'FATIGUE_L%'",
            (session_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _set_end_time(session_id: str, end_time: datetime):
    """Give a CRASHED session an end time so analytics can compute its duration."""
    conn = database.get_connection()
    try:
        conn.execute("UPDATE sessions SET end_time=? WHERE session_id=?",
                     (end_time.isoformat(), session_id))
        conn.commit()
    finally:
        conn.close()
