"""
Drive Misty by hand, without the agents, and watch every sensor live.

Type commands like "move forward 1 meter" at the prompt. Each runs through the same robot tools
the agents use (robot_tools/tools.py), so a move stops at a bump, or when something comes closer
than stop_within, exactly as it would for an agent. Meanwhile a page on localhost shows every
sensor live (distance, ledge and bump sensors, and Misty's own hazard events), and the same
stream can be followed from another terminal.

Run it with the robot env (.venv), the one the robot MCP server uses:
    .venv/bin/python -m scripts.drive_console                       # the robot (172.20.10.2 or $MISTY_IP)
    .venv/bin/python -m scripts.drive_console --sim office_kitchen  # the simulator instead
    .venv/bin/python -m scripts.drive_console --open                # also open the page
  then open http://127.0.0.1:8765, or follow it in another terminal:
    curl -N http://127.0.0.1:8765/live.txt     # one line per update
    curl -N http://127.0.0.1:8765/events       # the page's JSON stream (server-sent events)
    curl http://127.0.0.1:8765/state           # one snapshot
  Windows: .venv\\Scripts\\python -m scripts.drive_console

Commands (meters and degrees unless you say cm or mm):
    move forward 1 meter | forward 0.5 | f 50 cm      move backward 0.3 | back 30 cm | b 0.3
    turn left 90 | left 45 deg | l 90                 turn right 30 | right 90 | r 90
    add "stop 0.3" (or "stop within 0") to a move     that move's stop_within; 0 = bumpers only
    stop_within 0.2      the default for later moves (starts at 0.15)
    sensors              print the current readings
    stop                 stop moving; Ctrl-C during a move and the page's STOP button stop it too
    help, quit

Every sensor message is saved to data/drive_console_<time>_sensors.jsonl, every command's result
to data/drive_console_<time>_tools.jsonl.

Close hw_console and the agents first: this connects to Misty's sensors under the same names.
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_runtime.config import DEFAULT_MISTY_IP
from robot_tools import config as robot_config
from robot_tools import runtime, sensors, tools

PAGE = Path(__file__).with_name("drive_console.html")
MAX_MOVE_M = 5.0
_f = sensors._field            # case-insensitive field lookup, as SensorMonitor uses

# Misty's own hazard notifications, for the page; the agents' monitor doesn't subscribe to them.
EXTRA_SUBSCRIPTIONS = [{"Operation": "subscribe", "Type": "HazardNotification", "DebounceMs": 0,
                        "EventName": "console_hazard", "Message": ""}]
_HAZARD_KEYS = {"bumpsensorshazardstate", "timeofflightsensorshazardstate", "drivestopped",
                "criticalinternalerror", "excessivespeedhazard", "motorstallhazard",
                "currentsensorshazard"}
_SHORT = {"front_left": "FL", "front_center": "FC", "front_right": "FR", "back": "BK"}


# ---------------------------------------------------------------------------- live state
class Live:
    """Everything the page shows: the latest raw reading of every sensor, the move in progress,
    the last result and recent events. Written by the sensor stream (via the monitor's listener)
    and the command loop; read by the web server's threads."""

    def __init__(self, monitor, mode: str, target: str, sensor_log: Path):
        self.monitor = monitor
        self.mode, self.target = mode, target
        self.stop_within = tools.DEFAULT_STOP_WITHIN_M
        self._lock = threading.Lock()
        self._range = {}          # sensor -> {"status", "t", "times": deque}
        self._edge = {}           # corner -> {"distance", "status", "hazard", "t"}
        self._bumpers = {b: False for b in sensors.BUMPERS}
        self._hazard = None
        self._drive = None
        self._last = None
        self._events = deque(maxlen=200)
        self._seq = 0
        self._subscribers = set()
        sensor_log.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(sensor_log, "a", encoding="utf-8")
        monitor.add_listener(self.on_message)

    # -- stream ---------------------------------------------------------------------------
    def on_message(self, data: dict) -> None:
        """The monitor's listener: every pub/sub message, on the thread that received it."""
        t = self.monitor.now()
        line = json.dumps({"t": round(t, 3), "wall": round(time.time(), 3), "msg": data},
                          default=str)
        payload = _f(data, "message")
        with self._lock:
            if not self._log.closed:
                self._log.write(line + "\n")
            if not isinstance(payload, dict):
                return
            sensor_id = str(_f(payload, "sensorId") or "").lower()
            if _f(payload, "distanceInMeters") is not None:
                if sensor_id in sensors._TOF_IDS:
                    entry = self._range.setdefault(sensors._TOF_IDS[sensor_id],
                                                   {"times": deque(maxlen=8)})
                    entry.update(status=_f(payload, "status"), t=t)
                    entry["times"].append(t)
                elif sensor_id in sensors._EDGE_IDS or str(_f(payload, "type")).lower() == "edge":
                    corner = sensors._EDGE_IDS.get(sensor_id, sensor_id)
                    self._edge[corner] = {"distance": _f(payload, "distanceInMeters"),
                                          "status": _f(payload, "status"),
                                          "hazard": bool(_f(payload, "inHazard")), "t": t}
                return
            contacted = _f(payload, "isContacted")
            if contacted is not None:
                bumper = (sensors._BUMP_IDS.get(sensor_id) or sensors._BUMP_NAMES.get(
                    str(_f(payload, "sensorName") or "").lower()))
                if bumper is not None:
                    self._bumpers[bumper] = bool(contacted)
                    self._event_locked("bump", f"{bumper} bumper "
                                               f"{'pressed' if contacted else 'released'}")
                return
            if any(str(key).lower() in _HAZARD_KEYS for key in payload):
                active = _hazards_in(payload)
                self._hazard = {"active": active, "wall": time.time()}
                self._event_locked("hazard", "Misty hazard: " + (", ".join(active) or "clear"))

    # -- the command loop -----------------------------------------------------------------
    def start_drive(self, command: str, planned_s: float, stop_within=None) -> None:
        with self._lock:
            self._drive = {"command": command, "started": time.monotonic(),
                           "planned_s": round(planned_s, 2), "stop_within": stop_within}
            self._event_locked("command", command)

    def end_drive(self, outcome: str, detail: str) -> None:
        with self._lock:
            command = self._drive["command"] if self._drive else ""
            self._drive = None
            self._last = {"command": command, "outcome": outcome, "detail": detail,
                          "wall": time.time()}
            self._event_locked("result", f"{outcome}: {detail}")

    def event(self, kind: str, text: str) -> None:
        with self._lock:
            self._event_locked(kind, text)

    def _event_locked(self, kind, text) -> None:
        self._seq += 1
        item = {"seq": self._seq, "wall": time.time(), "kind": kind, "text": text}
        self._events.append(item)
        for q in list(self._subscribers):
            q.put(item)

    # -- readers ----------------------------------------------------------------------------
    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subscribers.add(q)
            recent = list(self._events)[-30:]
        return q, recent

    def unsubscribe(self, q) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def snapshot(self) -> dict:
        now = self.monitor.now()
        agents_view = self.monitor.distances()
        with self._lock:
            ranges = {}
            for sensor in sensors.RANGE_SENSORS:
                raw = self._range.get(sensor, {})
                times = raw.get("times") or ()
                hz = ((len(times) - 1) / (times[-1] - times[0])
                      if len(times) > 1 and times[-1] > times[0] else None)
                ranges[sensor] = {
                    "value": agents_view[sensor], "status": raw.get("status"),
                    "age_ms": None if "t" not in raw else round((now - raw["t"]) * 1000),
                    "hz": None if hz is None else round(hz, 1)}
            edges = {corner: {"distance": e["distance"], "status": e["status"],
                              "drop": sensors.ledge_drop(e["distance"], e["status"], e["hazard"]),
                              "hazard": e["hazard"],
                              "age_ms": round((now - e["t"]) * 1000)}
                     for corner, e in self._edge.items()}
            drive = None
            if self._drive is not None:
                drive = dict(self._drive)
                drive["elapsed_s"] = round(time.monotonic() - drive.pop("started"), 2)
            return {"wall": time.time(), "mode": self.mode, "target": self.target,
                    "online": self.monitor.online, "stop_within": self.stop_within,
                    "range": ranges, "edge": edges, "bumpers": dict(self._bumpers),
                    "drive": drive, "last": self._last, "hazard": self._hazard,
                    "ledge_drop_m": sensors.LEDGE_DROP_M}

    def close(self) -> None:
        with self._lock:
            self._log.close()


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hazards_in(payload: dict) -> list:
    """The hazards a HazardNotification says are active, whatever its exact layout: every
    {.., "inHazard": true} entry under a hazard list, named by its sensor or zone, and every
    hazard flag that is simply true."""
    active = []
    for key, value in payload.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _f(item, "inHazard"):
                    name = (_f(item, "sensorName") or _f(item, "zone") or _f(item, "name")
                            or "?")
                    active.append(f"{key}:{name}")
        elif value is True:
            active.append(str(key))
    return active


# ---------------------------------------------------------------------------- formatting
def _fmt(value) -> str:
    if value == sensors.UNKNOWN:
        return "?"
    if value == sensors.NO_READING:
        return "none"
    return f"{value:.2f}"


def _ledge(edge: dict, corner: str) -> str:
    reading = edge.get(corner)
    distance = None if reading is None else _number(reading["distance"])
    if distance is None:
        return "?"
    return f"{distance * 100:.1f}" + ("!" if reading["drop"] else "")


def readings_line(snap: dict) -> str:
    """Range sensors (m), ledge sensors (cm, "!" = a drop) and pressed bumpers, on one line."""
    ranges = "  ".join(f"{_SHORT[s]} {_fmt(snap['range'][s]['value']):>5}"
                       for s in sensors.RANGE_SENSORS)
    edge = snap["edge"]
    ledge = ("ledge " + " ".join(_ledge(edge, c) for c in sensors.LEDGES) + " cm"
             if edge else "ledge -")
    pressed = [b for b, down in snap["bumpers"].items() if down]
    return f"{ranges} | {ledge} | bumpers {', '.join(pressed) or '-'}"


def describe(result: dict):
    """(outcome, one-line detail) for a movement tool's result."""
    if result.get("error"):
        return "error", result["error"]
    if result.get("ok"):
        return "ok", f"done in {result.get('duration_ms', 0) / 1000:.2f} s"
    if "ledge" in result:
        ledge = result["ledge"]
        where = ", ".join(ledge["ledges"])
        if ledge.get("already_detected"):
            return "refused", f"did not move: drop under the {where} ledge sensor"
        return "ledge", (f"stopped after {ledge['after_ms'] / 1000:.2f} s: drop under the "
                         f"{where} ledge sensor"
                         + (f", ~{ledge['estimated_traveled_m']} m traveled"
                            if "estimated_traveled_m" in ledge else ""))
    if "obstacle" in result:
        ob = result["obstacle"]
        if ob.get("already_within"):
            return "refused", (f"did not move: {ob['sensor']} already reads "
                               f"{ob['distance_m']} m (limit {ob['limit_m']} m)")
        return "obstacle", (f"stopped after {ob['after_ms'] / 1000:.2f} s: {ob['sensor']} read "
                            f"{ob['distance_m']} m (limit {ob['limit_m']} m), "
                            f"~{ob.get('estimated_traveled_m', '?')} m traveled")
    if "bump" in result:
        bump = result["bump"]
        if bump.get("already_pressed"):
            return "refused", f"did not move: {', '.join(bump['bumpers'])} bumper pressed"
        return "bump", (f"stopped after {bump['after_ms'] / 1000:.2f} s: "
                        f"{', '.join(bump['bumpers'])} bumper pressed, "
                        f"~{bump.get('estimated_traveled_m', '?')} m traveled")
    return "?", json.dumps(result)


# ---------------------------------------------------------------------------- commands
_NUM = r"(\d+(?:\.\d+)?|\.\d+)"
_MOVE = re.compile(
    r"(?:(?:move|drive|go)\s+)?(?P<dir>forwards?|ahead|fwd|f|backwards?|back|reverse|b)"
    rf"\s+{_NUM}\s*(?P<unit>m|meters?|metres?|cm|centimeters?|centimetres?|mm)?"
    rf"(?:\s+stop(?:[\s_]*within)?\s+(?P<stop>{_NUM[1:-1]})\s*(?P<sunit>m|cm)?)?")
_TURN = re.compile(rf"(?:(?:turn|rotate)\s+)?(?P<dir>left|l|right|r)(?:\s+{_NUM}\s*"
                   r"(?:°|deg|degs|degrees?)?)?")
_SET_STOP = re.compile(rf"stop[\s_]*within\s+(?P<stop>{_NUM[1:-1]})\s*(?P<sunit>m|cm)?")


def _metres(value: str, unit) -> float:
    unit = unit or "m"
    if unit.startswith(("cm", "centi")):
        return float(value) / 100
    if unit == "mm":
        return float(value) / 1000
    return float(value)


def parse(line: str):
    """One typed line as ("move", direction, metres, stop_within|None), ("turn", direction,
    degrees), ("set_stop", metres), ("stop",), ("sensors",), ("help",), ("quit",), (None,) for
    an empty line, or ("error", message)."""
    text = re.sub(r"\s+", " ", line.strip().lower())
    if not text:
        return (None,)
    if text in ("quit", "exit", "q", ":q"):
        return ("quit",)
    if text in ("help", "h", "?"):
        return ("help",)
    if text in ("stop", "s", "halt"):
        return ("stop",)
    if text in ("sensors", "sensor", "read", "readings"):
        return ("sensors",)
    m = _SET_STOP.fullmatch(text)
    if m:
        return ("set_stop", _metres(m.group("stop"), m.group("sunit")))
    m = _MOVE.fullmatch(text)
    if m:
        direction = "forward" if m.group("dir")[0] in "fa" else "backward"
        metres = _metres(m.group(2), m.group("unit"))
        stop = None if m.group("stop") is None else _metres(m.group("stop"), m.group("sunit"))
        if not 0 < metres <= MAX_MOVE_M:
            return ("error", f"move between 0 and {MAX_MOVE_M:g} m at a time")
        return ("move", direction, metres, stop)
    m = _TURN.fullmatch(text)
    if m:
        degrees = float(m.group(2)) if m.group(2) else 90.0
        if not 0 < degrees <= 360:
            return ("error", "turn between 0 and 360 degrees")
        return ("turn", "left" if m.group("dir")[0] == "l" else "right", degrees)
    return ("error", f"don't understand {line.strip()!r}; type help")


HELP = """commands (meters and degrees unless you say cm or mm):
  move forward 1 meter | forward 0.5 | f 50 cm      move backward 0.3 | back 30 cm | b 0.3
  turn left 90 | left 45 deg | l 90                 turn right 30 | right 90 | r 90
  ... stop 0.3 / stop within 0   at the end of a move: that move's stop_within (0 = bumpers only)
  stop_within 0.2      default for later moves        sensors   print the readings
  stop                 stop moving (Ctrl-C during a move, or the page's STOP, too)   quit"""


def run_move(live: Live, direction: str, metres: float, stop_within) -> None:
    stop_within = live.stop_within if stop_within is None else stop_within
    planned_s = runtime.calc_drive_time(metres) / 1000
    command = f"move {direction} {metres:g} m (stop_within {stop_within:g} m)"
    print(f"  {command}, about {planned_s:.1f} s ... Ctrl-C stops it")
    live.start_drive(command, planned_s, stop_within)
    move = tools.move_forward if direction == "forward" else tools.move_backward
    _run(live, lambda: move(metres, stop_within=stop_within))


def run_turn(live: Live, direction: str, degrees: float) -> None:
    planned_s = runtime.calc_turn_time(degrees) / 1000
    command = f"turn {direction} {degrees:g}°"
    print(f"  {command}, about {planned_s:.1f} s ... Ctrl-C stops it")
    live.start_drive(command, planned_s)
    turn = tools.turn_left if direction == "left" else tools.turn_right
    _run(live, lambda: turn(degrees))


def _run(live: Live, call) -> None:
    try:
        result = call()
    except KeyboardInterrupt:
        tools.stop()
        live.end_drive("stopped", "stopped with Ctrl-C")
        print("  stopped (Ctrl-C)")
        return
    outcome, detail = describe(result)
    live.end_drive(outcome, detail)
    print(f"  {outcome.upper()}: {detail}")
    print(f"  now: {readings_line(live.snapshot())}")


# ---------------------------------------------------------------------------- web server
def make_handler(live: Live, stopping: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # keep the console quiet
            pass

        def _send(self, code, body: bytes, kind: str):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _start_stream(self, kind: str):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/index.html"):
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/state":
                    self._send(200, json.dumps(live.snapshot()).encode(), "application/json")
                elif path == "/events":
                    self._events()
                elif path == "/live.txt":
                    self._text()
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path.split("?")[0] != "/stop":
                self._send(404, b"not found", "text/plain")
                return
            tools.stop()
            live.event("stop", "STOP pressed on the page")
            self._send(200, b'{"ok": true}', "application/json")

        def _events(self):
            """Server-sent events: the whole state 10 times a second, and each event as it
            happens (the last 30 first, so a new page has some history)."""
            self._start_stream("text/event-stream")
            q, recent = live.subscribe()
            try:
                for item in recent:
                    self._sse("log", item)
                next_state = 0.0
                while not stopping.is_set():
                    if time.monotonic() >= next_state:
                        self._sse("state", live.snapshot())
                        next_state = time.monotonic() + 0.1
                    try:
                        self._sse("log", q.get(timeout=max(0.0, next_state - time.monotonic())))
                    except queue.Empty:
                        pass
            finally:
                live.unsubscribe(q)

        def _sse(self, name, obj):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        def _text(self):
            """One readings line four times a second, and each event as it happens."""
            self._start_stream("text/plain; charset=utf-8")
            q, _ = live.subscribe()
            try:
                next_line = 0.0
                while not stopping.is_set():
                    if time.monotonic() >= next_line:
                        snap = live.snapshot()
                        drive = snap["drive"]
                        tail = (f" | {drive['command']} {drive['elapsed_s']:.1f}/"
                                f"{drive['planned_s']:.1f} s" if drive else "")
                        self._line(f"{readings_line(snap)}{tail}")
                        next_line = time.monotonic() + 0.25
                    try:
                        item = q.get(timeout=max(0.0, next_line - time.monotonic()))
                        self._line(f"** {item['kind'].upper()}: {item['text']}")
                    except queue.Empty:
                        pass
            finally:
                live.unsubscribe(q)

        def _line(self, text):
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-5]
            self.wfile.write(f"{stamp}  {text}\n".encode())
            self.wfile.flush()

    return Handler


# ---------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Drive Misty by typed commands, without the agents, and watch its sensors "
                    "live on a local page.")
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--ip", help="robot address (default: $MISTY_IP, else "
                                    f"{DEFAULT_MISTY_IP})")
    where.add_argument("--sim", metavar="SCENE", help="drive the simulator in this scene")
    where.add_argument("--stub", action="store_true", help="no robot at all (dry run)")
    ap.add_argument("--port", type=int, default=8765, help="port for the live page (8765)")
    ap.add_argument("--open", action="store_true", help="open the live page in a browser")
    args = ap.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    data_dir = Path(robot_config.DEFAULT_DATA_DIR)
    tool_log = data_dir / f"drive_console_{stamp}_tools.jsonl"
    sensor_log = data_dir / f"drive_console_{stamp}_sensors.jsonl"
    os.environ["TOOL_LOG_PATH"] = str(tool_log)
    os.environ["WORLD_STATE_PATH"] = str(data_dir / f"drive_console_{stamp}_world.json")
    if args.sim:
        os.environ["ROBOT_SIM_SCENE"] = args.sim
        os.environ.pop("ROBOT_STUB", None)
        mode, target = "sim", args.sim
    elif args.stub:
        os.environ["ROBOT_STUB"] = "1"
        os.environ.pop("MISTY_IP", None)
        mode, target = "stub", "no robot"
    else:
        target = args.ip or os.environ.get("MISTY_IP") or DEFAULT_MISTY_IP
        os.environ["MISTY_IP"] = target
        os.environ.pop("ROBOT_STUB", None)
        mode = "real"
    runtime.reset()

    try:
        runtime.get_robot()                 # real mode: fails here if Misty is unreachable
    except RuntimeError as e:
        print(f"{e}\nCheck that Misty is on and on the same Wi-Fi, or pass --ip / --sim.")
        return 2
    monitor = runtime.get_sensors()
    live = Live(monitor, mode, target, sensor_log)
    monitor.add_subscriptions(EXTRA_SUBSCRIPTIONS)
    if mode == "sim":
        monitor.idle(0.4)       # simulated time only moves when asked: one round of readings

    stopping = threading.Event()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(live, stopping))
    except OSError as e:
        print(f"can't serve the live page on port {args.port} ({e}); try --port")
        return 2
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="live-page", daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"

    print(f"Misty drive console: {mode} ({target})")
    print(f"  live page   {url}")
    print(f"  terminal    curl -N {url}/live.txt")
    print(f"  logs        {sensor_log}\n              {tool_log}")
    deadline = time.monotonic() + 3.0
    while mode == "real" and not monitor.online and time.monotonic() < deadline:
        time.sleep(0.1)
    print("  sensors     " + ("streaming" if monitor.online else
                              "no data yet: moves run open-loop until it arrives"))
    print(f"  stop_within {live.stop_within:g} m by default. Type help for commands.\n")
    if args.open:
        import webbrowser
        webbrowser.open(url)

    try:
        while True:
            try:
                line = input("misty> ")
            except KeyboardInterrupt:
                tools.stop()
                print("\n  (stopped; type quit to leave)")
                continue
            except EOFError:
                print()
                break
            cmd = parse(line)
            kind = cmd[0]
            if kind is None:
                continue
            if kind == "quit":
                break
            if kind == "help":
                print(HELP)
            elif kind == "error":
                print(f"  {cmd[1]}")
            elif kind == "stop":
                tools.stop()
                live.event("stop", "stop typed at the console")
                print("  stopped")
            elif kind == "sensors":
                snap = live.snapshot()
                print(f"  {readings_line(snap)}")
                if snap["hazard"]:
                    print(f"  Misty hazard: {', '.join(snap['hazard']['active']) or 'clear'}")
                if not snap["online"]:
                    print("  (no live sensor data)")
            elif kind == "set_stop":
                live.stop_within = max(0.0, cmd[1])
                live.event("info", f"stop_within set to {live.stop_within:g} m")
                print(f"  stop_within is now {live.stop_within:g} m"
                      + (" (bumpers only)" if live.stop_within == 0 else ""))
            elif kind == "move":
                run_move(live, cmd[1], cmd[2], cmd[3])
            elif kind == "turn":
                run_turn(live, cmd[1], cmd[2])
    finally:
        stopping.set()
        try:
            tools.stop()
        except Exception:
            pass
        server.shutdown()
        runtime.reset()                     # unsubscribes and closes the sensor stream
        live.close()
        print(f"bye. Sensor log: {sensor_log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
