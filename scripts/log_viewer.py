"""
See a session's agents at work: a page on localhost that draws the team, the user's turns, who
told whom what, in order, and what each agent was thinking.

    python -m scripts.log_viewer data/hw_session_20260928_160525_story.txt   # one session
    python -m scripts.log_viewer data/eval_v2_20260922_002856                # a run: pick an episode
    python -m scripts.log_viewer --latest                                    # the newest log in data/
  options: --port 8766, --open (open the page in a browser)

Use the agent env (.venv-agent/bin/python) to read an older log: it can then draw every agent of
the architecture, including ones the session never called. Any Python 3.10+ works otherwise; the
viewer needs nothing beyond the standard library.

It reads the logs every console session and evaluation episode already writes, for one prefix:
  <x>_trace.jsonl   every event as JSON, timed, with the team's hierarchy (newer sessions)
  <x>_story.txt     the readable story; parsed instead, for older sessions without a trace
  <x>_events.jsonl  the hook log: which agents started, and every tool call at any depth. It
                    fills in what the main stream never shows (V2's experts, most of all)
  <x>_sensors.jsonl every distance, ledge and bump reading the robot reported (console sessions):
                    shown live, and as a chart of each turn with its steps marked on it
and keeps re-reading them, so a session that is still running updates on the page as it goes.
The run commands start the viewer for you with --live:
    .venv-agent/bin/python -m scripts.hw_console --live
    .venv-agent/bin/python -m evaluation.run_tasks --live ...
"""
import argparse
import json
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).with_name("log_viewer.html")
REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
DEFAULT_PORT = 8766
POLL_S = 0.25

DIRECTOR, USER, ROBOT, UNKNOWN = "director", "user", "robot", "?"
_SUFFIXES = ("_trace.jsonl", "_story.txt", "_events.jsonl", "_sensors.jsonl", "_tools.jsonl",
             "_beliefs.json",
             "_sim.json", ".debug.log", ".info.log", ".full.log", ".json", ".log")
_LAUNCH_ACK = "Async agent launched successfully"
_TRAILERS = (re.compile(r"^\s*agentId:.*$", re.M), re.compile(r"<usage>.*?</usage>", re.S))
#: The stream's names for an agent it can't name: speech the robot logged from deeper down
#: (session.SpeechTail), or a subagent whose spawn it never saw.
_PLACEHOLDERS = {"dialogue", "subagent", "?"}


# ================================================================================ finding logs
class Run:
    """One session or episode: the files that share a prefix."""

    def __init__(self, prefix: Path):
        self.prefix = Path(prefix)
        self.trace = self._file("_trace.jsonl")
        self.story = self._file("_story.txt")
        self.events = self._file("_events.jsonl")

    @property
    def sensors(self):
        """The sensor log, looked up each time: it appears when the robot first reports."""
        return self._file("_sensors.jsonl")

    def _file(self, suffix):
        path = Path(str(self.prefix) + suffix)
        return path if path.exists() else None

    @property
    def exists(self) -> bool:
        return bool(self.trace or self.story)

    @property
    def id(self) -> str:
        try:
            return str(self.prefix.resolve().relative_to(REPO))
        except ValueError:
            return str(self.prefix.resolve())

    def files(self) -> list:
        return [f for f in (self.trace, self.story, self.events) if f]

    def signature(self) -> tuple:
        """Changes whenever one of its files does (or appears). Not the sensor log: that grows
        many times a second and is streamed on its own (read_sensor_rows)."""
        files = [Path(str(self.prefix) + s) for s in ("_trace.jsonl", "_story.txt",
                                                       "_events.jsonl")]
        return tuple((f.stat().st_size, f.stat().st_mtime_ns) if f.exists() else None
                     for f in files)

    def mtime(self) -> float:
        return max((f.stat().st_mtime for f in self.files()), default=0.0)


