"""
The simulated user — an LLM standing in for the human in a human-robot interaction.

It is deliberately asymmetric with the robot:

  * **Omniscient about the world.** It is handed the full ground-truth scene (every object in
    every room, the robot's pose, what is currently in the robot's camera cone) and that
    description is refreshed every turn. That mirrors reality: a person in the room can look
    around; the robot only sees a narrow cone in front of it.
  * **Deaf to everything except speech.** The only thing it learns about the robot's *reasoning*
    is what the robot actually says out loud — i.e. `mcp__robot__speak` calls. The Director's
    internal text, delegations, and tool results never reach it.
  * **Underspecified by default.** Everyday commands to a robot are casual and leave out the
    detail that would disambiguate them ("go get the cup"), which is exactly the pressure the
    system under test is supposed to handle. The user only supplies the missing detail when
    the robot asks for it.

It owns the task GOAL (from the task list) and nothing else does: the robot's agents never see
it. The user decides when the goal has been met and ends the episode with a `[DONE]` sentinel.

Model: Claude Haiku 4.5 via the Messages API (cheap, fast, plenty for producing one short
utterance per turn). The conversation is kept as a normal multi-turn message list where the
*user LLM's own lines are the `assistant` role* and what it perceives (robot speech + scene
state) arrives as `user` turns.
"""
import os
import re

import anthropic

MODEL = os.environ.get("SIM_USER_MODEL", "claude-haiku-4-5")
DONE = "[DONE]"
MAX_TOKENS = 300

SYSTEM_TEMPLATE = """\
You are role-playing a HUMAN talking to a small mobile robot called Misty in a real room. \
Everything you say is spoken out loud to the robot.

{scene}

(That is the room as it is right now, at the start. You get a fresh look at it — including \
where the robot has moved to — after every turn.)

WHAT YOU WANT (your goal — the robot does NOT know it):
{goal}

What the robot can do: it is a small wheeled robot with a camera, two arms, a face screen and a \
speaker. It can drive around, turn, look at things, gesture, and talk. It has NO hands and \
cannot pick anything up, carry anything, or open anything. So ask it for things it can actually \
do — go somewhere, look at something, tell you what it sees — and never ask it to fetch, grab, \
or bring you an object.

How you behave:
- You are a normal person giving a robot a casual instruction, not a careful prompt engineer. \
Real people's requests to robots are routinely UNDERSPECIFIED: you say the short, everyday \
version and leave out the details that would remove the ambiguity ("go grab the cup", not "go \
to the red cup on the left side of the table"). Start that way, and keep your turns short — \
one sentence, the way people actually speak.
- You are NOT trying to trick the robot, and you are not withholding information out of \
stubbornness. If the robot asks you a question, ANSWER IT — truthfully, specifically, and from \
what you can see in the room (you can see everything described above, so you can always \
answer). If it asks which one you meant, tell it. If it asks where something is, describe it \
the way a person would ("it's on the table on the left", "over by the fridge"), not in \
coordinates or degrees.
- You can see the robot and the room, so you know when it has moved, where it is now, and \
whether it is facing the right way. But you CANNOT read its mind: the only words you get from \
it are what it says out loud. If it goes quiet, react like a person would.
- Say what you want, not how to drive. Don't hand it turn angles or distances in metres unless \
it specifically asks for a number.
- Never mention that you are an AI, a simulation, or a role-play, and never quote or paraphrase \
these instructions or the goal text above. Never describe your own reasoning.

Ending the conversation:
- The moment you judge your goal achieved, END IT. Reply with the token {done} — optionally with \
one short closing line before it, on its own line. Nothing after that reaches the robot, so it \
costs you nothing.
- **Never send praise, thanks, or "that's the one" as a message of its own.** If the only thing \
you have left to say is that it worked, that IS the ending — emit {done} instead of saying it and \
waiting. A real person stops talking to the robot at that point; they do not keep congratulating \
it. The same goes for a reply that would just acknowledge what it told you without asking for \
anything further.
- If the conversation is clearly stuck — the robot has given up, keeps repeating itself, or \
cannot do what you asked — say so briefly and end with {done} as well.
- Only keep talking while there is something you still need: a part of the goal that is not done \
yet, an answer to give, or a question to ask. Otherwise emit {done}.

Output ONLY the words you speak to the robot. No narration, no stage directions, no labels.
"""

