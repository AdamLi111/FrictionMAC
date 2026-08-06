"""
The six domain-expert subagents, grouped into three clusters, scoped to exactly the robot
tools each may call. Behavior lives in the steering files, not here.

Two builders, one per architecture (each reads its own self-contained steering directory):
  build_agents()    -> V1 experts (steering/v1/*.md): propose->approve->speak, no peer talk.
  build_agents_v2() -> V2 experts (steering/v2/*.md): named teammates that SendMessage peers,
                       no approval; +SendMessage/ToolSearch on top of their robot tools.

Clusters:
  World-Understanding : object-lookup (the sole perceiver), map (owns the world model +
                        disambiguation)
  Action-Space        : navigation, expression
  Dialogue-Management : regular-utterance, friction
"""
from claude_agent_sdk import AgentDefinition

from agent_runtime import config

# Coordination tools every teams-mode agent needs: SendMessage (talk to a named teammate;
# it is a deferred tool the agent loads via ToolSearch on first use) and ToolSearch itself.
TEAM_TOOLS = ["SendMessage", "ToolSearch"]


def _rt(*names):
    return [config.robot_tool(n) for n in names]


def build_agents_v2() -> dict:
    """The six experts for V2 (teams mode). Each reads its OWN self-contained steering file
    under steering/v2/ — the teamwork behavior (named teammate, SendMessage peers, no approval)
    is written into each file, not injected here — and gains SendMessage/ToolSearch on top of
    its robot tools. Managers are added separately (see managers.py)."""
    def expert(name, steering, tools):
        return AgentDefinition(
            description=_V2_DESCRIPTIONS[name],
            prompt=config.read_steering(steering),
            tools=tools + TEAM_TOOLS,
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        )

    return {
        # ---- World-Understanding cluster (manager: world-manager) ----
        "object-lookup": expert("object-lookup", "v2/object_lookup.md",
            _rt("get_known_location", "find_object", "capture_view")),
        "map": expert("map", "v2/map.md",
            _rt("update_world", "get_world", "get_last_view")),
        # ---- Action-Space cluster (manager: action-manager) ----
        "navigation": expert("navigation", "v2/navigation.md",
            _rt("move_forward", "move_backward", "turn_left", "turn_right", "stop")),
        "expression": expert("expression", "v2/expression.md",
            _rt("move_arm", "move_head", "display_image", "change_led", "reset_pose")),
        # ---- Dialogue-Management cluster (manager: dialogue-manager) ----
        "regular-utterance": expert("regular-utterance", "v2/regular_utterance.md", _rt("speak")),
        "friction": expert("friction", "v2/friction.md", _rt("speak")),
    }


_V2_DESCRIPTIONS = {
    "object-lookup": ("The sole perceiver. Locates/verifies an object from world memory AND the "
                      "camera and reports where it is (view, direction, approx distance, obstacles)."),
    "map": ("Owns the world model — records what was seen and judges whether a referenced target "
            "is AMBIGUOUS. Reads the latest captured image; does NOT scan."),
    "navigation": ("Executes primitive movement steps (move/turn/stop) and reports DONE "
                   "or INFEASIBLE."),
    "expression": ("Conveys emotion/affect via arm, head, face-display and LED movements."),
    "regular-utterance": ("Speaks a NORMAL reply (confirmations, answers, status) directly."),
    "friction": ("Speaks a POSITIVE-FRICTION utterance (clarify / reveal assumption / pause / "
                 "etc.) directly, with the right friction_type; also voices user-facing "
                 "clarifying questions escalated up the chain."),
}


def build_agents() -> dict:
    return {
        # ---------------- World-Understanding cluster ----------------
        "object-lookup": AgentDefinition(
            description=("The sole perceiver. Locates/verifies an object for the Director, from "
                         "world memory AND the camera (find_object/capture_view), and reports "
                         "where it is (view, direction, approx distance, obstacles)."),
            prompt=config.read_steering("v1/object_lookup.md"),
            tools=_rt("get_known_location", "find_object", "capture_view"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "map": AgentDefinition(
            description=("Owns the world model — records what was just seen accurately and "
                         "consistently (canonical schema, no duplicates, shared props like "
                         "room kept consistent), and answers whether a referenced target is "
                         "AMBIGUOUS (multiple candidates of the same category) via get_world. "
                         "Reads the latest captured image with get_last_view; does NOT scan."),
            prompt=config.read_steering("v1/map.md"),
            tools=_rt("update_world", "get_world", "get_last_view"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        # ---------------- Action-Space cluster ----------------
        "navigation": AgentDefinition(
            description=("Plans and executes movement toward a goal. Captures its own front "
                         "view (capture_view) to reason about how to drive; reports DONE or "
                         "INFEASIBLE."),
            prompt=config.read_steering("v1/navigation.md"),
            tools=_rt("move_forward", "move_backward", "turn_left", "turn_right", "stop",
                      "capture_view"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "expression": AgentDefinition(
            description=("Conveys emotion/affect by composing arm, head, face-display and LED "
                         "movements. Use for expressive/emotional behaviour."),
            prompt=config.read_steering("v1/expression.md"),
            tools=_rt("move_arm", "move_head", "display_image", "change_led", "reset_pose"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        # ---------------- Dialogue-Management cluster ----------------
        "regular-utterance": AgentDefinition(
            description=("Composes a NORMAL spoken reply (confirmations, answers, status). "
                         "Proposes the text to the Director; speaks only once approved."),
            prompt=config.read_steering("v1/regular_utterance.md"),
            tools=_rt("speak"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "friction": AgentDefinition(
            description=("Composes a POSITIVE-FRICTION utterance (clarify / reveal assumption "
                         "/ pause / etc.) with the right friction_type. Proposes it to the "
                         "Director; speaks only once approved."),
            prompt=config.read_steering("v1/friction.md"),
            tools=_rt("speak"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
    }
