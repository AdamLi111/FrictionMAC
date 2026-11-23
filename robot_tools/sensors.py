"""
Misty II's proximity sensors, read live: the four outward-facing time-of-flight (ToF) range
sensors, the four downward-facing ledge sensors, and the four bump sensors.

What the hardware gives (Misty's docs):
  - Range ToF: three on the front of the base (left, centre, right) and one on the back. Each
    reports ONE distance, in metres, to the nearest surface inside its narrow cone, plus a status
    code (0 = valid, 1xx = low confidence, 2xx = error). A reading has no angle: its direction is
    simply which sensor produced it. 10-1200 mm at 1 mm resolution, less accurate beyond 1.2 m.
    Misty publishes neither the cone's width nor the angle of the two outer front sensors.
  - Four more ToF sensors point down at the base's corners to detect ledges (10-150 mm). On a
    floor they read 2-4 cm, about seven times a second. Misty's own hazard system counts a
    reading over 0.06 m as a drop and flags it (`inHazard`); here a corner sees a drop when Misty
    flags it, when it reads over LEDGE_DROP_M, or when the sensor trusts no reading at all (no
    floor within its range is exactly what a stairwell or table edge looks like).
  - Bump: ten contact switches, reported as four sensors (front-left, front-right, rear-left,
    rear-right), with one event on contact and another on release.
  - Misty's hazard system stops a forward drive when a front bumper is pressed (or a ledge is
    seen) and ignores forward drive commands until the bumper is released. It does NOT stop for
    anything the range sensors see. Without reading the ledge sensors, a move Misty halted at a
    ledge would look like one that simply finished.

None of this is available over REST, only over the WebSocket pub/sub at ws://<ip>/pubsub, so
SensorMonitor keeps one connection open on a daemon thread. Each range and ledge sensor gets its
own subscription, because a single ToF stream favours whichever sensor updated last. The vendored
mistyPy Event class isn't used: its callbacks have the pre-1.0 websocket-client signature, which
websocket-client 1.x (the robot env's) calls with an extra argument, so its subscription is never
even sent. The callbacks here accept either convention.

Times are the monitor's own monotonic clock at the moment a message ARRIVED (a few tens of ms
after the event), so they compare directly with the time a drive was commanded. A bump also
keeps the robot's own `created` timestamp, for the record.

A drive watches this stream to know when to stop (`wait_for_stop`): at the first bumper contact,
at the first drop under a ledge sensor on the end it drives toward, or at the first trusted range
reading closer than that sensor's limit (the agent's `stop_within`, smaller for the two angled
front sensors). That is the whole monitor: the agent states the condition, and this code checks
every reading against it as it arrives.

The simulation feeds the same class. Its robot (sim/robot.py) publishes Misty-format messages
into `feed`, and is passed in as `source`: then the monitor's clock is the simulation's, and
waiting on the monitor is what moves simulated time forward (`source.pump`), so the tools that
drive the real robot drive the simulated one unchanged.
"""
import json
import math
import sys
import threading
import time

#: Agent-facing sensor names, in a fixed order.
RANGE_SENSORS = ("front_left", "front_center", "front_right", "back")
BUMPERS = ("front_left", "front_right", "rear_left", "rear_right")
FRONT_BUMPERS = ("front_left", "front_right")
REAR_BUMPERS = ("rear_left", "rear_right")
LEDGES = ("front_left", "front_right", "rear_left", "rear_right")    # the base's four corners
FRONT_LEDGES = ("front_left", "front_right")
REAR_LEDGES = ("rear_left", "rear_right")

TOF_MAX_RANGE_M = 1.2          # Misty: 10-1200 mm, less accurate beyond
NO_READING = "no reading"      # the sensor answered but measured nothing reliable
UNKNOWN = "unknown"            # no recent data from that sensor at all
OFFLINE = "offline: no distance, ledge or bump data"
MAX_AGE_S = 1.0                # a reading older than this counts as UNKNOWN
LEDGE_DROP_M = 0.06            # Misty's default: a ledge sensor reading farther is a drop
FLOOR = "floor"
DROP = "drop"

