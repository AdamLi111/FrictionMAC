"""
Agent runtime: Director + 3 domain experts + explicit friction routing + inert gate.

Runs the Director agent (real Claude API) with the stubbed robot tool layer connected over
MCP. The Director delegates to world-understanding / action / dialogue subagents; friction
(ask_clarification) fires on genuine ambiguity per the Director's steering. A PreToolUse hook
can gate ask_clarification for a future friction-OFF ablation (inert unless FRICTION_OFF=1).

Usage:
    python -m agent_runtime.main "move forward 1 meter"
    ROBOT_STUB_SCENE=tests/fixtures/scene_two_mugs python -m agent_runtime.main "go to the mug"
"""
import os
import sys

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from agent_runtime import config, experts, hooks

DEFAULT_TOOL_LOG = config.DATA_DIR / "agent_tool_calls.jsonl"
DEFAULT_WORLD_STATE = config.DATA_DIR / "agent_world_state.json"


def build_options(tool_log, world_state, scene=None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model="sonnet",
        cli_path=config.find_cli(),
        system_prompt=config.read_steering("director.md"),
        mcp_servers={config.ROBOT_SERVER: config.robot_mcp_config(tool_log, world_state, scene)},
        agents=experts.build_agents(),
        allowed_tools=["Agent"] + config.ALL_ROBOT_TOOLS,   # 'Agent' enables delegation
        hooks=hooks.build_hooks(),
        permission_mode="default",
        setting_sources=[],          # hermetic: ignore ambient .claude/settings.json
        max_turns=24,
    )


async def run(prompt: str, *, tool_log=None, world_state=None, scene=None) -> dict:
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)
    tool_log = tool_log or DEFAULT_TOOL_LOG
    world_state = world_state or DEFAULT_WORLD_STATE

    seen_tool_uses, final_text = [], None
    options = build_options(tool_log, world_state, scene)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        seen_tool_uses.append(block.name)
                        print(f"[tool_use] {block.name}  {block.input}")
                    elif isinstance(block, TextBlock) and block.text.strip():
                        print(f"[assistant] {block.text.strip()[:300]}")
            elif isinstance(message, ResultMessage):
                final_text = message.result
                print(f"[result:{message.subtype}] {message.result}")

    return {"tool_uses": seen_tool_uses, "final_text": final_text}


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "move forward 1 meter"
    scene = os.environ.get("ROBOT_STUB_SCENE")  # allow scripted scene from env for manual runs
    anyio.run(lambda: run(prompt, scene=scene))


if __name__ == "__main__":
    main()
