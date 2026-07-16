# ponder_multi_agent — robot tool layer

Phase 1: a **standalone MCP server** exposing the Misty robot's physical, vision, speech,
and world-memory capabilities as 14 tools. No agent loop / Director / steering files yet —
those come in later phases on top of this layer. See [UNDERSTANDING.md](UNDERSTANDING.md)
for the full design and the reference capabilities this wraps.

## Layout

```
robot_tools/
  server.py          # FastMCP server — registers the 14 MCP tools
  tools.py           # the 14 tool callables (logging, redaction, never-raise)
  runtime.py         # lazy singletons + calibration bridge + stub-aware sleep/capture
  backend.py         # real mistyPy Robot (fail-loud) vs StubRobot
  world_state.py     # WorldState — persistent JSON world memory (atomic, non-destructive)
  logging_jsonl.py   # ToolLogger — append-only JSONL tool-call log
  config.py          # env-driven config
  vendor/            # copied, UNMODIFIED-in-spirit reference code:
    action_executor.py   # from EmbodiedPF, narration STRIPPED; source of truth for calibration
    vision_handler.py    # from EmbodiedPF, verbatim
    mistyPy/             # Misty Python SDK, verbatim
tests/                 # stub-mode test suite (no hardware)
```

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

# Real mode (default): talks to a physical Misty; FAILS LOUDLY if unreachable.
MISTY_IP=192.168.1.50 python -m robot_tools.server

# Stub mode (opt-in): fakes all robot calls for offline dev — no hardware needed.
ROBOT_STUB=1 python -m robot_tools.server
```

## Config (env)

| Var | Meaning | Default |
|---|---|---|
| `MISTY_IP` | Robot address (real mode) | — (required in real mode) |
| `ROBOT_STUB` | `1` = offline stub mode | unset (real mode) |
| `WORLD_STATE_PATH` | World-memory JSON file | `data/world_state.json` |
| `TOOL_LOG_PATH` | JSONL tool-call log | `data/tool_calls.jsonl` |

## The 14 tools

Movement: `move_forward`, `move_backward`, `strafe_left`, `strafe_right`, `turn_left`,
`turn_right`, `stop` · Navigation: `spatial_navigate` · Speech: `speak`,
`ask_clarification` · Vision: `capture_view`, `find_object` · World memory:
`get_known_location`, `update_world`.

- Tools **never narrate** — only `speak`/`ask_clarification` talk.
- Vision tools return **raw base64 frames**; the agent (a VLM) reasons over them — no VLM runs in the server.
- Every tool call logs one JSONL line `{timestamp, name, args, result}`; image results are logged **redacted** (frame count/size, not raw base64).

## Test

```bash
pip install -e ".[dev]"
python -m pytest        # stub mode; no hardware, no agent
```
