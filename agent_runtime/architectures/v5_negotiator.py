"""
V5 — flat, no approval, plus a negotiator that decides when Misty slows down.

V4's topology and experts, with one agent added and one decision moved to it. In V1–V4 the
Director decides when to apply positive friction, from a generic sense of "ambiguity", and the
friction agent only words the utterance. Here the **negotiator** judges every user turn, and every
surprise an expert reports. It carries the friction ontology of İnan et al. (2025) and MAC's
ambiguity checks (Acikgoz et al., 2025), and its VERDICT is enforced by
agent_runtime/negotiation.py: nothing runs before a new user turn is judged, navigation waits
while the negotiator is gathering or asking, and Misty asks once, then waits for the answer.

The other experts read V4's steering unchanged, so V4 → V5 isolates the negotiator.
"""
from agent_runtime import config, experts, hooks, negotiation
from agent_runtime.architectures.base import Architecture


class NegotiatorArchitecture(Architecture):
    name = "v5"
    description = ("Flat like v4, plus a negotiator that decides when Misty slows down "
                   "(positive friction), enforced by a gate.")
    # A negotiator consult per user turn, another per checkpoint, and the odd held delegation the
    # Director re-issues: a little more headroom than V4's 40.
    max_turns = 50

    def root_prompt(self) -> str:
        return config.read_steering("v5/director.md")

    def agents(self) -> dict:
        return experts.build_agents_v5()

    def hook_set(self, agent_names: set) -> dict:
        # A fresh gate on every call: build_options runs once per SDK session, so each console
        # session and each evaluation episode gets its own protocol state and dialogue.
        return hooks.merge_hooks(super().hook_set(agent_names),
                                 negotiation.NegotiationGate().hook_set())
