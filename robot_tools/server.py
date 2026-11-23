"""
Standalone MCP server exposing the robot tools (FastMCP).

Run:
    MISTY_IP=192.168.1.50 python -m robot_tools.server      # real mode (fails loudly if down)
    ROBOT_STUB=1 python -m robot_tools.server               # offline stub mode

Concurrency: every tool wrapper is async and offloads the (blocking) robot work to a worker
thread via anyio.to_thread. That keeps the event loop free so independent tool calls run
concurrently; conflicting ones serialize on the per-motor locks in tools.py/runtime.py.
The logging/redaction/never-raise behavior lives in robot_tools.tools.

THE TOOL DESCRIPTIONS HERE ARE THE AGENT-FACING CONTRACT. An agent that holds a tool gets this
module's description + the signature below (rendered to JSON Schema) resident in its context on
every inference; it never sees tools.py. So anything an agent must know to call a tool correctly
-- ranges, units, sign conventions, valid values, what comes back -- belongs HERE and nowhere
else. Numbers and value sets are interpolated from the constants in tools.py rather than retyped,
so a limit can never drift from the code that enforces it, and no steering file repeats them.
"""
import base64
import io
import os
from typing import Literal, get_args

import anyio
from mcp.server.fastmcp import FastMCP, Image

from . import runtime, sensors, tools

mcp = FastMCP("ponder-robot-tools")

#: Closed value sets, mirrored into the JSON Schema as enums so an invalid value is rejected at
#: the boundary instead of reaching the robot. Asserted against tools.py at import.
FrictionType = Literal["none", "probing", "assumption_reveal", "overspecification",
                       "reflective_pause", "reinforcement"]
Arm = Literal["left", "right", "both"]
assert set(get_args(FrictionType)) == set(tools.FRICTION_TYPES), \
    "FrictionType enum is out of sync with tools.FRICTION_TYPES"


async def _off(fn, *args):
    """Run a blocking tool function in a worker thread (frees the event loop)."""
    return await anyio.to_thread.run_sync(lambda: fn(*args))


#: Default longest-edge cap (px) for frames sent to the VLM. Downscaling is ON by default to
#: bound per-frame image tokens; override with IMAGE_MAX_DIM (set 0 to disable resizing).
DEFAULT_IMAGE_MAX_DIM = 1024


def _downscale_jpeg(raw: bytes) -> bytes:
    """Cap the resolution of a frame BEFORE it goes to the VLM: shrink the JPEG so its longest
    edge is at most IMAGE_MAX_DIM pixels (default 1024; aspect ratio preserved, never upscaled),
    bounding per-frame image tokens. No-op when the cap is <= 0 (IMAGE_MAX_DIM=0 disables it),
    when the frame is already within the cap, or on any failure (perception must never break)."""
    try:
        max_dim = int(os.environ.get("IMAGE_MAX_DIM", str(DEFAULT_IMAGE_MAX_DIM)))
    except ValueError:
        max_dim = DEFAULT_IMAGE_MAX_DIM
    if max_dim <= 0:
        return raw
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(raw))
        if max(im.size) <= max_dim:
            return raw  # already within the cap
        im.thumbnail((max_dim, max_dim))  # shrink-only, keeps aspect ratio
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return raw  # on any error, send the original frame rather than fail perception


def _frame_content(b64: str, label: str):
    """Return an MCP Image the agent can visually perceive, or a text note when the frame
    isn't a real JPEG (e.g. the inert stub frame with no scripted scene)."""
    try:
        raw = base64.b64decode(b64) if b64 else b""
    except Exception:
        raw = b""
    if raw[:2] == b"\xff\xd8":  # JPEG magic bytes
        return Image(data=_downscale_jpeg(raw), format="jpeg")
    return f"{label}: (stub frame — no real image; set ROBOT_STUB_SCENE to script a scene)"


