# ponder_multi_agent

A multi-agent robot system for the Misty II, built on the **Claude Agent SDK**. Two layers,
two Python environments, talking over MCP:

- **`robot_tools/`** — a standalone **MCP server** (16 tools) wrapping Misty's real
  capabilities: movement, expression, speech, vision, and persistent world memory. Runs in
  the robot env (`.venv`).
- **`agent_runtime/`** — a **Director** agent that delegates to six domain-expert subagents,
  connected to the robot MCP server. Runs in the agent env (`.venv-agent`, Python ≥3.10, has
  `claude-agent-sdk`). Ships **four selectable architectures** (see below); pick one with
  `AGENT_ARCH`.

On top of those sits **`evaluation/`** — a task list and a simulated user, for running the whole
system at scale against the offline simulation instead of by hand. See
[Experiments](#experiments-the-task-list--a-simulated-user).

Robot access has two modes, chosen by `MISTY_IP`: **real** (fails loudly if unreachable) or
**stub** (`ROBOT_STUB=1`, or simply no `MISTY_IP` — fakes all robot calls for offline dev).

## The 16 robot tools (`mcp__robot__*`)

| Group | Tools |
|---|---|
| Movement (DRIVE) | `move_forward`, `move_backward`, `turn_left`, `turn_right`, `stop` |
| Expression (ARM_LEFT/ARM_RIGHT/HEAD/FACE/LED) | `move_arm`, `move_head`, `display_image`, `change_led`, `reset_pose` |
| Speech | `speak(text, friction_type)` |
| Vision | `capture_view`, `get_last_view` |
| World memory | `get_known_location`, `update_world`, `get_world` |

- **Concurrency + locks:** server tool calls are offloaded to threads, and each physical
  motor resource has its own lock — calls on the *same* motor serialize (e.g. a 360° scan vs.
  driving), calls on *different* motors run concurrently. The two arms are independent locks.
- **Vision** returns real image content the agent (a VLM) reasons over; no VLM runs in the
  server. `capture_view` shoots one frame of what's directly ahead (narrow ~45° FOV — to see
  elsewhere, turn first); `get_last_view` re-serves the last capture's frames with **no new
  shot and no motor**. In **sim** mode both return a synthetic text POV instead of an image.
- **Tool descriptions are the agent-facing contract.** An agent holding a tool has its
  description + JSON Schema resident on every inference and never sees `tools.py`, so ranges,
  units, sign conventions and valid values live in [`server.py`](robot_tools/server.py) —
  interpolated from the clamp constants in [`tools.py`](robot_tools/tools.py) so a stated limit
  can't drift from the enforced one. Steering files must not restate them.
- **Speech** is one tool; `friction_type` is required (`none` or one of the five positive-
  friction types) and is the logged record of friction applied.
- Every tool call logs one JSONL line `{timestamp, name, args, result}`; image results are
  logged **redacted** (frame count/size, not raw base64).

## The agents (Director + 6 experts, 3 clusters)

- **World-Understanding:** `object-lookup` (the sole perceiver — camera + memory) and `map`
  (owns the world model: records what was seen from the cached image, keeps it consistent,
  and judges ambiguity via `get_world`).
- **Action-Space:** `navigation` (movement primitives) and `expression` (arm/head/face/LED).
- **Dialogue-Management:** `regular-utterance` (normal speech) and `friction` (positive-
  friction utterances). Both *propose* wording; the Director approves, then a fresh
  delegation speaks it.

Behavior lives in the steering files under [`agent_runtime/steering/`](agent_runtime/steering/),
not in code.

## Architectures (run options)

The multi-agent **topology** is a selectable "run option", chosen with the `AGENT_ARCH` env var
(default `v1`) and honored by both entry points. Each variant is an `Architecture` subclass in
[`agent_runtime/architectures/`](agent_runtime/architectures/) registered in that package's
`__init__.py`; the message-collection harness is architecture-agnostic, so adding a variant
touches nothing else. New variants (V3, …) drop in as another subclass + registry entry.

