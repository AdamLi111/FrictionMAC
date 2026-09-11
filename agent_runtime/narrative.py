"""
The readable log: who thought what, who was asked to do what, and what came back.

The `.log` transcript is a flat, timestamped stream of every SDK message — complete, but hard
to follow: a subagent's reasoning and the Director's reasoning look identical, tool results are
raw Python reprs, and one blob of thinking runs to 400 characters on a single line. This module
renders the SAME events as a story you can read top to bottom, with every line attributed to the
agent that produced it and nested under the delegation it belongs to.

Attribution comes from `AssistantMessage.parent_tool_use_id`: `None` means the Director, and any
other value is the `id` of the `Agent` tool call that spawned the subagent — so mapping those ids
to `subagent_type` (which `TurnEngine` does) names every thought and every tool call. Nothing is
inferred or reconstructed; this is a different rendering of the same stream, written alongside
the flat transcript rather than instead of it.

Both front ends write one: `data/eval_.../<task>_story.txt` per episode, and
`data/hw_session_<ts>_story.txt` per console session.
"""
import json
import re
import textwrap
import time

WIDTH = 94                  # wrap column for prose
MAX_PAYLOAD = 1500          # chars of any single tool result / report kept verbatim
_BAR = "─"

# Harness boilerplate that carries no information about what the robot did. The launch ack is
# what the `Agent` tool returns for a background delegation; the trailers are appended to every
# subagent report by the CLI.
_LAUNCH_ACK = "Async agent launched successfully"
_TRAILERS = (
    re.compile(r"^\s*agentId:.*$", re.M),                       # internal id + SendMessage hint
    re.compile(r"<usage>.*?</usage>", re.S),                    # token/duration block
)


