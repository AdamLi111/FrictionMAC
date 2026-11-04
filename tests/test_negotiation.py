"""
Tests for V5's negotiation protocol (agent_runtime.negotiation).

V5's whole point is that the decision to slow down belongs to the negotiator and the code holds
the team to it, so these pin the holds themselves: nothing runs before a new user turn is judged;
GATHER keeps navigation waiting until the negotiator has seen what perception found; FRICTION
keeps navigation and ordinary speech waiting until the friction move is voiced, and Misty asks
once; an expert's surprise sends the decision back to the negotiator. No SDK and no API: the
gate is fed the same dicts the SDK hands its hooks.
"""
import asyncio
import json

import pytest

from agent_runtime import negotiation as neg


@pytest.fixture(autouse=True)
def events(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("AGENT_EVENT_LOG", str(path))
    monkeypatch.delenv("FRICTION_OFF", raising=False)
    return path


def _user(gate, words):
    """A user command exactly as the front ends send it (session.stamp_command's header)."""
    return gate.user_turn(f"[clock 2026-11-03 21:00:00 | 12s since your last reply]\n\n{words}")


def _verdict(gate, verdict, friction_type="none", trigger="none"):
    gate.report("negotiator", f"Reasoning about the turn.\n\nVERDICT: {verdict}\n"
                              f"TRIGGER: {trigger}\nFRICTION_TYPE: {friction_type}\n"
                              f"SURFACE: none\nINTENT: go to the cup\nNEXT: navigation")


def _may(gate, agent):
    return gate.held(agent, background=False) is None


# ------------------------------------------------------------------------------- the holds
def test_a_new_user_turn_holds_everything_until_the_negotiator_has_judged_it():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    assert gate.phase == neg.JUDGE
    for agent in ("navigation", "object-lookup", "regular-utterance", "friction", "expression"):
        assert not _may(gate, agent), agent
    assert _may(gate, "negotiator")
    assert _may(gate, "map")                       # map only records; it commits nothing


def test_the_negotiator_must_be_consulted_in_the_foreground():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    assert gate.held("negotiator", background=True)
    assert gate.held("negotiator", background=None)   # omitted means background
    assert gate.held("negotiator", background=False) is None


def test_proceed_releases_everything():
    gate = neg.NegotiationGate()
    _user(gate, "turn left")
    _verdict(gate, "PROCEED")
    assert gate.phase == neg.CLEAR
    for agent in ("navigation", "object-lookup", "regular-utterance", "friction", "expression"):
        assert _may(gate, agent), agent


def test_gather_holds_navigation_until_the_negotiator_sees_what_was_found():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "GATHER")
    assert _may(gate, "object-lookup") and not _may(gate, "navigation")
    gate.report("object-lookup", "A red cup ~10° left, on the table.\nSTATUS: FOUND")
    assert not _may(gate, "navigation")            # finding it is not permission to drive
    _verdict(gate, "PROCEED", "assumption_reveal", "inferred_target")
    assert _may(gate, "navigation")


def test_friction_is_voiced_before_the_robot_acts_and_only_once():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "FRICTION", "probing", "referential_ambiguity")
    assert _may(gate, "friction")
    assert not _may(gate, "navigation") and not _may(gate, "regular-utterance")
    assert "probing" in gate.held("navigation", background=False)

    gate.robot_spoke("I see a red cup and a blue cup. Which one?", "probing")
    assert gate.phase == neg.ASKED
    for agent in ("friction", "navigation", "regular-utterance"):
        assert not _may(gate, agent), agent

    _user(gate, "the red one")                     # the answer re-opens judgment
    assert gate.phase == neg.JUDGE


def test_a_normal_utterance_does_not_count_as_the_friction_move():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "FRICTION", "probing")
    gate.robot_spoke("Okay!", "none")
    assert gate.phase == neg.FRICTION


def test_a_surprise_from_an_expert_goes_back_to_the_negotiator():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "PROCEED")
    gate.report("object-lookup", "Two cups: red ~10° left, blue ~15° right.\nSTATUS: MULTIPLE")
    assert gate.phase == neg.JUDGE
    reason = gate.held("navigation", background=False)
    assert "object-lookup" in reason and "MULTIPLE" in reason
    assert _may(gate, "negotiator")


@pytest.mark.parametrize("agent, status", [
    ("object-lookup", "NOT_FOUND"), ("map", "AMBIGUOUS"), ("map", "NONE"),
    ("navigation", "INFEASIBLE")])
def test_every_kind_of_surprise_is_a_checkpoint(agent, status):
    gate = neg.NegotiationGate()
    _user(gate, "go to the fridge")
    _verdict(gate, "PROCEED")
    gate.report(agent, f"Report.\n**STATUS:** {status}")
    assert gate.phase == neg.JUDGE


