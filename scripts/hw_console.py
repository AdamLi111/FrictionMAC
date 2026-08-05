"""
Interactive robot console — a real conversation with Misty.

On screen you see ONLY your typed lines and Misty's spoken replies ("Misty: ..."). Each
session writes one transcript at the chosen LOG_LEVEL (INFO | DEBUG | FULL, default DEBUG) to
`data/hw_session_<ts>.<level>.log`.

One persistent session, so Misty remembers the conversation across turns.

Multi-turn robustness: a background task started in one turn can complete during the next, and
its completion notification must not bleed into (and hijack) the following user turn. To handle
that cleanly:
  * a single reader task drains the SDK message stream into a queue (the generator is never
    cancelled mid-read — that would corrupt it; only queue reads get timeouts);
  * completed task-ids are tracked at the SESSION level, so a late/duplicate completion is
    recognised as already-done and ignored;
  * each turn drains trailing messages (waits for a quiet gap after the Director finishes) so
    stragglers are consumed within the turn;
  * any messages buffered between turns are drained before the next user command is sent.

Run (real robot):    MISTY_IP=172.20.10.2 .venv-agent/bin/python -m scripts.hw_console
Run (dry, no robot): .venv-agent/bin/python -m scripts.hw_console

Type 'quit' (or Ctrl-C) to end.
"""
import math
import os
import sys
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
QUIET_S = 2.0   # after the Director finishes, wait this long of silence to drain stragglers


def _terminal_status(msg):
    cls = type(msg).__name__
    if cls == "TaskNotificationMessage":
        return getattr(msg, "status", None)
    if cls == "TaskUpdatedMessage":
        return (getattr(msg, "patch", None) or {}).get("status")
    return None


def _raw(msg) -> str:
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
    """One transcript at the chosen level (cumulative: INFO⊂DEBUG⊂FULL)."""
    def __init__(self, path, level):
        self.threshold = _LEVEL[level]
        self.f = open(path, "a", encoding="utf-8")

    def emit(self, level, text):
        if _LEVEL[level] <= self.threshold:
            self.f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            self.f.flush()

    def close(self):
        self.f.close()


def _process(msg, logs, turn, sess):
    """Route one message to the right log level(s); print Misty's speech; track tasks.
    `turn` holds this turn's pending/result_seen/spoke; `sess` holds session-wide task labels
    and the set of already-completed task-ids (for cross-turn dedup)."""
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
                    fgbg = "background" if inp.get("run_in_background") else "foreground"
                    logs.emit("DEBUG", f"[delegate→{st}] ({fgbg}) {inp.get('description', '')} "
                                       f"| prompt={inp.get('prompt', '')}")
                elif name == SPEAK:
                    text = inp.get("text", "")
                    print(f"Misty: {text}")
                    logs.emit("INFO", f"Misty: {text}")
                    logs.emit("DEBUG", f"[speak/{inp.get('friction_type', '')}] {text}")
                    turn["spoke"] = True
                else:
                    logs.emit("DEBUG", f"[tool] {name} input={inp}")

    elif cls == "UserMessage":
        for b in getattr(msg, "content", None) or []:
            if hasattr(b, "content"):
                logs.emit("DEBUG", f"[report] {b.content}")
            elif hasattr(b, "text") and b.text.strip():
                logs.emit("DEBUG", f"[report] {b.text.strip()}")

    elif isinstance(msg, ResultMessage):
        turn["result_seen"] = True
        logs.emit("INFO", f"● turn complete ({msg.subtype})")
        if msg.result:
            logs.emit("DEBUG", f"[result] {msg.result}")

    elif isinstance(msg, SystemMessage):
        tid = agent_main._task_started_id(msg)
        if tid and tid not in sess["labels"]:
            data = getattr(msg, "data", None) or {}
            st = data.get("subagent_type", "?")
            sess["labels"][tid] = st
            turn["pending"].add(tid)
            logs.emit("INFO", f"  ▶ delegated to {st} — {data.get('description', '')}")
        done = agent_main._task_terminal_id(msg)
        if done:
            if done in sess["done"]:
                # A late/duplicate completion of a task already finished in an earlier turn.
                logs.emit("FULL", f"[stale task completion ignored] {done}")
            else:
                sess["done"].add(done)
                turn["pending"].discard(done)
                st = sess["labels"].get(done, "?")
                mark = "✓" if _terminal_status(msg) in ("completed", "succeeded") else "✗"
                logs.emit("INFO", f"  {mark} {st} {_terminal_status(msg)}")
        logs.emit("FULL", _raw(msg))

    else:
        logs.emit("FULL", _raw(msg))


