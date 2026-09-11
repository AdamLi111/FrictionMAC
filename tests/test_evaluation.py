"""
Tests for the experiment layer: the task list, the omniscient scene view, and the simulated
user's plumbing.

No API calls and no agent: the simulated user is driven with a fake client, so what is checked
is the conversation bookkeeping (roles, the [DONE] sentinel, transcript) rather than what the
model chooses to say.
"""
import pytest

from evaluation import tasks as task_mod
from robot_tools.sim import describe_world, load_scene
from robot_tools.sim.omniscient import _doorways
from robot_tools.sim.world_model import SimWorld


# ------------------------------------------------------------------------------- task list
def test_bundled_task_list_is_consistent_with_the_scenes():
    """Every task names a scene that loads, and a `target` that exists in it."""
    all_tasks = task_mod.load_tasks()
    assert all_tasks
    assert task_mod.validate(all_tasks) == []


def test_every_task_carries_a_goal_and_a_category():
    for t in task_mod.load_tasks():
        assert t.goal.strip(), t.task_id
        assert t.category != "uncategorized", t.task_id
        assert t.max_user_turns > 0, t.task_id


def test_task_requires_scene_and_goal():
    with pytest.raises(ValueError):
        task_mod.Task.from_dict({"task_id": "x", "name": "no goal", "scene": "office_kitchen"})


def test_select_filters_are_anded_and_capped():
    ts = task_mod.load_tasks()
    amb = task_mod.select(ts, categories=["referential_ambiguity"])
    assert amb and all(t.category == "referential_ambiguity" for t in amb)
    one = task_mod.select(ts, categories=["referential_ambiguity"], limit=1)
    assert len(one) == 1
    # a filter that can't match anything yields nothing rather than everything
    assert task_mod.select(ts, task_ids=["amb_001"], categories=["expression"]) == []


def test_unknown_task_fields_are_kept_not_dropped():
    t = task_mod.Task.from_dict({"task_id": "x", "scene": "office_kitchen", "goal": "g",
                                 "note": "keep me"})
    assert t.extra == {"note": "keep me"}


# --------------------------------------------------------------------- omniscient scene view
def _two_room_scene():
    """Two rooms split by a wall with a 1 m doorway at y=1.5..2.5, one object each side."""
    return SimWorld({
        "name": "t",
        "robot": {"position": [-1.0, 0.0], "heading": 0},
        "objects": [
            {"name": "red cup", "shape": {"type": "point", "position": [-1.0, 2.0],
                                          "radius": 0.05},
             "room": "office", "properties": {"color": "red"}},
            {"name": "fridge", "shape": {"type": "rect", "min": [2.0, 3.0], "max": [2.6, 3.6]},
             "room": "kitchen"},
            {"name": "wall", "shape": {"type": "segment", "points": [[0, -1], [0, 1.5]],
                                       "thickness": 0.1}},
            {"name": "wall", "shape": {"type": "segment", "points": [[0, 2.5], [0, 4]],
                                       "thickness": 0.1}},
        ],
    })


def test_omniscient_view_shows_every_room_and_object():
    """The user sees the whole scene — including what the robot's narrow FOV cannot."""
    world = _two_room_scene()
    text = describe_world(world)
    assert "office" in text and "kitchen" in text
    assert "red cup" in text and "fridge" in text
    assert "color: red" in text                       # properties are exposed to the user
    # ...while the robot's own POV has the cup ahead of it but not the fridge next door.
    seen = {v["name"] for v in world.visible_objects()}
    assert "red cup" in seen and "fridge" not in seen


def test_omniscient_view_reports_the_robot_pose_and_camera_cone():
    world = _two_room_scene()
    assert "In the robot's camera view right now: red cup" in describe_world(world)
    world.turn(180)                                   # now facing away from everything
    assert "camera view right now: nothing" in describe_world(world)


def test_omniscient_view_tracks_movement():
    world = _two_room_scene()
    before = describe_world(world)
    world.move(1.0)
    after = describe_world(world)
    assert before != after
    assert "1.0 m from the robot" in after            # cup is 1 m away after driving 1 m

