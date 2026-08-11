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
import math
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

from agent_runtime import architectures, config

DEFAULT_TOOL_LOG = config.DATA_DIR / "agent_tool_calls.jsonl"
DEFAULT_WORLD_STATE = config.DATA_DIR / "agent_world_state.json"

# Terminal statuses for a background task (from TaskNotification.status / TaskUpdated.patch).
_TERMINAL = {"completed", "failed", "stopped", "succeeded"}
POLL_S = 3.0                                      # idle gap that means "no more messages"
# Overall per-turn cap. A turn takes as long as it needs by default (no cap); set
# COLLECT_MAX_S>0 only if you want a hard safety ceiling to guard a genuine hang.
_collect_cap = float(os.environ.get("COLLECT_MAX_S", "0"))
MAX_COLLECT_S = _collect_cap if _collect_cap > 0 else math.inf


def build_options(tool_log, world_state, scene=None, arch=None) -> ClaudeAgentOptions:
    """Build SDK options for the selected architecture (run option).

    `arch` may be an Architecture name (e.g. "v2"), an Architecture instance, or None — in
    which case the AGENT_ARCH env var (default "v1") decides. The message-collection loops
    below are architecture-agnostic, so switching variants changes only this wiring."""
    if not isinstance(arch, architectures.Architecture):
        arch = architectures.get(arch)
    return arch.build_options(tool_log, world_state, scene)


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


async def run(prompt: str, *, tool_log=None, world_state=None, scene=None, arch=None) -> dict:
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)
    # In REAL mode (MISTY_IP set), verify the robot is reachable BEFORE starting the SDK client
    # so an unreachable robot fails fast without spending tokens. Skipped in stub mode
    # (MISTY_IP unset), so stub tests are unaffected. Raises config.RobotUnreachable on failure.
    config.preflight_robot()
    tool_log = tool_log or DEFAULT_TOOL_LOG
    world_state = world_state or DEFAULT_WORLD_STATE

    seen_tool_uses, final_text = [], None
    pending, started, completed = set(), set(), set()  # background task IDs
    result_seen = False
    architecture = arch if isinstance(arch, architectures.Architecture) else architectures.get(arch)
    print(f"[arch] {architecture.name} — {architecture.description}")
    options = build_options(tool_log, world_state, scene, arch=architecture)

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
    # Usage: python -m agent_runtime.main [--arch v1|v2] [--stub] "<command>"
    #   - architecture may also be chosen with AGENT_ARCH; the flag wins if given.
    #   - real robot at DEFAULT_MISTY_IP by default (no MISTY_IP needed); --stub for offline.
    argv = sys.argv[1:]
    arch, stub = None, False
    while argv and argv[0].startswith("--"):
        if argv[0] == "--arch":
            arch, argv = argv[1], argv[2:]
        elif argv[0] == "--stub":
            stub, argv = True, argv[1:]
        else:
            break
    prompt = argv[0] if argv else "move forward 1 meter"
    scene = os.environ.get("ROBOT_STUB_SCENE")  # allow scripted scene from env for manual runs

    ip = config.select_robot_target(stub=stub)   # default to real 172.20.10.2 unless --stub
    print(f"[robot] {'STUB (offline)' if ip is None else f'REAL @ {ip}'}")
    try:
        config.load_env()
        config.preflight_robot(ip)               # fail fast (before tokens) if unreachable
    except config.RobotUnreachable as e:
        print(f"\n[abort] {e}")
        sys.exit(1)
    anyio.run(lambda: run(prompt, scene=scene, arch=arch))


if __name__ == "__main__":
    main()
