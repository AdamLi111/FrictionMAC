"""
Hardware probe: can we read Misty's distance (time-of-flight) and bump sensors?

Run it with the robot env (.venv), the interpreter the robot MCP server uses, so it exercises the
code the agents rely on (robot_tools/sensors.py). Nothing moves; the probe only listens.

  1. Reachability: GET http://<ip>/api/device.
  2. Raw stream (a few seconds): ONE unfiltered TimeOfFlight subscription, listing every distance
     sensor that reports and the SensorId / SensorPosition it reports under. This is the ground
     truth for step 3, whose subscriptions filter each range sensor on its SensorId.
  3. SensorMonitor, the class the agents use (20 s by default): the four distances live, plus
     every bumper press and release. Press each bumper during this window, and hold one down for
     a few seconds to see whether Misty reports the release. Ctrl-C ends the window early.

Every message received is saved to data/sensor_probe_<time>.jsonl (or --log).
Close hw_console / the agents first: step 3 subscribes under the same event names they use.

    .venv/bin/python -m scripts.sensor_probe                  # robot at the default 172.20.10.2
    .venv/bin/python -m scripts.sensor_probe --seconds 40     # more time to press the bumpers
    .venv/bin/python -m scripts.sensor_probe --no-bump        # distance sensors only
    MISTY_IP=192.168.1.50 .venv/bin/python -m scripts.sensor_probe
    Windows: .venv\\Scripts\\python -m scripts.sensor_probe

Exit code: 0 = all four range sensors reached SensorMonitor, and so did a bumper press (unless
--no-bump); 1 = something is missing, and the summary says what; 2 = couldn't connect.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from agent_runtime.config import DEFAULT_MISTY_IP
from robot_tools import config as robot_config
from robot_tools import sensors

RAW_EVENT = "sensor_probe_tof_unfiltered"
_ABBR = {"front_left": "FL", "front_center": "FC", "front_right": "FR", "back": "BK"}
_f = sensors._field          # case-insensitive field lookup, the one SensorMonitor uses


# ---------------------------------------------------------------------------- message helpers
def _parse(raw):
    """One pub/sub message: parsed JSON if it is JSON, else the text."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _payload(msg):
    return _f(msg, "message") if isinstance(msg, dict) else None


def range_sensor(payload):
    """The name SensorMonitor files a ToF reading under (the same lookup as SensorMonitor.feed),
    or None for one it drops: a downward edge sensor, or an id/position it doesn't know."""
    return (sensors._TOF_IDS.get(str(_f(payload, "sensorId") or "").lower())
            or sensors._TOF_POSITIONS.get(str(_f(payload, "sensorPosition") or "").lower()))


def bumper(payload):
    """The name SensorMonitor files a bump event under, or None if it doesn't recognise it."""
    return (sensors._BUMP_IDS.get(str(_f(payload, "sensorId") or "").lower())
            or sensors._BUMP_NAMES.get(str(_f(payload, "sensorName") or "").lower()))


def _valid(status) -> bool:
    """SensorMonitor's rule: status 0 (or no status at all) is a distance it trusts."""
    try:
        return status is None or int(status) == 0
    except (TypeError, ValueError):
        return False


def _describe(msg) -> str:
    if isinstance(msg, dict) and isinstance(_payload(msg), str):
        return f"{_f(msg, 'eventName')}: {_payload(msg)}"
    return json.dumps(msg, default=str)[:200]


class Tally:
    """One distance sensor's messages: how many, their status codes, the valid distances."""

    def __init__(self):
        self.n = 0
        self.n_valid = 0
        self.statuses = Counter()
        self.last = self.lo = self.hi = None

    def add(self, payload):
        self.n += 1
        status = _f(payload, "status")
        self.statuses[status] += 1
        try:
            d = float(_f(payload, "distanceInMeters"))
        except (TypeError, ValueError):
            return
        if _valid(status):
            self.n_valid += 1
            self.last = d
            self.lo = d if self.lo is None else min(self.lo, d)
            self.hi = d if self.hi is None else max(self.hi, d)

    def status_text(self) -> str:
        return " ".join(f"{s}:{n}" for s, n in self.statuses.most_common())


