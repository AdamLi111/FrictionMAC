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
    """True when ROBOT_STUB=1 (explicit opt-in). Never inferred."""
    return os.environ.get("ROBOT_STUB") == "1"


def misty_ip():
    """Robot address for real mode (e.g. '192.168.1.50'). None if unset."""
    return os.environ.get("MISTY_IP")


def world_state_path() -> str:
    """Persistent JSON file backing WorldState (cross-session memory)."""
    return os.environ.get("WORLD_STATE_PATH") or os.path.join(DEFAULT_DATA_DIR, "world_state.json")


def tool_log_path() -> str:
    """JSONL file where every tool call is logged."""
    return os.environ.get("TOOL_LOG_PATH") or os.path.join(DEFAULT_DATA_DIR, "tool_calls.jsonl")
