import sqlite3
import uuid
import os
from datetime import datetime, timedelta
from config import (
    DB_PATH, DATA_DIR, CLIP_RETENTION_DAYS,
    EVENT_RETENTION_DAYS, AUDIT_RETENTION_DAYS,
    MANAGER_PIN_DEFAULT, MIN_EVENT_THRESHOLD
)

def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL") # CRITICAL — do not remove
    conn.execute("PRAGMA foreign_keys=ON")  # enforce FK constraints
    return conn

#Database schema design:
# 1. operators: operator_id (PK), name, pin_hash, baseline_ear
#2. sessions: session_id (PK), operator_id (FK), machine_id, start_time, end_time, status, last_checkpoint, perclos_checkpoint
#3. events: event_id (PK), session_id (FK), timestamp, event_type, ear_value, mar_value, pitch_value, perclos_value, fatigue_score, alert_level, acknowledged, ack_time_sec, clip_path
#4. audit_log: log_id (PK), timestamp, action, actor, session_id (nullable), detail
#5. system_config: key (PK), value, min_value, updated_at
#we can add more according to the requirement but these are the basic tables we need to start with and we can add more tables if required in future

def create_tables():
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                operator_id     TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                pin_hash        TEXT NOT NULL,
                baseline_ear    REAL DEFAULT 0.30,
                baseline_mar    REAL DEFAULT 0.10,
                baseline_pitch  REAL DEFAULT 2.0,
                glasses_mode    INTEGER DEFAULT 0,
                created_at      TEXT NOT NULL,
                last_seen       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id          TEXT PRIMARY KEY,
                operator_id         TEXT NOT NULL,
                machine_id          TEXT DEFAULT 'LAPTOP-DEMO',
                start_time          TEXT NOT NULL,
                end_time            TEXT,
                status              TEXT DEFAULT 'ACTIVE',
                last_checkpoint     TEXT,
                perclos_checkpoint  REAL DEFAULT 0.0,
                threshold_raised    REAL DEFAULT 0.0,
                glasses_mode        INTEGER DEFAULT 0,
                demo_mode           INTEGER DEFAULT 0,
                FOREIGN KEY (operator_id) REFERENCES operators(operator_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id        TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                ear_value       REAL,
                mar_value       REAL,
                pitch_value     REAL,
                perclos_value   REAL,
                fatigue_score   REAL,
                alert_level     INTEGER DEFAULT 0,
                acknowledged    INTEGER DEFAULT 0,
                ack_time_sec    REAL,
                clip_path       TEXT,
                deleted_at      TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                log_id      TEXT PRIMARY KEY,
                timestamp   TEXT NOT NULL,
                action      TEXT NOT NULL,
                actor       TEXT NOT NULL,
                session_id  TEXT,
                detail      TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                min_value   TEXT,
                updated_at  TEXT NOT NULL
            )
        """)

        conn.commit()
        print("[DB] All 5 tables created successfully.")
        _seed_default_config(conn)
        _seed_demo_operator(conn)

    except Exception as e:
        print(f"[DB ERROR] create_tables failed: {e}")
        raise
    finally:
        conn.close()


def _seed_default_config(conn):
    """Insert default system_config values if they don't exist."""
    defaults = [
        ("manager_pin",     MANAGER_PIN_DEFAULT, None),
        ("l1_threshold",    "0.15", str(MIN_EVENT_THRESHOLD)),
        ("l2_threshold",    "0.25", str(MIN_EVENT_THRESHOLD)),
        ("l3_threshold",    "0.40", str(MIN_EVENT_THRESHOLD)),
        ("default_language","marathi", None),
        ("machine_id",      "LAPTOP-DEMO", None),
        ("site_name",       "Demo Site", None),
    ]
    now = _now()
    for key, value, min_val in defaults:
        conn.execute("""
            INSERT OR IGNORE INTO system_config (key, value, min_value, updated_at)
            VALUES (?, ?, ?, ?)
        """, (key, value, min_val, now))
    conn.commit()
    print("[DB] Default config seeded.")


def _seed_demo_operator(conn):
    """Create a demo operator so you can test immediately."""
    existing = conn.execute(
        "SELECT operator_id FROM operators WHERE operator_id='OP001'"
    ).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO operators
            (operator_id, name, pin_hash, baseline_ear, baseline_mar,
             baseline_pitch, glasses_mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("OP001", "Demo Operator", "1234",
              0.30, 0.10, 2.0, 0, _now()))
        conn.commit()
        print("[DB] Demo operator OP001 created.")

def get_operator(operator_id: str):
    """Fetch operator profile. Returns dict or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM operators WHERE operator_id=?", (operator_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_operator_baseline(operator_id: str, baseline_ear: float,
                              baseline_mar: float, baseline_pitch: float,
                              glasses_mode: bool):
# Update operator's baseline values after calibration.
#glasses_mode is stored as INTEGER 0/1 in DB for simplicity.(SQLite does not have a native BOOLEAN type)
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE operators
            SET baseline_ear=?, baseline_mar=?, baseline_pitch=?,
                glasses_mode=?, last_seen=?
            WHERE operator_id=?
        """, (baseline_ear, baseline_mar, baseline_pitch,
              int(glasses_mode), _now(), operator_id))
        conn.commit()
        print(f"[DB] Baseline updated for {operator_id}: EAR={baseline_ear:.3f}")
    finally:
        conn.close()


