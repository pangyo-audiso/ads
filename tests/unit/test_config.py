"""Config loading/validation, runtime resolution and paths helpers."""

from __future__ import annotations

import json
import re
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest

from ads.config import (ConfigError, default_config, load_config, parse_config,
                        resolve_runtime)
from ads.paths import (AGENTS, HUMAN, LAYOUT, OverlapError, check_overlap, locked_json,
                       session_name, slugify)

REPO = Path(__file__).resolve().parents[2]


def repo_toml() -> dict[str, Any]:
    with open(REPO / "ads.toml", "rb") as f:
        return tomllib.load(f)


# --- defaults --------------------------------------------------------------------

def test_repo_toml_equals_builtin_defaults() -> None:
    cfg = load_config(REPO / "ads.toml")
    default = default_config()
    assert cfg.source == (REPO / "ads.toml").resolve()
    for section in ("ads", "agents", "delivery", "protocol", "editor"):
        assert getattr(cfg, section) == getattr(default, section), section


def test_default_values() -> None:
    cfg = default_config()
    assert list(cfg.agents) == list(AGENTS)
    assert cfg.agents["orchestrator"].model == "claude-opus-5-5"
    assert cfg.agents["orchestrator"].reports_to == HUMAN
    assert cfg.agents["planner"].model == "claude-fable-5-1"
    assert cfg.agents["evaluator"].reports_to == "planner"
    assert cfg.agents["tester"].model == "claude-sonnet-5-5"
    assert all(a.effort is None for a in cfg.agents.values())
    assert cfg.ads.tmux_socket == "ads" and cfg.ads.tmux_prefix == "C-a"
    assert cfg.delivery.startup_timeout_s == 180 and cfg.delivery.tick_ms == 500
    assert cfg.protocol.max_review_rounds == 3 and cfg.protocol.max_report_nudges == 2
    assert cfg.editor.vi_mode is True and cfg.editor.history_file == "work/input_history"


def test_missing_sections_take_defaults() -> None:
    cfg = parse_config({"delivery": {"tick_ms": 250}})
    assert cfg.delivery.tick_ms == 250
    assert cfg.delivery.paste_settle_ms == 150
    assert cfg.agents == default_config().agents


def test_effort_and_model_override() -> None:
    data = repo_toml()
    data["agents"]["planner"]["effort"] = "xhigh"
    data["agents"]["coder-1"]["model"] = "claude-haiku-5"
    cfg = parse_config(data)
    assert cfg.agents["planner"].effort == "xhigh"
    assert cfg.agents["coder-1"].model == "claude-haiku-5"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_effort_enum_valid(effort: str) -> None:
    data = repo_toml()
    data["agents"]["tester"]["effort"] = effort
    assert parse_config(data).agents["tester"].effort == effort


def test_config_is_frozen() -> None:
    cfg = default_config()
    with pytest.raises(AttributeError):
        cfg.delivery.tick_ms = 1  # type: ignore[misc]


# --- validation errors (table-driven) ------------------------------------------------

def _mut(fn):  # small helper so the table reads as data
    def apply() -> dict[str, Any]:
        data = repo_toml()
        fn(data)
        return data
    return apply


ERROR_CASES = [
    ("unknown agent key", _mut(lambda d: d["agents"]["planner"].update(modle="x")),
     r"agents\.planner\.modle: unknown key"),
    ("unknown section", _mut(lambda d: d.update(extra={})), r"^extra: unknown key"),
    ("unknown ads key", _mut(lambda d: d["ads"].update(tmux_sock="x")), r"ads\.tmux_sock: unknown key"),
    ("unknown delivery key", _mut(lambda d: d["delivery"].update(tick=1)), r"delivery\.tick: unknown key"),
    ("unknown editor key", _mut(lambda d: d["editor"].update(emacs=True)), r"editor\.emacs: unknown key"),
    ("missing agent", _mut(lambda d: d["agents"].pop("coder-2")), r"missing agent\(s\): coder-2"),
    ("extra agent", _mut(lambda d: d["agents"].update({"coder-3": {"model": "m", "reports_to": "developer"}})),
     r"unknown agent\(s\): agents\.coder-3"),
    ("bad reports_to", _mut(lambda d: d["agents"]["tester"].update(reports_to="boss")),
     r"agents\.tester\.reports_to: invalid target 'boss'"),
    ("self reports_to", _mut(lambda d: d["agents"]["tester"].update(reports_to="tester")),
     r"agents\.tester\.reports_to"),
    ("bad effort", _mut(lambda d: d["agents"]["planner"].update(effort="ultra")),
     r"agents\.planner\.effort: 'ultra' not in"),
    ("effort type", _mut(lambda d: d["agents"]["planner"].update(effort=3)),
     r"agents\.planner\.effort: expected str"),
    ("empty model", _mut(lambda d: d["agents"]["planner"].update(model=" ")),
     r"agents\.planner\.model: must not be empty"),
    ("zero bound", _mut(lambda d: d["delivery"].update(tick_ms=0)),
     r"delivery\.tick_ms: must be a positive integer"),
    ("negative bound", _mut(lambda d: d["protocol"].update(max_report_nudges=-1)),
     r"protocol\.max_report_nudges: must be a positive integer"),
    ("int as str", _mut(lambda d: d["delivery"].update(stale_busy_s="600")),
     r"delivery\.stale_busy_s: expected int, got str"),
    ("bool as int", _mut(lambda d: d["delivery"].update(dead_grace_s=True)),
     r"delivery\.dead_grace_s: expected int, got bool"),
    ("bool type", _mut(lambda d: d["ads"].update(attach="yes")), r"ads\.attach: expected bool"),
    ("str type", _mut(lambda d: d["ads"].update(tmux_prefix=1)), r"ads\.tmux_prefix: expected str"),
    ("section not table", _mut(lambda d: d.update(delivery=5)), r"delivery: expected a table"),
]


