

import sys, os, time, threading, queue, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.stress.scenario_generator import (
    Segment, SegmentType, NoiseProfile, build_timeline
)

from config import (
    ALERT_QUEUE_MAX, DB_QUEUE_MAX, FPS_TARGET,
    FATIGUE_L1_THRESH, FATIGUE_L2_THRESH, FATIGUE_L3_THRESH,
)
from core.state_machine import StateMachine, SystemState
from core.database import create_tables, open_session, close_session, get_connection
from core.detection import DetectionThread
from core.alert_engine import AlertEngine
from core.storage_engine import StorageEngine

passed = 0
failed = 0


def check(name, condition, expected=None, got=None):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name}")
        if expected is not None:
            print(f"     Expected: {expected}")
            print(f"     Got:      {got}")
        failed += 1


def ensure_stress_operator(baseline_ear=0.30, baseline_mar=0.10,
                            baseline_pitch=2.0, glasses_mode=False):
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT operator_id FROM operators WHERE operator_id='STRESS_OP'"
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO operators
                (operator_id, name, pin_hash, baseline_ear, baseline_mar,
                 baseline_pitch, glasses_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, ("STRESS_OP", "Stress Test Operator", "0000",
                  baseline_ear, baseline_mar, baseline_pitch, int(glasses_mode)))
            conn.commit()
    finally:
        conn.close()



# HARNESS — builds real threads, feeds synthetic frames
# Check the working

class StressHarness:
    def __init__(self, baseline_ear=0.30, baseline_mar=0.10,
                 baseline_pitch=2.0, glasses_mode=False,
                 demo_mode=False, language="marathi"):
        self.sm             = StateMachine()
        self.frame_buffer   = {"frame": None, "annotated": None, "raw": None}
        self.frame_lock     = threading.Lock()
        self.alert_queue    = queue.Queue(maxsize=ALERT_QUEUE_MAX)
        self.db_queue       = queue.Queue(maxsize=DB_QUEUE_MAX)
        self.ack_event      = threading.Event()
        self.shutdown_event = threading.Event()

        ensure_stress_operator(baseline_ear, baseline_mar, baseline_pitch, glasses_mode)
        self.session_id = open_session("STRESS_OP", demo_mode=demo_mode)

        self.session_state = {
            "session_id":       self.session_id,
            "operator_id":       "STRESS_OP",
            "operator_name":     "Stress Test Operator",
            "start_time":        time.time(),
            "demo_mode":         demo_mode,
            "baseline_ear":      baseline_ear,
            "baseline_mar":      baseline_mar,
            "baseline_pitch":    baseline_pitch,
            "glasses_mode":      glasses_mode,
            "drowsy_at_start":   False,
            "perclos_current":   0.0,
            "fatigue_score":     0.0,
            "face_detected":     True,
            "threshold_raised":  0.0,
            "brightness":        100.0,
            "current_ear":       baseline_ear,
            "current_mar":       baseline_mar,
            "current_pitch":     baseline_pitch,
            "fps":               FPS_TARGET,
            "alert_level":       0,
            "alert_active":      False,
            "ack_times":         [],
            "storage_free_mb":   9999.0,
        }
        self.session_lock = threading.Lock()

        # Real DetectionThread instance — camera never started.
        # We reuse its actual PERCLOS/alert/face-loss methods directly.
        self.det = DetectionThread(
            self.sm, self.frame_buffer, self.frame_lock,
            self.alert_queue, self.db_queue,
            self.ack_event, self.shutdown_event,
            self.session_state, self.session_lock,
        )

        # Real AlertEngine + StorageEngine — actually started as threads.
        self.storage = StorageEngine(
            self.db_queue, self.shutdown_event,
            self.session_state, self.session_lock,
        )
        self.alert = AlertEngine(
            self.alert_queue, self.db_queue,
            self.ack_event, self.shutdown_event,
            self.session_state, self.session_lock,
            language=language,
        )

        self.sm.transition(SystemState.WAITING_OPERATOR)
        self.sm.transition(SystemState.CALIBRATING)
        self.sm.transition(SystemState.MONITORING)

        # Tracking for assertions
        self.alert_events_seen = []     # number of alert_queue puts observed
        self.max_fatigue_score = 0.0
        self.exceptions        = []

    def start_threads(self):
        self.storage.start()
        self.alert.start()
        time.sleep(0.1)

    def feed_sample(self, sample):
        """Push one synthetic FrameSample through the real pipeline."""
        with self.session_lock:
            self.session_state["current_ear"]   = sample.ear
            self.session_state["current_mar"]   = sample.mar
            self.session_state["current_pitch"] = sample.pitch
            self.session_state["brightness"]    = sample.brightness
            self.session_state["face_detected"] = sample.face_detected

        try:
            if not sample.face_detected:
                self.det._handle_face_loss()
                return

            self.det._reset_face_loss()

            # Update EMA exactly as the real detection loop does
            from config import EMA_ALPHA
            self.det.smooth_ear   = EMA_ALPHA * sample.ear   + (1-EMA_ALPHA) * self.det.smooth_ear
            self.det.smooth_mar   = EMA_ALPHA * sample.mar   + (1-EMA_ALPHA) * self.det.smooth_mar
            self.det.smooth_pitch = EMA_ALPHA * sample.pitch + (1-EMA_ALPHA) * self.det.smooth_pitch

            with self.session_lock:
                glasses_mode = self.session_state.get("glasses_mode", False)

            qsize_before = self.alert_queue.qsize()

            fatigue_score = self.det._update_perclos(glasses_mode)
            self.max_fatigue_score = max(self.max_fatigue_score, fatigue_score)

            self.det._alert_decision(
                fatigue_score, sample.ear, sample.mar, sample.pitch
            )

            qsize_after = self.alert_queue.qsize()
            if qsize_after > qsize_before:
                self.alert_events_seen.append(time.time())

        except Exception as e:
            self.exceptions.append(str(e))

    def shutdown(self):
        try:
            self.db_queue.put_nowait({
                "action": "SESSION_CLOSE",
                "session_id": self.session_id,
                "operator_name": "Stress Test Operator",
                "generate_report": False,
            })
        except queue.Full:
            pass
        time.sleep(0.3)
        self.shutdown_event.set()
        self.ack_event.set()
        for q in (self.alert_queue, self.db_queue):
            try:
                q.put_nowait("SHUTDOWN")
            except queue.Full:
                pass
        self.storage.join(timeout=3)
        self.alert.join(timeout=3)


