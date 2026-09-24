"""
One robot conversation, driven turn by turn — shared by every front end.

This is the machinery that was inside `scripts/hw_console.py`: open ONE persistent SDK session
so the Director remembers the conversation, then read the message stream one user turn at a
time. It is here (not in the console) because the evaluation harness needs exactly the same
turn semantics with a different input source — a simulated user instead of a keyboard — and
this logic is subtle enough that two copies would drift.

Multi-turn robustness (why it is not just `async for msg in client.receive_messages()`):
a background task started in one turn can complete during the next, and its completion
notification must not bleed into (and hijack) the following turn. So:
  * a single reader task drains the SDK message stream into a queue (the generator is never
    cancelled mid-read — that would corrupt it; only queue reads get timeouts);
  * completed task-ids are tracked at the SESSION level, so a late/duplicate completion is
    recognised as already-done and ignored;
  * each turn drains trailing messages (waits for a quiet gap after the Director finishes) so
    stragglers are consumed within the turn;
  * anything buffered between turns is drained before the next command is sent.

The front end supplies two callbacks, plus an optional third:
  emit(level, text)              -> transcript logging ("INFO" | "DEBUG" | "FULL")
  on_speech(text, friction_type) -> the robot spoke; this is the ONLY user-visible output
  trace(event: dict)             -> the same messages as ATTRIBUTED, STRUCTURED events
                                    ({kind, agent, ...}), for the readable narrative log

Attribution: `AssistantMessage.parent_tool_use_id` is `None` for the Director and otherwise the
`id` of the `Agent` tool call that spawned the subagent, so recording the ids of the Director's
`Agent` calls names every later thought, tool call and report. See `narrative.py`.
"""
import json
import math
import time
from collections import Counter
from contextlib import asynccontextmanager

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)

from agent_runtime import config, main as agent_main

SPEAK = config.robot_tool("speak")

QUIET_S = 2.0   # after the Director finishes, wait this long of silence to drain stragglers
# Case A only: when a background task completes AFTER the Director already ended a turn, it
# usually triggers a follow-up turn. Hold the turn open this long for that follow-up before
# giving up (a completion with no follow-up shouldn't hang). Does NOT apply while the Director
# is still mid-turn — there we wait for the real ResultMessage (bounded by max_collect_s).
CONTINUATION_GRACE_S = 20.0
# A turn that has produced NO speech is not obviously finished. The top-level stream only shows
# one level of delegation, so work running deeper (a V2 manager's expert, say) is invisible here
# and `pending` looks empty — end the turn at the first 2s lull and you cut a cascade that was
# still running, then report "(no spoken reply)" for a command the robot was about to answer.
# So when nothing has been said yet, require this much genuine quiet — no messages AND no robot
# tool calls — before calling the turn silent.
SILENT_GRACE_S = 20.0


def stamp_command(cmd: str, last_reply_at: float | None) -> str:
    """Prefix the command with the wall-clock time and how long it has been since the Director
    last replied, so the Director can reason about elapsed time (how long the previous task or
    the user took). The raw command is still what gets logged/shown; only the copy sent to the
    Director carries this header."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if last_reply_at is None:
        gap = "session just started"
    else:
        gap = f"{time.monotonic() - last_reply_at:.0f}s since your last reply"
    return f"[clock {now} | {gap}]\n\n{cmd}"


class SpeechTail:
    """Speech as the ROBOT recorded it, not as the message stream happened to surface it.

    Detecting speech from `speak` tool-use blocks in the SDK stream only works while the speaker
    is a DIRECT child of the top-level agent. It is not in V2: there the speaker is a grandchild
    (Director → dialogue-manager → regular-utterance), and a grandchild's tool calls never reach
    the top-level stream — so the robot talks, the harness hears nothing, and (in an evaluation
    run) the simulated user is told the robot was silent and starts asking "can you hear me?"
    while Misty is in fact answering.

    The MCP server's tool log is the authoritative record: one line per call, opened/written/
    closed under a lock, from the server process, at any nesting depth. This tails it."""

    def __init__(self, path):
        self.path = str(path)
        self._offset = 0
        #: when the robot last did ANYTHING (any tool call) — liveness the SDK stream can't show
        self.last_activity: float | None = None

    def drain(self) -> list[dict]:
        """Every `speak` logged since the last call, in order. Also notes any other tool call,
        as evidence that work is still in flight (see `last_activity`)."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        # Only consume whole lines — the server may be mid-write on the last one.
        cut = chunk.rfind("\n")
        if cut < 0:
            return []
        self._offset += len(chunk[:cut + 1].encode("utf-8"))
        self.last_activity = time.monotonic()      # the robot did something just now

        out = []
        for line in chunk[:cut].splitlines():
            line = line.strip()
            if not line or '"speak"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("name") != "speak":
                continue
            args = entry.get("args") or {}
            out.append({"text": args.get("text", ""),
                        "friction_type": args.get("friction_type", "")})
        return out