@pytest.mark.parametrize("name,make,pattern", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_validation_errors(name: str, make, pattern: str) -> None:
    with pytest.raises(ConfigError, match=pattern):
        parse_config(make())


def test_toml_syntax_error(tmp_path: Path) -> None:
    p = tmp_path / "ads.toml"
    p.write_text("[ads\n")
    with pytest.raises(ConfigError, match="TOML syntax error"):
        load_config(p)


def test_explicit_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


# --- lookup and runtime resolution ---------------------------------------------------

def test_load_from_runtime(tmp_runtime: Path) -> None:
    (tmp_runtime / "ads.toml").write_text(
        (tmp_runtime / "ads.toml").read_text().replace("tick_ms = 500", "tick_ms = 777"))
    assert load_config(runtime=tmp_runtime).delivery.tick_ms == 777


def test_explicit_path_wins(tmp_runtime: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.toml"
    other.write_text("[delivery]\ntick_ms = 42\n")
    assert load_config(other, runtime=tmp_runtime).delivery.tick_ms == 42


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(runtime=tmp_path)
    assert cfg.source is None and cfg == default_config()


def test_resolve_runtime_arg_beats_env(tmp_runtime: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("ADS_RUNTIME", str(env_dir))
    assert resolve_runtime(str(tmp_runtime)) == tmp_runtime.resolve()
    assert resolve_runtime(None) == env_dir.resolve()


def test_resolve_runtime_walks_up(tmp_runtime: Path) -> None:
    deep = tmp_runtime / "a" / "b"
    deep.mkdir(parents=True)
    assert resolve_runtime(None, cwd=deep) == tmp_runtime.resolve()


def test_resolve_runtime_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADS_RUNTIME", raising=False)
    with pytest.raises(ConfigError, match="cannot find the ads runtime"):
        resolve_runtime(None, cwd=tmp_path)


def test_resolve_runtime_bad_arg(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--runtime: not a directory"):
        resolve_runtime(str(tmp_path / "missing"))


# --- paths helpers --------------------------------------------------------------

@pytest.mark.parametrize("project,runtime,ok", [
    ("/a/rt", "/a/rt", False),           # equal
    ("/a/rt/proj", "/a/rt", False),      # project inside runtime
    ("/a", "/a/rt", False),              # runtime inside project
    ("/a/proj", "/a/rt", True),          # siblings
    ("/a/rt2", "/a/rt", True),           # common prefix, not nested
])
def test_check_overlap(project: str, runtime: str, ok: bool) -> None:
    if ok:
        check_overlap(project, runtime)
    else:
        with pytest.raises(OverlapError):
            check_overlap(project, runtime)


def test_check_overlap_resolves_symlinks(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    rt.mkdir()
    link = tmp_path / "link"
    link.symlink_to(rt)
    with pytest.raises(OverlapError):
        check_overlap(link, rt)


@pytest.mark.parametrize("text,slug", [
    ("My Project", "my-project"), ("ads-demo", "ads-demo"), ("__x__", "x"), ("한글", "x"), ("", "x"),
])
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug


def test_session_name() -> None:
    name = session_name("/tmp/ads-demo")
    assert re.fullmatch(r"ads-ads-demo-[0-9a-f]{6}", name)
    assert session_name("/tmp/ads-demo/") == name
    assert session_name("/other/ads-demo") != name


def test_layout() -> None:
    assert set(LAYOUT) == {*AGENTS, HUMAN}
    assert LAYOUT[HUMAN] == (0, 3)
    assert LAYOUT["orchestrator"] == (0, 0) and LAYOUT["evaluator"] == (1, 0)
    assert len(set(LAYOUT.values())) == 8


def test_locked_json_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "state.json"
    with locked_json(p) as d:
        assert d == {}
        d["state"] = "idle"
        d["subject"] = "naïve \"quotes\"\n"
    assert json.loads(p.read_text()) == {"state": "idle", "subject": "naïve \"quotes\"\n"}
    with locked_json(p) as d:
        assert d["state"] == "idle"
    assert not list(p.parent.glob("*.tmp"))


def test_locked_json_no_write_on_error(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    with locked_json(p) as d:
        d["n"] = 1
    with pytest.raises(RuntimeError):
        with locked_json(p) as d:
            d["n"] = 2
            raise RuntimeError
    assert json.loads(p.read_text()) == {"n": 1}


def test_locked_json_concurrent_increments(tmp_path: Path) -> None:
    p = tmp_path / "counter.json"
    threads, per_thread = 8, 50

    def work() -> None:
        for _ in range(per_thread):
            with locked_json(p) as d:
                d["n"] = d.get("n", 0) + 1

    ts = [threading.Thread(target=work) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert json.loads(p.read_text())["n"] == threads * per_thread
