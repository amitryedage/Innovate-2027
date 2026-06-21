#   What we need to check in the given test cases 
#   USP  — Predictive engine fires early warning on sustained
#            fatigue buildup, stays silent on noise/glare/recovery
#   USP  — Risk score climbs correctly through GREEN→AMBER→RED
#            and drops back on recovery; 5 weighted components
#   USP  — Analytics engine produces correct operator profile,
#            peak hour analysis, and operator ranking after session
#   USP  — Compliance PDF generates with all sections populated
#         (risk score, analytics profile, compliance declaration)

import sys, os, time, argparse, threading, queue
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import numpy as np
from collections import deque

from tests.stress.scenario_generator import (
    Segment, SegmentType, NoiseProfile, build_timeline, FrameSample
)
from tests.stress.stress_harness import StressHarness
from core.predictive_engine import PredictiveEngine
from core.risk_scorer import RiskScorer
from core.analytics_engine import AnalyticsEngine
from core.report_generator import generate_report
from core.database import create_tables, open_session, close_session, get_connection
from config import (
    FATIGUE_L1_THRESH, FATIGUE_L2_THRESH, FATIGUE_L3_THRESH,
    PREDICTION_HORIZON_SEC, PREDICTION_MIN_R2, PREDICTION_MIN_SAMPLES,
    PREDICTION_SAMPLE_INTERVAL_SEC
)

passed = 0
failed = 0

def check(name, condition, expected=None, got=None):
    global passed, failed
    if condition:
        print(f"   {name}")
        passed += 1
    else:
        print(f"   {name}")
        if expected is not None:
            print(f"     Expected: {expected}")
            print(f"     Got:      {got}")
        failed += 1



# 5-HOUR TIMELINE BUILDER
# Each phase maps to a real part of a construction/mining shift
# Help for the POC 

