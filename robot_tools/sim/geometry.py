"""
2-D geometry primitives for the simulation world model.

Everything in the sim is an *object* with a `shape`; there is no separate "obstacle"
concept. A shape is one of:
  - point   : {"type": "point",   "position": [x, y], "radius": r}      (cups, bottles…)
  - segment : {"type": "segment", "points": [[x1,y1],[x2,y2]], "thickness": t}  (walls)
  - rect    : {"type": "rect",    "min": [x,y], "max": [x,y]}           (tables, big furniture)

A room is authored as several independent wall `segment`s (a doorway is just a gap between
two segments). Walls occlude vision and block movement; both follow from the shape geometry.

Coordinate convention (shared with the world model): position [x, y]; heading in degrees with
0° = +y ("north"/forward), and a heading's forward unit vector is (sin θ, cos θ). Turning right
increases θ (clockwise), turning left decreases it.
"""
import math

Point = tuple  # (x, y)


# --------------------------------------------------------------------------- scalar helpers
def _clamp01(t: float) -> float:
    return 0.0 if t < 0.0 else 1.0 if t > 1.0 else t


def dist_point_to_segment(p, a, b) -> float:
    """Shortest distance from point p to the line segment a—b."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 <= 1e-12:                      # degenerate segment == point
        return math.hypot(px - ax, py - ay)
    t = _clamp01(((px - ax) * dx + (py - ay) * dy) / seg_len2)
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def dist_point_to_rect(p, mn, mx) -> float:
    """Shortest distance from point p to an axis-aligned rectangle [mn, mx]. 0 if inside."""
    px, py = p
    dx = max(mn[0] - px, 0.0, px - mx[0])
    dy = max(mn[1] - py, 0.0, py - mx[1])
    return math.hypot(dx, dy)


def _ccw(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d) -> bool:
    """True if segment a—b properly crosses (or touches) segment c—d."""
    d1 = _ccw(c, d, a)
    d2 = _ccw(c, d, b)
    d3 = _ccw(a, b, c)
    d4 = _ccw(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # Collinear/touching endpoints: treat a touch as an intersection.
    def on(seg_a, seg_b, pt):
        return (min(seg_a[0], seg_b[0]) - 1e-9 <= pt[0] <= max(seg_a[0], seg_b[0]) + 1e-9 and
                min(seg_a[1], seg_b[1]) - 1e-9 <= pt[1] <= max(seg_a[1], seg_b[1]) + 1e-9)
    if abs(d1) < 1e-12 and on(c, d, a):
        return True
    if abs(d2) < 1e-12 and on(c, d, b):
        return True
    if abs(d3) < 1e-12 and on(a, b, c):
        return True
    if abs(d4) < 1e-12 and on(a, b, d):
        return True
    return False


def _rect_edges(mn, mx):
    x0, y0 = mn
    x1, y1 = mx
    return (
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    )


# --------------------------------------------------------------------------- shape queries
def shape_clearance(shape, p) -> float:
    """Distance from point p to the SOLID boundary of `shape` (0 if p is inside/on it).
    Used for collision: the robot (a disc of radius R) collides when clearance < R."""
    kind = shape["type"]
    if kind == "point":
        return max(0.0, math.hypot(p[0] - shape["position"][0], p[1] - shape["position"][1])
                   - shape.get("radius", 0.0))
    if kind == "segment":
        (a, b) = shape["points"]
        return max(0.0, dist_point_to_segment(p, a, b) - shape.get("thickness", 0.0) / 2.0)
    if kind == "rect":
        return dist_point_to_rect(p, shape["min"], shape["max"])
    raise ValueError(f"unknown shape type: {kind!r}")


def shape_blocks_ray(shape, p, q) -> bool:
    """True if `shape` lies across the sight-line p→q (i.e. it occludes q as seen from p).
    Points do not occlude (too small); segments and rects do."""
    kind = shape["type"]
    if kind == "point":
        return False
    if kind == "segment":
        (a, b) = shape["points"]
        return segments_intersect(p, q, a, b)
    if kind == "rect":
        return any(segments_intersect(p, q, e0, e1) for (e0, e1) in _rect_edges(shape["min"], shape["max"]))
    return False


def shape_samples(shape):
    """Representative points ON the shape, for visibility/bearing estimation of extended
    objects. A point yields itself; a segment/rect yields several points along its extent."""
    kind = shape["type"]
    if kind == "point":
        return [tuple(shape["position"])]
    if kind == "segment":
        (a, b) = shape["points"]
        n = 7
        return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1))
                for i in range(n)]
    if kind == "rect":
        mn, mx = shape["min"], shape["max"]
        pts = []
        for (e0, e1) in _rect_edges(mn, mx):
            pts.append(e0)
            pts.append(((e0[0] + e1[0]) / 2.0, (e0[1] + e1[1]) / 2.0))
        pts.append(((mn[0] + mx[0]) / 2.0, (mn[1] + mx[1]) / 2.0))
        return pts
    return []


def shape_centroid(shape):
    kind = shape["type"]
    if kind == "point":
        return tuple(shape["position"])
    if kind == "segment":
        (a, b) = shape["points"]
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    if kind == "rect":
        mn, mx = shape["min"], shape["max"]
        return ((mn[0] + mx[0]) / 2.0, (mn[1] + mx[1]) / 2.0)
    return (0.0, 0.0)


# --------------------------------------------------------------------------- raycasting
def ray_segment_distance(o, d, a, b):
    """Distance from ray origin o (unit direction d) to segment a—b, or None if it misses.
    Used to clip a sight-ray at the first wall it hits."""
    ax, ay = a
    bx, by = b
    ex, ey = bx - ax, by - ay             # segment direction
    # perpendicular to ray direction
    px, py = -d[1], d[0]
    denom = ex * px + ey * py
    if abs(denom) < 1e-12:                 # parallel
        return None
    v1x, v1y = o[0] - ax, o[1] - ay
    u = (v1x * px + v1y * py) / denom      # param along segment [0,1]
    if u < 0.0 or u > 1.0:
        return None
    # distance along the ray
    t = (ex * v1y - ey * v1x) / denom
    if t < 0.0:
        return None
    return t


def ray_shape_distance(o, d, shape):
    """Distance along ray o+d to where it first meets `shape` (an occluder), or None.
    Points don't occlude; segments and rects do."""
    kind = shape["type"]
    if kind == "segment":
        (a, b) = shape["points"]
        return ray_segment_distance(o, d, a, b)
    if kind == "rect":
        best = None
        for (e0, e1) in _rect_edges(shape["min"], shape["max"]):
            t = ray_segment_distance(o, d, e0, e1)
            if t is not None and (best is None or t < best):
                best = t
        return best
    return None


# --------------------------------------------------------------------------- bearings
def relative_bearing(robot_pos, heading_deg, target) -> float:
    """Signed bearing of `target` from the robot, in degrees within (-180, 180].
    Negative = to the LEFT, positive = to the RIGHT (matches turn_right = +θ)."""
    dx = target[0] - robot_pos[0]
    dy = target[1] - robot_pos[1]
    angle_to = math.degrees(math.atan2(dx, dy))     # 0 = +y, matches heading convention
    rel = (angle_to - heading_deg + 180.0) % 360.0 - 180.0
    return rel
