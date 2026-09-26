import streamlit as st
import numpy as np
import cv2
import math
import time
import random
import io
import wave
import os
import tempfile
from collections import deque

st.set_page_config(
    page_title="Weather Mixing Booth",
    page_icon="🎛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

WIDTH = 800
HEIGHT = 450
FPS = 20

MEDIAPIPE_AVAILABLE = False
try:
    import mediapipe as mp
    MEDIAPIPE_AVAILABLE = True
except Exception:
    mp = None


# ------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------

def clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(value)))


def lerp(a, b, t):
    return a + (b - a) * clamp(t)


def ease_in_out(t):
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)


def mix(color_a, color_b, t):
    t = clamp(t)
    return tuple(
        int(round(a + (b - a) * t))
        for a, b in zip(color_a, color_b)
    )


def crossfader_geom():
    x0 = int(WIDTH * 0.18)
    x1 = int(WIDTH * 0.82)
    y = int(HEIGHT * 0.87)
    return x0, x1, y


def knob_geom():
    rain_center = (int(WIDTH * 0.16), int(HEIGHT * 0.72))
    wind_center = (int(WIDTH * 0.84), int(HEIGHT * 0.72))
    radius = max(24, int(HEIGHT * 0.08))
    return rain_center, wind_center, radius


# ------------------------------------------------------------
# Session state
# ------------------------------------------------------------

def init_state():
    now = time.time()

    clouds = []
    rng = random.Random(11)
    for _ in range(6):
        clouds.append(
            {
                "x": rng.random(),
                "y": 0.10 + rng.random() * 0.24,
                "scale": 0.6 + rng.random() * 0.9,
                "speed": 0.5 + rng.random(),
            }
        )

    defaults = {
        "epoch": now,
        "demo_start": now,
        "last_frame_time": now,
        "frame_count": 0,
        "auto_demo": True,
        "manual_crossfade": 0.15,
        "manual_rain": 0.35,
        "manual_wind": 0.25,
        "bpm": 112,
        "trigger_drop": False,
        "trigger_scratch": False,
        "drop_until": 0.0,
        "scratch_until": 0.0,
        "lightning_until": 0.0,
        "hands_up_since": None,
        "last_camera_drop": 0.0,
        "camera_enabled": False,
        "camera_error": None,
        "scratch_history": deque(maxlen=30),
        "clouds": clouds,
        "rain": [],
        "record_path": None,
        "request_recording": False,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if "white_frame" not in st.session_state:
        st.session_state.white_frame = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)


init_state()


# ------------------------------------------------------------
# Beat / timing
# ------------------------------------------------------------

def beat_pulse(now, bpm):
    bpm = max(1, float(bpm))
    beat_len = 60.0 / bpm
    elapsed = max(0.0, now - st.session_state.epoch)
    beat_count = int(elapsed / beat_len)
    phase = (elapsed % beat_len) / beat_len
    pulse = max(0.0, 1.0 - phase * 3.2)
    return pulse, beat_count, phase


# ------------------------------------------------------------
# Demo choreography
# ------------------------------------------------------------