# RUN ONE PROFILE
# Profile section 


def run_profile(noise_name: str, duration_sec: int, seed: int) -> dict:
    print(f"\n{'='*60}")
    print(f"  PROFILE: normal-eyes  |  noise: {noise_name}  |  "
          f"duration: {duration_sec}s")
    print(f"{'='*60}")

    noise = NOISE_PROFILES[noise_name]
    gen = ScenarioGenerator(
        baseline_ear=0.30, baseline_mar=0.10, baseline_pitch=2.0,
        noise=noise, seed=seed,
    )

    harness = StressHarness(
        baseline_ear=0.30, baseline_mar=0.10, baseline_pitch=2.0,
        glasses_mode=False,
    )
    harness.start_threads()

    t0 = time.time()
    frame_count = 0
    for sample in gen.generate(duration_sec=duration_sec, fps=FPS_TARGET):
        harness.feed_sample(sample)
        frame_count += 1
        # Let alert/storage threads breathe periodically — keeps this
        # from being purely synchronous and closer to real timing
        if frame_count % 500 == 0:
            time.sleep(0.01)

    wall_time = time.time() - t0

    # Give AlertEngine a moment to process any queued L2/L3 ack-wait cycles
    time.sleep(1.0)

    result = {
        "noise_profile":     noise_name,
        "frames_processed":  frame_count,
        "wall_time_sec":     round(wall_time, 2),
        "max_fatigue_score": round(harness.max_fatigue_score, 3),
        "alert_events_seen": len(harness.alert_events_seen),
        "exceptions":         harness.exceptions,
        "session_id":         harness.session_id,
    }

    harness.shutdown()
    return result



# MAIN
# Entry point for the program 

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true",
                       help="Run 60s per profile instead of 30min (fast smoke test)")
    args = parser.parse_args()

    duration_sec = 60 if args.fast else 30 * 60
    mode_label   = "QUICK (60s/profile)" if args.fast else "FULL (30min/profile)"

    print("=" * 60)
    print(f"  STRESS TEST -- Normal Eyes Operator Profile")
    print(f"  Mode: {mode_label}")
    print(f"  Profiles: {list(NOISE_PROFILES.keys())}")
    print("=" * 60)

    create_tables()

    all_results = []
    for i, noise_name in enumerate(NOISE_PROFILES.keys()):
        result = run_profile(noise_name, duration_sec, seed=100 + i)
        all_results.append(result)

        print(f"\n  Results for {noise_name}:")
        print(f"    Frames processed:   {result['frames_processed']}")
        print(f"    Wall time:          {result['wall_time_sec']}s")
        print(f"    Max fatigue score:  {result['max_fatigue_score']}")
        print(f"    Alert events seen:  {result['alert_events_seen']}")
        print(f"    Exceptions:         {len(result['exceptions'])}")

        # Assertions per profile
        check(f"[{noise_name}] No exceptions during processing",
              len(result["exceptions"]) == 0,
              "0 exceptions", f"{len(result['exceptions'])} exceptions: "
              f"{result['exceptions'][:3]}")

        check(f"[{noise_name}] Correct frame count processed",
              result["frames_processed"] == duration_sec * FPS_TARGET,
              duration_sec * FPS_TARGET, result["frames_processed"])

        check(f"[{noise_name}] Fatigue score reached L1 threshold "
              f"({FATIGUE_L1_THRESH}) at least once",
              result["max_fatigue_score"] >= FATIGUE_L1_THRESH,
              f">= {FATIGUE_L1_THRESH}", result["max_fatigue_score"])

        check(f"[{noise_name}] At least one alert event was queued",
              result["alert_events_seen"] >= 1,
              ">= 1", result["alert_events_seen"])

    # Cross-profile comparison.
    # Multiprofile comparsion
    print(f"\n{'='*60}")
    print("  CROSS-PROFILE SUMMARY")
    print(f"{'='*60}")
    for r in all_results:
        print(f"  {r['noise_profile']:18s} | "
              f"max_score={r['max_fatigue_score']:.3f} | "
              f"alerts={r['alert_events_seen']:3d} | "
              f"exceptions={len(r['exceptions'])}")

    # Sanity: darker/noisier profiles should not silently produce zero signal
    dark_result = next(r for r in all_results if r["noise_profile"] == "very_dark")
    check("very_dark profile still reaches meaningful fatigue scores "
          "(noise doesn't mask real signal)",
          dark_result["max_fatigue_score"] >= FATIGUE_L1_THRESH,
          f">= {FATIGUE_L1_THRESH}", dark_result["max_fatigue_score"])

    
    # SUMMARY
    # over all summary of the test cases 
   
    total = passed + failed
    print(f"  RESULTS: {passed}/{total} checks passed")
    if failed == 0:
        print("  ALL STRESS CHECKS PASSED")
        print("  Normal-eyes profile survives all lighting/noise conditions.")
    else:
        print(f"  {failed} CHECKS FAILED -- investigate before Week 2")
  

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()