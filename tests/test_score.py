"""
Tests for episode scoring and the run summary (evaluation.score).

Two things are checked: that outcome signals read ground truth rather than the robot's own
claims, and that world-model accuracy scores the agents' free-form beliefs the way they are
actually written ("10° left-front", "room": "unknown", `far_chair` for a `chair`).
"""
import json

import pytest

from evaluation import score
from evaluation.tasks import Task
from robot_tools.sim.world_model import SimWorld


def _scene():
    """Two rooms, a cup in each — so a name can be right in one room and wrong in the other."""
    return {
        "name": "t",
        "robot": {"position": [0.0, 0.0], "heading": 0},
        "objects": [
            {"name": "red cup", "shape": {"type": "point", "position": [0.0, 2.0],
                                          "radius": 0.05}, "room": "office"},
            {"name": "blue cup", "shape": {"type": "point", "position": [2.0, 0.0],
                                           "radius": 0.05}, "room": "kitchen"},
            {"name": "chair", "shape": {"type": "rect", "min": [-1.5, 1.0],
                                        "max": [-1.0, 1.5]}, "room": "office"},
            {"name": "wall", "shape": {"type": "segment", "points": [[-3, 4], [3, 4]],
                                       "thickness": 0.1}, "room": "office"},
        ],
    }


def _task(**kw):
    base = {"task_id": "t1", "name": "t", "scene": "office_kitchen", "goal": "g",
            "category": "referential_ambiguity", "target": "red cup"}
    base.update(kw)
    return Task(**base)


def _record(world, **kw):
    rec = {"final_world": world.snapshot(), "collisions": [], "user_said_done": True,
           "robot_utterances": 1, "dialogue": [{"spoke": True}], "duration_s": 10.0,
           "user_turns": 2, "category": "referential_ambiguity", "end_reason": "user_ended"}
    rec.update(kw)
    return rec


# ------------------------------------------------------------------------ bearing parsing
@pytest.mark.parametrize("text, expected", [
    ("10° left-front", -10.0),
    ("20° to the right", 20.0),
    ("directly ahead", 0.0),
    ("straight ahead on the table", 0.0),
    ("spanning 20° left to 20° right", 0.0),       # extended object → midpoint
    ("spanning 10° left to 30° left", -20.0),
    ("on my left", -45.0),                          # coarse, no number
    ("behind me", 180.0),
    ("", None),                                     # nothing said → not scored as wrong
    ("on the table, nearby", None),                 # distance only, no direction
])
def test_parse_bearing_handles_how_agents_actually_write_directions(text, expected):
    assert score.parse_bearing(text) == expected


# --------------------------------------------------------------------------- world model
def test_room_is_scored_against_the_objects_real_room():
    world = SimWorld(_scene())
    beliefs = {
        "red cup": {"room": "office"},                 # right
        "blue cup": {"room": "office"},                 # wrong — it's in the kitchen
        "chair": {"room": "unknown"},                   # unstated → neither right nor wrong
    }
    wm = score.world_model_accuracy(beliefs, world, [])
    assert (wm["room_correct"], wm["room_wrong"], wm["room_unstated"]) == (1, 1, 1)
    assert wm["room_accuracy"] == 0.5


def test_direction_is_scored_against_the_pose_at_the_time_it_was_recorded():
    """A bearing is meaningless without the pose it was taken from — the tool layer logs it."""
    world = SimWorld(_scene())
    # Robot at the origin, turned 45° right: the red cup at (0,2) is then 45° to its LEFT.
    rows = [{"name": "update_world", "args": {"object": "red cup"}, "pose": [0.0, 0.0, 45.0]},
            {"name": "update_world", "args": {"object": "blue cup"}, "pose": [0.0, 0.0, 45.0]}]
    beliefs = {
        "red cup": {"direction_last_seen": "45° to the left"},    # true
        "blue cup": {"direction_last_seen": "40° to the left"},   # actually 45° RIGHT
    }
    wm = score.world_model_accuracy(beliefs, world, rows)
    assert wm["direction_correct"] == 1 and wm["direction_wrong"] == 1
    by = {e["recorded"]: e for e in wm["per_entry"]}
    assert by["red cup"]["direction_truth"] == pytest.approx(-45.0, abs=1)
    assert by["blue cup"]["direction"] == "wrong"


def test_direction_without_a_logged_pose_is_unscorable_not_wrong():
    world = SimWorld(_scene())
    wm = score.world_model_accuracy({"red cup": {"direction_last_seen": "10° left"}}, world, [])
    assert wm["per_entry"][0]["direction"] == "unscorable"
    assert wm["direction_accuracy"] is None          # nothing was scored, so no score