def prefix_of(path: Path) -> Path:
    text = str(path)
    for suffix in _SUFFIXES:
        if text.endswith(suffix):
            return Path(text[:-len(suffix)])
    return path


def runs_in(directory: Path) -> list:
    """Every session or episode with a trace or story directly in `directory`, newest last."""
    prefixes = {prefix_of(p) for p in directory.glob("*_trace.jsonl")}
    prefixes |= {prefix_of(p) for p in directory.glob("*_story.txt")}
    return sorted((Run(p) for p in prefixes), key=lambda r: r.mtime())


def newest_log(root: Path = DATA_DIR):
    """The newest trace or story anywhere under `root`."""
    found = [p for p in root.rglob("*_story.txt")] + [p for p in root.rglob("*_trace.jsonl")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


# ================================================================================ reading
def _strip_trailers(text: str) -> str:
    for pattern in _TRAILERS:
        text = pattern.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text(content) -> str:
    """A report or tool result as plain text (the trace already collapsed camera frames)."""
    if isinstance(content, list):
        return "\n".join((b.get("text") or "") if isinstance(b, dict) else str(b)
                         for b in content).strip()
    return "" if content is None else str(content)


def read_trace(path: Path):
    """(header, records) from a `_trace.jsonl`. A half-written last line is skipped."""
    header, records = {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "header":
                header = record
            else:
                records.append(record)
    return header, records


# ---- the readable story, for logs from before the trace existed ----------------------------
_TURN = re.compile(r"^─+ TURN (\d+) ─+ (\d{1,2}:\d{2}:\d{2})\s*$")
_ITEMS = [
    ("user", re.compile(r"^USER ▶$")),
    ("think", re.compile(r"^(\S+) · thinking$")),
    ("text", re.compile(r"^(\S+) · note \(internal, not spoken\)$")),
    ("delegate", re.compile(r"^(\S+) ▶ delegates to (\S+)\s+\((background|foreground)\)$")),
    ("tool", re.compile(r"^(\S+) · calls ([^\s(]+)\((.*)\)$")),
    ("speak", re.compile(r"^🔊 MISTY SPEAKS\s+\(friction: (.*)\)$")),
    ("report", re.compile(r"^(\S+) ▪ reports back to the Director$")),
    ("background", re.compile(r"^\((\S+) is now running in the background")),
    ("refused", re.compile(r"^✗ (\S+) did not report: the delegation was refused or failed$")),
    ("task_end", re.compile(r"^(?:✓|✗) (\S+) finished \((.*)\)$")),
    ("no_speech", re.compile(r"^\(the robot said nothing this turn")),
    ("turn_end", re.compile(r"^── turn (\d+) ended · (.*)$")),
]
_LIST_ITEM = re.compile(r"^(\d+\.|[-*•]|\*\*)\s")


def _agent(label: str) -> str:
    return DIRECTOR if label in ("DIRECTOR", "director", "main") else label


def _reflow(lines: list, width: int = 94) -> str:
    """Undo the story's word-wrapping: rejoin a line the writer broke because it was full,
    keep the author's own line breaks (short lines, list items, blank lines)."""
    out = []
    for line in lines:
        text = line.strip()
        if (out and out[-1] and text and not _LIST_ITEM.match(text)
                and out[-1].endswith("\x00")):
            out[-1] = out[-1][:-1] + " " + text
        else:
            out.append(text)
        if text and len(line.rstrip()) >= width - 22:
            out[-1] += "\x00"                       # looks wrapped: the next line continues it
    return "\n".join(s.rstrip("\x00") for s in out).strip()


def read_story(path: Path):
    """(header, records) from a `_story.txt`, in the trace's record format. Records carry no
    times except a turn's start; a speech's speaker is guessed from where it sits."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    header, records = {"title": "", "fields": {}}, []
    i = 0
    if lines and lines[0].startswith("="):          # ==== title / fields ====
        i = 1
        key = None
        while i < len(lines) and not lines[i].startswith("="):
            line = lines[i]
            if line.startswith("      ") and key:
                header["fields"][key] = (header["fields"][key] + " " + line.strip()).strip()
            elif line.strip().endswith(":") and line.startswith("  "):
                key = line.strip()[:-1]
                header["fields"][key] = ""
            elif line.strip() and not header["title"]:
                header["title"] = line.strip()
            i += 1
        i += 1
    arch = (header["fields"].get("architecture") or "").split(" ")[0]
    header["arch"] = arch or None

    turn, item, context = 0, None, DIRECTOR

    def close():
        nonlocal item
        if item is None:
            return
        body = [b for b in item.pop("body")]
        kind = item["kind"]
        if kind == "delegate":
            text_lines, brief = [], False
            for indent, text in body:
                if text.startswith("task: ") and not brief:
                    item["description"] = text[len("task: "):]
                elif text == "brief:":
                    brief = True
                else:
                    text_lines.append(" " * indent + text)
            item["prompt"] = _reflow(text_lines)
        elif kind == "tool":
            result = [" " * ind + t for ind, t in body]
            text = _reflow(result)
            if text.startswith("→ "):
                records.append(dict(item))
                item = {"type": "event", "turn": turn, "kind": "tool_result",
                        "agent": item["agent"], "content": text[2:]}
                records.append(item)
                item = None
                return
        else:
            text = _reflow([" " * ind + t for ind, t in body])
            if kind == "speak":
                text = text.strip()
                if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
                    text = text[1:-1]
                item["text"] = text
            elif kind in ("report",):
                item["content"] = text
            elif kind == "user":
                item["text"] = text
            elif kind in ("think", "text"):
                item["text"] = text
        records.append(item)
        item = None

    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("="):                    # the footer
            close()
            break
        m = _TURN.match(line.strip())
        if m:
            close()
            turn = int(m.group(1))
            records.append({"type": "user", "turn": turn, "text": "", "clock": m.group(2)})
            context = DIRECTOR
            continue
        stripped = line.strip()
        if not stripped:
            if item is not None:
                item["body"].append((0, ""))
            continue
        indent = len(line) - len(line.lstrip(" "))
        matched = None
        if indent in (2, 5):
            for kind, pattern in _ITEMS:
                m = pattern.match(stripped)
                if m:
                    matched = (kind, m)
                    break
        if matched is None:
            if item is not None:
                base = item["indent"] + 4
                item["body"].append((max(0, indent - base), stripped))
            continue
        close()
        kind, m = matched
        if kind == "user":
            # the TURN line already opened the turn; its text is this item's body
            item = {"type": "user_text", "turn": turn, "kind": "user", "indent": indent,
                    "body": []}
        elif kind in ("think", "text"):
            agent = _agent(m.group(1))
            context = agent if agent != DIRECTOR else context
            item = {"type": "event", "turn": turn, "kind": kind, "agent": agent,
                    "indent": indent, "body": []}
        elif kind == "delegate":
            item = {"type": "event", "turn": turn, "kind": "delegate",
                    "agent": _agent(m.group(1)), "to": m.group(2),
                    "background": m.group(3) == "background", "indent": indent, "body": []}
        elif kind == "tool":
            agent = _agent(m.group(1))
            if agent != DIRECTOR:
                context = agent
            item = {"type": "event", "turn": turn, "kind": "tool", "agent": agent,
                    "name": m.group(2), "args_text": m.group(3), "input": {},
                    "indent": indent, "body": []}
        elif kind == "speak":
            item = {"type": "event", "turn": turn, "kind": "speak",
                    "agent": DIRECTOR if indent == 2 else context, "guessed": indent != 2,
                    "friction_type": m.group(1), "indent": indent, "body": []}
        elif kind in ("report", "refused"):
            item = {"type": "event", "turn": turn, "kind": "report", "agent": m.group(1),
                    "error": kind == "refused", "indent": indent, "body": []}
        elif kind == "background":
            records.append({"type": "event", "turn": turn, "kind": "report",
                            "agent": m.group(1), "content": _LAUNCH_ACK})
        elif kind == "task_end":
            records.append({"type": "event", "turn": turn, "kind": "task_end",
                            "agent": m.group(1), "status": m.group(2)})
        elif kind == "no_speech":
            records.append({"type": "no_speech", "turn": turn})
        elif kind == "turn_end":
            records.append({"type": "turn_end", "turn": int(m.group(1)),
                            "summary": m.group(2)})
    close()
    # fold each USER ▶ body into its turn's record
    out, pending_turn = [], None
    for record in records:
        if record.get("type") == "user":
            pending_turn = record
            out.append(record)
        elif record.get("type") == "user_text":
            if pending_turn is not None:
                pending_turn["text"] = record.get("text", "")
        else:
            out.append(record)
    for record in out:
        record.pop("indent", None)
    return header, out


def read_sensor_rows(path: Path, offset: int = 0):
    """(rows, next_offset): the sensor log's complete lines from byte `offset` on. A line still
    being written is left for next time."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    rows = []
    for line in data[:end].splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows, offset + end


def read_hooks(path: Path) -> list:
    """The hook log as [{"t": epoch, "event", "agent", "agent_id", "tool", "input"}]."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                raw = json.loads(line)
                t = datetime.fromisoformat(raw["timestamp"]).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            rows.append({"t": t, "event": raw.get("event"), "agent": raw.get("agent_type"),
                         "agent_id": raw.get("agent_id"), "tool": raw.get("tool"),
                         "input": raw.get("input") or {}})
    return rows


# ================================================================================ the model
def _short_tool(name: str) -> str:
    return name.split("__")[-1] if name else ""


def _call(name: str, args) -> str:
    if isinstance(args, dict):
        inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}"
                          for k, v in args.items())
    else:
        inner = str(args or "")
    return f"{_short_tool(name)}({inner})"


def _architecture_hierarchy(arch):
    """{agent: parent} for a named architecture: from the architecture itself when the Agent SDK
    is importable (the agent env), else V2's known shape, else nothing (then it's inferred)."""
    if not arch:
        return {}
    try:
        from agent_runtime import architectures
        return dict(architectures.get(arch).hierarchy())
    except BaseException:           # no SDK in this env, or an architecture it can't build
        pass
    if arch == "v2":
        from agent_runtime import topology
        return topology.v2_parents()
    return {}


class _Builder:
    """Turns a run's records (+ hook log) into the page's model."""

    def __init__(self, run: Run):
        self.run = run
        self.notes = []
        if run.trace:
            self.header, self.records = read_trace(run.trace)
            self.format = "trace"
        else:
            self.header, self.records = read_story(run.story)
            self.format = "story"
            self.notes.append("This log predates the JSON trace, so it was read from the story "
                              "text: line breaks are approximate, and steps known only from the "
                              "hook log are listed at the end of their turn.")
        self.hooks = read_hooks(run.events) if run.events else []
        self.turns = []
        self.delegator = {}         # agent -> who last delegated to it (its report goes there)
        self.id_to_agent = {h["agent_id"]: h["agent"] for h in self.hooks
                            if h.get("agent_id") and h.get("agent")}

    # ---- steps -------------------------------------------------------------------------
    def _turn(self, n, record=None):
        while len(self.turns) < max(n, 1):
            k = len(self.turns) + 1
            self.turns.append({"n": k, "user": "", "t": None, "clock": None, "steps": [],
                               "silent": False, "end": None})
        turn = self.turns[max(n, 1) - 1]
        return turn

    def _resolve(self, name):
        if not name:
            return UNKNOWN
        name = str(name)
        if name in ("main", "director", "DIRECTOR", "team-lead"):
            return DIRECTOR
        return self.id_to_agent.get(name, name)

    def _add(self, turn, step):
        step.setdefault("source", "log")
        turn["steps"].append(step)
        return step

    def build_steps(self):
        last_tool = {}              # agent -> its last tool/message step, for the result
        for r in self.records:
            kind_type = r.get("type")
            turn = self._turn(r.get("turn") or 1)
            t = r.get("t")
            if kind_type == "user":
                turn["user"] = r.get("text", "")
                turn["t"] = t
                turn["clock"] = r.get("clock") or (
                    time.strftime("%H:%M:%S", time.localtime(t)) if t else None)
                self._add(turn, {"kind": "ask", "from": USER, "to": DIRECTOR,
                                 "text": r.get("text", ""), "t": t})
                continue
            if kind_type == "no_speech":
                turn["silent"] = True
                continue
            if kind_type == "turn_end":
                turn["end"] = {k: r.get(k) for k in ("subtype", "seconds", "timed_out",
                                                      "counts", "summary") if r.get(k)}
                continue
            if kind_type != "event":
                continue
            kind = r.get("kind")
            agent = self._resolve(r.get("agent") or DIRECTOR)
            if kind in ("think", "text"):
                self._add(turn, {"kind": "think" if kind == "think" else "note",
                                 "agent": agent, "text": r.get("text", ""), "t": t})
            elif kind == "delegate":
                to = self._resolve(r.get("to"))
                self.delegator[to] = agent
                self._add(turn, {"kind": "delegate", "from": agent, "to": to,
                                 "title": r.get("description", ""),
                                 "text": r.get("prompt", ""),
                                 "background": bool(r.get("background")), "t": t})
            elif kind == "tool":
                name = r.get("name", "")
                args = r.get("input") or {}
                if _short_tool(name) == "SendMessage":
                    to = self._resolve(args.get("to") or args.get("recipient"))
                    step = self._add(turn, {
                        "kind": "message", "from": agent, "to": to,
                        "title": args.get("summary", ""),
                        "text": str(args.get("message") or args.get("content") or ""),
                        "t": t})
                else:
                    call = _call(name, args) if args else (
                        f"{_short_tool(name)}({r.get('args_text', '')})")
                    step = self._add(turn, {"kind": "tool", "from": agent,
                                            "to": ROBOT if name.startswith("mcp__") or
                                            "__" not in name else None,
                                            "title": call, "text": call,
                                            "tool": _short_tool(name), "t": t})
                last_tool[agent] = step
            elif kind == "tool_result":
                step = last_tool.pop(agent, None)
                if step is not None:
                    step["result"] = _text(r.get("content"))
            elif kind == "speak":
                speaker = agent if agent not in _PLACEHOLDERS else UNKNOWN
                self._add(turn, {"kind": "speak", "from": speaker, "to": USER,
                                 "text": r.get("text", ""),
                                 "friction": r.get("friction_type") or "none",
                                 "guessed": bool(r.get("guessed")) or speaker == UNKNOWN,
                                 "t": t})
            elif kind == "report":
                body = _text(r.get("content"))
                if _LAUNCH_ACK in body:
                    for step in reversed(turn["steps"]):
                        if step.get("kind") == "delegate" and step.get("to") == agent:
                            step["background"] = True
                            break
                    continue
                self._add(turn, {"kind": "report", "from": agent,
                                 "to": self.delegator.get(agent, DIRECTOR),
                                 "text": _strip_trailers(body), "error": bool(r.get("error")),
                                 "t": t})
            elif kind == "task_end":
                self._add(turn, {"kind": "end", "agent": agent,
                                 "text": f"{agent} finished ({r.get('status')})",
                                 "status": r.get("status"), "t": t})

    # ---- the hook log -----------------------------------------------------------------
    def merge_hooks(self):
        """Fill in what the main stream missed: who really spoke an utterance the stream
        couldn't attribute, and the work of agents it never showed at all."""
        if not self.hooks:
            return
        seen = set()
        for turn in self.turns:
            for step in turn["steps"]:
                for key in ("from", "agent"):
                    if step.get(key) and step.get("source") == "log":
                        seen.add(step[key])
        starts = [turn["t"] for turn in self.turns]
        if self.format == "story":
            starts = self._story_turn_times()
        timed = self.format == "trace"
        added = 0
        for h in self.hooks:
            agent = h["agent"]
            if not agent:
                continue                       # the Director: the stream has it all
            turn = self._turn_at(h["t"], starts)
            if turn is None:
                continue
            tool = _short_tool(h.get("tool") or "")
            if h["event"] == "pre_tool_use" and tool == "speak":
                text = str((h.get("input") or {}).get("text", ""))
                same = [s for s in turn["steps"] if s.get("kind") == "speak"
                        and s.get("text") == text]
                if not same:            # timed into a neighbouring turn: the words decide
                    same = [s for t_ in self.turns for s in t_["steps"]
                            if s.get("kind") == "speak" and s.get("text") == text
                            and (s.get("guessed") or s.get("from") == UNKNOWN)]
                unnamed = next((s for s in same if s.get("guessed")
                                or s.get("from") == UNKNOWN), None)
                if unnamed is not None:
                    unnamed["from"], unnamed["guessed"] = agent, False
                    continue
                if same:
                    continue
                if agent in seen:
                    continue
                self._add(turn, {"kind": "speak", "from": agent, "to": USER, "text": text,
                                 "friction": (h.get("input") or {}).get("friction_type", "none"),
                                 "t": h["t"], "source": "hooks"})
                added += 1
                continue
            if agent in seen:
                continue
            if h["event"] == "subagent_start":
                parent = self.delegator.get(agent) or self.parents.get(agent, DIRECTOR)
                self._add(turn, {"kind": "delegate", "from": parent, "to": agent,
                                 "title": "(started)", "t": h["t"], "source": "hooks",
                                 "text": "The log records that it started, not its brief."})
                added += 1
            elif h["event"] == "pre_tool_use":
                args = h.get("input") or {}
                if tool == "SendMessage":
                    self._add(turn, {"kind": "message", "from": agent,
                                     "to": self._resolve(args.get("to")),
                                     "title": args.get("summary", ""),
                                     "text": str(args.get("message") or ""),
                                     "t": h["t"], "source": "hooks"})
                elif tool in ("Agent", "Task"):
                    to = self._resolve(args.get("subagent_type"))
                    self.delegator[to] = agent
                    self._add(turn, {"kind": "delegate", "from": agent, "to": to,
                                     "title": args.get("description", ""),
                                     "text": args.get("prompt", ""), "t": h["t"],
                                     "background": bool(args.get("run_in_background")),
                                     "source": "hooks"})
                else:
                    call = _call(h.get("tool") or "", args)
                    self._add(turn, {"kind": "tool", "from": agent, "to": ROBOT,
                                     "title": call, "text": call, "tool": tool,
                                     "t": h["t"], "source": "hooks"})
                added += 1
        if added:
            for turn in self.turns:
                if timed:
                    turn["steps"].sort(key=lambda s: (s.get("t") is None, s.get("t") or 0))
                else:
                    log = [s for s in turn["steps"] if s.get("source") == "log"]
                    extra = sorted((s for s in turn["steps"] if s.get("source") == "hooks"),
                                   key=lambda s: s.get("t") or 0)
                    turn["steps"] = log + extra
            self.notes.append("Steps marked 'from the hook log' come from the tool-call log: "
                              "these agents never appeared in the main stream, so only what "
                              "they started, called and said is known, not their reasoning.")

    def _story_turn_times(self):
        """Each turn's start as a time, from its HH:MM:SS and the hook log's date."""
        if not self.hooks:
            return [None for _ in self.turns]
        day = datetime.fromtimestamp(self.hooks[0]["t"]).date()
        times, previous = [], None
        for turn in self.turns:
            clock = turn.get("clock")
            if not clock:
                times.append(None)
                continue
            stamp = datetime.combine(day, datetime.strptime(clock, "%H:%M:%S").time())
            t = stamp.timestamp()
            if previous is not None and t < previous - 3600:    # crossed midnight
                t += 86400
            times.append(t)
            previous = t
        return times

    def _turn_at(self, t, starts):
        """The turn running at time `t`: the last one that started by then."""
        best = None
        for turn, start in zip(self.turns, starts):
            if start is not None and start <= t + 0.05:
                best = turn
        if best is None and self.turns:
            best = self.turns[0]
        return best

    # ---- the hierarchy ------------------------------------------------------------------
    def hierarchy(self):
        parents = dict(self.header.get("hierarchy") or {})
        if not parents:
            parents = _architecture_hierarchy(self.header.get("arch"))
            if parents:
                self.notes.append("The team was drawn from the architecture's definition.")
        self.parents = parents
        return parents

    def complete_hierarchy(self):
        """Add any agent the log shows but the hierarchy lacks, under whoever delegated to it."""
        for turn in self.turns:
            for step in turn["steps"]:
                for key in ("from", "to", "agent"):
                    name = step.get(key)
                    if name in (None, DIRECTOR, USER, ROBOT, UNKNOWN) or name in self.parents:
                        continue
                    self.parents[name] = self.delegator.get(name, DIRECTOR)

    # ---- assembly -----------------------------------------------------------------------
    def build(self) -> dict:
        self.hierarchy()
        self.build_steps()
        self.merge_hooks()
        self.complete_hierarchy()
        for turn in self.turns:
            for i, step in enumerate(turn["steps"], 1):
                step["i"] = i
        header = self.header
        return {
            "title": header.get("title") or self.run.prefix.name,
            "fields": header.get("fields") or {},
            "arch": header.get("arch"),
            "hierarchy": self.parents,
            "turns": self.turns,
            "notes": self.notes,
            "run": self.run.id,
            "format": self.format,
            "files": [str(f.name) for f in self.run.files()],
            "sensors": self.run.sensors is not None,
            "updated": self.run.mtime(),
            "ended": any(r.get("type") == "footer" for r in self.records),
        }


def build_model(run: Run) -> dict:
    return _Builder(run).build()


# ================================================================================ serving
class Viewer:
    """What the server shows: one run, or every run in a directory (newest first, and with
    `follow`, always the newest)."""

    def __init__(self, target: Path, follow: bool = False):
        target = Path(target)
        self.directory = target if target.is_dir() else None
        self.single = None if self.directory else Run(prefix_of(target))
        self.follow = follow
        self._cache = {}
        self._lock = threading.Lock()

    def runs(self) -> list:
        if self.single is not None:
            return [self.single]
        return list(reversed(runs_in(self.directory)))

    def find(self, run_id):
        runs = self.runs()
        if not runs:
            return None
        if not run_id or run_id == "latest":
            return runs[0]
        return next((r for r in runs if r.id == run_id), None)

    def model(self, run: Run) -> dict:
        signature = run.signature()
        with self._lock:
            cached = self._cache.get(run.id)
            if cached and cached[0] == signature:
                return cached[1]
        model = build_model(run) if run.exists else {
            "title": run.prefix.name, "turns": [], "hierarchy": {}, "fields": {},
            "notes": ["Waiting for the session to write its first lines…"], "run": run.id,
            "files": [], "format": None, "updated": 0, "ended": False}
        with self._lock:
            self._cache[run.id] = (signature, model)
        return model

    def run_list(self) -> list:
        return [{"id": r.id, "name": r.prefix.name, "updated": r.mtime()} for r in self.runs()]


def make_handler(viewer: Viewer, stopping: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body: bytes, kind):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            run_id = (query.get("run") or [None])[0]
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/runs":
                    self._json({"runs": viewer.run_list(), "follow": viewer.follow})
                elif url.path == "/api/model":
                    run = viewer.find(run_id)
                    self._json(viewer.model(run) if run else {"error": "no such log"})
                elif url.path == "/api/sensors":
                    run = viewer.find(run_id)
                    offset = int((query.get("from") or ["0"])[0] or 0)
                    if run is None or run.sensors is None:
                        self._json({"rows": [], "offset": 0})
                    else:
                        rows, offset = read_sensor_rows(run.sensors, offset)
                        self._json({"rows": rows, "offset": offset})
                elif url.path == "/api/stream":
                    self._stream(run_id)
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _stream(self, run_id):
            """Server-sent events: the model now, then again whenever its files change, and the
            run list whenever a new session or episode appears."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_sig, last_runs, last_ping = None, None, time.monotonic()
            sensor_run, sensor_offset = None, 0
            while not stopping.is_set():
                runs = viewer.run_list()
                names = [r["id"] for r in runs]
                if names != last_runs:
                    self._event("runs", {"runs": runs, "follow": viewer.follow})
                    last_runs = names
                run = viewer.find(run_id)
                if run is not None:
                    sig = (run.id, run.signature())
                    if sig != last_sig:
                        self._event("model", viewer.model(run))
                        last_sig = sig
                    # The sensor readings: everything so far on the first pass, then just
                    # what's new, several times a second.
                    if run.id != sensor_run:
                        sensor_run, sensor_offset = run.id, 0
                        self._event("sensors", {"run": run.id, "reset": True, "rows": []})
                    if run.sensors is not None:
                        rows, sensor_offset = read_sensor_rows(run.sensors, sensor_offset)
                        if rows:
                            self._event("sensors", {"run": run.id, "rows": rows})
                if time.monotonic() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()
                stopping.wait(POLL_S)

        def _event(self, name, obj):
            data = json.dumps(obj, ensure_ascii=False)
            self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())
            self.wfile.flush()

    return Handler


def start(target, *, port: int = DEFAULT_PORT, follow: bool = False, open_browser=False):
    """Serve `target` (a log file, a run prefix, or a directory of runs) on 127.0.0.1 in a
    background thread, trying the next ports if `port` is taken. Returns (url, stop)."""
    viewer = Viewer(Path(target), follow=follow)
    stopping = threading.Event()
    server, last_error = None, None
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate),
                                         make_handler(viewer, stopping))
            break
        except OSError as e:
            last_error = e
    if server is None:
        raise OSError(f"no free port from {port} to {port + 9}: {last_error}")
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="log-viewer", daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    if open_browser:
        webbrowser.open(url)

    def stop():
        stopping.set()
        server.shutdown()

    return url, stop


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="See a session's agents at work on a local page: the team, the user's "
                    "turns, who told whom what, and each agent's reasoning.")
    ap.add_argument("log", nargs="?", help="a _story.txt / _trace.jsonl (or any file of the run), "
                                          "a run prefix, or a directory such as data/eval_v2_...")
    ap.add_argument("--latest", action="store_true", help="the newest log anywhere in data/")
    ap.add_argument("--follow", action="store_true",
                    help="with a directory: always show the newest session or episode")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"(default {DEFAULT_PORT})")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args(argv)

    if args.latest:
        newest = newest_log()
        if newest is None:
            print("no story or trace logs under data/")
            return 2
        target = newest
    elif args.log:
        target = Path(args.log)
        if not target.exists() and not Run(target).exists and not target.is_dir():
            print(f"no log at {target}")
            return 2
    else:
        ap.print_help()
        return 2
    url, stop = start(target, port=args.port, follow=args.follow, open_browser=args.open)
    print(f"log viewer: {url}   ({target})   Ctrl-C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
