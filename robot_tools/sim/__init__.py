"""
Simulation backend for the robot tools.

A ground-truth 2-D world (world_model.SimWorld) stands in for the physical Misty when
ROBOT_SIM is active. It sits BEHIND the MCP stub: movement tools mutate the world (with
wall-aware collision), and capture_view returns a synthetic text POV (FOV + occlusion) instead
of a JPEG. The same agents, steering, and MCP tools run unchanged against it.

See scene.py for the scene schema (every entity, walls included, is an object with a `shape`).
"""
from .scene import load_scene
from .world_model import SimWorld

__all__ = ["load_scene", "SimWorld"]
