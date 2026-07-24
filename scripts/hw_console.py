"""
Interactive robot console — a real conversation with Misty.

On screen you see ONLY your typed lines and Misty's spoken replies ("Misty: ..."). Each
session writes THREE leveled transcripts (cumulative, most-verbose contains the others):

  * <session>.info.log   — overall flow only: the command, which agent(s) were delegated to
                           and whether they finished/failed, what Misty said, turn complete.
  * <session>.debug.log  — INFO + the agents' responses, thinking/decision process, tool
                           calls, and subagents' returned reports, plus result text.
  * <session>.full.log   — everything, including the claude-agent system boilerplate
                           (init dump, token counters, stderr, raw message data).

One persistent session, so Misty remembers the conversation across turns.

Run (real robot):    MISTY_IP=172.20.10.2 .venv-agent/bin/python -m scripts.hw_console
Run (dry, no robot): .venv-agent/bin/python -m scripts.hw_console

Type 'quit' (or Ctrl-C) to end.
"""
import os
import time

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)

from agent_runtime import config, main as agent_main

SPEAK = config.robot_tool("speak")
QUIT = {"quit", "exit", "q", ":q"}
_LEVEL = {"INFO": 0, "DEBUG": 1, "FULL": 2}


def _terminal_status(msg):
    cls = type(msg).__name__
    if cls == "TaskNotificationMessage":
        return getattr(msg, "status", None)
    if cls == "TaskUpdatedMessage":
        return (getattr(msg, "patch", None) or {}).get("status")
    return None


def _raw(msg) -> str:
    """Full untruncated dump of a message for the FULL log."""
    cls = type(msg).__name__
    lines = [f"<<{cls}>>"]
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for b in content:
            bcls = type(b).__name__
            if hasattr(b, "text"):
                lines.append(f"    [{bcls}] {b.text}")
            elif hasattr(b, "thinking"):
                lines.append(f"    [{bcls}] {b.thinking}")
            elif hasattr(b, "name") and hasattr(b, "input"):
                lines.append(f"    [{bcls}] {b.name} input={b.input}")
            elif hasattr(b, "content"):
                lines.append(f"    [{bcls}] result={b.content}")
            else:
                lines.append(f"    [{bcls}] {b!r}")
    else:
        for attr in ("subtype", "result", "status", "task_id", "summary", "patch", "data"):
            v = getattr(msg, attr, None)
            if v is not None:
                lines.append(f"    {attr}={v}")
    return "\n".join(lines)


class Logs:
    """Writes one transcript at the chosen level. Levels are cumulative (INFO⊂DEBUG⊂FULL):
    at DEBUG you get INFO + DEBUG entries; at FULL you get everything."""
    def __init__(self, path, level):
        self.threshold = _LEVEL[level]
        self.f = open(path, "a", encoding="utf-8")

    def emit(self, level, text):
        if _LEVEL[level] <= self.threshold:   # keep entries at or below the chosen verbosity
            self.f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            self.f.flush()

    def close(self):
        self.f.close()


