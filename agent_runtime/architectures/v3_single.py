"""
V3 — single agent.

One agent holds **all** the robot tools and interacts with the user directly: it perceives,
reasons, moves, expresses, keeps the world model, and speaks itself. There is no delegation, no
subagents, and no teams machinery — the polar opposite of V2. This is the baseline against which
the multi-agent variants are compared.
"""
from agent_runtime import config
from agent_runtime.architectures.base import Architecture


class SingleAgentArchitecture(Architecture):
    name = "v3"
    description = "Single agent: one agent holds all robot tools and talks to the user directly (no delegation)."
    max_turns = 40

    def root_prompt(self) -> str:
        return config.read_steering("v3/agent.md")

    def agents(self) -> dict:
        return {}   # no subagents

    def allowed_tools(self) -> list[str]:
        # The single agent CALLS every robot tool itself — no `Agent` tool, no delegation.
        return list(config.ALL_ROBOT_TOOLS)