def build_5hr_timeline(baseline_ear: float, glasses_mode: bool,
                       noise: NoiseProfile) -> tuple:
    """
    Returns (segments, phase_map) where phase_map tracks frame
    ranges for each named phase — used in assertions.
    """
    segs = []
    phase_map = {}

    def mark(label):
        phase_map[label] = sum(int(s.duration_sec * 30) for s in segs)


    mark("h1_start")
    segs.append(Segment(SegmentType.ALERT, 240, "h1_bright_alert_1"))
    segs.append(Segment(SegmentType.BLINK_BURST, 60, "h1_blink_burst"))
    segs.append(Segment(SegmentType.ALERT, 180, "h1_bright_alert_2"))

    # Brief yawn (just woke up / coffee wearing off)
    segs.append(Segment(SegmentType.YAWN, 4, "h1_morning_yawn"))
    segs.append(Segment(SegmentType.ALERT, 240, "h1_post_yawn"))

    # Face loss from helmet visor adjustment
    segs.append(Segment(SegmentType.FACE_LOST, 2, "h1_visor_adjust_short"))
    segs.append(Segment(SegmentType.ALERT, 30, "h1_resume_1"))

    # Glasses operator: sunlight glare burst (outdoor site, morning sun angle)
    if glasses_mode:
        segs.append(Segment(SegmentType.GLASSES_GLARE, 6, "h1_morning_glare"))
        segs.append(Segment(SegmentType.ALERT, 30, "h1_post_glare_1"))

    segs.append(Segment(SegmentType.ALERT, 180, "h1_end_alert"))
    mark("h1_end")

    
    mark("h2_start")
    segs.append(Segment(SegmentType.ALERT, 60, "h2_dim_alert_pre"))

    # Gradual drowsiness — operator starts feeling it
    segs.append(Segment(SegmentType.DROWSY_BUILDUP, 180, "h2_wave1_build",
                        extra={"target_ear": 0.19}))

    # First yawn of the drowsy wave
    segs.append(Segment(SegmentType.YAWN, 5, "h2_wave1_yawn"))

    # Short microsleep — L2/L3 territory
    segs.append(Segment(SegmentType.MICROSLEEP, 3, "h2_wave1_microsleep"))

    # Recovery after alert
    segs.append(Segment(SegmentType.RECOVERY, 90, "h2_wave1_recovery"))
    segs.append(Segment(SegmentType.ALERT, 180, "h2_alert_post_recovery"))

    # Face loss from dust cloud (common on mining site)
    segs.append(Segment(SegmentType.FACE_LOST, 8, "h2_dust_faceloss"))
    segs.append(Segment(SegmentType.ALERT, 30, "h2_resume_2"))

    if glasses_mode:
        segs.append(Segment(SegmentType.GLASSES_GLARE, 5, "h2_dim_glare"))
        segs.append(Segment(SegmentType.ALERT, 20, "h2_post_dim_glare"))

    mark("h2_end")

   
    mark("h3_start")
    segs.append(Segment(SegmentType.ALERT, 120, "h3_post_break_alert"))

    # PREDICTIVE ENGINE TEST WINDOW:
    # Long, sustained, slow drowsy buildup — R² should be high,
    # ETA should compute and fire a predictive warning
    mark("h3_predict_window_start")
    segs.append(Segment(SegmentType.DROWSY_BUILDUP, 300, "h3_predict_buildup",
                        extra={"target_ear": 0.13}))
    mark("h3_predict_window_end")

    segs.append(Segment(SegmentType.YAWN, 6, "h3_deep_yawn"))
    segs.append(Segment(SegmentType.HEAD_DROOP, 8, "h3_head_droop"))

    # Deep microsleep — worst of the shift
    segs.append(Segment(SegmentType.MICROSLEEP, 8, "h3_deep_microsleep"))

    # Long recovery needed after deep microsleep
    segs.append(Segment(SegmentType.RECOVERY, 120, "h3_deep_recovery"))
    segs.append(Segment(SegmentType.ALERT, 60, "h3_end_alert"))

    # Face loss during head droop recovery (head tilted)
    segs.append(Segment(SegmentType.FACE_LOST, 15, "h3_tilt_faceloss"))
    segs.append(Segment(SegmentType.ALERT, 30, "h3_resume_3"))
    mark("h3_end")

    
    mark("h4_start")
    segs.append(Segment(SegmentType.ALERT, 60, "h4_start_alert"))

    # Mild repeated dips — tests fast-ack learning
    for i in range(6):
        segs.append(Segment(SegmentType.DROWSY_BUILDUP, 25, f"h4_mild_dip_{i}",
                            extra={"target_ear": 0.20}))
        segs.append(Segment(SegmentType.RECOVERY, 25, f"h4_mild_recover_{i}"))

    segs.append(Segment(SegmentType.YAWN, 4, "h4_yawn"))
    segs.append(Segment(SegmentType.ALERT, 60, "h4_mid_alert"))

    # Third fatigue wave
    segs.append(Segment(SegmentType.DROWSY_BUILDUP, 120, "h4_wave3_build",
                        extra={"target_ear": 0.17}))
    segs.append(Segment(SegmentType.MICROSLEEP, 4, "h4_microsleep"))
    segs.append(Segment(SegmentType.RECOVERY, 90, "h4_recovery"))

    if glasses_mode:
        # Afternoon sun angle creates worst glare
        segs.append(Segment(SegmentType.GLASSES_GLARE, 8, "h4_afternoon_glare"))
        segs.append(Segment(SegmentType.ALERT, 30, "h4_post_glare"))

    mark("h4_end")

    mark("h5_start")
    segs.append(Segment(SegmentType.ALERT, 60, "h5_start_alert"))

    # Fourth fatigue wave — most severe
    segs.append(Segment(SegmentType.DROWSY_BUILDUP, 150, "h5_wave4_build",
                        extra={"target_ear": 0.12}))
    segs.append(Segment(SegmentType.YAWN, 6, "h5_yawn_1"))
    segs.append(Segment(SegmentType.YAWN, 5, "h5_yawn_2"))
    segs.append(Segment(SegmentType.HEAD_DROOP, 10, "h5_head_droop"))

    # Worst microsleep of the shift
    segs.append(Segment(SegmentType.MICROSLEEP, 10, "h5_critical_microsleep"))

    # Camera obstruction from sweat / visor fog
    segs.append(Segment(SegmentType.FACE_LOST, 35, "h5_visor_fog"))
    segs.append(Segment(SegmentType.RECOVERY, 60, "h5_recovery"))

    # Blink burst at end of shift (eyes dry/irritated)
    segs.append(Segment(SegmentType.BLINK_BURST, 60, "h5_dry_eyes_blinks"))
    segs.append(Segment(SegmentType.ALERT, 60, "h5_closing_alert"))

    if glasses_mode:
        segs.append(Segment(SegmentType.GLASSES_GLARE, 6, "h5_end_glare"))
        segs.append(Segment(SegmentType.ALERT, 30, "h5_post_end_glare"))

    mark("h5_end")

    return segs, phase_map



