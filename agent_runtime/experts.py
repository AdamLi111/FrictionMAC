"""
The seven domain-expert subagents, grouped into three clusters, built from their steering
files and scoped to exactly the robot tools each may call. Behavior lives in the steering
files, not here.

Clusters:
  World-Understanding : object-lookup (the sole perceiver), map (owns the world model +
                        disambiguation)
  Action-Space        : navigation, expression
  Dialogue-Management : regular-utterance, friction
"""
from claude_agent_sdk import AgentDefinition

from agent_runtime import config


def _rt(*names):
    return [config.robot_tool(n) for n in names]


def build_agents() -> dict:
    return {
        # ---------------- World-Understanding cluster ----------------
        "object-lookup": AgentDefinition(
            description=("The sole perceiver. Locates/verifies an object for the Director, from "
                         "world memory AND the camera (find_object/capture_view), and reports "
                         "where it is (view, direction, approx distance, obstacles)."),
            prompt=config.read_steering("object_lookup.md"),
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
            prompt=config.read_steering("map.md"),
            tools=_rt("update_world", "get_world", "get_last_view"),
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
            tools=_rt("move_arm", "move_head", "display_image", "change_led", "reset_pose"),
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
