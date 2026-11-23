"""
Tests for the simulation backend (robot_tools.sim) and its integration behind the MCP stub.

Covers the ground-truth physics (wall-aware collision, narrow FOV, wall occlusion) and the
tool-layer wiring (capture_view returns a text POV; movement surfaces collisions) in sim mode.
"""
import json
import math
import os

import pytest

from robot_tools.sim.world_model import SimWorld


def _scene(**over):
    scene = {
        "name": "t",
        "robot": {"position": [0.0, 0.0], "heading": 0},
        "fov_degrees": 45,
        "max_view_distance": 8.0,
        "robot_radius": 0.12,
        "objects": [
            {"name": "cup", "shape": {"type": "point", "position": [0.0, 2.0], "radius": 0.05}},
            {"name": "wall", "shape": {"type": "segment", "points": [[-2, 3], [2, 3]],
                                       "thickness": 0.1}},
        ],
    }
    scene.update(over)
    return scene


# ------------------------------------------------------------------ physics
_WALL = {"name": "wall", "shape": {"type": "segment", "points": [[-2, 3], [2, 3]],
                                   "thickness": 0.1}}


def test_forward_stops_before_wall():
    w = SimWorld(_scene(objects=[_WALL]))     # wall only, clear straight path up to it
    res = w.move(5.0)                          # wall centre at y=3, thickness 0.1
    assert res["collision"] == "wall"
    # stops at wall face minus robot radius: y ≈ 3 - 0.05 - 0.12
    assert w.robot_pos[1] == pytest.approx(2.83, abs=0.02)
    assert w.collisions and w.collisions[-1]["object"] == "wall"


def test_forward_hits_object_in_path_first():
    w = SimWorld(_scene())                     # cup is dead ahead at y=2, before the wall
    res = w.move(5.0)
    assert res["collision"] == "cup"           # everything is collidable, no "obstacle" flag


def test_clear_move_has_no_collision():
    w = SimWorld(_scene(objects=[]))
    res = w.move(1.5)
    assert res["collision"] is None
    assert w.robot_pos[1] == pytest.approx(1.5, abs=1e-6)


def test_fov_hides_object_behind_you():
    w = SimWorld(_scene())
    assert any(o["name"] == "cup" for o in w.visible_objects())   # cup is dead ahead
    w.turn(180)                                                   # now facing away
    assert not any(o["name"] == "cup" for o in w.visible_objects())


def test_wall_occludes_object_behind_it():
    # cup at y=4 sits BEHIND the wall at y=3 → hidden; without the wall it's visible.
    behind = _scene(objects=[
        {"name": "cup", "shape": {"type": "point", "position": [0.0, 4.0], "radius": 0.05}},
        {"name": "wall", "shape": {"type": "segment", "points": [[-2, 3], [2, 3]],
                                   "thickness": 0.1}},
    ])
    assert not any(o["name"] == "cup" for o in SimWorld(behind).visible_objects())

    no_wall = _scene(objects=[behind["objects"][0]])
    assert any(o["name"] == "cup" for o in SimWorld(no_wall).visible_objects())


def _seg(name, a, b):
    return {"name": name, "shape": {"type": "segment", "points": [a, b], "thickness": 0.1}}


def test_a_close_wall_is_seen_wherever_the_robot_meets_it():
    """Walls used to be seen only through 7 points along their length, so close up, with none of
    them inside the narrow cone, the view said "the way ahead looks clear/empty" a metre from
    the wall, and the next move_forward hit it. 0.83 is midway between two of those points."""
    for offset in (0.0, 0.5, 0.83):
        for gap in (2.0, 1.0, 0.6, 0.3):
            w = SimWorld(_scene(robot={"position": [offset, 0.0], "heading": 0},
                                objects=[_seg("wall", [-5, gap], [5, gap])]))
            seen = {o["name"]: o for o in w.visible_objects()}
            assert "wall" in seen, (offset, gap)
            # the distance to its face, straight ahead; not to the nearest of a few points
            assert seen["wall"]["distance"] == pytest.approx(gap - 0.05, abs=0.01), (offset, gap)
            assert "clear/empty" not in w.describe_view()