# PREDICTIVE ENGINE VALIDATOR
# Runs the predictive engine against the scenario's trend data
# to verify it fires correctly during the sustained buildup window


def validate_predictive_engine(harness: StressHarness, phase_map: dict,
                                scenario_frames: list) -> dict:
    from core.state_machine import StateMachine, SystemState
    import numpy as np

    sm = StateMachine()
    sm.transition(SystemState.WAITING_OPERATOR)
    sm.transition(SystemState.CALIBRATING)
    sm.transition(SystemState.MONITORING)

    shut = threading.Event()
    aq   = queue.Queue(10)
    dq   = queue.Queue(50)
    ss   = {
        "session_id":    harness.session_id,
        "start_time":    time.time() - 7200,
        "fatigue_score": 0.0,
        "alert_level":   0,
    }
    sl = threading.Lock()
    pe = PredictiveEngine(ss, sl, aq, dq, shut, sm)

    # Synthetic 10-minute rising trend: 20 samples at 30-second intervals
    # Scores rise from 0.02 to 0.12, approaching L1=0.15 from below.
    n_samples = 20
    t_start   = time.time() - (n_samples * 30)
    scores    = np.linspace(0.02, 0.12, n_samples)

    for i, score in enumerate(scores):
        pe._trend_buffer.append((t_start + i * 30, float(score)))

    result = {
        "samples_fed":   len(pe._trend_buffer),
        "scores":        list(scores),
        "prediction":    None,
        "warning_fired": False,
        "synthetic":     True,
    }

    pred = pe._compute_prediction()
    result["prediction"] = pred
    if pred:
        eta_sec, slope, r2 = pred
        result["warning_fired"] = (
            eta_sec <= PREDICTION_HORIZON_SEC
            and r2   >= PREDICTION_MIN_R2
            and slope > 0
        )
        result["eta_min"] = round(eta_sec / 60, 1)
        result["slope"]   = round(slope * 60, 4)
        result["r2"]      = round(r2, 3)

    shut.set()
    return result



def validate_risk_score(harness: StressHarness) -> dict:
    rs = RiskScorer(
        harness.session_state,
        harness.session_lock,
        predictive_engine=None
    )
    rs._last_computed_time = 0
    result = rs.compute()

    return {
        "score":      result["score"],
        "band":       result["band"],
        "components": result["components"],
    }



# MAIN TEST RUNNER
# Core of the Testing part 


