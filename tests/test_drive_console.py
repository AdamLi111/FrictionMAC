"""
The hand-driving console (scripts/drive_console.py): its command parser, how it words results,
and the live state its page is drawn from. No robot and no web server: the live state is fed the
messages Misty's pub/sub would send, through the same SensorMonitor listener the console uses.
"""
import json

import pytest

from scripts import drive_console as dc


@pytest.mark.parametrize("line, expected", [
    ("move forward 1 meter", ("move", "forward", 1.0, None)),
    ("Move  Forward 5 meters", ("move", "forward", 5.0, None)),
    ("forward 0.5", ("move", "forward", 0.5, None)),
    ("f 50 cm", ("move", "forward", 0.5, None)),
    ("move backward 30cm", ("move", "backward", 0.3, None)),
    ("back 0.3 stop within 0", ("move", "backward", 0.3, 0.0)),
    ("forward 1 stop 20 cm", ("move", "forward", 1.0, 0.2)),
    ("go ahead 1m stop_within 0.3", ("move", "forward", 1.0, 0.3)),
    ("turn left 90 degrees", ("turn", "left", 90.0)),
    ("turn right 30°", ("turn", "right", 30.0)),
    ("l 45", ("turn", "left", 45.0)),
    ("r", ("turn", "right", 90.0)),
    ("stop_within 0.2", ("set_stop", 0.2)),
    ("stop within 20 cm", ("set_stop", 0.2)),
    ("stop", ("stop",)),
    ("sensors", ("sensors",)),
    ("quit", ("quit",)),
    ("   ", (None,)),
])
def test_commands_parse(line, expected):
    got = dc.parse(line)
    assert got[0] == expected[0]
    for g, e in zip(got[1:], expected[1:]):
        assert g == pytest.approx(e) if isinstance(e, float) else g == e


@pytest.mark.parametrize("line", ["move forward 9 meters", "turn left 400", "spin around",
                                  "forward", "move sideways 1"])
def test_nonsense_and_overlong_moves_are_refused(line):
    assert dc.parse(line)[0] == "error"


def test_results_are_worded_by_what_stopped_the_move():
    assert dc.describe({"ok": True, "duration_ms": 2500})[0] == "ok"
    obstacle = {"ok": False, "obstacle": {"sensor": "front_center", "distance_m": 0.12,
                                          "limit_m": 0.15, "after_ms": 800,
                                          "estimated_traveled_m": 0.1}}
    outcome, detail = dc.describe(obstacle)
    assert outcome == "obstacle" and "front_center" in detail and "0.12" in detail
    refused = {"ok": False, "obstacle": dict(obstacle["obstacle"], already_within=True)}
    assert dc.describe(refused)[0] == "refused"
    bump = {"ok": False, "bump": {"bumpers": ["front_left"], "after_ms": 1250,
                                  "estimated_traveled_m": 0.25}}
    assert dc.describe(bump)[0] == "bump"
    ledge = {"ok": False, "ledge": {"ledges": ["front_left"], "after_ms": 1000,
                                    "estimated_traveled_m": 0.16}}
    assert dc.describe(ledge)[0] == "ledge" and "front_left" in dc.describe(ledge)[1]
    held = {"ok": False, "ledge": {"ledges": ["rear_left"], "after_ms": 0,
                                   "already_detected": True}}
    assert dc.describe(held)[0] == "refused"
    assert dc.describe({"ok": False, "error": "RuntimeError: boom"}) == ("error",
                                                                         "RuntimeError: boom")


class _Clock:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


def _msg(**payload):
    return {"eventName": "x", "message": payload}


@pytest.fixture
def live(tmp_path):
    from robot_tools import sensors
    monitor = sensors.SensorMonitor(clock=_Clock())
    monitor._connected = True
    return dc.Live(monitor, "real", "test", tmp_path / "sensors.jsonl"), monitor


def test_the_live_state_shows_every_sensor_the_page_draws(live):
    state, monitor = live
    monitor.feed(_msg(distanceInMeters=0.42, sensorId="toffc", status=0, type="Range"))
    monitor.feed(_msg(distanceInMeters=0.023, sensorId="tofdfl", status=0, type="Edge",
                      inHazard=False))
    monitor.feed(_msg(distanceInMeters=0.12, sensorId="tofdfr", status=0, type="Edge",
                      inHazard=True))
    monitor.feed(_msg(isContacted=True, sensorId="bfr"))
    monitor.feed(_msg(timeOfFlightSensorsHazardState=[
        {"sensorName": "TOF_DownFrontRight", "inHazard": True}], criticalInternalError=False))

    snap = state.snapshot()
    assert snap["range"]["front_center"]["value"] == 0.42          # what the agents would see
    assert snap["range"]["back"]["value"] == "unknown"
    assert snap["edge"]["front_left"]["drop"] is False
    assert snap["edge"]["front_right"]["drop"] is True             # a ledge
    assert snap["bumpers"]["front_right"] is True
    assert snap["hazard"]["active"] == ["timeOfFlightSensorsHazardState:TOF_DownFrontRight"]
    json.dumps(snap)                                               # the page gets it as JSON

    kinds = [e["kind"] for e in state.subscribe()[1]]
    assert kinds == ["bump", "hazard"]
    assert "ledge 2.3 12.0! ? ? cm" in dc.readings_line(snap)


def test_every_message_is_logged_for_later(live, tmp_path):
    state, monitor = live
    monitor.feed(_msg(distanceInMeters=0.42, sensorId="toffc", status=0))
    monitor.feed('{"eventName": "y", "message": "Registration Status: API event registered."}')
    state.close()
    rows = [json.loads(line) for line in (tmp_path / "sensors.jsonl").read_text().splitlines()]
    assert [r["msg"]["eventName"] for r in rows] == ["x", "y"]
    assert rows[0]["t"] == 50.0