def test_entries_referring_to_nothing_real_are_flagged():
    world = SimWorld(_scene())
    wm = score.world_model_accuracy({"teapot": {"room": "office"}}, world, [])
    assert wm["ungrounded"] == ["teapot"]
    assert wm["grounded"] == 0
    assert wm["room_correct"] == 0                   # ungrounded entries aren't room-scored


def test_invented_names_for_real_objects_are_flagged_separately():
    """`far_chair` does refer to a real chair, but it is not a name the scene uses."""
    world = SimWorld(_scene())
    wm = score.world_model_accuracy({"far_chair": {"room": "office"}}, world, [])
    assert wm["ungrounded"] == [] and wm["grounded"] == 1
    assert wm["invented_names"] == ["far_chair"]
    assert wm["per_entry"][0]["matched_object"] == "chair"


def test_recall_measures_what_was_seen_but_never_written_down():
    world = SimWorld(_scene())
    rows = [{"name": "capture_view", "visible": [{"name": "red cup"}, {"name": "chair"},
                                                 {"name": "wall"}]}]
    wm = score.world_model_accuracy({"red cup": {}}, world, rows)
    assert wm["objects_seen"] == 2                   # walls don't count as things to record
    assert wm["objects_seen_but_unrecorded"] == ["chair"]
    assert wm["recall"] == 0.5


# ------------------------------------------------------------------------------- outcome
def test_reached_target_uses_ground_truth_not_the_robots_claim():
    world = SimWorld(_scene())
    world.move(1.5)                                   # robot centre to (0, 1.5); cup at (0, 2)
    near = score.score_episode(_record(world), _task())
    assert near["reached_target"] is True
    assert near["target_distance_m"] == pytest.approx(0.45, abs=0.05)

    far = SimWorld(_scene())                          # never moved: 1.95 m away
    assert score.score_episode(_record(far), _task())["reached_target"] is False


def test_contact_with_the_target_counts_as_arrival():
    world = SimWorld(_scene())
    rec = _record(world, collisions=["red cup"])
    assert score.score_episode(rec, _task())["reached_target"] is True


def test_reached_target_is_none_when_the_task_has_no_target():
    world = SimWorld(_scene())
    s = score.score_episode(_record(world), _task(target=None))
    assert s["reached_target"] is None and s["target_distance_m"] is None


def test_user_verdict_and_silence_are_recorded():
    world = SimWorld(_scene())
    rec = _record(world, user_said_done=False, robot_utterances=0,
                  dialogue=[{"spoke": False}, {"spoke": False}])
    s = score.score_episode(rec, _task())
    assert s["user_judged_success"] is False
    assert s["spoke_at_all"] is False and s["silent_turns"] == 2


# ------------------------------------------------------------------------------- rollup
def test_summary_aggregates_success_reasons_and_timing():
    eps = [
        {"task_id": "a", "category": "cat1", "end_reason": "user_ended", "duration_s": 100.0,
         "user_turns": 2, "score": {"user_judged_success": True, "reached_target": True,
                                    "collisions": 0, "spoke_at_all": True,
                                    "world_model": {"entries": 2, "room_accuracy": 1.0,
                                                    "direction_accuracy": 0.5, "recall": 1.0,
                                                    "ungrounded": [], "invented_names": []}}},
        {"task_id": "b", "category": "cat1", "end_reason": "turn_timeout", "duration_s": 300.0,
         "user_turns": 4, "score": {"user_judged_success": False, "reached_target": False,
                                    "collisions": 6, "spoke_at_all": False,
                                    "world_model": {"entries": 1, "room_accuracy": 0.0,
                                                    "direction_accuracy": None, "recall": 0.0,
                                                    "ungrounded": ["teapot"],
                                                    "invented_names": []}}},
    ]
    s = score.summarise(eps)
    assert s["episodes"] == 2
    assert s["user_judged_success"] == 1 and s["user_judged_success_rate"] == 0.5
    assert s["reached_target_rate"] == 0.5
    assert s["end_reasons"] == {"user_ended": 1, "turn_timeout": 1}
    assert s["mean_duration_s"] == 200.0 and s["mean_user_turns"] == 3.0
    assert s["silent_episodes"] == 1
    assert s["world_model"]["room_accuracy"] == 0.5        # mean over episodes that scored one
    assert s["world_model"]["direction_accuracy"] == 0.5   # the None episode is skipped
    assert s["world_model"]["ungrounded_total"] == 1
    assert s["by_category"]["cat1"] == {"episodes": 2, "user_judged_success": 1,
                                        "reached": 1, "reach_scored": 2}

    text = score.format_summary(s, arch="v4")
    assert "user-judged success      1/2  (50%)" in text
    assert "turn_timeout" in text and "mean duration            200.0s" in text


def test_summary_of_nothing_is_not_a_crash():
    assert score.summarise([]) == {"episodes": 0}
    assert score.format_summary({"episodes": 0}) == "no episodes"
