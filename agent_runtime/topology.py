"""
Who answers to whom in each architecture, with no Agent SDK import, so a tool that only reads logs
(scripts/log_viewer.py) can draw the team in either env.

The flat variants need nothing here: every agent answers to the Director. Only V2's middle layer
has a shape of its own, and its managers' steering (v2/manager_*.md) assigns them these experts.
"""
DIRECTOR = "director"

#: V2: the experts each Domain Manager supervises.
V2_SUPERVISES = {
    "world-manager": ("object-lookup", "map"),
    "action-manager": ("navigation", "expression"),
    "dialogue-manager": ("regular-utterance", "friction"),
}


def v2_parents() -> dict:
    """{agent: the agent it answers to} for V2."""
    parents = {manager: DIRECTOR for manager in V2_SUPERVISES}
    for manager, experts in V2_SUPERVISES.items():
        parents.update({expert: manager for expert in experts})
    return parents
