
import threading
import time
import os
import queue

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from config import (
    AUDIO_DIR,
    AUDIO_L1_BEEP, AUDIO_L2_HINDI, AUDIO_L2_MARATHI,
    AUDIO_L3_HINDI, AUDIO_L3_MARATHI, AUDIO_CAMERA_CHECK,
    AUDIO_CALIBRATION,
    DEFAULT_LANGUAGE,
    COOLDOWN_L1_SEC, COOLDOWN_L2_SEC, COOLDOWN_L3_SEC,
    ACK_TIMEOUT_SEC,
    FAST_ACK_TIME_SEC, FAST_ACK_COUNT,
    FAST_ACK_RAISE, FAST_ACK_MAX_RAISE,
)
from core.database import (
    insert_event, acknowledge_event, write_audit_log
)

# Try importing pygame — if not installed, fall back to print-only mode
try:
    import pygame
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
    AUDIO_AVAILABLE = True
    print("[T3] pygame.mixer initialized — audio ready.")
except Exception as e:
    AUDIO_AVAILABLE = False
    print(f"[T3]  pygame not available ({e}) — alerts will print to console only.")

SHUTDOWN_SENTINEL = "SHUTDOWN"


class AlertEngine(threading.Thread):
    

    def __init__(self, alert_queue, db_queue, ack_event,
                 shutdown_event, session_state, session_lock,
                 language: str = None):
        super().__init__(name="AlertEngine", daemon=True)

        self.alert_queue    = alert_queue
        self.db_queue       = db_queue
        self.ack_event      = ack_event
        self.shutdown_event = shutdown_event
        self.session_state  = session_state
        self.session_lock   = session_lock
        self.language       = language or DEFAULT_LANGUAGE

        # Cooldown tracking — last time each level fired
        self.last_alert_time  = {1: 0.0, 2: 0.0, 3: 0.0}
        self.cooldowns        = {
            1: COOLDOWN_L1_SEC,
            2: COOLDOWN_L2_SEC,
            3: COOLDOWN_L3_SEC,
        }

        # Current alert state
        self.current_level    = 0
        self.current_event_id = None
        self.alert_fired_time = 0.0

        # False alert learning
        self.fast_ack_counter  = 0
        self.threshold_raised  = 0.0   # total EAR raise applied this shift

        # Preloaded sounds cache
        self._sounds = {}

        print("[T3] AlertEngine initialized.")

    
    # MAIN RUN LOOP
    # Starting point for the alret engine

    def run(self):
        print("[T3] Alert engine starting...")
        self._preload_sounds()
        print("[T3] Alert engine ready. Listening on alert_queue.")

        while not self.shutdown_event.is_set():
            try:
                msg = self.alert_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg == SHUTDOWN_SENTINEL:
                print("[T3] Shutdown sentinel received. Exiting.")
                break

            if not isinstance(msg, dict):
                continue

            alert_type = msg.get("type", "")

            if alert_type == "CAMERA_OBSTRUCTION":
                self._handle_camera_obstruction(msg)
            elif alert_type.startswith("FATIGUE_L"):
                self._handle_fatigue_alert(msg)

        self._cleanup()

    
    # FATIGUE ALERT HANDLER
    # Check for the all the condition 

    def _handle_fatigue_alert(self, event: dict):
    
        level      = event.get("level", 1)
        session_id = event.get("session_id")
        now        = time.time()

        # Check cooldown — don't repeat alerts too quickly
        elapsed_since_last = now - self.last_alert_time.get(level, 0)
        cooldown           = self.cooldowns.get(level, 60)

        if elapsed_since_last < cooldown and self.current_level >= level:
            # Already at this level and in cooldown — skip
            return

        # Update state
        self.current_level    = level
        self.alert_fired_time = now
        self.last_alert_time[level] = now

        # Update dashboard
        with self.session_lock:
            self.session_state["alert_level"]        = level
            self.session_state["alert_active"]       = True
            self.session_state["alert_fired_time"]   = now

        print(f"\n[T3]  ALERT LEVEL {level} | "
              f"EAR={event.get('ear', 0):.3f} | "
              f"PERCLOS={event.get('perclos', 0):.1f}%")

        # Insert event to DB via db_queue
        event_id = None
        try:
            self.db_queue.put_nowait({
                "action":      "INSERT_EVENT",
                "data":        event,
                "callback_key": "event_id",
            })
        except queue.Full:
            pass

        # Play audio
        audio_file = self._get_audio_file(level)
        self._play_audio(audio_file, level)

        # Start ack timer — wait ACK_TIMEOUT_SEC for operator response
        acked = self._wait_for_ack(level)

        ack_time = time.time() - self.alert_fired_time

        if acked:
            print(f"[T3]  Alert L{level} acknowledged in {ack_time:.1f}s")
            self._process_ack(level, ack_time, event)

            # Reset alert state
            with self.session_lock:
                self.session_state["alert_active"] = False
                self.session_state["alert_level"]  = 0
            self.current_level = 0

        else:
            # Not acknowledged in time — escalate
            print(f"[T3]   Alert L{level} NOT acknowledged in {ACK_TIMEOUT_SEC}s — escalating")
            self._escalate(level, event)

            # Wait for the acknowledgement and oprator for L3 Until he send the message  

    def _wait_for_ack(self, level: int) -> bool:
        self.ack_event.clear()

        if level < 3:
            # L1 and L2: wait once for ACK_TIMEOUT_SEC
            acked = self.ack_event.wait(timeout=ACK_TIMEOUT_SEC)
            return acked
        else:
            # L3: repeat audio every 15 seconds until acked
            deadline = time.time() + 120   # max 2 minutes of L3
            while time.time() < deadline:
                if self.shutdown_event.is_set():
                    return False
                acked = self.ack_event.wait(timeout=15.0)
                if acked:
                    return True
                # Repeat L3 audio
                audio_file = self._get_audio_file(3)
                self._play_audio(audio_file, 3)
                print("[T3]  L3 repeating — operator not responding!")
            return False

    