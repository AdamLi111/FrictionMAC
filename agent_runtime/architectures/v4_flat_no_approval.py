"""
V4 — flat orchestrator/worker, no approval anywhere (a no-friction-gate variant of V1).

Identical to V1 (Director delegates directly to the seven experts across three clusters; only
experts call robot tools) except that every approval gate is removed: dialogue agents speak
directly and navigation plans-and-drives directly, with no propose→approve step. Behavior lives
in steering/v4/*.md.
"""
from agent_runtime import config, experts
from agent_runtime.architectures.base import Architecture


class FlatNoApprovalArchitecture(Architecture):
    name = "v4"
    description = "Flat like v1, but no approval — experts speak and drive directly."
    max_turns = 40

    def root_prompt(self) -> str:
        return config.read_steering("v4/director.md")

    def agents(self) -> dict:
        return experts.build_agents_v4()
