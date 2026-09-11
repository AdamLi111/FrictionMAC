"""
Run the task list against the robot system, with a simulated user doing the talking.

One **episode** per task:

    scene ──> sim world (behind the MCP stub, exactly as in an interactive run)
    goal  ──> simulated user (Claude Haiku, omniscient about the scene)

               user utterance ──> Director (the architecture under test)
               robot `speak`  ──> user            [the only channel back]
               (repeat until the user is satisfied, stuck, or out of turns)

The robot side is completely unchanged: same architectures, same steering, same
`mcp__robot__*` tools, same persistent-session turn semantics as `scripts/hw_console.py` —
the simulated user simply takes the keyboard's place. Nothing in the robot's context ever
contains the task goal or the ground-truth scene.

Each episode is isolated: its own scene, its own belief store (`get_world`/`update_world`), its
own tool-call log, and its own live sim-state file — so nothing leaks between tasks.

Usage
-----
    # everything, on the architecture under test
    AGENT_ARCH=v4 .venv-agent/bin/python -m evaluation.run_tasks

    # a slice
    .venv-agent/bin/python -m evaluation.run_tasks --category referential_ambiguity
    .venv-agent/bin/python -m evaluation.run_tasks --task amb_001 --task cross_001
    .venv-agent/bin/python -m evaluation.run_tasks --arch v1 --limit 3 --repeats 2

    # inspect the list without spending tokens
    .venv-agent/bin/python -m evaluation.run_tasks --list
    .venv-agent/bin/python -m evaluation.run_tasks --validate

Outputs (under `data/eval_<arch>_<timestamp>/`)
    run.json                     the whole run: config + every episode
    <task_id>[_rN].json          one episode: dialogue, speech, end reason, final world
    <task_id>[_rN]_story.txt     READ THIS ONE — the agents' thinking as a readable story
    <task_id>[_rN].log           the Director-side transcript at LOG_LEVEL (flat, complete)
    <task_id>[_rN]_tools.jsonl   every robot tool call
    <task_id>[_rN]_sim.json      ground-truth world as the episode left it
    <task_id>[_rN]_beliefs.json  the agents' own world model (only if they wrote to it)
"""
import argparse
import json
import os
import sys
import time
import traceback

import anyio

from agent_runtime import (architectures, config, main as agent_main, narrative,
                           session as sess_mod)
from evaluation import tasks as task_mod
from evaluation.simulated_user import SimulatedUser, SimulatedUserError

DEFAULT_TURN_TIMEOUT_S = 300.0   # per user turn; a safety ceiling for unattended runs
_LEVEL = {"INFO": 0, "DEBUG": 1, "FULL": 2}


class EpisodeLog:
    """Director-side transcript for one episode (same levels as the console's)."""

    def __init__(self, path, level):
        self.threshold = _LEVEL[level]
        self.f = open(path, "a", encoding="utf-8")

    def emit(self, level, text):
        if _LEVEL[level] <= self.threshold:
            self.f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            self.f.flush()

    def close(self):
        self.f.close()


def scene_brief(scene: str, state_path: str) -> str:
    """The omniscient view handed to the user, refreshed from the LIVE world.

    The sim world itself lives in the MCP server process, which mirrors it to `state_path` on
    every move; a snapshot is a superset of the scene schema, so it reloads as a scene. Before
    the robot's first movement the file may not exist yet — then the pristine scene IS the
    current state."""
    from robot_tools.sim import describe_world, load_scene

    path = state_path if os.path.exists(state_path) else scene
    return describe_world(load_scene(path))


def final_world(state_path: str, scene: str) -> dict:
    """Ground truth as the episode left it: pose, collisions, action history."""
    from robot_tools.sim import load_scene

    if os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f)
    return load_scene(scene).snapshot()      # never moved — the start state is the end state


