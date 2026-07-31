# ponder_multi_agent

A multi-agent robot system for the Misty II, built on the **Claude Agent SDK**. Two layers,
two Python environments, talking over MCP:

- **`robot_tools/`** — a standalone **MCP server** (18 tools) wrapping Misty's real
  capabilities: movement, expression, speech, vision, and persistent world memory. Runs in
  the robot env (`.venv`).
- **`agent_runtime/`** — a **Director** agent that delegates to six domain-expert subagents,
  connected to the robot MCP server. Runs in the agent env (`.venv-agent`, Python ≥3.10, has
  `claude-agent-sdk`).

Robot access has two modes, chosen by `MISTY_IP`: **real** (fails loudly if unreachable) or
**stub** (`ROBOT_STUB=1`, or simply no `MISTY_IP` — fakes all robot calls for offline dev).

## The 19 robot tools (`mcp__robot__*`)

| Group | Tools |
|---|---|
| Movement (DRIVE) | `move_forward`, `move_backward`, `strafe_left`, `strafe_right`, `turn_left`, `turn_right`, `stop` |
| Expression (ARM_LEFT/ARM_RIGHT/HEAD/FACE/LED) | `move_arm`, `move_head`, `display_image`, `change_led`, `reset_pose` |
| Speech | `speak(text, friction_type)` |
| Vision | `capture_view`, `get_last_view`, `find_object` |
| World memory | `get_known_location`, `update_world`, `get_world` |

- **Concurrency + locks:** server tool calls are offloaded to threads, and each physical
  motor resource has its own lock — calls on the *same* motor serialize (e.g. a 360° scan vs.
  driving), calls on *different* motors run concurrently. The two arms are independent locks.
- **Vision** returns real image content the agent (a VLM) reasons over; no VLM runs in the
  server. `find_object` does a 360° scan (4 views); `get_last_view` re-serves the last
  capture's frames with **no new scan**.
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

## Run

```bash
# one-time setup
python -m venv .venv        && .venv/bin/pip install -e .            # robot env
python -m venv .venv-agent  && .venv-agent/bin/pip install claude-agent-sdk

# interactive console (real conversation with Misty; type commands, see what it says):
MISTY_IP=172.20.10.2 .venv-agent/bin/python -m scripts.hw_console
LOG_LEVEL=INFO       .venv-agent/bin/python -m scripts.hw_console    # stub (no movement)

# single command (one-shot):
MISTY_IP=172.20.10.2 .venv-agent/bin/python -m agent_runtime.main "go to the mug"
```

`LOG_LEVEL` (`INFO` | `DEBUG` | `FULL`, default `DEBUG`) sets the transcript verbosity written
to `data/hw_session_<ts>.<level>.log`. The console shows only your input and Misty's speech.

## Config (env)

| Var | Meaning | Default |
|---|---|---|
| `MISTY_IP` | Robot address → **real** mode | unset → stub |
| `ROBOT_STUB` | `1` = stub mode (robot-layer only) | unset |
| `ROBOT_STUB_SCENE` | dir of `<direction>.jpg` frames the stub serves (perception tests) | unset |
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
