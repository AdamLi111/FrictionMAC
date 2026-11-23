"""
The log viewer (scripts/log_viewer.py) and the structured trace it reads (agent_runtime/narrative).

Sessions are written through the real NarrativeLog, exactly as hw_console and the evaluation runner
write them, then read back: from the JSON trace, and from the story text alone (as for a log that
predates the trace). No agents, no robot, no API.
"""
import json
import time
import urllib.request

import pytest

from agent_runtime import narrative
from scripts import log_viewer as lv

V2 = {"world-manager": "director", "action-manager": "director", "dialogue-manager": "director",
      "object-lookup": "world-manager", "map": "world-manager", "navigation": "action-manager",
      "expression": "action-manager", "regular-utterance": "dialogue-manager",
      "friction": "dialogue-manager"}


def _write_session(prefix):
    """A two-turn V2 session, through the same calls TurnEngine and the front ends make."""
    story = narrative.NarrativeLog(f"{prefix}_story.txt")
    story.header("MISTY SESSION test", {"architecture": "v2 (Managers)", "mode": "SIM"},
                 arch="v2", hierarchy=V2)
    story.user_turn(1, "go to the red cup")
    story.event({"kind": "think", "agent": "director", "text": "Find the cup first."})
    story.event({"kind": "delegate", "agent": "director", "to": "world-manager",
                 "description": "find the cup", "prompt": "Locate the red cup.",
                 "background": True})
    story.event({"kind": "report", "agent": "world-manager",
                 "content": "Async agent launched successfully.\nagentId: abc"})
    story.event({"kind": "think", "agent": "world-manager", "text": "Ask object-lookup."})
    story.event({"kind": "delegate", "agent": "world-manager", "to": "object-lookup",
                 "description": "look", "prompt": "Look for a red cup.", "background": False})
    story.event({"kind": "tool", "agent": "object-lookup", "name": "mcp__robot__capture_view",
                 "input": {}})
    story.event({"kind": "tool_result", "agent": "object-lookup",
                 "content": [{"type": "text", "text": "a red cup, 1.2 m ahead"}]})
    story.event({"kind": "report", "agent": "object-lookup",
                 "content": "Red cup 1.2 m ahead.\nagentId: x1\n<usage>tokens: 5</usage>"})
    story.event({"kind": "tool", "agent": "world-manager", "name": "SendMessage",
                 "input": {"to": "main", "message": "Cup found, 1.2 m ahead.",
                           "summary": "cup found"}})
    story.event({"kind": "speak", "agent": "dialogue", "text": "Heading to the red cup.",
                 "friction_type": "none"})
    story.event({"kind": "task_end", "agent": "world-manager", "status": "completed"})
    story.turn_end(subtype="success", seconds=12.0)
    story.user_turn(2, "thanks")
    story.event({"kind": "text", "agent": "director", "text": "Nothing to do."})
    story.no_speech()
    story.turn_end(subtype="success", seconds=2.0)
    story.footer("SESSION ENDED — 2 user turn(s).")
    story.close()


def _write_hooks(prefix, t0):
    rows = [  # regular-utterance never shows up in the stream; map did nothing visible either
        {"event": "subagent_start", "agent_type": "regular-utterance", "agent_id": "r1"},
        {"event": "pre_tool_use", "tool": "mcp__robot__speak", "agent_type": "regular-utterance",
         "agent_id": "r1", "input": {"text": "Heading to the red cup.", "friction_type": "none"}},
        {"event": "pre_tool_use", "tool": "mcp__robot__update_world", "agent_type": "map",
         "agent_id": "m1", "input": {"object": "red cup", "info": {"room": "kitchen"}}},
        {"event": "pre_tool_use", "tool": "mcp__robot__capture_view",
         "agent_type": "object-lookup", "agent_id": "o1", "input": {}},
    ]
    with open(f"{prefix}_events.jsonl", "w", encoding="utf-8") as f:
        for k, row in enumerate(rows):
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 + 1 + k)) + "+00:00"
            f.write(json.dumps({"timestamp": stamp, **row}) + "\n")


