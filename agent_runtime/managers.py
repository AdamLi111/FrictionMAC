"""
The three Domain Managers (V2 mid-layer). Each supervises one expert cluster: it parses the
Director's task, delegates sub-tasks to its experts (via the `Agent` tool), coordinates with
peer managers when useful (via `SendMessage`), and answers or escalates information requests
that bubble up from its experts. Managers are pure coordinators — they hold NO robot tools;
all actuation happens at the expert leaves. Behavior lives in the steering files.
"""
from claude_agent_sdk import AgentDefinition

from agent_runtime import config
from agent_runtime.experts import TEAM_TOOLS

# A manager delegates (Agent) and talks (SendMessage/ToolSearch); it never actuates the robot.
_MANAGER_TOOLS = ["Agent"] + TEAM_TOOLS


def build_managers() -> dict:
    def manager(name, steering, description):
        return AgentDefinition(
            description=description,
            prompt=config.read_steering(steering),
            tools=_MANAGER_TOOLS,           # no mcpServers: managers coordinate, don't actuate
            model="inherit",
        )

    return {
        "world-manager": manager(
            "world-manager", "v2/manager_world.md",
            ("Domain Manager for World-Understanding. Supervises object-lookup (perception) and "
             "map (world model + ambiguity). Delegate here for anything about perceiving, "
             "locating, recording, or disambiguating objects/scene.")),
        "action-manager": manager(
            "action-manager", "v2/manager_action.md",
            ("Domain Manager for Action-Space. Supervises navigation (movement primitives) and "
             "expression (arm/head/face/LED). Delegate here for movement toward a target and for "
             "emotional/affective expression.")),
        "dialogue-manager": manager(
            "dialogue-manager", "v2/manager_dialogue.md",
            ("Domain Manager for Dialogue. Supervises regular-utterance (normal speech) and "
             "friction (positive-friction turns). Delegate here for anything the robot should "
             "SAY, including asking the user a clarifying question.")),
    }