def test_omniscient_view_reports_collisions():
    world = _two_room_scene()
    world.turn(90)
    world.move(3.0)                                   # into the wall segment
    assert "bumped into" in describe_world(world)


def test_snapshot_round_trips_history_so_the_user_keeps_seeing_collisions():
    """The harness re-reads the live world every turn (the sim lives in the MCP server process),
    so a snapshot has to carry what the robot has already done — otherwise the user would learn
    about a collision for exactly one turn and then forget it."""
    world = _two_room_scene()
    world.turn(90)
    world.move(3.0)                                   # bump the wall
    reloaded = SimWorld(world.snapshot())
    assert [c["object"] for c in reloaded.collisions] == ["wall"]
    assert reloaded.action_history == world.action_history
    assert "bumped into" in describe_world(reloaded)
    # A pristine scene file carries neither key, so it starts clean.
    fresh = load_scene("office_kitchen")
    assert fresh.collisions == [] and fresh.action_history == []


def test_doorways_are_recovered_from_the_wall_gaps():
    """A doorway is not an object anywhere — it's the gap between two collinear walls."""
    doors = _doorways(_two_room_scene())
    assert len(doors) == 1
    assert doors[0]["width"] == pytest.approx(1.0)
    assert doors[0]["centre"] == pytest.approx((0.0, 2.0))


def test_bundled_scene_doorway_detection():
    doors = _doorways(load_scene("office_kitchen"))
    assert len(doors) == 1 and doors[0]["width"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- simulated user
class _FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0)
        return type("R", (), {
            "content": [type("B", (), {"type": "text", "text": text})()],
            "stop_reason": "end_turn",
        })()


class _FakeClient:
    def __init__(self, replies):
        self.messages = _FakeMessages(replies)


def _sim_user_module():
    # The simulated user lives in the agent env (it needs `anthropic`); the robot env doesn't
    # have it, so these tests skip there rather than fail.
    return pytest.importorskip("evaluation.simulated_user")


def _user(replies, task_id="amb_001"):
    mod = _sim_user_module()
    task = task_mod.select(task_mod.load_tasks(), task_ids=[task_id])[0]
    return mod, mod.SimulatedUser(task, describe_world(load_scene(task.scene)),
                                  client=_FakeClient(replies))


def test_user_prompt_carries_the_goal_and_the_scene_but_the_robot_never_sees_them():
    _, user = _user(["hi"])
    assert user.task.goal in user.system
    assert "red cup" in user.system                   # the omniscient scene
    assert "underspecified" in user.system.lower()    # the constraint the experiment needs


def test_user_conversation_uses_assistant_role_for_its_own_lines():
    """The user LLM speaks as `assistant`; robot speech arrives as `user` input."""
    _, user = _user(["Go look at the cup.", "The red one."])
    user.opening_utterance()
    user.reply(["Which cup did you mean?"], "scene")
    roles = [m["role"] for m in user.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert 'The robot said: "Which cup did you mean?"' in user.messages[2]["content"]


def test_user_ends_the_episode_on_the_done_sentinel():
    done = _sim_user_module().DONE
    _, user = _user(["Go look at the cup.", f"Great, thanks!\n{done}"])
    assert user.opening_utterance() == "Go look at the cup."
    assert user.reply(["I'm at the red cup."], "scene") is None
    assert user.done is True
    assert user.transcript[-1]["text"] == "Great, thanks!"      # sentinel stripped
    assert user.reply(["anything else?"], "scene") is None      # stays ended


def test_user_is_told_when_the_robot_said_nothing():
    _, user = _user(["Go look at the cup.", "Hello? Did you hear me?"])
    user.opening_utterance()
    user.reply([], "scene")
    assert "did not say anything" in user.messages[2]["content"]


def test_user_utterances_are_stripped_of_labels_and_quotes():
    _, user = _user(['You: "Go look at the cup."'])
    assert user.opening_utterance() == "Go look at the cup."
