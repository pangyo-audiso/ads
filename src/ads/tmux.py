"""Thin subprocess wrapper around a private tmux server and the fixed ads layout (plan §4.2)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_CONF = Path(__file__).resolve().parent / "tmux" / "ads.tmux.conf"

# What every pane runs until the supervisor respawns it with the real command.
PLACEHOLDER: tuple[str, ...] = ("sleep", "infinity")
WINDOWS: tuple[str, ...] = ("agents", "team", "supervisor")
SUPERVISOR = "supervisor"


class TmuxError(RuntimeError):
    """A tmux command exited non-zero."""


class Tmux:
    """All commands go to `tmux -L <socket>`; `conf` is passed with `-f` (only read when the server starts)."""

    def __init__(self, socket: str, conf: Path | str | None = None, tmux_bin: str = "tmux") -> None:
        self.socket = socket
        self.conf = Path(conf) if conf else None
        self.tmux_bin = tmux_bin

    # --- core ---------------------------------------------------------------
    def base(self) -> list[str]:
        argv = [self.tmux_bin, "-L", self.socket]
        if self.conf:
            argv += ["-f", str(self.conf)]
        return argv

    def run(self, *args: str, check: bool = True, input: str | None = None) -> str:
        """Run one tmux command; return stdout without the trailing newline."""
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}  # allow use from inside tmux
        cp = subprocess.run([*self.base(), *args], capture_output=True, text=True, input=input, env=env)
        if check and cp.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args)}: exit {cp.returncode}: {cp.stderr.strip()}")
        return cp.stdout.removesuffix("\n")

    # --- server / session ---------------------------------------------------
    def has_session(self, session: str) -> bool:
        cp = subprocess.run([*self.base(), "has-session", "-t", f"={session}"], capture_output=True)
        return cp.returncode == 0

    def new_session(self, session: str, *, window: str | None = None, cwd: Path | str | None = None,
                    width: int = 240, height: int = 70, command: Sequence[str] | None = None,
                    env: Mapping[str, str] | None = None) -> str:
        """`new-session -d`, starting the server with `-f conf` if needed; returns the first pane id."""
        args = ["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session, "-x", str(width), "-y", str(height)]
        if window:
            args += ["-n", window]
        if cwd:
            args += ["-c", str(cwd)]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        if command:
            args.append(shlex.join(command))
        return self.run(*args)

    def kill_session(self, session: str) -> None:
        self.run("kill-session", "-t", f"={session}", check=False)

    def kill_server(self) -> None:
        self.run("kill-server", check=False)

    # --- panes ----------------------------------------------------------------
    def capture(self, pane: str, lines: int = 80) -> str:
        """Plain text of the visible screen plus up to `lines` lines of history, last `lines` lines kept."""
        out = self.run("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{lines}")
        return "\n".join(out.splitlines()[-lines:])

    def paste_line(self, pane: str, text: str) -> None:
        """Paste one line as a bracketed paste (no Enter): load-buffer -b + paste-buffer -p -r -d."""
        if "\n" in text or "\r" in text:
            raise ValueError("paste_line takes a single line")
        buf = f"ads-{uuid.uuid4().hex[:12]}"
        with tempfile.NamedTemporaryFile("w", prefix="ads-paste-", suffix=".txt", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            self.run("load-buffer", "-b", buf, tmp)
            self.run("paste-buffer", "-p", "-r", "-d", "-b", buf, "-t", pane)
        finally:
            os.unlink(tmp)

    def send_keys(self, pane: str, *keys: str, literal: bool = False) -> None:
        self.run("send-keys", "-t", pane, *(["-l"] if literal else []), *keys)

    def respawn(self, pane: str, argv: Sequence[str], env: Mapping[str, str] | None = None,
                cwd: Path | str | None = None) -> None:
        """`respawn-pane -k [-c cwd] [-e K=V ...] <command>`."""
        args = ["respawn-pane", "-k", "-t", pane]
        if cwd:
            args += ["-c", str(cwd)]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args.append(shlex.join(argv))
        self.run(*args)

    def display(self, target: str, fmt: str) -> str:
        return self.run("display-message", "-p", "-t", target, fmt)

    def pane_dead(self, pane: str) -> bool:
        return self.display(pane, "#{pane_dead}") == "1"

    def set_pane_opt(self, pane: str, name: str, value: str) -> None:
        self.run("set-option", "-p", "-t", pane, name, value)

    def get_pane_opt(self, pane: str, name: str) -> str:
        return self.run("show-options", "-p", "-v", "-t", pane, name, check=False)


# --- layout (plan §4.2) -------------------------------------------------------------------

def runtime_conf(runtime: Path | str) -> Path:
    """`<runtime>/src/ads/tmux/ads.tmux.conf` (V3); the packaged copy if the runtime has none."""
    candidate = Path(runtime) / "src" / "ads" / "tmux" / "ads.tmux.conf"
    return candidate if candidate.is_file() else DEFAULT_CONF


def build_layout(tmux: Tmux, session: str, project: Path | str, runtime: Path | str, *,
                 supervisor_cmd: Sequence[str] | None = None, width: int = 240,
                 height: int = 70) -> dict[str, str]:
    """Create the ads session and write `work/run/panes.json`; return {role: pane_id}.

    window 0 "agents": orchestrator | planner / tester | human (tiled 2x2)
    window 1 "team":   evaluator | developer / coder-1 | coder-2
    window 2 "supervisor" (created with -d): `supervisor_cmd`, or the placeholder.
    Every pane gets `@ads_role` (and `@ads_state` = "-"); agent panes run PLACEHOLDER
    until the supervisor respawns them. The server is started with `-f <runtime conf>`.
    """
    from ads.paths import LAYOUT, Runtime

    rt = Runtime(Path(runtime))
    if tmux.conf is None:
        tmux.conf = runtime_conf(rt.root)
    if tmux.has_session(session):
        raise TmuxError(f"session {session} already exists")
    by_slot = {slot: role for role, slot in LAYOUT.items()}
    placeholder = shlex.join(PLACEHOLDER)
    panes: dict[str, str] = {}
    for win in (0, 1):
        if win == 0:
            tmux.new_session(session, window=WINDOWS[0], cwd=project, width=width, height=height,
                             command=PLACEHOLDER)
        else:
            tmux.run("new-window", "-d", "-t", f"={session}:{win}", "-n", WINDOWS[win],
                     "-c", str(project), placeholder)
        target = f"={session}:{win}"
        for _ in range(3):
            tmux.run("split-window", "-t", target, "-c", str(project), placeholder)
            tmux.run("select-layout", "-t", target, "tiled")  # keep room for the next split
        tmux.run("select-layout", "-t", target, "tiled")
        listing = tmux.run("list-panes", "-t", target, "-F", "#{pane_index} #{pane_id}")
        for line in listing.splitlines():
            idx, pane_id = line.split()
            panes[by_slot[(win, int(idx))]] = pane_id
    sup = tmux.run("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:2",
                   "-n", WINDOWS[2], "-c", str(rt.root),
                   shlex.join(supervisor_cmd) if supervisor_cmd else placeholder)
    panes[SUPERVISOR] = sup
    for role, pane_id in panes.items():
        tmux.set_pane_opt(pane_id, "@ads_role", role)
        tmux.set_pane_opt(pane_id, "@ads_state", "-")
    tmux.run("select-window", "-t", f"={session}:0")
    tmux.run("select-pane", "-t", panes["human"])
    ordered = {role: panes[role] for role in (*LAYOUT, SUPERVISOR)}
    write_panes(rt, ordered)
    return ordered


def write_panes(runtime, panes: Mapping[str, str]) -> Path:
    """Atomically write `work/run/panes.json`."""
    from ads.paths import Runtime

    rt = runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime))
    rt.run.mkdir(parents=True, exist_ok=True)
    tmp = rt.panes_json.with_name("." + rt.panes_json.name + ".tmp")
    tmp.write_text(json.dumps(dict(panes), indent=1) + "\n")
    os.replace(tmp, rt.panes_json)
    return rt.panes_json


def read_panes(runtime) -> dict[str, str]:
    from ads.paths import Runtime

    rt = runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime))
    data = json.loads(rt.panes_json.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{rt.panes_json}: not a JSON object")
    return {str(k): str(v) for k, v in data.items()}
