"""
Background-task delivery probe.

Question: in our query() / ClaudeSDKClient mode, when the Director finishes its turn while a
subagent is still running in the background, is the task's completion (task_notification /
task_updated terminal) ever DELIVERED — and if so, is it before or after the ResultMessage?

Design (per the two refinements):
  - The background subagent is RELIABLY SLOW: it calls find_object once, and we run the stub
    with ROBOT_STUB_SLOW=1 so the 360° scan actually sleeps (~8s). So it is guaranteed still
    in-flight when the Director finishes.
  - The Director is told to spawn it with run_in_background:true and then IMMEDIATELY finish
    (do NOT hold the turn) — so we test the real in-flight/abandonment case.
  - We read with receive_messages() (NOT receive_response()) so we keep reading PAST the
    ResultMessage. That distinguishes "never sent" (abandoned) from "sent but unread"
    (receive_response would have stopped too early).

Run:  ROBOT_STUB_SLOW=1 .venv-agent/bin/python -m scripts.bg_probe
"""
import json
import os
import time

import anyio
from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from agent_runtime import config, hooks

TOOL_LOG = config.DATA_DIR / "bg_probe_tool_calls.jsonl"
WORLD = config.DATA_DIR / "bg_probe_world.json"
EVENTS = config.DATA_DIR / "bg_probe_events.jsonl"

SLOW_WORKER = AgentDefinition(
    description="A deliberately slow background worker (for testing only).",
    prompt=("You are a slow test worker. Call mcp__robot__find_object('probe-target') exactly "
            "once — it performs a slow 360° scan — then reply with 'STATUS: DONE'. Do nothing "
            "else."),
    tools=[config.robot_tool("find_object")],
    mcpServers=[config.ROBOT_SERVER],
    model="inherit",
)

SYSTEM = ("You have one subagent named 'slow-worker'. When the user asks, IMMEDIATELY delegate "
          "to slow-worker using the Agent tool with run_in_background: true, then IMMEDIATELY "
          "give a one-sentence final answer WITHOUT waiting for it. Do not call any other "
          "tools. Do not wait for the worker.")

PROMPT = "Kick off the slow worker in the background, then finish right away."

READ_WINDOW_S = 35  # keep reading past the ResultMessage this long


def _terminal_task(msg) -> bool:
    cls = type(msg).__name__
    if cls == "TaskNotificationMessage":
        return getattr(msg, "status", None) in ("completed", "failed", "stopped")
    if cls == "TaskUpdatedMessage":
        patch = getattr(msg, "patch", None) or {}
        return isinstance(patch, dict) and patch.get("status") in (
            "completed", "failed", "stopped", "succeeded")
    return False


async def run():
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)
    for p in (TOOL_LOG, WORLD, EVENTS):
        if p.exists():
            p.unlink()
    os.environ["ROBOT_STUB_SLOW"] = "1"     # make the stub scan genuinely slow
    os.environ["AGENT_EVENT_LOG"] = str(EVENTS)
    os.environ.pop("MISTY_IP", None)        # stub mode

    opts = ClaudeAgentOptions(
        model="sonnet",
        cli_path=config.find_cli(),
        system_prompt=SYSTEM,
        mcp_servers={config.ROBOT_SERVER: config.robot_mcp_config(TOOL_LOG, WORLD, None)},
        agents={"slow-worker": SLOW_WORKER},
        allowed_tools=["Agent", config.robot_tool("find_object")],
        hooks=hooks.build_hooks(),
        permission_mode="default",
        setting_sources=[],
        max_turns=12,
    )

    t0 = time.monotonic()

    def ts():
        return f"+{time.monotonic() - t0:5.1f}s"

    result_at = None
    terminal_task_at = None
    saw_system = []

    async with ClaudeSDKClient(options=opts) as client:
        await client.query(PROMPT)
        with anyio.move_on_after(READ_WINDOW_S):
            async for msg in client.receive_messages():
                cls = type(msg).__name__
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            print(f"{ts()} [tool_use] {b.name} {b.input}")
                        elif isinstance(b, TextBlock) and b.text.strip():
                            print(f"{ts()} [assistant] {b.text.strip()[:120]}")
                elif isinstance(msg, ResultMessage):
                    result_at = time.monotonic() - t0
                    print(f"{ts()} [RESULT] turn complete (subtype={msg.subtype}) "
                          f"-- receive_response() would STOP here; we keep reading")
                elif isinstance(msg, SystemMessage):
                    saw_system.append(cls)
                    extra = {k: getattr(msg, k, None) for k in ("subtype", "status", "task_id")}
                    print(f"{ts()} [system:{cls}] {extra}")
                    if _terminal_task(msg) and terminal_task_at is None:
                        terminal_task_at = time.monotonic() - t0
                        print(f"{ts()} *** terminal TASK message received ***")
                # Stop once we've seen the result AND a terminal task signal.
                if result_at is not None and terminal_task_at is not None:
                    break

    # ---- verdict ----
    names = [json.loads(l)["name"] for l in TOOL_LOG.read_text().splitlines() if l.strip()] \
        if TOOL_LOG.exists() else []
    events = [json.loads(l) for l in EVENTS.read_text().splitlines() if l.strip()] \
        if EVENTS.exists() else []
    starts = [e for e in events if e.get("event") == "subagent_start"]
    stops = [e for e in events if e.get("event") == "subagent_stop"]

    print("\n==== SUMMARY ====")
    print(f"ResultMessage at:        {result_at}")
    print(f"terminal task msg at:    {terminal_task_at}")
    print(f"system msgs seen:        {saw_system}")
    print(f"subagent find_object logged (ran): {'find_object' in names}")
    print(f"subagent_start / stop:   {len(starts)} / {len(stops)}")
    if terminal_task_at is not None and result_at is not None:
        rel = "AFTER" if terminal_task_at > result_at else "BEFORE"
        print(f"VERDICT: completion DELIVERED, {rel} the ResultMessage "
              f"-> receive_response() would have {'MISSED it' if rel=='AFTER' else 'caught it'}.")
    elif "find_object" in names and len(stops) >= 1:
        print("VERDICT: subagent RAN TO COMPLETION but NO terminal task message was delivered.")
    else:
        print("VERDICT: no completion delivered and subagent did not finish "
              "-> background task ABANDONED when the turn ended.")


if __name__ == "__main__":
    anyio.run(run)
