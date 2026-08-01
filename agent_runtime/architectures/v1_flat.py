"""
V1 — flat orchestrator/worker (the original design).

Director delegates directly to the seven domain-expert subagents (three clusters); only the
experts call robot tools; speech is gated propose -> approve -> speak. This is a thin wrapper
that packages the existing wiring as a selectable Architecture; behavior is unchanged.
"""
from agent_runtime import config, experts
from agent_runtime.architectures.base import Architecture


class FlatArchitecture(Architecture):
    name = "v1"
    description = "Flat: Director delegates directly to 7 experts (3 clusters). Original design."
    max_turns = 40

    def root_prompt(self) -> str:
        return config.read_steering("v1/director.md")

    def agents(self) -> dict:
        return experts.build_agents()
