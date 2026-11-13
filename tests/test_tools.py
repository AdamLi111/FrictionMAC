"""
Stub-mode tests for the robot tool layer. No hardware, no agent.
Covers the six required areas from the build spec.
"""
import glob
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_log(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------- Test 1: all tools
def test_all_17_tools_callable_and_shaped(stub_env):
    from robot_tools import sensors, tools

    assert len(tools.ALL_TOOLS) == 17

    # movement
    r = tools.move_forward(2.0)
    assert r["ok"] is True and isinstance(r["duration_ms"], int)
    assert tools.move_backward(1.0)["ok"] is True
    assert tools.turn_left(90)["ok"] is True
    assert tools.turn_right(45)["ok"] is True
    assert tools.stop() == {"ok": True}

    # sensing: the stub robot has no sensors, and says so rather than inventing readings
    d = tools.read_distance_sensors()
    assert d["ok"] is True and d["bumpers_pressed"] == [] and d["sensors"] == sensors.OFFLINE
    assert d["distances_m"] == {s: sensors.UNKNOWN for s in sensors.RANGE_SENSORS}

    # expression
    assert tools.move_arm("both", 30)["ok"] is True
    assert tools.move_head(pitch=-10)["ok"] is True
    assert tools.display_image("e_Joy.jpg")["ok"] is True
    assert tools.change_led(0, 255, 0)["ok"] is True
    assert tools.reset_pose(hold_seconds=0)["ok"] is True

    # speech (friction_type required)
    s = tools.speak("hello", friction_type="none")
    assert s["ok"] is True and s["friction_type"] == "none"

    # vision
    cv = tools.capture_view()
    assert cv["ok"] is True and isinstance(cv["image"], str) and cv["image"]
    lv = tools.get_last_view()   # after capture_view -> 1 cached frame
    assert lv["ok"] is True and len(lv["frames"]) == 1 and lv["frames"][0]["direction"] == "ahead"

    # world memory
    assert tools.get_known_location("nothing-known") is None
    uw = tools.update_world("mug", {"location": "kitchen counter"})
    assert uw["ok"] is True and uw["stored"]["location"] == "kitchen counter"
    gw = tools.get_world()
    assert gw["ok"] is True and gw["world"]["mug"]["location"] == "kitchen counter"

    # Every call above logged exactly one line; nothing raised.
    log = _read_log(stub_env["log_path"])
    tool_names = {e["name"] for e in log}
    for name in ["move_forward", "move_backward",
                 "turn_left", "turn_right", "stop", "read_distance_sensors",
                 "move_arm", "move_head",
                 "display_image", "change_led", "reset_pose", "speak", "capture_view",
                 "get_last_view", "get_known_location", "update_world",
                 "get_world"]:
        assert name in tool_names, f"{name} not logged"


# --------------------------------- Test 2: friction_type labels every speak in the log
def test_speak_logs_friction_type(stub_env):
    from robot_tools import tools

    tools.speak("normal response", friction_type="none")
    tools.speak("did you mean the red one?", friction_type="probing")

    log = _read_log(stub_env["log_path"])
    speaks = [e for e in log if e["name"] == "speak"]

    assert len(speaks) == 2
    labels = [e["args"]["friction_type"] for e in speaks]
    # A friction turn is distinguishable from a normal one purely by the required label.
    assert "none" in labels and "probing" in labels
    frictionful = [e for e in speaks if e["args"]["friction_type"] != "none"]
    assert len(frictionful) == 1 and frictionful[0]["result"]["friction_type"] == "probing"


# ----------------------------------- Test 3: movement calibrated ms + zero narration
def test_movement_issues_calibrated_drive_time_and_never_narrates(stub_env):
    from robot_tools import tools, runtime

    robot = runtime.get_robot()  # StubRobot singleton

    tools.move_forward(2.0)
    tools.turn_left(90)
    tools.move_backward(1.5)
    tools.turn_right(30)

    drives = robot.calls_of("drive_time")
    # move_forward(2.0): linear +50, calibrated drive ms
    assert drives[0]["kwargs"]["linearVelocity"] == 50
    assert drives[0]["kwargs"]["timeMs"] == runtime.calc_drive_time(2.0)
    # turn_left(90): angular +100, calibrated turn ms
    assert drives[1]["kwargs"]["angularVelocity"] == 100
    assert drives[1]["kwargs"]["timeMs"] == runtime.calc_turn_time(90)
    # move_backward(1.5): linear -50
    assert drives[2]["kwargs"]["linearVelocity"] == -50
    assert drives[2]["kwargs"]["timeMs"] == runtime.calc_drive_time(1.5)
    # turn_right(30): angular -100
    assert drives[3]["kwargs"]["angularVelocity"] == -100
    assert drives[3]["kwargs"]["timeMs"] == runtime.calc_turn_time(30)

    # No narration: movement tools must never speak.
    assert robot.calls_of("speak") == []


# ------------------------------------------- Test 4: cross-PROCESS world persistence
def test_world_state_persists_across_processes(tmp_path):
    world_path = str(tmp_path / "world_state.json")
    log_path = str(tmp_path / "tool_calls.jsonl")
    env = dict(os.environ)
    env.update({"ROBOT_STUB": "1", "WORLD_STATE_PATH": world_path, "TOOL_LOG_PATH": log_path})
    env.pop("MISTY_IP", None)

    # Process A: write, then exit.
    code_a = (
        "from robot_tools import tools, runtime; runtime.reset();"
        "tools.update_world('mug', {'location': 'kitchen counter'})"
    )
    pa = subprocess.run([sys.executable, "-c", code_a], cwd=REPO, env=env,
                        capture_output=True, text=True)
    assert pa.returncode == 0, pa.stderr

    # Process B: a fresh interpreter reads it back.
    code_b = (
        "from robot_tools import tools, runtime; runtime.reset();"
        "import json; print(json.dumps(tools.get_known_location('mug')))"
    )
    pb = subprocess.run([sys.executable, "-c", code_b], cwd=REPO, env=env,
                        capture_output=True, text=True)
    assert pb.returncode == 0, pb.stderr
    assert json.loads(pb.stdout.strip()) == {"location": "kitchen counter"}


# ---------------------------------------------------- Test 5: world-state safety
def test_update_preserves_other_objects(stub_env):
    from robot_tools import runtime
    world = runtime.get_world()

    world.update_world("mug", {"location": "kitchen"})
    world.update_world("keys", {"location": "hallway"})
    world.update_world("mug", {"color": "blue"})  # update mug again

    data = json.load(open(stub_env["world_path"]))
    assert data["keys"] == {"location": "hallway"}          # untouched
    assert data["mug"] == {"location": "kitchen", "color": "blue"}  # merged, nothing wiped


def test_interrupted_write_does_not_corrupt_existing_file(stub_env, monkeypatch):
    from robot_tools import runtime, world_state
    world = runtime.get_world()

    world.update_world("mug", {"location": "kitchen"})
    world.update_world("keys", {"location": "hallway"})
    before = json.load(open(stub_env["world_path"]))

    # Simulate a crash at the atomic-commit step.
    def boom(*a, **k):
        raise RuntimeError("simulated crash during os.replace")

    monkeypatch.setattr(world_state.os, "replace", boom)
    with pytest.raises(RuntimeError):
        world.update_world("lamp", {"location": "desk"})
    monkeypatch.undo()

    # Existing file is still valid JSON, unchanged, and no temp files leaked.
    after = json.load(open(stub_env["world_path"]))
    assert after == before
    assert glob.glob(os.path.join(os.path.dirname(stub_env["world_path"]), ".world-*.tmp")) == []

    # And the store still works afterwards.
    world.update_world("lamp", {"location": "desk"})
    assert json.load(open(stub_env["world_path"]))["lamp"] == {"location": "desk"}


# ------------------------------------------ Test 6: vision frame shapes
def test_capture_view_returns_one_frame(stub_env):
    from robot_tools import tools

    result = tools.capture_view()
    assert isinstance(result["image"], str) and result["image"]


def test_capture_view_image_result_is_redacted_in_log(stub_env):
    from robot_tools import tools

    tools.capture_view()
    log = _read_log(stub_env["log_path"])

    cv = [e for e in log if e["name"] == "capture_view"][-1]
    # Redacted: counts/sizes, not raw base64.
    assert cv["result"]["frames"] == 1 and "image" not in cv["result"]


# ------------------------------------------ Test 7: motor-lock semantics
def test_motor_locks_serialize_same_resource_but_not_different(stub_env):
    from robot_tools import runtime

    # Same resource -> the same lock object; different resources -> different locks.
    assert runtime.motor_lock("DRIVE") is runtime.motor_lock("DRIVE")
    assert runtime.motor_lock("DRIVE") is not runtime.motor_lock("ARM_LEFT")
    # The two arms are independent actuators -> distinct locks.
    assert runtime.motor_lock("ARM_LEFT") is not runtime.motor_lock("ARM_RIGHT")

    drive = runtime.motor_lock("DRIVE")
    assert drive.acquire(blocking=False) is True
    try:
        # While DRIVE is held, another DRIVE acquisition must fail (mutual exclusion)...
        assert runtime.motor_lock("DRIVE").acquire(blocking=False) is False
        # ...but a different motor resource stays free (can run concurrently).
        left = runtime.motor_lock("ARM_LEFT")
        assert left.acquire(blocking=False) is True
        # ...and the OTHER arm is independent (left held doesn't block right).
        assert runtime.motor_lock("ARM_RIGHT").acquire(blocking=False) is True
        runtime.motor_lock("ARM_RIGHT").release()
        left.release()
    finally:
        drive.release()


# ------------------------------------------ Test 8: distance + bump sensors (the robot's path)
# The physical robot's path runs in stub mode here: SensorMonitor is fed the messages Misty's
# pub/sub would send, on an injected clock, so "how far into the move" is exact.
def _tof(sensor_id, metres, status=0):
    return json.dumps({"eventName": f"ponder_tof_{sensor_id}", "message": {
        "created": "2026-11-12T20:00:00.0000000Z", "distanceInMeters": metres,
        "sensorId": sensor_id, "status": status, "type": "Range"}})


def _bump(sensor_id, contacted):
    return json.dumps({"eventName": "ponder_bump", "message": {
        "created": "2026-11-12T20:00:01.2500000Z", "isContacted": contacted,
        "sensorId": sensor_id}})


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def monitor(stub_env):
    """A connected SensorMonitor installed as the runtime's, driven by hand."""
    from robot_tools import runtime, sensors
    m = sensors.SensorMonitor(clock=_Clock())
    m._connected = True              # as if subscribed to the robot's pub/sub
    runtime._state["sensors"] = m
    return m


def test_range_readings_are_parsed_and_judged_by_status(monitor):
    from robot_tools import sensors
    monitor.feed(_tof("toffc", 0.42))
    monitor.feed(_tof("toffl", 0.9, status=102))      # signal fail: nothing it trusts
    monitor.feed({"EventName": "x", "Message": {      # PascalCase works too
        "DistanceInMeters": 1.05, "SensorId": "toffr", "Status": 0}})
    monitor.feed(_tof("tofdfl", 0.02))                # a downward edge sensor: not ours
    monitor.feed('{"eventName": "ponder_tof_tofr", '
                 '"message": "Registration Status: API event registered."}')
    monitor.feed("not json")
    assert monitor.distances() == {"front_left": sensors.NO_READING, "front_center": 0.42,
                                   "front_right": 1.05, "back": sensors.UNKNOWN}
    monitor._clock.t += 5                             # five seconds with nothing new
    assert set(monitor.distances().values()) == {sensors.UNKNOWN}


def test_a_bump_mid_drive_stops_the_robot_and_says_how_far_it_got(monitor):
    from robot_tools import runtime, tools
    robot = runtime.get_robot()
    send = robot.drive_time

    def drive_into_a_wall(*args, **kwargs):           # contact 1.25 s into the drive
        send(*args, **kwargs)
        monitor._clock.t += 1.25
        monitor.feed(_bump("bfl", True))
        monitor.feed(_tof("toffc", 0.01))

    robot.drive_time = drive_into_a_wall
    r = tools.move_forward(2.0)
    assert r["ok"] is False and r["bump"]["bumpers"] == ["front_left"]
    assert r["bump"]["after_ms"] == 1250
    # 1250 ms is the calibrated time to cover 0.25 m from rest, so that is about where it hit
    assert r["bump"]["estimated_traveled_m"] == pytest.approx(0.25, abs=0.01)
    assert r["bump"]["time"].startswith("2026-11-12T20:00:01")      # the robot's own clock
    assert r["distances_m"]["front_center"] == 0.01
    assert robot.calls_of("stop")                      # stopped at the contact


def test_the_robot_wont_drive_into_a_bumper_that_is_still_pressed(monitor):
    from robot_tools import runtime, tools
    robot = runtime.get_robot()
    monitor.feed(_bump("bfr", True))                   # resting against something
    monitor._clock.t += 1
    r = tools.move_forward(1.0)
    assert r["ok"] is False and r["bump"]["already_pressed"] is True
    assert r["bump"]["bumpers"] == ["front_right"]
    assert robot.calls_of("drive_time") == []          # never sent: Misty would ignore it
    assert tools.move_backward(0.2)["ok"] is True      # backing away is allowed
    monitor.feed(_bump("bfr", False))                  # released
    assert tools.move_forward(1.0)["ok"] is True


def test_without_sensor_data_a_move_says_the_sensors_are_offline(stub_env):
    from robot_tools import sensors, tools
    r = tools.move_forward(1.0)
    assert r["ok"] is True and r["sensors"] == sensors.OFFLINE
    assert set(r["distances_m"].values()) == {sensors.UNKNOWN}


def test_each_range_sensor_gets_its_own_subscription():
    from robot_tools import sensors
    subs = sensors.subscription_messages()
    tof = [s for s in subs if s["Type"] == "TimeOfFlight"]
    assert sorted(s["EventConditions"][0]["Value"] for s in tof) == [
        "toffc", "toffl", "toffr", "tofr"]
    assert [s["Type"] for s in subs].count("BumpSensor") == 1
    assert len({s["EventName"] for s in subs}) == len(subs)


def test_the_websocket_callbacks_take_either_calling_convention(monitor):
    """websocket-client < 1.0 calls a bound method without the app, >= 1.0 with it first."""
    from robot_tools import sensors
    monitor._on_message(_tof("toffc", 0.5))
    monitor._on_message(object(), _tof("tofr", 0.7))
    readings = monitor.distances()
    assert readings["front_center"] == 0.5 and readings["back"] == 0.7

    sent = []

    class _App:
        def send(self, text):
            sent.append(json.loads(text))

    monitor._on_open(_App())
    assert sent == sensors.subscription_messages()


def test_drive_progress_inverts_the_calibration():
    from robot_tools import runtime
    for metres in (0.1, 0.5, 1.0, 2.0):
        elapsed = runtime.calc_drive_time(metres)
        assert runtime.calc_drive_progress(elapsed, 3.0) == pytest.approx(metres, abs=0.01)
    assert runtime.calc_drive_progress(0, 2.0) == 0.0
    assert runtime.calc_drive_progress(10 ** 6, 2.0) == 2.0     # never more than planned


# ------------------------------------------ Test 9: the MCP server itself loads
def test_the_mcp_server_module_imports():
    """Nothing above imports server.py, so an MCP SDK it can't run on (mcp 2.x renamed FastMCP)
    passed every test and only surfaced at runtime, as every expert failing with
    "unrecognized [mcp__robot__speak]". Importing it registers the tools but starts nothing."""
    pytest.importorskip("mcp")
    import robot_tools.server  # noqa: F401
