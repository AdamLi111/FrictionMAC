"""
The seven domain-expert subagents, grouped into three clusters, built from their steering
files and scoped to exactly the robot tools each may call. Behavior lives in the steering
files, not here.

Clusters:
  World-Understanding : object-lookup, map, disambiguation   (perception shared by all three)
  Action-Space        : navigation, expression
  Dialogue-Management : regular-utterance, friction
"""
from claude_agent_sdk import AgentDefinition

from agent_runtime import config

# Perception is shared across the whole World-Understanding cluster (per the design): every
# WU agent can look, and decides for itself whether it needs to.
PERCEPTION = ("find_object", "capture_view")


def _rt(*names):
    return [config.robot_tool(n) for n in names]


def build_agents() -> dict:
    return {
        # ---------------- World-Understanding cluster ----------------
        "object-lookup": AgentDefinition(
            description=("Locates/verifies an object for the Director, from world memory AND "
                         "the camera, and reports where it is (view, direction, approx "
                         "distance, obstacles). Use to find or check on a target."),
            prompt=config.read_steering("object_lookup.md"),
            tools=_rt("get_known_location", *PERCEPTION),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "map": AgentDefinition(
            description=("Owns the world model: decides what is worth remembering and writes "
                         "it with update_world, keeping one canonical schema, no duplicates, "
                         "and shared properties (e.g. room) consistent across entries."),
            prompt=config.read_steering("map.md"),
            tools=_rt("update_world", "get_world", "get_known_location", *PERCEPTION),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "disambiguation": AgentDefinition(
            description=("Decides whether a referenced target is ambiguous — e.g. two mugs of "
                         "different colours known/seen — and reports CLEAR or AMBIGUOUS with "
                         "the candidates. Reads the world model and may look."),
            prompt=config.read_steering("disambiguation.md"),
            tools=_rt("get_world", "get_known_location", *PERCEPTION),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        # ---------------- Action-Space cluster ----------------
        "navigation": AgentDefinition(
            description=("Executes the primitive movement steps the Director specifies "
                         "(move/turn/strafe/stop) and reports DONE or INFEASIBLE."),
            prompt=config.read_steering("navigation.md"),
            tools=_rt("move_forward", "move_backward", "strafe_left", "strafe_right",
                      "turn_left", "turn_right", "stop"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "expression": AgentDefinition(
            description=("Conveys emotion/affect by composing arm, head, face-display and LED "
                         "movements. Use for expressive/emotional behaviour."),
            prompt=config.read_steering("expression.md"),
            tools=_rt("move_arm", "move_head", "display_image", "change_led"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        # ---------------- Dialogue-Management cluster ----------------
        "regular-utterance": AgentDefinition(
            description=("Composes a NORMAL spoken reply (confirmations, answers, status). "
                         "Proposes the text to the Director; speaks only once approved."),
            prompt=config.read_steering("regular_utterance.md"),
            tools=_rt("speak"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "friction": AgentDefinition(
            description=("Composes a POSITIVE-FRICTION utterance (clarify / reveal assumption "
                         "/ pause / etc.) with the right friction_type. Proposes it to the "
                         "Director; speaks only once approved."),
            prompt=config.read_steering("friction.md"),
            tools=_rt("speak"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
    }
