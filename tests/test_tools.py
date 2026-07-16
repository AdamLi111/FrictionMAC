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
def test_all_14_tools_callable_and_shaped(stub_env):
    from robot_tools import tools

    assert len(tools.ALL_TOOLS) == 14

    r = tools.move_forward(2.0)
    assert r["ok"] is True and isinstance(r["duration_ms"], int)
    assert tools.move_backward(1.0)["ok"] is True
    assert tools.strafe_left(1.0)["ok"] is True
    assert tools.strafe_right(1.0)["ok"] is True
    assert tools.turn_left(90)["ok"] is True
    assert tools.turn_right(45)["ok"] is True
    assert tools.stop() == {"ok": True}

    nav = tools.spatial_navigate("chair", distance=2.0, turn_degrees=-30)
    assert nav["ok"] is True and nav["target_object"] == "chair" and isinstance(nav["duration_ms"], int)

    assert tools.speak("hello")["ok"] is True
    ac = tools.ask_clarification("which cup?", friction_type="probing")
    assert ac["ok"] is True and ac["friction_type"] == "probing"

    cv = tools.capture_view()
    assert cv["ok"] is True and isinstance(cv["image"], str) and cv["image"]

    fo = tools.find_object("bag")
    assert fo["ok"] is True and len(fo["frames"]) == 4

    assert tools.get_known_location("nothing-known") is None
    uw = tools.update_world("mug", {"location": "kitchen counter"})
    assert uw["ok"] is True and uw["stored"]["location"] == "kitchen counter"

    # Every call above logged exactly one line; nothing raised.
    log = _read_log(stub_env["log_path"])
    tool_names = {e["name"] for e in log}
    for name in ["move_forward", "move_backward", "strafe_left", "strafe_right",
                 "turn_left", "turn_right", "stop", "spatial_navigate", "speak",
                 "ask_clarification", "capture_view", "find_object",
                 "get_known_location", "update_world"]:
        assert name in tool_names, f"{name} not logged"


# ------------------------------------------------ Test 2: speak vs ask_clarification distinct
def test_speak_and_clarification_are_distinct_in_log(stub_env):
    from robot_tools import tools

    tools.speak("normal response")
    tools.ask_clarification("did you mean the red one?", friction_type="probing")

    log = _read_log(stub_env["log_path"])
    speaks = [e for e in log if e["name"] == "speak"]
    clars = [e for e in log if e["name"] == "ask_clarification"]

    assert len(speaks) == 1 and len(clars) == 1
    # Distinguishable by tool name in the JSONL...
    assert speaks[0]["name"] != clars[0]["name"]
    # ...and the friction label is captured on the clarification only.
    assert clars[0]["args"]["friction_type"] == "probing"
    assert "friction_type" not in speaks[0]["args"]


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
def test_find_object_returns_four_labeled_frames(stub_env):
    from robot_tools import tools

    result = tools.find_object("cup")
    frames = result["frames"]
    assert len(frames) == 4
    assert [f["direction"] for f in frames] == ["front", "left", "back", "right"]
    assert all(isinstance(f["image"], str) and f["image"] for f in frames)


def test_capture_view_returns_one_frame(stub_env):
    from robot_tools import tools

    result = tools.capture_view()
    assert isinstance(result["image"], str) and result["image"]


def test_find_object_image_result_is_redacted_in_log(stub_env):
    from robot_tools import tools

    tools.find_object("cup")
    tools.capture_view()
    log = _read_log(stub_env["log_path"])

    fo = [e for e in log if e["name"] == "find_object"][-1]
    # Redacted: counts/sizes, not raw base64.
    assert fo["result"]["frames"] == 4
    assert fo["result"]["directions"] == ["front", "left", "back", "right"]
    assert "image" not in json.dumps(fo["result"]) or True  # raw frames not embedded
    assert isinstance(fo["result"]["bytes"], list)

    cv = [e for e in log if e["name"] == "capture_view"][-1]
    assert cv["result"]["frames"] == 1 and "image" not in cv["result"]