async def run_episode(task, arch, out_dir, *, level="DEBUG", turn_timeout=DEFAULT_TURN_TIMEOUT_S,
                      label=None) -> dict:
    """Drive one task to completion. Returns the episode record (also written to disk)."""
    label = label or task.task_id
    paths = {
        "transcript": os.path.join(out_dir, f"{label}.log"),
        "tools": os.path.join(out_dir, f"{label}_tools.jsonl"),
        "beliefs": os.path.join(out_dir, f"{label}_beliefs.json"),
        "sim_state": os.path.join(out_dir, f"{label}_sim.json"),
        "events": os.path.join(out_dir, f"{label}_events.jsonl"),
        "episode": os.path.join(out_dir, f"{label}.json"),
        "story": os.path.join(out_dir, f"{label}_story.txt"),
    }

    # Per-episode environment. The scene picks the world; the paths keep episodes isolated.
    os.environ["ROBOT_SIM"] = "1"
    os.environ["ROBOT_SIM_SCENE"] = task.scene
    os.environ["SIM_STATE_PATH"] = paths["sim_state"]
    os.environ["AGENT_EVENT_LOG"] = paths["events"]
    os.environ.pop("MISTY_IP", None)                    # sim is offline, like the stub

    logs = EpisodeLog(paths["transcript"], level)
    logs.emit("INFO", f"[episode] task={task.task_id} scene={task.scene} arch={arch.name}")
    logs.emit("INFO", f"[goal (user-side only)] {task.goal}")

    record = {
        "task_id": task.task_id, "label": label, "name": task.name, "scene": task.scene,
        "category": task.category, "target": task.target, "goal": task.goal,
        "arch": arch.name, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dialogue": [],          # [{"turn", "user", "robot_speech", "spoke", "timed_out"}]
        "user_turns": 0, "robot_utterances": 0,
        "end_reason": None, "error": None,
        "paths": {k: os.path.relpath(v, config.REPO) for k, v in paths.items()},
    }

    # The readable companion to the flat transcript: who thought what, nested under the
    # delegation it belongs to. Written as the episode runs, so it can be read live.
    story = narrative.NarrativeLog(paths["story"])
    story.header(f"EPISODE {label} — {task.name}", {
        "scene": task.scene,
        "architecture": f"{arch.name} ({arch.description})",
        "category": task.category,
        "the user's goal (the robot is never told this)": task.goal,
    })

    engine = sess_mod.TurnEngine(logs.emit, lambda text, friction: None,
                                 trace=story.event, max_collect_s=turn_timeout)
    options = agent_main.build_options(paths["tools"], paths["beliefs"], None, arch=arch)
    options.stderr = lambda line: logs.emit("FULL", f"[stderr] {line.rstrip()}")

    user = SimulatedUser(task, scene_brief(task.scene, paths["sim_state"]))
    t0 = time.monotonic()
    try:
        async with sess_mod.robot_session(options) as (client, recv):
            last_reply_at = None
            utterance = await anyio.to_thread.run_sync(user.opening_utterance)
            while True:
                if utterance is None:
                    record["end_reason"] = "user_ended"
                    break
                if record["user_turns"] >= task.max_user_turns:
                    record["end_reason"] = "max_user_turns"
                    break

                record["user_turns"] += 1
                print(f"    you> {utterance}")
                logs.emit("INFO", f"\n===== you: {utterance} =====")
                story.user_turn(record["user_turns"], utterance)
                engine.drain_now(recv)
                turn_t0 = time.monotonic()
                await client.query(sess_mod.stamp_command(utterance, last_reply_at))
                turn = await engine.collect_turn(recv)
                last_reply_at = time.monotonic()

                speech = [s["text"] for s in turn["speech"]]
                for s in speech:
                    print(f"    Misty: {s}")
                if not speech:
                    print("    (no spoken reply)")
                    story.no_speech()
                story.turn_end(subtype=turn["subtype"], seconds=time.monotonic() - turn_t0,
                               timed_out=turn["timed_out"])
                record["robot_utterances"] += len(speech)
                record["dialogue"].append({
                    "turn": record["user_turns"], "user": utterance,
                    "robot_speech": turn["speech"], "spoke": turn["spoke"],
                    "timed_out": turn["timed_out"],
                })
                if turn["timed_out"]:
                    record["end_reason"] = "turn_timeout"
                    break

                utterance = await anyio.to_thread.run_sync(
                    user.reply, speech, scene_brief(task.scene, paths["sim_state"]))
    except SimulatedUserError as e:
        record["end_reason"], record["error"] = "user_error", str(e)
        logs.emit("INFO", f"[abort] {e}")
    except Exception as e:                                  # keep the batch running
        record["end_reason"], record["error"] = "harness_error", f"{type(e).__name__}: {e}"
        logs.emit("INFO", f"[abort] {traceback.format_exc()}")

    record["duration_s"] = round(time.monotonic() - t0, 1)
    record["user_said_done"] = user.done
    record["user_transcript"] = user.transcript
    record["final_world"] = final_world(paths["sim_state"], task.scene)
    record["collisions"] = [c["object"] for c in record["final_world"].get("collisions", [])]
    record["robot_actions"] = record["final_world"].get("action_history", [])
    logs.emit("INFO", f"[episode end] reason={record['end_reason']} "
                      f"turns={record['user_turns']} duration={record['duration_s']}s")
    logs.close()
    story.footer(
        f"EPISODE ENDED — {record['end_reason']} after {record['user_turns']} user turn(s), "
        f"{record['duration_s']}s.\n"
        f"The user considered the goal met: {record['user_said_done']}.\n"
        f"Ground truth — robot ended at "
        f"{record['final_world']['robot']['position']} facing "
        f"{record['final_world']['robot']['heading']}°; "
        f"collisions: {', '.join(record['collisions']) or 'none'}.\n"
        + (f"Error: {record['error']}\n" if record["error"] else ""))
    story.close()
    with open(paths["episode"], "w") as f:
        json.dump(record, f, indent=2)
    return record


