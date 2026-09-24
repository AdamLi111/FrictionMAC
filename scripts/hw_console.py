"""
Interactive robot console — a real conversation with Misty.

On screen you see ONLY your typed lines and Misty's spoken replies ("Misty: ..."). Each
session writes one transcript at the chosen LOG_LEVEL (INFO | DEBUG | FULL, default DEBUG) to
`data/hw_session_<ts>.<level>.log`.

One persistent session, so Misty remembers the conversation across turns. The turn-by-turn
machinery (one reader task, cross-turn task dedup, straggler draining) lives in
[agent_runtime/session.py](../agent_runtime/session.py) and is shared with the evaluation
harness; this file is just the human front end: banner, input source, and logging.

Run (real robot):    MISTY_IP=172.20.10.2 .venv-agent/bin/python -m scripts.hw_console
Run (dry, no robot): .venv-agent/bin/python -m scripts.hw_console

Type 'quit' (or Ctrl-C) to end.
"""
import os
import sys
import time

import anyio

from agent_runtime import (architectures, config, main as agent_main, narrative,
                           session as sess_mod)

QUIT = {"quit", "exit", "q", ":q"}
_LEVEL = {"INFO": 0, "DEBUG": 1, "FULL": 2}


class Logs:
    """One transcript at the chosen level (cumulative: INFO⊂DEBUG⊂FULL)."""
    def __init__(self, path, level):
        self.threshold = _LEVEL[level]
        self.f = open(path, "a", encoding="utf-8")

    def emit(self, level, text):
        if _LEVEL[level] <= self.threshold:
            self.f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            self.f.flush()

    def close(self):
        self.f.close()


async def run():
    config.load_env()
    config.DATA_DIR.mkdir(exist_ok=True)

    # Robot target: real @ DEFAULT_MISTY_IP by default (no MISTY_IP needed); --stub for offline.
    # Preflight the connection BEFORE opening the SDK client so an unreachable robot (e.g. wrong
    # Wi-Fi) aborts immediately without spending API tokens.
    ip = config.select_robot_target(stub="--stub" in sys.argv[1:])
    try:
        config.preflight_robot(ip)
    except config.RobotUnreachable as e:
        print(f"\n[abort] {e}")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")

    level = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    if level not in _LEVEL:
        print(f"(unknown LOG_LEVEL={level!r}; using DEBUG. Options: INFO, DEBUG, FULL)")
        level = "DEBUG"
    transcript = config.DATA_DIR / f"hw_session_{stamp}.{level.lower()}.log"
    story_path = config.DATA_DIR / f"hw_session_{stamp}_story.txt"
    tool_log = config.DATA_DIR / f"hw_session_{stamp}_tools.jsonl"
    world = config.belief_store_path()   # sim uses a separate per-scene file from the real robot
    os.environ["AGENT_EVENT_LOG"] = str(config.DATA_DIR / f"hw_session_{stamp}_events.jsonl")

    logs = Logs(transcript, level)

    arch = architectures.get(None)   # honors AGENT_ARCH, else the default variant
    sim_scene = os.environ.get("ROBOT_SIM_SCENE") if (
        os.environ.get("ROBOT_SIM") == "1" or os.environ.get("ROBOT_SIM_SCENE")) else None
    if sim_scene is not None or os.environ.get("ROBOT_SIM") == "1":
        mode = f"SIM (world model, scene={sim_scene or 'office_kitchen'})"
    elif ip:
        mode = f"REAL robot @ {ip}"
    else:
        mode = "STUB (no physical movement — omit --stub to use the real robot)"
    print(f"── Misty console ──  arch: {arch.name} ({arch.description})  |  "
          f"mode: {mode}  |  log level: {level}")

    # Optional voice input (VOICE=1) — needs the real robot. VOICE_LAPTOP_MIC=1 = laptop mic.
    voice = None
    if os.environ.get("VOICE") == "1":
        if ip is None:
            print("(VOICE=1 ignored — voice input needs the real Misty; running in stub/text mode)")
        else:
            from agent_runtime import speech
            use_laptop = os.environ.get("VOICE_LAPTOP_MIC") == "1"
            voice = speech.VoiceInput(ip, use_laptop_mic=use_laptop).start()
            src = "laptop mic" if use_laptop else "Misty's mic"
            print(f"── Voice input ON ({src}) — say '{speech.WAKE_WORD}' then your command; "
                  f"say 'quit' to end.")
    if voice is None:
        print("Type a command and press Enter. 'quit' to end.")
    print()
    logs.emit("INFO", f"[session start] arch={arch.name} mode={mode} level={level} "
                      f"voice={bool(voice)}")

    options = agent_main.build_options(tool_log, world, None, arch=arch)
    options.stderr = lambda line: logs.emit("FULL", f"[stderr] {line.rstrip()}")

    # The readable companion to the flat transcript — who thought what, nested under the
    # delegation it belongs to. Written live, so you can tail it while talking to the robot.
    story = narrative.NarrativeLog(story_path)
    story.header(f"MISTY SESSION {stamp}",
                 {"architecture": f"{arch.name} ({arch.description})", "mode": mode})

    # The console's only user-visible output: what Misty says.
    engine = sess_mod.TurnEngine(logs.emit,
                                 lambda text, friction: print(f"Misty: {text}"),
                                 trace=story.event, speech_log=tool_log)

    turns = 0                  # counted out here so the session footer can see it
    try:
        async with sess_mod.robot_session(options) as (client, recv):
            last_reply_at = None   # monotonic time the Director last finished a turn
            try:
                while True:
                    try:
                        if voice is not None:
                            # Block (in a worker thread) for the next spoken command, same
                            # place typed input would go.
                            cmd = (await anyio.to_thread.run_sync(voice.next_command) or "").strip()
                            if cmd:
                                print(f"you> {cmd}")
                        else:
                            cmd = (await anyio.to_thread.run_sync(lambda: input("you> "))).strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
                    if cmd.lower() in QUIT:
                        break
                    if not cmd:
                        continue
                    engine.drain_now(recv)           # clear inter-turn stragglers first
                    logs.emit("INFO", f"\n===== you: {cmd} =====")
                    turns += 1
                    story.user_turn(turns, cmd)
                    turn_t0 = time.monotonic()
                    await client.query(sess_mod.stamp_command(cmd, last_reply_at))
                    turn = await engine.collect_turn(recv)
                    last_reply_at = time.monotonic()
                    if not turn["spoke"]:
                        print("(no spoken reply)")
                        logs.emit("INFO", "  (no spoken reply)")
                        story.no_speech()
                    story.turn_end(subtype=turn["subtype"],
                                   seconds=time.monotonic() - turn_t0,
                                   timed_out=turn["timed_out"])
            finally:
                if voice is not None:
                    voice.cleanup()
    finally:
        logs.emit("INFO", "[session end]")
        logs.close()
        story.footer(f"SESSION ENDED — {turns} user turn(s).")
        story.close()
        print(f"\nSession ended.\n  transcript ({level}): {transcript}\n  readable: {story_path}")


if __name__ == "__main__":
    try:
        anyio.run(run)
    except KeyboardInterrupt:
        pass   # clean exit on Ctrl-C (e.g. while blocked waiting on voice input)
