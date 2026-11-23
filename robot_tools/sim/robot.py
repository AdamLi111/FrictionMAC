"""
The simulated Misty: drives through the sim world over simulated time, and streams its distance
and bump sensors the way the real robot does.

On the robot, a drive is a timed command (drive_time), the robot moves while it runs, and the
sensors arrive as a stream of pub/sub messages that SensorMonitor (sensors.py) collects and the
drive tools watch, to stop at a bump or when something gets closer than `stop_within`. SimRobot
reproduces that chain, so the sim runs the same tools.py and SensorMonitor code as the robot:

  - drive_time() and stop() start and end a motion, and the robot moves along it as simulated
    time passes. A straight drive follows the drive calibration (runtime.drive_distance_at), so
    it moves at the speed the tools assume the real robot does; a turn rotates at a steady rate.
    The world's geometry stops the robot at the first thing it touches.
  - The four range sensors publish Misty-format TimeOfFlight messages, each on its own cycle of
    about 0.32 s, as measured on the robot, with a few millimetres of noise: status 0 and the gap
    to the nearest surface in the cone when there is one within range, otherwise status 102
    (signal fail) and a meaningless distance, as the real sensors give when nothing is in front.
  - The four bumpers publish a BumpSensor message on every contact and every release, a moment
    after it happens. A bumper stays pressed while the body touches something on its side.

Nothing waits on the wall clock. Simulated time passes only when something waits on the monitor
(`pump`), so a four-second drive takes milliseconds and every run with the same seed repeats.
BUMP_LATENCY_S and STOP_LATENCY_S are assumptions; the rest of the timing was measured on Misty.
"""
import heapq
import itertools
import json
import math
import random
import threading
import time
from datetime import datetime, timezone

from .. import runtime
from ..backend import StubRobot
from ..sensors import _BUMP_IDS, _TOF_IDS, BUMPERS, RANGE_SENSORS, SensorMonitor

TOF_PERIOD_S = 0.316       # one range sensor's reading interval on Misty (median, 2026-11-22)
TOF_JITTER_S = 0.02        # spread of that interval either way
TOF_NOISE_SD_M = 0.003     # spread of a steady reading on the robot (it wandered about ±5 mm)
BUMP_LATENCY_S = 0.05      # contact to message (assumed: "a few tens of ms")
STOP_LATENCY_S = 0.1       # stop() to wheels stopped (assumed)
PHYSICS_DT_S = 0.02        # motion is stepped at least this finely
_EPS = 1e-9

# How Misty itself names each sensor in its messages.
TOF_ID = {name: sensor_id for sensor_id, name in _TOF_IDS.items()}
TOF_POSITION = {"front_left": "Left", "front_center": "Center", "front_right": "Right",
                "back": "Back"}
BUMP_ID = {name: sensor_id for sensor_id, name in _BUMP_IDS.items()}
BUMP_NAME = {"front_left": "Bump_FrontLeft", "front_right": "Bump_FrontRight",
             "rear_left": "Bump_RearLeft", "rear_right": "Bump_RearRight"}


