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
    monitor = _state.get("sensors")
    if monitor is not None:
        monitor.close()
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
    """The robot the tools command: the real Misty, the stub in stub mode, or in sim mode the
    simulated Misty (sim/robot.py), which drives through the sim world and streams its sensors."""
    if "robot" not in _state:
        if config.is_sim():
            from .sim.robot import SimRobot
            _state["robot"] = SimRobot(get_sim_world())
        else:
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


def get_sensors():
    """Misty's distance + bump sensors (sensors.SensorMonitor): streamed from the robot in real
    mode; streamed from the simulated robot in sim mode, in the same message format and at the
    same rate; never connected in stub mode, so it has no data and tools report the sensors
    offline."""
    if "sensors" not in _state:
        if config.is_sim():
            _state["sensors"] = get_robot().monitor
        else:
            from .sensors import SensorMonitor
            _state["sensors"] = SensorMonitor(
                None if config.is_stub() else config.misty_ip()).start()
    return _state["sensors"]


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


def drive_distance_at(elapsed_ms: float, planned_m: float) -> float:
    """The drive calibration run backwards: how far (m) a straight move planned for `planned_m`
    has got `elapsed_ms` after it was commanded. The calibration gives the time a drive from
    rest needs to cover a distance, so this is the distance whose time is `elapsed_ms`, found by
    bisection so the curve's shape stays in the vendored calibration. Capped at the plan. The
    simulated robot moves by exactly this curve, so its speed is the calibration's."""
    planned_m = abs(planned_m)
    if elapsed_ms <= 0 or planned_m <= 0:
        return 0.0
    if elapsed_ms >= calc_drive_time(planned_m):
        return planned_m
    lo, hi = 0.0, planned_m
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if calc_drive_time(mid) < elapsed_ms:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def calc_drive_progress(elapsed_ms: float, planned_m: float) -> float:
    """About how far (m, to the cm) a straight move had got `elapsed_ms` into it: what a bump or
    obstacle result reports as `estimated_traveled_m`. Only as good as the drive calibration."""
    return round(drive_distance_at(elapsed_ms, planned_m), 2)


# --- stub-aware helpers ---
def wait_budget(seconds: float) -> float:
    """How long a tool may wait on the robot's sensors: `seconds` on hardware, and in the sim,
    where waiting is what moves simulated time forward and costs no real time; 0 in stub mode so
    tests run fast. ROBOT_STUB_SLOW=1 honors the delays even in stub mode (used to make a stub
    tool call reliably slow, e.g. for the background-task in-flight probe)."""
    if config.is_sim():
        return max(0.0, seconds)
    if config.is_stub() and os.environ.get("ROBOT_STUB_SLOW") != "1":
        return 0.0
    return max(0.0, seconds)


def sleep(seconds: float) -> None:
    """Real sequencing delay on hardware; a no-op in stub mode (see wait_budget) and in the sim,
    where nothing waits on the wall clock."""
    if config.is_sim():
        return
    seconds = wait_budget(seconds)
    if seconds:
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
