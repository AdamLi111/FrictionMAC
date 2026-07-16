"""
Shared fixtures. All tests run in STUB mode (no hardware, no agent) with tool-call log
and world-state file pointed at a per-test temp dir.
"""
import os

import pytest


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOT_STUB", "1")
    monkeypatch.delenv("MISTY_IP", raising=False)
    world_path = str(tmp_path / "world_state.json")
    log_path = str(tmp_path / "tool_calls.jsonl")
    monkeypatch.setenv("WORLD_STATE_PATH", world_path)
    monkeypatch.setenv("TOOL_LOG_PATH", log_path)

    from robot_tools import runtime
    runtime.reset()  # drop singletons so the temp paths / stub mode take effect
    yield {"world_path": world_path, "log_path": log_path, "tmp_path": tmp_path}
    runtime.reset()
