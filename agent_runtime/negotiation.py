"""
V5's negotiation protocol: the negotiator decides when Misty slows down, and the code holds the
team to that decision.

In V1–V4, positive friction is the deciding agent's call (the Director; V3's single agent), made
from the friction ontology and triggers in steering/shared/friction_policy.md, and nothing holds it
to them; the friction agent only words the utterance. V5 moves the decision to a negotiator agent
that carries the same ontology and triggers (steering/v5/negotiator.md). Steering can tell the
Director to consult it, but cannot make it do so or obey the answer. So this module keeps a
small per-session state machine, fed by SDK hooks, that holds the Director's delegations:

    phase     entered on                                       held (Director can't delegate to)
    JUDGE     a new user turn; an expert reporting a surprise  everyone but negotiator and map
    GATHER    VERDICT: GATHER (look before deciding)           navigation
    FRICTION  VERDICT: FRICTION (ask/reveal before acting)     navigation, regular-utterance
    ASKED     the friction move has been spoken                navigation, regular-utterance,
                                                               friction
    CLEAR     VERDICT: PROCEED                                 nothing

A *surprise* is an expert's final STATUS that the plan didn't expect (object-lookup MULTIPLE /
NOT_FOUND, map AMBIGUOUS / NONE, navigation INFEASIBLE), or a `SURPRISE: <what>` line in an
object-lookup or navigation report (something in the way, an obstacle stop, a detour), which they
add even when the task succeeded. What the robot does about it is then the negotiator's call too: a
checkpoint, not the Director's improvisation.

The same hooks record the dialogue verbatim (each user utterance, everything Misty says) and
append it to every negotiator brief, so its judgment never rests on the Director's paraphrase.

Only `hook_set()` touches the SDK, so the protocol is unit-testable in either environment.
"""
import re

from agent_runtime import hooks

NEGOTIATOR = "negotiator"

JUDGE, GATHER, FRICTION, ASKED, CLEAR = "JUDGE", "GATHER", "FRICTION", "ASKED", "CLEAR"

#: Delegations held in each phase. JUDGE holds everyone outside _OPEN_WHILE_JUDGING instead, and
#: the negotiator itself is never held.
_HELD = {
    GATHER: {"navigation"},
    FRICTION: {"navigation", "regular-utterance"},
    ASKED: {"navigation", "regular-utterance", "friction"},
    CLEAR: set(),
}
_OPEN_WHILE_JUDGING = {NEGOTIATOR, "map"}     # map only records; it commits the robot to nothing
_ON_VERDICT = {"PROCEED": CLEAR, "GATHER": GATHER, "FRICTION": FRICTION}

# The verdict block the negotiator ends with, read through the markdown a model wraps it in
# ("**VERDICT:** GATHER"). Fields are read at the start of a line, so a word in its reasoning
# ("the trigger is...") can't pose as one, and the LAST match wins, so a verdict mentioned
# earlier can't override the block at the end. A VERDICT written mid-line is still accepted
# when there is no line-start one. An expert's STATUS line is read the same last-wins way.
_LINE_START = r"(?im)^[\W_]*"
_VALUE = r"[\s:*_`]*([a-z_]+)"
_VERDICT = re.compile(_LINE_START + r"VERDICT[\s:*_`]*(PROCEED|GATHER|FRICTION)\b")
_VERDICT_ANYWHERE = re.compile(r"VERDICT[\s:*_`]*(PROCEED|GATHER|FRICTION)\b", re.I)
_FRICTION_TYPE = re.compile(_LINE_START + "FRICTION_TYPE" + _VALUE)
_TRIGGER = re.compile(_LINE_START + "TRIGGER" + _VALUE)
_STATUS = re.compile(r"STATUS[\s:*_`]*([A-Z_]+)")
_SURPRISES = {"MULTIPLE", "NOT_FOUND", "AMBIGUOUS", "NONE", "INFEASIBLE"}
_SURPRISE_FROM = {"object-lookup", "map", "navigation"}
# The line object-lookup and navigation add to a report when something surprised them, read
# last-wins at the start of a line like the verdict fields. "SURPRISE: none" is no surprise.
_SURPRISE_LINE = re.compile(_LINE_START + r"SURPRISE[\s*_`]*:[\s*_`]*(.*\S)")
_FLAGGED_FROM = {"object-lookup", "navigation"}
_NO_SURPRISE = {"", "none", "n/a", "no", "nothing"}

_CLOCK = re.compile(r"^\s*\[clock [^\]]*\]\s*")      # session.stamp_command's header
_LAUNCH_ACK = "Async agent launched successfully"   # a background launch, not a report
_MAX_LINE = 400                                     # chars of one utterance kept in the brief


def _last(pattern, text: str) -> str | None:
    found = pattern.findall(text)
    return found[-1] if found else None


def _flagged(text: str) -> str | None:
    """What a report's `SURPRISE:` line says, or None when there is none (or it says none)."""
    flag = (_last(_SURPRISE_LINE, text) or "").strip(" *_`.")
    return flag[:120] if flag.lower() not in _NO_SURPRISE else None