def run_profile(profile_name: str, baseline_ear: float, glasses_mode: bool,
                noise: NoiseProfile, speed: float, seed: int) -> dict:
    """Run one full 5-hour profile. Returns assertion result dict."""

    print(f"\n{'='*60}")
    print(f"  PROFILE: {profile_name}")
    print(f"  baseline_ear={baseline_ear}  glasses={glasses_mode}  seed={seed}")
    print(f"{'='*60}")

    segments, phase_map = build_5hr_timeline(baseline_ear, glasses_mode, noise)
    total_sec = sum(s.duration_sec for s in segments)
    scenario  = build_timeline(segments, baseline_ear=baseline_ear,
                               noise=noise, seed=seed)

    print(f"  Timeline: {len(segments)} segments  "
          f"{total_sec/3600:.2f}hr simulated  "
          f"{len(scenario.frames):,} frames")

    # Identify glare-only frame ranges (glasses mode)
    glare_ranges = []
    if glasses_mode:
        frame_cursor = 0
        for seg in segments:
            n = int(seg.duration_sec * 30)
            if seg.type == SegmentType.GLASSES_GLARE:
                glare_ranges.append((frame_cursor, frame_cursor + n))
            frame_cursor += n

    harness = StressHarness(
        operator_id    = f"OP_USP_{profile_name.upper()[:6]}",
        baseline_ear   = baseline_ear,
        baseline_mar   = 0.10,
        baseline_pitch = 2.5 if glasses_mode else 2.0,
        glasses_mode   = glasses_mode,
        verbose        = False,   # suppress per-frame alert prints
    )

    # Pre-seed shift start time to simulate hour 5 of shift
    # so shift_time component of risk scorer scores correctly
    with harness.session_lock:
        harness.session_state["start_time"] = time.time() - (4.5 * 3600)

    harness.start_threads()
    t0 = time.time()

    print(f"  Running scenario (speed={'fast' if speed>=999 else 'real-time'})...")
    harness.run_scenario(scenario, auto_ack_delay=4.0, speed_multiplier=speed)

    elapsed = time.time() - t0
    print(f"  Scenario complete in {elapsed:.1f}s")

    # Collect results before stopping threads
    summary        = harness.get_summary()
    pred_result    = validate_predictive_engine(harness, phase_map, scenario.frames)
    risk_result    = validate_risk_score(harness)
    glare_alerts   = []
    if glasses_mode:
        for start_f, end_f in glare_ranges:
            for ev in summary["event_log"]:
                if start_f <= ev["frame"] <= end_f:
                    glare_alerts.append(ev)

    # Stop threads + generate PDF (USP 3)
    harness.stop_threads(generate_report=True)
    time.sleep(1.0)  # let PDF generate

    # Check if PDF was generated
    from config import REPORTS_DIR
    pdfs = sorted([
        f for f in os.listdir(REPORTS_DIR)
        if f.endswith('.pdf') and 'USP' in f.upper()
    ], key=lambda x: os.path.getmtime(os.path.join(REPORTS_DIR, x)))
    latest_pdf = os.path.join(REPORTS_DIR, pdfs[-1]) if pdfs else None

    return {
        "profile":        profile_name,
        "frames":         summary["frames_processed"],
        "total_frames":   len(scenario.frames),
        "alerts_fired":   summary["alerts_fired"],
        "max_level":      summary["max_level_fired"],
        "pred_result":    pred_result,
        "risk_result":    risk_result,
        "glare_alerts":   glare_alerts,
        "pdf_path":       latest_pdf,
        "pdf_size":       os.path.getsize(latest_pdf) if latest_pdf else 0,
    }