def test_only_an_experts_final_status_line_counts():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "PROCEED")
    gate.report("navigation", "First try was STATUS: INFEASIBLE; re-planned round the box.\n"
                              "STATUS: DONE")
    assert gate.phase == neg.CLEAR


def test_a_surprise_does_not_reopen_a_question_already_asked():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "FRICTION", "probing")
    gate.robot_spoke("Which cup?", "probing")
    gate.report("object-lookup", "STATUS: MULTIPLE")
    assert gate.phase == neg.ASKED


# ------------------------------------------------------------------------- reading reports
def test_a_background_launch_ack_is_not_a_verdict():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    gate.report("negotiator", "Async agent launched successfully.\nagentId: a1b2")
    assert gate.phase == neg.JUDGE


def test_the_verdict_is_read_through_markdown_and_the_last_one_wins():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    gate.report("negotiator", "A VERDICT: PROCEED would be premature here.\n\n"
                              "**VERDICT:** FRICTION\n**FRICTION_TYPE:** `probing`")
    assert gate.phase == neg.FRICTION
    assert gate.friction_type == "probing"


def test_block_fields_are_read_from_their_own_lines_not_from_the_reasoning(events):
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    gate.report("negotiator", "Two cups are recorded. The trigger is clear here.\n\n"
                              "- VERDICT: FRICTION\n- TRIGGER: referential_ambiguity\n"
                              "- FRICTION_TYPE: probing\n- SURFACE: which trigger of the two?")
    verdict = [json.loads(line) for line in events.read_text().splitlines()
               if '"negotiator_verdict"' in line][-1]
    assert verdict["trigger"] == "referential_ambiguity"
    assert verdict["friction_type"] == "probing"


def test_a_verdict_written_mid_line_is_still_read():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    gate.report("negotiator", "Nothing ambiguous, so my VERDICT: PROCEED.")
    assert gate.phase == neg.CLEAR
    _user(gate, "now the other one")
    gate.report("negotiator", "That needs a look first. VERDICT: GATHER")
    assert gate.phase == neg.GATHER


def test_a_copied_template_line_is_not_mistaken_for_a_verdict():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    gate.report("negotiator", "VERDICT: <PROCEED, GATHER or FRICTION>")
    assert gate.phase == neg.CLEAR                 # fails open, and the event log says so


def test_an_unreadable_verdict_fails_open_and_is_logged(events):
    gate = neg.NegotiationGate()
    _user(gate, "turn left")
    gate.report("negotiator", "Looks fine to me, go ahead.")
    assert gate.phase == neg.CLEAR
    logged = [json.loads(line) for line in events.read_text().splitlines()]
    verdicts = [e for e in logged if e["event"] == "negotiator_verdict"]
    assert verdicts and verdicts[-1]["verdict"] is None


def test_an_empty_negotiator_report_fails_open():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    gate.report("negotiator", "")
    assert gate.phase == neg.CLEAR


def test_a_gate_that_never_sees_verdicts_stops_holding_the_robot():
    """If verdicts never reach the gate (a response it can't read, a hook that doesn't fire),
    the hold message keeps sending the Director back to the negotiator. That must not loop."""
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    for _ in range(2):
        _run(gate.before_delegation(_pre("negotiator"), "t", None))
        assert gate.phase == neg.JUDGE              # its verdicts are never reported back
    _run(gate.before_delegation(_pre("negotiator"), "t", None))
    assert gate.phase == neg.CLEAR


def test_a_verdict_resets_the_unanswered_count():
    gate = neg.NegotiationGate()
    for words in ("go to the cup", "the red one", "now the door"):
        _user(gate, words)
        _run(gate.before_delegation(_pre("negotiator"), "t", None))
        _verdict(gate, "GATHER")
        assert gate.phase == neg.GATHER


def test_verdicts_and_triggers_are_logged_for_analysis(events):
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "FRICTION", "probing", "referential_ambiguity")
    logged = [json.loads(line) for line in events.read_text().splitlines()]
    verdict = [e for e in logged if e["event"] == "negotiator_verdict"][-1]
    assert (verdict["verdict"], verdict["friction_type"], verdict["trigger"]) == (
        "FRICTION", "probing", "referential_ambiguity")


def test_a_background_completion_re_invoking_the_director_is_not_a_user_turn():
    gate = neg.NegotiationGate()
    assert _user(gate, "go to the cup") is True
    _verdict(gate, "PROCEED")
    assert gate.user_turn("<task-notification>map completed</task-notification>") is False
    assert gate.phase == neg.CLEAR


def test_an_unstamped_first_prompt_still_counts():
    """agent_runtime.main sends its one command without the clock header."""
    gate = neg.NegotiationGate()
    assert gate.user_turn("go to the cup") is True
    assert gate.phase == neg.JUDGE


def test_friction_off_ablation_does_not_hold_the_robot_for_a_refused_utterance(monkeypatch):
    monkeypatch.setenv("FRICTION_OFF", "1")
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _verdict(gate, "FRICTION", "probing")
    assert gate.phase == neg.CLEAR


