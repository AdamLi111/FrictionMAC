"""
Tests for environment resolution (agent_runtime.config).

These exist because both failures here are silent and surface far from their cause: a wrong
interpreter path means the robot MCP server never starts, and the agents then fail with
"unrecognized [mcp__robot__speak]" — which says nothing about venvs. Pure stdlib, so these run
in either environment.
"""
import os

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


def test_missing_claude_cli_explains_it_is_an_npm_install(monkeypatch):
    """`pip install claude-agent-sdk` does not provide the CLI the SDK shells out to."""
    monkeypatch.setattr(config.shutil, "which", lambda _: None)
    monkeypatch.setattr(config, "_CLI_FALLBACKS", ("/definitely/not/here/claude",))
    with pytest.raises(config.ClaudeCodeMissing) as caught:
        config.find_cli()
    assert "npm install -g @anthropic-ai/claude-code" in str(caught.value)


def test_an_installed_cli_on_path_is_used_as_is(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda _: "/somewhere/bin/claude")
    assert config.find_cli() == "/somewhere/bin/claude"
