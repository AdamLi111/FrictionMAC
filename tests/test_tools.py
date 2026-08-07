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
def test_all_16_tools_callable_and_shaped(stub_env):
    from robot_tools import tools

    assert len(tools.ALL_TOOLS) == 16

    # movement
    r = tools.move_forward(2.0)
    assert r["ok"] is True and isinstance(r["duration_ms"], int)
    assert tools.move_backward(1.0)["ok"] is True
    assert tools.turn_left(90)["ok"] is True
    assert tools.turn_right(45)["ok"] is True
    assert tools.stop() == {"ok": True}

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
                 "turn_left", "turn_right", "stop", "move_arm", "move_head",
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
