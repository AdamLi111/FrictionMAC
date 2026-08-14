"""
Runtime configuration, read from the environment.

Two runtimes (see UNDERSTANDING.md section 7):
  - Real mode (default): talk to a physical Misty at MISTY_IP; fail loudly if unreachable.
  - Stub mode (ROBOT_STUB=1, opt-in): fake all robot calls for offline desk testing.
"""
import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(_PKG_DIR)
DEFAULT_DATA_DIR = os.path.join(REPO_DIR, "data")


def is_stub() -> bool:
    """True when ROBOT_STUB=1 (explicit opt-in), or whenever the simulation is active —
    the sim replaces the physical robot, so it always runs offline."""
    return os.environ.get("ROBOT_STUB") == "1" or is_sim()


def is_sim() -> bool:
    """True when the offline simulation backend is active: ROBOT_SIM=1, or a scene given via
    ROBOT_SIM_SCENE. The sim stands in for the physical robot behind the stub."""
    return os.environ.get("ROBOT_SIM") == "1" or bool(os.environ.get("ROBOT_SIM_SCENE"))


def sim_scene_path() -> str:
    """Scene file (path or bundled name) backing the sim. Defaults to the office_kitchen demo."""
    return os.environ.get("ROBOT_SIM_SCENE") or "office_kitchen"


def sim_state_path() -> str:
    """Live snapshot of the sim's ground-truth world, rewritten on every move so an external
    tool (e.g. the visualizer) can see the robot's current pose. Distinct from the scene file,
    which stays the pristine initial condition."""
    return os.environ.get("SIM_STATE_PATH") or os.path.join(DEFAULT_DATA_DIR, "sim_state.json")


def misty_ip():
    """Robot address for real mode (e.g. '192.168.1.50'). None if unset."""
    return os.environ.get("MISTY_IP")


def world_state_path() -> str:
    """Persistent JSON file backing WorldState (cross-session memory)."""
    return os.environ.get("WORLD_STATE_PATH") or os.path.join(DEFAULT_DATA_DIR, "world_state.json")


def tool_log_path() -> str:
    """JSONL file where every tool call is logged."""
    return os.environ.get("TOOL_LOG_PATH") or os.path.join(DEFAULT_DATA_DIR, "tool_calls.jsonl")
