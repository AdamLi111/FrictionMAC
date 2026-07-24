"""
Robot backend selection.

Real mode (default): construct the vendored mistyPy Robot and FAIL LOUDLY at startup if
the robot is unreachable -- no silent pretending.

Stub mode (ROBOT_STUB=1): a drop-in StubRobot that records every call and returns fake
responses, so the whole tool layer can be exercised offline with no hardware.
"""
import requests

from . import config

# base64("stub-frame"); a non-empty stand-in for a captured JPEG frame.
STUB_FRAME_B64 = "c3R1Yi1mcmFtZQ=="


class StubRobot:
    """Records calls instead of touching hardware. Method surface matches mistyPy.Robot
    for the commands the tool layer uses."""

    def __init__(self):
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append({"method": method, "kwargs": kwargs})
        return {"stub": True, "method": method}

    def drive_time(self, linearVelocity=None, angularVelocity=None, timeMs=None, degree=None):
        return self._record("drive_time", linearVelocity=linearVelocity,
                            angularVelocity=angularVelocity, timeMs=timeMs, degree=degree)

    def stop(self, hold=None):
        return self._record("stop", hold=hold)

    def speak(self, text=None, **kwargs):
        return self._record("speak", text=text, **kwargs)

    def take_picture(self, **kwargs):
        self._record("take_picture", **kwargs)
        return {"result": {"name": "stub.jpg", "base64": STUB_FRAME_B64}}

    def move_head(self, **kwargs):
        return self._record("move_head", **kwargs)

    def move_arm(self, **kwargs):
        return self._record("move_arm", **kwargs)

    def change_led(self, **kwargs):
        return self._record("change_led", **kwargs)

    def display_image(self, **kwargs):
        return self._record("display_image", **kwargs)

    # --- test/introspection helpers ---
    def calls_of(self, method):
        return [c for c in self.calls if c["method"] == method]


def check_reachable(ip: str, timeout: float = 5.0) -> None:
    """Raise if the robot does not answer a lightweight GET."""
    resp = requests.get(f"http://{ip}/api/device", timeout=timeout)
    resp.raise_for_status()


def make_robot():
    """Return a StubRobot (stub mode) or a live mistyPy Robot (real mode)."""
    if config.is_stub():
        return StubRobot()

    ip = config.misty_ip()
    if not ip:
        raise RuntimeError(
            "MISTY_IP is not set. Set MISTY_IP to the robot's address for real mode, "
            "or set ROBOT_STUB=1 for offline development."
        )
    try:
        check_reachable(ip)
    except Exception as e:
        raise RuntimeError(
            f"Misty robot at {ip} is unreachable ({e}). Refusing to start in real mode. "
            "Fix connectivity, or set ROBOT_STUB=1 for offline development."
        ) from e

    from .vendor.mistyPy.Robot import Robot
    return Robot(ip)
