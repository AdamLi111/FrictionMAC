"""
Scoring an episode: did it work, and was the robot's world model true?

Two independent things get measured, and they are deliberately kept apart:

**Outcome.** There is no oracle for "the interaction went well", so three signals are recorded
side by side rather than collapsed into one number:
  - `user_judged_success` — the simulated user held the goal and decided it was met. The closest
    thing to a human judgment, and the headline number.
  - `reached_target` — ground truth: the robot physically ended up at the task's target object
    (or collided with it, which in this sim means arrived — every object is solid).
  - `end_reason` — how the episode stopped, which separates "gave up" from "ran out of turns"
    from "the harness broke".

**World-model accuracy.** The agents keep their own beliefs via `update_world`, free-form dicts
that look like:

    "red cup": {"room": "office", "spatial": "on table, nearby",
                "direction_last_seen": "10° left-front", "notes": "..."}

Those claims are checkable against the sim, which knows the truth. Three things are scored:
  - *grounding* — does the recorded name correspond to a real object in the scene, or did the
    agent invent an entry (`far_chair`) that exists nowhere?
  - *room* — does the recorded room match the object's actual room?
  - *direction* — a bearing like "10° to the left" is only meaningful relative to where the
    robot was standing when it was written, so it is compared against the true bearing from the
    pose recorded on the `update_world` call itself (`pose`, logged by the tool layer).
Plus *recall*: of the objects the camera actually showed the robot (`visible`, also logged by the
tool layer), how many made it into the world model at all.

Every metric degrades to `None`/"unknown" rather than guessing when the evidence isn't there —
a belief with no direction is not a wrong direction.
"""
import json
import math
import re

REACH_TOLERANCE_M = 0.75      # robot centre within this of the target's surface = "at" it
BEARING_TOLERANCE_DEG = 35.0  # a recorded direction this close to truth counts as right


# --------------------------------------------------------------------------- text → geometry
_SIDE_WORDS = (
    (r"\bdirectly ahead\b|\bstraight ahead\b|\bin front\b|\bahead of me\b", 0.0),
    (r"\bbehind\b", 180.0),
)


def parse_bearing(text: str) -> float | None:
    """A signed bearing (deg, −left/+right) from the way agents actually write directions.

    Handles "10° left-front", "20° to the right", "directly ahead", "spanning 20° left to 20°
    right" (→ the midpoint), and a bare "on my left" (→ a nominal 45°). Returns None when the
    text carries no direction at all, so "unstated" never scores as "wrong"."""
    if not isinstance(text, str) or not text.strip():
        return None
    low = text.lower()

    # "spanning 20° left to 20° right" — an extended object; score its midpoint.
    spans = re.findall(r"(\d+(?:\.\d+)?)\s*(?:°|deg\w*)?\s*(?:to\s+(?:my|your|the)\s+)?"
                       r"(left|right)", low)
    if len(spans) >= 2:
        vals = [(-1 if side == "left" else 1) * float(num) for num, side in spans]
        return sum(vals) / len(vals)
    if len(spans) == 1:
        num, side = spans[0]
        return (-1 if side == "left" else 1) * float(num)

    for pattern, value in _SIDE_WORDS:
        if re.search(pattern, low):
            return value
    if re.search(r"\bleft\b", low):
        return -45.0
    if re.search(r"\bright\b", low):
        return 45.0
    return None


def _direction_text(info: dict) -> str:
    """Whatever the agent wrote that could carry a direction (keys are its own invention)."""
    parts = [str(v) for k, v in info.items()
             if isinstance(v, str) and ("direction" in k.lower() or "bearing" in k.lower()
                                        or "spatial" in k.lower() or "where" in k.lower())]
    return " ; ".join(parts)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", str(name).lower().replace("_", " ")).strip()


def _match_objects(name: str, scene_objects) -> list:
    """Scene objects a recorded name refers to: exact first, else a containment match, so
    "red cup" hits `red cup` and "cup on table" still hits `red cup`/`blue cup`."""
    want = _norm(name)
    exact = [o for o in scene_objects if _norm(o.name) == want]
    if exact:
        return exact
    return [o for o in scene_objects
            if want and (_norm(o.name) in want or want in _norm(o.name))]


