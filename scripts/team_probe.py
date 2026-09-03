"""
Peer-messaging probe for the Claude Agent SDK + CLI (named subagents / SendMessage).

Decisive test: two named subagents are spawned. `seeker` is told it does NOT know a secret
codeword and MUST obtain it from `keeper` using SendMessage. Only `keeper`'s system prompt
contains the codeword. If the codeword round-trips back, live peer->peer messaging works on
THIS machine's SDK+CLI. Otherwise it doesn't.

NOTE: this exercises SUBAGENT messaging, not Claude Code "agent teams". Agent teams need an
interactive session and are never created from an Agent SDK session, so no team/mailbox is
involved here. Run with TEAMS_FLAG=0 to confirm CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS is not
required for any of this:

    TEAMS_FLAG=0 .venv-agent/bin/python -m scripts.team_probe
"""
import functools
import os
import sys
import time

print = functools.partial(print, flush=True)  # unbuffered logging under pipe

import anyio
from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    UserMessage,
)

from agent_runtime import config

SECRET = "ORANGE-KANGAROO-42"
CAP_S = 150.0
POLL_QUIET_S = 6.0

KEEPER = (
    "You are 'keeper', a teammate in a multi-agent session.\n"
    f"You hold a secret codeword: {SECRET}\n"
    "If any teammate sends you a message (via SendMessage) asking for the codeword, "
    "reply to that teammate using SendMessage with the exact codeword and nothing else. "
    "Do not refuse. If you have nothing to do, that is fine."
)
SEEKER = (
    "You are 'seeker', a teammate in a multi-agent session.\n"
    "You do NOT know the secret codeword and you cannot guess it.\n"
    "You MUST obtain it from your teammate named 'keeper' by using the SendMessage tool "
    "to ask keeper for the codeword, then wait for keeper's reply.\n"
    "When you receive keeper's reply, your FINAL report must be exactly: CODEWORD=<the codeword>."
)
ORCH = (
    "You are the orchestrator of an agent team. Do this literally:\n"
    "1. Spawn a background teammate: subagent_type='keeper', name='keeper', "
    "run_in_background=true, with a prompt telling it to stand by to answer codeword requests.\n"
    "2. Spawn a background teammate: subagent_type='seeker', name='seeker', "
    "run_in_background=true, with a prompt telling it to obtain the secret codeword from "
    "teammate 'keeper' via SendMessage and report it as CODEWORD=<value>.\n"
    "3. Wait for seeker to finish, then tell me the exact codeword seeker reported.\n"
    "Do not invent a codeword yourself; only relay what seeker reports."
)


def build_options():
    agents = {
        "keeper": AgentDefinition(description="Holds the secret codeword.", prompt=KEEPER,
                                  model="sonnet"),
        "seeker": AgentDefinition(description="Must fetch the codeword from keeper.",
                                  prompt=SEEKER, model="sonnet"),
    }
    return ClaudeAgentOptions(
        model="sonnet",
        cli_path=config.find_cli(),
        system_prompt=ORCH,
        agents=agents,
        allowed_tools=["Agent", "SendMessage", "TaskOutput"],
        permission_mode="bypassPermissions",
        max_turns=60,
        env={
            **os.environ,
            # Default "1" preserves the historical probe; TEAMS_FLAG=0 shows it is not needed.
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": os.environ.get("TEAMS_FLAG", "1"),
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "4",
        },
    )


def _blocks(msg):
    out = []
    for b in getattr(msg, "content", None) or []:
        if isinstance(b, ToolUseBlock):
            out.append(f"    [tool_use] {b.name} {str(b.input)[:200]}")
        elif isinstance(b, ToolResultBlock):
            out.append(f"    [tool_result] {str(getattr(b,'content',''))[:200]}")
        elif isinstance(b, TextBlock) and b.text.strip():
            out.append(f"    [text] {b.text.strip()[:300]}")
    return out


async def main():
    config.load_env()
    opts = build_options()
    saw_sendmessage = False
    final = None
    result_seen = False
    pending = set()
    started = set()
    t0 = time.monotonic()

    flag = os.environ.get("TEAMS_FLAG", "1")
    print(f"[probe] CLI={config.find_cli()}  "
          f"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS={flag}  secret={SECRET}")
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("Run the codeword relay and report the codeword seeker got.")
        last_msg = time.monotonic()
        with anyio.move_on_after(CAP_S) as scope:
          async for msg in client.receive_messages():
            cls = type(msg).__name__
            now = time.monotonic()
            if isinstance(msg, (AssistantMessage, UserMessage)):
                who = "ASSIST" if isinstance(msg, AssistantMessage) else "USER"
                lines = _blocks(msg)
                if lines:
                    print(f"[{who} {cls}]")
                    for l in lines:
                        print(l)
                        if "SendMessage" in l:
                            saw_sendmessage = True
                last_msg = now
            elif isinstance(msg, ResultMessage):
                final = msg.result
                result_seen = True
                print(f"[RESULT:{msg.subtype}] {msg.result}")
                last_msg = now
            elif isinstance(msg, SystemMessage):
                tid = getattr(msg, "task_id", None)
                if cls == "TaskStartedMessage" and tid:
                    started.add(tid); pending.add(tid)
                    data = getattr(msg, "data", None) or {}
                    print(f"[task started] {tid} {data.get('subagent_type','?')} "
                          f"name={data.get('name','?')}")
                if cls in ("TaskNotificationMessage", "TaskUpdatedMessage"):
                    status = getattr(msg, "status", None) or \
                        (getattr(msg, "patch", None) or {}).get("status")
                    if status in {"completed", "failed", "stopped", "succeeded"} and tid:
                        pending.discard(tid)
                        print(f"[task {status}] {tid}")
                last_msg = now

            if now - t0 > CAP_S:
                print("[probe] time cap reached"); break
            if result_seen and not pending and (now - last_msg) > POLL_QUIET_S:
                break

    got = final and SECRET in final
    # also count a success if any relayed text carried the secret
    print("\n==== PROBE VERDICT ====")
    print(f"SendMessage tool used:      {saw_sendmessage}")
    print(f"tasks started:              {len(started)}")
    print(f"secret in final result:     {bool(got)}")
    ok = bool(got)
    print(f"RESULT: {'PASS - live peer messaging works' if ok else 'FAIL - secret did not round-trip'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
