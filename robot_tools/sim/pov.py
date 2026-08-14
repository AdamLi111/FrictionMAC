"""
Synthetic text POV — the sim's stand-in for a camera frame.

capture_view() returns this text (there is no rendered image in sim). It describes only what
the robot can actually see: objects inside the narrow FOV cone that aren't hidden behind a wall,
with a coarse bearing and nearby-object context — deliberately shaped like the sort of thing a
VLM would report from a real frame, so the agents reason over it the same way. Distance is given
coarsely (the real steering tells perceivers not to trust precise distance).
"""


def _bearing_phrase(bearing: float, bmin: float, bmax: float, extended: bool) -> str:
    """Turn a signed bearing (deg; -left / +right) into natural wording."""
    def one(b):
        a = abs(b)
        if a < 6:
            return "directly ahead"
        side = "left" if b < 0 else "right"
        return f"~{round(a / 5) * 5}° to your {side}"
    if extended and (bmax - bmin) > 12:
        return f"spanning {one(bmin)} to {one(bmax)}"
    return one(bearing)


def _distance_phrase(dist: float) -> str:
    if dist < 0.8:
        return "very close"
    if dist < 2.0:
        return "nearby"
    if dist < 4.0:
        return "a short distance ahead"
    return "far off"


def describe_view(world) -> str:
    """One text POV of what's directly ahead within the ~FOV cone (occlusion respected)."""
    fov = round(world.fov)
    visible = world.visible_objects()
    if not visible:
        return (f"Directly ahead (~{fov}° FOV): nothing recognisable in view — the way ahead "
                f"looks clear/empty. Turn to look in another direction.")

    lines = [f"Directly ahead (~{fov}° FOV): you can see {len(visible)} thing"
             f"{'s' if len(visible) > 1 else ''} in this narrow view —"]
    for o in visible:
        where = _bearing_phrase(o["bearing"], o["bearing_min"], o["bearing_max"], o["extended"])
        dist = _distance_phrase(o["distance"])
        extra = []
        color = o["properties"].get("color")
        if color:
            extra.append(color)
        typ = o["properties"].get("type")
        if typ and typ != o["name"]:
            extra.append(typ)
        tag = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"  - {o['name']}{tag}: {where}, {dist}.")
    return "\n".join(lines)