# ------------------------------------------------------------------------------- the dialogue
def test_the_negotiator_is_handed_the_dialogue_verbatim():
    gate = neg.NegotiationGate()
    _user(gate, "go over to the cup")
    gate.robot_spoke("I see a red cup and a blue cup. Which one?", "probing")
    _user(gate, "the red one")
    brief = gate.negotiator_brief('Judge this turn: "the red one"')
    assert brief.startswith('Judge this turn: "the red one"')
    assert "USER: go over to the cup" in brief
    assert "MISTY (probing): I see a red cup and a blue cup. Which one?" in brief
    lines = brief.splitlines()
    assert lines[-2] == "USER: the red one" and lines[-1] == "--- end of dialogue ---"
    assert "[clock" not in brief


def test_a_long_dialogue_keeps_the_recent_lines():
    gate = neg.NegotiationGate(max_dialogue_lines=3)
    for i in range(5):
        _user(gate, f"command {i}")
    brief = gate.negotiator_brief("Judge it.")
    assert "(… 2 earlier line(s) omitted)" in brief
    assert "USER: command 4" in brief and "USER: command 1" not in brief


def test_no_dialogue_leaves_the_brief_alone():
    assert neg.NegotiationGate().negotiator_brief("Judge it.") == "Judge it."


# -------------------------------------------------------------------- the SDK-facing callbacks
def _pre(agent, prompt="Do it.", background=False):
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {"subagent_type": agent, "description": "x", "prompt": prompt,
                           "run_in_background": background}}


def _post(agent, response):
    return {"hook_event_name": "PostToolUse", "tool_name": "Agent",
            "tool_input": {"subagent_type": agent, "prompt": "VERDICT: PROCEED (in the brief)"},
            "tool_response": response}


def _run(coro):
    return asyncio.run(coro)


def test_the_callbacks_answer_in_the_sdk_hook_shapes():
    gate = neg.NegotiationGate()
    _run(gate.on_user_prompt({"hook_event_name": "UserPromptSubmit",
                              "prompt": "[clock 2026-11-03 21:00:00 | x]\n\ngo to the cup"},
                             None, None))

    held = _run(gate.before_delegation(_pre("navigation"), "t1", None))["hookSpecificOutput"]
    assert held["hookEventName"] == "PreToolUse"
    assert held["permissionDecision"] == "deny"
    assert "negotiator" in held["permissionDecisionReason"]

    out = _run(gate.before_delegation(_pre("negotiator", 'Judge: "go to the cup"'), "t2", None))
    out = out["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["prompt"].startswith('Judge: "go to the cup"')
    assert "USER: go to the cup" in out["updatedInput"]["prompt"]
    assert out["updatedInput"]["subagent_type"] == "negotiator"     # the rest is untouched
    assert out["updatedInput"]["run_in_background"] is False

    _run(gate.after_delegation(_post("negotiator", {"content": [
        {"type": "text", "text": "VERDICT: FRICTION\nFRICTION_TYPE: probing"}]}), "t2", None))
    assert gate.phase == neg.FRICTION

    _run(gate.after_speech({"hook_event_name": "PostToolUse", "tool_name": "mcp__robot__speak",
                            "tool_input": {"text": "Which cup?", "friction_type": "probing"}},
                           "t3", None))
    assert gate.phase == neg.ASKED


def test_the_brief_echoed_in_a_tool_response_is_not_read_as_the_report():
    gate = neg.NegotiationGate()
    _user(gate, "go to the cup")
    _run(gate.after_delegation(_post("negotiator", {
        "prompt": "VERDICT: PROCEED (in the brief)",
        "content": [{"type": "text", "text": "VERDICT: GATHER"}]}), "t1", None))
    assert gate.phase == neg.GATHER


def test_v5_registers_the_negotiator_and_keeps_v4s_experts_verbatim():
    pytest.importorskip("claude_agent_sdk")   # AgentDefinition / HookMatcher live in the SDK
    from agent_runtime import architectures, config

    arch = architectures.get("v5")
    agents = arch.agents()
    assert set(agents) == {"object-lookup", "map", "navigation", "expression",
                           "regular-utterance", "friction", "negotiator"}
    assert set(agents["negotiator"].tools) == {config.robot_tool(n) for n in (
        "get_world", "get_known_location", "get_last_view")}          # judges, never acts

    v4 = architectures.get("v4").agents()
    for name in ("object-lookup", "map", "navigation", "expression", "regular-utterance"):
        assert agents[name].prompt == v4[name].prompt, name           # V4 → V5 isolates it

    registered = arch.hook_set(set(agents))
    assert {"UserPromptSubmit", "PreToolUse", "PostToolUse"} <= set(registered)
    pre = [m.matcher for m in registered["PreToolUse"]]
    assert pre.count("^(Agent|Task)$") == 2       # the type gate and the negotiation gate
