"""
Omniscient view of the sim world — the counterpart to `pov.py`.

`pov.describe_view()` is what the ROBOT can see: one narrow ~45° cone, wall-occluded, no
distances it can trust. This module is what a HUMAN STANDING IN THE ROOM can see: every object
in every room, where the robot is, and which way it is facing. It exists for the simulated user
in the evaluation harness (`evaluation/`), which is deliberately omniscient — in the real world
the person giving the robot commands can look around the whole scene, while the robot perceives
only what is in front of its camera.

Nothing here is ever shown to the robot's agents; it is the user's side of the information
asymmetry.
"""
import math

from . import geometry as geo

DOORWAY_MIN_GAP = 0.35     # metres — a gap this wide or more between collinear walls is a door
_EPS = 1e-6


def _direction_phrase(bearing: float) -> str:
    """Signed bearing (deg, -left/+right) -> where that is relative to the robot's facing."""
    a = abs(bearing)
    side = "left" if bearing < 0 else "right"
    if a < 25:
        return "straight ahead of the robot"
    if a < 65:
        return f"ahead of the robot and to its {side}"
    if a < 115:
        return f"to the robot's {side}"
    if a < 155:
        return f"behind the robot and to its {side}"
    return "directly behind the robot"


def _compass(heading: float) -> str:
    """Scene-absolute facing, so the user can reason about the room independently of bearings.
    0° = +y; the labels are arbitrary but consistent with the scene files."""
    names = [("north (+y)", 0), ("north-east", 45), ("east (+x)", 90), ("south-east", 135),
             ("south (-y)", 180), ("south-west", 225), ("west (-x)", 270), ("north-west", 315)]
    h = heading % 360.0
    return min(names, key=lambda n: min(abs(h - n[1]), 360 - abs(h - n[1])))[0]


def _is_wall(obj) -> bool:
    return obj.shape["type"] == "segment"


def _doorways(world):
    """Gaps between collinear, axis-aligned wall segments — i.e. the openings between rooms.

    Scenes author a room as separate wall segments and leave a hole where the doorway is, so the
    doorway is not an object anywhere; it has to be recovered from the geometry. Only
    axis-aligned walls are analysed (all bundled scenes are), and anything else is skipped rather
    than guessed at."""
    lines = {}
    for obj in world.objects:
        if not _is_wall(obj):
            continue
        (a, b) = obj.shape["points"]
        if abs(a[0] - b[0]) < _EPS:                     # vertical wall: x is fixed
            key = ("x", round(a[0], 3))
            lo, hi = sorted((a[1], b[1]))
        elif abs(a[1] - b[1]) < _EPS:                   # horizontal wall: y is fixed
            key = ("y", round(a[1], 3))
            lo, hi = sorted((a[0], b[0]))
        else:
            continue                                    # diagonal wall — not analysed
        lines.setdefault(key, []).append((lo, hi))

    out = []
    for (axis, fixed), spans in lines.items():
        spans.sort()
        end = spans[0][1]
        for lo, hi in spans[1:]:
            gap = lo - end
            if gap >= DOORWAY_MIN_GAP:
                mid = (end + lo) / 2.0
                centre = (fixed, mid) if axis == "x" else (mid, fixed)
                out.append({"centre": centre, "width": gap, "axis": axis})
            end = max(end, hi)
    return out


def _object_lines(world, objects):
    pos = (world.robot_pos[0], world.robot_pos[1])
    lines = []
    for obj in objects:
        c = obj.centroid()
        dist = math.hypot(c[0] - pos[0], c[1] - pos[1])
        bearing = geo.relative_bearing(pos, world.heading, c)
        extra = [f"{k}: {v}" for k, v in sorted(obj.properties.items())]
        tag = f" [{'; '.join(extra)}]" if extra else ""
        lines.append(f"    - {obj.name}{tag} at ({c[0]:.1f}, {c[1]:.1f}) — "
                     f"{dist:.1f} m from the robot, {_direction_phrase(bearing)}.")
    return lines


def describe_world(world) -> str:
    """Full ground-truth description of the scene, written for a human observer in the room.

    Includes every object (grouped by room, with coordinates, distance and direction relative to
    the robot's current facing), the robot's pose, the doorways between rooms, and which objects
    happen to be inside the robot's narrow camera view right now — that last part is what lets
    the user tell "the robot cannot see it" apart from "the robot is confused"."""
    walls = [o for o in world.objects if _is_wall(o)]
    things = [o for o in world.objects if not _is_wall(o)]

    rooms, roomless = {}, []
    for obj in things:
        if obj.room:
            rooms.setdefault(obj.room, []).append(obj)
        else:
            roomless.append(obj)

    lines = [f"=== THE ROOM (scene '{world.name}') — everything you can see ===",
             "",
             f"You are standing in the room with the robot and can see all of it. The robot is at "
             f"({world.robot_pos[0]:.1f}, {world.robot_pos[1]:.1f}), facing {_compass(world.heading)} "
             f"(heading {world.heading:.0f}°). Its camera sees only a narrow ~{world.fov:.0f}° cone "
             f"in that direction, and walls block its view.",
             ""]

    for room in sorted(rooms, key=lambda r: (r is None, str(r))):
        lines.append(f"  {room}:")
        lines.extend(_object_lines(world, rooms[room]))
    if roomless:
        lines.append("  (not assigned to a room):")
        lines.extend(_object_lines(world, roomless))

    doors = _doorways(world)
    if doors:
        lines.append("")
        lines.append("  Openings between rooms (doorways — gaps in the walls):")
        for d in doors:
            cx, cy = d["centre"]
            lines.append(f"    - a {d['width']:.1f} m wide doorway at ({cx:.1f}, {cy:.1f})")
    if walls:
        lines.append(f"  Walls: {len(walls)} wall segments enclose and divide the rooms; the robot "
                     f"cannot drive or see through them.")

    visible = world.visible_objects()
    lines.append("")
    if visible:
        names = ", ".join(v["name"] for v in visible if v["name"] != "wall")
        lines.append(f"  In the robot's camera view right now: {names or 'only a wall'}.")
    else:
        lines.append("  In the robot's camera view right now: nothing — it is facing empty space.")

    if world.collisions:
        lines.append(f"  The robot has bumped into: "
                     f"{', '.join(c['object'] for c in world.collisions)}.")
    return "\n".join(lines)
