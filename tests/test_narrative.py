"""
Tests for the readable narrative log (agent_runtime.narrative).

Pure rendering — no SDK, no API, no agent. What matters here is that the reader can tell WHO
did something, and that harness boilerplate (background-launch acks, agentId/usage trailers,
camera frames, echoed `speak` results) never buries the actual thinking.
"""
import json

import pytest

from agent_runtime import narrative


@pytest.fixture
def story(tmp_path):
    log = narrative.NarrativeLog(tmp_path / "story.txt")
    yield log
    log.close()


def _text(log) -> str:
    log.f.flush()
    return open(log.f.name).read()


def test_events_are_attributed_and_nested_by_agent(story):
    story.user_turn(1, "Go look at the cup.")
    story.event({"kind": "think", "agent": "director", "text": "I should delegate this."})
    story.event({"kind": "delegate", "agent": "director", "to": "object-lookup",
                 "description": "Locate the cup", "prompt": "Look and report.",
                 "background": False})
    story.event({"kind": "think", "agent": "object-lookup", "text": "Capture a view first."})
    out = _text(story)

    assert "  DIRECTOR · thinking" in out
    assert "     object-lookup · thinking" in out          # subagents sit deeper
    assert "DIRECTOR ▶ delegates to object-lookup  (foreground)" in out
    assert "brief:" in out and "Look and report." in out
    # the Director's reasoning and the expert's reasoning are never confusable
    assert out.index("DIRECTOR · thinking") < out.index("object-lookup · thinking")


def test_background_launch_ack_is_not_rendered_as_a_report(story):
    """The Agent tool answers a background delegation with a boilerplate ack. It is not the
    subagent's report — the real report arrives later — so it must not be shown as one."""
    story.event({"kind": "report", "agent": "expression",
                 "content": "Async agent launched successfully. agentId: abc123 (internal)"})
    out = _text(story)
    assert "reports back" not in out
    assert "running in the background" in out
    assert "abc123" not in out


def test_report_trailers_are_stripped(story):
    story.event({"kind": "report", "agent": "map",
                 "content": "Recorded both cups.\nagentId: a1b2 (use SendMessage ...)\n"
                            "<usage>subagent_tokens: 900\ntool_uses: 2</usage>"})
    out = _text(story)
    assert "Recorded both cups." in out
    assert "agentId" not in out and "subagent_tokens" not in out


def test_speak_is_highlighted_and_its_result_echo_suppressed(story):
    story.event({"kind": "speak", "agent": "friction", "text": "Which cup did you mean?",
                 "friction_type": "probing"})
    story.event({"kind": "tool_result", "agent": "friction",
                 "content": '{"ok": true, "friction_type": "probing"}'})
    out = _text(story)
    assert "MISTY SPEAKS   (friction: probing)" in out
    assert '"Which cup did you mean?"' in out
    assert "ok=true" not in out          # the echo adds nothing next to the utterance itself


def test_ordinary_tool_results_are_compacted_to_one_line(story):
    story.event({"kind": "tool", "agent": "expression", "name": "mcp__robot__move_arm",
                 "input": {"arm": "right", "position": -25}})
    story.event({"kind": "tool_result", "agent": "expression",
                 "content": json.dumps({"ok": True, "arm": "right", "position": -25.0},
                                       indent=2)})
    out = _text(story)
    assert "calls mcp__robot__move_arm(arm='right', position=-25)" in out
    assert "→ ok=true, arm=right, position=-25.0" in out    # not four pretty-printed lines


def test_camera_frames_and_huge_payloads_are_collapsed(story):
    frame = "/9j/4AAQSkZJRg" + "A" * 40_000                  # a base64 JPEG, near enough
    story.event({"kind": "tool_result", "agent": "object-lookup",
                 "content": [{"type": "text", "text": frame}]})
    story.event({"kind": "tool_result", "agent": "object-lookup",
                 "content": [{"type": "image", "source": {"data": "x" * 20_480}}]})
    out = _text(story)
    assert frame[:60] not in out
    assert out.count("camera frame") == 2


def test_long_prose_is_wrapped_but_not_truncated(story):
    thought = "word " * 200
    story.event({"kind": "think", "agent": "director", "text": thought})
    out = _text(story)
    assert all(len(line) <= narrative.WIDTH for line in out.splitlines())
    assert out.count("word") == 200                # wrapping must not drop the reasoning


def test_turn_end_summarises_the_turn(story):
    story.user_turn(2, "The red one.")
    story.event({"kind": "delegate", "agent": "director", "to": "navigation",
                 "description": "drive", "prompt": "go", "background": False})
    story.event({"kind": "tool", "agent": "navigation", "name": "mcp__robot__move_forward",
                 "input": {"distance": 0.5}})
    story.event({"kind": "speak", "agent": "regular-utterance", "text": "Done.",
                 "friction_type": "none"})
    story.turn_end(subtype="success", seconds=24.3)
    out = _text(story)
    assert "TURN 2" in out
    assert ("turn 2 ended · success · 24s · 1 delegation(s) · 1 tool call(s) · "
            "1 utterance(s)") in out


def test_turn_timeout_is_called_out(story):
    story.user_turn(1, "hello")
    story.turn_end(timed_out=True, seconds=300)
    assert "CUT OFF (turn timeout)" in _text(story)


def test_silence_is_recorded(story):
    story.user_turn(1, "hello")
    story.no_speech()
    assert "the user hears silence" in _text(story)