# ---- movement (DRIVE) ----
# Drive/turn calls hold one shared DRIVE lock, so two of them never overlap; a call on a
# different motor (arm/head/face/LED) runs concurrently. Every movement result also carries the
# distance sensors' readings after the move, and a "ledge", "bump" or "obstacle" block if one
# stopped it.
@mcp.tool()
async def move_forward(distance: float = 1.0,
                       stop_within: float = tools.DEFAULT_STOP_WITHIN_M) -> dict:
    """Drive straight forward `distance` meters, stopping early if the front-centre distance
    sensor sees something closer than `stop_within` meters (default 0.15; 0 turns this off, for
    when you mean to touch what's ahead). The two angled front sensors mostly see what is beside
    your path, so they stop the robot only when something is within 0.06 m of them.

    Returns {"ok": true, "duration_ms": N, "distances_m": {...}} when the move completed;
    `distances_m` is the distance-sensor reading taken right after the move (the same thing
    read_distance_sensors returns).

    `ok: false` means the move did not complete as asked, and which key is present says why:
      - "ledge": a ledge sensor under a front corner saw a drop (no floor: stairs, a table
        edge), so the robot stopped before going over it. It names the corner(s) (`ledges`),
        `after_ms` and `estimated_traveled_m`. Never drive toward a drop; back away from it.
        With `already_detected: true` the robot did not move at all, because a drop was already
        under a front corner.
      - "obstacle": a distance sensor read inside its limit, so the robot stopped early. It
        names the `sensor`, its reading (`distance_m`), the `limit_m` it crossed, `after_ms`
        (how long into the move) and `estimated_traveled_m` (about how far the robot got,
        estimated from the drive's timing). The sensors report a few times a second, so the
        robot may coast a few centimetres closer than that reading before it stops:
        `distances_m` says where it ended up. With `already_within: true` the robot did not
        move at all, because something was already that close.
      - "bump": a bumper was pressed, so the robot stopped early. It names the bumper(s) and
        gives `after_ms` and `estimated_traveled_m` as above. With `already_pressed: true` the
        robot did not move at all: a bumper on that end was already pressed against something,
        and it won't drive into it -- back up or turn first.
      - "error": the command could not be issued at all (e.g. the robot became unreachable);
        whether it moved is unknown.

    `ok: true` means nothing stopped the move. It does NOT mean "I arrived": confirm progress by
    looking (capture_view). If a "sensors" key says offline, there was no sensor data, so
    `ok: true` then says nothing about whether anything was near or touched. A
    `ledges_detected` key lists any corner that sees a drop right now."""
    return await _off(tools.move_forward, distance, stop_within)


@mcp.tool()
async def move_backward(distance: float = 1.0,
                        stop_within: float = tools.DEFAULT_STOP_WITHIN_M) -> dict:
    """Drive straight backward `distance` meters, stopping early if the `back` distance sensor
    sees something closer than `stop_within` meters (default 0.15; 0 turns this off), or a ledge
    sensor under a rear corner sees a drop. Same return shape as move_forward. The rear bumpers,
    the rear ledge sensors and the `back` distance sensor cover this direction; the
    forward-facing camera never sees behind the robot."""
    return await _off(tools.move_backward, distance, stop_within)


@mcp.tool()
async def turn_left(degrees: float) -> dict:
    """Rotate left (counter-clockwise) `degrees` degrees in place. Returns {"ok": true,
    "duration_ms": N, "distances_m": {...}}: the distance readings at the new heading. A bumper
    pressed mid-turn, or a drop under any corner, stops it early (ok: false with a "bump" or
    "ledge" block, as for move_forward); while a corner is over a drop the robot won't turn at
    all -- drive straight away from the drop first."""
    return await _off(tools.turn_left, degrees)


@mcp.tool()
async def turn_right(degrees: float) -> dict:
    """Rotate right (clockwise) `degrees` degrees in place. Same return shape as turn_left."""
    return await _off(tools.turn_right, degrees)


@mcp.tool()
async def stop() -> dict:
    """Stop all movement immediately (pre-empts an in-flight drive)."""
    return await _off(tools.stop)