def _terminal_status(msg):
    cls = type(msg).__name__
    if cls == "TaskNotificationMessage":
        return getattr(msg, "status", None)
    if cls == "TaskUpdatedMessage":
        return (getattr(msg, "patch", None) or {}).get("status")
    return None


def raw(msg) -> str:
    """Everything about one message, for FULL-level transcripts."""
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


@asynccontextmanager
async def robot_session(options):
    """An open SDK session plus a queue of its messages: `(client, recv)`.

    The reader task is never cancelled mid-turn (only at session end), so the SDK's message
    generator is never left corrupted."""
    async with ClaudeSDKClient(options=options) as client:
        send, recv = anyio.create_memory_object_stream(max_buffer_size=math.inf)
        async with anyio.create_task_group() as tg:
            async def reader():
                try:
                    async for msg in client.receive_messages():
                        send.send_nowait(msg)
                finally:
                    send.close()

            tg.start_soon(reader)
            try:
                yield client, recv
            finally:
                tg.cancel_scope.cancel()          # stop the reader; end the session


class TurnEngine:
    """Reads the message stream one user turn at a time, for the life of a session.

    Session-scoped state (which delegated tasks have been seen and which have already reported
    a terminal status) lives on the instance, which is what makes cross-turn dedup possible —
    so create ONE engine per session, not one per turn."""

    def __init__(self, emit, on_speech, *, trace=None, speech_log=None,
                 max_collect_s: float | None = None, quiet_s: float = QUIET_S):
        self.emit = emit
        self.on_speech = on_speech
        self.trace = trace or (lambda event: None)
        # Where speech is counted from. With a tool log, THAT is the source of truth (it sees
        # every speaker at any delegation depth — see SpeechTail); the stream then only feeds
        # the readable narrative. Without one, the stream is all there is.
        self.speech = SpeechTail(speech_log) if speech_log else None
        # tool_use_id of an `Agent` call -> the subagent it spawned. This is what turns
        # `parent_tool_use_id` on a later message into an agent NAME.
        self.agent_of: dict[str, str] = {}
        # None = a turn takes as long as it needs (the console default). A number is a safety
        # ceiling against a genuine hang — what an unattended batch run wants.
        self.max_collect_s = agent_main.MAX_COLLECT_S if max_collect_s is None else max_collect_s
        self.quiet_s = quiet_s
        self.labels: dict[str, str] = {}    # task_id -> subagent_type
        self.done: set[str] = set()         # task_ids that already reported terminal status

    def _who(self, msg) -> str:
        """Which agent produced this message: "director", or the subagent's name."""
        parent = getattr(msg, "parent_tool_use_id", None)
        if parent is None:
            return "director"
        return self.agent_of.get(parent, "subagent")

    # ---------------------------------------------------------------------- message routing
    def process(self, msg, turn):
        """Route one message to the right log level(s), surface speech, track delegated tasks.
        `turn` holds this turn's mutable bookkeeping (see `_new_turn`)."""
        cls = type(msg).__name__

        if isinstance(msg, AssistantMessage):
            who = self._who(msg)
            for b in msg.content:
                bcls = type(b).__name__
                if bcls == "ThinkingBlock" and getattr(b, "thinking", "").strip():
                    self.emit("DEBUG", f"[think:{who}] {b.thinking.strip()}")
                    self.trace({"kind": "think", "agent": who, "text": b.thinking.strip()})
                elif bcls == "TextBlock" and getattr(b, "text", "").strip():
                    self.emit("DEBUG", f"[{who}] {b.text.strip()}")
                    self.trace({"kind": "text", "agent": who, "text": b.text.strip()})
                elif bcls == "ToolUseBlock":
                    name, inp = b.name, (b.input or {})
                    if name in ("Agent", "Task"):
                        st = inp.get("subagent_type", "?")
                        fgbg = "background" if inp.get("run_in_background") else "foreground"
                        if getattr(b, "id", None):
                            self.agent_of[b.id] = st      # names its messages from here on
                        self.emit("DEBUG", f"[delegate→{st}] ({fgbg}) "
                                           f"{inp.get('description', '')} "
                                           f"| prompt={inp.get('prompt', '')}")
                        self.trace({"kind": "delegate", "agent": who, "to": st,
                                    "description": inp.get("description", ""),
                                    "prompt": inp.get("prompt", ""),
                                    "background": bool(inp.get("run_in_background"))})
                    elif name == SPEAK:
                        text = inp.get("text", "")
                        friction = inp.get("friction_type", "")
                        self.emit("DEBUG", f"[speak/{friction}] {text}")
                        self.trace({"kind": "speak", "agent": who, "text": text,
                                    "friction_type": friction})
                        turn["rendered"][text] += 1      # so the tail doesn't re-render it
                        if self.speech is None:
                            self._heard(turn, text, friction)
                    else:
                        self.emit("DEBUG", f"[tool:{who}] {name} input={inp}")
                        self.trace({"kind": "tool", "agent": who, "name": name, "input": inp})

        elif cls == "UserMessage":
            who = self._who(msg)
            for b in getattr(msg, "content", None) or []:
                if hasattr(b, "content"):
                    self.emit("DEBUG", f"[result→{who}] {b.content}")
                    # A tool result whose tool_use_id is one of our OWN delegations is not a
                    # tool result at all — it is the subagent's report coming back.
                    src = self.agent_of.get(getattr(b, "tool_use_id", None) or "")
                    if src:
                        self.trace({"kind": "report", "agent": src, "content": b.content})
                    else:
                        self.trace({"kind": "tool_result", "agent": who, "content": b.content})
                elif hasattr(b, "text") and b.text.strip():
                    # The brief a subagent was handed — already shown at the delegation, so it
                    # goes to the flat transcript only.
                    self.emit("DEBUG", f"[brief→{who}] {b.text.strip()}")

        elif isinstance(msg, ResultMessage):
            turn["result_seen"] = True
            turn["awaiting_continuation"] = False   # this turn produced its result
            turn["subtype"] = msg.subtype
            self.emit("INFO", f"● turn complete ({msg.subtype})")
            if msg.result:
                self.emit("DEBUG", f"[result] {msg.result}")

        elif isinstance(msg, SystemMessage):
            tid = agent_main._task_started_id(msg)
            if tid and tid not in self.labels:
                data = getattr(msg, "data", None) or {}
                st = data.get("subagent_type", "?")
                self.labels[tid] = st
                turn["pending"].add(tid)
                self.emit("INFO", f"  ▶ delegated to {st} — {data.get('description', '')}")
            done = agent_main._task_terminal_id(msg)
            if done:
                if done in self.done:
                    # A late/duplicate completion of a task already finished in an earlier turn.
                    self.emit("FULL", f"[stale task completion ignored] {done}")
                else:
                    self.done.add(done)
                    turn["pending"].discard(done)
                    st = self.labels.get(done, "?")
                    mark = "✓" if _terminal_status(msg) in ("completed", "succeeded") else "✗"
                    self.emit("INFO", f"  {mark} {st} {_terminal_status(msg)}")
                    self.trace({"kind": "task_end", "agent": st,
                                "status": _terminal_status(msg)})
                    # Only when this completion lands AFTER the Director already ended its turn
                    # (result_seen) is it the fragmented case (case A): the completion triggers
                    # a follow-up turn, so wait for it (bounded by last_done). If the Director
                    # is still mid-turn (case B — a foreground step it's about to act on), do
                    # NOT arm this; the loop keeps waiting for the real ResultMessage instead
                    # of a grace timeout.
                    if turn["result_seen"]:
                        turn["result_seen"] = False
                        turn["awaiting_continuation"] = True
                        turn["last_done"] = time.monotonic()
            self.emit("FULL", raw(msg))

        else:
            self.emit("FULL", raw(msg))

    # ------------------------------------------------------------------------ the turn loops
    @staticmethod
    def _new_turn():
        return {"pending": set(), "result_seen": False, "spoke": False,
                "awaiting_continuation": False, "last_done": None, "speech": [],
                "timed_out": False, "subtype": None,
                # texts already shown in the narrative from the stream, so a tail-sourced
                # duplicate of the same utterance isn't rendered twice
                "rendered": Counter(),
                #: when a message last arrived — half of the silence test (see SILENT_GRACE_S)
                "last_message": time.monotonic()}

    def _heard(self, turn, text: str, friction: str):
        """Record one utterance as delivered to the user."""
        self.emit("INFO", f"Misty: {text}")
        turn["speech"].append({"text": text, "friction_type": friction})
        turn["spoke"] = True
        self.on_speech(text, friction)

    def _drain_speech(self, turn):
        """Pull any speech the robot logged but the stream didn't surface."""
        if self.speech is None:
            return
        for said in self.speech.drain():
            if turn["rendered"][said["text"]] > 0:
                turn["rendered"][said["text"]] -= 1      # already in the narrative
            else:
                self.trace({"kind": "speak", "agent": "dialogue", "text": said["text"],
                            "friction_type": said["friction_type"]})
            self._heard(turn, said["text"], said["friction_type"])

    def drain_now(self, recv):
        """Consume everything currently buffered (stragglers / an inter-turn
        notification-triggered turn) WITHOUT blocking, so it can't merge into the next turn."""
        scratch = self._new_turn()
        n = 0
        while True:
            try:
                msg = recv.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream):
                break
            if n == 0:
                self.emit("DEBUG", "[between turns] draining buffered messages")
            n += 1
            self.process(msg, scratch)
        return scratch

    async def collect_turn(self, recv) -> dict:
        """Read one turn to completion and return its bookkeeping (`spoke`, `speech`, …).

        Ends only after the Director has finished AND no background task is still pending AND
        the stream has been quiet for `quiet_s` (so trailing completions are drained into this
        turn). Reads the queue (safe to time out), never the raw generator."""
        turn = self._new_turn()
        t0 = time.monotonic()
        while True:
            if time.monotonic() - t0 > self.max_collect_s:
                self.emit("INFO", "  ! turn timeout")
                turn["timed_out"] = True
                break
            msg = None
            with anyio.move_on_after(self.quiet_s) as scope:
                try:
                    msg = await recv.receive()
                except anyio.EndOfStream:
                    break
            if not scope.cancelled_caught:
                turn["last_message"] = time.monotonic()
                self.process(msg, turn)
            # Speech can come from a depth the stream never surfaces, so check the robot's own
            # record before every decision about whether this turn is finished.
            self._drain_speech(turn)
            # Once the Director's turn has ended AND the robot has delivered its spoken reply,
            # the command is complete — hand control straight back even if a fire-and-forget
            # background task (e.g. a world-model recording) is still running; it keeps going
            # and is drained on the next turn. `spoke` distinguishes a real answer from the
            # fragmented mid-cascade case, which hasn't spoken yet.
            if turn["spoke"] and turn["result_seen"]:
                break
            if scope.cancelled_caught:                    # quiet for quiet_s
                if turn["pending"]:
                    continue                              # a task is still working
                if turn["result_seen"]:
                    if turn["spoke"]:
                        break                             # answered and idle → done
                    # Nothing said yet. Deeper delegations don't register as `pending`, so hold
                    # the turn open until the robot has been genuinely quiet — no new messages
                    # and no tool calls — for SILENT_GRACE_S. Only then is it really silent.
                    idle_for = time.monotonic() - max(
                        turn["last_message"],
                        (self.speech.last_activity if self.speech else 0) or 0)
                    if idle_for > SILENT_GRACE_S:
                        break
                    continue
                if turn["awaiting_continuation"]:
                    # Case A (fragmented): the Director ended a turn and a background
                    # completion is expected to trigger a follow-up. Wait for it, but bound the
                    # wait so a completion with no follow-up can't hang the turn.
                    if (turn["last_done"]
                            and time.monotonic() - turn["last_done"] > CONTINUATION_GRACE_S):
                        break
                    continue
                # Case B: the Director is still mid-turn (no ResultMessage yet), just paused
                # between steps (e.g. reviewing a proposed plan — model-generation latency).
                # Keep waiting for its real ResultMessage; do NOT end on a timer (that
                # truncated the turn before).
                continue
        self._drain_speech(turn)          # anything logged during the final quiet window
        return turn
