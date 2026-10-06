"""Helpers for tmux integration tests (imported as a plain module; see conftest.py)."""

from __future__ import annotations

import itertools
import json
import os
import re
import time
from pathlib import Path

from ads.tmux import Tmux

REPO = Path(__file__).resolve().parents[2]
FAKE_AGENT = REPO / "tests" / "fake_agent.py"
FIXTURES = REPO / "tests" / "fixtures" / "dialogs"
_counter = itertools.count(1)


def unique_socket() -> str:
    return f"ads-test-{os.getpid()}-{next(_counter)}"


def kill_and_clean(t: Tmux) -> None:
    """kill-server and remove the leftover socket file."""
    t.kill_server()
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    (base / t.socket).unlink(missing_ok=True)


def wait_for(pred, timeout: float = 10.0, interval: float = 0.05, tick=None):
    """Poll pred() (calling tick() first, if given) until truthy; return its value or False."""
    end = time.time() + timeout
    while True:
        if tick is not None:
            tick()
        value = pred()
        if value:
            return value
        if time.time() >= end:
            return False
        time.sleep(interval)


def set_config(runtime: Path, section: str, **values) -> None:
    """Rewrite `[section] key = value` lines in runtime/ads.toml (keys must already exist)."""
    path = runtime / "ads.toml"
    text = path.read_text()
    head, sep, rest = text.partition(f"[{section}]\n")
    assert sep, f"no [{section}] in ads.toml"
    for key, value in values.items():
        rest, n = re.subn(rf"(?m)^{re.escape(key)} = .*$", f"{key} = {value}", rest, count=1)
        assert n == 1, f"{section}.{key} not found"
    path.write_text(head + sep + rest)


def write_fake(runtime: Path, **per_agent: dict) -> None:
    """runtime/fake.json knobs read by tests/fake_agent.py at start ("*" = every agent)."""
    path = runtime / "fake.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    for agent, knobs in per_agent.items():
        data.setdefault(agent.replace("_", "-") if agent != "star" else "*", {}).update(knobs)
    path.write_text(json.dumps(data))


def fake_log(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []




FAST_DELIVERY = {"tick_ms": 100, "paste_settle_ms": 150, "confirm_timeout_s": 2,
                 "dead_grace_s": 1}


class Cell:
    """A runtime + private tmux layout + in-process Supervisor driven by run_once()."""

    def __init__(self, tmux: Tmux, runtime: Path, project: Path) -> None:
        from ads.paths import Runtime
        self.tmux = tmux
        self.rt = Runtime(runtime)
        self.project = project
        self.log = runtime / "fake.jsonl"
        self.sup = None
        self.panes: dict[str, str] = {}

    @classmethod
    def create(cls, tmux: Tmux, runtime: Path, project: Path, monkeypatch, *, fake=None,
               delivery=None, protocol=None) -> "Cell":
        from ads.supervisor import write_session
        from ads.tmux import build_layout
        cell = cls(tmux, runtime, project)
        project.mkdir(parents=True, exist_ok=True)
        cell.rt.ensure()
        set_config(runtime, "delivery", **{**FAST_DELIVERY, **(delivery or {})})
        if protocol:
            set_config(runtime, "protocol", **protocol)
        monkeypatch.setenv("ADS_CLAUDE_BIN", str(FAKE_AGENT))
        monkeypatch.setenv("ADS_RUNTIME", str(runtime))
        monkeypatch.delenv("ADS_AGENT", raising=False)
        knobs = {"*": {"busy_s": "0.5", "log": str(cell.log)}}
        for agent, k in (fake or {}).items():
            knobs.setdefault(agent, {}).update(k)
        (runtime / "fake.json").write_text(json.dumps(knobs))
        cell.panes = build_layout(tmux, "ads-cell", project, runtime)
        write_session(runtime, socket=tmux.socket, session="ads-cell", project=project)
        return cell

    def fake(self, agent: str, **knobs) -> None:
        """Change fake knobs (take effect at the agent's next (re)spawn)."""
        path = self.rt.root / "fake.json"
        data = json.loads(path.read_text())
        data.setdefault(agent, {}).update({k: str(v) for k, v in knobs.items()})
        path.write_text(json.dumps(data))

    def new_supervisor(self):
        from ads.supervisor import Supervisor
        return Supervisor(self.rt, human_argv=["sleep", "infinity"])

    def start(self) -> None:
        self.sup = self.new_supervisor()
        assert self.sup.acquire()
        self.sup.start()

    def run_until(self, pred, timeout: float = 15.0):
        return wait_for(pred, timeout=timeout, interval=0.05, tick=self.sup.run_once)

    def state(self, agent: str) -> dict:
        from ads.bus.state import read_state
        return read_state(self.rt, agent)

    def states(self) -> dict[str, str]:
        from ads.bus.state import all_states
        return {a: s["state"] for a, s in all_states(self.rt).items()}

    def all_idle(self, timeout: float = 20.0) -> bool:
        return bool(self.run_until(lambda: set(self.states().values()) == {"idle"}, timeout))

    def msg(self, msg_id: str):
        from ads.bus import store
        return store.get(self.rt, msg_id)

    def send(self, **kw):
        from ads.bus import ledger
        from ads.config import load_config
        kw.setdefault("subject", "test")
        kw.setdefault("body", "body\n")
        return ledger.send(self.rt, load_config(runtime=self.rt.root), **kw)

    def events(self, agent: str | None = None, event: str | None = None) -> list[dict]:
        return [e for e in fake_log(self.log)
                if (agent is None or e.get("agent") == agent)
                and (event is None or e.get("event") == event)]

    def capture(self, agent: str) -> str:
        return self.tmux.capture(self.panes[agent])

    def close(self) -> None:
        if self.sup is not None:
            self.sup.close()