# ---- sensing (no motor) ----
# The sensor names, range and the two sentinel values are interpolated from sensors.py, the
# code that produces them, so no steering file needs to repeat them.
@mcp.tool(description=(
    f"Read the robot's distance sensors, without moving: {', '.join(sensors.RANGE_SENSORS)}. "
    f"Three sit on the front of the base and one on the back. Each value is the distance in "
    f"metres from the robot's body to the nearest surface inside that sensor's narrow cone: one "
    f"number per sensor and no angle -- the direction is which sensor it is. They measure out "
    f"to about {sensors.TOF_MAX_RANGE_M:g} m, at the height of the base. "
    f"\"{sensors.NO_READING}\" means the sensor measured nothing reliable, usually because "
    f"nothing is within range (likely open space, but not proof of it); \"{sensors.UNKNOWN}\" "
    f"means no recent data from that sensor. Also returns `bumpers_pressed`: which of "
    f"{', '.join(sensors.BUMPERS)} are pressed against something right now; and "
    f"`ledges_detected`: which corners' downward ledge sensors see a drop (no floor), where the "
    f"robot must not drive."))
async def read_distance_sensors() -> dict:
    return await _off(tools.read_distance_sensors)


# ---- expression (ARM_LEFT / ARM_RIGHT / HEAD / FACE / LED) ----
# The three descriptions below are generated from the clamp constants and name lists in tools.py
# -- the same values the implementation enforces -- so what an agent is told is the real limit by
# construction. Do not retype these numbers here or in any steering file.
@mcp.tool(description=(
    f"Move an arm to `position` degrees. Valid range {tools.ARM_MIN:g} (straight UP) .. "
    f"{tools.ARM_MAX:g} (straight DOWN); values outside are silently CLAMPED into that range, "
    f"and the `position` in the result is the value actually applied -- read it back rather than "
    f"assuming you got what you asked for. `arm`: left, right, or both. The two arms are "
    f"independent motors, so they may hold different positions and move at the same time."))
async def move_arm(arm: Arm = "both", position: float = 0.0, velocity: float = 50.0) -> dict:
    return await _off(tools.move_arm, arm, position, velocity)


@mcp.tool(description=(
    f"Move the head, in degrees. Values outside the ranges below are silently CLAMPED, and the "
    f"result carries the values actually applied. "
    f"`pitch` {tools.HEAD_PITCH_MIN:g}..{tools.HEAD_PITCH_MAX:g} (NEGATIVE = up); "
    f"`roll` {tools.HEAD_ROLL_MIN:g}..{tools.HEAD_ROLL_MAX:g} (tilt); "
    f"`yaw` {tools.HEAD_YAW_MIN:g}..{tools.HEAD_YAW_MAX:g} (NEGATIVE = right)."))
async def move_head(pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0,
                    velocity: float = 50.0) -> dict:
    return await _off(tools.move_head, pitch, roll, yaw, velocity)


@mcp.tool(description=(
    f"Show a face image. Names are CASE-SENSITIVE and must already exist on the robot -- an "
    f"unknown name fails silently (the face simply doesn't change), so prefer a confirmed one. "
    f"Confirmed present on this robot: {', '.join(tools.CONFIRMED_FACE_IMAGES)}. "
    f"Stock Misty defaults (expected, but not verified on this robot): "
    f"{', '.join(tools.STANDARD_FACE_IMAGES)}."))
async def display_image(image_name: str) -> dict:
    return await _off(tools.display_image, image_name)


@mcp.tool()
async def change_led(red: int = 0, green: int = 0, blue: int = 0) -> dict:
    """Set the chest LED colour (0-255 each)."""
    return await _off(tools.change_led, red, green, blue)


@mcp.tool()
async def reset_pose(hold_seconds: float = 2.0) -> dict:
    """Hold the current expression for `hold_seconds`, then return the robot to neutral (arms
    down, head level, default face, LED off). Call after an expression so it doesn't stay stuck."""
    return await _off(tools.reset_pose, hold_seconds)


