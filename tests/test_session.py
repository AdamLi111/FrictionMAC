"""
Tests for the turn engine's speech accounting (agent_runtime.session).

The regression these exist for: in V2 the speaker is a GRANDCHILD of the top-level agent
(Director → dialogue-manager → regular-utterance) and a grandchild's tool calls never reach the
top-level message stream. Detecting speech from the stream therefore reported "(no spoken
reply)" on every turn of a V2 run while the robot was in fact talking — and the simulated user,
told it had been answered with silence, escalated to "can you hear me?".

Speech is now counted from the MCP server's tool log, which records every call at any depth.
No SDK and no API here: the engine is fed the log directly.
"""
import json

import pytest

# The turn engine imports the Agent SDK, which only the agent env (.venv-agent) has; in the
# robot env these tests skip rather than fail collection.
pytest.importorskip("claude_agent_sdk")

from agent_runtime import session as sess  # noqa: E402  (must follow the skip guard)


def _log(path, *entries):
    """Append tool-log lines exactly as robot_tools.logging_jsonl writes them."""
    with open(path, "a", encoding="utf-8") as f:
        for name, args in entries:
            f.write(json.dumps({"timestamp": "2026-09-22T04:56:00Z", "name": name,
                                "args": args, "result": {"ok": True}}) + "\n")


def _speak(text, friction="none"):
    return ("speak", {"text": text, "friction_type": friction})


@pytest.fixture
def tool_log(tmp_path):
    return tmp_path / "tools.jsonl"


# ------------------------------------------------------------------------------- SpeechTail
def test_tail_returns_only_speech_and_only_once(tool_log):
    tail = sess.SpeechTail(tool_log)
    assert tail.drain() == []                       # no file yet — a session that hasn't run

    _log(tool_log, ("capture_view", {}), _speak("Hello."), ("move_forward", {"distance": 1}))
    assert tail.drain() == [{"text": "Hello.", "friction_type": "none"}]
    assert tail.drain() == []                       # consumed, not replayed

    _log(tool_log, _speak("Which cup?", "probing"))
    assert tail.drain() == [{"text": "Which cup?", "friction_type": "probing"}]


def test_tail_ignores_a_partially_written_line(tool_log):
    """The server may be mid-write; half a line must not be parsed or skipped."""
    _log(tool_log, _speak("Complete."))
    with open(tool_log, "a", encoding="utf-8") as f:
        f.write('{"timestamp": "…", "name": "speak", "args": {"text": "Half')
    tail = sess.SpeechTail(tool_log)
    assert tail.drain() == [{"text": "Complete.", "friction_type": "none"}]

    with open(tool_log, "a", encoding="utf-8") as f:   # the rest of that line arrives
        f.write('written.", "friction_type": "none"}}\n')
    assert tail.drain() == [{"text": "Halfwritten.", "friction_type": "none"}]


def test_tail_survives_a_corrupt_line(tool_log):
    with open(tool_log, "a", encoding="utf-8") as f:
        f.write('{"name": "speak", not json at all\n')
    _log(tool_log, _speak("Still here."))
    assert sess.SpeechTail(tool_log).drain() == [{"text": "Still here.",
                                                  "friction_type": "none"}]


# ------------------------------------------------------------------------------- TurnEngine
def _engine(tool_log, **kw):
    heard, traced = [], []
    engine = sess.TurnEngine(lambda level, text: None,
                             lambda text, friction: heard.append((text, friction)),
                             trace=traced.append, speech_log=tool_log, **kw)
    return engine, heard, traced


def test_speech_from_a_depth_the_stream_never_shows_is_still_counted(tool_log):
    """The V2 regression: nothing about this utterance appears in the message stream."""
    engine, heard, traced = _engine(tool_log)
    turn = engine._new_turn()

    _log(tool_log, _speak("I see a red cup and a blue cup. Which one?", "probing"))
    engine._drain_speech(turn)

    assert turn["spoke"] is True
    assert turn["speech"] == [{"text": "I see a red cup and a blue cup. Which one?",
                              "friction_type": "probing"}]
    assert heard == [("I see a red cup and a blue cup. Which one?", "probing")]
    assert [t["kind"] for t in traced] == ["speak"]          # and it reaches the story log


def test_an_utterance_is_never_counted_twice(tool_log):
    """V1/V4 surface the speak block in the stream AND log it. One utterance, one count."""
    engine, heard, traced = _engine(tool_log)
    turn = engine._new_turn()

    engine.process(_assistant_speak("Done."), turn)          # stream sees it
    _log(tool_log, _speak("Done."))                          # the robot logs the same call
    engine._drain_speech(turn)

    assert turn["speech"] == [{"text": "Done.", "friction_type": "none"}]
    assert heard == [("Done.", "none")]
    assert [t["kind"] for t in traced].count("speak") == 1


def test_the_same_sentence_said_twice_is_counted_twice(tool_log):
    engine, heard, _ = _engine(tool_log)
    turn = engine._new_turn()
    _log(tool_log, _speak("Ready."), _speak("Ready."))
    engine._drain_speech(turn)
    assert len(turn["speech"]) == 2
    assert heard == [("Ready.", "none"), ("Ready.", "none")]


def test_without_a_tool_log_the_stream_is_still_used(tool_log):
    """hw_console against a robot whose log we aren't tailing must keep working."""
    heard = []
    engine = sess.TurnEngine(lambda level, text: None,
                             lambda text, friction: heard.append(text))
    turn = engine._new_turn()
    engine.process(_assistant_speak("Hi there."), turn)
    assert turn["spoke"] is True and heard == ["Hi there."]


# --- minimal stand-ins for the SDK message objects the engine reads -----------------------
class _ToolUseBlock:
    def __init__(self, name, input):
        self.id, self.name, self.input = "toolu_1", name, input


def _assistant_speak(text, friction="none"):
    from claude_agent_sdk import AssistantMessage
    block = _ToolUseBlock(sess.SPEAK, {"text": text, "friction_type": friction})
    # ToolUseBlock is matched by class NAME in the engine, so the stand-in must share it.
    block.__class__.__name__ = "ToolUseBlock"
    return AssistantMessage(content=[block], model="test", parent_tool_use_id=None)