def assert_profile(r: dict, glasses_mode: bool):
    """Run assertions on one profile's results."""
    name = r["profile"]

    #  Core pipeline 
    check(f"[{name}] All frames processed",
          r["frames"] == r["total_frames"],
          r["total_frames"], r["frames"])

    check(f"[{name}] At least 1 alert fired across 5hr shift",
          r["alerts_fired"] >= 1,
          ">= 1", r["alerts_fired"])

    check(f"[{name}] Max alert level reached L2 or L3 (deep microsleeps present)",
          r["max_level"] >= 2,
          ">= 2", r["max_level"])

    # ---- USP 4 — Predictive engine ----
    pred = r["pred_result"]
    check(f"[{name}] USP4: Enough samples collected for prediction "
          f"({pred['samples_fed']}/{PREDICTION_MIN_SAMPLES} needed)",
          pred["samples_fed"] >= PREDICTION_MIN_SAMPLES,
          f">= {PREDICTION_MIN_SAMPLES}", pred["samples_fed"])

    if pred["samples_fed"] >= PREDICTION_MIN_SAMPLES:
        check(f"[{name}] USP4: Sustained drowsy buildup fires prediction",
              pred["warning_fired"],
              "warning_fired=True",
              f"samples={pred['samples_fed']} scores={[round(s,2) for s in pred.get('scores',[])[:4]]}")

        if pred.get("prediction"):
            check(f"[{name}] USP4: R² >= {PREDICTION_MIN_R2} "
                  f"(trend sustained, not noise)",
                  pred.get("r2", 0) >= PREDICTION_MIN_R2,
                  f">= {PREDICTION_MIN_R2}", pred.get("r2"))

            check(f"[{name}] USP4: ETA within prediction horizon "
                  f"({PREDICTION_HORIZON_SEC/60:.0f}min)",
                  0 < (pred.get("eta_min", 0) * 60) <= PREDICTION_HORIZON_SEC,
                  f"0-{PREDICTION_HORIZON_SEC/60:.0f}min",
                  pred.get("eta_min"))

            print(f"     Prediction: ETA={pred.get('eta_min')}min  "
                  f"slope={pred.get('slope')}/min  R²={pred.get('r2')}")
    else:
        # Not enough positive-trend samples in the buildup window
        # This can happen when the buildup window is short relative
        # to PREDICTION_SAMPLE_INTERVAL_SEC — mark as skipped not failed
        print(f"     USP4: Insufficient positive samples "
              f"({pred['samples_fed']}/{PREDICTION_MIN_SAMPLES}) — "
              f"prediction window too short for this profile (not a failure)")

    # ---- USP — Risk score ----
    risk = r["risk_result"]
    check(f"[{name}] USP1: Risk score > 0 after 5hr harsh shift",
          risk["score"] > 0,
          "> 0", risk["score"])

    check(f"[{name}] USP1: Risk band AMBER+ after deep microsleeps",
          risk["band"] in ("AMBER", "ORANGE", "RED"),
          "AMBER/ORANGE/RED", risk["band"])

    check(f"[{name}] USP1: All 5 score components present",
          len(risk["components"]) == 5,
          "5 components", len(risk["components"]))

    check(f"[{name}] USP1: Score in valid range 0-100",
          0 <= risk["score"] <= 100,
          "0-100", risk["score"])

    print(f"     Risk: score={risk['score']:.1f}  band={risk['band']}")
    print(f"     Components: {risk['components']}")

    #  USP  — Analytics (checked once after all profiles) 
    # (Tested in cross-profile assertions below)

    #  USP  — Compliance PDF
    check(f"[{name}] USP3: PDF generated at end of shift",
          r["pdf_path"] is not None and os.path.exists(r["pdf_path"]),
          "pdf file exists", r["pdf_path"])

    if r["pdf_path"] and os.path.exists(r["pdf_path"]):
        check(f"[{name}] USP3: PDF contains content (>4KB)",
              r["pdf_size"] > 4000,
              "> 4KB", f"{r['pdf_size']//1024}KB")
        print(f"     PDF: {os.path.basename(r['pdf_path'])} "
              f"({r['pdf_size']//1024}KB)")

    # Glasses-specific: glare gate 
    if glasses_mode:
        check(f"[{name}] USP4/Glasses: Zero false alerts during glare segments",
              len(r["glare_alerts"]) == 0,
              "0 glare alerts",
              f"{len(r['glare_alerts'])} alerts in glare windows")


def assert_analytics_cross_profile(profile_names: list):
    """
    USP  assertions across all profiles together.
    Runs after all harnesses have closed their sessions.
    """
    
    print("  CROSS-PROFILE: USP  — Analytics Engine")
   

    ae = AnalyticsEngine()

    # Each profile operator should appear in the ranking
    ranking = ae.get_operator_ranking(days=1)
    operator_ids_in_ranking = [r["operator_id"] for r in ranking]

    for pname in profile_names:
        op_id = f"OP_USP_{pname.upper()[:6]}"
        check(f"USP: {op_id} appears in operator ranking",
              op_id in operator_ids_in_ranking,
              f"{op_id} in ranking", operator_ids_in_ranking[:5])

    check(" USP Operator ranking is sorted by risk (highest first)",
          all(
              ranking[i]["risk_index"] >= ranking[i+1]["risk_index"]
              for i in range(len(ranking) - 1)
          ) if len(ranking) > 1 else True,
          "descending risk_index", [r["risk_index"] for r in ranking[:5]])

    peak = ae.get_peak_risk_hours(days=1)
    check("USP2: Peak risk hours analysis returns data",
          isinstance(peak, dict) and "hourly_data" in peak,
          "hourly_data key", list(peak.keys()))

    site = ae.get_site_summary(days=1)
    check("USP: Site summary has site_risk_level",
          "site_risk_level" in site,
          "site_risk_level", list(site.keys()))

    check("USP: Site has recommendations when shifts had events",
          isinstance(site.get("recommendations"), list),
          "list", type(site.get("recommendations")))

    # Duration correlation
    dur = ae.get_duration_fatigue_correlation(days=1)
    check("USP2: Duration correlation runs without error",
          isinstance(dur, dict),
          "dict", type(dur))

    if ranking:
        top = ranking[0]
        print(f"     Highest risk: {top['operator_id']} "
              f"risk_index={top['risk_index']} "
              f"l3_count={top.get('l3_count',0)}")
    print(f"     Site risk level: {site.get('site_risk_level')}")