def _strip_trailers(text: str) -> str:
    for pattern in _TRAILERS:
        text = pattern.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _compact(text: str) -> str:
    """A tool's JSON result on one line — `{"ok": true, "arm": "right"}` reads better as
    `ok=true, arm=right` than as four pretty-printed lines."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return text
    def scalar(v):
        if isinstance(v, bool) or v is None:
            return json.dumps(v)            # true / false / null, not Python's True/None
        return v if isinstance(v, str) else json.dumps(v)

    if isinstance(value, dict):
        return ", ".join(f"{k}={scalar(v)}" for k, v in value.items())
    return text


def _looks_like_base64(text: str) -> bool:
    """A camera frame, not prose: long, unbroken, and made of base64 characters."""
    head = text[:400]
    return len(text) > 600 and " " not in head and "\n" not in head


def _clean_payload(value) -> str:
    """A tool result / report as readable text, with camera frames and giant blobs collapsed.

    Real-mode `capture_view` returns a base64 JPEG; dumping that into a human log would bury
    everything else in it, so it becomes a one-line placeholder."""
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                if block.get("type") == "image":
                    src = block.get("source") or {}
                    data = src.get("data") or ""
                    parts.append(f"<camera frame, {len(data) // 1024} KB>")
                    continue
                text = block.get("text") or ""
            else:
                text = str(block)
            parts.append(f"<camera frame, {len(text) // 1024} KB>"
                         if _looks_like_base64(text) else text)
        value = "\n".join(p for p in parts if p)
    text = str(value if value is not None else "")
    if _looks_like_base64(text):
        return f"<camera frame, {len(text) // 1024} KB>"
    if len(text) > MAX_PAYLOAD:
        return text[:MAX_PAYLOAD] + f"\n… [+{len(text) - MAX_PAYLOAD} more characters]"
    return text


class NarrativeLog:
    """One readable transcript. Fed the structured events `TurnEngine` emits."""

    def __init__(self, path, *, width: int = WIDTH):
        self.f = open(path, "a", encoding="utf-8")
        self.width = width
        self.turn = 0
        self._counts = {"delegations": 0, "tools": 0, "utterances": 0}
        self._last_tool: dict[str, str] = {}   # agent -> the tool it called most recently

    # ------------------------------------------------------------------------------ plumbing
    def _write(self, text: str = ""):
        self.f.write(text + "\n")
        self.f.flush()

    def _block(self, text: str, indent: int):
        """Wrapped prose at `indent`, keeping the author's own paragraph breaks."""
        pad = " " * indent
        for para in (text or "").strip().split("\n"):
            para = para.rstrip()
            if not para:
                self._write()
                continue
            for line in textwrap.wrap(para, width=self.width - indent) or [""]:
                self._write(pad + line)

    @staticmethod
    def _depth(agent: str) -> int:
        """The Director sits one level in; everyone it delegates to sits one level deeper."""
        return 2 if agent == "director" else 5

    @staticmethod
    def _label(agent: str) -> str:
        return "DIRECTOR" if agent == "director" else agent

    # -------------------------------------------------------------------------------- header
    def header(self, title: str, fields: dict):
        self._write("=" * self.width)
        self._write(f"  {title}")
        for k, v in fields.items():
            if v is None:
                continue
            self._write(f"  {k}:")
            self._block(str(v), 6)
        self._write("=" * self.width)
        self._write()

    # ---------------------------------------------------------------------------- the events
    def user_turn(self, n: int, text: str):
        self.turn = n
        self._counts = {"delegations": 0, "tools": 0, "utterances": 0}
        self._write()
        stamp = time.strftime("%H:%M:%S")
        head = f"{_BAR * 4} TURN {n} "
        self._write(head + _BAR * max(1, self.width - len(head) - len(stamp) - 1) + f" {stamp}")
        self._write()
        self._write("  USER ▶")
        self._block(text, 8)
        self._write()

    def event(self, e: dict):
        kind, agent = e.get("kind"), e.get("agent", "director")
        indent = self._depth(agent)
        who = self._label(agent)

        if kind == "think":
            self._write(f"{' ' * indent}{who} · thinking")
            self._block(e["text"], indent + 4)
            self._write()

        elif kind == "text":
            # The Director's own message text is never heard by the user; a subagent's is its
            # report in progress. Either way it is internal — labelled as such.
            self._write(f"{' ' * indent}{who} · note (internal, not spoken)")
            self._block(e["text"], indent + 4)
            self._write()

        elif kind == "delegate":
            self._counts["delegations"] += 1
            mode = "background" if e.get("background") else "foreground"
            self._write(f"{' ' * indent}{who} ▶ delegates to {e['to']}  ({mode})")
            if e.get("description"):
                self._write(f"{' ' * (indent + 4)}task: {e['description']}")
            if e.get("prompt"):
                self._write(f"{' ' * (indent + 4)}brief:")
                self._block(e["prompt"], indent + 8)
            self._write()

        elif kind == "tool":
            self._counts["tools"] += 1
            self._last_tool[agent] = e["name"]
            args = ", ".join(f"{k}={v!r}" for k, v in (e.get("input") or {}).items())
            self._write(f"{' ' * indent}{who} · calls {e['name']}({args})")

        elif kind == "tool_result":
            # `speak`'s own result is just {"ok": true} echoed back — the utterance itself is
            # already shown, so the echo would only add noise.
            if self._last_tool.get(agent, "").endswith("speak"):
                return
            body = _compact(_clean_payload(e.get("content")))
            if body.strip():
                self._block(f"→ {body}", indent + 4)
            self._write()

        elif kind == "speak":
            self._counts["utterances"] += 1
            self._last_tool[agent] = "speak"      # so the {"ok": true} echo is suppressed
            friction = e.get("friction_type") or "none"
            self._write(f"{' ' * indent}🔊 MISTY SPEAKS   (friction: {friction})")
            self._block(f'"{e["text"]}"', indent + 4)
            self._write()

        elif kind == "report":
            body = _clean_payload(e.get("content"))
            if _LAUNCH_ACK in body:
                # Not a report: the Agent tool's acknowledgement that a background delegation
                # started. The real report arrives later, as its own event.
                self._write(f"{' ' * indent}({who} is now running in the background — its "
                            f"report arrives later)")
                self._write()
                return
            self._write(f"{' ' * indent}{who} ▪ reports back to the Director")
            self._block(_strip_trailers(body), indent + 4)
            self._write()

        elif kind == "task_end":
            mark = "✓" if e.get("status") in ("completed", "succeeded") else "✗"
            self._write(f"{' ' * indent}{mark} {who} finished ({e.get('status')})")
            self._write()

    def no_speech(self):
        self._write("  (the robot said nothing this turn — the user hears silence)")

    def turn_end(self, *, subtype: str | None = None, seconds: float | None = None,
                 timed_out: bool = False):
        bits = [f"turn {self.turn} ended"]
        if timed_out:
            bits.append("CUT OFF (turn timeout)")
        elif subtype:
            bits.append(subtype)
        if seconds is not None:
            bits.append(f"{seconds:.0f}s")
        bits += [f"{self._counts['delegations']} delegation(s)",
                 f"{self._counts['tools']} tool call(s)",
                 f"{self._counts['utterances']} utterance(s)"]
        self._write(f"  {_BAR * 2} " + " · ".join(bits))

    def footer(self, text: str):
        self._write()
        self._write("=" * self.width)
        self._block(text, 2)
        self._write("=" * self.width)

    def close(self):
        self.f.close()
