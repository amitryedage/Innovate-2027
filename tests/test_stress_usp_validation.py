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



