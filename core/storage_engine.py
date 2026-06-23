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
    print("[T4]  OpenCV not available — clip saving disabled.")

SHUTDOWN_SENTINEL = "SHUTDOWN"


class StorageEngine(threading.Thread):
    """
    Thread 4 — All disk I/O isolated here.

    Handles these message types from db_queue:
        INSERT_EVENT        → insert_event() in SQLite
        ACKNOWLEDGE_EVENT   → acknowledge_event() in SQLite
        CHECKPOINT          → update_checkpoint() in SQLite
        SAVE_CLIP           → save 10s MP4 clip to disk
        GENERATE_REPORT     → trigger PDF generation
        SESSION_CLOSE       → close session + auto-delete old clips
        FLAG_THRESHOLD_CHANGE → log threshold change

    Constructor args:
        db_queue        : queue.Queue from Thread 1 + Thread 3
        shutdown_event  : threading.Event
        session_state   : dict (shared)
        session_lock    : threading.Lock
    """

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
    # Entry point of the storage engine 

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
                print(f"[T4] Error processing '{action}': {e}")
                import traceback
                traceback.print_exc()

        self._cleanup()

    
    # MESSAGE DISPATCHER
    

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
    # Any event happened it get stored 

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

    
    # ACKNOWLEDGEMENT
   

    def _handle_acknowledge(self, msg: dict):
        """Update event with ack time."""
        ack_time   = msg.get("ack_time", 0.0)
        event_data = msg.get("event", {})

        # We don't have event_id here directly —
        # find the most recent unacknowledged event for this session
        with self.session_lock:
            session_id = self.session_state.get("session_id")

        if not session_id:
            return

        # Log ack in audit log
        write_audit_log(
            "EVENT_ACKNOWLEDGED", "OPERATOR",
            session_id,
            f"Level {event_data.get('level',0)} ack in {ack_time:.1f}s"
        )

        # Update session state ack ratio tracking
        with self.session_lock:
            acks = self.session_state.get("ack_times", [])
            acks.append(ack_time)
            self.session_state["ack_times"] = acks

    # CHECKPOINT
    # help at the time of crash recovery 

    def _handle_checkpoint(self, msg: dict):
        """Write PERCLOS state checkpoint for crash recovery."""
        update_checkpoint(
            session_id      = msg["session_id"],
            perclos_value   = msg.get("perclos", 0.0),
            threshold_raised= msg.get("threshold", 0.0),
        )

    
    # CLIP SAVING
    # As the proof 

    def _save_clip(self, pre_frames: list, event_id: str, session_id: str):
        """
        Save 10-second video clip (5s pre + 5s post event) to disk.
        Only for Level 3 fatigue events.
        """
        if not CV2_AVAILABLE:
            print("[T4]  OpenCV not available — clip not saved.")
            return

        # Check available storage
        free_mb = self._get_free_storage_mb()
        if free_mb < STORAGE_MIN_MB:
            print(f"[T4] Storage low ({free_mb:.0f}MB) — clip skipped.")
            self.clips_skipped += 1
            with self.session_lock:
                self.session_state["storage_warning"] = True
            return

        if free_mb < STORAGE_WARN_MB:
            print(f"[T4]  Storage critically low ({free_mb:.0f}MB)!")
            with self.session_lock:
                self.session_state["storage_critical"] = True
            return

        # Generate clip filename
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        filename      = f"clip_{session_id[:8]}_{timestamp_str}.mp4"
        filepath      = os.path.join(CLIPS_DIR, filename)

        try:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(
                filepath, fourcc, CLIP_FPS,
                (CLIP_WIDTH, CLIP_HEIGHT)
            )

            if not writer.isOpened():
                print(f"[T4]  Could not open VideoWriter for {filepath}")
                return

            frames_written = 0
            for frame in pre_frames:
                if frame is not None:
                    resized = cv2.resize(frame, (CLIP_WIDTH, CLIP_HEIGHT))
                    writer.write(resized)
                    frames_written += 1

            writer.release()

            if frames_written > 0:
                clip_size = os.path.getsize(filepath)
                self.clips_saved  += 1
                self.bytes_written += clip_size
                print(f"[T4] 📹 Clip saved: {filename} "
                      f"({frames_written} frames, {clip_size//1024}KB)")

                # Store clip path in DB
                try:
                    from core.database import get_connection
                    conn = get_connection()
                    try:
                        conn.execute("""
                            UPDATE events SET clip_path=?
                            WHERE session_id=? AND clip_path IS NULL
                            ORDER BY timestamp DESC LIMIT 1
                        """, (filepath, session_id))
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as e:
                    print(f"[T4] Could not update clip_path: {e}")
            else:
                os.remove(filepath)
                print("[T4]Clip had no frames — deleted.")

        except Exception as e:
            print(f"[T4] Clip save error: {e}")
            if os.path.exists(filepath):
                os.remove(filepath)

    def _handle_save_clip(self, msg: dict):
        """Handle explicit SAVE_CLIP message."""
        self._save_clip(
            pre_frames = msg.get("frames", []),
            event_id   = msg.get("event_id", str(uuid.uuid4())),
            session_id = msg.get("session_id", ""),
        )

  
    # SESSION CLOSE
    # End of the session 
    def _handle_session_close(self, msg: dict):
        """
        Close session in DB, run auto-delete, trigger PDF generation.
        Called when operator ends shift or new operator swipes in.
        """
        session_id = msg.get("session_id")
        if not session_id:
            return

        print(f"[T4] Closing session {session_id}...")

        # Close session in DB
        close_session(session_id)

        # Run auto-delete of old clips (7-day retention)
        print("[T4] Running auto-delete of old clips...")
        auto_delete_old_clips()

        # Update storage info in session_state
        with self.session_lock:
            self.session_state["storage_free_mb"] = self._get_free_storage_mb()

        # Trigger PDF generation if requested
        if msg.get("generate_report", True):
            self._handle_generate_report({
                "session_id": session_id,
                "operator_name": msg.get("operator_name", "Unknown"),
            })

        print(f"[T4] Session {session_id} closed cleanly.")

    
    # REPORT GENERATION
    # Check all the edge cases here 

    def _handle_generate_report(self, msg: dict):
        """
        Trigger PDF report generation.
        Imports report_generator lazily to avoid circular imports.
        Wraps in try-except — PDF failure must never block session close.
        """
        session_id    = msg.get("session_id")
        operator_name = msg.get("operator_name", "Unknown")

        if not session_id:
            return

        print(f"[T4] Generating PDF report for session {session_id}...")
        try:
            from core.report_generator import generate_report
            pdf_path = generate_report(session_id, operator_name)
            if pdf_path:
                print(f"[T4] PDF report saved: {pdf_path}")
                with self.session_lock:
                    self.session_state["last_report_path"] = pdf_path
            else:
                print("[T4]PDF generation returned no path.")
        except ImportError:
            print("[T4] report_generator not yet implemented (Day 5).")
        except Exception as e:
            print(f"[T4]  PDF generation failed: {e}")
            write_audit_log(
                "PDF_FAILED", "SYSTEM", session_id,
                f"Error: {str(e)[:100]}"
            )

    
    # THRESHOLD CHANGE FLAG
   

    def _handle_threshold_change(self, msg: dict):
        """Log threshold change to session state for PDF report."""
        with self.session_lock:
            self.session_state["threshold_raised"] = msg.get("amount", 0)

  
    # STORAGE UTILITIES
  

    def _get_free_storage_mb(self) -> float:
        """Returns free disk space in MB for the clips directory."""
        try:
            usage = shutil.disk_usage(CLIPS_DIR)
            return usage.free / (1024 * 1024)
        except Exception:
            return 9999.0   # assume plenty if check fails

    def get_stats(self) -> dict:
        """Return storage stats for dashboard display."""
        return {
            "events_written": self.events_written,
            "clips_saved":    self.clips_saved,
            "clips_skipped":  self.clips_skipped,
            "bytes_written":  self.bytes_written,
            "free_mb":        self._get_free_storage_mb(),
        }

    
    # CLEANUP
    def _cleanup(self):
        print(f"[T4] Final stats: events={self.events_written} "
              f"clips={self.clips_saved} skipped={self.clips_skipped}")
        print("[T4] StorageEngine cleaned up.")