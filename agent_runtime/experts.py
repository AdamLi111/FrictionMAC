"""
The three domain-expert subagents, built from their steering files and scoped to exactly the
robot tools each may call. Behavior lives in the steering files, not here.
"""
from claude_agent_sdk import AgentDefinition

from agent_runtime import config


def _rt(*names):
    return [config.robot_tool(n) for n in names]


def build_agents() -> dict:
    return {
        "world-understanding": AgentDefinition(
            description=("Perceives the scene via the robot's camera/360 scan (it is a VLM and "
                         "judges the images), reads/writes world memory, and reports whether a "
                         "target is CLEAR, AMBIGUOUS, or NOT_FOUND. Use FIRST for any request "
                         "about a physical target or place."),
            prompt=config.read_steering("world_understanding.md"),
            tools=_rt("capture_view", "find_object", "get_known_location", "update_world"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "action": AgentDefinition(
            description=("Executes robot movement (drive, turn, strafe, navigate, stop) and "
                         "reports DONE or INFEASIBLE. Use to physically carry out a movement "
                         "once the target/instruction is clear."),
            prompt=config.read_steering("action.md"),
            tools=_rt("move_forward", "move_backward", "strafe_left", "strafe_right",
                      "turn_left", "turn_right", "stop", "spatial_navigate"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
        "dialogue": AgentDefinition(
            description=("Talks to the user: `speak` for normal responses, `ask_clarification` "
                         "for a clarifying/friction question. Use to ask the user something or "
                         "to deliver a spoken reply."),
            prompt=config.read_steering("dialogue.md"),
            tools=_rt("speak", "ask_clarification"),
            mcpServers=[config.ROBOT_SERVER],
            model="inherit",
        ),
    }
