"""Load and validate ads.toml (plan §7) into frozen dataclasses."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from ads.paths import AGENTS, HUMAN

EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(ValueError):
    """Invalid configuration or unresolvable runtime."""


@dataclass(frozen=True)
class AdsSection:
    tmux_socket: str = "ads"  # socket PREFIX: a project's cell runs on `tmux -L <prefix>-<name>`
    tmux_prefix: str = "C-a"
    attach: bool = True
    claude_bin: str = "claude"
    git_init: bool = True


@dataclass(frozen=True)
class AgentCfg:
    model: str
    reports_to: str
    effort: str | None = None


@dataclass(frozen=True)
class DeliveryCfg:
    tick_ms: int = 500
    paste_settle_ms: int = 150
    confirm_timeout_s: int = 10
    max_enter_retries: int = 2
    max_paste_retries: int = 3
    stale_busy_s: int = 600
    startup_timeout_s: int = 180
    dead_grace_s: int = 5


@dataclass(frozen=True)
class ProtocolCfg:
    max_review_rounds: int = 3
    max_report_nudges: int = 2


@dataclass(frozen=True)
class EditorCfg:
    vi_mode: bool = True
    history_file: str = "work/input_history"  # relative to the project state dir


DEFAULT_AGENTS: dict[str, AgentCfg] = {
    "orchestrator": AgentCfg("claude-opus-5-5", HUMAN),
    "planner": AgentCfg("claude-fable-5-1", "orchestrator"),
    "tester": AgentCfg("claude-sonnet-5-5", "orchestrator"),
    "evaluator": AgentCfg("claude-fable-5-1", "planner"),
    "developer": AgentCfg("claude-sonnet-5-5", "orchestrator"),
    "coder-1": AgentCfg("claude-sonnet-5-5", "developer"),
    "coder-2": AgentCfg("claude-sonnet-5-5", "developer"),
}


@dataclass(frozen=True)
class Config:
    ads: AdsSection
    agents: dict[str, AgentCfg]
    delivery: DeliveryCfg
    protocol: ProtocolCfg
    editor: EditorCfg
    source: Path | None = None  # file it was loaded from; None = built-in defaults


def default_config() -> Config:
    return Config(AdsSection(), dict(DEFAULT_AGENTS), DeliveryCfg(), ProtocolCfg(), EditorCfg())


# --- parsing -----------------------------------------------------------------

def _table(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a table")
    return raw


def _check_type(value: Any, expected: type, path: str) -> None:
    # bool is a subclass of int; keep them apart.
    ok = isinstance(value, expected) and not (expected is int and isinstance(value, bool))
    if not ok:
        raise ConfigError(f"{path}: expected {expected.__name__}, got {type(value).__name__}")


def _build[T](cls: type[T], raw: Any, path: str) -> T:
    """Build a flat dataclass from a TOML table: unknown keys, wrong types, non-positive ints are errors."""
    raw = _table(raw, path)
    known = {f.name: type(f.default) for f in fields(cls)}  # type: ignore[arg-type]
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"{path}.{key}: unknown key")
        _check_type(value, known[key], f"{path}.{key}")
        if known[key] is int and value <= 0:
            raise ConfigError(f"{path}.{key}: must be a positive integer, got {value}")
    return cls(**raw)


def _agents(raw: Any) -> dict[str, AgentCfg]:
    raw = _table(raw, "agents")
    names = set(raw)
    if names != set(AGENTS):
        missing = sorted(set(AGENTS) - names)
        extra = sorted(names - set(AGENTS))
        parts = []
        if extra:
            parts.append("unknown agent(s): " + ", ".join(f"agents.{n}" for n in extra))
        if missing:
            parts.append("missing agent(s): " + ", ".join(missing))
        raise ConfigError(f"agents: exactly {len(AGENTS)} agents required; " + "; ".join(parts))
    out: dict[str, AgentCfg] = {}
    for name in AGENTS:
        path = f"agents.{name}"
        tbl = _table(raw[name], path)
        for key in tbl:
            if key not in ("model", "effort", "reports_to"):
                raise ConfigError(f"{path}.{key}: unknown key")
        default = DEFAULT_AGENTS[name]
        model = tbl.get("model", default.model)
        _check_type(model, str, f"{path}.model")
        if not model.strip():
            raise ConfigError(f"{path}.model: must not be empty")
        reports_to = tbl.get("reports_to", default.reports_to)
        _check_type(reports_to, str, f"{path}.reports_to")
        if reports_to not in (*AGENTS, HUMAN) or reports_to == name:
            raise ConfigError(f"{path}.reports_to: invalid target {reports_to!r}")
        effort = tbl.get("effort")
        if effort is not None:
            _check_type(effort, str, f"{path}.effort")
            if effort not in EFFORTS:
                raise ConfigError(f"{path}.effort: {effort!r} not in {', '.join(EFFORTS)}")
        out[name] = AgentCfg(model=model, reports_to=reports_to, effort=effort)
    return out


SECTIONS = ("ads", "agents", "delivery", "protocol", "editor")


def parse_config(data: dict[str, Any], source: Path | None = None) -> Config:
    """Validate a parsed TOML document; missing sections/keys take defaults."""
    for key in data:
        if key not in SECTIONS:
            raise ConfigError(f"{key}: unknown key")
    return Config(
        ads=_build(AdsSection, data.get("ads", {}), "ads"),
        agents=_agents(data["agents"]) if "agents" in data else dict(DEFAULT_AGENTS),
        delivery=_build(DeliveryCfg, data.get("delivery", {}), "delivery"),
        protocol=_build(ProtocolCfg, data.get("protocol", {}), "protocol"),
        editor=_build(EditorCfg, data.get("editor", {}), "editor"),
        source=source,
    )


def load_config(path: Path | str | None = None, runtime: Path | str | None = None) -> Config:
    """Explicit path, else `<runtime>/ads.toml`, else built-in defaults."""
    if path is None and runtime is not None:
        candidate = Path(runtime) / "ads.toml"
        if candidate.is_file():
            path = candidate
    if path is None:
        return default_config()
    path = Path(path).expanduser().resolve()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: TOML syntax error: {e}") from None
    return parse_config(data, source=path)


def resolve_runtime(arg: Path | str | None = None, cwd: Path | None = None) -> Path:
    """--runtime, else $ADS_RUNTIME, else nearest dir with ads.toml walking up from cwd."""
    if arg:
        return _existing_dir(Path(arg), "--runtime")
    env = os.environ.get("ADS_RUNTIME")
    if env:
        return _existing_dir(Path(env), "$ADS_RUNTIME")
    start = (cwd or Path.cwd()).resolve()
    for d in (start, *start.parents):
        if (d / "ads.toml").is_file():
            return d
    raise ConfigError(
        f"cannot find the ads runtime: no ads.toml in {start} or its parents; "
        "pass --runtime or set $ADS_RUNTIME"
    )


def _existing_dir(p: Path, what: str) -> Path:
    p = p.expanduser().resolve()
    if not p.is_dir():
        raise ConfigError(f"{what}: not a directory: {p}")
    return p
