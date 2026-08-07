"""
Architecture abstraction — the extension point for parallel "run options".

Each concrete Architecture is one wiring of the same robot MCP layer into a different
multi-agent topology (V1 flat Director+experts, V2 Director+managers+experts, and future
variants). The message-collection harness in `main.py` / `hw_console.py` is
architecture-agnostic: it only reads messages and tracks background tasks, so a new variant
is added by subclassing `Architecture` and registering it — nothing in the run loops changes.

A subclass supplies the pieces that actually differ between topologies:
  - `root_prompt()`     : the top-level (Director) system prompt
  - `agents()`          : the AgentDefinition registry (all delegatable agents, flat)
  - `allowed_tools()`   : tools the top-level agent may use (delegation + auto-approval set)
  - `subprocess_env()`  : extra env for the CLI subprocess (e.g. agent-teams flag)
Everything shared (model, MCP wiring, hooks, permission mode, hermetic settings) lives in
`build_options()` here so variants stay small and comparable.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

from claude_agent_sdk import ClaudeAgentOptions

from agent_runtime import config, hooks


class Architecture(ABC):
    #: short registry key, e.g. "v1" (also settable via AGENT_ARCH)
    name: str = ""
    #: one-line human description (shown by the CLI when listing variants)
    description: str = ""
    #: top-level model for the Director/orchestrator
    model: str = "sonnet"
    #: agentic-turn budget for the top-level loop
    max_turns: int = 40

    # ---- pieces each variant overrides ----
    @abstractmethod
    def root_prompt(self) -> str:
        """System prompt for the top-level (Director) agent."""

    @abstractmethod
    def agents(self) -> dict:
        """Flat registry {name: AgentDefinition} of every delegatable agent."""

    def allowed_tools(self) -> list[str]:
        """Tools the top-level agent may call. Robot tools are listed for auto-approval of
        the *subagents'* calls (permission is global), even when the Director never calls
        them itself — its steering keeps it to delegation."""
        return ["Agent"] + config.ALL_ROBOT_TOOLS

    def disallowed_tools(self) -> list[str]:
        return list(config.DISALLOWED_BUILTINS)

    def subprocess_env(self) -> dict:
        """Extra env vars for the Claude CLI subprocess (merged over os.environ)."""
        return {}

    # ---- shared assembly ----
    def build_options(self, tool_log, world_state, scene=None) -> ClaudeAgentOptions:
        agents = self.agents()
        opts = ClaudeAgentOptions(
            model=self.model,
            cli_path=config.find_cli(),
            system_prompt=self.root_prompt(),
            mcp_servers={config.ROBOT_SERVER: config.robot_mcp_config(tool_log, world_state, scene)},
            agents=agents,
            allowed_tools=self.allowed_tools(),
            disallowed_tools=self.disallowed_tools(),
            # Gate delegation to exactly this architecture's agents (blocks the general-purpose
            # fallback on a typeless/unknown subagent_type). Empty for single-agent V3, which
            # has no Agent tool anyway.
            hooks=hooks.build_hooks(valid_agents=set(agents.keys())),
            permission_mode="default",
            setting_sources=[],          # hermetic: ignore ambient .claude/settings.json
            max_turns=self.max_turns,
            # Camera frames (base64 JPEGs) can exceed the SDK's default 1 MB stdio buffer and
            # kill the reader — most visible in V3, where the agent ingests images in the main
            # conversation. Raise it generously (env-tunable).
            max_buffer_size=int(os.environ.get("SDK_MAX_BUFFER_MB", "64")) * 1024 * 1024,
        )
        extra = self.subprocess_env()
        if extra:
            # env passed to the CLI subprocess; merge over the inherited environment so we
            # only add (e.g. the agent-teams flag), never drop PATH / ANTHROPIC_API_KEY.
            opts.env = {**os.environ, **extra}
        return opts
