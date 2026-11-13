"""
The 17 robot tools (plain callables).

Every tool is wrapped by @_tool: it logs one JSONL line per call ({name, args, result,
timestamp}), redacts image-bearing results, and never raises (errors become
{"ok": False, "error": ...}). server.py registers thin MCP wrappers over these.

Design rules (see UNDERSTANDING.md section 7):
  - Movement wrappers reuse the vendored calibration math and issue robot.drive_time(...)
    directly. They NEVER narrate -- speak() and ask_clarification() are the only talkers.
  - Every movement result reports the distance + bump sensors (sensors.py): the distances right
    after the move, and a "bump" block if a bumper was pressed during it.
  - Vision returns raw base64 frames; no VLM runs here (the agent reasons over the images).
"""
import contextlib
import functools
import inspect
from datetime import datetime, timezone

from . import config, runtime, sensors


def _sim_truth(tool_name: str) -> dict:
    """Ground truth to record NEXT TO a call, for the evaluator — never returned to the agent.

    In sim mode the world knows things the log otherwise loses: where the robot was standing
    when the call happened (without which a belief like "10° to the left" cannot be checked
    against reality), and, for a capture, which objects were actually in view (without which
    "did it record what it saw?" is unanswerable). Empty outside sim mode."""
    if not config.is_sim():
        return {}
    world = runtime.get_sim_world()
    if world is None:
        return {}
    truth = {"pose": [round(world.robot_pos[0], 3), round(world.robot_pos[1], 3),
                      round(world.heading, 1)]}
    if tool_name == "capture_view":
        truth["visible"] = [{"name": v["name"], "bearing": round(v["bearing"], 1),
                             "distance": round(v["distance"], 2), "room": v["room"]}
                            for v in world.visible_objects()]
    return truth


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
            # `_sim_truth` runs AFTER the tool, so a movement's pose is where it ended up and a
            # capture's `visible` is what the camera actually had.
            runtime.get_logger().log(fn.__name__, arg_dict, logged, **_sim_truth(fn.__name__))
            return result

        return wrapper

    return deco


# --------------------------------------------------------------------------- movement
# All wheel movement holds the DRIVE lock, so two drive/turn calls can never overlap on the
# motors. Non-DRIVE tools run concurrently.
_SETTLE_S = 0.4   # after a real move: wait this long, at most, for every range sensor to report


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sensor_readings() -> dict:
    """The distance sensors as a result field, plus a note when there is no sensor data (so an
    `ok: true` can't be mistaken for "nothing was touched")."""
    if config.is_sim():
        return {"distances_m": runtime.get_sim_world().range_readings()}
    monitor = runtime.get_sensors()
    out = {"distances_m": monitor.distances()}
    if not monitor.online:
        out["sensors"] = sensors.OFFLINE
    return out


def _pressed_bumpers() -> list:
    if config.is_sim():
        return runtime.get_sim_world().bumpers_pressed()
    return runtime.get_sensors().pressed()


def _held_by_bumper(direction: str):
    """The result for a straight drive that can't happen, or None. While a bumper on the end the
    robot would drive toward is pressed, Misty's hazard system ignores the drive (it would push
    into whatever the bumper touches), so it is reported instead of being sent and waited out."""
    end = sensors.FRONT_BUMPERS if direction == "forward" else sensors.REAR_BUMPERS
    pressed = [b for b in _pressed_bumpers() if b in end]
    if not pressed:
        return None
    which = (f"the {pressed[0]} bumper is" if len(pressed) == 1
             else f"the {' and '.join(pressed)} bumpers are")
    return {"ok": False, "duration_ms": 0,
            "bump": {"bumpers": pressed, "after_ms": 0, "estimated_traveled_m": 0.0,
                     "already_pressed": True},
            "message": (f"did not move: {which} pressed against something, and the robot won't "
                        f"drive {direction} into it. Back up or turn first."),
            **_sensor_readings()}


def _drive_sim(distance: float, ms: int) -> dict:
    """Apply a straight move to the sim world (exact distance, not reconstructed from ms) and
    surface any collision, with the bump the real robot's bumpers would have reported.
    `distance` may be negative for a backward move."""
    world = runtime.get_sim_world()
    col = world.move(distance)
    runtime.save_sim_state()
    runtime.sleep(ms / 1000)
    if col["collision"]:
        moved = col["moved"]
        return {"ok": False, "collision": col["collision"], "message": col["message"],
                "duration_ms": ms,
                "bump": {"bumpers": col["bumpers"], "after_ms": runtime.calc_drive_time(moved),
                         "estimated_traveled_m": round(moved, 2), "time": _now_iso()},
                **_sensor_readings()}
    return {"ok": True, "duration_ms": ms, **_sensor_readings()}


def _drive_real(linear: int, angular: int, ms: int, planned_m=None) -> dict:
    """One timed drive on the robot, watched by the bump sensors. Misty's hazard system already
    halts a forward drive when a front bumper is pressed; stop() is sent on any contact too (it
    is harmless, and covers reversing and turning), the wait ends at the contact instead of
    sleeping out the planned time, and the contact becomes the result: which bumper, how long
    into the move, and for a straight drive (`planned_m`) about how far the robot got, from the
    drive calibration run backwards. Without sensor data this is the old open-loop wait."""
    monitor = runtime.get_sensors()
    robot = runtime.get_robot()
    t0 = monitor.now()
    robot.drive_time(linear, angular, ms)
    if monitor.wait_for_contact(since=t0, timeout=runtime.wait_budget(ms / 1000)):
        robot.stop()
    monitor.wait_fresh(since=monitor.now(), timeout=runtime.wait_budget(_SETTLE_S))
    contacts = monitor.contacts_since(t0)     # also catches one that landed as the move ended
    if not contacts:
        return {"ok": True, "duration_ms": ms, **_sensor_readings()}
    after_ms = max(0, int(round((contacts[0]["t"] - t0) * 1000)))
    hit = {c["bumper"] for c in contacts}
    bump = {"bumpers": [b for b in sensors.BUMPERS if b in hit], "after_ms": after_ms}
    if planned_m is not None:
        bump["estimated_traveled_m"] = runtime.calc_drive_progress(after_ms, planned_m)
    if contacts[0].get("time"):
        bump["time"] = contacts[0]["time"]
    return {"ok": False, "duration_ms": ms, "bump": bump,
            "message": f"a bumper was pressed {after_ms} ms into the move, so the robot stopped",
            **_sensor_readings()}