| `AGENT_ARCH` | Topology | Notes |
|---|---|---|
| `v1` (default) | **Flat:** Director → 6 experts (3 clusters) | Original design. Only experts actuate; speech is gated propose → approve → speak. |
| `v2` | **Managers:** Director → 3 Domain Managers → 6 experts, mutually addressable | A mid-layer of Domain Managers (World-Understanding / Action-Space / Dialogue) parses and re-delegates. Every agent is spawned with a `name`, which makes it **addressable by `SendMessage` and resumable** — messaging an idle agent reloads its transcript, so a manager keeps context across tasks. Coordination flows sideways (manager↔manager) on top of the `Agent` hierarchy that carries tasks down. Managers coordinate only (no robot tools); actuation stays at the experts. **No approval gate.** The two perceivers (`object-lookup`, `map`) are spawned **fresh per task** (never resumed) so camera frames don't accumulate. |
| `v3` | **Single agent:** one agent holds **all** the tools and talks to the user directly | No delegation, no subagents — one agent perceives, reasons, moves, expresses, keeps the world model, and speaks itself. The baseline the multi-agent variants are compared against. |
| `v4` | **Flat, no approval:** Director → 6 experts (3 clusters) | Same topology as `v1`, but **every approval gate removed** — dialogue agents speak directly and navigation plans-and-drives directly (no propose → approve step). Isolates the effect of the approval gate against `v1`. |

- **These are named subagents, not a Claude Code "agent team".** Agent teams require an
  interactive session and are never formed from an Agent SDK session, so no team config, shared
  task list or mailbox is created (`~/.claude/teams` and `~/.claude/tasks` stay absent).
- **v2 still needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`**, which the architecture sets on the
  CLI subprocess automatically, for one specific reason: **cross-agent name resolution**. With the
  flag off, an agent messaging another agent it did not itself spawn fails with
  `No agent named '<x>' is reachable`, and only raw agentIds work — so manager↔manager traffic
  breaks while Director→manager keeps working. Reproduce both directions with
  [`scripts/team_probe.py`](scripts/team_probe.py) (`TEAMS_FLAG=0` fails, `TEAMS_FLAG=1` passes).
  Verified on **CLI 2.1.211 + SDK 0.2.120**.
- v2 raises the top-level turn budget and the subagent spawn depth
  (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=4`) for the extra tier.
- The one hop that is **not** a `SendMessage` is manager → Director escalation: a manager that
  needs the user consulted finishes its task with `STATUS: NEED_USER_INFO: <question>`, which the
  Director resolves (via the dialogue-manager → friction → user), then re-delegates — the same
  reliable return-and-continue channel V1 uses.

## Setup (from a fresh clone)

Four things, and **three of them are not Python** — the most common failure is stopping after
the venvs and getting `Claude Code not found`.

```bash
# 1. The Claude Code CLI. The Agent SDK does not contain the agent runtime — it shells out to
#    this binary, so nothing works without it. Needs Node 18+.
npm install -g @anthropic-ai/claude-code
claude --version          # must print a version; if not, it isn't on PATH

# 2. The two Python environments (they are deliberately separate — see the top of this file).
python -m venv .venv        && .venv/bin/pip install -e ".[dev]"      # robot env (MCP tools)
python -m venv .venv-agent  && .venv-agent/bin/pip install claude-agent-sdk anthropic

# 3. Credentials. `.env` is gitignored, so a fresh clone has none — create it:
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# 4. Check it end to end without a robot or a single API token:
.venv/bin/python -m pytest                                   # robot layer + harness
.venv-agent/bin/python -m evaluation.run_tasks --validate    # task list vs. scenes
```

On `anthropic` (step 2): the robot agents reach Claude through the CLI, but the simulated user in
[`evaluation/`](evaluation/) calls the Messages API directly, so the eval harness needs that
package too. On credentials (step 3): the CLI can also carry its own login (`claude` then
`/login`), which covers the robot side — but the simulated user still needs `ANTHROPIC_API_KEY`
in `.env` or an `ant auth login` profile.

## Run

```bash
# interactive console (real conversation with Misty; type commands, see what it says):
# Real robot at 172.20.10.2 is the DEFAULT — no MISTY_IP needed. Add --stub for offline.
.venv-agent/bin/python -m scripts.hw_console
.venv-agent/bin/python -m scripts.hw_console --stub    # stub (no robot, no movement)

# voice input (real robot only): say "hey misty" + your command; say "quit" to end.
# NOTE: pin websocket-client==0.57.0 — the vendored mistyPy events use its old callback API.
.venv-agent/bin/pip install SpeechRecognition "websocket-client==0.57.0" requests sounddevice numpy  # one-time
VOICE=1 .venv-agent/bin/python -m scripts.hw_console                    # Misty's built-in mic
VOICE=1 VOICE_LAPTOP_MIC=1 .venv-agent/bin/python -m scripts.hw_console # laptop mic instead

# single command (one-shot):
.venv-agent/bin/python -m agent_runtime.main "go to the mug"

# choose the architecture (default v1); works for main and hw_console:
AGENT_ARCH=v2 .venv-agent/bin/python -m agent_runtime.main "go to the mug"
.venv-agent/bin/python -m agent_runtime.main --arch v2 "go to the mug"   # flag also works
AGENT_ARCH=v2 .venv-agent/bin/python -m scripts.hw_console               # interactive, v2
```

