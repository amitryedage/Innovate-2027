import random
import math
from dataclasses import dataclass, field
from enum import Enum

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from config import FPS_TARGET, EAR_OPEN_NORMAL


class SegmentType(Enum):
    ALERT          = "alert"            # normal, fully awake
    DROWSY_BUILDUP = "drowsy_buildup"    # EAR drifts down gradually
    MICROSLEEP     = "microsleep"        # short full eye-closure burst
    YAWN           = "yawn"              # MAR spike, open-hold-close
    HEAD_DROOP     = "head_droop"        # pitch climbs and holds
    BLINK_BURST    = "blink_burst"        # rapid normal blinks (noise stress)
    FACE_LOST      = "face_lost"          # face_detected goes False
    RECOVERY       = "recovery"           # returning to alert baseline
    GLASSES_GLARE  = "glasses_glare"      # sudden EAR dropout (reflection artifact)


@dataclass
class FrameSample:
    ear: float
    mar: float
    pitch: float
    face_detected: bool = True


@dataclass
class NoiseProfile:
    """Configurable sensor/vibration noise added on top of the base signal."""
    ear_jitter:   float = 0.015   # std dev added to EAR per frame
    mar_jitter:   float = 0.01
    pitch_jitter: float = 0.8     # degrees
    vibration_hz: float = 9.0     # simulated cab vibration frequency
    vibration_amp:float = 0.01    # amplitude of vibration sine wave on EAR



# SEGMENT GENERATORS
# Each returns a list[FrameSample] for `duration_sec` at FPS_TARGET


def _frames(duration_sec: float) -> int:
    return max(1, int(duration_sec * FPS_TARGET))


def gen_alert(duration_sec: float, baseline_ear: float, rng: random.Random,
              noise: NoiseProfile) -> list:
    """Fully alert operator — EAR near baseline, occasional natural blinks."""
    out = []
    n = _frames(duration_sec)
    blink_every = rng.randint(int(2.5*FPS_TARGET), int(5*FPS_TARGET))
    for i in range(n):
        ear = baseline_ear + rng.gauss(0, noise.ear_jitter)
        # Natural blink — brief dip
        if i % blink_every < 2:
            ear = 0.08 + rng.uniform(0, 0.03)
        mar   = 0.08 + rng.gauss(0, noise.mar_jitter)
        pitch = 2.0  + rng.gauss(0, noise.pitch_jitter)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.6), pitch))
    return out


def gen_drowsy_buildup(duration_sec: float, baseline_ear: float,
                       target_ear: float, rng: random.Random,
                       noise: NoiseProfile) -> list:
    """EAR linearly drifts from baseline down to target_ear over the segment."""
    out = []
    n = _frames(duration_sec)
    for i in range(n):
        frac = i / max(n - 1, 1)
        base = baseline_ear + (target_ear - baseline_ear) * frac
        ear  = base + rng.gauss(0, noise.ear_jitter)
        mar  = 0.10 + frac * 0.15 + rng.gauss(0, noise.mar_jitter)
        pitch= 2.0  + frac * 6.0  + rng.gauss(0, noise.pitch_jitter)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.6), pitch))
    return out


def gen_microsleep(duration_sec: float, rng: random.Random,
                   noise: NoiseProfile) -> list:
    """Short full eye-closure burst — EAR near zero for the duration."""
    out = []
    n = _frames(duration_sec)
    for _ in range(n):
        ear = 0.05 + rng.uniform(0, 0.04)
        mar = 0.10 + rng.gauss(0, noise.mar_jitter)
        pitch = 14.0 + rng.gauss(0, noise.pitch_jitter * 1.5)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.6), pitch))
    return out


def gen_yawn(duration_sec: float, rng: random.Random,
            noise: NoiseProfile) -> list:
    """Mouth opens, holds wide, closes — classic yawn shape (sine bump)."""
    out = []
    n = _frames(duration_sec)
    for i in range(n):
        frac = i / max(n - 1, 1)
        mar_shape = math.sin(frac * math.pi)            # 0 -> 1 -> 0
        mar  = 0.10 + mar_shape * 0.65 + rng.gauss(0, noise.mar_jitter)
        ear  = 0.27 + rng.gauss(0, noise.ear_jitter)     # eyes squint slightly
        pitch= 4.0  + mar_shape * 4.0 + rng.gauss(0, noise.pitch_jitter)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.85), pitch))
    return out


