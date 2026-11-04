"""
Tests for environment resolution (agent_runtime.config).

These exist because both failures here are silent and surface far from their cause: a wrong
interpreter path means the robot MCP server never starts, and the agents then fail with
"unrecognized [mcp__robot__speak]" — which says nothing about venvs. Pure stdlib, so these run
in either environment.
"""
import os
import sys

import pytest

from agent_runtime import config


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO", tmp_path)
    return tmp_path


def _make(venv_bin: str, exe: str, root):
    path = root / ".venv" / venv_bin / exe
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_finds_the_posix_interpreter(fake_repo, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    expected = _make("bin", "python", fake_repo)
    assert config.robot_python() == expected


def test_finds_the_windows_interpreter(fake_repo, monkeypatch):
    """A venv on Windows is `.venv\\Scripts\\python.exe` — the layout that broke a fresh clone."""
    monkeypatch.setattr(os, "name", "nt")
    expected = _make("Scripts", "python.exe", fake_repo)
    assert config.robot_python() == expected


def test_posix_falls_back_to_python3(fake_repo, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    expected = _make("bin", "python3", fake_repo)
    assert config.robot_python() == expected


def test_a_missing_robot_env_says_how_to_create_it(fake_repo, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(config.RobotEnvMissing) as caught:
        config.robot_python()
    message = str(caught.value)
    assert "robot environment is not installed" in message
    assert "python -m venv .venv" in message          # both platforms' commands are offered
    assert "Scripts\\pip" in message


def test_the_mcp_server_is_launched_with_that_interpreter(fake_repo, monkeypatch, tmp_path):
    """The whole point: this path becomes the MCP server's command line."""
    monkeypatch.setattr(os, "name", "posix")
    interpreter = _make("bin", "python", fake_repo)
    cfg = config.robot_mcp_config(tmp_path / "tools.jsonl", tmp_path / "world.json")
    assert cfg["command"] == str(interpreter)
    assert cfg["args"] == ["-m", "robot_tools.server"]


def test_missing_claude_cli_says_both_ways_to_get_one(monkeypatch):
    """Not on PATH and not bundled in the SDK (an old SDK, or none): say how to get one."""
    monkeypatch.setattr(config.shutil, "which", lambda _: None)
    monkeypatch.setattr(config, "_CLI_FALLBACKS", ("/definitely/not/here/claude",))
    monkeypatch.setattr(config, "bundled_cli", lambda: None)
    with pytest.raises(config.ClaudeCodeMissing) as caught:
        config.find_cli()
    message = str(caught.value)
    assert "pip install -U claude-agent-sdk" in message
    assert "npm install -g @anthropic-ai/claude-code" in message


def test_the_cli_bundled_in_the_sdk_is_used_when_none_is_installed(monkeypatch, tmp_path):
    """A machine without Node has no npm-installed CLI, but the SDK wheel carries one."""
    bundled = tmp_path / "claude"
    bundled.write_text("")
    monkeypatch.setattr(config.shutil, "which", lambda _: None)
    monkeypatch.setattr(config, "_CLI_FALLBACKS", ("/definitely/not/here/claude",))
    monkeypatch.setattr(config, "bundled_cli", lambda: bundled)
    assert config.find_cli() == str(bundled)


def test_an_installed_cli_wins_over_the_bundled_one(monkeypatch, tmp_path):
    monkeypatch.setattr(config.shutil, "which", lambda _: "/somewhere/bin/claude")
    monkeypatch.setattr(config, "bundled_cli", lambda: tmp_path / "claude")
    assert config.find_cli() == "/somewhere/bin/claude"


def test_robot_sessions_leave_out_the_accounts_claude_ai_connectors(fake_repo, monkeypatch,
                                                                   tmp_path):
    """setting_sources=[] keeps ambient settings out, but the logged-in account's claude.ai
    connectors need their own switch, or they load into every architecture's robot session."""
    pytest.importorskip("claude_agent_sdk")
    from agent_runtime import architectures
    monkeypatch.setattr(os, "name", "posix")
    _make("bin", "python", fake_repo)
    monkeypatch.setattr(config, "find_cli", lambda: "/somewhere/bin/claude")
    for name in architectures.available():
        opts = architectures.get(name).build_options(tmp_path / "t.jsonl", tmp_path / "w.json")
        assert opts.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false", name
        assert opts.env.get("PATH") == os.environ.get("PATH"), name   # added to, never replaced


def test_no_sdk_means_no_bundled_cli(monkeypatch):
    """The robot env has no SDK; looking for its bundled CLI must not raise there."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)   # makes the import fail
    assert config.bundled_cli() is None


def test_an_installed_cli_on_path_is_used_as_is(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda _: "/somewhere/bin/claude")
    assert config.find_cli() == "/somewhere/bin/claude"
