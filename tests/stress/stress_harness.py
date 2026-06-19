import sys, os, time, threading, queue, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from config import FPS_TARGET, ALERT_QUEUE_MAX, DB_QUEUE_MAX
from core.state_machine import StateMachine, SystemState
from core.database import (
    create_tables, verify_database, open_session, close_session,
    get_operator, get_connection
)
from core.detection import DetectionThread
from core.alert_engine import AlertEngine
from core.storage_engine import StorageEngine
from tests.stress.scenario_generator import ScenarioResult, FrameSample

# Check if the database is accessible before starting threads, to avoid silent failures in the middle of the test run. If the database is not accessible, raise an exception immediately so the test fails fast with a clear error message, rather than hanging or losing data later on when the threads try to write to the database.
def _ensure_operator(operator_id: str, name: str = None):
    if get_operator(operator_id):
        return
    from datetime import datetime
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO operators
            (operator_id, name, pin_hash, baseline_ear, baseline_mar,
             baseline_pitch, glasses_mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (operator_id, name or operator_id, "0000",
              0.30, 0.10, 2.0, 0, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()

# SHARED OBJECTS help threads communicate and maintain state. In the real app these are global singletons, but here we instantiate them inside the StressHarness class to allow multiple independent instances if needed (e.g. for parallel test runs). The DetectionThread, AlertEngine, and StorageEngine will all reference these shared objects to coordinate their work, just like in the real app.
class StressHarness:
    def __init__(self, operator_id: str = "OP001",
                baseline_ear: float = 0.30,
                baseline_mar: float = 0.10,
                baseline_pitch: float = 2.0,
                glasses_mode: bool = False,
                language: str = "marathi",
                verbose: bool = True):

        self.verbose = verbose
        self.operator_id = operator_id

        create_tables()
        verify_database()
        _ensure_operator(operator_id, name=f"StressTest-{operator_id}")

        self.sm             = StateMachine()
        self.alert_queue    = queue.Queue(maxsize=ALERT_QUEUE_MAX)
        self.db_queue       = queue.Queue(maxsize=DB_QUEUE_MAX)
        self.frame_buffer   = {"frame": None, "annotated": None, "raw": None}
        self.frame_lock     = threading.Lock()
        self.ack_event      = threading.Event()
        self.shutdown_event = threading.Event()
        self.session_lock   = threading.Lock()

        self.session_id = open_session(operator_id, demo_mode=False)

        self.session_state = {
            "session_id":       self.session_id,
            "operator_id":      operator_id,
            "operator_name":    f"StressTest-{operator_id}",
            "start_time":       time.time(),
            "demo_mode":        False,
            "baseline_ear":     baseline_ear,
            "baseline_mar":     baseline_mar,
            "baseline_pitch":   baseline_pitch,
            "glasses_mode":     glasses_mode,
            "drowsy_at_start":  False,
            "perclos_current":  0.0,
            "fatigue_score":    0.0,
            "face_detected":    True,
            "threshold_raised": 0.0,
            "brightness":       100.0,
            "current_ear":      baseline_ear,
            "current_mar":      baseline_mar,
            "current_pitch":    baseline_pitch,
            "fps":              FPS_TARGET,
            "alert_level":      0,
            "alert_active":     False,
            "alert_fired_time": 0.0,
            "ack_times":        [],
            "storage_free_mb":  9999.0,
            "storage_warning":  False,
            "storage_critical": False,
            "last_report_path": None,
        }

        self.sm.transition(SystemState.WAITING_OPERATOR)
        self.sm.transition(SystemState.CALIBRATING)
        self.sm.transition(SystemState.MONITORING)

        # Real DetectionThread instance — camera loop never started.
        # We call its internal methods directly on injected frames.
        # Help to understand the exact math and timing of PERCLOS updates and alert decisions as EAR/Mar/Pitch change over time in the scenarios.
        self.detector = DetectionThread(
            self.sm, self.frame_buffer, self.frame_lock,
            self.alert_queue, self.db_queue, self.ack_event,
            self.shutdown_event, self.session_state, self.session_lock
        )
        from config import PERCLOS_WINDOW_FRAMES
        for _ in range(PERCLOS_WINDOW_FRAMES):
            self.detector.ear_deque.append(baseline_ear)
        self.detector.smooth_ear   = baseline_ear
        self.detector.smooth_mar   = baseline_mar
        self.detector.smooth_pitch = baseline_pitch

        # Real Thread 3 + Thread 4 — actually started as threads
        self.alert_engine = AlertEngine(
            self.alert_queue, self.db_queue, self.ack_event,
            self.shutdown_event, self.session_state, self.session_lock,
            language=language
        )
        self.storage_engine = StorageEngine(
            self.db_queue, self.shutdown_event,
            self.session_state, self.session_lock
        )

        # Event log for assertions afterwards
        self.event_log = []
        self._patch_alert_queue_logging()

        self.frames_processed = 0
        self.auto_acks = []   # (delay_sec) — simulated operator response times

    def _patch_alert_queue_logging(self):
        """Wrap alert_queue.put_nowait so we can log every fired alert."""
        original_put = self.alert_queue.put_nowait

        def logging_put(item):
            if isinstance(item, dict):
                self.event_log.append({
                    "t":     time.time(),
                    "frame": self.frames_processed,
                    "type":  item.get("type"),
                    "level": item.get("level"),
                    "ear":   item.get("ear"),
                    "perclos": item.get("perclos"),
                    "score": item.get("fatigue_score"),
                })
                if self.verbose:
                    print(f"  [frame {self.frames_processed:5d}] "
                          f" {item.get('type')} "
                          f"EAR={item.get('ear', 0):.3f} "
                          f"PERCLOS={item.get('perclos', 0):.1f}%")
            return original_put(item)

        self.alert_queue.put_nowait = logging_put

    def start_threads(self):
        self.alert_engine.start()
        self.storage_engine.start()
        time.sleep(0.2)

    def stop_threads(self, generate_report: bool = False):
        try:
            self.db_queue.put_nowait({
                "action":          "SESSION_CLOSE",
                "session_id":      self.session_id,
                "operator_name":   self.session_state["operator_name"],
                "generate_report": generate_report,
            })
        except queue.Full:
            pass
        time.sleep(0.8)

        self.shutdown_event.set()
        self.ack_event.set()
        for q in (self.alert_queue, self.db_queue):
            try:
                q.put_nowait("SHUTDOWN")
            except queue.Full:
                pass
        self.alert_engine.join(timeout=3)
        self.storage_engine.join(timeout=3)

  
    # FRAME INJECTION — the core of the harness
    # Do help test various scenarios (e.g. long runs with no alert, or escalating alerts with no ack) by feeding in frames from ScenarioResult objects, which are generated by scenario_generator.py based on real recorded sessions. We call the DetectionThread's internal methods directly to process each frame, which exercises the exact production code path for PERCLOS and alert decisions.
    # Help to understand how different patterns of EAR/Mar/Pitch changes produce different alert levels, and how the system responds to them over time (e.g. does PERCLOS rise as expected during long eye closures? Do alerts escalate correctly if no ack is sent? Does the system recover correctly when EAR rises again?). By adjusting the auto_ack_delay parameter, we can simulate different operator response times and test the ack timing logic and alert escalation behavior under various conditions.


    def run_scenario(self, scenario: ScenarioResult,
                     auto_ack_delay: float = None,
                     speed_multiplier: float = 1.0):
        """
        Feeds every frame in the scenario through the REAL detection
        pipeline (EMA smoothing, PERCLOS update, alert decision —
        exactly the methods detection.py uses on real camera frames).
        auto_ack_delay: if set, automatically fires ack_event this many
                        seconds after any alert (simulates operator
                        response). None = never auto-ack (tests escalation).
        speed_multiplier: >1.0 skips the real-time sleep for fast test runs.
                          1.0 = real wall-clock pacing (use for ack-timer tests).
        """
        last_ack_check = 0
        for sample in scenario.frames:
            with self.session_lock:
                self.session_state["current_ear"]   = sample.ear
                self.session_state["current_mar"]   = sample.mar
                self.session_state["current_pitch"] = sample.pitch
                self.session_state["face_detected"] = sample.face_detected

            if sample.face_detected:
                # EMA smoothing — reuse the real detector's state
                self.detector.smooth_ear = (
                    0.15 * sample.ear + 0.85 * self.detector.smooth_ear
                )
                self.detector.smooth_mar = (
                    0.15 * sample.mar + 0.85 * self.detector.smooth_mar
                )
                self.detector.smooth_pitch = (
                    0.15 * sample.pitch + 0.85 * self.detector.smooth_pitch
                )

                glasses = self.session_state.get("glasses_mode", False)
                fatigue_score = self.detector._update_perclos(glasses)
                self.detector._alert_decision(
                    fatigue_score, sample.ear, sample.mar, sample.pitch
                )
                self.detector._reset_face_loss()
            else:
                self.detector._handle_face_loss()

            self.frames_processed += 1

            # Auto-ack simulation
            if auto_ack_delay is not None:
                with self.session_lock:
                    active = self.session_state.get("alert_active", False)
                if active and (time.time() - last_ack_check) > auto_ack_delay:
                    self.ack_event.set()
                    time.sleep(0.05)
                    self.ack_event.clear()
                    last_ack_check = time.time()

            if speed_multiplier < 999:
                time.sleep((1.0 / FPS_TARGET) / speed_multiplier)

    
    # ASSERTIONS / REPORTING
    # Report generation is tested end-to-end in test_report_generation.py, so here we just return a summary of what happened for test assertions. We do NOT
    # assert on exact alert timings or fatigue scores, since those are already tested in isolation in test_ear.py and test_alert_engine.py. Instead we assert on the overall pattern of alert levels, number of alerts, and final PERCLOS/score at the end of the scenario.

    def get_summary(self) -> dict:
        levels_fired = [e["level"] for e in self.event_log if e.get("level")]
        return {
            "frames_processed": self.frames_processed,
            "alerts_fired":      len(levels_fired),
            "max_level_fired":   max(levels_fired) if levels_fired else 0,
            "event_log":         self.event_log,
            "final_perclos":     self.session_state.get("perclos_current", 0),
            "final_score":       self.session_state.get("fatigue_score", 0),
            "threshold_raised":  self.session_state.get("threshold_raised", 0),
        }