class Log:
    """Every message, one JSON line each: {"phase", "t" (s into the phase), "msg"}. Step 3 writes
    from the monitor's WebSocket thread, hence the lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, phase, t, msg):
        line = json.dumps({"phase": phase, "t": round(t, 3), "msg": msg}, default=str)
        with self._lock:
            if not self._fh.closed:
                self._fh.write(line + "\n")

    def close(self):
        with self._lock:
            self._fh.close()


# ---------------------------------------------------------------------------- step 1
def check_reachable(ip, timeout=4.0):
    """None if Misty's HTTP API answered, else why not."""
    try:
        with urllib.request.urlopen(f"http://{ip}/api/device", timeout=timeout) as resp:
            resp.read()
        return None
    except Exception as e:
        return f"{e.__class__.__name__}: {e}"


# ---------------------------------------------------------------------------- step 2
def raw_scan(ip, seconds, log):
    """One unfiltered TimeOfFlight subscription for `seconds`. Returns
    ({(sensorId, sensorPosition, type): Tally}, [messages that weren't distance readings])."""
    import websocket            # websocket-client: a robot-env dependency
    ws = websocket.create_connection(f"ws://{ip}/pubsub", timeout=5)
    seen, other = {}, []
    t0 = time.monotonic()
    try:
        ws.send(json.dumps({"Operation": "subscribe", "Type": "TimeOfFlight", "DebounceMs": 0,
                            "EventName": RAW_EVENT, "Message": ""}))
        ws.settimeout(0.25)
        while time.monotonic() - t0 < seconds:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            msg = _parse(raw)
            log.write("raw", time.monotonic() - t0, msg)
            payload = _payload(msg)
            if isinstance(payload, dict) and _f(payload, "distanceInMeters") is not None:
                key = tuple(str(_f(payload, k)) for k in ("sensorId", "sensorPosition", "type"))
                seen.setdefault(key, Tally()).add(payload)
            else:
                other.append(msg)
    finally:
        try:
            ws.send(json.dumps({"Operation": "unsubscribe", "EventName": RAW_EVENT, "Message": ""}))
        except Exception:
            pass
        ws.close()
    return seen, other


def print_raw(seen, seconds):
    if not seen:
        print("  No distance data at all.")
        return
    print(f"  {'sensorId':<9} {'position':<15} {'type':<6} {'msgs':>5} {'per s':>6} {'valid':>6} "
          f"{'last valid':>10}  {'sensors.py name':<16} statuses")

    def order(item):             # the four range sensors first, in sensors.py's order
        name = range_sensor({"sensorId": item[0][0], "sensorPosition": item[0][1]})
        return (sensors.RANGE_SENSORS.index(name) if name else len(sensors.RANGE_SENSORS), item[0])

    for (sid, pos, typ), t in sorted(seen.items(), key=order):
        name = range_sensor({"sensorId": sid, "sensorPosition": pos}) or "(ignored)"
        last = f"{t.last:.3f} m" if t.last is not None else "-"
        print(f"  {sid:<9} {pos:<15} {typ:<6} {t.n:>5} {t.n / seconds:>6.1f} "
              f"{t.n_valid / t.n:>6.0%} {last:>10}  {name:<16} {t.status_text()}")


