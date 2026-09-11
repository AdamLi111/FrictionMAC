"""
The task list — the layer on top of the sim scenes.

A **scene** (robot_tools/sim/scenes/*.json) is a plain world: rooms, objects, a robot. It has no
notion of what should happen in it. A **task** adds the missing half: a GOAL, held by the
simulated user. Same scene + different goal = a different task, so the two layers are separate
files and this module is the join.

The list itself is data (`task_list.json`) so tasks can be added without touching code.
"""
import json
import os
from dataclasses import dataclass, field

TASK_LIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_list.json")


@dataclass
class Task:
    task_id: str
    name: str
    scene: str                     # bundled scene name or path -> ROBOT_SIM_SCENE
    goal: str                      # the user's intent (only the simulated user sees this)
    category: str = "uncategorized"
    target: str | None = None      # scene object the goal centres on, if any
    max_user_turns: int = 10
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        known = {"task_id", "name", "scene", "goal", "category", "target", "max_user_turns"}
        missing = {"task_id", "scene", "goal"} - d.keys()
        if missing:
            raise ValueError(f"task is missing required field(s) {sorted(missing)}: {d}")
        return cls(
            task_id=d["task_id"],
            name=d.get("name", d["task_id"]),
            scene=d["scene"],
            goal=d["goal"],
            category=d.get("category") or "uncategorized",
            target=d.get("target"),
            max_user_turns=int(d.get("max_user_turns", 10)),
            extra={k: v for k, v in d.items() if k not in known},
        )


def load_tasks(path: str | None = None) -> list[Task]:
    """Every task in the list, in file order."""
    with open(path or TASK_LIST_PATH) as f:
        data = json.load(f)
    return [Task.from_dict(t) for t in data["tasks"]]


def select(tasks: list[Task], *, task_ids=None, categories=None, scenes=None,
           limit: int | None = None) -> list[Task]:
    """Filter the list. Each filter is ANDed; `None` means "don't filter on this"."""
    out = tasks
    if task_ids:
        wanted = set(task_ids)
        out = [t for t in out if t.task_id in wanted]
    if categories:
        wanted = set(categories)
        out = [t for t in out if t.category in wanted]
    if scenes:
        wanted = set(scenes)
        out = [t for t in out if t.scene in wanted]
    return out[:limit] if limit else out


def categories(tasks: list[Task]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.category] = counts.get(t.category, 0) + 1
    return dict(sorted(counts.items()))


def validate(tasks: list[Task]) -> list[str]:
    """Cross-check the task list against the scenes it references: unique ids, loadable scene,
    and a `target` that actually names an object in that scene. Returns a list of problems
    (empty == clean). Used by `run_tasks.py --validate`."""
    from robot_tools.sim import load_scene

    problems, seen = [], set()
    for t in tasks:
        if t.task_id in seen:
            problems.append(f"{t.task_id}: duplicate task_id")
        seen.add(t.task_id)
        try:
            world = load_scene(t.scene)
        except Exception as e:
            problems.append(f"{t.task_id}: scene {t.scene!r} failed to load ({e})")
            continue
        if t.target and t.target not in {o.name for o in world.objects}:
            problems.append(f"{t.task_id}: target {t.target!r} is not an object in scene "
                            f"{t.scene!r}")
    return problems
