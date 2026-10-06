"""Project registry of a runtime: `<runtime>/projects/<name>/project.json`, and project selection.

Stdlib + `ads.paths` only (used on the `ads hook` path).

- A project is identified by its absolute path; `find_by_path` scans every project.json,
  so the same project always maps to the same state dir.
- `<name>` is the slug of the project folder's basename, or `<slug>-<sha1(path)[:6]>` when
  that name is already taken by a different project path.
- `select` picks the project a CLI command acts on (see its docstring for the order).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ads.paths import PROJECTS_DIR, ProjectState, slugify


class ProjectError(ValueError):
    """Unknown project, or no project could be selected."""


def projects_dir(runtime: Path | str) -> Path:
    return Path(runtime) / PROJECTS_DIR


def read_info(state: ProjectState) -> dict[str, Any]:
    """project.json as a dict ({} if missing or unreadable)."""
    try:
        data = json.loads(state.project_json.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def all_projects(runtime: Path | str) -> list[ProjectState]:
    """Every registered project (a dir with a project.json), sorted by name."""
    base = projects_dir(runtime)
    if not base.is_dir():
        return []
    return [ProjectState.of(runtime, d.name) for d in sorted(base.iterdir())
            if (d / "project.json").is_file()]


def _norm(path: Path | str) -> Path:
    return Path(path).expanduser().absolute().resolve()


def find_by_path(runtime: Path | str, project: Path | str) -> ProjectState | None:
    want = _norm(project)
    for st in all_projects(runtime):
        p = read_info(st).get("path")
        if p and _norm(p) == want:
            return st
    return None


def find_by_name(runtime: Path | str, name: str) -> ProjectState | None:
    if not name or "/" in name or name in (".", ".."):
        return None
    st = ProjectState.of(runtime, name)
    return st if st.dir.is_dir() else None


def name_for(runtime: Path | str, project: Path | str) -> str:
    """The state-dir name for a project path (existing registration wins)."""
    found = find_by_path(runtime, project)
    if found:
        return found.name
    path = _norm(project)
    slug = slugify(path.name)
    if not ProjectState.of(runtime, slug).dir.exists():
        return slug
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:6]
    return f"{slug}-{digest}"


def register(runtime: Path | str, project: Path | str) -> ProjectState:
    """Find or create the state dir of `project`; (re)write project.json."""
    path = _norm(project)
    st = ProjectState.of(runtime, name_for(runtime, path))
    info = read_info(st)
    if info.get("path") != str(path) or info.get("name") != st.name:
        st.dir.mkdir(parents=True, exist_ok=True)
        data = {"name": st.name, "path": str(path),
                "created": info.get("created")
                or datetime.now().astimezone().isoformat(timespec="seconds")}
        tmp = st.project_json.with_name(".project.json.tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, st.project_json)
    return st


def project_path(state: ProjectState) -> Path | None:
    p = read_info(state).get("path")
    return Path(p) if p else None


# --- running? -------------------------------------------------------------------------------

def read_session(state: ProjectState) -> dict[str, Any] | None:
    try:
        data = json.loads(state.session_json.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("socket") and data.get("session") else None


def is_running(state: ProjectState) -> bool:
    """The tmux session recorded in the project's session.json exists."""
    sess = read_session(state)
    if not sess:
        return False
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    try:
        cp = subprocess.run(["tmux", "-L", sess["socket"], "has-session", "-t",
                             f"={sess['session']}"], capture_output=True, env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def running_projects(runtime: Path | str) -> list[ProjectState]:
    return [st for st in all_projects(runtime) if is_running(st)]


# --- selection --------------------------------------------------------------------------------

def is_bare_name(arg: str) -> bool:
    """`audiso-rag` (no `/`, not `.`/`..`, not starting with `~`) names a sibling of the runtime."""
    return bool(arg) and "/" not in arg and arg not in (".", "..") and not arg.startswith("~")


def resolve_project_arg(runtime: Path | str, arg: str, cwd: Path | None = None) -> Path:
    """Absolute project dir for `ads <arg>`: a bare name is `<runtime>/../<name>`; anything
    else is a path relative to the current directory."""
    if is_bare_name(arg):
        return (Path(runtime).absolute().parent / arg).resolve()
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = (cwd or Path.cwd()) / p
    return p.absolute().resolve()


def lookup(runtime: Path | str, arg: str, cwd: Path | None = None) -> ProjectState | None:
    """`-p <name|path>`: a state-dir name first, then a project path (bare name → sibling)."""
    return find_by_name(runtime, arg) or find_by_path(runtime, resolve_project_arg(runtime, arg, cwd))


def describe(runtime: Path | str) -> str:
    rows = []
    for st in all_projects(runtime):
        rows.append(f"{st.name} ({project_path(st)}{', running' if is_running(st) else ''})")
    return "; ".join(rows) if rows else "none registered (start one with `ads <project>`)"


def select(runtime: Path | str, arg: str | None = None, env: Mapping[str, str] | None = None,
           cwd: Path | None = None) -> ProjectState:
    """The project a command acts on. Order:

    1. explicit `-p/--project <name|path>`;
    2. `$ADS_STATE_DIR`, then `$ADS_PROJECT` (set inside a cell);
    3. the current directory inside a registered project path (deepest match);
    4. the only running project; else the only registered project;
    5. error listing the projects.
    """
    env = os.environ if env is None else env
    runtime = Path(runtime)
    if arg:
        st = lookup(runtime, arg, cwd)
        if st is None:
            raise ProjectError(f"unknown project {arg!r}; projects: {describe(runtime)}")
        return st
    sd = env.get("ADS_STATE_DIR")
    if sd and Path(sd).is_dir():
        return ProjectState.at(sd)
    pj = env.get("ADS_PROJECT")
    if pj:
        st = find_by_path(runtime, pj)
        if st:
            return st
    here = _norm(cwd or Path.cwd())
    best: tuple[int, ProjectState] | None = None
    projects = all_projects(runtime)
    for st in projects:
        p = project_path(st)
        if p and here.is_relative_to(_norm(p)):
            depth = len(_norm(p).parts)
            if best is None or depth > best[0]:
                best = (depth, st)
    if best:
        return best[1]
    running = [st for st in projects if is_running(st)]
    if len(running) == 1:
        return running[0]
    if not running and len(projects) == 1:
        return projects[0]
    what = "several projects are running" if running else "no project selected"
    raise ProjectError(f"{what}; pass -p <name|path>. Projects: {describe(runtime)}")
