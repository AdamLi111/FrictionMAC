# AGENT_UNDERSTANDING.md — Claude Agent SDK verification (Phase 2)

SDK facts verified against the **current official docs** (fetched 2026-07-15), not memory.
Primary sources:
- Quickstart / Python ref: https://code.claude.com/docs/en/agent-sdk/python
- Subagents: https://code.claude.com/docs/en/agent-sdk/subagents
- MCP: https://code.claude.com/docs/en/agent-sdk/mcp
- Hooks: https://code.claude.com/docs/en/agent-sdk/hooks
- Permissions: https://code.claude.com/docs/en/agent-sdk/permissions

This phase builds the agent runtime **on top of** the finished 14-tool `robot_tools` MCP
server. Two envs: agents run in a **Python 3.10+** env (new `agent_runtime`), the robot MCP
server keeps running in its **3.9** env; they talk over **MCP stdio**. Robot stays
`ROBOT_STUB=1` the whole phase; **API + agents are REAL**. No hardware.

---

## 1. Package + install

- PyPI package: **`claude-agent-sdk`** — current version **0.2.120** (confirmed via `pip index`).
- Install: `pip install claude-agent-sdk`. Requires **Python ≥ 3.10**.
- Also needs the Claude Code CLI runtime under the hood and an `ANTHROPIC_API_KEY` in env.
- Imports used:
  ```python
  from claude_agent_sdk import (
      query, ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
      HookMatcher, PermissionResultAllow, PermissionResultDeny,
      AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
  )
  ```

## 2. Running the agent loop

Two entry points (both verified):

```python
# One-shot / streaming
async def query(*, prompt: str | AsyncIterable[dict],
                options: ClaudeAgentOptions | None = None,
                transport: Transport | None = None) -> AsyncIterator[Message]

# Multi-turn / interactive session (Director will use this)
class ClaudeSDKClient:
    def __init__(self, options=None, transport=None)
    async def connect(self, prompt=None) -> None
    async def query(self, prompt, session_id="default") -> None
    async def receive_response(self) -> AsyncIterator[Message]      # one turn's messages
    async def receive_messages(self) -> AsyncIterator[Message]      # continuous
    async def interrupt(self); async def disconnect(self)
    async def get_mcp_status(self) -> McpStatusResponse             # useful to assert MCP wired
```

Consuming results — iterate messages, read tool-use + final text:
```python
async for message in client.receive_response():
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):   # block.name / block.input / block.id
                ...
            elif isinstance(block, TextBlock):
                ...
    elif isinstance(message, ResultMessage):
        print(message.result, message.subtype)    # subtype: "success" | "error" | ...
```

`ClaudeAgentOptions` fields we'll use: `system_prompt`, `mcp_servers`, `agents`,
`allowed_tools`, `disallowed_tools`, `permission_mode`, `can_use_tool`, `hooks`, `model`,
`env`, `cwd`, `max_turns`, `setting_sources`.

## 3. Subagents (domain experts) — defined in code

```python
@dataclass
class AgentDefinition:
    description: str            # required: when the Director should delegate here
    prompt: str                # required: the expert's system prompt (= its steering file)
    tools: list[str] | None = None
    disallowedTools: list[str] | None = None     # NOTE: camelCase
    model: str | None = None                      # 'opus'|'sonnet'|'haiku'|'inherit'|id
    mcpServers: list[str | dict] | None = None    # camelCase — MCP servers this agent may use
    maxTurns: int | None = None                   # camelCase
    permissionMode: PermissionMode | None = None  # camelCase
    skills / memory / effort / background / initialPrompt  # other optional fields
```
Passed via `ClaudeAgentOptions(agents={"world-understanding": AgentDefinition(...), ...})`.
**To let the Director delegate, its `allowed_tools` must include the `Agent` tool** (this is
the renamed `Task` tool — see §7). Each expert gets `mcpServers=["robot"]` and a `tools`
list restricted to exactly the `mcp__robot__*` tools it may call.

## 4. Connecting our MCP server (stdio)

`mcp_servers` is a dict keyed by a **server name we choose** — that key becomes the tool
namespace. We'll use the key **`robot`** (letters only → simple exact hook matching).

```python
options = ClaudeAgentOptions(
    mcp_servers={
        "robot": {
            "type": "stdio",                       # optional but explicit
            "command": "/abs/path/to/robot_env/bin/python",   # the ROBOT'S 3.9 venv python
            "args": ["-m", "robot_tools.server"],
            "env": {"ROBOT_STUB": "1", "TOOL_LOG_PATH": "<shared>", "WORLD_STATE_PATH": "<shared>"},
        }
    },
    allowed_tools=["Agent",
                   "mcp__robot__move_forward", "mcp__robot__turn_left", ...],
)
```
- Tool names are **`mcp__robot__<tool>`** (e.g. `mcp__robot__ask_clarification`).
- `cwd` must be the repo root (or set `PYTHONPATH`) so `-m robot_tools.server` imports.
- `allowed_tools` entries auto-approve those tools; anything not listed falls to
  `can_use_tool` / hooks / permission prompt. `get_mcp_status()` confirms the server connected.

## 5. Hooks (logging + the inert friction gate)

