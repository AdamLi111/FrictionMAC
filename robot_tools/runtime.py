"""
Process-wide runtime singletons and shared helpers.

Holds the robot, vision handler, world memory, and tool logger as lazily-constructed
singletons, plus the calibration bridge into the vendored ActionExecutor and a
stub-aware sleep / frame-capture. Call reset() to drop the singletons (used by tests
that swap in temp paths / toggle stub mode).
"""
import base64
import os
import threading

from . import backend, config
from .logging_jsonl import ToolLogger
from .world_state import WorldState
from .vendor.action_executor import ActionExecutor
from .vendor.vision_handler import VisionHandler

# Re-export the stub frame so tools/tests share one definition.
FAKE_FRAME = backend.STUB_FRAME_B64

_state = {}

# Per-motor-resource locks. Tool calls that drive the SAME physical resource must not
# overlap (mutual exclusion); calls on DIFFERENT resources may run concurrently. The MCP
# server offloads each tool call to a worker thread, so these are threading.Locks. See
# the resource -> tool mapping in tools.py.
# NOTE: the two arms are independent actuators -> ARM_LEFT / ARM_RIGHT are separate locks,
# so a left-arm move and a right-arm move can run at the same time.
_MOTOR_RESOURCES = ("DRIVE", "HEAD", "ARM_LEFT", "ARM_RIGHT", "FACE", "LED")
_MOTOR_LOCKS = {r: threading.Lock() for r in _MOTOR_RESOURCES}


def motor_lock(resource: str) -> "threading.Lock":
    """The mutex guarding one physical motor resource
    (DRIVE / HEAD / ARM_LEFT / ARM_RIGHT / FACE / LED)."""
    return _MOTOR_LOCKS[resource]

# Calibration comes straight from the vendored ActionExecutor -- it is the source of
# truth for meters/degrees -> milliseconds. The helpers are pure math (they never touch
# self.robot), so a robot-less instance is fine.
_calib = ActionExecutor(None)


# Cache of the MOST RECENT capture: a list of {"direction", "image"} (one entry per capture_view).
# get_last_view() serves this so the map agent can look at what was just seen without a new shot.
_last_capture = []
_last_capture_lock = threading.Lock()


def set_last_capture(frames):
    global _last_capture
    with _last_capture_lock:
        _last_capture = list(frames)


def get_last_capture():
    with _last_capture_lock:
        return list(_last_capture)


def reset():
    _state.clear()
    set_last_capture([])


def get_logger() -> ToolLogger:
    if "logger" not in _state:
        _state["logger"] = ToolLogger(config.tool_log_path())
    return _state["logger"]


def get_world() -> WorldState:
    if "world" not in _state:
        _state["world"] = WorldState(config.world_state_path(), logger=get_logger())
    return _state["world"]


def get_robot():
    if "robot" not in _state:
        _state["robot"] = backend.make_robot()
    return _state["robot"]


def get_sim_world():
    """The ground-truth simulation world (sim mode only), loaded once from the scene file.
    Movement tools mutate it and capture_view reads it; it is distinct from the agent's belief
    store in get_world(). Returns None when the sim is not active."""
    if not config.is_sim():
        return None
    if "sim_world" not in _state:
        from .sim import load_scene
        _state["sim_world"] = load_scene(config.sim_scene_path())
        save_sim_state()                 # write the start pose so external viewers see it
    return _state["sim_world"]


def save_sim_state():
    """Atomically write the live sim world to sim_state_path so a separate process (e.g. the
    visualizer) can render the robot's current pose. No-op outside sim mode."""
    world = _state.get("sim_world")
    if world is None:
        return
    import json
    path = config.sim_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(world.snapshot(), f, indent=2)
    os.replace(tmp, path)                 # atomic: readers never see a half-written file


def get_vision():
    """VisionHandler in real mode; None in stub mode (frames are faked)."""
    if "vision" not in _state:
        _state["vision"] = None if config.is_stub() else VisionHandler(get_robot(), config.misty_ip())
    return _state["vision"]


# --- calibration bridge ---
def calc_drive_time(distance) -> int:
    return _calib._calculate_drive_time(distance)


def calc_turn_time(degrees) -> int:
    return _calib._calculate_turn_time(degrees)


# --- stub-aware helpers ---
def sleep(seconds: float) -> None:
    """Real sequencing delay on hardware; a no-op in stub mode so tests run fast.
    Set ROBOT_STUB_SLOW=1 to honor the delays even in stub mode (used to make a stub
    tool call reliably slow, e.g. for the background-task in-flight probe)."""
    if config.is_stub() and os.environ.get("ROBOT_STUB_SLOW") != "1":
        return
    import time
    time.sleep(seconds)


def _scene_frame(direction: str) -> str:
    """Stub frame source. If ROBOT_STUB_SCENE points at a dir with <direction>.jpg files,
    serve those real JPEGs (base64) so a VLM can genuinely perceive a scripted scene during
    testing; otherwise return the inert FAKE_FRAME. Test scaffolding only."""
    scene = os.environ.get("ROBOT_STUB_SCENE")
    if scene:
        path = os.path.join(scene, f"{direction}.jpg")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
    return FAKE_FRAME


def capture_frame(direction: str = "front"):
    """One base64 JPEG frame: faked (or scripted, in stub scene mode) in stub mode, real
    capture otherwise. `direction` labels which scan view this is (ignored in real mode)."""
    if config.is_stub():
        return _scene_frame(direction)
    vision = get_vision()
    return vision.capture_and_encode() if vision else None
