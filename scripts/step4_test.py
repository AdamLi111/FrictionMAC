"""
Step 3+4 checkpoint tests (real API, robot stubbed). Run one part per invocation:

    python -m scripts.step4_test sanity   # Step 3: "move forward 1 meter" -> Action moves
    python -m scripts.step4_test on       # Step 4: ambiguous scene -> friction FIRES
    python -m scripts.step4_test off       # Step 4: FRICTION_OFF=1 -> ZERO clarifications

Each part clears its own logs, runs the Director, then asserts against the JSONL the STUBBED
MCP server wrote (the measurement boundary).
"""
import json
import os
import sys
from pathlib import Path

import anyio

from agent_runtime import config, main

SCENE = str(config.REPO / "tests" / "fixtures" / "scene_two_mugs")


def _fresh(path: Path):
    if path.exists():
        path.unlink()


def _tool_names(log_path: Path):
    if not log_path.exists():
        return []
    return [json.loads(l)["name"] for l in log_path.read_text().splitlines() if l.strip()]


def _run(prompt, tag, scene=None):
    tool_log = config.DATA_DIR / f"step4_{tag}.jsonl"
    world = config.DATA_DIR / f"step4_{tag}_world.json"
    events = config.DATA_DIR / f"step4_{tag}_events.jsonl"
    config.DATA_DIR.mkdir(exist_ok=True)
    for p in (tool_log, world, events):
        _fresh(p)
    os.environ["AGENT_EVENT_LOG"] = str(events)

    print(f"\n=== RUN [{tag}] prompt={prompt!r} scene={'yes' if scene else 'no'} "
          f"FRICTION_OFF={os.environ.get('FRICTION_OFF')} ===")
    anyio.run(lambda: main.run(prompt, tool_log=tool_log, world_state=world, scene=scene))

    names = _tool_names(tool_log)
    print(f"[server JSONL tool calls] {names}")
    return names


def part_sanity():
    names = _run("Move forward 1 meter.", "sanity")
    ok = "move_forward" in names and "ask_clarification" not in names
    print("SANITY:", "PASS" if ok else "FAIL",
          "(expected move_forward, no clarification)")
    return ok


def part_on():
    os.environ.pop("FRICTION_OFF", None)
    names = _run("Go to the mug.", "on", scene=SCENE)
    n_clar = names.count("ask_clarification")
    ok = n_clar >= 1
    print("FRICTION-ON:", "PASS" if ok else "FAIL",
          f"(ask_clarification count = {n_clar}, expected >= 1)")
    return ok


def part_off():
    os.environ["FRICTION_OFF"] = "1"
    names = _run("Go to the mug.", "off", scene=SCENE)
    n_clar = names.count("ask_clarification")
    ok = n_clar == 0
    print("FRICTION-OFF:", "PASS" if ok else "FAIL",
          f"(ask_clarification count = {n_clar}, expected 0 — gate works)")
    return ok


PARTS = {"sanity": part_sanity, "on": part_on, "off": part_off}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "on"
    ok = PARTS[which]()
    sys.exit(0 if ok else 1)