@pytest.fixture
def session(tmp_path):
    prefix = tmp_path / "hw_session_test"
    _write_session(prefix)
    return prefix


def test_the_story_writer_also_writes_a_json_trace(session):
    """Regression: an event's own `kind` key once collided with the writer's signature and
    crashed the session at its first agent event."""
    lines = [json.loads(line) for line in open(f"{session}_trace.jsonl", encoding="utf-8")]
    assert lines[0]["type"] == "header" and lines[0]["hierarchy"] == V2
    events = [line for line in lines if line["type"] == "event"]
    assert events[0] == {**events[0], "kind": "think", "agent": "director", "turn": 1}
    assert all("t" in line for line in lines)
    assert [line["type"] for line in lines][-1] == "footer"
    assert "Find the cup first." in open(f"{session}_story.txt", encoding="utf-8").read()


def _steps(model, n):
    return [(s["kind"], s.get("from") or s.get("agent"), s.get("to"))
            for s in model["turns"][n - 1]["steps"]]


def test_the_viewer_turns_a_trace_into_numbered_steps(session):
    model = lv.build_model(lv.Run(session))
    assert model["format"] == "trace" and model["arch"] == "v2" and model["ended"]
    assert model["hierarchy"] == V2
    turn = model["turns"][0]
    assert turn["user"] == "go to the red cup" and [s["i"] for s in turn["steps"]] == list(
        range(1, len(turn["steps"]) + 1))
    assert _steps(model, 1) == [
        ("ask", "user", "director"), ("think", "director", None),
        ("delegate", "director", "world-manager"), ("think", "world-manager", None),
        ("delegate", "world-manager", "object-lookup"), ("tool", "object-lookup", "robot"),
        ("report", "object-lookup", "world-manager"),         # back to whoever delegated
        ("message", "world-manager", "director"),             # SendMessage to "main"
        ("speak", "?", "user"), ("end", "world-manager", None)]
    steps = {s["i"]: s for s in turn["steps"]}
    assert steps[3]["background"] is True                     # from the launch acknowledgement
    assert steps[6]["result"] == "a red cup, 1.2 m ahead"
    assert steps[7]["text"] == "Red cup 1.2 m ahead."         # trailers stripped
    assert model["turns"][1]["silent"] is True


def test_an_old_log_without_a_trace_reads_the_same_from_its_story(session):
    import os
    os.remove(f"{session}_trace.jsonl")
    model = lv.build_model(lv.Run(session))
    assert model["format"] == "story" and model["arch"] == "v2"
    kinds = [k for k, _, _ in _steps(model, 1)]
    assert kinds == ["ask", "think", "delegate", "think", "delegate", "tool", "report",
                     "message", "speak", "end"]
    turn = model["turns"][0]
    assert turn["user"] == "go to the red cup" and turn["clock"]
    report = next(s for s in turn["steps"] if s["kind"] == "report")
    assert report["from"] == "object-lookup" and report["to"] == "world-manager"
    assert next(s for s in turn["steps"] if s["kind"] == "delegate")["text"] == "Locate the red cup."


def _second_turn_a_minute_later(prefix):
    """The writer ran both turns within a millisecond; real turns are seconds apart."""
    path = f"{prefix}_trace.jsonl"
    lines = [json.loads(line) for line in open(path, encoding="utf-8")]
    for line in lines:
        if line.get("turn") == 2 or line["type"] == "footer":
            line["t"] += 60
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)
    return lines[1]["t"]


