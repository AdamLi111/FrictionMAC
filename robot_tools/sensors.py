"""
Misty II's proximity sensors, read live: the four outward-facing time-of-flight (ToF) range
sensors and the four bump sensors.

What the hardware gives (Misty's docs):
  - Range ToF: three on the front of the base (left, centre, right) and one on the back. Each
    reports ONE distance, in metres, to the nearest surface inside its narrow cone, plus a status
    code (0 = valid, 1xx = low confidence, 2xx = error). A reading has no angle: its direction is
    simply which sensor produced it. 10-1200 mm at 1 mm resolution, less accurate beyond 1.2 m.
    Misty publishes neither the cone's width nor the angle of the two outer front sensors.
  - Four more ToF sensors point down at the base's corners to detect ledges (10-150 mm). Misty's
    own hazard system watches those; they are not read here.
  - Bump: ten contact switches, reported as four sensors (front-left, front-right, rear-left,
    rear-right), with one event on contact and another on release.
  - Misty's hazard system stops a forward drive when a front bumper is pressed (or a ledge is
    seen) and ignores forward drive commands until the bumper is released. It does NOT stop for
    anything the range sensors see.

None of this is available over REST, only over the WebSocket pub/sub at ws://<ip>/pubsub, so
SensorMonitor keeps one connection open on a daemon thread. Each range sensor gets its own
subscription, because a single ToF stream favours whichever sensor updated last. The vendored
mistyPy Event class isn't used: its callbacks have the pre-1.0 websocket-client signature, which
websocket-client 1.x (the robot env's) calls with an extra argument, so its subscription is never
even sent. The callbacks here accept either convention.

Times are the monitor's own monotonic clock at the moment a message ARRIVED (a few tens of ms
after the event), so they compare directly with the time a drive was commanded. A bump also
keeps the robot's own `created` timestamp, for the record.
"""
import json
import sys
import threading
import time

#: Agent-facing sensor names, in a fixed order.
RANGE_SENSORS = ("front_left", "front_center", "front_right", "back")
BUMPERS = ("front_left", "front_right", "rear_left", "rear_right")
FRONT_BUMPERS = ("front_left", "front_right")
REAR_BUMPERS = ("rear_left", "rear_right")

TOF_MAX_RANGE_M = 1.2          # Misty: 10-1200 mm, less accurate beyond
NO_READING = "no reading"      # the sensor answered but measured nothing reliable
UNKNOWN = "unknown"            # no recent data from that sensor at all
OFFLINE = "offline: no distance or bump data"
MAX_AGE_S = 1.0                # a reading older than this counts as UNKNOWN

_TOF_IDS = {"toffl": "front_left", "toffc": "front_center", "toffr": "front_right",
            "tofr": "back"}
_TOF_POSITIONS = {"left": "front_left", "center": "front_center", "right": "front_right",
                  "back": "back"}
_BUMP_IDS = {"bfl": "front_left", "bfr": "front_right", "brl": "rear_left", "brr": "rear_right"}
_BUMP_NAMES = {"bump_frontleft": "front_left", "bump_frontright": "front_right",
               "bump_rearleft": "rear_left", "bump_rearright": "rear_right"}

_TOF_DEBOUNCE_MS = 100         # each range sensor about ten times a second
_BUMP_DEBOUNCE_MS = 0          # every contact and every release
_RECONNECT_S = 2.0
_SILENT_S = 2.0                # connected, but no data for this long: not actually streaming
_KEEP_CONTACTS = 50


def subscription_messages() -> list:
    """Misty pub/sub subscriptions: one per range sensor, filtered on its SensorId, and one for
    all four bump sensors."""
    subs = [{"Operation": "subscribe", "Type": "TimeOfFlight", "DebounceMs": _TOF_DEBOUNCE_MS,
             "EventName": f"ponder_tof_{sensor_id}", "Message": "",
             "EventConditions": [{"Property": "SensorId", "Inequality": "=", "Value": sensor_id}]}
            for sensor_id in _TOF_IDS]
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


class SensorMonitor:
    """The latest range readings and bump state, fed by Misty's event stream (or by `feed` in
    tests). Thread-safe: the WebSocket thread writes, tool threads read and wait."""

    def __init__(self, ip=None, clock=time.monotonic):
        self.ip = ip
        self._clock = clock
        self._cond = threading.Condition()
        self._ranges = {}             # sensor -> (distance, status, arrived)
        self._pressed = {}            # bumper -> currently pressed
        self._contacts = []           # [{"bumper", "t", "time"}], most recent last
        self._last_data = None
        self._connected = False
        self._stopping = threading.Event()
        self._thread = None
        self._app = None

    def now(self) -> float:
        return self._clock()

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
                for sub in subscription_messages():
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
        for sub in subscription_messages():
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
        range or bump reading (a subscription acknowledgement, a downward edge sensor) is
        ignored, and a malformed message never raises."""
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        payload = _field(data, "message")
        if not isinstance(payload, dict):
            return                        # e.g. "Registration Status: API event registered."
        now = self._clock()
        if _field(payload, "distanceInMeters") is not None:
            sensor = (_TOF_IDS.get(str(_field(payload, "sensorId") or "").lower())
                      or _TOF_POSITIONS.get(str(_field(payload, "sensorPosition") or "").lower()))
            if sensor is None:
                return                    # a downward (edge) sensor
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

    # ------------------------------------------------------------------ queries
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
                    valid = status is None or int(status) == 0
                    out[sensor] = round(float(distance), 3) if valid else NO_READING
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

    def wait_for_contact(self, since: float, timeout: float) -> list:
        """Block until a bumper contact arrives at or after `since`, or `timeout` seconds pass.
        Returns the contacts (empty on timeout). A drive uses this as its wait, so it returns the
        moment the robot touches something instead of sleeping out the planned duration."""
        deadline = self._clock() + max(0.0, timeout)
        with self._cond:
            while True:
                hits = [dict(c) for c in self._contacts if c["t"] >= since]
                if hits:
                    return hits
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    def wait_fresh(self, since: float, timeout: float) -> bool:
        """Block until every range sensor has reported at or after `since` (or `timeout`), so a
        reading taken right after a move describes where the robot ended up. Returns at once when
        offline: no fresh data is coming."""
        if not self.online:
            return False
        deadline = self._clock() + max(0.0, timeout)
        with self._cond:
            while True:
                if all(self._ranges.get(s, (None, None, -1.0))[2] >= since
                       for s in RANGE_SENSORS):
                    return True
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