# ------------------------------------------------------------------------------- the scoring
def _tool_rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def world_model_accuracy(beliefs: dict, world, tool_rows: list) -> dict:
    """Score the agents' recorded beliefs against the sim's ground truth."""
    objects = [o for o in world.objects if o.name != "wall"]

    # Where the robot stood at each update_world call, so a direction can be checked against
    # the geometry as it was at that moment (the tool layer logs `pose` next to the call).
    pose_of_update = {}
    for row in tool_rows:
        if row.get("name") == "update_world" and row.get("pose"):
            pose_of_update[str((row.get("args") or {}).get("object", ""))] = row["pose"]

    entries, per_entry = [], []
    for name, info in (beliefs or {}).items():
        info = info if isinstance(info, dict) else {"value": info}
        matches = _match_objects(name, objects)
        entry = {"recorded": name, "grounded": bool(matches),
                 "matched_object": matches[0].name if matches else None,
                 # A name the scene doesn't actually use — `far_chair` for a `chair`. It refers
                 # to something real (so it is grounded), but the label is the agent's
                 # invention, which matters when beliefs are meant to be a shared vocabulary.
                 "exact_name": any(_norm(o.name) == _norm(name) for o in matches),
                 "room": "unknown", "direction": "unknown"}

        if matches:
            # Room: correct if it matches ANY instance of that name — with two identically
            # named objects (a plant per room) the belief store cannot say which one it means.
            claimed = str(info.get("room", "")).strip().lower()
            if claimed and claimed not in ("unknown", "unsure", "n/a", "none", ""):
                truth = {str(o.room).lower() for o in matches if o.room}
                entry["room"] = "correct" if claimed in truth else "wrong"
                entry["room_claimed"], entry["room_truth"] = claimed, sorted(truth)

            claimed_bearing = parse_bearing(_direction_text(info))
            pose = pose_of_update.get(name)
            if claimed_bearing is not None and pose:
                from robot_tools.sim import geometry as geo
                # Nearest instance to the claim — the fairest reading of an ambiguous name.
                truths = [geo.relative_bearing((pose[0], pose[1]), pose[2], o.centroid())
                          for o in matches]
                best = min(truths, key=lambda t: abs(t - claimed_bearing))
                entry.update({
                    "direction": ("correct" if abs(best - claimed_bearing)
                                  <= BEARING_TOLERANCE_DEG else "wrong"),
                    "direction_claimed": round(claimed_bearing, 1),
                    "direction_truth": round(best, 1),
                    "direction_error": round(abs(best - claimed_bearing), 1),
                })
            elif claimed_bearing is not None:
                entry["direction"] = "unscorable"   # no pose logged for this update
        per_entry.append(entry)
        entries.append(name)

    # Recall: of what the camera actually showed, how much was written down?
    seen = {v["name"] for row in tool_rows for v in (row.get("visible") or [])
            if v.get("name") != "wall"}
    recorded_names = {e["matched_object"] for e in per_entry if e["matched_object"]}
    missed = sorted(seen - recorded_names)

    def count(field, value):
        return sum(1 for e in per_entry if e.get(field) == value)

    room_scored = count("room", "correct") + count("room", "wrong")
    dir_scored = count("direction", "correct") + count("direction", "wrong")
    return {
        "entries": len(per_entry),
        "grounded": sum(1 for e in per_entry if e["grounded"]),
        "ungrounded": [e["recorded"] for e in per_entry if not e["grounded"]],
        "invented_names": [e["recorded"] for e in per_entry
                           if e["grounded"] and not e["exact_name"]],
        "room_correct": count("room", "correct"),
        "room_wrong": count("room", "wrong"),
        "room_unstated": count("room", "unknown"),
        "room_accuracy": (count("room", "correct") / room_scored) if room_scored else None,
        "direction_correct": count("direction", "correct"),
        "direction_wrong": count("direction", "wrong"),
        "direction_unstated": count("direction", "unknown"),
        "direction_accuracy": (count("direction", "correct") / dir_scored) if dir_scored else None,
        "mean_direction_error_deg": (
            round(sum(e["direction_error"] for e in per_entry if "direction_error" in e)
                  / max(1, sum(1 for e in per_entry if "direction_error" in e)), 1)
            if any("direction_error" in e for e in per_entry) else None),
        "objects_seen": len(seen),
        "objects_seen_but_unrecorded": missed,
        "recall": (len(seen & recorded_names) / len(seen)) if seen else None,
        "per_entry": per_entry,
    }


def score_episode(record: dict, task, *, beliefs_path=None, tools_path=None) -> dict:
    """Outcome signals + world-model accuracy for one finished episode."""
    from robot_tools.sim import load_scene
    from robot_tools.sim import geometry as geo
    from robot_tools.sim.world_model import SimWorld

    world = SimWorld(record["final_world"]) if record.get("final_world") \
        else load_scene(task.scene)
    rows = _tool_rows(tools_path) if tools_path else []

    reached = None
    if task.target:
        targets = [o for o in world.objects if o.name == task.target]
        if targets:
            pos = (world.robot_pos[0], world.robot_pos[1])
            gap = min(geo.shape_clearance(o.shape, pos) for o in targets)
            reached = bool(gap <= REACH_TOLERANCE_M or task.target in record.get("collisions", []))

    beliefs = {}
    if beliefs_path:
        try:
            with open(beliefs_path, encoding="utf-8") as f:
                beliefs = json.load(f)
        except (OSError, ValueError):
            beliefs = {}

    return {
        "user_judged_success": bool(record.get("user_said_done")),
        "reached_target": reached,
        "target_distance_m": (round(min(geo.shape_clearance(o.shape,
                                        (world.robot_pos[0], world.robot_pos[1]))
                                        for o in world.objects if o.name == task.target), 2)
                              if task.target and any(o.name == task.target for o in world.objects)
                              else None),
        "collisions": len(record.get("collisions", [])),
        "spoke_at_all": record.get("robot_utterances", 0) > 0,
        "silent_turns": sum(1 for t in record.get("dialogue", []) if not t.get("spoke")),
        "world_model": world_model_accuracy(beliefs, world, rows),
    }


