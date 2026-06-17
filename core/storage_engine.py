import threading
import time
import os
import queue
import uuid
import shutil

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import (
    CLIPS_DIR, REPORTS_DIR,
    CLIP_WIDTH, CLIP_HEIGHT, CLIP_FPS,
    CLIP_DURATION_PRE_SEC, CLIP_DURATION_POST_SEC,
    STORAGE_MIN_MB, STORAGE_WARN_MB,
    CLIP_RETENTION_DAYS, CHECKPOINT_INTERVAL_SEC,
)
from core.database import (
    insert_event, acknowledge_event, update_checkpoint,
    write_audit_log, auto_delete_old_clips, close_session,
)

# Try importing cv2 for clip saving
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    print("[T4]   OpenCV not available — clip saving disabled.")

SHUTDOWN_SENTINEL = "SHUTDOWN"


class StorageEngine(threading.Thread):
    def __init__(self, db_queue, shutdown_event, session_state, session_lock):
        super().__init__(name="StorageEngine", daemon=True)

        self.db_queue       = db_queue
        self.shutdown_event = shutdown_event
        self.session_state  = session_state
        self.session_lock   = session_lock

        # Ensure output directories exist
        os.makedirs(CLIPS_DIR,   exist_ok=True)
        os.makedirs(REPORTS_DIR, exist_ok=True)

        # Stats for dashboard
        self.events_written  = 0
        self.clips_saved     = 0
        self.clips_skipped   = 0
        self.bytes_written   = 0

        # Pending clip frames (post-event capture)
        self._pending_clip   = None
        self._post_frames    = []
        self._post_target    = int(CLIP_DURATION_POST_SEC * 30)

        print("[T4] StorageEngine initialized.")

    
    # MAIN RUN LOOP
    # Entry point for the thread — listens on db_queue and dispatches messages.  

    def run(self):
        print("[T4] Storage engine starting...")
        print("[T4] Listening on db_queue.")

        while not self.shutdown_event.is_set():
            try:
                msg = self.db_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg == SHUTDOWN_SENTINEL:
                print("[T4] Shutdown sentinel received. Exiting.")
                break

            if not isinstance(msg, dict):
                continue

            action = msg.get("action", "")
            try:
                self._dispatch(action, msg)
            except Exception as e:
                print(f"[T4]  Error processing '{action}': {e}")
                import traceback
                traceback.print_exc()

        self._cleanup()

    
    # MESSAGE DISPATCHER
    # 

    def _dispatch(self, action: str, msg: dict):
        if action == "INSERT_EVENT":
            self._handle_insert_event(msg)
        elif action == "ACKNOWLEDGE_EVENT":
            self._handle_acknowledge(msg)
        elif action == "CHECKPOINT":
            self._handle_checkpoint(msg)
        elif action == "SAVE_CLIP":
            self._handle_save_clip(msg)
        elif action == "SESSION_CLOSE":
            self._handle_session_close(msg)
        elif action == "FLAG_THRESHOLD_CHANGE":
            self._handle_threshold_change(msg)
        elif action == "GENERATE_REPORT":
            self._handle_generate_report(msg)
        else:
            pass  # Unknown action — ignore silently

    
    # EVENT INSERTION
    # Insert fatigue/face-loss/tamper event into SQLite database and optionally save clip.

    def _handle_insert_event(self, msg: dict):
        """Write fatigue/face-loss/tamper event to SQLite."""
        data = msg.get("data", {})
        event_type = data.get("type", "UNKNOWN")
        session_id = data.get("session_id")

        if not session_id:
            return

        event_id = insert_event(
            session_id    = session_id,
            event_type    = event_type,
            ear_value     = data.get("ear"),
            mar_value     = data.get("mar"),
            pitch_value   = data.get("pitch"),
            perclos_value = data.get("perclos"),
            fatigue_score = data.get("fatigue_score"),
            alert_level   = data.get("level", 0),
        )

        self.events_written += 1

        # For Level 3 fatigue events — trigger clip save
        if data.get("level", 0) >= 3 and "clip_frames" in data:
            self._save_clip(
                pre_frames = data["clip_frames"],
                event_id   = event_id,
                session_id = session_id,
            )

        if self.events_written % 10 == 0:
            print(f"[T4] Events written: {self.events_written}")

   
   