_TOF_IDS = {"toffl": "front_left", "toffc": "front_center", "toffr": "front_right",
            "tofr": "back"}
_TOF_POSITIONS = {"left": "front_left", "center": "front_center", "right": "front_right",
                  "back": "back"}
_EDGE_IDS = {"tofdfl": "front_left", "tofdfr": "front_right", "tofdrl": "rear_left",
             "tofdrr": "rear_right"}
_EDGE_POSITIONS = {"downfrontleft": "front_left", "downfrontright": "front_right",
                   "downbackleft": "rear_left", "downbackright": "rear_right"}
_BUMP_IDS = {"bfl": "front_left", "bfr": "front_right", "brl": "rear_left", "brr": "rear_right"}
_BUMP_NAMES = {"bump_frontleft": "front_left", "bump_frontright": "front_right",
               "bump_rearleft": "rear_left", "bump_rearright": "rear_right"}

_TOF_DEBOUNCE_MS = 100         # at most ten a second; the range sensors give about three
_EDGE_DEBOUNCE_MS = 100        # the ledge sensors give about seven
_BUMP_DEBOUNCE_MS = 0          # every contact and every release
_RECONNECT_S = 2.0
_SILENT_S = 2.0                # connected, but no data for this long: not actually streaming
_KEEP_CONTACTS = 50
_TIME_EPS = 1e-9            # a wait this close to its deadline is over


def subscription_messages() -> list:
    """Misty pub/sub subscriptions: one per range sensor and one per ledge sensor, each filtered
    on its SensorId, and one for all four bump sensors."""
    subs = [{"Operation": "subscribe", "Type": "TimeOfFlight", "DebounceMs": debounce,
             "EventName": f"ponder_{kind}_{sensor_id}", "Message": "",
             "EventConditions": [{"Property": "SensorId", "Inequality": "=", "Value": sensor_id}]}
            for kind, ids, debounce in (("tof", _TOF_IDS, _TOF_DEBOUNCE_MS),
                                        ("edge", _EDGE_IDS, _EDGE_DEBOUNCE_MS))
            for sensor_id in ids]
    subs.append({"Operation": "subscribe", "Type": "BumpSensor", "DebounceMs": _BUMP_DEBOUNCE_MS,
                 "EventName": "ponder_bump", "Message": ""})
    return subs


def _field(payload: dict, name: str):
    """A field of an event payload in whatever case it arrives (Misty's messages are camelCase;
    its event filters name the same fields in PascalCase)."""
    want = name.lower()
    for key, value in payload.items():
        if key.lower() == want:
            return value
    return None


def _trusted(status) -> bool:
    """A range reading the sensor itself trusts: status 0, or no status field at all. Anything
    else (1xx low confidence, 2xx error) means it measured nothing reliable."""
    try:
        return status is None or int(status) == 0
    except (TypeError, ValueError):
        return False


def ledge_drop(distance, status, in_hazard=False) -> bool:
    """Whether a ledge sensor's reading means there is no floor under that corner: Misty's own
    hazard flag, a reading the sensor doesn't trust (nothing within its 10-150 mm range), or a
    trusted reading farther than LEDGE_DROP_M."""
    if in_hazard or not _trusted(status):
        return True
    try:
        return float(distance) > LEDGE_DROP_M
    except (TypeError, ValueError):
        return True