async def run(args) -> dict:
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)
    arch = architectures.get(args.arch)

    all_tasks = task_mod.load_tasks(args.task_list)
    selected = task_mod.select(all_tasks, task_ids=args.task, categories=args.category,
                               scenes=args.scene, limit=args.limit)
    if not selected:
        print("[abort] no tasks matched the filters. --list shows what is available.")
        return {}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = config.DATA_DIR / f"eval_{arch.name}_{stamp}"
    out_dir.mkdir(parents=True)

    episodes_planned = len(selected) * args.repeats
    print(f"── eval run ──  arch: {arch.name} ({arch.description})")
    print(f"   tasks: {len(selected)}  |  repeats: {args.repeats}  |  "
          f"episodes: {episodes_planned}  |  out: {out_dir}")
    print()

    run_record = {
        "arch": arch.name, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task_list": os.path.relpath(args.task_list or task_mod.TASK_LIST_PATH, config.REPO),
        "user_model": os.environ.get("SIM_USER_MODEL", "claude-haiku-4-5"),
        "repeats": args.repeats, "turn_timeout_s": args.turn_timeout,
        "log_level": args.log_level, "episodes": [],
    }
    for i, task in enumerate(selected, 1):
        for r in range(1, args.repeats + 1):
            label = task.task_id if args.repeats == 1 else f"{task.task_id}_r{r}"
            n = (i - 1) * args.repeats + r
            print(f"[{n}/{episodes_planned}] {label} — {task.name}  (scene: {task.scene})")
            ep = await run_episode(task, arch, str(out_dir), level=args.log_level,
                                   turn_timeout=args.turn_timeout, label=label)
            run_record["episodes"].append(ep)
            print(f"    → {ep['end_reason']} in {ep['user_turns']} user turn(s), "
                  f"{ep['duration_s']}s"
                  + (f", collisions: {', '.join(ep['collisions'])}" if ep["collisions"] else ""))
            print()
            with open(out_dir / "run.json", "w") as f:      # rewritten after each episode
                json.dump(run_record, f, indent=2)

    reasons: dict[str, int] = {}
    for ep in run_record["episodes"]:
        reasons[ep["end_reason"]] = reasons.get(ep["end_reason"], 0) + 1
    print("── run complete ──")
    print(f"   episodes: {len(run_record['episodes'])}  |  end reasons: {reasons}")
    print(f"   {out_dir}/run.json")
    return run_record


def main():
    p = argparse.ArgumentParser(
        description="Run the task list with a simulated user against the robot system.")
    p.add_argument("--arch", default=None,
                   help=f"architecture under test (default: AGENT_ARCH, else "
                        f"{architectures.DEFAULT_ARCH}). Options: "
                        f"{', '.join(sorted(architectures.available()))}")
    p.add_argument("--task", action="append", help="run only this task_id (repeatable)")
    p.add_argument("--category", action="append", help="run only this category (repeatable)")
    p.add_argument("--scene", action="append", help="run only tasks on this scene (repeatable)")
    p.add_argument("--limit", type=int, default=None, help="cap how many tasks are run")
    p.add_argument("--repeats", type=int, default=1,
                   help="run each task this many times (the interaction is stochastic)")
    p.add_argument("--turn-timeout", type=float, default=DEFAULT_TURN_TIMEOUT_S,
                   help="seconds a single robot turn may take before the episode is cut off")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "DEBUG").upper(),
                   choices=sorted(_LEVEL), help="Director-side transcript verbosity")
    p.add_argument("--task-list", default=None, help="path to an alternative task_list.json")
    p.add_argument("--list", action="store_true", help="print the task list and exit")
    p.add_argument("--validate", action="store_true",
                   help="check the task list against the scenes it references, then exit")
    args = p.parse_args()

    all_tasks = task_mod.load_tasks(args.task_list)
    if args.list:
        print(f"{len(all_tasks)} tasks — {task_mod.categories(all_tasks)}\n")
        for t in all_tasks:
            print(f"  {t.task_id:<12} {t.category:<22} {t.scene:<24} {t.name}")
        return
    if args.validate:
        problems = task_mod.validate(all_tasks)
        for msg in problems:
            print(f"  ✗ {msg}")
        print(f"{len(all_tasks)} tasks, {len(problems)} problem(s).")
        sys.exit(1 if problems else 0)

    anyio.run(lambda: run(args))


if __name__ == "__main__":
    main()
