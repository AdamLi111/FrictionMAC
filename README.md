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

## Run

```bash
# one-time setup
python -m venv .venv        && .venv/bin/pip install -e .            # robot env
python -m venv .venv-agent  && .venv-agent/bin/pip install claude-agent-sdk

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

## Test

```bash
.venv/bin/python -m pytest              # robot-layer stub tests (no hardware, no agent)
.venv-agent/bin/python -m scripts.step4_test on   # real-API friction test (stub robot)
```

See [UNDERSTANDING.md](UNDERSTANDING.md) (Phase-1 tool-layer design) and
[AGENT_UNDERSTANDING.md](AGENT_UNDERSTANDING.md) (Claude Agent SDK notes) for background.