def auto_controls(elapsed):
    t = elapsed % 32.0
    mode = "CALM INTRO"
    drop = False
    scratch = False
    lightning = False
    hands = []

    x0, x1, cf_y = crossfader_geom()
    rain_center, wind_center, radius = knob_geom()

    if t < 5.0:
        crossfade = 0.08 + 0.02 * math.sin(t * 1.3)
        rain = 0.18 + 0.03 * math.sin(t * 0.9)
        wind = 0.15
        mode = "CALM INTRO"
        hands = [(WIDTH * 0.28, HEIGHT * 0.52)]

    elif t < 12.0:
        p = ease_in_out((t - 5.0) / 7.0)
        crossfade = lerp(0.08, 0.92, p)
        rain = lerp(0.18, 0.48, p)
        wind = lerp(0.15, 0.42, p)
        mode = "CROSSFADER BLEND"
        handle_x = lerp(x0, x1, crossfade)
        hands = [(handle_x, cf_y - 35)]

    elif t < 18.0:
        p = ease_in_out((t - 12.0) / 6.0)
        crossfade = 0.92
        rain = lerp(0.48, 0.96, p)
        wind = lerp(0.42, 0.78, p)
        mode = "RAIN KNOB"
        hand_y = rain_center[1] + int(radius * 2.2) - int(rain * radius * 3.0)
        hands = [(rain_center[0], hand_y)]

    elif t < 22.0:
        drop = True
        crossfade = 1.0
        rain = 1.0
        wind = 0.97
        mode = "WEATHER DROP"
        hands = [
            (WIDTH * 0.36, HEIGHT * 0.16),
            (WIDTH * 0.64, HEIGHT * 0.16),
        ]
        if (t - 18.0) < 0.4 or random.random() < 0.2:
            lightning = True

    elif t < 26.0:
        crossfade = 0.94
        rain = 0.90
        wind = 0.84
        mode = "STORM HOLD"
        hands = [(WIDTH * 0.50, HEIGHT * 0.42)]

    elif t < 29.0:
        scratch = True
        crossfade = 0.5 + 0.42 * math.sin(t * 16.0)
        rain = 0.86
        wind = 0.76
        mode = "SCRATCH STUTTER"
        handle_x = lerp(x0, x1, crossfade)
        hands = [(handle_x, cf_y - 30)]

    else:
        p = ease_in_out((t - 29.0) / 3.0)
        crossfade = lerp(0.90, 0.10, p)
        rain = lerp(0.80, 0.20, p)
        wind = lerp(0.70, 0.20, p)
        mode = "CALM DOWN"
        hands = [(WIDTH * 0.72, HEIGHT * 0.50)]

    return {
        "crossfade": clamp(crossfade),
        "rain": clamp(rain),
        "wind": clamp(wind),
        "drop": drop,
        "scratch": scratch,
        "lightning": lightning,
        "hands": hands,
        "mode": mode,
    }


def compute_controls(now, consume_events=True):
    if st.session_state.auto_demo:
        ctrl = auto_controls(now - st.session_state.demo_start)
    else:
        ctrl = {
            "crossfade": clamp(st.session_state.manual_crossfade),
            "rain": clamp(st.session_state.manual_rain),
            "wind": clamp(st.session_state.manual_wind),
            "drop": False,
            "scratch": False,
            "lightning": False,
            "hands": [],
            "mode": "MANUAL MIX",
        }

    if consume_events:
        if st.session_state.get("trigger_drop"):
            st.session_state.trigger_drop = False
            st.session_state.drop_until = now + 3.0
            st.session_state.lightning_until = now + 0.35

        if st.session_state.get("trigger_scratch"):
            st.session_state.trigger_scratch = False
            st.session_state.scratch_until = now + 0.35

    drop_active = now < st.session_state.drop_until or ctrl.get("drop", False)
    scratch_active = now < st.session_state.scratch_until or ctrl.get("scratch", False)
    lightning_active = now < st.session_state.lightning_until or ctrl.get("lightning", False)

    if drop_active:
        ctrl["crossfade"] = 1.0
        ctrl["rain"] = 1.0
        ctrl["wind"] = max(ctrl.get("wind", 0.0), 0.95)
        ctrl["drop"] = True
        ctrl["mode"] = "DROP OVERRIDE"
        if random.random() < 0.12:
            lightning_active = True

    if scratch_active:
        ctrl["scratch"] = True
        if "SCRATCH" not in ctrl["mode"]:
            ctrl["mode"] += " + SCRATCH"

    ctrl["lightning"] = lightning_active
    return ctrl


# ------------------------------------------------------------
# Optional webcam / MediaPipe hand control
# ------------------------------------------------------------

def get_camera_hands():
    if not MEDIAPIPE_AVAILABLE or not st.session_state.get("camera_enabled", False):
        return []

    try:
        if st.session_state.get("hand_solver") is None:
            st.session_state.hand_solver = mp.solutions.hands.Hands(
                max_num_hands=2,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )

        if st.session_state.get("camera") is None:
            st.session_state.camera = cv2.VideoCapture(0)

        cap = st.session_state.camera
        if not cap.isOpened():
            return []

        ok, cam_frame = cap.read()
        if not ok:
            return []

        cam_frame = cv2.resize(cam_frame, (320, 240))
        rgb = cv2.cvtColor(cam_frame, cv2.COLOR_BGR2RGB)
        result = st.session_state.hand_solver.process(rgb)

        hands = []
        if result.multi_hand_landmarks:
            for hand_landmarks in result.multi_hand_landmarks:
                wrist = hand_landmarks.landmark[0]
                middle = hand_landmarks.landmark[9]
                x = 1.0 - float((wrist.x + middle.x) / 2.0)
                y = float((wrist.y + middle.y) / 2.0)
                hands.append((clamp(x), clamp(y)))

        return hands

    except Exception as exc:
        st.session_state.camera_error = str(exc)
        return []


