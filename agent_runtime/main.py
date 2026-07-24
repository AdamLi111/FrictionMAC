"""
Agent runtime: Director + domain-expert clusters, with async (concurrent) delegation.

Runs the Director agent (real Claude API) with the stubbed/real robot tool layer over MCP.
The Director may delegate to several subagents concurrently (`run_in_background: true`); this
harness keeps the MCP session connected and reads PAST the Director's ResultMessage until every
background task has reported a terminal status, then disconnects. That is what lets concurrent
subagents actually run to completion (disconnecting early cancels in-flight tasks), so the
Director never has to spin to "hold the turn open."

Usage:
    python -m agent_runtime.main "move forward 1 meter"
    MISTY_IP=... python -m agent_runtime.main "go over to the door"     # real robot
"""
import os
import sys
import time

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from agent_runtime import config, experts, hooks

DEFAULT_TOOL_LOG = config.DATA_DIR / "agent_tool_calls.jsonl"
DEFAULT_WORLD_STATE = config.DATA_DIR / "agent_world_state.json"

# Built-in tools the robot Director should never use (it delegates via `Agent` and speaks via
# the dialogue subagents). Blocking these removes the arbitrary-shell risk and the no-op
# "spin" tools it previously used to hold a turn open.
DISALLOWED = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
              "ScheduleWakeup"]

# Terminal statuses for a background task (from TaskNotification.status / TaskUpdated.patch).
_TERMINAL = {"completed", "failed", "stopped", "succeeded"}
POLL_S = 3.0                                      # idle gap that means "no more messages"
MAX_COLLECT_S = float(os.environ.get("COLLECT_MAX_S", "300"))  # overall safety cap


def build_options(tool_log, world_state, scene=None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model="sonnet",
        cli_path=config.find_cli(),
        system_prompt=config.read_steering("director.md"),
        mcp_servers={config.ROBOT_SERVER: config.robot_mcp_config(tool_log, world_state, scene)},
        agents=experts.build_agents(),
        allowed_tools=["Agent"] + config.ALL_ROBOT_TOOLS,   # 'Agent' enables delegation
        disallowed_tools=DISALLOWED,                        # lock the Director to delegation
        hooks=hooks.build_hooks(),
        permission_mode="default",
        setting_sources=[],          # hermetic: ignore ambient .claude/settings.json
        max_turns=40,
    )


def _task_started_id(msg):
    if type(msg).__name__ == "TaskStartedMessage":
        return getattr(msg, "task_id", None)
    return None


def _task_terminal_id(msg):
    cls = type(msg).__name__
    if cls == "TaskNotificationMessage" and getattr(msg, "status", None) in _TERMINAL:
        return getattr(msg, "task_id", None)
    if cls == "TaskUpdatedMessage":
        patch = getattr(msg, "patch", None) or {}
        if isinstance(patch, dict) and patch.get("status") in _TERMINAL:
            return getattr(msg, "task_id", None)
    return None


async def run(prompt: str, *, tool_log=None, world_state=None, scene=None) -> dict:
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)
    tool_log = tool_log or DEFAULT_TOOL_LOG
    world_state = world_state or DEFAULT_WORLD_STATE

    seen_tool_uses, final_text = [], None
    pending, started, completed = set(), set(), set()  # background task IDs
    result_seen = False
    options = build_options(tool_log, world_state, scene)

    def _handle(message):
        nonlocal final_text, result_seen
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    seen_tool_uses.append(block.name)
                    print(f"[tool_use] {block.name}  {block.input}")
                elif isinstance(block, TextBlock) and block.text.strip():
                    print(f"[assistant] {block.text.strip()[:300]}")
        elif isinstance(message, ResultMessage):
            final_text = message.result
            result_seen = True
            print(f"[result:{message.subtype}] {message.result}")
        elif isinstance(message, SystemMessage):
            tid = _task_started_id(message)
            if tid:
                pending.add(tid); started.add(tid)
                print(f"[task started] {tid}")
            done = _task_terminal_id(message)
            if done:
                pending.discard(done); completed.add(done)
                print(f"[task done] {done}")

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        # Read with a plain async-for (never cancel an in-flight receive — that corrupts the
        # generator). Stop once the turn ended AND no background task is still running; an
        # outer timeout guards a genuine hang.
        with anyio.move_on_after(MAX_COLLECT_S) as scope:
            async for msg in client.receive_messages():
                _handle(msg)
                if result_seen and not pending:
                    break
        if scope.cancelled_caught:
            print("[warn] overall collect timeout reached")

    if pending:
        # The server closes the message stream only after background work has run; a leftover
        # here means its terminal ACK wasn't observed before the stream closed, not that live
        # work was cut off (a genuinely slow, still-running task keeps the stream open and is
        # collected — see scripts/bg_probe.py).
        print(f"[info] stream closed; {len(pending)} background task ack(s) not observed "
              f"(their tool work already executed): {sorted(pending)}")
    return {
        "tool_uses": seen_tool_uses,
        "final_text": final_text,
        "tasks_started": len(started),
        "tasks_completed": len(completed),
        "tasks_pending_at_exit": len(pending),
    }


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "move forward 1 meter"
    scene = os.environ.get("ROBOT_STUB_SCENE")  # allow scripted scene from env for manual runs
    anyio.run(lambda: run(prompt, scene=scene))


if __name__ == "__main__":
    main()