def test_furniture_ahead_is_seen_at_its_nearest_face():
    """Off-centre in front of a long table, none of its 9 old sample points (corners, edge
    midpoints, centre) fell inside the cone: the table straight ahead was invisible."""
    table = {"name": "table", "shape": {"type": "rect", "min": [-1.5, 1.0], "max": [1.5, 1.6]}}
    w = SimWorld(_scene(robot={"position": [0.7, 0.0], "heading": 0}, objects=[table]))
    seen = {o["name"]: o for o in w.visible_objects()}
    assert seen["table"]["distance"] == pytest.approx(1.0, abs=0.01)


def test_a_wall_hides_the_wall_behind_it_except_through_a_doorway():
    far = _seg("far wall", [-4, 4], [4, 4])
    solid = SimWorld(_scene(objects=[_seg("near wall", [-4, 2], [4, 2]), far]))
    assert "far wall" not in {o["name"] for o in solid.visible_objects()}

    doorway = SimWorld(_scene(objects=[_seg("near wall", [-4, 2], [-0.4, 2]),
                                       _seg("near wall", [0.4, 2], [4, 2]), far]))
    seen = {o["name"]: o for o in doorway.visible_objects()}
    assert "near wall" in seen
    # only the slice of the far wall that shows through the 0.8 m gap, about ±11° from here
    assert -12 < seen["far wall"]["bearing_min"] < seen["far wall"]["bearing_max"] < 12


def test_walls_outside_the_cone_stay_unseen():
    w = SimWorld(_scene(objects=[_seg("side wall", [2, -1], [2, 1])]))   # off to the right
    assert w.visible_objects() == []
    w.turn(90)                                                         # face it
    assert [o["name"] for o in w.visible_objects()] == ["side wall"]


def test_a_one_object_visibility_check_agrees_with_the_full_view():
    """evaluation.tasks.visibility asks about one target at a time through a cheaper cast; it
    must see exactly what capture_view would from the same pose."""
    from robot_tools.sim import load_scene

    w = load_scene("office_kitchen")
    for pos in ([-1.5, 0.0], [1.5, 2.5], [-2.0, 3.0]):
        w.robot_pos = list(pos)
        for heading in range(0, 360, 15):
            w.heading = float(heading)
            rays = w._ray_hits()
            for obj in w.objects:
                assert w._visible_samples(obj) == w._visible_samples(obj, rays), (pos, heading)


# ------------------------------------------------------------- distance + bump sensors
def test_range_sensors_measure_the_gap_from_the_body_to_a_wall_ahead():
    w = SimWorld(_scene(objects=[_seg("wall", [-5, 1.0], [5, 1.0])]))
    r = w.range_readings()
    # the wall's face is at 0.95 and the body's radius 0.12: the centre sensor reads the gap
    assert r["front_center"] == pytest.approx(0.83, abs=0.005)
    # the outer sensors look 30° out, so their nearest ray (17.5° off) meets the wall farther on
    assert r["front_left"] == pytest.approx(r["front_right"])
    assert r["front_left"] == pytest.approx(0.95 / math.cos(math.radians(17.5)) - 0.12, abs=0.005)
    assert r["back"] == "no reading"


def test_range_sensors_read_nothing_beyond_their_reach():
    w = SimWorld(_scene(objects=[_seg("wall", [-5, 3.0], [5, 3.0])]))
    assert set(w.range_readings().values()) == {"no reading"}


def test_range_sensors_see_small_objects_the_robot_could_drive_into():
    cup = {"name": "cup", "shape": {"type": "point", "position": [0.0, 0.5], "radius": 0.05}}
    w = SimWorld(_scene(objects=[cup]))
    assert w.range_readings()["front_center"] == pytest.approx(0.5 - 0.05 - 0.12, abs=0.005)


def test_the_back_sensor_looks_behind():
    w = SimWorld(_scene(objects=[_seg("wall", [-5, -0.6], [5, -0.6])]))
    r = w.range_readings()
    assert r["back"] == pytest.approx(0.6 - 0.05 - 0.12, abs=0.005)
    assert r["front_center"] == "no reading"