def test_the_hook_log_names_hidden_speakers_and_fills_in_hidden_agents(session):
    _write_hooks(session, _second_turn_a_minute_later(session))
    model = lv.build_model(lv.Run(session))
    turn = model["turns"][0]
    speak = next(s for s in turn["steps"] if s["kind"] == "speak")
    assert speak["from"] == "regular-utterance" and not speak.get("guessed")
    hidden = [s for s in turn["steps"] if s.get("source") == "hooks"]
    assert ("tool", "map") in [(s["kind"], s["from"]) for s in hidden]
    assert ("delegate", "dialogue-manager", "regular-utterance") in [
        (s["kind"], s["from"], s["to"]) for s in hidden]
    # object-lookup is in the stream already, so its hook-logged call isn't added twice
    assert sum(s["kind"] == "tool" and s.get("from") == "object-lookup"
               for s in turn["steps"]) == 1
    assert [s["i"] for s in turn["steps"]] == list(range(1, len(turn["steps"]) + 1))


def test_a_speaker_is_named_by_its_words_even_when_its_time_falls_in_another_turn(session):
    """The hook log's clock and the trace's can straddle a turn boundary; the words still match."""
    t1 = _second_turn_a_minute_later(session)
    path = f"{session}_trace.jsonl"
    lines = [json.loads(line) for line in open(path, encoding="utf-8")]
    speak = next(line for line in lines if line.get("kind") == "speak")
    lines.remove(speak)
    speak["turn"], speak["t"] = 2, t1 + 61                # spoken in turn 2 ...
    lines.insert(-1, speak)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)
    _write_hooks(session, t1)                             # ... but logged during turn 1
    model = lv.build_model(lv.Run(session))
    spoken = [s for t in model["turns"] for s in t["steps"] if s["kind"] == "speak"]
    assert [(s["from"], s.get("source")) for s in spoken] == [("regular-utterance", "log")]


def test_the_server_serves_the_page_the_runs_and_a_model(session):
    url, stop = lv.start(session.parent, port=8990)
    try:
        page = urllib.request.urlopen(url + "/", timeout=5).read().decode()
        assert "Agent log viewer" in page
        runs = json.loads(urllib.request.urlopen(url + "/api/runs", timeout=5).read())
        assert [r["name"] for r in runs["runs"]] == ["hw_session_test"]
        model = json.loads(urllib.request.urlopen(
            url + "/api/model?run=" + runs["runs"][0]["id"], timeout=5).read())
        assert len(model["turns"]) == 2
    finally:
        stop()


def test_the_sensor_log_is_read_a_complete_line_at_a_time(session):
    path = f"{session}_sensors.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"t": 1.0, "k": "range", "s": "back", "d": 0.5, "st": 0, "ok": true}\n')
        f.write('{"t": 1.3, "k": "bump", "s": "front_le')              # still being written
    rows, offset = lv.read_sensor_rows(path)
    assert [r["s"] for r in rows] == ["back"]
    with open(path, "a", encoding="utf-8") as f:
        f.write('ft", "on": true}\n')
    rows, offset = lv.read_sensor_rows(path, offset)
    assert rows == [{"t": 1.3, "k": "bump", "s": "front_left", "on": True}]
    assert lv.read_sensor_rows(path, offset) == ([], offset)
    model = lv.build_model(lv.Run(session))
    assert model["sensors"] is True
    url, stop = lv.start(session.parent, port=8995)
    try:
        run_id = json.loads(urllib.request.urlopen(url + "/api/runs", timeout=5).read())["runs"][0]["id"]
        got = json.loads(urllib.request.urlopen(f"{url}/api/sensors?run={run_id}", timeout=5).read())
        assert [r["k"] for r in got["rows"]] == ["range", "bump"]
    finally:
        stop()


def test_every_run_starts_with_an_empty_memory_file(tmp_path):
    from robot_tools.world_state import WorldState, clear_worldstate
    path = tmp_path / "world.json"
    WorldState(str(path)).update_world("mug", {"room": "kitchen"})
    clear_worldstate(path)
    assert path.read_text().strip() == "{}"
    clear_worldstate(tmp_path / "never_written.json")       # also fine when there was none
    assert (tmp_path / "never_written.json").read_text().strip() == "{}"