# ---------------------------------------------------------------------------- step 3
class ProbeMonitor(sensors.SensorMonitor):
    """The agents' SensorMonitor, unchanged except that feed() also logs and tallies every
    message and queues bump events for the main thread to print. Same connection, same
    subscriptions, same parsing."""

    def __init__(self, ip, log):
        super().__init__(ip)
        self.t0 = time.monotonic()
        self._log = log
        self._tally_lock = threading.Lock()
        self.ranges = {s: Tally() for s in sensors.RANGE_SENSORS}
        self.dropped_tof = Counter()     # (sensorId, position) SensorMonitor ignored
        self.other = []                  # acks, errors: anything that isn't sensor data
        self.bumps = queue.Queue()       # (t, bumper name or None, contacted, payload)

    def feed(self, raw):
        t = time.monotonic() - self.t0
        msg = _parse(raw)
        self._log.write("monitor", t, msg)
        super().feed(raw)                # the agents' view is updated before we report it
        payload = _payload(msg)
        with self._tally_lock:
            if not isinstance(payload, dict):
                self.other.append(msg)
            elif _f(payload, "distanceInMeters") is not None:
                name = range_sensor(payload)
                if name:
                    self.ranges[name].add(payload)
                else:
                    self.dropped_tof[(str(_f(payload, "sensorId")),
                                      str(_f(payload, "sensorPosition")))] += 1
            elif _f(payload, "isContacted") is not None:
                self.bumps.put((t, bumper(payload), bool(_f(payload, "isContacted")), payload))
            else:
                self.other.append(msg)


def _fmt(v) -> str:
    if v == sensors.UNKNOWN:
        return "?".rjust(5)
    if v == sensors.NO_READING:
        return "none".rjust(5)
    return f"{v:5.3f}"


def _drain_bumps(mon, obs):
    while True:
        try:
            t, name, contacted, payload = mon.bumps.get_nowait()
        except queue.Empty:
            return
        if name is None:
            obs["unknown_bumps"].append(payload)
            print(f"  {t:5.1f}s  BUMP  unrecognised sensor, SensorMonitor ignores it: {payload}")
        elif contacted:
            obs["presses"][name] += 1
            obs["down_at"][name] = t
            print(f"  {t:5.1f}s  BUMP  {name} pressed")
        else:
            obs["releases"][name] += 1
            down = obs["down_at"].pop(name, None)
            if down is not None:
                obs["held"][name].append(t - down)
            print(f"  {t:5.1f}s  BUMP  {name} released"
                  + ("" if down is None else f" (held {t - down:.1f} s)"))


def monitor_phase(ip, seconds, log, want_bump):
    """Run SensorMonitor for `seconds`, printing what the agents would see. Returns (monitor,
    observations), or None if it never connected."""
    mon = ProbeMonitor(ip, log).start()
    deadline = time.monotonic() + 6
    while not mon.connected and time.monotonic() < deadline:
        time.sleep(0.05)
    if not mon.connected:
        mon.close()
        return None
    if want_bump:
        print("  >>> Press each bumper now: front-left, front-right, rear-left, rear-right.")
        print("  >>> Hold one down for ~3 s before letting go, to see if the release is reported.")
    print(f"  Metres. 'none' = the sensor trusts no reading (usually nothing within "
          f"{sensors.TOF_MAX_RANGE_M:g} m); '?' = no data in the last {sensors.MAX_AGE_S:g} s.")
    obs = {"seen": Counter(), "after_first": Counter(), "stale": Counter(),
           "presses": Counter(), "releases": Counter(), "held": {b: [] for b in sensors.BUMPERS},
           "down_at": {}, "unknown_bumps": [], "stopped_early": False}
    started = time.monotonic()
    next_line = 0.0
    try:
        while True:
            elapsed = time.monotonic() - started
            _drain_bumps(mon, obs)
            d = mon.distances()
            for s in sensors.RANGE_SENSORS:
                if d[s] != sensors.UNKNOWN:
                    obs["seen"][s] += 1
                if obs["seen"][s]:
                    obs["after_first"][s] += 1
                    obs["stale"][s] += d[s] == sensors.UNKNOWN   # had data, then went quiet
            if elapsed >= next_line:
                print(f"  {time.monotonic() - mon.t0:5.1f}s  "
                      + "  ".join(f"{_ABBR[s]} {_fmt(d[s])}" for s in sensors.RANGE_SENSORS)
                      + f"   pressed: {', '.join(mon.pressed()) or '-'}")
                next_line += 1.0
            if elapsed >= seconds:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        obs["stopped_early"] = True
        print("  (stopped early)")
    obs["window"] = max(time.monotonic() - started, 1e-6)
    _drain_bumps(mon, obs)
    obs["contacts"] = Counter(c["bumper"] for c in mon.contacts_since(mon.t0))
    mon.close()
    return mon, obs