def apply_camera_controls(hands, ctrl, now):
    if not hands:
        st.session_state.hands_up_since = None
        return ctrl

    ctrl["hands"] = [(int(x * WIDTH), int(y * HEIGHT)) for x, y in hands]

    # Drop gesture: both hands raised above head height for 0.5 seconds.
    if len(hands) >= 2 and all(y < 0.25 for _, y in hands):
        if st.session_state.hands_up_since is None:
            st.session_state.hands_up_since = now
        elif now - st.session_state.hands_up_since >= 0.5 and now > st.session_state.last_camera_drop + 3.0:
            st.session_state.drop_until = now + 3.0
            st.session_state.lightning_until = now + 0.35
            st.session_state.last_camera_drop = now
            ctrl["drop"] = True
            ctrl["lightning"] = True
            ctrl["crossfade"] = 1.0
            ctrl["rain"] = 1.0
            ctrl["wind"] = 1.0
            ctrl["mode"] = "CAMERA DROP"
    else:
        st.session_state.hands_up_since = None

    # Zone controls.
    for x, y in hands:
        # Crossfader zone.
        if 0.70 <= y <= 0.97 and 0.15 <= x <= 0.85:
            ctrl["crossfade"] = clamp((x - 0.15) / 0.70)
            ctrl["mode"] = "CAMERA CROSSFADER"

            # Scratch detection: rapid back-and-forth horizontal motion.
            history = st.session_state.scratch_history
            history.append((now, x))
            if len(history) >= 6:
                directions = []
                for i in range(1, len(history)):
                    dx = history[i][1] - history[i - 1][1]
                    if abs(dx) > 0.004:
                        directions.append(1 if dx > 0 else -1)

                if len(directions) >= 4:
                    changes = sum(
                        1
                        for i in range(1, len(directions))
                        if directions[i] != directions[i - 1]
                    )
                    duration = history[-1][0] - history[0][0]
                    if changes >= 3 and duration < 0.8:
                        st.session_state.scratch_until = now + 0.35
                        ctrl["scratch"] = True
                        history.clear()
            break

        # Rain knob zone.
        if 0.25 <= y <= 0.75 and 0.05 <= x <= 0.35:
            ctrl["rain"] = clamp(1.0 - (y - 0.25) / 0.50)
            ctrl["mode"] = "CAMERA RAIN KNOB"

        # Wind knob zone.
        elif 0.25 <= y <= 0.75 and 0.65 <= x <= 0.95:
            ctrl["wind"] = clamp(1.0 - (y - 0.25) / 0.50)
            ctrl["mode"] = "CAMERA WIND KNOB"

    return ctrl


# ------------------------------------------------------------
# Weather particle system
# ------------------------------------------------------------

def new_rain_particle():
    return {
        "x": random.random() * WIDTH,
        "y": random.uniform(-HEIGHT, HEIGHT),
        "len": random.uniform(8, 20),
        "speed": random.uniform(0.55, 1.15),
        "drift": random.uniform(-0.04, 0.04),
    }


def update_rain(frame, dt, rain, wind, pulse, hidden=False):
    particles = st.session_state.rain
    target = int(650 * clamp(rain) * (0.65 + 0.5 * pulse))

    while len(particles) < target:
        particles.append(new_rain_particle())

    if len(particles) > target:
        del particles[target:]

    # Update positions even when hidden, so motion stays continuous.
    for p in particles:
        fall_speed = p["speed"] * (0.55 + 1.7 * wind + 0.35 * pulse)
        p["y"] += fall_speed * dt * HEIGHT
        p["x"] += (p["drift"] + wind * 0.45) * dt * WIDTH

        if p["y"] > HEIGHT + 20:
            p["y"] = -20
            p["x"] = random.random() * WIDTH

        if p["x"] > WIDTH + 30:
            p["x"] -= WIDTH + 60
        elif p["x"] < -30:
            p["x"] += WIDTH + 60

    if hidden:
        return

    color = (235, 215, 180)
    for p in particles:
        x = int(p["x"])
        y = int(p["y"])
        dx = int((wind * 0.6 + p["drift"]) * p["len"] * 3.0)
        cv2.line(
            frame,
            (x, y),
            (x - dx, y + int(p["len"])),
            color,
            1,
            cv2.LINE_AA,
        )


# ------------------------------------------------------------
# Visual layers
# ------------------------------------------------------------

