"""Multi-project runtime: registry/naming, bare names, selection order, per-project isolation."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from ads import cli, launcher
from ads import projects as P
from ads.bus import ledger, store
from ads.bus import state as S
from ads.config import default_config
from ads.paths import ProjectState


@pytest.fixture
def runtime(tmp_runtime: Path, monkeypatch) -> Path:
    monkeypatch.setenv("ADS_RUNTIME", str(tmp_runtime))
    monkeypatch.delenv("ADS_AGENT", raising=False)
    return tmp_runtime


def _proj(tmp_path: Path, *parts: str) -> Path:
    p = tmp_path.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


# --- registry / naming -----------------------------------------------------------------------

def test_register_writes_project_json_and_is_stable(runtime: Path, tmp_path: Path) -> None:
    proj = _proj(tmp_path, "work", "My Project")
    st = P.register(runtime, proj)
    assert st.name == "my-project" and st.dir == runtime / "projects" / "my-project"
    info = json.loads(st.project_json.read_text())
    assert info["name"] == "my-project" and info["path"] == str(proj.resolve())
    assert info["created"]
    # same path (also spelled differently) -> same state dir, created kept
    again = P.register(runtime, tmp_path / "work" / "x" / ".." / "My Project")
    assert again == st and json.loads(st.project_json.read_text())["created"] == info["created"]
    assert P.find_by_path(runtime, proj) == st
    assert P.all_projects(runtime) == [st]


def test_name_collision_gets_hashed_name(runtime: Path, tmp_path: Path) -> None:
    a = _proj(tmp_path, "a", "app")
    b = _proj(tmp_path, "b", "app")
    sa = P.register(runtime, a)
    sb = P.register(runtime, b)
    digest = hashlib.sha1(str(b.resolve()).encode()).hexdigest()[:6]
    assert sa.name == "app" and sb.name == f"app-{digest}"
    # lookups stay by path: each project always maps to its own dir
    assert P.register(runtime, a) == sa and P.register(runtime, b) == sb
    assert P.find_by_path(runtime, b) == sb
    assert {s.name for s in P.all_projects(runtime)} == {"app", f"app-{digest}"}


def test_is_bare_name_and_resolution(runtime: Path, tmp_path: Path) -> None:
    for bare in ("audiso-rag", "x", ".hidden", "a.b"):
        assert P.is_bare_name(bare), bare
    for path in ("./x", "../x", ".", "..", "~/x", "~", "/abs/x", "a/b", ""):
        assert not P.is_bare_name(path), path
    assert P.resolve_project_arg(runtime, "audiso-rag", cwd=Path("/somewhere")) == \
        runtime.parent / "audiso-rag"
    assert P.resolve_project_arg(runtime, "./audiso-rag", cwd=tmp_path / "c") == \
        tmp_path / "c" / "audiso-rag"
    assert P.resolve_project_arg(runtime, "../q", cwd=tmp_path / "c") == tmp_path / "q"
    assert P.resolve_project_arg(runtime, "~/zz") == Path("~/zz").expanduser().resolve()


# --- selection order -----------------------------------------------------------------------------

@pytest.fixture
def two(runtime: Path, tmp_path: Path) -> tuple[ProjectState, ProjectState]:
    a = P.register(runtime, _proj(tmp_path, "alpha"))
    b = P.register(runtime, _proj(tmp_path, "beta"))
    return a, b


def test_select_explicit_name_or_path(runtime: Path, two, tmp_path: Path) -> None:
    a, b = two
    env = {"ADS_STATE_DIR": str(b.dir)}
    assert P.select(runtime, "alpha", env) == a                       # -p beats env
    assert P.select(runtime, str(tmp_path / "alpha"), env) == a       # by path
    assert P.select(runtime, "beta", {}) == b
    with pytest.raises(P.ProjectError, match="unknown project 'nope'.*alpha.*beta"):
        P.select(runtime, "nope", {})


def test_select_env_then_cwd(runtime: Path, two, tmp_path: Path, monkeypatch) -> None:
    a, b = two
    deep = _proj(tmp_path, "alpha", "src", "pkg")
    assert P.select(runtime, None, {"ADS_STATE_DIR": str(b.dir)}, cwd=deep) == b   # env > cwd
    assert P.select(runtime, None, {"ADS_PROJECT": str(tmp_path / "beta")}, cwd=deep) == b
    assert P.select(runtime, None, {}, cwd=deep) == a                              # cwd inside
    assert P.select(runtime, None, {}, cwd=tmp_path / "beta") == b


def test_select_single_running_else_error(runtime: Path, two, tmp_path: Path,
                                          monkeypatch) -> None:
    a, b = two
    elsewhere = _proj(tmp_path, "elsewhere")
    with pytest.raises(P.ProjectError, match="no project selected.*-p.*alpha.*beta"):
        P.select(runtime, None, {}, cwd=elsewhere)
    monkeypatch.setattr(P, "is_running", lambda st: st == b)
    assert P.select(runtime, None, {}, cwd=elsewhere) == b
    monkeypatch.setattr(P, "is_running", lambda st: True)
    with pytest.raises(P.ProjectError, match="several projects are running"):
        P.select(runtime, None, {}, cwd=elsewhere)


def test_select_only_registered_project(runtime: Path, tmp_path: Path) -> None:
    a = P.register(runtime, _proj(tmp_path, "solo"))
    assert P.select(runtime, None, {}, cwd=_proj(tmp_path, "elsewhere")) == a


def test_cli_project_flag_and_env(runtime: Path, two, tmp_path: Path, monkeypatch,
                                  capsys) -> None:
    a, b = two
    monkeypatch.chdir(_proj(tmp_path, "elsewhere"))
    assert cli.main(["status", "--json"]) == 1
    assert "no project selected" in capsys.readouterr().err
    assert cli.main(["status", "--json", "-p", "beta"]) == 0
    assert json.loads(capsys.readouterr().out)["project"]["name"] == "beta"
    assert cli.main(["-p", "alpha", "status", "--json"]) == 0      # leading -p is moved
    assert json.loads(capsys.readouterr().out)["project"]["state_dir"] == str(a.dir)
    monkeypatch.setenv("ADS_STATE_DIR", str(b.dir))
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"]["name"] == "beta"
    monkeypatch.delenv("ADS_STATE_DIR")
    monkeypatch.chdir(tmp_path / "alpha")
    assert cli.main(["note", "alpha lesson"]) == 0
    assert "alpha lesson" in a.claude_md.read_text()
    assert not b.claude_md.exists() or "alpha lesson" not in b.claude_md.read_text()


# --- isolation -------------------------------------------------------------------------------------

def test_projects_are_isolated(runtime: Path, two, monkeypatch, capsys) -> None:
    a, b = two
    cfg = default_config()
    a.ensure()
    b.ensure()
    m = ledger.send(a, cfg, from_="human", to="orchestrator", type="instruct", subject="A only",
                    body="x")
    S.transition(a, "planner", "respawn")
    assert [x.id for x in store.all_messages(a)] == [m.id]
    assert store.all_messages(b) == []
    assert len(ledger.open_tasks(a)) == 1 and ledger.open_tasks(b) == []
    assert S.read_state(a, "planner")["state"] == "starting"
    assert S.read_state(b, "planner")["state"] == "down"
    # seq counters are per project: B's first message is also #1
    mb = ledger.send(b, cfg, from_="human", to="orchestrator", type="instruct", subject="B",
                     body="y")
    assert mb.seq == 1 and m.seq == 1
    assert store.body_path(a, m.id).is_relative_to(a.dir)
    # status via the CLI shows each project's own data
    assert cli.main(["status", "--json", "-p", "alpha"]) == 0
    da = json.loads(capsys.readouterr().out)
    assert [t["subject"] for t in da["tasks"]] == ["A only"]
    assert cli.main(["status", "--json", "-p", "beta"]) == 0
    assert [t["subject"] for t in json.loads(capsys.readouterr().out)["tasks"]] == ["B"]
    # ads send into one project does not touch the other
    assert cli.main(["send", "-p", "beta", "--from", "human", "--to", "planner", "--type",
                     "info", "--subject", "hi", "--body", "b"]) == 0
    capsys.readouterr()
    assert len(store.all_messages(b)) == 2 and len(store.all_messages(a)) == 1


def test_resume_uuids_are_per_project(runtime: Path, two) -> None:
    a, b = two
    ua = launcher.load_or_create_session_uuid(a, "planner")
    ub = launcher.load_or_create_session_uuid(b, "planner")
    assert ua != ub
    assert launcher.session_file(a, "planner") == a.dir / "work/agents/planner/session.json"
    cfg = default_config()
    argv = launcher.claude_argv(cfg, a, "planner", launcher.load_or_create_session_uuid(a, "planner"),
                                resume=True)
    assert argv[-2:] == ["--resume", ua]
    assert uuid.UUID(json.loads(launcher.session_file(b, "planner").read_text())["session_id"]) \
        == uuid.UUID(ub)


# --- list / stop --all without cells ------------------------------------------------------------

def test_list_and_stop_all_idle_runtime(runtime: Path, two, capsys) -> None:
    a, b = two
    cfg = default_config()
    ledger.send(a, cfg, from_="orchestrator", to="planner", type="instruct", subject="t",
                body="x")
    assert cli.main(["list", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert set(rows) == {"alpha", "beta"}
    assert rows["alpha"]["open_tasks"] == 1 and rows["beta"]["open_tasks"] == 0
    assert rows["alpha"]["phase"] == "plan" and rows["beta"]["phase"] is None
    assert rows["alpha"]["running"] is False
    assert rows["alpha"]["socket"].endswith("-alpha")
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["NAME", "PATH", "RUNNING", "SOCKET", "PHASE", "OPEN",
                                           "TASKS"]
    assert cli.main(["stop", "--all"]) == 0
    assert "no running ads cells" in capsys.readouterr().out
    assert cli.main(["stop", "--all", "-p", "alpha"]) == 1


def test_hook_env_fallback_runtime_plus_project(runtime: Path, two, tmp_path: Path) -> None:
    import io
    from ads import hooks
    a, _ = two
    code = hooks.main("session-end", stdin=io.StringIO('{"reason": "logout"}'),
                      stdout=io.StringIO(),
                      env={"ADS_RUNTIME": str(runtime), "ADS_PROJECT": str(tmp_path / "alpha"),
                           "ADS_AGENT": "planner"})
    assert code == 0 and S.read_state(a, "planner")["reason"] == "logout"