def gen_head_droop(duration_sec: float, rng: random.Random,
                   noise: NoiseProfile) -> list:
    """Head pitch climbs and holds above droop threshold."""
    out = []
    n = _frames(duration_sec)
    rise_frames = max(1, n // 4)
    for i in range(n):
        if i < rise_frames:
            pitch = 2.0 + (18.0 - 2.0) * (i / rise_frames)
        else:
            pitch = 18.0 + rng.gauss(0, noise.pitch_jitter)
        ear = 0.20 + rng.gauss(0, noise.ear_jitter)
        mar = 0.10 + rng.gauss(0, noise.mar_jitter)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.6), pitch))
    return out


def gen_blink_burst(duration_sec: float, baseline_ear: float,
                    rng: random.Random, noise: NoiseProfile) -> list:
    """Rapid natural blinking — stresses debounce filter, should NOT alert."""
    out = []
    n = _frames(duration_sec)
    i = 0
    while i < n:
        blink_gap = rng.randint(int(0.3*FPS_TARGET), int(0.8*FPS_TARGET))
        for _ in range(blink_gap):
            if i >= n: break
            ear = baseline_ear + rng.gauss(0, noise.ear_jitter)
            out.append(FrameSample(_clip(ear), 0.08, 2.0))
            i += 1
        # quick blink — exactly 1-2 frames, must NOT trigger debounce (needs 3)
        for _ in range(rng.randint(1, 2)):
            if i >= n: break
            out.append(FrameSample(0.08, 0.08, 2.0))
            i += 1
    return out


def gen_face_lost(duration_sec: float) -> list:
    """Face not detected — used to test face-loss policy timers."""
    n = _frames(duration_sec)
    return [FrameSample(0.0, 0.0, 0.0, face_detected=False) for _ in range(n)]


def gen_recovery(duration_sec: float, from_ear: float, baseline_ear: float,
                 rng: random.Random, noise: NoiseProfile) -> list:
    """Operator recovers — EAR climbs back from drowsy level to baseline."""
    out = []
    n = _frames(duration_sec)
    for i in range(n):
        frac = i / max(n - 1, 1)
        base = from_ear + (baseline_ear - from_ear) * frac
        ear  = base + rng.gauss(0, noise.ear_jitter)
        mar  = 0.10 + rng.gauss(0, noise.mar_jitter)
        pitch= 8.0 * (1 - frac) + 2.0 + rng.gauss(0, noise.pitch_jitter)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.6), pitch))
    return out


def gen_glasses_glare(duration_sec: float, rng: random.Random,
                      noise: NoiseProfile) -> list:
    """
    Sudden EAR dropout caused by lens reflection — a glasses-specific
    artifact, not real fatigue. Tests whether glasses_mode weighting
    correctly avoids false-alerting on this.This help help to understand if the system can distinguish between actual fatigue and artifacts caused by glasses glare, ensuring that it does not raise false alarms in such scenarios.
    """
    out = []
    n = _frames(duration_sec)
    for _ in range(n):
        ear = 0.06 + rng.uniform(0, 0.03)   # looks like closed eyes
        mar = 0.09 + rng.gauss(0, noise.mar_jitter)   # but mouth/pitch normal
        pitch = 2.0 + rng.gauss(0, noise.pitch_jitter)
        out.append(FrameSample(_clip(ear), _clip(mar, 0, 0.6), pitch))
    return out


def _clip(v: float, lo: float = 0.0, hi: float = 0.5) -> float:
    return max(lo, min(hi, v))


# Apply cab vibration sine wave on top of an already-generated sequence
def apply_vibration(frames: list, noise: NoiseProfile) -> list:
    out = []
    for i, f in enumerate(frames):
        t = i / FPS_TARGET
        vib = noise.vibration_amp * math.sin(2 * math.pi * noise.vibration_hz * t)
        out.append(FrameSample(
            ear   = _clip(f.ear + vib),
            mar   = f.mar,
            pitch = f.pitch,
            face_detected = f.face_detected,
        ))
    return out



# SEGMENT REGISTRY — maps SegmentType to generator function


SEGMENT_GENERATORS = {
    SegmentType.ALERT:          gen_alert,
    SegmentType.DROWSY_BUILDUP: gen_drowsy_buildup,
    SegmentType.MICROSLEEP:     gen_microsleep,
    SegmentType.YAWN:           gen_yawn,
    SegmentType.HEAD_DROOP:     gen_head_droop,
    SegmentType.BLINK_BURST:    gen_blink_burst,
    SegmentType.FACE_LOST:      gen_face_lost,
    SegmentType.RECOVERY:       gen_recovery,
    SegmentType.GLASSES_GLARE:  gen_glasses_glare,
}


@dataclass
class Segment:
    """One named, timed block in a scenario timeline."""
    type: SegmentType
    duration_sec: float
    label: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    frames: list             # list[FrameSample]
    segments: list           # list[Segment] for reporting/debugging
    seed: int


