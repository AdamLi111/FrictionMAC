"""
V2 — Director + Domain Managers + experts, as a live agent team.

A mid-layer of three Domain Managers sits between the Director and the six experts:
  Director  --Agent-->  {world,action,dialogue}-manager  --Agent-->  their experts

All eleven agents run as **named, persistent teammates in a single team** (CLI agent-teams
mode). On top of the delegation hierarchy that carries tasks DOWN, any teammate can talk to
any other DIRECTLY via `SendMessage`, which is how the lateral/upward coordination happens:
  - managers <-> managers  (coordination)
  - experts  <-> experts    (e.g. object-lookup asks map about ambiguity)
  - expert -> manager -> Director -> dialogue/friction -> user  (information requests)
No approval gate: experts call their tools directly once their manager assigns the task.

The whole team is enabled by one CLI env flag; everything else reuses the shared assembly in
`Architecture.build_options`.
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
            # Turn on the CLI's agent-teams / SendMessage machinery (env flag only; the
            # --agent-teams CLI flag is rejected in headless SDK mode). Verified working on
            # CLI 2.1.211 + SDK 0.2.120 via scripts/team_probe.py.
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            # Director(main) -> manager(1) -> expert(2) is within the default depth of 3; set
            # 4 for headroom so nested spawns never silently fail.
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "4",
        }
