"""
Standalone MCP server exposing the robot tools (FastMCP).

Run:
    MISTY_IP=192.168.1.50 python -m robot_tools.server      # real mode (fails loudly if down)
    ROBOT_STUB=1 python -m robot_tools.server               # offline stub mode

Concurrency: every tool wrapper is async and offloads the (blocking) robot work to a worker
thread via anyio.to_thread. That keeps the event loop free so independent tool calls run
concurrently; conflicting ones serialize on the per-motor locks in tools.py/runtime.py.
The logging/redaction/never-raise behavior lives in robot_tools.tools.
"""
import base64
import io
import os

import anyio
from mcp.server.fastmcp import FastMCP, Image

from . import runtime, tools

mcp = FastMCP("ponder-robot-tools")


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
@mcp.tool()
async def move_forward(distance: float = 1.0) -> dict:
    """Drive straight forward `distance` meters."""
    return await _off(tools.move_forward, distance)


@mcp.tool()
async def move_backward(distance: float = 1.0) -> dict:
    """Drive straight backward `distance` meters."""
    return await _off(tools.move_backward, distance)


@mcp.tool()
async def turn_left(degrees: float) -> dict:
    """Rotate left (counter-clockwise) `degrees` degrees in place."""
    return await _off(tools.turn_left, degrees)


@mcp.tool()
async def turn_right(degrees: float) -> dict:
    """Rotate right (clockwise) `degrees` degrees in place."""
    return await _off(tools.turn_right, degrees)


@mcp.tool()
async def stop() -> dict:
    """Stop all movement immediately (pre-empts an in-flight drive)."""
    return await _off(tools.stop)


# ---- expression (ARM / HEAD / FACE / LED) ----
@mcp.tool()
async def move_arm(arm: str = "both", position: float = 0.0, velocity: float = 50.0) -> dict:
    """Move an arm to `position` degrees (~ -90 up .. 90 down). arm: left|right|both."""
    return await _off(tools.move_arm, arm, position, velocity)


@mcp.tool()
async def move_head(pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0,
                    velocity: float = 50.0) -> dict:
    """Move the head: pitch (up/down), roll (tilt), yaw (left/right), in degrees."""
    return await _off(tools.move_head, pitch, roll, yaw, velocity)


@mcp.tool()
async def display_image(image_name: str) -> dict:
    """Show a face image (Misty eye images e.g. 'e_Joy.jpg', 'e_Anger.jpg', 'e_Sadness.jpg',
    'e_Surprise.jpg', 'e_DefaultContent.jpg')."""
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
@mcp.tool()
async def speak(text: str, friction_type: str) -> dict:
    """Say `text` aloud. `friction_type` is REQUIRED: 'none' for a normal utterance, or one of
    {probing, assumption_reveal, overspecification, reflective_pause, reinforcement} for a
    positive-friction turn. The label is logged as the record of friction applied."""
    return await _off(tools.speak, text, friction_type)


# ---- vision (raw frames as viewable image content) ----
# The camera's field of view is narrow (~45°): one capture shows only what's roughly straight
# ahead. To see another direction, turn, then capture again. Frames are labeled "directly ahead
# (~45° FOV)" — the robot's current heading — not fixed cardinal views.
@mcp.tool()
async def capture_view():
    """Capture one image of what's directly ahead (~45° FOV) and return it as viewable image
    content."""
    r = await _off(tools.capture_view)
    if not r.get("ok"):
        return f"capture_view failed: {r.get('error')}"
    return ["Directly ahead (~45° FOV):", _frame_content(r.get("image"), "directly ahead")]


@mcp.tool()
async def get_last_view():
    """Return the MOST RECENT captured frame as viewable image content, WITHOUT capturing anew
    (no camera trigger, no motor)."""
    r = await _off(tools.get_last_view)
    if not r.get("ok"):
        return f"get_last_view failed: {r.get('error')}"
    frames = r.get("frames") or []
    if not frames:
        return "No frames have been captured yet."
    out = ["Most recent capture (no new shot):"]
    for fr in frames:
        out.append("Directly ahead (~45° FOV):")
        out.append(_frame_content(fr["image"], "directly ahead"))
    return out


# ---- world memory ----
@mcp.tool()
async def get_known_location(object: str):
    """Look up remembered info about `object`; returns its info dict or null."""
    return await _off(tools.get_known_location, object)


@mcp.tool()
async def update_world(object: str, info: dict) -> dict:
    """Remember `info` (open dict) about `object`. Persisted atomically; merges, never wipes."""
    return await _off(tools.update_world, object, info)


@mcp.tool()
async def get_world() -> dict:
    """Return the ENTIRE world model {object: info, ...} — enumerate everything known."""
    return await _off(tools.get_world)


def main():
    # Construct the robot up front so real mode fails loudly here if it's unreachable.
    runtime.get_robot()
    mcp.run()


if __name__ == "__main__":
    main()