def flash_frame(frame, alpha):
    alpha = clamp(alpha, 0.0, 0.9)
    if alpha <= 0.0:
        return
    cv2.addWeighted(
        st.session_state.white_frame,
        float(alpha),
        frame,
        1.0 - float(alpha),
        0.0,
        frame,
    )


def draw_background(frame, crossfade, pulse):
    top_calm = (235, 206, 135)
    horizon_calm = (190, 235, 255)
    top_storm = (70, 52, 42)
    horizon_storm = (110, 102, 96)

    top = mix(top_calm, top_storm, crossfade)
    horizon = mix(horizon_calm, horizon_storm, crossfade)

    if pulse > 0:
        top = mix(top, (255, 255, 255), pulse * 0.05)
        horizon = mix(horizon, (255, 255, 255), pulse * 0.03)

    strips = 30
    for i in range(strips):
        t = i / float(strips - 1)
        y0 = int(i * HEIGHT / strips)
        y1 = int((i + 1) * HEIGHT / strips)
        color = mix(top, horizon, t)
        cv2.rectangle(frame, (0, y0), (WIDTH, y1), color, -1)

    sun_strength = clamp(1.0 - crossfade * 1.3)
    if sun_strength > 0.03:
        overlay = frame.copy()
        center = (int(WIDTH * 0.79), int(HEIGHT * 0.16))
        cv2.circle(overlay, center, int(HEIGHT * 0.08), (90, 220, 255), -1, cv2.LINE_AA)
        cv2.circle(overlay, center, int(HEIGHT * 0.13), (90, 220, 255), -1, cv2.LINE_AA)
        cv2.addWeighted(
            overlay,
            0.35 * sun_strength,
            frame,
            1.0 - 0.35 * sun_strength,
            0.0,
            frame,
        )


def draw_clouds(frame, dt, crossfade, wind):
    color = mix((255, 255, 255), (115, 110, 105), crossfade)

    for cloud in st.session_state.clouds:
        cloud["x"] += (0.008 + 0.07 * wind) * cloud["speed"] * dt
        if cloud["x"] > 1.25:
            cloud["x"] = -0.25

        cx = int(cloud["x"] * WIDTH)
        cy = int(cloud["y"] * HEIGHT)
        s = int(cloud["scale"] * HEIGHT * 0.10)

        cv2.ellipse(
            frame,
            (cx, cy),
            (int(s * 2.1), int(s * 0.9)),
            0,
            0,
            360,
            color,
            -1,
            cv2.LINE_AA,
        )
        cv2.ellipse(
            frame,
            (cx - int(s * 1.6), cy + int(s * 0.25)),
            (int(s * 1.4), int(s * 0.65)),
            0,
            0,
            360,
            color,
            -1,
            cv2.LINE_AA,
        )
        cv2.ellipse(
            frame,
            (cx + int(s * 1.7), cy + int(s * 0.3)),
            (int(s * 1.5), int(s * 0.7)),
            0,
            0,
            360,
            color,
            -1,
            cv2.LINE_AA,
        )


def draw_birds(frame, now, crossfade):
    strength = clamp(1.0 - crossfade / 0.35)
    if strength <= 0:
        return

    color = (40, 60, 80)
    for i in range(4):
        bx = int((0.16 + 0.20 * i + 0.04 * math.sin(now * 0.4 + i * 1.7)) * WIDTH)
        by = int((0.18 + 0.05 * math.sin(now * 0.7 + i * 2.3)) * HEIGHT)
        span = int(8 + 3 * math.sin(now * 2.0 + i))

        cv2.line(frame, (bx - span, by), (bx, by - 4), color, 1, cv2.LINE_AA)
        cv2.line(frame, (bx, by - 4), (bx + span, by), color, 1, cv2.LINE_AA)