def get_all_operators():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT operator_id, name FROM operators ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def open_session(operator_id: str, demo_mode: bool = False) -> str:
   #Every time new session Id get generated
    session_id = str(uuid.uuid4())
    machine_id = get_config("machine_id") or "LAPTOP-DEMO"
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO sessions
            (session_id, operator_id, machine_id, start_time, status, demo_mode)
            VALUES (?, ?, ?, ?, 'ACTIVE', ?)
        """, (session_id, operator_id, machine_id, _now(), int(demo_mode)))
        conn.commit()
        write_audit_log("SESSION_START", "SYSTEM", session_id,
                        f"Operator {operator_id} shift started")
        print(f"[DB] Session opened: {session_id}")
        return session_id
    finally:
        conn.close()


def close_session(session_id: str):
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE sessions
            SET status='CLOSED', end_time=?
            WHERE session_id=?
        """, (_now(), session_id))
        conn.commit()
        write_audit_log("SESSION_END", "SYSTEM", session_id, "Session closed cleanly")
        print(f"[DB] Session closed: {session_id}")
    finally:
        conn.close()


def get_active_session():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE status='ACTIVE' ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_session_crashed(session_id: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sessions SET status='CRASHED' WHERE session_id=?",
            (session_id,)
        )
        conn.commit()
        write_audit_log("CRASH", "SYSTEM", session_id, "Unclean shutdown detected")
    finally:
        conn.close()


def update_checkpoint(session_id: str, perclos_value: float,
                      threshold_raised: float = 0.0):
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE sessions
            SET perclos_checkpoint=?, last_checkpoint=?, threshold_raised=?
            WHERE session_id=?
        """, (perclos_value, _now(), threshold_raised, session_id))
        conn.commit()
    finally:
        conn.close()


def insert_event(session_id: str, event_type: str,
                 ear_value: float = None, mar_value: float = None,
                 pitch_value: float = None, perclos_value: float = None,
                 fatigue_score: float = None, alert_level: int = 0,
                 clip_path: str = None) -> str:
   
    event_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO events
            (event_id, session_id, timestamp, event_type, ear_value,
             mar_value, pitch_value, perclos_value, fatigue_score,
             alert_level, clip_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (event_id, session_id, _now(), event_type,
              ear_value, mar_value, pitch_value, perclos_value,
              fatigue_score, alert_level, clip_path))
        conn.commit()
        return event_id
    finally:
        conn.close()


def acknowledge_event(event_id: str, ack_time_sec: float):
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE events
            SET acknowledged=1, ack_time_sec=?
            WHERE event_id=?
        """, (ack_time_sec, event_id))
        conn.commit()
    finally:
        conn.close()


def get_session_events(session_id: str):
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE session_id=?
            ORDER BY timestamp ASC
        """, (session_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_session_summary(session_id: str) -> dict:
    """
    Returns count of each event type for the session.
    Used in PDF report header summary.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT event_type, COUNT(*) as count,
                   AVG(ack_time_sec) as avg_ack_time
            FROM events
            WHERE session_id=? AND event_type LIKE 'FATIGUE%'
            GROUP BY event_type
        """, (session_id,)).fetchall()
        summary = {r["event_type"]: {
            "count": r["count"],
            "avg_ack_time": r["avg_ack_time"]
        } for r in rows}
        return summary
    finally:
        conn.close()


def write_audit_log(action: str, actor: str,
                    session_id: str = None, detail: str = None):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO audit_log (log_id, timestamp, action, actor, session_id, detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), _now(), action, actor, session_id, detail))
        conn.commit()
    finally:
        conn.close()




def get_config(key: str) -> str:
    """Get a config value by key. Returns string or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_config(key: str, value: str, actor: str = "MANAGER"):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT min_value FROM system_config WHERE key=?", (key,)
        ).fetchone()
        if row and row["min_value"] is not None:
            if float(value) < float(row["min_value"]):
                raise ValueError(
                    f"Cannot set {key} below minimum {row['min_value']} (privacy lock)"
                )
        conn.execute("""
            UPDATE system_config SET value=?, updated_at=? WHERE key=?
        """, (value, _now(), key))
        conn.commit()
        write_audit_log("THRESHOLD_CHANGE", actor,
                        detail=f"{key} changed to {value}")
    finally:
        conn.close()




def auto_delete_old_clips():
    cutoff = (datetime.now() - timedelta(days=CLIP_RETENTION_DAYS)).isoformat()
    conn = get_connection()
    try:
        # Get clip paths before deleting records
        old_clips = conn.execute("""
            SELECT clip_path FROM events
            WHERE clip_path IS NOT NULL AND timestamp < ?
        """, (cutoff,)).fetchall()

        # Delete the actual files
        deleted = 0
        for row in old_clips:
            if row["clip_path"] and os.path.exists(row["clip_path"]):
                try:
                    os.remove(row["clip_path"])
                    deleted += 1
                except Exception as e:
                    print(f"[DB] Could not delete clip: {e}")

        # Null out clip_path in DB (keep event record, just remove clip)
        conn.execute("""
            UPDATE events SET clip_path=NULL, deleted_at=?
            WHERE clip_path IS NOT NULL AND timestamp < ?
        """, (_now(), cutoff))
        conn.commit()
        if deleted > 0:
            print(f"[DB] Auto-deleted {deleted} old clips.")
    finally:
        conn.close()


#Utiliy are basic functions which are used in different part of project and these are not related to database but these are used in database for example _now() function is used to get current time in ISO format which is used in database for timestamp and other fields and these utility functions can be used in other part of project as well if required.

def _now() -> str:
    """Returns current datetime as ISO format string."""
    return datetime.now().isoformat()


def verify_database():
    conn = get_connection()
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        required = ["operators", "sessions", "events", "audit_log", "system_config"]
        for t in required:
            if t not in table_names:
                raise RuntimeError(f"[DB] Missing table: {t}")
        # Verify WAL mode
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "wal":
            raise RuntimeError(f"[DB] WAL mode not active! Current mode: {mode}")
        print(f"[DB] Verification passed. Tables: {table_names}. Mode: {mode}")
        return True
    finally:
        conn.close()