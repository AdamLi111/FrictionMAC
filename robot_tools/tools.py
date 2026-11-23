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
  - A straight move also stops itself near an obstacle: the agent sets `stop_within` (default
    0.15 m) for the sensor looking along the path (the angled front sensors use at most 0.06 m),
    and the drive checks every reading against it as it arrives, returning an "obstacle" block.
  - Any move stops at a drop: the ledge sensors on the end it heads toward (all four for a turn)
    end it with a "ledge" block, and a move toward a drop already seen isn't sent.
  - The same movement code drives the real robot and the simulated one (sim/robot.py): both
    stream their sensors into a SensorMonitor, and the simulation's runs on simulated time.
  - Vision returns raw base64 frames; no VLM runs here (the agent reasons over the images).
"""
import contextlib
import functools
import inspect

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
_SETTLE_S = 0.4   # after a move: wait this long, at most, for every range sensor to report
_COAST_S = 0.15   # after an early stop: the robot is still coasting to a halt for about this long
DEFAULT_STOP_WITHIN_M = 0.15
# The two angled front sensors mostly see what is BESIDE the robot's path (a wall it is passing,
# a door frame it will clear), so they stop a move only when something is closer than this:
# closer than the robot could pass by without touching it. Only the sensor looking straight
# along the path uses the agent's `stop_within`.
SIDE_STOP_WITHIN_M = 0.06
_AHEAD = {"forward": "front_center", "backward": "back"}
_BESIDE = {"forward": ("front_left", "front_right"), "backward": ()}
# The ledge sensors on the end a move heads toward; a turn swings every corner.
_LEDGES = {"forward": sensors.FRONT_LEDGES, "backward": sensors.REAR_LEDGES,
           "turn": sensors.LEDGES}


def _sensor_readings() -> dict:
    """The distance sensors as a result field, any corner whose ledge sensor sees a drop, and a
    note when there is no sensor data (so an `ok: true` can't be mistaken for "nothing was
    touched")."""
    monitor = runtime.get_sensors()
    out = {"distances_m": monitor.distances()}
    drops = monitor.drops()
    if drops:
        out["ledges_detected"] = drops
    if not monitor.online:
        out["sensors"] = sensors.OFFLINE
    return out


def _pressed_bumpers() -> list:
    return runtime.get_sensors().pressed()


def _limits(direction: str, stop_within: float) -> dict:
    """{range sensor: the reading that stops a straight drive}: `stop_within` for the sensor
    looking straight along the path, and at most SIDE_STOP_WITHIN_M for the angled ones. 0 turns
    the check off."""
    if stop_within <= 0:
        return {}
    limits = {_AHEAD[direction]: stop_within}
    for sensor in _BESIDE[direction]:
        limits[sensor] = min(stop_within, SIDE_STOP_WITHIN_M)
    return limits


def _held_by_ledge(kind: str):
    """The result for a move toward a drop, or None. While a ledge sensor on the end the robot
    would head toward (any corner, for a turn) sees no floor, the move isn't sent: it would take
    the robot over the edge."""
    drops = [c for c in runtime.get_sensors().drops() if c in _LEDGES[kind]]
    if not drops:
        return None
    which = f"the {' and '.join(drops)} ledge sensor{'s see' if len(drops) > 1 else ' sees'}"
    if kind == "turn":
        what, then = "turn while a corner is over it", "Drive straight away from the drop first."
    else:
        away = "backward" if kind == "forward" else "forward"
        what, then = f"drive {kind} toward it", f"Move {away}, away from it."
    return {"ok": False, "duration_ms": 0,
            "ledge": {"ledges": drops, "after_ms": 0, "already_detected": True},
            "message": f"did not move: {which} a drop (no floor), and the robot won't {what}. "
                       f"{then}",
            **_sensor_readings()}


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


def _already_within(limits: dict):
    """The result for a straight drive its own limits would end at once, or None: a sensor
    facing that way already reads inside its limit, so the move isn't sent (the robot would
    creep forward a little on every try). A reading the sensor doesn't trust, or no recent one,
    doesn't hold the robot; the bumpers still stop it."""
    readings = runtime.get_sensors().distances()
    close = [(readings[s] - limit, s, readings[s], limit) for s, limit in limits.items()
             if isinstance(readings[s], float) and readings[s] < limit]
    if not close:
        return None
    _, sensor, distance, limit = min(close)
    return {"ok": False, "duration_ms": 0,
            "obstacle": {"sensor": sensor, "distance_m": distance, "limit_m": limit,
                         "after_ms": 0, "estimated_traveled_m": 0.0, "already_within": True},
            "message": (f"did not move: the {sensor} distance sensor already reads {distance} m, "
                        f"inside its {limit} m limit. To go closer on purpose, pass a smaller "
                        f"stop_within (0 turns the check off); otherwise turn or back up first."),
            **_sensor_readings()}


def _drive(linear: int, angular: int, ms: int, *, planned_m=None, planned_deg=None,
           limits=None, ledges=()) -> dict:
    """One timed drive, watched by the sensors, on the real robot or the simulated one.

    The wait ends at the first bumper contact, at the first drop under a ledge sensor in
    `ledges`, or at the first trusted range reading inside its limit in `limits` (the agent's
    own stop condition, checked against every reading as it arrives), instead of sleeping out
    the planned time; stop() is then sent. (Misty's hazard system already halts a forward drive
    when a front bumper is pressed or a ledge is seen; stop() is harmless there and covers
    everything else.) Whatever ended the move becomes the result: which ledge sensor, bumper or
    distance sensor, how long into the move, and for a straight drive (`planned_m`) about how far
    the robot got, from the drive calibration run backwards. Without sensor data this is the old
    open-loop wait."""
    monitor = runtime.get_sensors()
    robot = runtime.get_robot()
    if config.is_sim():                   # the simulated robot is told the exact amount
        robot.plan_motion(distance_m=planned_m, degrees=planned_deg)
    t0 = monitor.now()
    robot.drive_time(linear, angular, ms)
    reason = monitor.wait_for_stop(since=t0, timeout=runtime.wait_budget(ms / 1000),
                                   limits=limits, ledges=ledges)
    # The readings a result reports are taken after the robot came to rest: after an early stop,
    # only from once it has coasted to a halt, so `distances_m` is where it ended up rather than
    # where it was when the stop was sent.
    coast = 0.0
    if reason is not None:
        robot.stop()
        coast = _COAST_S
    monitor.wait_fresh(since=monitor.now() + coast,
                       timeout=runtime.wait_budget(coast + _SETTLE_S))
    runtime.save_sim_state()

    def when(t) -> dict:
        after_ms = max(0, int(round((t - t0) * 1000)))
        block = {"after_ms": after_ms}
        if planned_m is not None:
            block["estimated_traveled_m"] = runtime.calc_drive_progress(after_ms, planned_m)
        return block

    result, why = {"ok": True, "duration_ms": ms}, []
    # Also catches a drop or a contact that landed as the move ended.
    drops = [d for d in monitor.drops_since(t0) if d["ledge"] in ledges]
    if drops:
        seen = {d["ledge"] for d in drops}
        corners = [c for c in sensors.LEDGES if c in seen]
        result["ledge"] = {"ledges": corners, **when(drops[0]["t"])}
        why.append(f"the {' and '.join(corners)} ledge sensor{'s' if len(corners) > 1 else ''} "
                   f"saw a drop {result['ledge']['after_ms']} ms into the move")
    contacts = monitor.contacts_since(t0)
    if contacts:
        hit = {c["bumper"] for c in contacts}
        result["bump"] = {"bumpers": [b for b in sensors.BUMPERS if b in hit],
                          **when(contacts[0]["t"])}
        if contacts[0].get("time"):
            result["bump"]["time"] = contacts[0]["time"]
        why.append(f"a bumper was pressed {result['bump']['after_ms']} ms into the move")
    if not why and reason is not None and "obstacle" in reason:
        seen = reason["obstacle"]
        result["obstacle"] = {"sensor": seen["sensor"], "distance_m": seen["distance_m"],
                              "limit_m": seen["limit_m"], **when(seen["t"])}
        why.append(f"the {seen['sensor']} distance sensor read {seen['distance_m']} m "
                   f"{result['obstacle']['after_ms']} ms into the move, inside its "
                   f"{seen['limit_m']} m limit")
    if why:
        result["ok"] = False
        result["message"] = "; ".join(why) + ", so the robot stopped"
    result.update(_sensor_readings())
    return result


def _straight(direction: str, distance: float, stop_within) -> dict:
    ms = runtime.calc_drive_time(distance)
    limits = _limits(direction, max(0.0, float(stop_within or 0.0)))
    with runtime.motor_lock("DRIVE"):
        held = (_held_by_ledge(direction) or _held_by_bumper(direction)
                or _already_within(limits))
        if held:
            return held
        return _drive(50 if direction == "forward" else -50, 0, ms, planned_m=distance,
                      limits=limits, ledges=_LEDGES[direction])


def _turn(angular: int, degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    with runtime.motor_lock("DRIVE"):
        held = _held_by_ledge("turn")
        if held:
            return held
        return _drive(0, angular, ms, planned_deg=degrees, ledges=_LEDGES["turn"])


@_tool()
def move_forward(distance: float = 1.0, stop_within: float = DEFAULT_STOP_WITHIN_M) -> dict:
    return _straight("forward", distance, stop_within)


@_tool()
def move_backward(distance: float = 1.0, stop_within: float = DEFAULT_STOP_WITHIN_M) -> dict:
    return _straight("backward", distance, stop_within)


@_tool()
def turn_left(degrees: float) -> dict:
    return _turn(100, degrees)          # positive angular = counter-clockwise


@_tool()
def turn_right(degrees: float) -> dict:
    return _turn(-100, degrees)


@_tool()
def stop() -> dict:
    # stop() must not wait behind an in-flight drive — it pre-empts, so no DRIVE lock.
    runtime.get_robot().stop()
    return {"ok": True}


# --------------------------------------------------------------------------- sensing
@_tool()
def read_distance_sensors() -> dict:
    """The four range (time-of-flight) sensors, in metres from the robot's body, which bumpers
    are pressed right now, and which corners' ledge sensors see a drop. No motor moves, so no
    lock."""
    monitor = runtime.get_sensors()
    return {"ok": True, **_sensor_readings(), "bumpers_pressed": monitor.pressed(),
            "ledges_detected": monitor.drops()}


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