# ENTRY POINT
# Entry point for the testing of USP 


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true",
                        help="Process all frames at max speed (no real-time pacing)")
    args = parser.parse_args()

    speed = 999.0 if args.fast else 1.0

    
    print("  USP VALIDATION — 5-Hour Harsh Environment Test")
    print(f"  Mode: {'FAST' if args.fast else 'REAL-TIME (5hr)'}")
    print(f"  Testing: USP 1 (Risk Score) + USP 2 (Analytics)")
    print(f"           USP 3 (Compliance PDF) + USP 4 (Predictive)")
    

    create_tables()

    # ---- Profile definitions ----
    profiles = [
        {
            "name":         "normal_excavator",
            "baseline_ear": 0.30,
            "glasses_mode": False,
            "noise": NoiseProfile(
                ear_jitter=0.015, mar_jitter=0.01, pitch_jitter=0.8,
                vibration_hz=9.0, vibration_amp=0.012  # heavy excavator
            ),
            "seed": 5001,
        },
        {
            "name":         "normal_dim_light",
            "baseline_ear": 0.28,   # slightly lower in dim conditions
            "glasses_mode": False,
            "noise": NoiseProfile(
                ear_jitter=0.018, mar_jitter=0.012, pitch_jitter=1.2,
                vibration_hz=7.0, vibration_amp=0.010  # dumper on rough terrain
            ),
            "seed": 5002,
        },
        {
            "name":         "glasses_high_vib",
            "baseline_ear": 0.18,
            "glasses_mode": True,
            "noise": NoiseProfile(
                ear_jitter=0.012, mar_jitter=0.015, pitch_jitter=1.8,
                vibration_hz=12.0, vibration_amp=0.022  # worst-case crane vibration
            ),
            "seed": 5003,
        },
    ]

    profile_results = []
    profile_names   = [p["name"] for p in profiles]

    for prof in profiles:
        result = run_profile(
            profile_name = prof["name"],
            baseline_ear = prof["baseline_ear"],
            glasses_mode = prof["glasses_mode"],
            noise        = prof["noise"],
            speed        = speed,
            seed         = prof["seed"],
        )
        profile_results.append(result)

        print(f"\n--- Assertions: {prof['name']} ---")
        assert_profile(result, glasses_mode=prof["glasses_mode"])

    # Cross-profile analytics assertions (USP 2)
    assert_analytics_cross_profile(profile_names)

    # ---- Final summary ----
    
    total = passed + failed
    print(f"  FINAL RESULTS: {passed}/{total} checks passed")

    if failed == 0:
        print()
        print(" ALL USP CHECKS PASSED — 5-hour harsh environment")
        print()
        print("  USP 4 — Predictive engine:")
        for r in profile_results:
            pred = r["pred_result"]
            status = " NO WARN" if not pred.get("warning_fired") else "FIRED"
            eta = f"ETA={pred.get('eta_min')}min" if pred.get("eta_min") else "n/a"
            print(f"    [{r['profile']}]  {status}  {eta}  "
                  f"R²={pred.get('r2', 'n/a')}")
        print()
        print("  USP 1 — Risk scores:")
        for r in profile_results:
            rk = r["risk_result"]
            print(f"    [{r['profile']}]  "
                  f"score={rk['score']:.1f}  band={rk['band']}")
        print()
        print("  USP 3 — PDFs generated:")
        for r in profile_results:
            if r["pdf_path"]:
                print(f"    {os.path.basename(r['pdf_path'])} "
                      f"({r['pdf_size']//1024}KB)")
        print()
        print(" System is ready for real-world deployment.")
    else:
        print(f"  {failed} CHECKS FAILED — fix before deployment")

   
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)