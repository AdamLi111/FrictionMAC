"""
Agent-runtime configuration and wiring.

The agent process (this package, run in the .venv-agent / Python 3.10+ env) talks to the
robot tool layer over MCP stdio. The robot MCP server runs in a SEPARATE env (.venv here,
standing in for the robot's 3.9 env) with ROBOT_STUB=1 for the whole agent phase.
"""
import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STEERING_DIR = Path(__file__).resolve().parent / "steering"
DATA_DIR = REPO / "data"

# The robot tool layer's interpreter (has mcp + requests + robot_tools on PYTHONPATH).
ROBOT_PYTHON = REPO / ".venv" / "bin" / "python"

# MCP server name -> tools are namespaced mcp__robot__<tool>. Letters only = simple matching.
ROBOT_SERVER = "robot"


def load_env() -> None:
    """Load .env (ANTHROPIC_API_KEY) into os.environ without overriding existing values."""
    envf = REPO / ".env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def find_cli() -> str:
    """Absolute path to the Claude Code CLI the SDK shells out to (arm64 build)."""
    return shutil.which("claude") or "/opt/homebrew/bin/claude"


def read_steering(name: str) -> str:
    """Read a steering file (behavior lives here, not in hardcoded branches)."""
    return (STEERING_DIR / name).read_text()


def robot_mcp_config(tool_log_path: Path, world_state_path: Path, scene: str | None = None) -> dict:
    """Stdio MCP server config pointing at the robot tool layer.

    Mode is chosen by the MISTY_IP env var:
      - MISTY_IP set  -> REAL mode: the server connects to the physical Misty and fails
                         loudly at startup if it's unreachable.
      - MISTY_IP unset -> STUB mode (default): the robot is faked for offline dev. `scene`
                         (a dir of scripted <direction>.jpg frames) is served in stub mode
                         only (ROBOT_STUB_SCENE) for perception tests."""
    env = {
        "PYTHONPATH": str(REPO),                # make robot_tools importable via -m
        "PATH": os.environ.get("PATH", ""),
        "TOOL_LOG_PATH": str(tool_log_path),    # shared measurement boundary (JSONL)
        "WORLD_STATE_PATH": str(world_state_path),
    }
    misty_ip = os.environ.get("MISTY_IP")
    if misty_ip:
        env["MISTY_IP"] = misty_ip              # REAL robot
    else:
        env["ROBOT_STUB"] = "1"                 # offline stub (default)
        if scene:
            env["ROBOT_STUB_SCENE"] = str(scene)
        if os.environ.get("ROBOT_STUB_SLOW") == "1":
            env["ROBOT_STUB_SLOW"] = "1"         # honor sleeps in stub (make tools slow)
    return {
        "type": "stdio",
        "command": str(ROBOT_PYTHON),
        "args": ["-m", "robot_tools.server"],
        "env": env,
    }


# All 14 robot tools, namespaced. Handy for allowed_tools / per-expert scoping.
def robot_tool(name: str) -> str:
    return f"mcp__{ROBOT_SERVER}__{name}"


ALL_ROBOT_TOOLS = [robot_tool(n) for n in [
    "move_forward", "move_backward", "strafe_left", "strafe_right",
    "turn_left", "turn_right", "stop",
    "move_arm", "move_head", "display_image", "change_led", "reset_pose",
    "speak",
    "capture_view", "get_last_view", "find_object",
    "get_known_location", "update_world", "get_world",
]]

# Built-in tools no coordinator/expert should ever use (arbitrary-shell / filesystem /
# web / scheduling). Shared by every architecture; each arch decides its delegation tools
# on top of this. Kept here (not in main.py) so all architectures reference one list.
DISALLOWED_BUILTINS = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "WebFetch",
                       "WebSearch", "ScheduleWakeup"]