def _process(msg, logs, state):
    """Route one message to the right log level(s); print Misty's speech; track tasks."""
    cls = type(msg).__name__

    if isinstance(msg, AssistantMessage):
        for b in msg.content:
            bcls = type(b).__name__
            if bcls == "ThinkingBlock" and getattr(b, "thinking", "").strip():
                logs.emit("DEBUG", f"[think] {b.thinking.strip()}")
            elif bcls == "TextBlock" and getattr(b, "text", "").strip():
                logs.emit("DEBUG", f"[director] {b.text.strip()}")
            elif bcls == "ToolUseBlock":
                name, inp = b.name, (b.input or {})
                if name in ("Agent", "Task"):
                    st = inp.get("subagent_type", "?")
                    logs.emit("DEBUG", f"[delegate→{st}] {inp.get('description', '')} "
                                       f"| prompt={inp.get('prompt', '')}")
                elif name == SPEAK:
                    text = inp.get("text", "")
                    print(f"Misty: {text}")                     # <-- only console output
                    logs.emit("INFO", f"Misty: {text}")
                    logs.emit("DEBUG", f"[speak/{inp.get('friction_type', '')}] {text}")
                    state["spoke"] = True
                else:
                    logs.emit("DEBUG", f"[tool] {name} input={inp}")

    elif cls == "UserMessage":                                  # subagent reports / tool results
        for b in getattr(msg, "content", None) or []:
            if hasattr(b, "content"):
                logs.emit("DEBUG", f"[report] {b.content}")
            elif hasattr(b, "text") and b.text.strip():
                logs.emit("DEBUG", f"[report] {b.text.strip()}")

    elif isinstance(msg, ResultMessage):
        state["result_seen"] = True
        logs.emit("INFO", f"● turn complete ({msg.subtype})")
        if msg.result:
            logs.emit("DEBUG", f"[result] {msg.result}")

    elif isinstance(msg, SystemMessage):
        tid = agent_main._task_started_id(msg)
        if tid and tid not in state["labels"]:                  # report each task's start once
            data = getattr(msg, "data", None) or {}
            st = data.get("subagent_type", "?")
            state["pending"].add(tid)
            state["labels"][tid] = st
            logs.emit("INFO", f"  ▶ delegated to {st} — {data.get('description', '')}")
        done = agent_main._task_terminal_id(msg)
        if done and done not in state["done"]:                  # report each terminal once
            st = state["labels"].get(done, "?")
            status = _terminal_status(msg)
            state["pending"].discard(done)
            state["done"].add(done)
            mark = "✓" if status in ("completed", "succeeded") else "✗"
            logs.emit("INFO", f"  {mark} {st} {status}")
        logs.emit("FULL", _raw(msg))                            # raw system noise -> FULL only

    else:
        logs.emit("FULL", _raw(msg))


async def _collect_turn(agen, logs) -> bool:
    """Read one Director turn to completion (incl. background tasks). Plain async-for + break
    (never cancels an in-flight receive); outer timeout is only a hang guard."""
    state = {"pending": set(), "labels": {}, "done": set(), "result_seen": False, "spoke": False}
    with anyio.move_on_after(agent_main.MAX_COLLECT_S) as scope:
        async for msg in agen:
            _process(msg, logs, state)
            if state["result_seen"] and not state["pending"]:
                break
    if scope.cancelled_caught:
        logs.emit("INFO", "  ! timed out waiting for the robot")
        print("(timed out waiting for the robot)")
    return state["spoke"]


async def run():
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    level = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    if level not in _LEVEL:
        print(f"(unknown LOG_LEVEL={level!r}; using DEBUG. Options: INFO, DEBUG, FULL)")
        level = "DEBUG"
    transcript = config.DATA_DIR / f"hw_session_{stamp}.{level.lower()}.log"
    tool_log = config.DATA_DIR / f"hw_session_{stamp}_tools.jsonl"
    world = config.DATA_DIR / "agent_world_state.json"
    os.environ["AGENT_EVENT_LOG"] = str(config.DATA_DIR / f"hw_session_{stamp}_events.jsonl")

    logs = Logs(transcript, level)

    ip = os.environ.get("MISTY_IP")
    mode = f"REAL robot (MISTY_IP={ip})" if ip else \
        "STUB (no physical movement — set MISTY_IP to use the real robot)"
    print(f"── Misty console ──  mode: {mode}  |  log level: {level}")
    print("Type a command and press Enter. 'quit' to end.\n")
    logs.emit("INFO", f"[session start] mode={mode} level={level}")

    options = agent_main.build_options(tool_log, world, None)
    options.stderr = lambda line: logs.emit("FULL", f"[stderr] {line.rstrip()}")

    try:
        async with ClaudeSDKClient(options=options) as client:
            agen = client.receive_messages()      # one continuous stream, reused per turn
            while True:
                try:
                    cmd = (await anyio.to_thread.run_sync(lambda: input("you> "))).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if cmd.lower() in QUIT:
                    break
                if not cmd:
                    continue
                logs.emit("INFO", f"\n===== you: {cmd} =====")
                await client.query(cmd)
                spoke = await _collect_turn(agen, logs)
                if not spoke:
                    print("(no spoken reply)")
                    logs.emit("INFO", "  (no spoken reply)")
    finally:
        logs.emit("INFO", "[session end]")
        logs.close()
        print(f"\nSession ended. Transcript ({level}): {transcript}")


if __name__ == "__main__":
    anyio.run(run)