# ------------------------------------------------------------------------------ run rollup
def summarise(episodes: list[dict]) -> dict:
    """Aggregate a whole run: success rate, why episodes ended, timing, world-model accuracy."""
    n = len(episodes)
    if not n:
        return {"episodes": 0}

    def mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    scored = [e.get("score") or {} for e in episodes]
    reasons, by_category = {}, {}
    for e, s in zip(episodes, scored):
        reasons[e.get("end_reason") or "unknown"] = reasons.get(e.get("end_reason") or "unknown", 0) + 1
        cat = by_category.setdefault(e.get("category", "?"),
                                     {"episodes": 0, "user_judged_success": 0, "reached": 0,
                                      "reach_scored": 0})
        cat["episodes"] += 1
        cat["user_judged_success"] += bool(s.get("user_judged_success"))
        if s.get("reached_target") is not None:
            cat["reach_scored"] += 1
            cat["reached"] += bool(s.get("reached_target"))

    reach_scored = [s for s in scored if s.get("reached_target") is not None]
    wm = [s.get("world_model") or {} for s in scored]
    return {
        "episodes": n,
        "user_judged_success": sum(bool(s.get("user_judged_success")) for s in scored),
        "user_judged_success_rate": round(
            sum(bool(s.get("user_judged_success")) for s in scored) / n, 3),
        "reached_target": sum(bool(s.get("reached_target")) for s in reach_scored),
        "reached_target_scored": len(reach_scored),
        "reached_target_rate": (round(sum(bool(s.get("reached_target"))
                                          for s in reach_scored) / len(reach_scored), 3)
                                if reach_scored else None),
        "end_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "silent_episodes": sum(1 for s in scored if not s.get("spoke_at_all")),
        "mean_duration_s": mean([e.get("duration_s") for e in episodes]),
        "mean_user_turns": mean([e.get("user_turns") for e in episodes]),
        "mean_collisions": mean([s.get("collisions") for s in scored]),
        "world_model": {
            "entries_total": sum(w.get("entries", 0) for w in wm),
            "ungrounded_total": sum(len(w.get("ungrounded") or []) for w in wm),
            "invented_names_total": sum(len(w.get("invented_names") or []) for w in wm),
            "room_accuracy": mean([w.get("room_accuracy") for w in wm]),
            "direction_accuracy": mean([w.get("direction_accuracy") for w in wm]),
            "mean_direction_error_deg": mean([w.get("mean_direction_error_deg") for w in wm]),
            "recall": mean([w.get("recall") for w in wm]),
        },
        "by_category": by_category,
    }


def format_summary(summary: dict, *, arch: str = "", title: str = "RUN SUMMARY") -> str:
    """The same rollup as text, for the console and `summary.txt`."""
    if not summary.get("episodes"):
        return "no episodes"
    w = summary["world_model"]

    def pct(v):
        return "n/a" if v is None else f"{v * 100:.0f}%"

    lines = [
        "=" * 78,
        f"  {title}{f'  —  architecture {arch}' if arch else ''}",
        "=" * 78,
        f"  episodes                 {summary['episodes']}",
        f"  user-judged success      {summary['user_judged_success']}/{summary['episodes']}"
        f"  ({pct(summary['user_judged_success_rate'])})   ← the simulated user's own verdict",
        f"  reached target (truth)   {summary['reached_target']}/"
        f"{summary['reached_target_scored']}  ({pct(summary['reached_target_rate'])})"
        f"   ← ground-truth pose, tasks with a target only",
        f"  episodes with no speech  {summary['silent_episodes']}",
        "",
        f"  mean duration            {summary['mean_duration_s']}s per episode",
        f"  mean user turns          {summary['mean_user_turns']}",
        f"  mean collisions          {summary['mean_collisions']}",
        "",
        "  how episodes ended:",
    ]
    for reason, count in summary["end_reasons"].items():
        lines.append(f"      {count:>3}  {reason}")
    lines += [
        "",
        "  world model (the agents' own beliefs vs. ground truth):",
        f"      entries recorded     {w['entries_total']}"
        f"   (referring to nothing real: {w['ungrounded_total']};"
        f" made-up names for real things: {w['invented_names_total']})",
        f"      room correct         {pct(w['room_accuracy'])}",
        f"      direction correct    {pct(w['direction_accuracy'])}"
        f"   (mean error {w['mean_direction_error_deg']}°)",
        f"      recall of what it saw {pct(w['recall'])}",
        "",
        "  by category:",
    ]
    for cat, c in sorted(summary["by_category"].items()):
        reach = f"{c['reached']}/{c['reach_scored']}" if c["reach_scored"] else "  -  "
        lines.append(f"      {cat:<24} {c['user_judged_success']}/{c['episodes']} user-judged"
                     f"   {reach} reached")
    lines.append("=" * 78)
    return "\n".join(lines)