class SensorMonitor:
    """The latest range readings and bump state, fed by Misty's event stream (or by `feed` in
    tests). Thread-safe: the WebSocket thread writes, tool threads read and wait.

    `source` is the simulated robot when the simulation stands in for Misty: it supplies the
    clock and the messages, and waiting on the monitor advances it (see the module docstring)."""

    def __init__(self, ip=None, clock=time.monotonic, source=None):
        self.ip = ip
        self._clock = clock
        self._source = source
        # Reentrant, because in simulation a wait (which holds the lock) runs the simulation,
        # and the simulation feeds its messages in on the same thread.
        self._cond = threading.Condition(threading.RLock())
        self._ranges = {}             # sensor -> (distance, status, arrived)
        self._edges = {}              # corner -> (distance, status, drop, arrived)
        self._drops = []              # [{"ledge", "t"}]: each time a corner starts seeing a drop
        self._pressed = {}            # bumper -> currently pressed
        self._contacts = []           # [{"bumper", "t", "time"}], most recent last
        self._last_data = None
        self._connected = source is not None    # the simulation is "subscribed" from the start
        self._stopping = threading.Event()
        self._thread = None
        self._app = None
        self._listeners = []          # called with every message (add_listener)
        self._extra_subs = []         # subscriptions beyond range + bump (add_subscriptions)

    def now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------ watching the raw stream
    def add_listener(self, fn) -> None:
        """Call `fn(message)` with every pub/sub message received (parsed JSON), on the thread
        that received it, before the monitor files it: for watching the raw stream, e.g. the
        drive console's live page. Keep it quick; a listener that raises is ignored."""
        self._listeners.append(fn)

    def add_subscriptions(self, messages) -> None:
        """Subscribe to more of Misty's events on this connection, now and after every
        reconnect, for listeners to see (the monitor itself files only range and bump
        readings). Each message is a pub/sub subscribe, as in `subscription_messages`."""
        messages = list(messages)
        self._extra_subs.extend(messages)
        app = self._app if self._source is None else None
        if app is not None:
            for sub in messages:
                try:
                    app.send(json.dumps(sub))
                except Exception:
                    pass                  # not open yet: _on_open sends it with the rest

    def _all_subscriptions(self) -> list:
        return subscription_messages() + list(self._extra_subs)

    # ------------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def online(self) -> bool:
        """Connected AND actually receiving data (a range sensor reports several times a second,
        so a silent connection means the subscriptions aren't working)."""
        last = self._last_data
        return self._connected and last is not None and self._clock() - last <= _SILENT_S

    def start(self) -> "SensorMonitor":
        """Connect in the background (no-op without an ip, i.e. stub mode). Never raises: if the
        robot's pub/sub can't be reached the monitor just stays offline and keeps retrying."""
        if not self.ip or (self._thread is not None and self._thread.is_alive()):
            return self
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="misty-sensors", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stopping.set()
        app = self._app
        if app is not None:
            try:
                for sub in self._all_subscriptions():
                    app.send(json.dumps({"Operation": "unsubscribe",
                                         "EventName": sub["EventName"], "Message": ""}))
            except Exception:
                pass
            try:
                app.close()
            except Exception:
                pass
        self._connected = False

    def _run(self) -> None:
        import websocket   # robot-env dependency; imported here so stub and sim never need it
        url = f"ws://{self.ip}/pubsub"
        while not self._stopping.is_set():
            app = websocket.WebSocketApp(url, on_open=self._on_open, on_message=self._on_message,
                                         on_close=self._on_close, on_error=self._on_error)
            self._app = app
            try:
                app.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as e:
                self._on_error(e)
            self._connected = False
            self._stopping.wait(_RECONNECT_S)

    # websocket-client < 1.0 calls a bound method without the app and >= 1.0 with it, so each
    # callback takes *args: the app, when given, comes first and the payload last.
    def _on_open(self, *args) -> None:
        app = args[0] if args else self._app
        for sub in self._all_subscriptions():
            app.send(json.dumps(sub))
        self._connected = True
        print(f"[sensors] subscribed to distance + bump sensors at {self.ip}", file=sys.stderr)

    def _on_message(self, *args) -> None:
        if args:
            self.feed(args[-1])

    def _on_close(self, *args) -> None:
        if self._connected:
            print(f"[sensors] connection to {self.ip} closed", file=sys.stderr)
        self._connected = False

    def _on_error(self, *args) -> None:
        if args:
            print(f"[sensors] {args[-1]}", file=sys.stderr)

    # ------------------------------------------------------------------ ingest
    def feed(self, raw) -> None:
        """Take one pub/sub message (JSON text or an already-parsed dict). Anything that isn't a
        range, ledge or bump reading (a subscription acknowledgement, say) is ignored, and a
        malformed message never raises."""
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        for listener in list(self._listeners):
            try:
                listener(data)
            except Exception:
                pass
        payload = _field(data, "message")
        if not isinstance(payload, dict):
            return                        # e.g. "Registration Status: API event registered."
        now = self._clock()
        if _field(payload, "distanceInMeters") is not None:
            sensor = (_TOF_IDS.get(str(_field(payload, "sensorId") or "").lower())
                      or _TOF_POSITIONS.get(str(_field(payload, "sensorPosition") or "").lower()))
            if sensor is None:
                self._feed_ledge(payload, now)
                return
            with self._cond:
                self._ranges[sensor] = (_field(payload, "distanceInMeters"),
                                        _field(payload, "status"), now)
                self._last_data = now
                self._cond.notify_all()
            return
        contacted = _field(payload, "isContacted")
        bumper = (_BUMP_IDS.get(str(_field(payload, "sensorId") or "").lower())
                  or _BUMP_NAMES.get(str(_field(payload, "sensorName") or "").lower()))
        if contacted is None or bumper is None:
            return
        with self._cond:
            self._pressed[bumper] = bool(contacted)
            if contacted:
                self._contacts.append({"bumper": bumper, "t": now,
                                       "time": _field(payload, "created")})
                del self._contacts[:-_KEEP_CONTACTS]
            self._last_data = now
            self._cond.notify_all()

    def _feed_ledge(self, payload: dict, now: float) -> None:
        corner = (_EDGE_IDS.get(str(_field(payload, "sensorId") or "").lower())
                  or _EDGE_POSITIONS.get(str(_field(payload, "sensorPosition") or "").lower()))
        if corner is None:
            return
        distance, status = _field(payload, "distanceInMeters"), _field(payload, "status")
        drop = ledge_drop(distance, status, bool(_field(payload, "inHazard")))
        with self._cond:
            before = self._edges.get(corner)
            if drop and not (before and before[2]):
                self._drops.append({"ledge": corner, "t": now})
                del self._drops[:-_KEEP_CONTACTS]
            self._edges[corner] = (distance, status, drop, now)
            self._last_data = now
            self._cond.notify_all()

    # ------------------------------------------------------------------ queries
    def ledges(self, max_age: float = MAX_AGE_S) -> dict:
        """{corner: FLOOR | DROP | UNKNOWN} for the four ledge sensors; a reading older than
        `max_age` is UNKNOWN."""
        now = self._clock()
        with self._cond:
            return {c: UNKNOWN if c not in self._edges or now - self._edges[c][3] > max_age
                    else (DROP if self._edges[c][2] else FLOOR) for c in LEDGES}

    def drops(self) -> list:
        """The corners whose ledge sensor sees a drop right now (recent readings only)."""
        return [c for c, state in self.ledges().items() if state == DROP]

    def drops_since(self, since: float) -> list:
        """Every time a corner started seeing a drop at or after `since`, oldest first."""
        with self._cond:
            return [dict(d) for d in self._drops if d["t"] >= since]

    def distances(self, max_age: float = MAX_AGE_S) -> dict:
        """{sensor: metres | NO_READING | UNKNOWN} for the four range sensors. A non-zero status
        means the sensor measured nothing it trusts, which for a range sensor is usually nothing
        within range; a reading older than `max_age` is UNKNOWN. A reading without a status
        field is taken as valid."""
        now = self._clock()
        out = {}
        with self._cond:
            for sensor in RANGE_SENSORS:
                reading = self._ranges.get(sensor)
                if reading is None or now - reading[2] > max_age:
                    out[sensor] = UNKNOWN
                    continue
                distance, status, _ = reading
                try:
                    out[sensor] = round(float(distance), 3) if _trusted(status) else NO_READING
                except (TypeError, ValueError):
                    out[sensor] = NO_READING
        return out

    def pressed(self) -> list:
        """The bumpers pressed right now (contacted, not yet released)."""
        with self._cond:
            return [b for b in BUMPERS if self._pressed.get(b)]

    def contacts_since(self, since: float) -> list:
        """Every bumper contact that arrived at or after `since`, oldest first."""
        with self._cond:
            return [dict(c) for c in self._contacts if c["t"] >= since]

    def stop_reason(self, since: float, limits=None, ledges=()):
        """Why a move that began at `since` should stop now, or None, checking everything that
        arrived at or after `since`:
          - a bumper contact: {"bump": [contacts, oldest first]};
          - a corner in `ledges` starting to see a drop: {"ledge": [{"ledge", "t"}, ...]};
          - a trusted range reading nearer than its sensor's limit in `limits` ({sensor:
            metres}): {"obstacle": {"sensor", "distance_m", "limit_m", "t"}}, the one most
            inside its limit.
        A reading the sensor doesn't trust, or one from before the move, never stops it."""
        with self._cond:
            hits = [dict(c) for c in self._contacts if c["t"] >= since]
            if hits:
                return {"bump": hits}
            drops = [dict(d) for d in self._drops if d["t"] >= since and d["ledge"] in ledges]
            if drops:
                return {"ledge": drops}
            close = []
            for sensor, limit in (limits or {}).items():
                reading = self._ranges.get(sensor)
                if (not limit or limit <= 0 or reading is None or reading[2] < since
                        or not _trusted(reading[1])):
                    continue
                try:
                    distance = float(reading[0])
                except (TypeError, ValueError):
                    continue
                if distance < limit:
                    close.append((distance - limit, sensor, distance, limit, reading[2]))
            if not close:
                return None
            _, sensor, distance, limit, arrived = min(close)
            return {"obstacle": {"sensor": sensor, "distance_m": round(distance, 3),
                                 "limit_m": limit, "t": arrived}}

    def wait_for_stop(self, since: float, timeout: float, limits=None, ledges=()):
        """Block until `stop_reason` gives a reason, or `timeout` seconds pass (None). A drive
        uses this as its wait, so it returns the moment the robot touches something, nears a
        drop, or gets closer than a sensor's limit to something it sees, instead of sleeping out
        the planned duration."""
        deadline = self._clock() + max(0.0, timeout)
        with self._cond:
            while True:
                reason = self.stop_reason(since, limits, ledges)
                if reason is not None:
                    return reason
                remaining = deadline - self._clock()
                if remaining <= _TIME_EPS:
                    return None
                self._wait(remaining)

    def wait_fresh(self, since: float, timeout: float) -> bool:
        """Block until every range sensor has reported after `since` (or `timeout`), so a
        reading taken right after a move describes where the robot ended up, not the reading
        that stopped it. Returns at once when offline: no fresh data is coming."""
        if not self.online:
            return False
        deadline = self._clock() + max(0.0, timeout)
        with self._cond:
            while True:
                if all(self._ranges.get(s, (None, None, -math.inf))[2] > since
                       for s in RANGE_SENSORS):
                    return True
                remaining = deadline - self._clock()
                if remaining <= _TIME_EPS:
                    return False
                self._wait(remaining)

    def idle(self, seconds: float) -> None:
        """Let `seconds` pass on the monitor's clock while messages keep arriving. In the
        simulation this is how time moves on with no drive running."""
        deadline = self._clock() + max(0.0, seconds)
        with self._cond:
            while (remaining := deadline - self._clock()) > _TIME_EPS:
                self._wait(remaining)

    def _wait(self, remaining: float) -> None:
        """Let up to `remaining` seconds pass, with the lock held on entry. On the robot: block
        until the WebSocket thread delivers the next message. In simulation: advance the
        simulated robot, which feeds its messages in on this thread and returns after the next
        one (or once `remaining` has passed)."""
        if self._source is not None:
            self._source.pump(remaining)
        else:
            self._cond.wait(remaining)
