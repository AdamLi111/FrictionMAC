"""
Scene loading for the simulation.

A scene is a JSON file describing a room (or set of rooms) in the unified object schema — every
entity, walls included, is an object with a `shape`. Rooms are authored as individual wall
`segment`s; a doorway is simply a gap between two segments.

Schema:
{
  "name": "office_hallway",
  "robot": {"position": [0.0, 0.0], "heading": 0},
  "fov_degrees": 45,                 # optional, defaults to the narrow Misty FOV
  "max_view_distance": 8.0,          # optional
  "objects": [
    {"name": "red cup", "shape": {"type": "point", "position": [0.3, 2.0], "radius": 0.05},
     "room": "office", "properties": {"color": "red"}},
    {"name": "wall", "shape": {"type": "segment", "points": [[-2, 3], [2, 3]], "thickness": 0.1},
     "room": "office"}
  ]
}
"""
import json
import os

from .world_model import SimWorld

SCENES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes")


def load_scene(path_or_name: str) -> SimWorld:
    """Load a scene by file path, or by bare name from the bundled scenes/ directory."""
    path = path_or_name
    if not os.path.isfile(path):
        cand = os.path.join(SCENES_DIR, path_or_name)
        if not cand.endswith(".json"):
            cand += ".json"
        if os.path.isfile(cand):
            path = cand
        else:
            raise FileNotFoundError(
                f"sim scene not found: {path_or_name!r} (looked at {path_or_name} and {cand})")
    with open(path, "r") as f:
        scene = json.load(f)
    return SimWorld(scene)
