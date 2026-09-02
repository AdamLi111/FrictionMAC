"""
Agent-runtime configuration and wiring.

The agent process (this package, run in the .venv-agent / Python 3.10+ env) talks to the
robot tool layer over MCP stdio. The robot MCP server runs in a SEPARATE env (.venv here,
standing in for the robot's 3.9 env) with ROBOT_STUB=1 for the whole agent phase.
"""
import os
import shutil
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STEERING_DIR = Path(__file__).resolve().parent / "steering"
DATA_DIR = REPO / "data"


def belief_store_path() -> Path:
    """File where the agent's world-model beliefs (get_world / update_world / get_known_location)
    persist. In SIM mode this is a SEPARATE file per scene, so simulated beliefs never mix with
    the real-robot store (or with a different scene's). An explicit WORLD_STATE_PATH always wins."""
    override = os.environ.get("WORLD_STATE_PATH")
    if override:
        return Path(override)
    scene = os.environ.get("ROBOT_SIM_SCENE")
    if scene or os.environ.get("ROBOT_SIM") == "1":
        base = os.path.splitext(os.path.basename(scene))[0] if scene else "sim"
        return DATA_DIR / f"sim_world_{base}.json"
    return DATA_DIR / "agent_world_state.json"

# The robot is (almost) always at this address, so you never have to type MISTY_IP: real mode
# is the default and uses this IP. Opt into offline stub with ROBOT_STUB=1 (or --stub).
DEFAULT_MISTY_IP = "172.20.10.2"

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
    sim_scene = os.environ.get("ROBOT_SIM_SCENE")
    sim = os.environ.get("ROBOT_SIM") == "1" or bool(sim_scene)
    if sim:
        # Simulation stands in for the physical robot (offline, no MISTY_IP). The server reads
        # ROBOT_SIM / ROBOT_SIM_SCENE and backs the tools with a ground-truth SimWorld.
        env["ROBOT_SIM"] = "1"
        if sim_scene:
            env["ROBOT_SIM_SCENE"] = sim_scene
    elif misty_ip:
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


# All 16 robot tools, namespaced. Handy for allowed_tools / per-expert scoping.
def robot_tool(name: str) -> str:
    return f"mcp__{ROBOT_SERVER}__{name}"


ALL_ROBOT_TOOLS = [robot_tool(n) for n in [
    "move_forward", "move_backward", "turn_left", "turn_right", "stop",
    "move_arm", "move_head", "display_image", "change_led", "reset_pose",
    "speak",
    "capture_view", "get_last_view",
    "get_known_location", "update_world", "get_world",
]]

# Built-in tools no coordinator/expert should ever use: arbitrary shell, filesystem
# read/write/search, web, and scheduling. `Grep`/`Glob` are included so nothing can read the
# repo (e.g. the steering files) even if a stray full-tool agent were ever spawned. Shared by
# every architecture; each arch adds its delegation tools on top. Kept here (not in main.py) so
# all architectures reference one list.
DISALLOWED_BUILTINS = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "Grep", "Glob",
                       "WebFetch", "WebSearch", "ScheduleWakeup"]


class RobotUnreachable(RuntimeError):
    """Raised by the agent-side preflight when the real robot can't be reached, BEFORE the
    SDK client / any API tokens are spent."""


def select_robot_target(stub: bool = False) -> str | None:
    """Decide real-vs-stub for a human entry point and normalize the environment for the MCP
    subprocess. REAL mode is the default at DEFAULT_MISTY_IP (so you never type MISTY_IP);
    pass stub=True or set ROBOT_STUB=1 for offline development.

    Returns the robot IP (real mode) or None (stub/sim). Mutates os.environ so robot_mcp_config
    and the preflight agree on the chosen mode."""
    # Simulation is offline like the stub: no MISTY_IP, no preflight.
    if os.environ.get("ROBOT_SIM") == "1" or os.environ.get("ROBOT_SIM_SCENE"):
        os.environ.pop("MISTY_IP", None)
        return None
    if stub or os.environ.get("ROBOT_STUB") == "1":
        os.environ["ROBOT_STUB"] = "1"
        os.environ.pop("MISTY_IP", None)     # MISTY_IP would otherwise force real mode
        return None
    ip = os.environ.get("MISTY_IP") or DEFAULT_MISTY_IP
    os.environ["MISTY_IP"] = ip              # make the default explicit for the subprocess
    return ip


def preflight_robot(ip: str | None = None, timeout: float = 4.0) -> None:
    """Fail FAST if the real robot isn't reachable, so a robot on the wrong Wi-Fi doesn't waste
    API tokens (an unreachable robot makes the MCP tools fail to register and the Director burns
    turns before giving up). No-op in stub mode. Hits Misty's HTTP API with the stdlib only (no
    `requests` dependency in the agent env). Raises RobotUnreachable on failure."""
    ip = ip or os.environ.get("MISTY_IP")
    if not ip:
        return  # stub mode — nothing to check
    url = f"http://{ip}/api/device"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise RobotUnreachable(_unreachable_msg(ip, f"HTTP {resp.status}"))
    except RobotUnreachable:
        raise
    except Exception as e:
        raise RobotUnreachable(_unreachable_msg(ip, f"{e.__class__.__name__}: {e}")) from e


def _unreachable_msg(ip: str, detail: str) -> str:
    return (
        f"Misty is not reachable at {ip} ({detail}).\n"
        f"  - Check that Misty and this laptop are on the SAME Wi-Fi network.\n"
        f"  - Confirm the robot is powered on and reachable at {ip}.\n"
        f"  - For offline development without the robot, add --stub (or set ROBOT_STUB=1).\n"
        f"Aborting before the agent starts, so no API tokens are spent."
    )
