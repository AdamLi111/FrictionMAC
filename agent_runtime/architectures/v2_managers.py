"""
V2 — Director + Domain Managers + experts, as named, mutually-addressable subagents.

A mid-layer of three Domain Managers sits between the Director and the six experts:
  Director  --Agent-->  {world,action,dialogue}-manager  --Agent-->  their experts

Every agent is spawned with a `name`, which makes it addressable by `SendMessage` and
resumable: messaging an idle agent reloads its stored transcript and runs a fresh turn, so a
manager keeps its context across tasks. That is what carries coordination sideways and upward,
on top of the `Agent` hierarchy that carries tasks down:
  - managers <-> managers  (coordination; needs the env flag below)
  - expert -> its manager  (information requests, then escalated to the Director if needed)
No approval gate: experts call their tools directly once their manager assigns the task.

These are SUBAGENTS, not a Claude Code "agent team" -- agent teams need an interactive session
and are never formed from an Agent SDK session (no ~/.claude/teams or ~/.claude/tasks is ever
written). The env flag in `subprocess_env` buys one specific thing: cross-agent name
resolution, without which peer-to-peer messaging fails. Everything else reuses the shared
assembly in `Architecture.build_options`.
"""
from agent_runtime import config, experts, managers
from agent_runtime.architectures.base import Architecture


class DomainManagerArchitecture(Architecture):
    name = "v2"
    description = ("Managers: Director -> 3 Domain Managers -> 6 experts, as a live SendMessage "
                   "team (peer + upward coordination, no approval).")
    # More coordinators + real inter-agent messaging => more turns than the flat variant.
    max_turns = 80

    def root_prompt(self) -> str:
        return config.read_steering("v2/director.md")

    def agents(self) -> dict:
        # Flat registry: the Director spawns the 3 managers; each manager spawns its experts.
        # A single dict is correct because the SDK/CLI team is one flat namespace — steering,
        # not config, decides who spawns/messages whom.
        return {**managers.build_managers(), **experts.build_agents_v2()}

    def allowed_tools(self) -> list[str]:
        # Director delegates (Agent) and talks to managers (SendMessage/ToolSearch); robot tools
        # are listed only so the experts' calls are auto-approved (global permission).
        return ["Agent", "SendMessage", "ToolSearch"] + config.ALL_ROBOT_TOOLS

    def subprocess_env(self) -> dict:
        return {
            # Enables cross-agent name resolution, which PEER-TO-PEER SendMessage needs: with
            # the flag off, an agent messaging another agent it did not itself spawn fails with
            # "No agent named '<x>' is reachable" and only raw agentIds work. Spawner->child
            # addressing works either way, so the flag matters precisely for manager<->manager
            # traffic. Reproduce both directions with scripts/team_probe.py (TEAMS_FLAG=0|1).
            #
            # NOTE: this does NOT create a Claude Code "agent team". Agent teams require an
            # interactive session and are never formed from an Agent SDK session, so no team
            # config, task list or mailbox is created (~/.claude/teams and ~/.claude/tasks stay
            # absent). These agents are named SUBAGENTS that can message and resume each other.
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            # Director(main) -> manager(1) -> expert(2) is within the default depth of 3; set
            # 4 for headroom so nested spawns never silently fail.
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "4",
        }