def _drain_now(recv, logs, sess):
    """Consume everything currently buffered (stragglers / an inter-turn notification-triggered
    turn) WITHOUT blocking, so it can't merge into the next user turn."""
    scratch = {"pending": set(), "result_seen": False, "spoke": False}
    n = 0
    while True:
        try:
            msg = recv.receive_nowait()
        except (anyio.WouldBlock, anyio.EndOfStream):
            break
        if n == 0:
            logs.emit("DEBUG", "[between turns] draining buffered messages")
        n += 1
        _process(msg, logs, scratch, sess)


async def _collect_turn(recv, logs, sess) -> bool:
    """Read one user turn to completion. Ends only after the Director has finished AND no
    background task is still pending AND the stream has been quiet for QUIET_S (so trailing
    completions are drained into this turn). Reads the queue (safe to time out), never the raw
    generator."""
    turn = {"pending": set(), "result_seen": False, "spoke": False}
    t0 = time.monotonic()
    while True:
        if time.monotonic() - t0 > agent_main.MAX_COLLECT_S:
            logs.emit("INFO", "  ! turn timeout")
            break
        msg = None
        with anyio.move_on_after(QUIET_S) as scope:
            try:
                msg = await recv.receive()
            except anyio.EndOfStream:
                break
        if scope.cancelled_caught:                       # quiet for QUIET_S
            if turn["result_seen"] and not turn["pending"]:
                break                                    # settled and drained
            continue                                     # a task is still working
        _process(msg, logs, turn, sess)
    return turn["spoke"]


async def run():
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)

    # Robot target: real @ DEFAULT_MISTY_IP by default (no MISTY_IP needed); --stub for offline.
    # Preflight the connection BEFORE opening the SDK client so an unreachable robot (e.g. wrong
    # Wi-Fi) aborts immediately without spending API tokens.
    ip = config.select_robot_target(stub="--stub" in sys.argv[1:])
    try:
        config.preflight_robot(ip)
    except config.RobotUnreachable as e:
        print(f"\n[abort] {e}")
        return

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
    sess = {"labels": {}, "done": set()}   # session-wide task tracking (cross-turn dedup)

    mode = f"REAL robot @ {ip}" if ip else \
        "STUB (no physical movement — omit --stub to use the real robot)"
    print(f"── Misty console ──  mode: {mode}  |  log level: {level}")
    print("Type a command and press Enter. 'quit' to end.\n")
    logs.emit("INFO", f"[session start] mode={mode} level={level}")

    options = agent_main.build_options(tool_log, world, None)
    options.stderr = lambda line: logs.emit("FULL", f"[stderr] {line.rstrip()}")

    try:
        async with ClaudeSDKClient(options=options) as client:
            send, recv = anyio.create_memory_object_stream(max_buffer_size=math.inf)
            async with anyio.create_task_group() as tg:
                async def reader():
                    # Drain the SDK stream into the queue. Never cancelled mid-turn (only at
                    # session end), so the generator is never corrupted.
                    try:
                        async for msg in client.receive_messages():
                            send.send_nowait(msg)
                    finally:
                        send.close()

                tg.start_soon(reader)
                try:
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
                        _drain_now(recv, logs, sess)     # clear inter-turn stragglers first
                        logs.emit("INFO", f"\n===== you: {cmd} =====")
                        await client.query(cmd)
                        spoke = await _collect_turn(recv, logs, sess)
                        if not spoke:
                            print("(no spoken reply)")
                            logs.emit("INFO", "  (no spoken reply)")
                finally:
                    tg.cancel_scope.cancel()             # stop the reader; end the session
    finally:
        logs.emit("INFO", "[session end]")
        logs.close()
        print(f"\nSession ended. Transcript ({level}): {transcript}")


if __name__ == "__main__":
    anyio.run(run)
