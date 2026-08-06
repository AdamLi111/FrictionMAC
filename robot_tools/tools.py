"""
The 14 robot tools (plain callables).

Every tool is wrapped by @_tool: it logs one JSONL line per call ({name, args, result,
timestamp}), redacts image-bearing results, and never raises (errors become
{"ok": False, "error": ...}). server.py registers thin MCP wrappers over these.

Design rules (see UNDERSTANDING.md section 7):
  - Movement wrappers reuse the vendored calibration math and issue robot.drive_time(...)
    directly. They NEVER narrate -- speak() and ask_clarification() are the only talkers.
  - Vision returns raw base64 frames; no VLM runs here (the agent reasons over the images).
"""
import contextlib
import functools
import inspect

from . import runtime


def _tool(redact=None):
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                arg_dict = dict(bound.arguments)
            except TypeError:
                arg_dict = {"args": list(args), "kwargs": dict(kwargs)}

            try:
                result = fn(*args, **kwargs)
            except Exception as e:  # tools never raise; surface a shaped error instead
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                runtime.get_logger().log(fn.__name__, arg_dict, result)
                return result

            logged = redact(result) if redact else result
            runtime.get_logger().log(fn.__name__, arg_dict, logged)
            return result

        return wrapper

    return deco


# --------------------------------------------------------------------------- movement
# All wheel movement holds the DRIVE lock, so two drive/turn/scan calls can never overlap
# on the motors (find_object's 360 scan holds it too). Non-DRIVE tools run concurrently.
@_tool()
def move_forward(distance: float = 1.0) -> dict:
    ms = runtime.calc_drive_time(distance)
    with runtime.motor_lock("DRIVE"):
        runtime.get_robot().drive_time(50, 0, ms)
        runtime.sleep(ms / 1000)
    return {"ok": True, "duration_ms": ms}


@_tool()
def move_backward(distance: float = 1.0) -> dict:
    ms = runtime.calc_drive_time(distance)
    with runtime.motor_lock("DRIVE"):
        runtime.get_robot().drive_time(-50, 0, ms)
        runtime.sleep(ms / 1000)
    return {"ok": True, "duration_ms": ms}