def draw_lightning(frame, now, ctrl):
    if not ctrl.get("lightning", False):
        return

    flash_frame(frame, 0.18 + 0.15 * random.random())

    if st.session_state.frame_count % 2 == 0:
        x = random.randint(int(WIDTH * 0.2), int(WIDTH * 0.8))
        points = [(x, 0)]
        y = 0

        while y < HEIGHT * 0.65:
            y += random.randint(25, 70)
            x += random.randint(-50, 50)
            points.append((x, min(y, int(HEIGHT * 0.65))))

        for a, b in zip(points[:-1], points[1:]):
            cv2.line(frame, a, b, (255, 255, 235), 3, cv2.LINE_AA)

    cv2.putText(
        frame,
        "THUNDER CRACK",
        (int(WIDTH * 0.30), int(HEIGHT * 0.24)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )


def draw_knob(frame, center, radius, value, label, color):
    cx, cy = center
    value = clamp(value)

    cv2.circle(frame, (cx, cy), radius, (25, 25, 28), -1, cv2.LINE_AA)
    cv2.circle(frame, (cx, cy), radius, (200, 200, 210), 2, cv2.LINE_AA)

    cv2.ellipse(
        frame,
        (cx, cy),
        (radius - 7, radius - 7),
        0,
        135,
        405,
        (90, 90, 95),
        6,
        cv2.LINE_AA,
    )

    end_angle = int(135 + 270 * value)
    cv2.ellipse(
        frame,
        (cx, cy),
        (radius - 7, radius - 7),
        0,
        135,
        end_angle,
        color,
        6,
        cv2.LINE_AA,
    )

    angle_rad = math.radians(end_angle)
    x2 = int(cx + (radius - 14) * math.cos(angle_rad))
    y2 = int(cy + (radius - 14) * math.sin(angle_rad))
    cv2.line(frame, (cx, cy), (x2, y2), (235, 235, 240), 2, cv2.LINE_AA)

    text_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0]
    cv2.putText(
        frame,
        label,
        (cx - text_size[0] // 2, cy + radius + 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (230, 230, 235),
        1,
        cv2.LINE_AA,
    )

    percent_text = f"{int(value * 100)}%"
    percent_size = cv2.getTextSize(percent_text, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)[0]
    cv2.putText(
        frame,
        percent_text,
        (cx - percent_size[0] // 2, cy + 5),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.4,
        (250, 250, 250),
        1,
        cv2.LINE_AA,
    )


def draw_hud(frame, ctrl, bpm, pulse, now):
    panel = frame.copy()
    cv2.rectangle(
        panel,
        (0, int(HEIGHT * 0.66)),
        (WIDTH, HEIGHT),
        (12, 12, 16),
        -1,
    )
    cv2.addWeighted(panel, 0.52, frame, 0.48, 0.0, frame)

    # Gesture zone hints.
    cv2.rectangle(
        frame,
        (int(WIDTH * 0.15), int(HEIGHT * 0.78)),
        (int(WIDTH * 0.85), int(HEIGHT * 0.95)),
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        "CROSSFADER ZONE",
        (int(WIDTH * 0.38), int(HEIGHT * 0.815)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.4,
        (220, 220, 220),
        1,
        cv2.LINE_AA,
    )

    rain_center, wind_center, radius = knob_geom()

    cv2.rectangle(
        frame,
        (rain_center[0] - radius - 18, rain_center[1] - radius - 18),
        (rain_center[0] + radius + 18, rain_center[1] + radius + 18),
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    cv2.rectangle(
        frame,
        (wind_center[0] - radius - 18, wind_center[1] - radius - 18),
        (wind_center[0] + radius + 18, wind_center[1] + radius + 18),
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )

    # Crossfader.
    x0, x1, y = crossfader_geom()

    cv2.putText(
        frame,
        "CALM",
        (x0 - 55, y + 5),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (220, 230, 240),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        "STORM",
        (x1 + 10, y + 5),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (180, 200, 255),
        1,
        cv2.LINE_AA,
    )

    steps = 24
    for i in range(steps):
        t0 = i / float(steps)
        t1 = (i + 1) / float(steps)
        xx0 = int(lerp(x0, x1, t0))
        xx1 = int(lerp(x0, x1, t1))
        color = mix((170, 230, 120), (70, 70, 200), t0)
        cv2.line(frame, (xx0, y), (xx1, y), color, 8, cv2.LINE_AA)

    display_cross = clamp(ctrl["crossfade"])
    if ctrl.get("scratch", False):
        display_cross = clamp(display_cross + random.uniform(-0.03, 0.03))

    handle_x = int(lerp(x0, x1, display_cross))
    cv2.line(frame, (handle_x, y - 18), (handle_x, y + 18), (250, 250, 250), 4, cv2.LINE_AA)
    cv2.circle(frame, (handle_x, y), 14, (0, 220, 255), -1, cv2.LINE_AA)
    cv2.circle(frame, (handle_x, y), 14, (255, 255, 255), 2, cv2.LINE_AA)

    cv2.putText(
        frame,
        f"{display_cross:.2f}",
        (handle_x - 18, y - 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (250, 250, 250),
        1,
        cv2.LINE_AA,
    )

    # Knobs.
    draw_knob(frame, rain_center, radius, ctrl["rain"], "RAIN", (255, 170, 60))
    draw_knob(frame, wind_center, radius, ctrl["wind"], "WIND", (80, 220, 255))

    # BPM readout.
    cv2.rectangle(frame, (WIDTH - 170, 10), (WIDTH - 10, 52), (20, 20, 24), -1)
    cv2.putText(
        frame,
        f"{int(bpm)} BPM",
        (WIDTH - 160, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (240, 240, 245),
        1,
        cv2.LINE_AA,
    )
    beat_color = (0, 255, 255) if pulse > 0.45 else (90, 90, 100)
    cv2.circle(frame, (WIDTH - 25, 31), 8, beat_color, -1, cv2.LINE_AA)

    # Mode readout.
    mode_prefix = "AUTO DEMO • " if st.session_state.auto_demo else "MANUAL • "
    mode_text = mode_prefix + ctrl.get("mode", "LIVE")
    text_size = cv2.getTextSize(mode_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
    cv2.rectangle(frame, (10, 10), (20 + text_size[0], 52), (20, 20, 24), -1)
    cv2.putText(
        frame,
        mode_text,
        (15, 37),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (240, 240, 245),
        1,
        cv2.LINE_AA,
    )

    if ctrl.get("drop", False):
        cv2.putText(
            frame,
            "WEATHER DROP!",
            (WIDTH // 2 - 120, int(HEIGHT * 0.30)),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )

    if ctrl.get("scratch", False):
        cv2.putText(
            frame,
            "SCRATCH STUTTER",
            (WIDTH // 2 - 120, int(HEIGHT * 0.38)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 220, 255),
            2,
            cv2.LINE_AA,
        )


def draw_hand(frame, x, y, label="HAND"):
    x = int(x)
    y = int(y)

    cv2.circle(frame, (x, y), 22, (0, 255, 255), 2, cv2.LINE_AA)
    cv2.circle(frame, (x, y), 6, (0, 255, 255), -1, cv2.LINE_AA)

    for i in range(5):
        angle = -math.pi / 2.0 + (i - 2) * 0.38
        x1 = int(x + 10 * math.cos(angle))
        y1 = int(y + 10 * math.sin(angle))
        x2 = int(x + 22 * math.cos(angle))
        y2 = int(y + 22 * math.sin(angle))
        cv2.line(frame, (x1, y1), (x2, y2), (0, 255, 255), 2, cv2.LINE_AA)

    cv2.putText(
        frame,
        label,
        (x + 26, y - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (0, 255, 255),
        1,
        cv2.LINE_AA,
    )


def draw_hands(frame, hands):
    if not hands:
        return

    for idx, hand in enumerate(hands):
        x, y = hand
        label = "HAND L" if idx == 0 else "HAND R"
        draw_hand(frame, x, y, label)


# ------------------------------------------------------------
# Main frame renderer
# ------------------------------------------------------------

def render_frame(now, ctrl):
    dt = now - st.session_state.last_frame_time
    if dt <= 0 or dt > 0.25:
        dt = 0.033

    st.session_state.last_frame_time = now
    st.session_state.frame_count += 1

    bpm = st.session_state.bpm
    pulse, _, _ = beat_pulse(now, bpm)

    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)

    draw_background(frame, ctrl["crossfade"], pulse)
    draw_clouds(frame, dt, ctrl["crossfade"], ctrl["wind"])
    draw_birds(frame, now, ctrl["crossfade"])

    scratch_hidden = ctrl.get("scratch", False) and (st.session_state.frame_count % 2 == 0)
    update_rain(
        frame,
        dt,
        ctrl["rain"],
        ctrl["wind"],
        pulse,
        hidden=scratch_hidden,
    )

    draw_lightning(frame, now, ctrl)
    draw_hud(frame, ctrl, bpm, pulse, now)
    draw_hands(frame, ctrl.get("hands", []))

    if pulse > 0:
        flash_frame(frame, pulse * 0.045)

    if ctrl.get("scratch", False) and not scratch_hidden:
        flash_frame(frame, 0.05)

    return frame


# ------------------------------------------------------------
# Audio generation: simple beat track, no external assets
# ------------------------------------------------------------

@st.cache_data(show_spinner=False)
def make_demo_audio(bpm):
    sr = 22050
    seconds = 8
    total = int(sr * seconds)
    audio = np.zeros(total, dtype=np.float32)
    beat = 60.0 / max(1.0, float(bpm))

    def add(start_time, signal):
        i0 = int(start_time * sr)
        i1 = min(total, i0 + len(signal))
        if i0 < total and i1 > i0:
            audio[i0:i1] += signal[: i1 - i0]

    for beat_index in range(int(seconds / beat) + 1):
        beat_time = beat_index * beat

        # Kick.
        n = int(0.25 * sr)
        t = np.arange(n) / sr
        freq = 52 + 40 * np.exp(-t * 25)
        kick = np.sin(2 * np.pi * freq * t) * np.exp(-t * 16) * 0.9
        add(beat_time, kick)

        # Offbeat hat.
        n = int(0.04 * sr)
        hat = np.random.randn(n) * np.exp(-np.arange(n) / (sr * 0.006)) * 0.12
        add(beat_time + beat / 2.0, hat)

        # Bass pulse.
        n = int(0.30 * sr)
        t = np.arange(n) / sr
        bass = np.sin(2 * np.pi * 82 * t) * np.exp(-t * 8) * 0.12
        add(beat_time, bass)

    # Soft ambient noise bed.
    audio += np.random.randn(total) * 0.015

    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.72

    pcm = (audio * 32767).astype(np.int16)
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(pcm.tobytes())

    return buffer.getvalue()


# ------------------------------------------------------------
# Recording
# ------------------------------------------------------------

def open_video_writer(path, fps, size):
    for codec in ("avc1", "mp4v"):
        writer = cv2.VideoWriter(
            path,
            cv2.VideoWriter_fourcc(*codec),
            fps,
            size,
        )
        if writer.isOpened():
            return writer
        writer.release()
    return None


def record_live_take(seconds):
    seconds = int(min(max(seconds, 1), 60))
    filename = f"weather_dj_take_{int(time.time())}.mp4"
    path = os.path.join(tempfile.gettempdir(), filename)

    writer = open_video_writer(path, FPS, (WIDTH, HEIGHT))
    if writer is None:
        st.error("Could not open OpenCV VideoWriter. Try installing ffmpeg-compatible OpenCV.")
        return None

    total_frames = seconds * FPS
    start = time.time()

    progress = st.progress(0.0, text="Recording live take...")
    view = st.empty()

    try:
        for i in range(total_frames):
            target_time = start + i / FPS
            delay = target_time - time.time()
            if delay > 0:
                time.sleep(delay)

            now = time.time()
            ctrl = compute_controls(now, consume_events=False)
            frame = render_frame(now, ctrl)
            writer.write(frame)

            view.image(
                frame,
                channels="BGR",
                use_container_width=True,
                caption=f"Recording {i + 1}/{total_frames}",
            )
            progress.progress((i + 1) / total_frames, text=f"Recording {i + 1}/{total_frames}")

    finally:
        writer.release()

    progress.empty()
    view.empty()
    return path


# ------------------------------------------------------------
# Streamlit UI
# ------------------------------------------------------------

with st.sidebar:
    st.header("Control deck")

    st.checkbox(
        "Auto demo (simulated gesture performance)",
        key="auto_demo",
    )

    st.caption("Disable auto demo to play manually.")

    st.slider(
        "Crossfade: calm → storm",
        min_value=0.0,
        max_value=1.0,
        step=0.01,
        key="manual_crossfade",
        disabled=st.session_state.auto_demo,
    )

    st.slider(
        "Rain intensity",
        min_value=0.0,
        max_value=1.0,
        step=0.01,
        key="manual_rain",
        disabled=st.session_state.auto_demo,
    )

    st.slider(
        "Wind intensity",
        min_value=0.0,
        max_value=1.0,
        step=0.01,
        key="manual_wind",
        disabled=st.session_state.auto_demo,
    )

    st.slider(
        "BPM",
        min_value=70,
        max_value=170,
        step=1,
        key="bpm",
    )

    col_drop, col_scratch = st.columns(2)
    if col_drop.button("DROP"):
        st.session_state.trigger_drop = True
    if col_scratch.button("SCRATCH"):
        st.session_state.trigger_scratch = True

    st.divider()

    if MEDIAPIPE_AVAILABLE:
        st.checkbox(
            "Enable webcam hand tracking (optional)",
            key="camera_enabled",
        )

        if st.session_state.camera_error:
            st.warning(st.session_state.camera_error)

        if not st.session_state.camera_enabled:
            if st.session_state.get("camera") is not None:
                try:
                    st.session_state.camera.release()
                except Exception:
                    pass
                st.session_state.camera = None

            if st.session_state.get("hand_solver") is not None:
                try:
                    st.session_state.hand_solver.close()
                except Exception:
                    pass
                st.session_state.hand_solver = None
    else:
        st.info("Install mediapipe for webcam hand tracking. The auto demo works without it.")

    st.divider()

    audio_enabled = st.checkbox("Beat audio track", value=False)
    if audio_enabled:
        st.audio(make_demo_audio(int(st.session_state.bpm)), format="audio/wav")

    st.divider()

    st.slider(
        "Live take length in seconds (max 60)",
        min_value=1,
        max_value=60,
        value=10,
        step=1,
        key="record_seconds",
    )

    if st.button("Record live take & download"):
        st.session_state.request_recording = True

    st.caption("Recording captures the current live simulation, including auto demo or manual controls.")


st.title("🎛️ Weather Mixing Booth")
st.caption(
    "A performance-oriented weather DJ booth: crossfader, rain/wind knobs, beat pulse, "
    "scratch stutter, and a beat-synced weather drop. The demo starts automatically."
)


def live_view_inner():
    now = time.time()
    ctrl = compute_controls(now, consume_events=True)

    if st.session_state.get("camera_enabled", False) and MEDIAPIPE_AVAILABLE:
        hands = get_camera_hands()
        if hands:
            ctrl = apply_camera_controls(hands, ctrl, now)

    frame = render_frame(now, ctrl)

    st.image(frame, channels="BGR", use_container_width=True)

    metric_1, metric_2, metric_3 = st.columns(3)

    with metric_1:
        st.progress(clamp(ctrl["crossfade"]))
        st.caption(f"Crossfade {ctrl['crossfade']:.2f}")

    with metric_2:
        st.progress(clamp(ctrl["rain"]))
        st.caption(f"Rain {ctrl['rain']:.2f}")

    with metric_3:
        st.progress(clamp(ctrl["wind"]))
        st.caption(f"Wind {ctrl['wind']:.2f}")


fragment_decorator = getattr(st, "fragment", None)
if fragment_decorator is None:
    fragment_decorator = getattr(st, "experimental_fragment", None)

if fragment_decorator is not None:
    @fragment_decorator(run_every=0.12)
    def live_view():
        live_view_inner()
else:
    def live_view():
        st.markdown(
            '<meta http-equiv="refresh" content="0.35">',
            unsafe_allow_html=True,
        )
        live_view_inner()


live_view()


if st.session_state.get("request_recording"):
    st.session_state.request_recording = False
    record_seconds = int(st.session_state.get("record_seconds", 10))
    with st.spinner("Recording live take..."):
        recorded_path = record_live_take(record_seconds)
    if recorded_path:
        st.session_state.record_path = recorded_path


if st.session_state.get("record_path") and os.path.exists(st.session_state.record_path):
    st.subheader("Latest live take")
    st.video(st.session_state.record_path)

    with open(st.session_state.record_path, "rb") as video_file:
        st.download_button(
            "Download MP4",
            data=video_file.read(),
            file_name=os.path.basename(st.session_state.record_path),
            mime="video/mp4",
        )

    if st.button("Clear recording"):
        st.session_state.record_path = None
        st.rerun()


with st.expander("2-minute live demo script"):
    st.markdown(
        """
        **0:00–0:15** — Start with the DJ mixer overlay visible and the crossfader at the calm end.

        **0:15–0:40** — Slide across the crossfader zone to blend from calm weather into storm weather.

        **0:40–1:05** — Move to the rain knob zone and raise/lower the hand to change rain particle density.

        **1:05–1:30** — Raise both hands to trigger the weather drop: beat-synced lightning, thunder, and maximum storm intensity.

        **1:30–1:50** — Scratch back and forth in the crossfader zone to trigger the stutter/strobe effect.

        **1:50–2:00** — Mention recording mode and position the piece as a hybrid CV + audio-reactive interactive performance.
        """
    )


with st.expander("How the mapping works"):
    st.markdown(
        """
        - **Crossfader zone:** horizontal position blends from calm preset to storm preset.
        - **Rain knob zone:** vertical position controls rain intensity.
        - **Wind knob zone:** vertical position controls wind intensity.
        - **Beat clock:** BPM-derived pulse drives background brightness and particle spawn rate.
        - **Drop gesture:** both hands raised for 0.5 seconds triggers a beat-synced storm drop.
        - **Scratch gesture:** rapid horizontal movement triggers a 300 ms stutter/strobe effect.
        - **Recording:** live frames are written to MP4 with OpenCV VideoWriter.
        """
    )