# ---------------------------------------------------------------------------- verdict
def _diagnose(sensor, seen) -> list:
    """Why `sensor` never reached SensorMonitor, judged against the unfiltered stream."""
    want = next((sid for sid, name in sensors._TOF_IDS.items() if name == sensor), None)
    if not seen:
        return ["The unfiltered stream (step 2) had no distance data either: Misty isn't "
                "publishing time-of-flight readings at all."]
    hits = [(sid, pos) for sid, pos, _ in seen
            if range_sensor({"sensorId": sid, "sensorPosition": pos}) == sensor]
    if not hits:
        lines = [f"Not in the unfiltered stream either (no SensorId '{want}', and no position "
                 f"that maps to {sensor})."]
        unknown = sorted({f"{sid}/{pos}" for sid, pos, typ in seen if typ.lower() != "edge"
                          and range_sensor({"sensorId": sid, "sensorPosition": pos}) is None})
        if unknown:
            lines.append(f"Range sensors sensors.py doesn't recognise: {', '.join(unknown)}.")
        return lines
    ids = sorted({sid for sid, _ in hits})
    if want in ids:
        return [f"Misty streams it unfiltered as SensorId '{want}', but the filtered subscription "
                f"(EventConditions SensorId = '{want}') delivered nothing: the filter is what fails.",
                "Likely fix: filter on SensorPosition in sensors.subscription_messages(), as the "
                "vendored mistyPy EventFilters do."]
    return [f"Misty reports it as SensorId {', '.join(repr(i) for i in ids)} (position "
            f"{', '.join(repr(p) for _, p in hits)}), but sensors.py subscribes with SensorId "
            f"'{want}'. Fix: update _TOF_IDS in robot_tools/sensors.py."]


def summarize(seen, raw_other, mon, obs, want_bump) -> bool:
    ok = True
    window = obs["window"]
    print("\n=== Result ===")
    print("Distance sensors, as the agents see them (SensorMonitor.distances()):")
    shown = set()                # a hint shared by several sensors is printed once
    for s in sensors.RANGE_SENSORS:
        t = mon.ranges[s]
        if not obs["seen"][s]:
            ok = False
            print(f"  {s:<13} FAIL  no readings")
            for line in _diagnose(s, seen):
                if line not in shown:
                    shown.add(line)
                    print(f"{'':21}{line}")
            continue
        line = f"  {s:<13} PASS  {t.n} readings ({t.n / window:.1f}/s)"
        if t.n_valid:
            line += (f", {t.n_valid / t.n:.0%} valid, last {t.last:.3f} m "
                     f"(min {t.lo:.3f}, max {t.hi:.3f})")
        else:
            line += (f", none valid (status {t.status_text()}): nothing within "
                     f"{sensors.TOF_MAX_RANGE_M:g} m? Put a box ~0.5 m in front of it")
        if obs["stale"][s]:
            line += (f"; went stale (> {sensors.MAX_AGE_S:g} s without data) in "
                     f"{obs['stale'][s]} of {obs['after_first'][s]} checks")
        print(line)
    if mon.dropped_tof:
        print("  Also received, and ignored by SensorMonitor: "
              + ", ".join(f"{sid}/{pos} x{n}" for (sid, pos), n in mon.dropped_tof.items()))

    if want_bump:
        print("Bump sensors, as the agents see them (SensorMonitor contacts):")
        for b in sensors.BUMPERS:
            presses, releases = obs["presses"][b], obs["releases"][b]
            if not presses and not releases:
                print(f"  {b:<13} not pressed")
                continue
            verdict = "PASS" if obs["contacts"][b] else "FAIL"
            ok = ok and verdict == "PASS"
            line = (f"  {b:<13} {verdict}  {presses} press{'es' * (presses != 1)}, "
                    f"{releases} release{'s' * (releases != 1)}")
            if obs["held"][b]:
                line += ", held " + ", ".join(f"{h:.1f} s" for h in obs["held"][b])
            if b in obs["down_at"]:
                line += "; still pressed at the end: no release event arrived"
            if verdict == "FAIL":
                line += "; the event arrived but SensorMonitor registered no contact"
            print(line)
        if not sum(obs["contacts"].values()):
            ok = False
            if obs["unknown_bumps"]:
                print("  Bump events arrived, but none under a sensorId/sensorName sensors.py "
                      "knows (see the BUMP lines above): update _BUMP_IDS / _BUMP_NAMES.")
            else:
                print("  No bump events arrived. If you pressed a bumper, the BumpSensor "
                      "subscription isn't delivering; if not, re-run and press one (--seconds "
                      "40 gives more time, --no-bump skips this check).")

    others = Counter(_describe(m) for m in list(raw_other) + list(mon.other))
    if others:
        print("Other messages from Misty (subscription acks, errors):")
        for text, n in sorted(others.items()):
            print(f"  {text}" + (f"  (x{n})" if n > 1 else ""))
    return ok


