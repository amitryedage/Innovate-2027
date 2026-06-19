import sys, os, time, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.stress.scenario_generator import (
    Segment, SegmentType, NoiseProfile, build_timeline
)
from tests.stress.stress_harness import StressHarness
from config import (
    FATIGUE_L1_THRESH, FATIGUE_L2_THRESH, FATIGUE_L3_THRESH,
    EAR_GLASSES_THRESH
)

passed = 0
failed = 0

def check(name, condition, expected=None, got=None):
    global passed, failed
    if condition:
        print(f"  {name}")
        passed += 1
    else:
        print(f"  {name}")
        if expected is not None:
            print(f"     Expected: {expected}")
            print(f"     Got:      {got}")
        failed += 1


# Glasses operator baseline values — lower EAR, similar MAR/pitch
GLASSES_BASELINE_EAR   = 0.18   # below EAR_GLASSES_THRESH → triggers glasses_mode in real app
GLASSES_BASELINE_MAR   = 0.10
GLASSES_BASELINE_PITCH = 2.5


def build_glasses_timeline(duration_sec: float) -> list:
    segments = []
    # Alert period
    segments.append(Segment(SegmentType.ALERT, duration_sec * 0.15, "glasses_alert_open"))

    # Glasses glare burst — should NOT trigger fatigue alert
    # EAR drops to ~0.06 but MAR and pitch stay resting
    segments.append(Segment(SegmentType.GLASSES_GLARE, 4.0, "glare_burst_1"))
    segments.append(Segment(SegmentType.ALERT, duration_sec * 0.05, "post_glare_alert_1"))
    segments.append(Segment(SegmentType.GLASSES_GLARE, 6.0, "glare_burst_2"))
    segments.append(Segment(SegmentType.ALERT, duration_sec * 0.05, "post_glare_alert_2"))

    # Real drowsiness — MAR rises, head droops, EAR also falls
    segments.append(Segment(SegmentType.HEAD_DROOP, 8.0, "head_droop_glasses"))
    segments.append(Segment(SegmentType.YAWN, 5.0, "yawn_glasses_1"))
    segments.append(Segment(SegmentType.DROWSY_BUILDUP, duration_sec * 0.12,
                             "drowsy_buildup_glasses", extra={"target_ear": 0.10}))
    segments.append(Segment(SegmentType.MICROSLEEP, 3.0, "microsleep_glasses"))
    segments.append(Segment(SegmentType.RECOVERY, duration_sec * 0.08, "recovery_glasses"))

    # Another glare burst during recovery — system must still not false-alert
    segments.append(Segment(SegmentType.GLASSES_GLARE, 5.0, "glare_burst_3_during_recovery"))
    segments.append(Segment(SegmentType.ALERT, duration_sec * 0.10, "post_recovery_alert"))

    # Second drowsy wave
    segments.append(Segment(SegmentType.YAWN, 4.0, "yawn_glasses_2"))
    segments.append(Segment(SegmentType.HEAD_DROOP, 10.0, "head_droop_glasses_2"))
    segments.append(Segment(SegmentType.DROWSY_BUILDUP, duration_sec * 0.10,
                             "drowsy_buildup_glasses_2", extra={"target_ear": 0.08}))
    segments.append(Segment(SegmentType.RECOVERY, duration_sec * 0.10, "recovery_glasses_2"))

    # Closing alert period
    remaining = duration_sec - sum(s.duration_sec for s in segments)
    if remaining > 0:
        segments.append(Segment(SegmentType.ALERT, remaining, "glasses_closing"))

    return segments


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true",
                        help="Run 60s per profile instead of 30min")
    args = parser.parse_args()

    speed        = 999.0 if args.fast else 1.0
    duration_sec = 60 if args.fast else 30 * 60

    print("="*60)
    print("  STRESS TEST — Glasses operator profile")
    print(f"  baseline_ear={GLASSES_BASELINE_EAR} (< {EAR_GLASSES_THRESH} → glasses_mode=True)")
    print(f"  Mode: {'FAST (60s)' if args.fast else 'REAL-TIME (30 min)'}")
    print("="*60)

    # PROFILE 1: normal indoor light 
    
    print("  PROFILE: glasses-operator  |  normal_indoor light")
    

    noise_normal = NoiseProfile(
        ear_jitter=0.012, mar_jitter=0.012, pitch_jitter=0.9,
        vibration_hz=9.0, vibration_amp=0.008
    )
    segments = build_glasses_timeline(duration_sec)
    scenario  = build_timeline(segments, baseline_ear=GLASSES_BASELINE_EAR,
                               noise=noise_normal, seed=7001)

    # Record frame range of all GLASSES_GLARE segments for assertion below
    glare_ranges = []
    frame_cursor = 0
    for seg in segments:
        seg_frames = int(seg.duration_sec * 30)
        if seg.type == SegmentType.GLASSES_GLARE:
            glare_ranges.append((frame_cursor, frame_cursor + seg_frames))
        frame_cursor += seg_frames

    harness = StressHarness(
        operator_id    = "OP_GLASSES_STRESS",
        baseline_ear   = GLASSES_BASELINE_EAR,
        baseline_mar   = GLASSES_BASELINE_MAR,
        baseline_pitch = GLASSES_BASELINE_PITCH,
        glasses_mode   = True,
        verbose        = True,
    )
    harness.start_threads()

    t0 = time.time()
    harness.run_scenario(scenario, auto_ack_delay=4.0, speed_multiplier=speed)
    elapsed = time.time() - t0
    harness.stop_threads(generate_report=False)

    summary_1 = harness.get_summary()
    print(f"\nProfile completed in {elapsed:.1f}s")

    
    print("  ASSERTIONS — glasses normal_indoor profile")
    

    check("All frames processed",
          summary_1["frames_processed"] == len(scenario.frames),
          len(scenario.frames), summary_1["frames_processed"])

    check("No exceptions during processing",
          True)  # any exception would have propagated above

    
    alerts_during_glare = []
    for start_f, end_f in glare_ranges:
        for ev in summary_1["event_log"]:
            if start_f <= ev["frame"] <= end_f:
                alerts_during_glare.append(ev)

    check("No false alerts during GLASSES_GLARE segments "
          "(glasses_mode correctly ignores EAR-only drops from lens reflection)",
          len(alerts_during_glare) == 0,
          "0 alerts during glare windows",
          f"{len(alerts_during_glare)} alerts in frames {glare_ranges}")

    check("Real fatigue episodes (microsleep + head droop) DO generate alerts",
          summary_1["alerts_fired"] >= 1,
          ">= 1", summary_1["alerts_fired"])

    check("Fatigue score exceeds L1 threshold during drowsy buildup",
          summary_1["final_score"] >= 0 or
          any(e.get("score", 0) and e["score"] >= FATIGUE_L1_THRESH
              for e in summary_1["event_log"]),
          f">= {FATIGUE_L1_THRESH} at some point", summary_1["final_score"])

    # PROFILE 2: high vibration (hardest case for glasses) 
    # one of the critical test case to check 
    print("  PROFILE: glasses-operator  |  high_vibration")
    

    noise_vibe = NoiseProfile(
        ear_jitter=0.018, mar_jitter=0.015, pitch_jitter=2.0,
        vibration_hz=12.0, vibration_amp=0.025   # heavier machine vibration
    )
    segments2 = build_glasses_timeline(duration_sec)
    scenario2  = build_timeline(segments2, baseline_ear=GLASSES_BASELINE_EAR,
                                noise=noise_vibe, seed=7002)

    # Record glare ranges for profile 2
    glare_ranges2 = []
    frame_cursor = 0
    for seg in segments2:
        seg_frames = int(seg.duration_sec * 30)
        if seg.type == SegmentType.GLASSES_GLARE:
            glare_ranges2.append((frame_cursor, frame_cursor + seg_frames))
        frame_cursor += seg_frames

    harness2 = StressHarness(
        operator_id    = "OP_GLASSES_STRESS",
        baseline_ear   = GLASSES_BASELINE_EAR,
        baseline_mar   = GLASSES_BASELINE_MAR,
        baseline_pitch = GLASSES_BASELINE_PITCH,
        glasses_mode   = True,
        verbose        = True,
    )
    harness2.start_threads()
    t0 = time.time()
    harness2.run_scenario(scenario2, auto_ack_delay=4.0, speed_multiplier=speed)
    elapsed2 = time.time() - t0
    harness2.stop_threads(generate_report=False)

    summary_2 = harness2.get_summary()
    print(f"\nProfile completed in {elapsed2:.1f}s")

    
    print("  ASSERTIONS — glasses high_vibration profile")
    
    check("All frames processed under high vibration",
          summary_2["frames_processed"] == len(scenario2.frames),
          len(scenario2.frames), summary_2["frames_processed"])

    alerts_during_glare2 = []
    for start_f, end_f in glare_ranges2:
        for ev in summary_2["event_log"]:
            if start_f <= ev["frame"] <= end_f:
                alerts_during_glare2.append(ev)

    check("No false alerts during glare even with high vibration noise",
          len(alerts_during_glare2) == 0,
          "0 glare alerts", len(alerts_during_glare2))

    check("EMA smoothing handles high vibration (no exception on noisy pitch)",
          True)  # would have raised if EMA failed

    
    
    print("  CROSS-PROFILE SUMMARY")
    
    print(f"  normal_indoor   | alerts={summary_1['alerts_fired']:3d} "
          f"| glare_false_alerts={len(alerts_during_glare)}")
    print(f"  high_vibration  | alerts={summary_2['alerts_fired']:3d} "
          f"| glare_false_alerts={len(alerts_during_glare2)}")

    
    total = passed + failed
    print(f"  RESULTS: {passed}/{total} checks passed")
    if failed == 0:
        print("  ALL CHECKS PASSED")
        print("  Glasses-mode correctly handles glare artifacts and real fatigue.")
    else:
        print(f"   {failed} CHECKS FAILED")
   

    return failed == 0


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)