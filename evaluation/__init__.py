"""
The experiment layer: run the robot system at scale against the offline simulation.

Three pieces sit on top of the sim (`robot_tools/sim/`) and the agent runtime, and nothing in
either has to change for them to work:

  task_list.json / tasks.py  the TASK LIST — each task pairs one scene with a GOAL. A scene is a
                             plain world (rooms, objects, a robot) and says nothing about what
                             should happen in it; the goal is the missing half. Same scene +
                             different goal = a different task.
  simulated_user.py          the SIMULATED USER (Claude Haiku) — holds the goal, is omniscient
                             about the scene, hears only what the robot `speak`s, and phrases
                             its requests the underspecified way people actually do.
  run_tasks.py               the RUNNER — one isolated episode per task, driving the same
                             architectures, steering and MCP tools an interactive session uses.

    .venv-agent/bin/python -m evaluation.run_tasks --list
    AGENT_ARCH=v4 .venv-agent/bin/python -m evaluation.run_tasks
"""