# ---- speech (one tool; friction_type required) ----
# This is the ONLY way the robot talks. `friction_type` is a required enum (see FrictionType);
# the label written to the JSONL is the authoritative record of what friction was applied, so it
# must describe the utterance honestly. What each type MEANS is a research construct and lives in
# the dialogue agents' steering, not here.
@mcp.tool(description=(
    f"Say `text` aloud -- the only tool that produces speech. `friction_type` is REQUIRED and "
    f"must be exactly one of: {', '.join(tools.FRICTION_TYPES)}. Use 'none' for an ordinary "
    f"utterance; use one of the other five only when the utterance really is that kind of "
    f"positive-friction turn. The label is logged as the authoritative record of the friction "
    f"applied, so label honestly rather than conveniently."))
async def speak(text: str, friction_type: FrictionType) -> dict:
    return await _off(tools.speak, text, friction_type)


# ---- vision (viewable image content; synthetic text POV in sim) ----
# No VLM runs here: these hand the agent something to look at and it does the perceiving. Frames
# are always "directly ahead" at the robot's CURRENT heading -- never fixed cardinal views. The
# narrow-FOV consequence is stated in the descriptions below because agents need it at call time.
@mcp.tool()
async def capture_view():
    """Look at what is directly ahead. The camera's field of view is NARROW (~45 degrees): one
    capture shows only what is roughly straight in front of the robot, so to see another
    direction you must turn first and capture again. Returns viewable image content you are
    expected to actually reason over -- except in simulation, where it returns a synthetic TEXT
    description of the view instead of an image."""
    r = await _off(tools.capture_view)
    if not r.get("ok"):
        return f"capture_view failed: {r.get('error')}"
    if r.get("pov") is not None:            # sim mode: synthetic text POV, no image
        return r["pov"]
    return ["Directly ahead (~45° FOV):", _frame_content(r.get("image"), "directly ahead")]


@mcp.tool()
async def get_last_view():
    """Re-serve the MOST RECENT capture_view result WITHOUT capturing anew -- no camera trigger,
    no motor, so the robot does not move and nothing new is seen. Use it to look again at what
    was just captured. Same content type as capture_view (image, or synthetic text in
    simulation). Returns a note instead if nothing has been captured yet."""
    r = await _off(tools.get_last_view)
    if not r.get("ok"):
        return f"get_last_view failed: {r.get('error')}"
    frames = r.get("frames") or []
    if not frames:
        return "No frames have been captured yet."
    out = ["Most recent capture (no new shot):"]
    for fr in frames:
        if fr.get("pov") is not None:      # sim mode: synthetic text POV
            out.append(fr["pov"])
            continue
        out.append("Directly ahead (~45° FOV):")
        out.append(_frame_content(fr["image"], "directly ahead"))
    return out


# ---- world memory (the agents' BELIEF store) ----
# This is remembered belief, not ground truth, and not the simulator's world: it contains only
# what an agent chose to write. Keys are shared across every agent and every session, which is
# why the naming rule below is part of the contract rather than one agent's private convention.
@mcp.tool()
async def get_known_location(object: str):
    """Look up what is remembered about a single `object`. Returns its info dict, or null if
    nothing has ever been recorded under that exact key. Null means "not recorded", NOT "not
    there" -- it is not evidence about the physical world."""
    return await _off(tools.get_known_location, object)


@mcp.tool()
async def update_world(object: str, info: dict) -> dict:
    """Remember `info` about `object`, merged into any existing record (never wipes; persisted
    atomically). `object` is a shared key: use the common name, LOWERCASE and SINGULAR (e.g.
    "mug", "door"), and reuse the exact same key every time -- a variant spelling silently
    creates a second entry for the same thing. `info` is an open dict; check the existing record
    first so you extend it rather than fragment it."""
    return await _off(tools.update_world, object, info)


@mcp.tool()
async def get_world() -> dict:
    """Return the ENTIRE belief store as {object: info, ...}. Use it to enumerate everything
    known -- e.g. to find the existing key for something before writing, or to count how many
    recorded objects plausibly match a category the user named."""
    return await _off(tools.get_world)


def main():
    # Construct the robot up front so real mode fails loudly here if it's unreachable, and start
    # the sensor stream now so the first drive is already watched for bumps (a stream that can't
    # connect is not fatal: movement results then say the sensors are offline).
    runtime.get_robot()
    runtime.get_sensors()
    mcp.run()


if __name__ == "__main__":
    main()