class SimRobot(StubRobot):
    """A virtual Misty behind the robot interface the tools use. `monitor` is its
    SensorMonitor, fed by its own sensor stream; runtime.get_sensors() hands it to the tools."""

    def __init__(self, world, seed: int = 0):
        super().__init__()
        self.world = world
        self._rng = random.Random(seed)
        self._lock = threading.RLock()
        self._t = 0.0
        self._epoch = time.time()          # wall time that simulated time 0 stands for
        self._motion = None
        self._plan = {}
        self._pressed = set()
        self._queue = []                   # bump messages on their way: (due, seq, message)
        self._seq = itertools.count()
        n = len(RANGE_SENSORS)             # each sensor on its own cycle, staggered
        self._next_tof = {s: TOF_PERIOD_S * (i + 1) / n for i, s in enumerate(RANGE_SENSORS)}
        self.monitor = SensorMonitor(clock=self.now, source=self)
        with self._lock:
            self._update_bumpers()         # a robot placed against something starts pressed
        self.monitor.idle(TOF_PERIOD_S + TOF_JITTER_S)     # every sensor has reported once

    def now(self) -> float:
        """Simulated time, in seconds: the monitor's clock."""
        return self._t

    # ------------------------------------------------------------------ robot interface
    def plan_motion(self, distance_m=None, degrees=None):
        """The exact amount the next drive_time is for. The real robot only gets a velocity and
        a duration; the tools tell the simulation the distance or angle they asked for, so a 1 m
        move covers 1 m here instead of what the calibration's rounding would give back."""
        with self._lock:
            self._plan = {"distance_m": distance_m, "degrees": degrees}

    def drive_time(self, linearVelocity=None, angularVelocity=None, timeMs=None, degree=None):
        """Start a timed straight drive (linear only) or turn in place (angular only; positive
        is counter-clockwise, i.e. left). It replaces any motion still running, as on Misty."""
        with self._lock:
            result = super().drive_time(linearVelocity, angularVelocity, timeMs, degree)
            self._end_motion()
            plan, self._plan = self._plan, {}
            lin, ang = linearVelocity or 0, angularVelocity or 0
            motion = {"t0": self._t, "duration": max(0.0, (timeMs or 0) / 1000.0),
                      "stop_at": None, "done": 0.0, "hit": None}
            if lin and not ang:
                planned = plan.get("distance_m")
                if planned is None:
                    planned = runtime.drive_distance_at(timeMs or 0, 1e6)
                motion.update(kind="drive", sign=1.0 if lin > 0 else -1.0, planned=abs(planned))
            elif ang and not lin:
                planned = plan.get("degrees")
                if planned is None:
                    planned = _turn_degrees(timeMs or 0)
                # Counter-clockwise (left) lowers the heading, as SimWorld.turn counts it.
                motion.update(kind="turn", sign=-1.0 if ang > 0 else 1.0, planned=abs(planned))
            else:
                return result                  # an arc: the tools never send one
            self._motion = motion
            return result

    def stop(self, hold=None):
        """Stop the motion; the wheels come to rest STOP_LATENCY_S later."""
        with self._lock:
            result = super().stop(hold)
            motion = self._motion
            if motion is not None and motion["stop_at"] is None:
                motion["stop_at"] = self._t + STOP_LATENCY_S
            return result

    # ------------------------------------------------------------------ simulated time
    def pump(self, max_dt: float) -> None:
        """Advance simulated time by up to `max_dt`: move the robot, and feed the monitor every
        message that falls due, returning right after the first batch so the waiting tool can
        check it. SensorMonitor calls this whenever it waits."""
        with self._lock:
            target = self._t + max(0.0, max_dt)
            while True:
                if self._next_due() <= self._t + _EPS:
                    self._deliver_due()
                    return
                if target - self._t <= _EPS:
                    self._t = max(self._t, target)
                    self._step()
                    return
                self._t = min(target, self._next_due(), self._t + PHYSICS_DT_S)
                self._step()

    def _next_due(self) -> float:
        queued = self._queue[0][0] if self._queue else math.inf
        return min(queued, min(self._next_tof.values()))

    def _deliver_due(self) -> None:
        ready = []
        while self._queue and self._queue[0][0] <= self._t + _EPS:
            ready.append(heapq.heappop(self._queue)[2])
        for sensor in RANGE_SENSORS:
            if self._next_tof[sensor] <= self._t + _EPS:
                ready.append(self._tof_message(sensor))
                self._next_tof[sensor] += TOF_PERIOD_S + self._rng.uniform(-TOF_JITTER_S,
                                                                            TOF_JITTER_S)
        for message in ready:
            self.monitor.feed(json.dumps(message))    # the same parsing as the robot's stream

    # ------------------------------------------------------------------ physics
    def _step(self) -> None:
        """Move the robot along the current motion to the present time, then update the
        bumpers."""
        motion = self._motion
        if motion is not None:
            full = motion["t0"] + motion["duration"]
            end = full if motion["stop_at"] is None else min(full, motion["stop_at"])
            now = min(self._t, end)
            if motion["kind"] == "drive":
                if now >= full - _EPS:
                    goal = motion["planned"]
                else:
                    goal = runtime.drive_distance_at((now - motion["t0"]) * 1000.0,
                                                     motion["planned"])
                if motion["hit"] is None and goal - motion["done"] > _EPS:
                    moved, hit = self.world.advance(motion["sign"] * (goal - motion["done"]))
                    motion["done"] += moved
                    if hit is not None:            # pressed against it from now on
                        motion["hit"] = hit
                        self.world.note_collision(hit)
            else:
                frac = 1.0 if motion["duration"] <= 0 else min(
                    1.0, (now - motion["t0"]) / motion["duration"])
                goal = motion["planned"] * frac
                if goal - motion["done"] > _EPS:
                    self.world.rotate(motion["sign"] * (goal - motion["done"]))
                    motion["done"] = goal
            if self._t >= end - _EPS:
                self._end_motion()
        self._update_bumpers()

    def _end_motion(self) -> None:
        motion, self._motion = self._motion, None
        if motion is None:
            return
        if motion["kind"] == "drive":
            self.world.record_drive(motion["sign"] * motion["done"], motion["hit"])
        else:
            self.world.record_turn(motion["sign"] * motion["done"])

    def _update_bumpers(self) -> None:
        """Send a bump message for every bumper that was just pressed or released: those
        touching something on their side, plus, while a drive is pushing into what it hit, the
        bumpers on the end that hit it."""
        pressed = set(self.world.bumpers_pressed())
        motion = self._motion
        if motion is not None and motion["kind"] == "drive" and motion["hit"] is not None:
            end = "front" if motion["sign"] > 0 else "rear"
            pressed.update(self.world.bumpers_facing(motion["hit"], end))
        for bumper in BUMPERS:
            if (bumper in pressed) != (bumper in self._pressed):
                message = self._bump_message(bumper, bumper in pressed)
                heapq.heappush(self._queue, (self._t + BUMP_LATENCY_S, next(self._seq), message))
        self._pressed = pressed

    # ------------------------------------------------------------------ messages
    def _created(self) -> str:
        return datetime.fromtimestamp(self._epoch + self._t, timezone.utc).isoformat(
            timespec="milliseconds")

    def _tof_message(self, sensor: str) -> dict:
        gap = self.world.range_reading(sensor)
        if gap is None:          # signal fail: nothing in range, and the distance means nothing
            status, distance = 102, round(self._rng.uniform(0.0, 0.3), 3)
        else:
            status = 0
            distance = round(max(0.0, gap + self._rng.gauss(0.0, TOF_NOISE_SD_M)), 3)
        return {"eventName": f"ponder_tof_{TOF_ID[sensor]}", "message": {
            "created": self._created(), "distanceInMeters": distance,
            "sensorId": TOF_ID[sensor], "sensorPosition": TOF_POSITION[sensor],
            "status": status, "type": "Range"}}

    def _bump_message(self, bumper: str, contacted: bool) -> dict:
        return {"eventName": "ponder_bump", "message": {
            "created": self._created(), "isContacted": contacted,
            "sensorId": BUMP_ID[bumper], "sensorName": BUMP_NAME[bumper]}}


def _turn_degrees(time_ms: float) -> float:
    """The turn whose calibrated time is closest to `time_ms`, to 0.1°: only for a drive_time
    sent without plan_motion (the turn calibration isn't monotonic, so it has no inverse)."""
    return min(range(0, 3601), key=lambda tenth: abs(runtime.calc_turn_time(tenth / 10.0)
                                                     - time_ms)) / 10.0
