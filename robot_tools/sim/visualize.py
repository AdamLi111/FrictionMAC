"""
Minimalist top-down SVG visualizer for a sim scene.

Renders a scene as a clean floor-plan you can open in the IDE or a browser: walls as thick dark
segments, tables/boxes as light rects, small objects as labelled dots, and the robot as a marker
with a heading arrow and a translucent ~FOV wedge. Objects currently in view (FOV + occlusion,
per the world model) get a subtle highlight ring, so you can see what the robot perceives from
the start pose. No dependencies — it emits SVG text directly.

    python -m robot_tools.sim.visualize office_kitchen            # -> data/scene_office_kitchen.svg
    python -m robot_tools.sim.visualize path/to/scene.json out.svg
    python -m robot_tools.sim.visualize --live                    # the RUNNING session's live pose
"""
import math
import os
import sys

from .. import config
from . import geometry as geo
from .scene import load_scene

# Restrained, neutral palette (light theme).
_BG = "#ffffff"
_GRID = "#eef2f6"
_WALL = "#334155"
_RECT_FILL = "#e2e8f0"
_RECT_STROKE = "#94a3b8"
_LABEL = "#475569"
_ROBOT = "#f97316"          # single warm accent for the robot
_FOV = "#f97316"
_VISIBLE_RING = "#f59e0b"
_DOT_DEFAULT = "#64748b"

# Named colours → hex, for point objects that carry a properties.color.
_COLOR = {
    "red": "#ef4444", "blue": "#3b82f6", "green": "#22c55e", "black": "#1f2937",
    "white": "#cbd5e1", "yellow": "#eab308", "orange": "#f97316", "purple": "#a855f7",
    "grey": "#6b7280", "gray": "#6b7280",
}


def _bounds(world):
    xs, ys = [world.robot_pos[0]], [world.robot_pos[1]]
    for o in world.objects:
        s = o.shape
        if s["type"] == "point":
            r = s.get("radius", 0.05)
            xs += [s["position"][0] - r, s["position"][0] + r]
            ys += [s["position"][1] - r, s["position"][1] + r]
        elif s["type"] == "segment":
            for (px, py) in s["points"]:
                xs.append(px); ys.append(py)
        elif s["type"] == "rect":
            xs += [s["min"][0], s["max"][0]]
            ys += [s["min"][1], s["max"][1]]
    return min(xs), min(ys), max(xs), max(ys)


def render_svg(world, width=760, margin_m=0.6) -> str:
    minx, miny, maxx, maxy = _bounds(world)
    minx -= margin_m; miny -= margin_m; maxx += margin_m; maxy += margin_m
    world_w = max(maxx - minx, 1e-6)
    world_h = max(maxy - miny, 1e-6)
    pad = 24
    scale = (width - 2 * pad) / world_w
    height = int(world_h * scale + 2 * pad)

    def X(wx):
        return pad + (wx - minx) * scale

    def Y(wy):                       # flip: world +y (north) is up on screen
        return pad + (maxy - wy) * scale

    visible = {v["name"] for v in world.visible_objects()}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif, system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{_BG}"/>',
    ]

    # 1 m grid
    gx = math.floor(minx)
    while gx <= maxx:
        parts.append(f'<line x1="{X(gx):.1f}" y1="{pad}" x2="{X(gx):.1f}" y2="{height-pad}" '
                     f'stroke="{_GRID}" stroke-width="1"/>')
        gx += 1
    gy = math.floor(miny)
    while gy <= maxy:
        parts.append(f'<line x1="{pad}" y1="{Y(gy):.1f}" x2="{width-pad}" y2="{Y(gy):.1f}" '
                     f'stroke="{_GRID}" stroke-width="1"/>')
        gy += 1

    # rects first (furniture), then walls, then points on top
    for o in world.objects:
        if o.shape["type"] != "rect":
            continue
        mn, mx = o.shape["min"], o.shape["max"]
        x, y = X(mn[0]), Y(mx[1])
        w = (mx[0] - mn[0]) * scale
        h = (mx[1] - mn[1]) * scale
        ring = f'stroke="{_VISIBLE_RING}" stroke-width="2.5"' if o.name in visible else \
               f'stroke="{_RECT_STROKE}" stroke-width="1.5"'
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="2" '
                     f'fill="{_RECT_FILL}" {ring}/>')
        parts.append(_label(o.name, X((mn[0]+mx[0])/2), Y((mn[1]+mx[1])/2), anchor="middle"))

    for o in world.objects:
        if o.shape["type"] != "segment":
            continue
        (a, b) = o.shape["points"]
        tw = max(2.0, o.shape.get("thickness", 0.1) * scale)
        parts.append(f'<line x1="{X(a[0]):.1f}" y1="{Y(a[1]):.1f}" x2="{X(b[0]):.1f}" '
                     f'y2="{Y(b[1]):.1f}" stroke="{_WALL}" stroke-width="{tw:.1f}" '
                     f'stroke-linecap="round"/>')

    for o in world.objects:
        if o.shape["type"] != "point":
            continue
        cx, cy = X(o.shape["position"][0]), Y(o.shape["position"][1])
        color = _COLOR.get((o.properties.get("color") or "").lower(), _DOT_DEFAULT)
        r = max(4.0, o.shape.get("radius", 0.05) * scale)
        if o.name in visible:
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r+4:.1f}" fill="none" '
                         f'stroke="{_VISIBLE_RING}" stroke-width="2"/>')
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{color}" '
                     f'stroke="#ffffff" stroke-width="1"/>')
        parts.append(_label(o.name, cx + r + 4, cy + 3, anchor="start"))

    # robot: FOV wedge, heading arrow, body
    parts += _robot(world, X, Y, scale)

    # scale bar (1 m) + caption
    bar = 1.0 * scale
    by = height - 12
    parts.append(f'<line x1="{pad}" y1="{by}" x2="{pad+bar:.1f}" y2="{by}" stroke="{_LABEL}" '
                 f'stroke-width="2"/>')
    parts.append(f'<text x="{pad+bar+6:.1f}" y="{by+4}" font-size="11" fill="{_LABEL}">1 m</text>')
    parts.append(f'<text x="{pad}" y="16" font-size="13" fill="{_LABEL}" font-weight="600">'
                 f'{_esc(world.name)}</text>')
    parts.append('</svg>')
    return "\n".join(parts)


