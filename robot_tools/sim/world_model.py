"""
Ground-truth world model for the simulation.

This is the *physics/truth* of the simulated room: where the robot is, where every object is,
what collides, and what the camera can actually see (narrow FOV + wall occlusion). It is
deliberately separate from the agent's *belief* store (robot_tools.world_state / the
get_world/update_world tools) — the sim never writes the agent's memory; it only answers
perception and movement truthfully, exactly as the physical robot + room would.

Movement is open-loop dead-reckoning, matching the real tools: forward advances along the
current heading; turning rotates in place. Collisions are detected against every object's
geometry (there is no "obstacle" flag) by marching the robot disc along its path and stopping
just before the first overlap; the object hit is recorded so the evaluator can decide whether
that collision means success (the user asked to reach/bump it) or failure.
"""
import math

from . import geometry as geo

DEFAULT_FOV_DEGREES = 45.0        # matches the real Misty's narrow camera
DEFAULT_MAX_VIEW = 8.0            # metres
ROBOT_RADIUS = 0.12              # collision disc for the robot body
_STEP = 0.01                     # path-marching resolution for collision (metres)


class SimObject:
    def __init__(self, name, shape, room=None, properties=None):
        self.name = name
        self.shape = shape
        self.room = room
        self.properties = properties or {}

    def clearance(self, p):
        return geo.shape_clearance(self.shape, p)

    def blocks_ray(self, p, q):
        return geo.shape_blocks_ray(self.shape, p, q)

    def samples(self):
        return geo.shape_samples(self.shape)

    def centroid(self):
        return geo.shape_centroid(self.shape)


class SimWorld:
    def __init__(self, scene: dict):
        self.name = scene.get("name", "scene")
        robot = scene.get("robot", {})
        self.robot_pos = list(robot.get("position", [0.0, 0.0]))
        self.heading = float(robot.get("heading", 0.0))
        self.fov = float(scene.get("fov_degrees", DEFAULT_FOV_DEGREES))
        self.max_view = float(scene.get("max_view_distance", DEFAULT_MAX_VIEW))
        self.robot_radius = float(scene.get("robot_radius", ROBOT_RADIUS))
        self.objects = [
            SimObject(o["name"], o["shape"], o.get("room"), o.get("properties"))
            for o in scene.get("objects", [])
        ]
        self.action_history = []
        self._collision = None       # most recent collision, consumed by pop_collision()
        self.collisions = []         # full log (for the evaluator)

    # ------------------------------------------------------------------ movement
    def turn(self, degrees: float):
        """Rotate in place. Positive = right (clockwise); negative = left."""
        self.heading = (self.heading + degrees) % 360.0
        self.action_history.append(f"turned {'right' if degrees >= 0 else 'left'} "
                                   f"{abs(degrees):.0f}°")

    def move(self, distance: float) -> dict:
        """Advance `distance` metres along the current heading (negative = backward), stopping
        just before any collision. Returns {"collision": name|None, "message": str|None}."""
        rad = math.radians(self.heading)
        sign = 1.0 if distance >= 0 else -1.0
        ux, uy = math.sin(rad) * sign, math.cos(rad) * sign
        total = abs(distance)

        start = (self.robot_pos[0], self.robot_pos[1])
        steps = max(1, int(total / _STEP))
        last_safe = start
        hit = None
        for i in range(1, steps + 1):
            d = min(total, i * _STEP)
            cand = (start[0] + ux * d, start[1] + uy * d)
            obj = self._first_overlap(cand)
            if obj is not None:
                hit = obj
                break
            last_safe = cand

        self.robot_pos = [last_safe[0], last_safe[1]]
        moved = math.hypot(last_safe[0] - start[0], last_safe[1] - start[1])
        self.action_history.append(
            f"moved {'forward' if sign > 0 else 'backward'} {moved:.2f}m"
            + (f" then collided with {hit.name}" if hit else ""))

        if hit is not None:
            col = {
                "object": hit.name,
                "position": [round(self.robot_pos[0], 3), round(self.robot_pos[1], 3)],
                "message": f"collided with {hit.name}",
            }
            self._collision = col
            self.collisions.append(col)
            return {"collision": hit.name, "message": col["message"]}
        return {"collision": None, "message": None}

    def _first_overlap(self, p):
        """The first object whose solid geometry the robot disc at p overlaps, else None."""
        for obj in self.objects:
            if obj.clearance(p) < self.robot_radius:
                return obj
        return None

    def pop_collision(self):
        """Return and clear the most recent collision (so a tool call reports it once)."""
        col, self._collision = self._collision, None
        return col

    # ------------------------------------------------------------------ perception
    def _visible_samples(self, obj):
        """Samples of `obj` that are within FOV, in range, and not occluded by another object.
        Returns a list of (distance, bearing) for the visible ones."""
        half = self.fov / 2.0
        pos = (self.robot_pos[0], self.robot_pos[1])
        out = []
        for s in obj.samples():
            dist = math.hypot(s[0] - pos[0], s[1] - pos[1])
            if dist > self.max_view or dist < 1e-6:
                continue
            bearing = geo.relative_bearing(pos, self.heading, s)
            if abs(bearing) > half:
                continue
            if self._occluded(pos, s, obj):
                continue
            out.append((dist, bearing))
        return out

    def _occluded(self, p, q, target_obj) -> bool:
        """True if any OTHER object's geometry blocks the sight-line p→q."""
        for obj in self.objects:
            if obj is target_obj:
                continue
            if obj.blocks_ray(p, q):
                return True
        return False

    def visible_objects(self):
        """List of visible objects with computed viewing geometry, nearest first."""
        seen = []
        for obj in self.objects:
            samples = self._visible_samples(obj)
            if not samples:
                continue
            dist = min(s[0] for s in samples)
            bearings = [s[1] for s in samples]
            seen.append({
                "name": obj.name,
                "distance": dist,
                "bearing": min(bearings, key=abs),   # bearing of the nearest-to-centre part
                "bearing_min": min(bearings),
                "bearing_max": max(bearings),
                "extended": (obj.shape["type"] != "point"),
                "properties": obj.properties,
                "room": obj.room,
            })
        seen.sort(key=lambda o: o["distance"])
        return seen

    def describe_view(self) -> str:
        """Synthetic text POV of what's directly ahead (FOV + occlusion), for capture_view."""
        from . import pov
        return pov.describe_view(self)

    # ------------------------------------------------------------------ truth dump
    def snapshot(self) -> dict:
        """Full world state. Superset of the scene schema (adds collisions/action_history), so a
        snapshot can be re-loaded as a scene to view/replay the live pose."""
        return {
            "name": self.name,
            "fov_degrees": self.fov,
            "max_view_distance": self.max_view,
            "robot_radius": self.robot_radius,
            "robot": {"position": [round(v, 3) for v in self.robot_pos],
                      "heading": round(self.heading, 1)},
            "objects": [{"name": o.name, "shape": o.shape, "room": o.room,
                         "properties": o.properties} for o in self.objects],
            "collisions": list(self.collisions),
            "action_history": list(self.action_history),
        }