@_tool()
def move_forward(distance: float = 1.0) -> dict:
    ms = runtime.calc_drive_time(distance)
    with runtime.motor_lock("DRIVE"):
        held = _held_by_bumper("forward")
        if held:
            return held
        if config.is_sim():
            return _drive_sim(distance, ms)
        return _drive_real(50, 0, ms, planned_m=distance)


@_tool()
def move_backward(distance: float = 1.0) -> dict:
    ms = runtime.calc_drive_time(distance)
    with runtime.motor_lock("DRIVE"):
        held = _held_by_bumper("backward")
        if held:
            return held
        if config.is_sim():
            return _drive_sim(-distance, ms)
        return _drive_real(-50, 0, ms, planned_m=distance)


@_tool()
def turn_left(degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    with runtime.motor_lock("DRIVE"):
        if config.is_sim():
            runtime.get_sim_world().turn(-degrees)   # left = counter-clockwise = -θ
            runtime.save_sim_state()
            runtime.sleep(ms / 1000)
            return {"ok": True, "duration_ms": ms, **_sensor_readings()}
        return _drive_real(0, 100, ms)


@_tool()
def turn_right(degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    with runtime.motor_lock("DRIVE"):
        if config.is_sim():
            runtime.get_sim_world().turn(degrees)    # right = clockwise = +θ
            runtime.save_sim_state()
            runtime.sleep(ms / 1000)
            return {"ok": True, "duration_ms": ms, **_sensor_readings()}
        return _drive_real(0, -100, ms)


@_tool()
def stop() -> dict:
    # stop() must not wait behind an in-flight drive — it pre-empts, so no DRIVE lock.
    runtime.get_robot().stop()
    return {"ok": True}


# --------------------------------------------------------------------------- sensing
@_tool()
def read_distance_sensors() -> dict:
    """The four range (time-of-flight) sensors, in metres from the robot's body, and which
    bumpers are pressed right now. No motor moves, so no lock."""
    return {"ok": True, **_sensor_readings(), "bumpers_pressed": _pressed_bumpers()}


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


# Face images. CONFIRMED_* are verified present on this deployment's robot; STANDARD_* are the
# stock Misty defaults that should exist but are not verified here. Names are case-sensitive and
# must exist on the robot or the face silently won't change. Single source of truth: server.py
# builds display_image's tool description from these, and no steering file repeats them.
CONFIRMED_FACE_IMAGES = ("e_Amazement.jpg", "e_Surprise.jpg", "e_SleepingZZZ.jpg")
STANDARD_FACE_IMAGES = ("e_Joy.jpg", "e_Love.jpg", "e_Sadness.jpg", "e_Anger.jpg", "e_Fear.jpg",
                        "e_DefaultContent.jpg", "e_Disgust.jpg", "e_Rage.jpg")


@_tool()
def display_image(image_name: str) -> dict:
    """Show a face image. See CONFIRMED_FACE_IMAGES / STANDARD_FACE_IMAGES above."""
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
    if isinstance(result, dict) and "pov" in result:
        return {"pov_chars": len(result.get("pov") or "")}
    if isinstance(result, dict) and "image" in result:
        img = result.get("image") or ""
        return {"frames": 1 if img else 0, "bytes": len(img)}
    return result


@_tool(redact=_redact_capture)
def capture_view() -> dict:
    """One view of what's directly ahead (~45° FOV) for the agent to reason over. Real/stub mode
    returns a raw base64 JPEG; sim mode returns a synthetic text POV (no image). No VLM here."""
    if config.is_sim():
        pov = runtime.get_sim_world().describe_view()
        runtime.set_last_capture([{"direction": "ahead", "pov": pov}])
        return {"ok": True, "pov": pov}
    frame = runtime.capture_frame("front")
    runtime.set_last_capture([{"direction": "ahead", "image": frame}])   # cache for get_last_view
    return {"ok": True, "image": frame}


def _redact_frames(result):
    frames = result.get("frames") if isinstance(result, dict) else None
    if isinstance(frames, list):
        return {
            "frames": len(frames),
            "directions": [f.get("direction") for f in frames],
            "bytes": [len(f.get("image") or f.get("pov") or "") for f in frames],
        }
    return result


@_tool(redact=_redact_frames)
def get_last_view() -> dict:
    """The frame(s) from the MOST RECENT capture (cached), WITHOUT capturing anew — no camera
    trigger, no motor. Use to look again at what was just seen."""
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
    # sensing (no motor)
    read_distance_sensors,
    # expression (ARM/HEAD/FACE/LED)
    move_arm, move_head, display_image, change_led, reset_pose,
    # speech
    speak,
    # vision
    capture_view, get_last_view,
    # world memory
    get_known_location, update_world, get_world,
]