def test_a_head_on_collision_presses_both_front_bumpers_and_says_how_far_it_got():
    w = SimWorld(_scene(objects=[_seg("wall", [-5, 1.0], [5, 1.0])]))
    res = w.move(2.0)
    assert res["collision"] == "wall"
    assert res["bumpers"] == ["front_left", "front_right"]
    assert res["moved"] == pytest.approx(0.83, abs=0.011)    # stopped one step short of the face
    assert w.bumpers_pressed() == ["front_left", "front_right"]


def test_an_off_centre_hit_presses_the_bumper_on_that_side():
    cup = {"name": "cup", "shape": {"type": "point", "position": [-0.15, 1.0], "radius": 0.06}}
    res = SimWorld(_scene(objects=[cup])).move(2.0)
    assert res["collision"] == "cup" and res["bumpers"] == ["front_left"]


def test_reversing_into_a_wall_presses_the_rear_bumpers():
    res = SimWorld(_scene(objects=[_seg("wall", [-5, -0.5], [5, -0.5])])).move(-1.0)
    assert res["collision"] == "wall" and res["bumpers"] == ["rear_left", "rear_right"]


def test_backing_off_or_turning_away_releases_the_bumper():
    w = SimWorld(_scene(objects=[_seg("wall", [-5, 1.0], [5, 1.0])]))
    w.move(2.0)
    w.turn(90)                                   # the wall is now at the robot's flank
    assert w.bumpers_pressed() == []
    w.turn(-90)
    assert w.bumpers_pressed() == ["front_left", "front_right"]
    w.move(-0.2)
    assert w.bumpers_pressed() == []


def test_turn_sign_convention():
    w = SimWorld(_scene())
    w.turn(-90)                 # left = counter-clockwise
    assert w.heading == pytest.approx(270.0)
    w.turn(90)                  # right = clockwise, back to 0
    assert w.heading == pytest.approx(0.0)


# ------------------------------------------------------------------ tool-layer integration
@pytest.fixture
def sim_env(tmp_path, monkeypatch):
    scene_file = tmp_path / "scene.json"
    scene_file.write_text(json.dumps(_scene()))
    monkeypatch.setenv("ROBOT_SIM_SCENE", str(scene_file))
    monkeypatch.setenv("WORLD_STATE_PATH", str(tmp_path / "ws.json"))
    monkeypatch.setenv("TOOL_LOG_PATH", str(tmp_path / "tl.jsonl"))
    # Not the default data/sim_state.json, which a live session's visualizer reads.
    monkeypatch.setenv("SIM_STATE_PATH", str(tmp_path / "sim_state.json"))
    from robot_tools import runtime
    runtime.reset()
    yield
    runtime.reset()


def test_capture_view_returns_text_pov(sim_env):
    from robot_tools import config, tools
    assert config.is_sim() and config.is_stub()
    r = tools.capture_view()
    assert r["ok"] and "image" not in r
    assert "FOV" in r["pov"] and "cup" in r["pov"]


def test_a_collision_is_logged_for_the_evaluator_but_not_told_to_the_agent(sim_env):
    """Like the real robot, the agent learns that a bumper was pressed, never what it hit; the
    world's log keeps the name for scoring."""
    from robot_tools import runtime, tools
    r = tools.move_forward(5.0, stop_within=0)    # cup is in the straight path
    assert r["ok"] is False and "bump" in r
    assert "collision" not in r and "cup" not in json.dumps(r)
    assert runtime.get_sim_world().collisions[-1]["object"] == "cup"


def test_a_sim_bump_reads_like_the_real_robots(sim_env):
    """The same result shape the physical robot's bump sensors produce, so the agents' steering
    describes one interface."""
    from robot_tools import tools
    r = tools.move_forward(5.0, stop_within=0)    # cup centred in the path, 2 m ahead
    assert r["bump"]["bumpers"] == ["front_left", "front_right"]
    assert 1.5 < r["bump"]["estimated_traveled_m"] < 2.0
    assert r["distances_m"]["front_center"] < 0.02            # right up against it
    assert "sensors" not in r                                 # the sim always has sensor data

    # pressed against the cup, the robot won't drive into it again, but it can back away
    again = tools.move_forward(1.0)
    assert again["ok"] is False and again["bump"]["already_pressed"] is True
    assert tools.read_distance_sensors()["bumpers_pressed"] == ["front_left", "front_right"]
    assert tools.move_backward(0.3)["ok"] is True
    assert tools.read_distance_sensors()["bumpers_pressed"] == []


