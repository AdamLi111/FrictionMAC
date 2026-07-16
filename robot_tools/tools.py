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
@_tool()
def move_forward(distance: float = 1.0) -> dict:
    ms = runtime.calc_drive_time(distance)
    runtime.get_robot().drive_time(50, 0, ms)
    return {"ok": True, "duration_ms": ms}


@_tool()
def move_backward(distance: float = 1.0) -> dict:
    ms = runtime.calc_drive_time(distance)
    runtime.get_robot().drive_time(-50, 0, ms)
    return {"ok": True, "duration_ms": ms}


@_tool()
def strafe_left(distance: float = 1.0) -> dict:
    r = runtime.get_robot()
    r.drive_time(0, -100, 2150)            # pivot ~45 deg left
    runtime.sleep(2.5)
    ms = runtime.calc_drive_time(distance)
    r.drive_time(50, 0, ms)                # drive forward
    runtime.sleep(ms / 1000 + 0.5)
    r.drive_time(0, 100, 2150)             # pivot back
    return {"ok": True, "duration_ms": ms}


@_tool()
def strafe_right(distance: float = 1.0) -> dict:
    r = runtime.get_robot()
    r.drive_time(0, 100, 2150)             # pivot ~45 deg right
    runtime.sleep(2.5)
    ms = runtime.calc_drive_time(distance)
    r.drive_time(50, 0, ms)                # drive forward
    runtime.sleep(ms / 1000 + 0.5)
    r.drive_time(0, -100, 2150)            # pivot back
    return {"ok": True, "duration_ms": ms}


@_tool()
def turn_left(degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    runtime.get_robot().drive_time(0, 100, ms)
    return {"ok": True, "duration_ms": ms}


@_tool()
def turn_right(degrees: float) -> dict:
    ms = runtime.calc_turn_time(degrees)
    runtime.get_robot().drive_time(0, -100, ms)
    return {"ok": True, "duration_ms": ms}


@_tool()
def stop() -> dict:
    runtime.get_robot().stop()
    return {"ok": True}


# ------------------------------------------------------------------------- navigation
@_tool()
def spatial_navigate(target_object: str, distance: float = 0.0, turn_degrees: float = 0.0) -> dict:
    """Composite: turn (sign = direction) then drive with a collision margin
    (safe = max(0.3, distance - 0.5)). Kept as one tool because it bundles that margin."""
    r = runtime.get_robot()
    turn_ms = 0
    if turn_degrees:
        turn_ms = runtime.calc_turn_time(abs(turn_degrees))
        if turn_degrees < 0:
            r.drive_time(0, 100, turn_ms)   # left
        else:
            r.drive_time(0, -100, turn_ms)  # right
        runtime.sleep(turn_ms / 1000 + 0.5)

    drive_ms = 0
    if distance and distance > 0:
        safe = max(0.3, distance - 0.5)
        drive_ms = runtime.calc_drive_time(safe)
        r.drive_time(50, 0, drive_ms)
        runtime.sleep(drive_ms / 1000 + 0.5)

    return {"ok": True, "target_object": target_object, "duration_ms": turn_ms + drive_ms}


# ----------------------------------------------------------------------------- speech
@_tool()
def speak(text: str) -> dict:
    """Normal spoken response."""
    runtime.get_robot().speak(text)
    return {"ok": True}


@_tool()
def ask_clarification(question: str, friction_type: str = None) -> dict:
    """A clarification/friction turn -- a DISTINCT tool from speak() so clarifications are
    countable and gate-able later. friction_type is a logged label only, not enforced."""
    runtime.get_robot().speak(question)
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
    return {"ok": True, "image": runtime.capture_frame("front")}


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
    for i, direction in enumerate(directions):
        frames.append({"direction": direction, "image": runtime.capture_frame(direction)})
        if i < 3:
            r.drive_time(0, 100, runtime.calc_turn_time(90))  # turn 90 deg left
            runtime.sleep(2)
    # return to the original heading
    r.drive_time(0, 100, runtime.calc_turn_time(90))
    runtime.sleep(2)
    return {"ok": True, "target_object": target_object, "frames": frames}


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


# Canonical tool set, for server registration and tests.
ALL_TOOLS = [
    move_forward, move_backward, strafe_left, strafe_right,
    turn_left, turn_right, stop, spatial_navigate,
    speak, ask_clarification,
    capture_view, find_object,
    get_known_location, update_world,
]