@_tool()
def turn_left(degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    with runtime.motor_lock("DRIVE"):
        runtime.get_robot().drive_time(0, 100, ms)
        runtime.sleep(ms / 1000)
    return {"ok": True, "duration_ms": ms}


@_tool()
def turn_right(degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    with runtime.motor_lock("DRIVE"):
        runtime.get_robot().drive_time(0, -100, ms)
        runtime.sleep(ms / 1000)
    return {"ok": True, "duration_ms": ms}


@_tool()
def stop() -> dict:
    # stop() must not wait behind an in-flight drive — it pre-empts, so no DRIVE lock.
    runtime.get_robot().stop()
    return {"ok": True}


# ------------------------------------------------------------------------- expression
# Emotional expression via distinct motor resources (arms, head, face display, chest LED).
# Each holds its own lock so they can run concurrently with each other and with driving.
# Misty II hardware ranges (degrees), used to clamp so we never command out-of-range:
ARM_MIN, ARM_MAX = -29.0, 90.0          # -29 = straight up, 90 = straight down
HEAD_PITCH_MIN, HEAD_PITCH_MAX = -40.0, 26.0   # negative = up
HEAD_ROLL_MIN, HEAD_ROLL_MAX = -40.0, 40.0
HEAD_YAW_MIN, HEAD_YAW_MAX = -81.0, 81.0        # negative = right


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@_tool()
def move_arm(arm: str = "both", position: float = 0.0, velocity: float = 50.0) -> dict:
    """Move an arm to `position` degrees. Misty II range: **-29 (up) .. 90 (down)** (clamped).
    arm: left|right|both. The arms are independent motors with separate locks, so a left-arm
    move and a right-arm move can run concurrently; two moves of the SAME arm serialize."""
    position = _clamp(position, ARM_MIN, ARM_MAX)
    r = runtime.get_robot()
    arms = ["left", "right"] if arm == "both" else [arm]
    with contextlib.ExitStack() as stack:
        # Acquire each arm's own lock (fixed left→right order avoids deadlock).
        for a in arms:
            stack.enter_context(runtime.motor_lock(f"ARM_{a.upper()}"))
        for a in arms:
            r.move_arm(arm=a, position=position, velocity=velocity, units="degrees")
    return {"ok": True, "arm": arm, "position": position}


@_tool()
def move_head(pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0, velocity: float = 50.0) -> dict:
    """Move the head, degrees (clamped to Misty II ranges): pitch -40..26 (neg=up),
    roll -40..40, yaw -81..81 (neg=right)."""
    pitch = _clamp(pitch, HEAD_PITCH_MIN, HEAD_PITCH_MAX)
    roll = _clamp(roll, HEAD_ROLL_MIN, HEAD_ROLL_MAX)
    yaw = _clamp(yaw, HEAD_YAW_MIN, HEAD_YAW_MAX)
    with runtime.motor_lock("HEAD"):
        runtime.get_robot().move_head(pitch=pitch, roll=roll, yaw=yaw, velocity=velocity, units="degrees")
    return {"ok": True, "pitch": pitch, "roll": roll, "yaw": yaw}


@_tool()
def display_image(image_name: str) -> dict:
    """Show a face image (e.g. Misty's built-in eye images: 'e_Joy.jpg', 'e_Anger.jpg',
    'e_Sadness.jpg', 'e_Surprise.jpg', 'e_DefaultContent.jpg')."""
    with runtime.motor_lock("FACE"):
        runtime.get_robot().display_image(fileName=image_name)
    return {"ok": True, "image_name": image_name}


@_tool()
def change_led(red: int = 0, green: int = 0, blue: int = 0) -> dict:
    """Set the chest LED colour (0-255 each)."""
    with runtime.motor_lock("LED"):
        runtime.get_robot().change_led(red=red, green=green, blue=blue)
    return {"ok": True, "rgb": [red, green, blue]}


@_tool()
def reset_pose(hold_seconds: float = 2.0) -> dict:
    """Hold the current expression for `hold_seconds` (so it's actually seen), then return the
    robot to a neutral resting pose: arms down, head level, default face, LED off. Call this
    after an expression so the robot doesn't stay frozen in the pose."""
    runtime.sleep(max(0.0, hold_seconds))   # let the just-performed expression be seen (no locks held)
    r = runtime.get_robot()
    with contextlib.ExitStack() as stack:   # this touches every expression resource at once
        for res in ("ARM_LEFT", "ARM_RIGHT", "HEAD", "FACE", "LED"):
            stack.enter_context(runtime.motor_lock(res))
        r.move_arm(arm="left", position=ARM_MAX, velocity=50, units="degrees")   # arms down
        r.move_arm(arm="right", position=ARM_MAX, velocity=50, units="degrees")
        r.move_head(pitch=0, roll=0, yaw=0, velocity=50, units="degrees")         # head level
        r.display_image(fileName="e_DefaultContent.jpg")                          # default face
        r.change_led(red=0, green=0, blue=0)                                      # LED off
    return {"ok": True, "reset": True}


# Navigation is no longer a single composite tool. The Director composes primitive
# movements (turn_* + move_forward/backward) in a perceive -> move -> re-perceive loop
# instead. (Removed spatial_navigate; removed strafe_* — a differential-drive base can't
# strafe cleanly, so it's turn + drive.)


# ----------------------------------------------------------------------------- speech
# One speak tool. `friction_type` is REQUIRED and labels every utterance: "none" for a
# normal utterance, or one of the five positive-friction types for a friction turn. The
# label (in the JSONL) is the authoritative record of whether/what friction was applied.
FRICTION_TYPES = ("none", "probing", "assumption_reveal", "overspecification",
                  "reflective_pause", "reinforcement")


@_tool()
def speak(text: str, friction_type: str) -> dict:
    """Say `text` aloud. `friction_type` is required: 'none' for a normal utterance, else one
    of {probing, assumption_reveal, overspecification, reflective_pause, reinforcement}."""
    runtime.get_robot().speak(text)
    return {"ok": True, "friction_type": friction_type}


# ----------------------------------------------------------------------------- vision
def _redact_capture(result):
    if isinstance(result, dict) and "image" in result:
        img = result.get("image") or ""
        return {"frames": 1 if img else 0, "bytes": len(img)}
    return result


@_tool(redact=_redact_capture)
def capture_view() -> dict:
    """One raw base64 JPEG frame for the agent to reason over. No VLM here."""
    frame = runtime.capture_frame("front")
    runtime.set_last_capture([{"direction": "front", "image": frame}])   # cache for get_last_view
    return {"ok": True, "image": frame}


def _redact_find(result):
    frames = result.get("frames") if isinstance(result, dict) else None
    if isinstance(frames, list):
        return {
            "target_object": result.get("target_object"),
            "frames": len(frames),
            "directions": [f.get("direction") for f in frames],
            "bytes": [len(f.get("image") or "") for f in frames],
        }
    return result


@_tool(redact=_redact_find)
def find_object(target_object: str) -> dict:
    """Physical 360 deg scan -> 4 labeled raw frames (front/left/back/right). No VLM:
    the agent judges presence from the images."""
    r = runtime.get_robot()
    directions = ["front", "left", "back", "right"]
    frames = []
    with runtime.motor_lock("DRIVE"):  # the 360 scan turns the wheels — hold DRIVE throughout
        for i, direction in enumerate(directions):
            frames.append({"direction": direction, "image": runtime.capture_frame(direction)})
            if i < 3:
                r.drive_time(0, 100, runtime.calc_turn_time(90))  # turn 90 deg left
                runtime.sleep(2)
        # return to the original heading
        r.drive_time(0, 100, runtime.calc_turn_time(90))
        runtime.sleep(2)
    runtime.set_last_capture(frames)   # cache all 4 views for get_last_view
    return {"ok": True, "target_object": target_object, "frames": frames}


@_tool(redact=_redact_find)
def get_last_view() -> dict:
    """The frames from the MOST RECENT capture (cached), WITHOUT capturing anew — no camera
    trigger, no motor: one frame from a capture_view, or all four from a 360° find_object scan.
    Use to look at everything just seen."""
    return {"ok": True, "frames": runtime.get_last_capture()}


# ----------------------------------------------------------------------- world memory
@_tool()
def get_known_location(object: str):
    """Return the stored info dict for `object`, or None if unknown."""
    return runtime.get_world().get_known_location(object)


@_tool()
def update_world(object: str, info: dict) -> dict:
    """Merge `info` (open schema) into `object`'s record. Non-destructive; atomic; logged."""
    stored = runtime.get_world().update_world(object, info)
    return {"ok": True, "object": object, "stored": stored}


@_tool()
def get_world() -> dict:
    """Return the ENTIRE world model: {object: info, ...}. Use to enumerate everything known
    (e.g. to detect ambiguity, or to propagate a shared property across entries)."""
    return {"ok": True, "world": runtime.get_world().snapshot()}


# Canonical tool set, for server registration and tests.
ALL_TOOLS = [
    # movement (DRIVE)
    move_forward, move_backward, turn_left, turn_right, stop,
    # expression (ARM/HEAD/FACE/LED)
    move_arm, move_head, display_image, change_led, reset_pose,
    # speech
    speak,
    # vision
    capture_view, get_last_view, find_object,
    # world memory
    get_known_location, update_world, get_world,
]
