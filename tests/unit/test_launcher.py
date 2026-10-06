"""Launcher: settings.json schema, claude argv/env, prompt rendering and overrides."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from ads import launcher
from ads.config import load_config
from ads.paths import AGENTS


@pytest.fixture
def cfg(tmp_runtime: Path):
    return load_config(runtime=tmp_runtime)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_settings_schema(cfg, tmp_runtime: Path, project: Path) -> None:
    settings_path, prompt_path = launcher.render_agent_files(cfg, tmp_runtime, project, "planner")
    assert settings_path == tmp_runtime / "work/agents/planner/settings.json"
    assert prompt_path == tmp_runtime / "work/agents/planner/system-prompt.md"
    data = json.loads(settings_path.read_text())
    assert set(data) <= {"autoMemoryEnabled", "promptSuggestionEnabled", "env", "hooks"}
    assert data["autoMemoryEnabled"] is False
    assert data["promptSuggestionEnabled"] is False
    env = data["env"]
    assert env["ADS_AGENT"] == "planner"
    assert env["ADS_RUNTIME"] == str(tmp_runtime)
    assert env["ADS_PROJECT"] == str(project)
    assert Path(env["ADS_BIN"]).is_absolute()
    assert env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "0"
    assert "PATH" not in env
    assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"}
    subs = {"SessionStart": "session-start", "UserPromptSubmit": "prompt-submit", "Stop": "stop",
            "StopFailure": "stop-failure", "SessionEnd": "session-end"}
    for event, groups in data["hooks"].items():
        assert len(groups) == 1 and set(groups[0]) == {"hooks"}
        (hook,) = groups[0]["hooks"]
        assert set(hook) == {"type", "command", "timeout"}
        assert hook["type"] == "command"
        assert isinstance(hook["timeout"], int) and not isinstance(hook["timeout"], bool)
        exe, sub_cmd, sub = hook["command"].split(" ")
        assert Path(exe).is_absolute() and exe == env["ADS_BIN"]
        assert (sub_cmd, sub) == ("hook", subs[event])


def test_ads_bin_prefers_runtime_venv(tmp_runtime: Path) -> None:
    venv_bin = tmp_runtime / ".venv/bin"
    venv_bin.mkdir(parents=True)
    exe = venv_bin / "ads"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert launcher.ads_bin(tmp_runtime) == exe


def test_agent_env(cfg, tmp_runtime: Path, project: Path) -> None:
    env = launcher.agent_env(cfg, tmp_runtime, project, "coder-1", base_path="/usr/bin")
    assert env["ADS_AGENT"] == "coder-1"
    assert env["PATH"] == f"{Path(env['ADS_BIN']).parent}{os.pathsep}/usr/bin"
    assert env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"


def test_argv_fresh(cfg, tmp_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADS_CLAUDE_BIN", raising=False)
    sid = str(uuid.uuid4())
    argv = launcher.claude_argv(cfg, tmp_runtime, "planner", sid)
    d = tmp_runtime / "work/agents/planner"
    assert argv[0] == "claude"
    assert argv[argv.index("--model") + 1] == cfg.agents["planner"].model
    assert "--effort" not in argv
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--add-dir") + 1] == str(tmp_runtime)
    assert argv[argv.index("--settings") + 1] == str(d / "settings.json")
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(d / "system-prompt.md")
    i = argv.index("--disallowedTools")
    assert argv[i + 1:i + 4] == ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]
    assert argv[argv.index("--name") + 1] == "ads-planner"
    assert argv[-2:] == ["--session-id", sid]
    assert "--resume" not in argv


def test_argv_resume_effort_and_bin(cfg, tmp_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS_CLAUDE_BIN", "/x/fake_agent.py")
    cfg.agents["developer"] = replace(cfg.agents["developer"], effort="high")
    sid = str(uuid.uuid4())
    argv = launcher.claude_argv(cfg, tmp_runtime, "developer", sid, resume=True)
    assert argv[0] == "/x/fake_agent.py"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[-2:] == ["--resume", sid]
    assert "--session-id" not in argv


def test_session_uuid_persisted(tmp_runtime: Path) -> None:
    a = launcher.load_or_create_session_uuid(tmp_runtime, "tester")
    assert uuid.UUID(a).version == 4
    assert launcher.load_or_create_session_uuid(tmp_runtime, "tester") == a
    assert json.loads((tmp_runtime / "work/agents/tester/session.json").read_text())["session_id"] == a
    b = launcher.load_or_create_session_uuid(tmp_runtime, "tester", fresh=True)
    assert b != a and launcher.load_or_create_session_uuid(tmp_runtime, "tester") == b


def test_session_uuid_corrupt_file_replaced(tmp_runtime: Path) -> None:
    f = tmp_runtime / "work/agents/tester/session.json"
    f.parent.mkdir(parents=True)
    f.write_text("not json")
    assert uuid.UUID(launcher.load_or_create_session_uuid(tmp_runtime, "tester"))


def test_prompt_rendering_packaged(cfg, tmp_runtime: Path, project: Path) -> None:
    text = launcher.render_prompt(cfg, tmp_runtime, project, "planner")
    assert "You are **planner**" in text
    assert cfg.agents["planner"].model in text
    assert str(tmp_runtime) in text and str(project) in text
    assert f"{launcher.ads_bin(tmp_runtime)} send --to <sender>" in text
    assert "You report to **orchestrator**" in text
    for name in AGENTS:
        assert f"| {name}" in text
    assert "| planner (you) |" in text
    for ph in launcher.PLACEHOLDERS:
        assert "{" + ph + "}" not in text


def test_prompt_override_precedence(cfg, tmp_runtime: Path, project: Path) -> None:
    over = tmp_runtime / ".claude/ads/prompts"
    over.mkdir(parents=True)
    (over / "coder.md").write_text("CODER OVERRIDE for {agent} -> {reports_to}; keep {unknown} and {x: 1}")
    text = launcher.render_prompt(cfg, tmp_runtime, project, "coder-2")
    assert "You are **coder-2**" in text  # packaged common still used
    assert "CODER OVERRIDE for coder-2 -> developer; keep {unknown} and {x: 1}" in text
    (over / "common.md").write_text("COMMON OVERRIDE {agent}")
    text = launcher.render_prompt(cfg, tmp_runtime, project, "coder-2")
    assert text.startswith("COMMON OVERRIDE coder-2")
    assert "You are **" not in text
    assert "CODER OVERRIDE" in text


def test_role_of() -> None:
    assert launcher.role_of("coder-1") == "coder"
    assert launcher.role_of("orchestrator") == "orchestrator"