def _text_of(value) -> str:
    """The report inside a tool response. The Agent tool's response shape (a string, a list of
    content blocks, a dict around either) is not something this module should depend on, but
    the brief it was given must not be mistaken for the report, so `prompt` is skipped."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_text_of(v) for v in value)
    if isinstance(value, dict):
        for key in ("content", "text", "result"):
            if key in value:
                return _text_of(value[key])
        return "\n".join(_text_of(v) for k, v in value.items() if k != "prompt")
    return ""


class NegotiationGate:
    """One session's protocol state. V5 builds a fresh gate with each set of SDK options, so a
    phase or a dialogue never carries over between console sessions or evaluation episodes."""

    def __init__(self, max_dialogue_lines: int = 30):
        self.phase = CLEAR
        self.why_judging: str | None = None   # what awaits the negotiator (for the hold message)
        self.friction_type: str | None = None
        self.dialogue: list[str] = []
        self.max_dialogue_lines = max_dialogue_lines
        self._prompts = 0
        self._unanswered = 0   # negotiator consults since a verdict last reached the gate

    # ----------------------------------------------------------------------------- transitions
    def _enter(self, phase: str, **detail) -> None:
        if phase != self.phase:
            hooks._log_event({"event": "negotiation_phase", "from": self.phase, "to": phase,
                              **detail})
        self.phase = phase

    def user_turn(self, prompt: str) -> bool:
        """A prompt reached the Director. True if it was the user speaking, which re-opens
        judgment. Every front end stamps user commands with session.stamp_command's `[clock …]`
        header, so after the first prompt an unstamped one (a background task's completion
        re-invoking the Director) is not a new user turn."""
        prompt = prompt or ""
        if self._prompts and not _CLOCK.match(prompt):
            return False
        self._prompts += 1
        self._say("USER", _CLOCK.sub("", prompt))
        self.why_judging = "this user turn"
        self._enter(JUDGE, cause="user_turn")
        return True

    def robot_spoke(self, text: str, friction_type: str | None) -> None:
        frictive = bool(friction_type) and friction_type != "none"
        self._say(f"MISTY ({friction_type})" if frictive else "MISTY", text)
        if frictive and self.phase == FRICTION:
            self._enter(ASKED, friction_type=friction_type)

    def report(self, agent: str | None, text: str) -> None:
        """A delegation finished and its report reached the Director."""
        text = text or ""
        if _LAUNCH_ACK in text:
            return                      # a background launch: nothing has been reported yet
        if agent == NEGOTIATOR:
            self._take_verdict(text)    # an empty report is an unreadable verdict: fails open
            return
        if self.phase not in (CLEAR, GATHER):
            return                      # judging already, or waiting on the user's answer
        status = _last(_STATUS, text)
        if agent in _SURPRISE_FROM and status in _SURPRISES:
            self.why_judging = f"{agent}'s report (STATUS: {status})"
            self._enter(JUDGE, cause="checkpoint", agent=agent, status=status)
            return
        flag = _flagged(text) if agent in _FLAGGED_FROM else None
        if flag:
            self.why_judging = f"{agent}'s report (SURPRISE: {flag})"
            self._enter(JUDGE, cause="checkpoint", agent=agent, surprise=flag)

    def consulting(self) -> None:
        """The negotiator is about to be consulted. Two earlier consults whose verdict never
        reached the gate mean it can't see verdicts at all (e.g. a response shape it can't
        read). A gate that can't see verdicts must fail open, not hold the robot forever."""
        self._unanswered += 1
        if self._unanswered > 2 and self.phase != CLEAR:
            hooks._log_event({"event": "negotiator_verdict", "verdict": None,
                              "note": "verdicts are not reaching the gate"})
            self._enter(CLEAR, cause="verdict_not_observed")

    def _take_verdict(self, text: str) -> None:
        self._unanswered = 0
        verdict = (_last(_VERDICT, text) or _last(_VERDICT_ANYWHERE, text) or "").upper()
        self.friction_type = (_last(_FRICTION_TYPE, text) or "").lower() or None
        hooks._log_event({"event": "negotiator_verdict", "verdict": verdict or None,
                          "friction_type": self.friction_type,
                          "trigger": (_last(_TRIGGER, text) or "").lower() or None})
        if not verdict:
            # Fail open rather than wedge the turn: an unreadable report must not hold the robot
            # forever. The event above records that it happened, so a run can be audited for it.
            self._enter(CLEAR, cause="no_verdict_line")
        elif verdict == "FRICTION" and hooks.friction_off():
            # Ablation: the speak gate refuses friction utterances, so waiting for one would hold
            # the robot for nothing. Proceed, as that refusal tells the agents to.
            self._enter(CLEAR, cause="friction_off")
        else:
            self._enter(_ON_VERDICT[verdict], cause="verdict")

    # ---------------------------------------------------------------------------------- queries
    def held(self, subagent_type: str | None, background) -> str | None:
        """Why this delegation has to wait (the refusal the Director reads), or None if it may
        run now. `background` is the call's run_in_background as given; omitted means background."""
        if subagent_type == NEGOTIATOR:
            if background is not False:
                return ("Consult `negotiator` in the foreground: pass run_in_background: false "
                        "explicitly. Its verdict decides what happens next, so you need it back "
                        "before acting.")
            return None
        if self.phase == JUDGE:
            blocked = subagent_type not in _OPEN_WHILE_JUDGING
        else:
            blocked = subagent_type in _HELD[self.phase]
        return self._hold_message(subagent_type) if blocked else None

    def _hold_message(self, requested: str | None) -> str:
        if self.phase == JUDGE:
            return (f"Held: {self.why_judging} has not been judged by the negotiator yet. Delegate "
                    f"to `negotiator` first (run_in_background: false) with the user's exact words "
                    f"and what the team has found, then act on its VERDICT.")
        if self.phase == GATHER:
            return ("Held: the negotiator asked to look before acting (VERDICT: GATHER). Run the "
                    "perception it named, then consult `negotiator` again with what was found; "
                    "navigation waits for its PROCEED.")
        if self.phase == FRICTION:
            kind = self.friction_type or "the type it named"
            return (f"Held: the negotiator called for positive friction ({kind}). Delegate to "
                    f"`friction` with its FRICTION_TYPE and SURFACE first; `{requested}` waits "
                    f"until the user has answered.")
        return ("Held: Misty has asked the user and is waiting for the answer. End your turn; the "
                "answer arrives as the next user turn, and the negotiator judges it then.")

    def negotiator_brief(self, prompt: str) -> str:
        """The Director's brief plus the dialogue verbatim, which the negotiator's steering tells
        it to trust over any paraphrase in the brief."""
        if not self.dialogue:
            return prompt
        recent = self.dialogue[-self.max_dialogue_lines:]
        dropped = len(self.dialogue) - len(recent)
        return "\n".join([
            (prompt or "").rstrip(), "",
            "--- The dialogue so far, verbatim (added by the system; the last USER line is the "
            "turn being judged) ---",
            *([f"(… {dropped} earlier line(s) omitted)"] if dropped else []),
            *recent,
            "--- end of dialogue ---",
        ])

    def _say(self, speaker: str, text: str) -> None:
        text = " ".join((text or "").split())
        if len(text) > _MAX_LINE:
            text = text[:_MAX_LINE] + " …"
        self.dialogue.append(f"{speaker}: {text}")

    # ------------------------------------------------------------------------ SDK hook callbacks
    async def on_user_prompt(self, input_data, tool_use_id, context):
        self.user_turn(input_data.get("prompt", ""))
        return {}

    async def before_delegation(self, input_data, tool_use_id, context):
        tool_input = input_data.get("tool_input") or {}
        requested = tool_input.get("subagent_type")
        reason = self.held(requested, tool_input.get("run_in_background"))
        if reason:
            hooks._log_event({"event": "negotiation_hold", "phase": self.phase,
                              "requested": requested})
            return {"hookSpecificOutput": {
                "hookEventName": input_data["hook_event_name"],
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}
        if requested == NEGOTIATOR:
            self.consulting()
            # Hand the negotiator the dialogue as it was actually spoken. `allow` is what makes
            # the CLI apply `updatedInput`; the type gate's deny for a bad subagent_type still wins.
            return {"hookSpecificOutput": {
                "hookEventName": input_data["hook_event_name"],
                "permissionDecision": "allow",
                "updatedInput": {**tool_input,
                                 "prompt": self.negotiator_brief(tool_input.get("prompt", ""))},
            }}
        return {}

    async def after_delegation(self, input_data, tool_use_id, context):
        self.report((input_data.get("tool_input") or {}).get("subagent_type"),
                    _text_of(input_data.get("tool_response")))
        return {}

    async def after_speech(self, input_data, tool_use_id, context):
        # PostToolUse, not PreToolUse: only an utterance that was actually voiced counts (one the
        # FRICTION_OFF gate refused never reaches the user, so it must not move the protocol on).
        tool_input = input_data.get("tool_input") or {}
        self.robot_spoke(tool_input.get("text", ""), tool_input.get("friction_type"))
        return {}

    def hook_set(self) -> dict:
        """This gate's hook registrations, for `hooks.merge_hooks` over the shared set."""
        from claude_agent_sdk import HookMatcher   # the module's only SDK dependency
        delegation = "^(Agent|Task)$"
        return {
            "UserPromptSubmit": [HookMatcher(hooks=[self.on_user_prompt])],
            "PreToolUse": [HookMatcher(matcher=delegation, hooks=[self.before_delegation])],
            "PostToolUse": [HookMatcher(matcher=delegation, hooks=[self.after_delegation]),
                            HookMatcher(matcher=hooks.SPEAK, hooks=[self.after_speech])],
        }