# ---------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Check that Misty's distance and bump sensors report. Nothing moves.")
    ap.add_argument("--ip", default=os.environ.get("MISTY_IP") or DEFAULT_MISTY_IP,
                    help="robot address (default: $MISTY_IP, else %(default)s)")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="how long step 3 (SensorMonitor) listens (default: 20)")
    ap.add_argument("--raw-seconds", type=float, default=4.0,
                    help="how long step 2 (unfiltered stream) listens (default: 4)")
    ap.add_argument("--no-bump", action="store_true",
                    help="check the distance sensors only; don't require a bumper press")
    ap.add_argument("--log", type=Path,
                    help="where to save every message (default: data/sensor_probe_<time>.jsonl)")
    args = ap.parse_args(argv)
    want_bump = not args.no_bump
    log_path = args.log or (Path(robot_config.DEFAULT_DATA_DIR)
                            / f"sensor_probe_{datetime.now():%Y%m%d_%H%M%S}.jsonl")

    print(f"[1/3] Is Misty reachable at {args.ip}?")
    why = check_reachable(args.ip)
    if why:
        print(f"  No: {why}\n  Check that Misty is on and on the same Wi-Fi as this computer, "
              f"or pass --ip / set MISTY_IP.")
        return 2
    print("  Yes.")

    log = Log(log_path)
    try:
        print(f"\n[2/3] Unfiltered distance stream: which sensors report, and under which "
              f"SensorId ({args.raw_seconds:g} s)")
        try:
            seen, raw_other = raw_scan(args.ip, args.raw_seconds, log)
        except Exception as e:
            print(f"  Could not read ws://{args.ip}/pubsub: {e.__class__.__name__}: {e}")
            return 2
        print_raw(seen, args.raw_seconds)

        print(f"\n[3/3] SensorMonitor, the class the agents use ({args.seconds:g} s)")
        result = monitor_phase(args.ip, args.seconds, log, want_bump)
        if result is None:
            print(f"  SensorMonitor could not connect to ws://{args.ip}/pubsub.")
            return 2
        mon, obs = result
        ok = summarize(seen, raw_other, mon, obs, want_bump)
    finally:
        log.close()
    print(f"Every message received is in {log_path}")
    if ok:
        print("RESULT: PASS - " + ("distance and bump" if want_bump else "distance")
              + " readings reach SensorMonitor.")
    else:
        print("RESULT: FAIL - see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