Both entry points **preflight the robot connection** before starting the agent: in real mode
they check Misty is reachable at its IP, and if not (e.g. the robot is on a different Wi-Fi),
they **abort immediately with an error and spend zero API tokens**. Override the address with
`MISTY_IP=<ip>` if it ever differs from the default; use `--stub` (or `ROBOT_STUB=1`) to run
fully offline.

`LOG_LEVEL` (`INFO` | `DEBUG` | `FULL`, default `DEBUG`) sets the transcript verbosity written
to `data/hw_session_<ts>.<level>.log`. The console shows only your input and Misty's speech.
Every session also writes `data/hw_session_<ts>_story.txt` — the same events as a readable,
per-agent narrative of who thought what (see
[Reading what the agents were thinking](#reading-what-the-agents-were-thinking)).

## Simulation (offline world model)

A ground-truth 2-D world in [`robot_tools/sim/`](robot_tools/sim/) stands in for the physical
Misty, **behind the MCP stub** — so the same agents, steering, architectures, and `mcp__robot__*`
tools run unchanged against it. Turn it on with a scene:

```bash
ROBOT_SIM_SCENE=office_kitchen AGENT_ARCH=v4 .venv-agent/bin/python -m scripts.hw_console
```

- **Everything is an object with a `shape`** — `point` (cups), `segment` (walls, with
  thickness), or `rect` (tables). There is no "obstacle" flag; **every** object is collidable, and
  a collision records *which* object was hit (so an evaluator can call it success when the user
  asked to reach/bump that target, failure otherwise).
- **Walls block movement and occlude vision.** Rooms are authored as individual wall segments; a
  doorway is just a gap between two. Movement is wall-aware dead-reckoning; `capture_view` returns
  a **synthetic text POV** of only what's inside the narrow ~45° FOV and not hidden behind a wall.
- Scenes are JSON in [`robot_tools/sim/scenes/`](robot_tools/sim/scenes/) (schema in
  [`sim/scene.py`](robot_tools/sim/scene.py)); `ROBOT_SIM_SCENE` takes a bundled name or a path.
- **Visualize a scene** as a minimalist top-down SVG (walls, objects, robot heading + FOV, and
  what's currently in view): `python -m robot_tools.sim.visualize office_kitchen` → writes
  `data/scene_<name>.svg` (open in the IDE or a browser).
- **Watch the live pose.** The sim world lives in-memory in the MCP server process and mutates as
  the agent moves; it's mirrored to `data/sim_state.json` on every move. Render the *running*
  session's current pose with `python -m robot_tools.sim.visualize --live` (re-run after each
  command). The scene file itself stays the pristine initial condition — the sim never writes it.
- The sim world is **ground truth only** — separate from the agent's belief store
  (`get_world`/`update_world`), which the sim never writes.

## Experiments: the task list + a simulated user

A scene is a *world*; it says nothing about what should happen in it. [`evaluation/`](evaluation/)
adds the layer on top: a **task list** where each task pairs one scene with a **goal**, and a
**simulated user** (Claude Haiku) that holds that goal and talks to the robot. Same scene +
different goal = a different task.

```bash
.venv-agent/bin/python -m evaluation.run_tasks --list                    # the task list
.venv-agent/bin/python -m evaluation.run_tasks --validate                # check it vs the scenes
AGENT_ARCH=v4 .venv-agent/bin/python -m evaluation.run_tasks             # run everything
.venv-agent/bin/python -m evaluation.run_tasks --arch v1 --category referential_ambiguity
.venv-agent/bin/python -m evaluation.run_tasks --task amb_001 --repeats 3
```

The **information asymmetry is the point**, and it runs both ways:

- The **user is omniscient** — it gets the full ground-truth scene (every object in every room,
  the robot's pose, and what is currently inside the robot's camera cone), refreshed every turn,
  because a person in the room can look around. That view is
  [`sim/omniscient.py`](robot_tools/sim/omniscient.py), the counterpart to the robot's narrow
  [`sim/pov.py`](robot_tools/sim/pov.py). So it can answer **any** question the robot asks.
- The **user only ever hears `speak`** — the Director's own message text, its delegations, and
  every tool result are invisible to it. If a turn produces no `speak`, the user is told the robot
  said nothing, and reacts like a person would.
- **User utterances are underspecified on purpose.** Everyday requests to a robot leave out the
  detail that would disambiguate them ("go over to the cup"), which is the pressure the system is
  meant to handle; the user supplies the missing detail only when asked. The first utterance is
  held to the bare category noun — no colour, side, room or size.
- Only the user sees the **goal**. Nothing in the robot's context contains it, or the scene.
- The user decides when it's over (goal met, or clearly stuck) and ends the episode; a
  `max_user_turns` cap per task and a `--turn-timeout` per robot turn bound an unattended run.

Tasks live in [`evaluation/task_list.json`](evaluation/task_list.json) (schema documented in the
file's own `fields` block, loader in [`tasks.py`](evaluation/tasks.py)) — `task_id`, `scene`,
`goal`, a `category` to filter on (referential ambiguity, cross-room navigation, occluded target,
perception report, multi-step, absent object, expression, obstacle avoidance) and an optional
`target` naming the scene object the goal centres on. `--validate` checks every task's scene
loads and every `target` really exists in it.

Each episode is **isolated** — its own scene, belief store, tool log and live sim-state file, so
nothing leaks between tasks — and writes to `data/eval_<arch>_<ts>/`: `summary.txt` (the run
rollup), `run.json` (the whole run, rewritten after every episode so a long run is inspectable
while it goes), plus per episode `<task_id>_story.txt` (**the readable one** — see below), the
dialogue, end reason and score (`<task_id>.json`), the flat Director-side transcript (`.log`),
every tool call (`_tools.jsonl`) and the ground-truth world as the episode left it (`_sim.json`).

### Scoring: outcome and world-model accuracy

Every run ends with a summary (also written to `summary.txt`, and recomputable for any finished
run with `--summarize <run_dir>` — no tokens, everything needed is on disk):

```
  user-judged success      7/9  (78%)   ← the simulated user's own verdict
  reached target (truth)   5/8  (63%)   ← ground-truth pose, tasks with a target only
  episodes with no speech  1
  mean duration            148.2s per episode
  mean user turns          3.4
  how episodes ended:        7 user_ended   1 turn_timeout   1 max_user_turns
  world model (the agents' own beliefs vs. ground truth):
      entries recorded     14   (referring to nothing real: 1; made-up names: 2)
      room correct         86%
      direction correct    71%   (mean error 18.4°)
      recall of what it saw 64%
```

Outcome is three signals kept deliberately separate rather than collapsed into one number —
`user_judged_success` (the user held the goal and decided; the headline), `reached_target`
(ground truth: the robot's final pose is within 0.75 m of the target's surface, or it collided
with it, which in this sim means arrived), and `end_reason` (which separates giving up from
running out of turns from a harness failure).

**World-model accuracy** checks the agents' own beliefs — the free-form dicts `update_world`
writes, e.g. `"red cup": {"room": "office", "direction_last_seen": "10° left-front"}` — against
what the sim knows:

- *grounding* — does the recorded name refer to a real object, or to nothing (`teapot`)? And is
  the name one the scene actually uses, or the agent's invention for a real thing (`far_chair`)?
- *room* — does the claimed room match? (With two identically-named objects, matching either
  counts; the belief store has no way to say which instance it means.)
- *direction* — a bearing is only meaningful relative to where the robot stood when it was
  written, so the tool layer records the **pose alongside every call** in sim mode, and
  `"10° to the left"` is scored against the true bearing from that pose (±35° tolerance).
- *recall* — of the objects the camera actually showed (also recorded per `capture_view`), how
  many reached the world model at all.

Both of those are **log-only**: the pose and visible-object list are written beside the call and
never returned to the agent, so instrumenting this changed nothing the robot can perceive.
Anything the evidence can't settle scores `unknown`/`unscorable` rather than wrong — a belief
with no direction is not a wrong direction. Details in [`score.py`](evaluation/score.py).

### Reading what the agents were thinking

`<task_id>_story.txt` (and `data/hw_session_<ts>_story.txt` for console sessions) renders the same
message stream as a story you can read top to bottom, written live so you can `tail -f` it. Every
line is attributed and nested under the delegation it belongs to:

```
──── TURN 1 ─────────────────────────────────────────────────────── 00:42:10
  USER ▶
        Can you go look at the cup on the table?

  DIRECTOR · thinking
      The user wants me to go look at the cup on the table. …

  DIRECTOR ▶ delegates to object-lookup  (foreground)
      task: Locate and perceive the cup on the table
      brief: Find and look at the cup on the table. Report its direction …

     object-lookup · calls mcp__robot__capture_view()
         → Directly ahead (~45° FOV): you can see 4 things in this narrow view —
           - red cup (red): ~10° to your left, nearby.

     object-lookup ▪ reports back to the Director
         STATUS: MULTIPLE — I see two cups on the table …

     🔊 MISTY SPEAKS   (friction: probing)
         "I see a red cup to my left and a blue cup to my right. Which one …"

  ── turn 1 ended · success · 32s · 2 delegation(s) · 8 tool call(s) · 1 utterance(s)
```

Attribution is exact, not guessed: `AssistantMessage.parent_tool_use_id` is `None` for the
Director and otherwise the id of the `Agent` call that spawned the subagent, so
[session.py](agent_runtime/session.py) records those ids and [narrative.py](agent_runtime/narrative.py)
names every thought, tool call and report. Harness noise is dropped so it can't bury the
reasoning — background-launch acks, `agentId`/token trailers, the `{"ok": true}` echo after a
`speak`, and camera frames (a real-mode JPEG becomes `<camera frame, 34 KB>`). The flat `.log` is
still written next to it, unabridged, for when you need the raw stream.

The robot side is untouched: the runner drives the same architectures, steering, MCP tools and
persistent-session turn semantics as `hw_console`, via the shared engine in
[`agent_runtime/session.py`](agent_runtime/session.py). Only the input source changes — a
simulated user instead of a keyboard.

## Config (env)

| Var | Meaning | Default |
|---|---|---|
| `AGENT_ARCH` | architecture / run option (`v1` flat, `v2` managers, `v3` single agent, `v4` flat no-approval) | `v1` |
| `MISTY_IP` | Robot address; real mode is the default at this IP (override only if it differs) | `172.20.10.2` |
| `ROBOT_STUB` | `1` = offline stub (same as `--stub` on the entry points) | unset → real |
| `ROBOT_STUB_SCENE` | dir of `<direction>.jpg` frames the stub serves (perception tests) | unset |
| `ROBOT_SIM` / `ROBOT_SIM_SCENE` | run the **simulation** (a ground-truth 2-D world) behind the stub; `ROBOT_SIM_SCENE` picks a scene (path or bundled name) | unset / `office_kitchen` |
| `IMAGE_MAX_DIM` | cap the longest edge (px) of frames sent to the VLM; `0` disables resizing | `1024` |
| `SDK_MAX_BUFFER_MB` | SDK stdio per-message buffer (MB); raise if big image messages overflow it | `64` |
| `VOICE` | `1` = voice input in `hw_console` ("hey misty" + command; real robot only) | unset → typed |
| `VOICE_LAPTOP_MIC` | `1` = use the laptop mic (`sounddevice`) instead of Misty's built-in mic | unset → Misty mic |
| `VOICE_WAKE_WORD` | wake phrase to listen for | `hey misty` |
| `FRICTION_OFF` | `1` = gate positive-friction utterances (ablation) | unset (friction on) |
| `LOG_LEVEL` | console transcript level: `INFO`/`DEBUG`/`FULL` | `DEBUG` |
| `WORLD_STATE_PATH` / `TOOL_LOG_PATH` | world memory / tool-call log paths | under `data/` |
| `SIM_STATE_PATH` | where the sim mirrors its live ground-truth world (the eval harness sets one per episode) | `data/sim_state.json` |
| `SIM_USER_MODEL` | model backing the simulated user in `evaluation/` | `claude-haiku-4-5` |
| `ANTHROPIC_API_KEY` | read from `.env`; the simulated user calls the Messages API directly | — |

## Test

```bash
.venv/bin/python -m pytest              # robot-layer + task-list tests (no hardware, no agent)
.venv-agent/bin/python -m scripts.step4_test on   # real-API friction test (stub robot)
.venv-agent/bin/python -m evaluation.run_tasks --validate   # task list vs. the scenes
```

The agent env needs one extra package for the experiments (the simulated user talks to the
Messages API directly, not through the Agent SDK):

```bash
.venv-agent/bin/pip install anthropic
```

See [UNDERSTANDING.md](UNDERSTANDING.md) (Phase-1 tool-layer design) and
[AGENT_UNDERSTANDING.md](AGENT_UNDERSTANDING.md) (Claude Agent SDK notes) for background.
