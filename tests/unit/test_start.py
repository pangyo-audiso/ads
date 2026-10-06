"""`ads start|stop|attach` helpers (M5, plan §10)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ads import cli
from ads import start as S
from ads.config import default_config
from ads.paths import ProjectState


@pytest.fixture
def rt(tmp_runtime: Path, monkeypatch) -> Path:
    """The runtime root (ADS_RUNTIME set)."""
    monkeypatch.setenv("ADS_RUNTIME", str(tmp_runtime))
    return tmp_runtime


def test_normalize_bare_project() -> None:
    assert cli.normalize_argv(["/tmp/x", "--yes", "--no-attach"]) == \
        ["start", "/tmp/x", "--yes", "--no-attach"]
    args = cli.build_parser().parse_args(cli.normalize_argv(["/tmp/x", "--restart", "-y"]))
    assert args.cmd == "start" and args.restart and args.yes and not args.attach


def test_attach_restart_exclusive(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["start", "/tmp/x", "--attach", "--restart"])


def test_missing_project_declined(tmp_path, monkeypatch) -> None:
    out: list[str] = []
    p = tmp_path / "new"
    assert S.ensure_project(p, default_config(), yes=False, out=out.append,
                            asker=lambda q: "n") is False
    assert not p.exists() and "Not created" in out[-1]


def test_missing_project_non_tty_is_no(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(S, "_isatty", lambda: False)
    out: list[str] = []
    p = tmp_path / "new"
    assert S.ensure_project(p, default_config(), yes=False, out=out.append) is False
    assert not p.exists() and "--yes" in out[-1]


def test_missing_project_yes_creates_and_git_inits(tmp_path) -> None:
    out: list[str] = []
    p = tmp_path / "a" / "b"
    assert S.ensure_project(p, default_config(), yes=True, out=out.append) is True
    assert (p / "docs").is_dir() and (p / ".git").is_dir()


def test_missing_project_asked_yes(tmp_path) -> None:
    questions: list[str] = []
    p = tmp_path / "new"
    ok = S.ensure_project(p, default_config(), yes=False, out=lambda s: None,
                          asker=lambda q: questions.append(q) or "y")
    assert ok and p.is_dir()
    assert questions == [f"Project folder {p} does not exist. Create it? [y/N] "]


def test_existing_project_no_git_init(tmp_path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    assert S.ensure_project(p, default_config(), yes=False, out=lambda s: None)
    assert (p / "docs").is_dir() and not (p / ".git").exists()


def test_project_is_file(tmp_path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(S.StartError):
        S.ensure_project(f, default_config(), yes=True)


def test_claude_md_lab_notes(tmp_path) -> None:
    md = tmp_path / "CLAUDE.md"
    assert S.ensure_claude_md(md) is True
    assert "## Lab Notes" in md.read_text()
    assert S.ensure_claude_md(md) is False
    md.write_text("# Mine\n\nkeep me")
    assert S.ensure_claude_md(md) is True
    assert md.read_text() == "# Mine\n\nkeep me\n\n## Lab Notes\n"
    md.write_text("x\n## Lab Notes\n- [d a] n\n")
    assert S.ensure_claude_md(md) is False


def test_gitignore(tmp_path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("work\n*.pyc")
    assert S.ensure_gitignore(gi) == ["projects/", ".venv/"]
    assert gi.read_text() == "work\n*.pyc\nprojects/\n.venv/\n"
    assert S.ensure_gitignore(gi) == []


def test_ensure_runtime_and_state(rt: Path, tmp_path: Path) -> None:
    S.ensure_runtime(rt)
    assert (rt / "projects").is_dir() and "projects/" in (rt / ".gitignore").read_text()
    st = ProjectState.of(rt, "demo")
    S.ensure_state(st, tmp_path / "demo")
    for d in (st.plan / "drafts", st.run, st.msgs, st.logs, st.reviews, st.agent_dir("coder-2")):
        assert d.is_dir() and d.is_relative_to(rt / "projects" / "demo")
    text = st.claude_md.read_text()
    assert text.startswith("# ads project memory: demo\n")
    assert str(tmp_path / "demo") in text and "shared memory" in text
    assert text.rstrip().endswith("## Lab Notes")
    assert not (rt / "CLAUDE.md").exists() and not (rt / "work").exists()


def test_supervisor_pid(rt: Path) -> None:
    rt = ProjectState.of(rt, "demo")
    rt.ensure()
    assert S.supervisor_pid(rt) is None
    rt.supervisor_pid.write_text("999999999\n")
    assert S.supervisor_pid(rt) is None
    S.clear_stale_pid(rt)
    assert rt.supervisor_pid.read_text() == ""
    rt.supervisor_pid.write_text(f"{os.getpid()}\n")  # alive, but not a supervisor (pytest)
    assert S.supervisor_pid(rt) is None


def test_start_overlap_rejected(rt: Path, capsys) -> None:
    code = cli.main(["start", str(rt / "sub"), "--yes", "--no-attach"])
    assert code == 1 and "inside the runtime" in capsys.readouterr().err


def test_start_declined_exit0(rt: Path, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(S, "_isatty", lambda: False)
    p = tmp_path / "nope"
    assert cli.main([str(p), "--no-attach"]) == 0
    assert not p.exists()
    assert f"project: {p}" in capsys.readouterr().out
    assert not (rt / "projects" / "nope").exists()  # nothing registered when declined


def test_start_bare_name_is_sibling_of_runtime(rt: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(S, "_isatty", lambda: False)
    monkeypatch.chdir(rt)  # a bare name is NOT relative to the cwd
    assert cli.main(["audiso-rag", "--no-attach"]) == 0
    out = capsys.readouterr().out
    sibling = rt.parent / "audiso-rag"
    lines = out.splitlines()
    assert lines[0] == f"project: {sibling}"
    assert f"Project folder {sibling} does not exist" in out
    # a path with a slash keeps the cwd-relative behaviour (and is rejected inside the runtime)
    assert cli.main(["./audiso-rag", "--no-attach"]) == 1
    assert "inside the runtime" in capsys.readouterr().err


def test_preflight_aborts_on_fail(rt: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "doctor_rows", lambda *a, **k: [
        (cli.PASS, "x", "ok"), (cli.WARN, "w", "careful"), (cli.FAIL, "claude", "missing")])
    out: list[str] = []
    with pytest.raises(S.StartError, match="preflight failed"):
        S.preflight(rt, default_config(), tmp_path, out.append)
    assert out == ["WARN  w: careful", "FAIL  claude: missing"]


def test_doctor_key_probe(monkeypatch, capsys) -> None:
    import ads.editor as E
    called = []
    monkeypatch.setattr(E, "key_probe", lambda s: called.append(s) or 0)
    assert cli.main(["doctor", "--key-probe"]) == 0 and called == [5]


def test_nested_warning_mentions_prefix_and_extkeys() -> None:
    w = S.nested_warning("C-a")
    assert "C-a" in w and "C-b" in w and "extended-keys on" in w and "extkeys" in w