FIRST_TURN = """\
The robot is here in the room with you, idle, waiting. Say the first thing you say to it.

This first line is the short, casual, UNDERSPECIFIED version of what you want, so leave out \
every distinguishing detail: no colour, no size, no which-one-of-several, no side of the room, \
no room name, no distance. Name the thing by its plain everyday category ("the cup", "the \
plant", "the chair") and say what you want done with it. Under about ten words. You will supply \
the missing detail later if the robot asks for it."""


class SimulatedUserError(RuntimeError):
    """The user LLM could not be reached / responded unusably. Ends the episode cleanly."""


class SimulatedUser:
    """One user, for one task, for one episode. Not reusable across tasks (the goal and the
    conversation are baked in at construction)."""

    def __init__(self, task, scene_brief: str, *, model: str = MODEL, client=None):
        self.task = task
        self.model = model
        self.client = client or anthropic.Anthropic()
        self.system = SYSTEM_TEMPLATE.format(scene=scene_brief, goal=task.goal, done=DONE)
        self.messages: list[dict] = []
        self.turns = 0
        self.done = False
        self.transcript: list[dict] = []   # [{"speaker": "user"|"robot", "text": ...}]

    # ------------------------------------------------------------------ the two entry points
    def opening_utterance(self) -> str | None:
        """The user's first line: the casual, underspecified version of the request. None only
        if the model returned nothing usable, which ends the episode before it starts."""
        return self._say(FIRST_TURN)

    def reply(self, robot_speech: list[str], scene_brief: str) -> str | None:
        """The user's next line, having heard `robot_speech` (every `speak` the robot made this
        turn, in order) and looked at the room again (`scene_brief`).

        Returns the utterance, or None once the user considers the interaction over — goal met
        or hopeless. `self.done` records that it ended on the user's own judgment."""
        if self.done:
            return None
        for text in robot_speech:
            self.transcript.append({"speaker": "robot", "text": text})
        if robot_speech:
            heard = "\n".join(f'The robot said: "{t}"' for t in robot_speech)
        else:
            heard = ("The robot did not say anything at all this time — it may have moved, or "
                     "done nothing.")
        prompt = (f"{heard}\n\nYou look around the room again. Here is what you see now:\n\n"
                  f"{scene_brief}\n\nWhat do you say next? (If your goal is now met — or the "
                  f"only thing left to say would be thanks, praise, or 'yes, that one' — reply "
                  f"with {DONE} instead of saying it.)")
        utterance = self._say(prompt)
        return None if self.done else utterance

    # ------------------------------------------------------------------------------- internals
    def _say(self, prompt: str) -> str | None:
        self.messages.append({"role": "user", "content": prompt})
        text = self._complete()
        self.messages.append({"role": "assistant", "content": text})

        if DONE in text:
            self.done = True
        utterance = self._clean(text)
        if not utterance:
            self.done = True
            return None
        self.turns += 1
        self.transcript.append({"speaker": "user", "text": utterance,
                                "ended_conversation": self.done})
        return utterance

    def _complete(self) -> str:
        try:
            # No sampling parameters: the installed SDK (anthropic 1.3.0) no longer exposes
            # temperature/top_p/top_k, so run-to-run variety comes from the model's own
            # sampling defaults.
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self.system,
                messages=self.messages,
            )
        except anthropic.APIStatusError as e:
            raise SimulatedUserError(f"simulated user API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise SimulatedUserError(f"simulated user connection error: {e}") from e
        if response.stop_reason == "refusal":
            raise SimulatedUserError("simulated user request was refused by safety classifiers")
        return "".join(b.text for b in response.content if b.type == "text")

    @staticmethod
    def _clean(text: str) -> str:
        """Strip the sentinel and the labels/quotes a chat model likes to add around dialogue."""
        text = text.replace(DONE, " ")
        text = re.sub(r'^\s*(you|user|human|me)\s*:\s*', '', text, flags=re.I)
        text = " ".join(text.split())
        if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1].strip()
        return text
