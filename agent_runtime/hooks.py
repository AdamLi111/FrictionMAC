"""
Agent-side hooks: routing/tool-call logging + the (inert-by-default) friction gate.

- Logging: the robot MCP server already writes the canonical tool-call JSONL (name, args,
  result). These hooks add the *agent-side* view — which subagent made each call, and the
  Director's routing (SubagentStart/Stop) — to a separate JSONL.
- Friction gate: a PreToolUse hook on `mcp__robot__ask_clarification` that CAN deny the call,
  for a future friction-OFF ablation. It DEFAULTS TO ALLOW and only denies when the env flag
  FRICTION_OFF=1 is set at runtime. Dormant plumbing; zero effect on normal operation.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import HookMatcher

SPEAK = "mcp__robot__speak"


def friction_off() -> bool:
    return os.environ.get("FRICTION_OFF") == "1"


def _is_friction(tool_input: dict) -> bool:
    """A speak call is a friction turn iff its friction_type label is not 'none'."""
    ft = (tool_input or {}).get("friction_type")
    return ft is not None and ft != "none"


def _event_log_path() -> Path:
    return Path(os.environ.get("AGENT_EVENT_LOG", "data/agent_events.jsonl"))


def _log_event(entry: dict) -> None:
    path = _event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- friction gate (inert unless FRICTION_OFF=1) ---
# Gates `speak` calls that carry a friction label (friction_type != "none"). Normal
# utterances (friction_type == "none") are always allowed. Inert unless FRICTION_OFF=1.
async def friction_gate(input_data, tool_use_id, context):
    tool_input = input_data.get("tool_input", {})
    if not friction_off() or not _is_friction(tool_input):
        return {}  # allow: friction enabled, or this is a normal (non-friction) utterance
    _log_event({
        "event": "friction_gate_deny",
        "tool": input_data.get("tool_name"),
        "agent_type": input_data.get("agent_type"),
        "friction_type": tool_input.get("friction_type"),
        "text": tool_input.get("text"),
    })
    return {
        "hookSpecificOutput": {
            "hookEventName": input_data["hook_event_name"],
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Friction is disabled for this run (ablation). Do not apply friction (no "
                "clarifying/probing/pausing utterances). Proceed with a single best-guess "
                "interpretation and continue; a normal reply (friction_type='none') is fine."
            ),
        }
    }


# --- tool-call logger (agent-side view: which subagent, what args) ---
async def tool_logger(input_data, tool_use_id, context):
    _log_event({
        "event": "pre_tool_use",
        "tool": input_data.get("tool_name"),
        "agent_type": input_data.get("agent_type"),  # None => Director (top level)
        "agent_id": input_data.get("agent_id"),
        "input": input_data.get("tool_input"),
    })
    return {}


# --- routing logger (Director's delegation) ---
async def subagent_start(input_data, tool_use_id, context):
    _log_event({"event": "subagent_start", "agent_type": input_data.get("agent_type"),
                "agent_id": input_data.get("agent_id")})
    return {}


async def subagent_stop(input_data, tool_use_id, context):
    _log_event({"event": "subagent_stop", "agent_type": input_data.get("agent_type"),
                "agent_id": input_data.get("agent_id")})
    return {}


def build_hooks() -> dict:
    return {
        "PreToolUse": [
            HookMatcher(matcher=SPEAK, hooks=[friction_gate]),   # inert friction gate
            HookMatcher(matcher="^mcp__", hooks=[tool_logger]),  # all robot tools
        ],
        "SubagentStart": [HookMatcher(hooks=[subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[subagent_stop])],
    }
