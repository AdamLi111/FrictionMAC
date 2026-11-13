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

The robot's distance and bump sensors (robot_tools/sensors.py) are answered from the same
geometry: the four range sensors read the nearest solid surface in their cones, a collision
names the bumper(s) that hit, and a bumper stays pressed while the body touches something.
"""
import math

from . import geometry as geo
from ..sensors import BUMPERS, NO_READING, RANGE_SENSORS, TOF_MAX_RANGE_M

DEFAULT_FOV_DEGREES = 45.0        # matches the real Misty's narrow camera
DEFAULT_MAX_VIEW = 8.0            # metres
ROBOT_RADIUS = 0.12              # collision disc for the robot body
_STEP = 0.01                     # path-marching resolution for collision (metres)
RAY_STEP_DEGREES = 0.5           # spacing of the sight-rays that find walls and furniture

# Range sensors. Misty documents three on the front of the base and one on the back, out to
# TOF_MAX_RANGE_M, but publishes neither the width of a sensor's cone nor the angle of the two
# outer front ones. Those are this sim's assumptions: a 25° cone, typical of such sensors, with
# the outer two turned 30° out. Bearings are relative to the heading (negative = left).
TOF_BEARINGS = {"front_left": -30.0, "front_center": 0.0, "front_right": 30.0, "back": 180.0}
TOF_CONE_DEGREES = 25.0
TOF_RAY_STEP_DEGREES = 0.5
# Bumpers. A contact within BUMPER_ARC_DEGREES of dead ahead (or astern) presses the bumper on
# that side of the front (or back), and one within BOTH_BUMPERS_DEGREES of dead ahead/astern
# presses both; a contact at the robot's flank presses neither. "Touching" means within
# CONTACT_TOLERANCE of a surface, which covers where a collision leaves the robot (one _STEP
# short of the overlap).
BUMPER_ARC_DEGREES = 60.0
BOTH_BUMPERS_DEGREES = 15.0
CONTACT_TOLERANCE = 0.015


def bumpers_at(bearing: float, end=None) -> list:
    """The bumpers a contact at `bearing` (relative to the heading, negative = left) presses.
    `end` ("front" or "rear") is set for a collision, where the drive's direction decides which
    end hit; without it the contact has to lie within a bumper's arc."""
    astern = 180.0 - abs(bearing)          # 0 = dead astern
    if end is None:
        if abs(bearing) <= BUMPER_ARC_DEGREES:
            end = "front"
        elif astern <= BUMPER_ARC_DEGREES:
            end = "rear"
        else:
            return []
    if end == "front":
        if abs(bearing) <= BOTH_BUMPERS_DEGREES:
            return ["front_left", "front_right"]
        return ["front_left"] if bearing < 0 else ["front_right"]
    if astern <= BOTH_BUMPERS_DEGREES:
        return ["rear_left", "rear_right"]
    return ["rear_left"] if bearing < 0 else ["rear_right"]


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
        # History round-trips: snapshot() is a superset of the scene schema, so re-loading a
        # snapshot restores what the robot has already done (a pristine scene file simply has
        # neither key). The evaluation harness reads the live snapshot back every turn and would
        # otherwise lose every collision the robot has caused.
        self.action_history = list(scene.get("action_history", []))
        self._collision = None       # most recent collision, consumed by pop_collision()
        self.collisions = list(scene.get("collisions", []))   # full log (for the evaluator)

    # ------------------------------------------------------------------ movement
    def turn(self, degrees: float):
        """Rotate in place. Positive = right (clockwise); negative = left."""
        self.heading = (self.heading + degrees) % 360.0
        self.action_history.append(f"turned {'right' if degrees >= 0 else 'left'} "
                                   f"{abs(degrees):.0f}°")

    def move(self, distance: float) -> dict:
        """Advance `distance` metres along the current heading (negative = backward), stopping
        just before any collision. Returns {"collision": name|None, "message": str|None,
        "moved": metres actually travelled, "bumpers": the bumpers a collision pressed}."""
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
            contact = geo.shape_closest_point(hit.shape, last_safe)
            bumpers = bumpers_at(geo.relative_bearing(last_safe, self.heading, contact),
                                 end="front" if sign > 0 else "rear")
            return {"collision": hit.name, "message": col["message"], "moved": moved,
                    "bumpers": bumpers}
        return {"collision": None, "message": None, "moved": moved, "bumpers": []}

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

    # ------------------------------------------------------------------ proximity sensors
    def range_readings(self) -> dict:
        """What the four range sensors read from this pose: for each, the distance from the
        robot's body to the nearest solid surface inside its cone (any object, a cup included:
        anything the robot can bump into), or NO_READING when nothing lies within
        TOF_MAX_RANGE_M. Rays start at the robot's centre, so a hit minus the body radius is the
        gap in front of the bumper."""
        pos = (self.robot_pos[0], self.robot_pos[1])
        near = [o for o in self.objects
                if o.clearance(pos) <= self.robot_radius + TOF_MAX_RANGE_M]
        n = int(round(TOF_CONE_DEGREES / TOF_RAY_STEP_DEGREES)) + 1
        out = {}
        for sensor in RANGE_SENSORS:
            first = None
            for i in range(n):
                bearing = TOF_BEARINGS[sensor] - TOF_CONE_DEGREES / 2.0 + TOF_CONE_DEGREES * i / (n - 1)
                rad = math.radians(self.heading + bearing)
                ray = (math.sin(rad), math.cos(rad))
                for obj in near:
                    t = geo.ray_solid_distance(pos, ray, obj.shape)
                    if t is not None and (first is None or t < first):
                        first = t
            gap = None if first is None else max(0.0, first - self.robot_radius)
            out[sensor] = round(gap, 3) if gap is not None and gap <= TOF_MAX_RANGE_M else NO_READING
        return out

    def bumpers_pressed(self) -> list:
        """The bumpers pressed against something right now: those facing any object the body is
        touching. A collision leaves the bumper that hit pressed; backing off releases it, and so
        does turning until the contact is at the robot's flank."""
        pos = (self.robot_pos[0], self.robot_pos[1])
        pressed = set()
        for obj in self.objects:
            if obj.clearance(pos) < self.robot_radius + CONTACT_TOLERANCE:
                contact = geo.shape_closest_point(obj.shape, pos)
                pressed.update(bumpers_at(geo.relative_bearing(pos, self.heading, contact)))
        return [b for b in BUMPERS if b in pressed]

    # ------------------------------------------------------------------ perception
    def _visible_samples(self, obj, rays=None):
        """(distance, bearing) for each part of `obj` the camera sees right now: inside the FOV,
        in range, and not hidden behind another object.

        An extended object (wall, furniture) is found by sight-rays (`_ray_hits`), so it is seen
        however close the robot stands and whichever part of it is ahead, at the distance of its
        face; `rays` reuses a cast already made from this pose. A point is too small for rays to
        find reliably, so it is seen when its own bearing is in the FOV and the line to it is
        clear."""
        if obj.shape["type"] != "point":
            return (rays if rays is not None else self._ray_hits(only=obj)).get(obj, [])
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

    def _ray_hits(self, only=None):
        """Cast sight-rays edge to edge across the FOV, RAY_STEP_DEGREES apart, and return
        {object: [(distance, bearing), ...]}: for each ray, the extended object whose solid
        surface it meets first within range (whatever lies behind that is hidden on that ray).
        With `only`, rays that miss that object are skipped, which keeps a one-object question
        such as evaluation.tasks.visibility's cheap."""
        pos = (self.robot_pos[0], self.robot_pos[1])
        extended = [o for o in self.objects if o.shape["type"] != "point"]
        n = max(2, int(round(self.fov / RAY_STEP_DEGREES)) + 1)
        hits = {}
        for i in range(n):
            bearing = -self.fov / 2.0 + self.fov * i / (n - 1)
            rad = math.radians(self.heading + bearing)
            ray = (math.sin(rad), math.cos(rad))
            if only is not None:
                t = geo.ray_surface_distance(pos, ray, only.shape)
                if t is None or t > self.max_view:
                    continue
            first, first_t = None, None
            for obj in extended:
                t = geo.ray_surface_distance(pos, ray, obj.shape)
                if t is not None and t <= self.max_view and (first_t is None or t < first_t):
                    first, first_t = obj, t
            if first is not None:
                hits.setdefault(first, []).append((first_t, bearing))
        return hits

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
        rays = self._ray_hits()             # one cast serves every extended object
        for obj in self.objects:
            samples = self._visible_samples(obj, rays)
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