def _log_rows(tmp_path):
    with open(tmp_path / "tl.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_tool_log_records_the_pose_every_call_happened_at(sim_env, tmp_path):
    """Ground truth the log would otherwise lose. A belief like "10° to the left" can only be
    checked against the geometry if we know where the robot was standing when it was written,
    so sim mode records the pose beside every call — without changing what the agent sees."""
    from robot_tools import tools
    tools.turn_right(90)
    tools.move_forward(0.5)
    tools.update_world("cup", {"direction_last_seen": "90° to the left"})

    rows = {r["name"]: r for r in _log_rows(tmp_path)}
    assert rows["turn_right"]["pose"][2] == 90.0                 # heading after the turn
    assert rows["move_forward"]["pose"][0] == pytest.approx(0.5, abs=0.01)
    assert rows["update_world"]["pose"] == rows["move_forward"]["pose"]   # didn't move between
    # the agent is told nothing of the truth: no pose in what the tool returns, only what the
    # robot's own sensors would report
    returned = tools.turn_right(10)
    assert "pose" not in returned
    assert set(returned) == {"ok", "duration_ms", "distances_m"}


def test_capture_view_log_records_what_was_actually_visible(sim_env, tmp_path):
    """So "did it write down what it saw?" is answerable. The agent still receives only the
    text POV; the object list goes to the log only."""
    from robot_tools import tools
    result = tools.capture_view()
    assert "visible" not in result                                # not handed to the agent

    row = next(r for r in _log_rows(tmp_path) if r["name"] == "capture_view")
    # Everything the camera had, walls included — the scorer decides what counts as a thing
    # worth recording, so the log stays a faithful record.
    seen = {v["name"]: v for v in row["visible"]}
    assert set(seen) == {"cup", "wall"}
    assert seen["cup"]["distance"] == pytest.approx(2.0, abs=0.01)
    assert seen["cup"]["bearing"] == pytest.approx(0.0, abs=1)


def test_no_sim_truth_is_logged_outside_sim_mode(tmp_path, monkeypatch):
    """Real/stub runs have no ground truth to record, and must not gain phantom fields."""
    monkeypatch.delenv("ROBOT_SIM_SCENE", raising=False)
    monkeypatch.delenv("ROBOT_SIM", raising=False)
    monkeypatch.setenv("ROBOT_STUB", "1")
    monkeypatch.setenv("WORLD_STATE_PATH", str(tmp_path / "ws.json"))
    monkeypatch.setenv("TOOL_LOG_PATH", str(tmp_path / "tl.jsonl"))
    from robot_tools import runtime, tools
    runtime.reset()
    try:
        tools.turn_right(90)
        row = _log_rows(tmp_path)[0]
        assert "pose" not in row and "visible" not in row
    finally:
        runtime.reset()


def test_bundled_office_kitchen_scene_loads():
    from robot_tools.sim import load_scene
    w = load_scene("office_kitchen")
    assert w.objects and any(o.name == "umbrella" for o in w.objects)
    # umbrella is on the left wall → not visible straight ahead from the start pose
    assert not any(o["name"] == "umbrella" for o in w.visible_objects())


# ------------------------------------------------------------ streamed sensors (sim/robot.py)
# The simulated Misty drives over simulated time and streams its sensors into the same
# SensorMonitor the robot feeds, so these exercise the robot's own movement code.
_NEAR_WALL = {"name": "wall", "shape": {"type": "segment", "points": [[-2, 0.55], [2, 0.55]],
                                        "thickness": 0.1}}          # face 0.5 m ahead


@pytest.fixture
def near_wall_env(tmp_path, monkeypatch):
    """The robot at the origin facing a wall whose face is 0.5 m ahead (a 0.38 m gap)."""
    scene_file = tmp_path / "scene.json"
    scene_file.write_text(json.dumps(_scene(objects=[_NEAR_WALL])))
    monkeypatch.setenv("ROBOT_SIM_SCENE", str(scene_file))
    monkeypatch.setenv("WORLD_STATE_PATH", str(tmp_path / "ws.json"))
    monkeypatch.setenv("TOOL_LOG_PATH", str(tmp_path / "tl.jsonl"))
    monkeypatch.setenv("SIM_STATE_PATH", str(tmp_path / "sim_state.json"))
    from robot_tools import runtime
    runtime.reset()
    yield
    runtime.reset()


def _gap_ahead():
    from robot_tools import runtime
    return runtime.get_sim_world().range_readings()["front_center"]


def test_the_default_stop_keeps_the_robot_off_what_its_sensors_see(near_wall_env):
    """A forward move stops itself once a front sensor reads closer than stop_within (0.15 m):
    the robot never touches the wall, coasting a little past that reading before it stops."""
    from robot_tools import runtime, tools
    r = tools.move_forward(1.0)
    assert r["ok"] is False and "bump" not in r
    assert r["obstacle"]["sensor"] in ("front_left", "front_center", "front_right")
    assert r["obstacle"]["distance_m"] < 0.15 and r["obstacle"]["stop_within"] == 0.15
    assert 0.0 < _gap_ahead() < 0.15                       # stopped short, never touched
    assert runtime.get_sim_world().collisions == []
    # what the result reports is where the robot ended up, not the reading that stopped it
    assert r["distances_m"]["front_center"] == pytest.approx(_gap_ahead(), abs=0.012)


def test_a_move_that_would_start_inside_stop_within_is_not_sent(near_wall_env):
    from robot_tools import runtime, tools
    tools.move_forward(1.0)                                 # stops just short of the wall
    pose = list(runtime.get_sim_world().robot_pos)
    r = tools.move_forward(0.5)
    assert r["obstacle"]["already_within"] is True and r["duration_ms"] == 0
    assert runtime.get_sim_world().robot_pos == pose
    touch = tools.move_forward(0.5, stop_within=0)          # 0 turns the check off
    assert touch["ok"] is False and touch["bump"]["bumpers"] == ["front_left", "front_right"]


def test_backing_up_watches_the_back_sensor_and_the_rear_bumpers(near_wall_env):
    from robot_tools import tools
    tools.turn_left(180)                                    # the wall is now behind
    r = tools.move_backward(1.0)
    assert r["obstacle"]["sensor"] == "back"
    r = tools.move_backward(0.5, stop_within=0)
    assert r["bump"]["bumpers"] == ["rear_left", "rear_right"]


def test_each_range_sensor_reports_about_three_times_a_second(sim_env):
    """Misty's measured rate: about one reading every 0.32 s per sensor, each on its own cycle,
    with status 102 when nothing is in range (as here, before the robot has moved)."""
    from collections import Counter
    from robot_tools import runtime
    monitor = runtime.get_sensors()
    fed = []
    feed = monitor.feed
    monitor.feed = lambda raw: (fed.append(json.loads(raw)), feed(raw))
    monitor.idle(3.16)                                      # ten reading periods
    readings = [m["message"] for m in fed if "distanceInMeters" in m["message"]]
    per_sensor = Counter(m["sensorId"] for m in readings)
    assert set(per_sensor) == {"toffl", "toffc", "toffr", "tofr"}
    assert all(9 <= n <= 11 for n in per_sensor.values()), per_sensor
    assert {m["status"] for m in readings} == {102}


def test_turns_take_the_exact_angle_asked(sim_env):
    from robot_tools import runtime, tools
    tools.turn_left(90)
    world = runtime.get_sim_world()
    assert world.heading == pytest.approx(270.0)
    assert world.action_history[-1] == "turned left 90°"


def test_simulated_time_costs_no_real_time(sim_env):
    """A drive runs for its calibrated time on the simulated clock, in milliseconds of real
    time, so evaluation runs aren't slowed down to the robot's pace."""
    import time
    from robot_tools import runtime, tools
    start_sim, start = runtime.get_sensors().now(), time.perf_counter()
    tools.move_forward(1.5)
    tools.turn_right(180)
    simulated = runtime.get_sensors().now() - start_sim
    assert simulated >= (runtime.calc_drive_time(1.5) + runtime.calc_turn_time(180)) / 1000
    assert time.perf_counter() - start < 1.0