def _robot(world, X, Y, scale):
    px, py = world.robot_pos
    hx = math.sin(math.radians(world.heading))     # forward unit (world), 0°=+y
    hy = math.cos(math.radians(world.heading))
    half = world.fov / 2.0
    out = []
    # FOV: raycast the cone and clip each ray at the first wall, capped at max_view_distance,
    # so the shaded region is the ACTUAL visible area (walls cut it off, like the robot's view).
    pts = [(X(px), Y(py))]
    steps = max(8, int(world.fov / 1.5))
    for i in range(steps + 1):
        ang = world.heading - half + (world.fov * i / steps)
        dx, dy = math.sin(math.radians(ang)), math.cos(math.radians(ang))
        hit = world.max_view
        for o in world.objects:
            t = geo.ray_shape_distance((px, py), (dx, dy), o.shape)
            if t is not None and t < hit:
                hit = t
        pts.append((X(px + dx * hit), Y(py + dy * hit)))
    d = " ".join(f"{'M' if k == 0 else 'L'} {x:.1f} {y:.1f}" for k, (x, y) in enumerate(pts))
    out.append(f'<path d="{d} Z" fill="{_FOV}" fill-opacity="0.12"/>')
    # heading arrow
    ax, ay = X(px + hx * 0.7), Y(py + hy * 0.7)
    out.append(f'<line x1="{X(px):.1f}" y1="{Y(py):.1f}" x2="{ax:.1f}" y2="{ay:.1f}" '
               f'stroke="{_ROBOT}" stroke-width="2.5"/>')
    # body
    out.append(f'<circle cx="{X(px):.1f}" cy="{Y(py):.1f}" r="7" fill="{_ROBOT}" '
               f'stroke="#ffffff" stroke-width="1.5"/>')
    out.append(_label("robot", X(px) + 10, Y(py) - 8, anchor="start", color=_ROBOT, weight="600"))
    return out


def _label(text, x, y, anchor="start", color=_LABEL, weight="400"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="11" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{_esc(text)}</text>')


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m robot_tools.sim.visualize <scene|path|--live> [out.svg]")
        return 2
    if argv[0] == "--live":                       # render the running session's current pose
        state = config.sim_state_path()
        if not os.path.isfile(state):
            print(f"no live sim state at {state} — is a sim session running?")
            return 1
        argv = [state] + argv[1:]
    world = load_scene(argv[0])
    if len(argv) > 1:
        out = argv[1]
    else:
        base = os.path.splitext(os.path.basename(argv[0]))[0]
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "data")
        os.makedirs(data_dir, exist_ok=True)
        out = os.path.join(data_dir, f"scene_{base}.svg")
    with open(out, "w") as f:
        f.write(render_svg(world))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