Registered as `hooks={"PreToolUse": [HookMatcher(matcher=..., hooks=[cb]), ...]}`. Callback:
```python
async def cb(input_data: dict, tool_use_id: str | None, context) -> dict: ...
```
- `input_data` has `hook_event_name`, `tool_name`, `tool_input`, `session_id`, `cwd`, and
  (inside a subagent) `agent_id`, `agent_type`.
- **Deny** a call (PreToolUse):
  ```python
  return {"hookSpecificOutput": {
      "hookEventName": input_data["hook_event_name"],
      "permissionDecision": "deny",             # "allow" | "deny" | "ask" | "defer"
      "permissionDecisionReason": "friction disabled (ablation)"}}
  ```
- **Allow**: `return {}`.
- Multiple hooks for an event run **in parallel; any `deny` wins** — safe to run a global
  logger hook alongside the specific gate hook.
- Matchers: exact string for plain names; a matcher with regex chars is an unanchored regex.
  `"mcp__robot__ask_clarification"` is exact-match (letters/underscore only) → matches that
  one tool. `"^mcp__"` (regex) matches every robot tool → our global logger.
- Python `HookEvent`s available: `PreToolUse, PostToolUse, PostToolUseFailure,
  UserPromptSubmit, Stop, SubagentStart, SubagentStop, PreCompact, PermissionRequest,
  Notification`. (`SessionStart/End` are TS-only in the SDK.)
- **PostToolUse** input carries the tool result (for enriched logging); the exact result
  field name I'll confirm against the Python `HookInput` ref at build time (`/en/agent-sdk/python#hookinput`).

**Design consequence for logging:** the `robot_tools` MCP server *already* writes the
canonical JSONL (`{name, args, result, timestamp}`, `ask_clarification.friction_type`
included) for **every** tool call, because all agent tool calls flow through it. So the
measurement boundary is intact for free. The agent-side hooks will *add* value only:
`SubagentStart`/`SubagentStop` + `PreToolUse` (with `agent_id`/`agent_type`) to log the
**Director's routing decisions** into the same/adjacent log.

## 6. Permissions / `can_use_tool`

```python
async def can_use_tool(tool_name: str, input_data: dict, context) -> PermissionResult
# return PermissionResultAllow(updated_input=None) or PermissionResultDeny(message="...")
```
`permission_mode`: `"default" | "acceptEdits" | "bypassPermissions" | "plan" | "dontAsk" | "auto"`.
Ordering: **hooks run first (can deny even under bypass)**, then deny/ask/allow rules, then
`can_use_tool` as the fallback. → I'll implement the friction gate as a **PreToolUse hook**
(cleaner, composes with the logger, and fires for subagent calls) rather than `can_use_tool`.

## 7. Differences from the brief (follow the docs)

| Brief said | Reality (docs) | Impact |
|---|---|---|
| subagents delegated via "Task" | The Task tool is **renamed `Agent`** | Director's `allowed_tools` must include `"Agent"`, not `"Task"`. |
| `AgentDefinition` fields | Several are **camelCase**: `disallowedTools`, `mcpServers`, `maxTurns`, `permissionMode` | Use camelCase exactly or they're silently ignored. |
| "PreToolUse / canUseTool gate" | Both exist; **PreToolUse hook** is the better fit (runs before everything, covers subagents, composes with logger) | Gate = PreToolUse hook on `mcp__robot__ask_clarification`. |
| generic "connect MCP" | stdio config key = **`type/command/args/env`**; tool namespace = the **dict key** | Use key `robot`; point `command` at the robot's 3.9 python. |
| "log tool calls in the agent layer" | The **MCP server already logs all of them** | Agent hooks add routing/agent context, not duplicate the tool log. |

## 8. Planned build mapping (for steps 2–4; not built yet)

- `agent_runtime/` (new package, its own 3.10+ venv):
  - `steering/` — one file per agent: `director.md`, `world_understanding.md`, `action.md`,
    `dialogue.md` (loaded into `system_prompt` / `AgentDefinition.prompt`; behavior lives here).
  - `experts.py` — build the 3 `AgentDefinition`s from the steering files, each scoped to its
    `mcp__robot__*` tools.
  - `hooks.py` — global `^mcp__` logger hook + inert gate hook on
    `mcp__robot__ask_clarification` (deny iff `FRICTION_OFF=1`, else `{}` → allow).
  - `main.py` — wires `ClaudeAgentOptions` (mcp_servers→robot, agents, hooks, Director
    system_prompt, `allowed_tools` incl. `Agent`), runs `ClaudeSDKClient`.
- The three experts, tool scoping:
  - **World-Understanding** → `capture_view`, `find_object`, `get_known_location`,
    `update_world`; reports an explicit **AMBIGUITY** signal.
  - **Action** → `move_forward/backward`, `strafe_left/right`, `turn_left/right`, `stop`,
    `spatial_navigate`; reports feasibility/outcome.
  - **Dialogue** → `speak`, `ask_clarification` (carries `friction_type` ∈ {probing,
    assumption_reveal, overspecification, reflective_pause, reinforcement}).
- **Friction routing** is explicit in `director.md`: consult World-Understanding first; if it
  signals ambiguity for the target (or Action reports infeasibility) → route to Dialogue's
  `ask_clarification`; else proceed to Action and use `speak`. The agent decides to clarify
  (friction ON = normal). The gate stays **inert** (default allow) until `FRICTION_OFF=1`.

---

**Status: SDK verified, no agent code written. Awaiting your review before Step 2
(Director + MCP connection + one passthrough tool call).**
