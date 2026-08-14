"""
Tests for the simulation backend (robot_tools.sim) and its integration behind the MCP stub.

Covers the ground-truth physics (wall-aware collision, narrow FOV, wall occlusion) and the
tool-layer wiring (capture_view returns a text POV; movement surfaces collisions) in sim mode.
"""
import json
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


def test_move_forward_reports_collision(sim_env):
    from robot_tools import tools
    r = tools.move_forward(5.0)               # cup is in the straight path
    assert r["ok"] is False and r["collision"] == "cup"


def test_bundled_office_kitchen_scene_loads():
    from robot_tools.sim import load_scene
    w = load_scene("office_kitchen")
    assert w.objects and any(o.name == "umbrella" for o in w.objects)
    # umbrella is on the left wall → not visible straight ahead from the start pose
    assert not any(o["name"] == "umbrella" for o in w.visible_objects())
