"""Per-agent Claude Code launch files, argv and env (plan §4.1). Pure apart from writing work/agents/<a>/."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from ads.config import Config
from ads.paths import AGENTS, Runtime

# hook event -> (ads hook sub-event, timeout seconds)
HOOKS: dict[str, tuple[str, int]] = {
    "SessionStart": ("session-start", 10),
    "UserPromptSubmit": ("prompt-submit", 10),
    "Stop": ("stop", 15),
    "StopFailure": ("stop-failure", 5),
    "SessionEnd": ("session-end", 1),
}

DISALLOWED_TOOLS = ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode")
PLACEHOLDERS = ("agent", "role", "model", "runtime", "project", "ads_bin", "reports_to", "peers",
                "max_review_rounds")


def _rt(runtime: Runtime | Path | str) -> Runtime:
    return runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime).expanduser().absolute())


def role_of(agent: str) -> str:
    """`coder-1` -> `coder`; other agents are their own role."""
    return re.sub(r"-\d+$", "", agent)


def ads_bin(runtime: Runtime | Path | str) -> Path:
    """Absolute path of the `ads` executable used in hook commands.

    Order: `<runtime>/.venv/bin/ads`, sibling of sys.executable, `shutil.which("ads")`.
    """
    rt = _rt(runtime)
    candidates = [rt.root / ".venv" / "bin" / "ads", Path(sys.executable).absolute().parent / "ads"]
    which = shutil.which("ads")
    if which:
        candidates.append(Path(which).absolute())
    for c in candidates:
        if os.access(c, os.X_OK):
            return c
    return candidates[0]


def claude_bin(cfg: Config) -> str:
    return os.environ.get("ADS_CLAUDE_BIN") or cfg.ads.claude_bin


def agent_env(cfg: Config, runtime: Runtime | Path | str, project: Path | str, agent: str,
              base_path: str | None = None) -> dict[str, str]:
    """Environment variables added to the agent process (and mirrored into settings.json `env`)."""
    rt = _rt(runtime)
    bin_ = ads_bin(rt)
    path = os.environ.get("PATH", "") if base_path is None else base_path
    return {
        "ADS_AGENT": agent,
        "ADS_RUNTIME": str(rt.root),
        "ADS_PROJECT": str(Path(project).expanduser().absolute()),
        "ADS_BIN": str(bin_),
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # no dim "prompt suggestion" ghost text in the input box (M7 E2E): it costs a model
        # call per turn and shows up in captures of the input box the supervisor reads
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "0",
        "PATH": f"{bin_.parent}{os.pathsep}{path}" if path else str(bin_.parent),
    }


def settings_dict(cfg: Config, runtime: Runtime | Path | str, project: Path | str, agent: str) -> dict:
    """The exact settings.json document: autoMemoryEnabled, promptSuggestionEnabled, env, hooks."""
    rt = _rt(runtime)
    env = agent_env(cfg, rt, project, agent)
    env.pop("PATH")
    bin_ = ads_bin(rt)
    hooks = {
        event: [{"hooks": [{"type": "command", "command": f"{bin_} hook {sub}", "timeout": t}]}]
        for event, (sub, t) in HOOKS.items()
    }
    return {"autoMemoryEnabled": False, "promptSuggestionEnabled": False, "env": env,
            "hooks": hooks}


# --- prompts -------------------------------------------------------------------

def _packaged_prompt(name: str) -> str | None:
    try:
        return resources.files("ads").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def load_prompt(runtime: Runtime | Path | str, name: str) -> str | None:
    """Override `<runtime>/.claude/ads/prompts/<name>.md` wins over packaged `ads/prompts/<name>.md`."""
    override = _rt(runtime).root / ".claude" / "ads" / "prompts" / f"{name}.md"
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return _packaged_prompt(name)


def peers_table(cfg: Config, agent: str) -> str:
    rows = ["| agent | model | reports to |", "|---|---|---|"]
    for name in AGENTS:
        a = cfg.agents[name]
        me = " (you)" if name == agent else ""
        rows.append(f"| {name}{me} | {a.model} | {a.reports_to} |")
    return "\n".join(rows)


def render_text(text: str, values: dict[str, str]) -> str:
    """Replace only known `{placeholder}`s; other braces are left untouched."""
    return re.sub(r"\{(" + "|".join(values) + r")\}", lambda m: values[m[1]], text)


def render_prompt(cfg: Config, runtime: Runtime | Path | str, project: Path | str, agent: str) -> str:
    rt = _rt(runtime)
    a = cfg.agents[agent]
    values = {
        "agent": agent,
        "role": role_of(agent),
        "model": a.model,
        "runtime": str(rt.root),
        "project": str(Path(project).expanduser().absolute()),
        "ads_bin": str(ads_bin(rt)),
        "reports_to": a.reports_to,
        "peers": peers_table(cfg, agent),
        "max_review_rounds": str(cfg.protocol.max_review_rounds),
    }
    parts = [p for p in (load_prompt(rt, "common"), load_prompt(rt, role_of(agent))) if p]
    return render_text("\n\n".join(p.strip() for p in parts) + "\n", values)


# --- files ----------------------------------------------------------------------

def render_agent_files(cfg: Config, runtime: Runtime | Path | str, project: Path | str,
                       agent: str) -> tuple[Path, Path]:
    """Write work/agents/<a>/{settings.json,system-prompt.md}; return their paths."""
    rt = _rt(runtime)
    d = rt.agent_dir(agent)
    d.mkdir(parents=True, exist_ok=True)
    settings_path = d / "settings.json"
    prompt_path = d / "system-prompt.md"
    settings_path.write_text(json.dumps(settings_dict(cfg, rt, project, agent), indent=1) + "\n")
    prompt_path.write_text(render_prompt(cfg, rt, project, agent), encoding="utf-8")
    return settings_path, prompt_path


def session_file(runtime: Runtime | Path | str, agent: str) -> Path:
    return _rt(runtime).agent_dir(agent) / "session.json"


def load_or_create_session_uuid(runtime: Runtime | Path | str, agent: str, fresh: bool = False) -> str:
    """Return the agent's persisted Claude session uuid, creating (or with fresh=True, replacing) it."""
    path = session_file(runtime, agent)
    if not fresh:
        try:
            sid = json.loads(path.read_text()).get("session_id")
            if sid:
                return str(uuid.UUID(sid))
        except (FileNotFoundError, ValueError, AttributeError):
            pass
    sid = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": sid,
                                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    return sid


def claude_argv(cfg: Config, runtime: Runtime | Path | str, agent: str, session_uuid: str,
                resume: bool = False) -> list[str]:
    """Full claude argv (cwd must be the project dir)."""
    rt = _rt(runtime)
    a = cfg.agents[agent]
    d = rt.agent_dir(agent)
    argv = [claude_bin(cfg), "--model", a.model]
    if a.effort:
        argv += ["--effort", a.effort]
    argv += [
        "--dangerously-skip-permissions",
        "--add-dir", str(rt.root),
        "--settings", str(d / "settings.json"),
        "--append-system-prompt-file", str(d / "system-prompt.md"),
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--name", f"ads-{agent}",
    ]
    argv += ["--resume", session_uuid] if resume else ["--session-id", session_uuid]
    return argv
