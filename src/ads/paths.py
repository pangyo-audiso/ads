"""Runtime paths, fixed pane layout, naming helpers and locked JSON files."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HUMAN = "human"

# Agent order is the canonical order used everywhere (config, status, layout).
AGENTS: tuple[str, ...] = (
    "orchestrator",
    "planner",
    "tester",
    "evaluator",
    "developer",
    "coder-1",
    "coder-2",
)

# role -> (window index, pane index). Fixed; not configurable.
LAYOUT: dict[str, tuple[int, int]] = {
    "orchestrator": (0, 0),
    "planner": (0, 1),
    "tester": (0, 2),
    HUMAN: (0, 3),
    "evaluator": (1, 0),
    "developer": (1, 1),
    "coder-1": (1, 2),
    "coder-2": (1, 3),
}


class OverlapError(ValueError):
    """Project and runtime directories are equal or nested."""


@dataclass(frozen=True)
class Runtime:
    """Absolute paths inside the runtime directory (plan §3)."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "ads.toml"

    @property
    def claude_md(self) -> Path:
        return self.root / "CLAUDE.md"

    @property
    def plan(self) -> Path:
        return self.root / "plan"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def run(self) -> Path:
        return self.work / "run"

    @property
    def session_json(self) -> Path:
        return self.run / "session.json"

    @property
    def panes_json(self) -> Path:
        return self.run / "panes.json"

    @property
    def supervisor_pid(self) -> Path:
        return self.run / "supervisor.pid"

    @property
    def poke(self) -> Path:
        return self.run / "poke"

    @property
    def requests(self) -> Path:
        return self.run / "requests"

    @property
    def alerts(self) -> Path:
        return self.run / "alerts"

    @property
    def seq(self) -> Path:
        return self.run / "seq"

    @property
    def agents(self) -> Path:
        return self.work / "agents"

    @property
    def msgs(self) -> Path:
        return self.work / "msgs"

    @property
    def tasks(self) -> Path:
        return self.work / "tasks"

    @property
    def state(self) -> Path:
        return self.work / "state"

    @property
    def reviews(self) -> Path:
        return self.work / "reviews"

    @property
    def logs(self) -> Path:
        return self.work / "logs"

    @property
    def input_history(self) -> Path:
        return self.work / "input_history"

    def agent_dir(self, agent: str) -> Path:
        return self.agents / agent

    def state_file(self, agent: str) -> Path:
        return self.state / f"{agent}.json"

    def ensure(self) -> None:
        """Create all work/ subdirectories."""
        for d in (self.run, self.requests, self.alerts, self.msgs, self.tasks,
                  self.state, self.reviews, self.logs, self.plan / "drafts"):
            d.mkdir(parents=True, exist_ok=True)
        for agent in AGENTS:
            self.agent_dir(agent).mkdir(parents=True, exist_ok=True)


def slugify(text: str, max_len: int = 32) -> str:
    """Lowercase, alnum-and-dash slug; never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return slug or "x"


def session_name(project: Path | str) -> str:
    """tmux session name `ads-<slug>-<sha1[:6]>` for an absolute project path."""
    abs_path = Path(project).expanduser().resolve()
    digest = hashlib.sha1(str(abs_path).encode()).hexdigest()[:6]
    return f"ads-{slugify(abs_path.name)}-{digest}"


def check_overlap(project: Path | str, runtime: Path | str) -> None:
    """Raise OverlapError if project == runtime or one contains the other."""
    p = Path(project).expanduser().resolve()
    r = Path(runtime).expanduser().resolve()
    if p == r:
        raise OverlapError(f"project and runtime are the same directory: {p}")
    if p.is_relative_to(r):
        raise OverlapError(f"project {p} is inside the runtime {r}")
    if r.is_relative_to(p):
        raise OverlapError(f"runtime {r} is inside the project {p}")


@contextmanager
def locked_json(path: Path | str) -> Iterator[dict[str, Any]]:
    """Lock `<path>.lock`, yield the JSON dict at path ({} if missing), write it back atomically.

    Mutate the yielded dict in place. If the body raises, nothing is written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text() or "{}")
            except FileNotFoundError:
                data = {}
            if not isinstance(data, dict):
                raise ValueError(f"{path} does not contain a JSON object")
            yield data
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                    f.write("\n")
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
