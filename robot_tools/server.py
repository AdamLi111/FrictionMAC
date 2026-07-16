"""
Standalone MCP server exposing the 14 robot tools (FastMCP).

Run:
    MISTY_IP=192.168.1.50 python -m robot_tools.server      # real mode (fails loudly if down)
    ROBOT_STUB=1 python -m robot_tools.server               # offline stub mode

Each MCP tool is a thin wrapper over robot_tools.tools; the logging/redaction/never-raise
behavior lives there.
"""
import base64

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image

from . import runtime, tools

mcp = FastMCP("ponder-robot-tools")


def _frame_content(b64: str, label: str):
    """Return an MCP Image the agent can visually perceive, or a text note when the frame
    isn't a real JPEG (e.g. the inert stub frame with no scripted scene)."""
    try:
        raw = base64.b64decode(b64) if b64 else b""
    except Exception:
        raw = b""
    if raw[:2] == b"\xff\xd8":  # JPEG magic bytes
        return Image(data=raw, format="jpeg")
    return f"{label}: (stub frame — no real image; set ROBOT_STUB_SCENE to script a scene)"


# ---- movement ----
@mcp.tool()
def move_forward(distance: float = 1.0) -> dict:
    """Drive straight forward `distance` meters."""
    return tools.move_forward(distance)


@mcp.tool()
def move_backward(distance: float = 1.0) -> dict:
    """Drive straight backward `distance` meters."""
    return tools.move_backward(distance)


@mcp.tool()
def strafe_left(distance: float = 1.0) -> dict:
    """Sidestep left `distance` meters (pivot, drive, pivot back)."""
    return tools.strafe_left(distance)


@mcp.tool()
def strafe_right(distance: float = 1.0) -> dict:
    """Sidestep right `distance` meters (pivot, drive, pivot back)."""
    return tools.strafe_right(distance)


@mcp.tool()
def turn_left(degrees: float) -> dict:
    """Rotate left (counter-clockwise) `degrees` degrees in place."""
    return tools.turn_left(degrees)


@mcp.tool()
def turn_right(degrees: float) -> dict:
    """Rotate right (clockwise) `degrees` degrees in place."""
    return tools.turn_right(degrees)


@mcp.tool()
def stop() -> dict:
    """Stop all movement immediately."""
    return tools.stop()


# ---- navigation ----
@mcp.tool()
def spatial_navigate(target_object: str, distance: float = 0.0, turn_degrees: float = 0.0) -> dict:
    """Turn toward `target_object` (negative degrees = left) then drive to it with a
    collision margin. Decide `turn_degrees`/`distance` from a prior capture_view/find_object."""
    return tools.spatial_navigate(target_object, distance, turn_degrees)


# ---- speech (two distinct tools) ----
@mcp.tool()
def speak(text: str) -> dict:
    """Say `text` aloud (normal response)."""
    return tools.speak(text)


@mcp.tool()
def ask_clarification(question: str, friction_type: str = None) -> dict:
    """Ask the user a clarifying/friction question. Distinct from speak() so clarifications
    are countable. `friction_type` is a logged label only (not enforced)."""
    return tools.ask_clarification(question, friction_type)


# ---- vision (raw frames; the agent reasons over them) ----
@mcp.tool()
def capture_view():
    """Capture one image from the camera and return it as viewable image content for you
    (the caller) to reason over. No VLM runs here."""
    r = tools.capture_view()
    if not r.get("ok"):
        return f"capture_view failed: {r.get('error')}"
    return _frame_content(r.get("image"), "front view")


@mcp.tool()
def find_object(target_object: str):
    """Do a physical 360 deg scan and return 4 labeled views (front/left/back/right) as
    viewable image content. No VLM runs -- YOU judge whether `target_object` is present, and
    whether there are multiple plausible candidates (ambiguity), from the images."""
    r = tools.find_object(target_object)
    if not r.get("ok"):
        return f"find_object failed: {r.get('error')}"
    out = [f"360-degree scan for '{target_object}'. Judge presence and count from these "
           f"{len(r['frames'])} views (a candidate may appear in more than one view):"]
    for fr in r["frames"]:
        out.append(f"View: {fr['direction']}")
        out.append(_frame_content(fr["image"], fr["direction"]))
    return out


# ---- world memory ----
@mcp.tool()
def get_known_location(object: str):
    """Look up remembered info about `object`; returns its info dict or null."""
    return tools.get_known_location(object)


@mcp.tool()
def update_world(object: str, info: dict) -> dict:
    """Remember `info` (open dict) about `object`. Persisted atomically; merges, never wipes."""
    return tools.update_world(object, info)


def main():
    # Construct the robot up front so real mode fails loudly here if it's unreachable.
    runtime.get_robot()
    mcp.run()


if __name__ == "__main__":
    main()
