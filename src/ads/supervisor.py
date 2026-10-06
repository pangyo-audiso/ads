"""`ads supervisor`: the single tmux writer (plan §4.7, §5 delivery + failure matrix).

Runs in hidden tmux window 2. One instance per project (flock on
`<runtime>/projects/<name>/work/run/supervisor.pid`); projects run side by side.
Each iteration (`run_once`, every `tick_ms` or when the poke file's mtime changes):

  requests → alerts → pane_dead grace + down cascade → restarting timeout → hold release
  → deliveries → nudge exhaustion → stale guard → gated dialog watchdog → @ads_state mirror

Delivery is idle-gated and confirmed only by the prompt-submit hook (`ledger.mark_delivered`);
the screen is read only to verify a paste, retry a swallowed Enter, and spot busy/dialogs.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ads import dialogs, launcher
from ads.bus import envelope, ledger, store
from ads.bus import state as agent_state
from ads.config import Config, load_config
from ads.paths import AGENTS, HUMAN, ProjectState, StateLike, as_state
from ads.tmux import SUPERVISOR, Tmux, TmuxError, read_panes, runtime_conf

RESTARTING_TIMEOUT_S = 60.0   # restarting (SessionEnd clear/resume) without SessionStart → down
STALE_SAMPLE_S = 10.0         # stale guard: seconds between the 3 confirming captures
STALE_SAMPLES = 3
DIALOG_TRIES = 3              # Down → re-capture attempts before giving up on a dialog
KEY_SETTLE_S = 0.3            # wait after a key before re-capturing
POKE_POLL_S = 0.05


class SupervisorError(RuntimeError):
    """Missing session/panes files or another fatal setup problem."""


# --- session.json -----------------------------------------------------------------------

def write_session(state: StateLike, *, socket: str, session: str,
                  project: Path | str, resume: bool = False) -> Path:
    """`<state>/work/run/session.json`: what the supervisor (and stop/attach/list) need to
    find the cell."""
    rt = _rt(state)
    rt.run.mkdir(parents=True, exist_ok=True)
    data = {"socket": socket, "session": session,
            "project": str(Path(project).expanduser().resolve()),
            "runtime": str(rt.runtime), "name": rt.name, "state_dir": str(rt.dir),
            "resume": bool(resume),
            "created": datetime.now().astimezone().isoformat(timespec="seconds")}
    tmp = rt.session_json.with_name("." + rt.session_json.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, rt.session_json)
    return rt.session_json


def read_session(state: StateLike) -> dict[str, Any]:
    rt = _rt(state)
    try:
        data = json.loads(rt.session_json.read_text())
    except (FileNotFoundError, ValueError) as e:
        raise SupervisorError(f"cannot read {rt.session_json}: {e}") from None
    for key in ("socket", "session", "project"):
        if not data.get(key):
            raise SupervisorError(f"{rt.session_json}: missing {key!r}")
    return data


def _rt(state: StateLike) -> ProjectState:
    return as_state(state)


def write_request(state: StateLike, op: str, **fields: Any) -> Path:
    """Drop `work/run/requests/<ns>-<op>.json` for the supervisor and touch the poke file.

    Same format as the editor's `/ads restart`: {"op": "restart", "agent": ..., "resume": ...}.
    """
    rt = _rt(state)
    rt.requests.mkdir(parents=True, exist_ok=True)
    path = rt.requests / f"{time.time_ns()}-{op}.json"
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps({"op": op, **fields,
                               "created": datetime.now().astimezone().isoformat()}) + "\n")
    os.replace(tmp, path)
    ledger.touch_poke(rt)
    return path


def respawn_supervisor(state: StateLike) -> str:
    """`respawn-pane -k` tmux window 2 with a fresh `ads supervisor`; returns the pane id."""
    rt = _rt(state)
    sess = read_session(rt)
    try:
        pane = read_panes(rt)[SUPERVISOR]
    except (OSError, ValueError, KeyError) as e:
        raise SupervisorError(f"no supervisor pane in {rt.panes_json}: {e}") from None
    tmux = Tmux(sess["socket"], conf=runtime_conf(rt.runtime))
    try:
        tmux.respawn(pane, supervisor_argv(rt), cell_env(rt, sess["project"]), cwd=rt.runtime)
    except TmuxError as e:
        raise SupervisorError(str(e)) from None
    return pane


def supervisor_argv(state: StateLike, config: str | None = None) -> list[str]:
    """`ads supervisor --runtime R -p <name> [--config F]` for this project's window 2."""
    rt = _rt(state)
    argv = [str(launcher.ads_bin(rt)), "supervisor", "--runtime", str(rt.runtime),
            "-p", rt.name]
    return argv + (["--config", config] if config else [])


def cell_env(state: StateLike, project: Path | str) -> dict[str, str]:
    """Env of the supervisor and editor panes: ADS_RUNTIME, ADS_STATE_DIR, ADS_PROJECT."""
    return {**_rt(state).env(), "ADS_PROJECT": str(project)}


# --- single instance --------------------------------------------------------------------

class PidLock:
    """flock(LOCK_EX|LOCK_NB) on the pid file, held for the process lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return True

    def holder(self) -> str:
        try:
            return self.path.read_text().strip() or "?"
        except OSError:
            return "?"

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.ftruncate(self.fd, 0)  # keep the inode: never unlink a flock'ed file
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


# --- per-agent memory -------------------------------------------------------------------

@dataclass
class AgentMem:
    dead_since: float | None = None
    inflight: str | None = None
    last_enter: float = 0.0
    dialog_hits: dict[str, deque[float]] = field(default_factory=dict)
    stale_ok: int = 0
    stale_last: float = 0.0
    startup_warned: bool = False
    mirrored: str | None = None


def _age_s(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        since = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    now = datetime.now(since.tzinfo) if since.tzinfo else datetime.now()
    return (now - since).total_seconds()


class Supervisor:
    def __init__(self, state: StateLike, cfg: Config | None = None, *,
                 tmux: Tmux | None = None, session: dict[str, Any] | None = None,
                 panes: dict[str, str] | None = None, human_argv: list[str] | None = None,
                 log_stdout: bool = False) -> None:
        self.rt = _rt(state)
        self.rt.ensure()
        self.cfg = cfg or load_config(runtime=self.rt.runtime)
        self.session = session or read_session(self.rt)
        self.project = Path(self.session["project"])
        self.tmux = tmux or Tmux(self.session["socket"], conf=runtime_conf(self.rt.runtime))
        try:
            self.panes = panes or read_panes(self.rt)
        except (OSError, ValueError) as e:
            raise SupervisorError(f"cannot read {self.rt.panes_json}: {e}") from None
        missing = [a for a in (*AGENTS, HUMAN) if a not in self.panes]
        if missing:
            raise SupervisorError(f"{self.rt.panes_json}: no pane for {', '.join(missing)}")
        self.ads_bin = str(launcher.ads_bin(self.rt))
        self.human_argv = human_argv or [self.ads_bin, "input", "--runtime",
                                         str(self.rt.runtime), "-p", self.rt.name]
        self.mem: dict[str, AgentMem] = {a: AgentMem() for a in AGENTS}
        self.lock = PidLock(self.rt.supervisor_pid)
        self.stopping = False
        self._caps: dict[str, str] = {}
        self._poke_mtime = self._poke()
        self.log = self._make_logger(log_stdout)

    # --- logging ------------------------------------------------------------------------
    def _make_logger(self, stdout: bool) -> logging.Logger:
        log = logging.getLogger(f"ads.supervisor.{abs(hash(str(self.rt.dir))):x}.{id(self):x}")
        log.setLevel(logging.INFO)
        log.propagate = False
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        self.rt.logs.mkdir(parents=True, exist_ok=True)
        handlers: list[logging.Handler] = [logging.FileHandler(self.rt.logs / "supervisor.log")]
        if stdout:
            handlers.append(logging.StreamHandler(sys.stdout))
        for h in handlers:
            h.setFormatter(fmt)
            log.addHandler(h)
        return log

    def close(self) -> None:
        for h in list(self.log.handlers):
            h.close()
            self.log.removeHandler(h)
        self.lock.release()

    # --- tmux helpers ---------------------------------------------------------------------
    def capture(self, agent: str, fresh: bool = False) -> str:
        if fresh or agent not in self._caps:
            try:
                self._caps[agent] = self.tmux.capture(self.panes[agent], lines=60)
            except TmuxError as e:
                self.log.warning("%s: capture failed: %s", agent, e)
                self._caps[agent] = ""
        return self._caps[agent]

    def keys(self, agent: str, *keys: str) -> None:
        self.tmux.send_keys(self.panes[agent], *keys)
        self._caps.pop(agent, None)

    def pane_dead(self, agent: str) -> bool:
        try:
            return self.tmux.pane_dead(self.panes[agent])
        except TmuxError:
            return True

    def _poke(self) -> float:
        try:
            return self.rt.poke.stat().st_mtime
        except OSError:
            return 0.0

    # --- launch -----------------------------------------------------------------------------
    def acquire(self) -> bool:
        return self.lock.acquire()

    def start(self) -> None:
        """Launch every agent (and the human editor) unless already running from a previous
        supervisor of this cell, which is adopted instead."""
        resume = bool(self.session.get("resume"))
        for agent in AGENTS:
            if self._launched(agent) and not self.pane_dead(agent):
                self._adopt(agent)
            else:
                self.launch(agent, resume=resume, reason="startup")
        if not (self._launched(HUMAN) and not self._dead_pane(self.panes[HUMAN])):
            self.launch_human()
        self.log.info("supervisor %d up: session %s, project %s", os.getpid(),
                      self.session["session"], self.project)

    def _launched(self, role: str) -> bool:
        return self.tmux.get_pane_opt(self.panes[role], "@ads_launched") == "1"

    def _dead_pane(self, pane: str) -> bool:
        try:
            return self.tmux.pane_dead(pane)
        except TmuxError:
            return True

    def _adopt(self, agent: str) -> None:
        n = self._requeue_delivering(agent)
        st = agent_state.read_state(self.rt, agent)
        if st.get("inflight_msg"):
            agent_state.transition(self.rt, agent, "inflight", {"msg_id": None})
        self.log.info("%s: adopted running pane (%s)%s", agent, st.get("state"),
                      f"; {n} stale delivering message(s) requeued" if n else "")

    def _requeue_delivering(self, agent: str) -> int:
        n = 0
        with ledger.ledger_lock(self.rt):
            for m in store.inflight_for(self.rt, agent):
                store.update(self.rt, m.id, status="queued", log_extra={"requeue": "stale"})
                n += 1
        return n

    def launch(self, agent: str, resume: bool = False, reason: str = "respawn") -> None:
        """(Re)spawn an agent pane with claude; state → starting."""
        cfg, rt = self.cfg, self.rt
        if resume and not launcher.session_file(rt, agent).exists():
            self.log.warning("%s: no previous Claude session to resume; starting fresh", agent)
            resume = False
        launcher.render_agent_files(cfg, rt, self.project, agent)
        sid = launcher.load_or_create_session_uuid(rt, agent, fresh=not resume)
        argv = launcher.claude_argv(cfg, rt, agent, sid, resume=resume)
        env = launcher.agent_env(cfg, rt, self.project, agent)
        self._requeue_delivering(agent)
        agent_state.transition(rt, agent, "respawn", {"reason": reason})
        self.mem[agent] = AgentMem()
        self._caps.pop(agent, None)
        pane = self.panes[agent]
        self.tmux.respawn(pane, argv, env, cwd=self.project)
        self.tmux.set_pane_opt(pane, "@ads_launched", "1")
        self.log.info("%s: launched (%s%s) session %s", agent, reason,
                      ", --resume" if resume else "", sid)

    def launch_human(self) -> None:
        pane = self.panes[HUMAN]
        self.tmux.respawn(pane, self.human_argv, cell_env(self.rt, self.project),
                          cwd=self.project)
        self.tmux.set_pane_opt(pane, "@ads_launched", "1")
        self.log.info("human: editor launched")

    def restart_self(self) -> None:
        """Respawn window 2 with a fresh supervisor (kills this process)."""
        pane = self.panes.get(SUPERVISOR)
        if not pane:
            self.log.error("supervisor restart: no supervisor pane in panes.json")
            return
        self.log.info("supervisor: restarting myself in %s", pane)
        self.lock.release()
        respawn_supervisor(self.rt)

    # --- main loop --------------------------------------------------------------------------
    def run_once(self) -> None:
        self._caps.clear()
        self.process_requests()
        if self.stopping:
            return
        self.process_alerts()
        for agent in AGENTS:
            self.check_dead(agent)
        self.check_restarting()
        released = ledger.release_held(self.rt)
        if released:
            self.log.info("released held: %s", ", ".join(released))
        for agent in AGENTS:
            self.deliver(agent)
        self.nudge_exhaustion()
        for agent in AGENTS:
            self.stale_guard(agent)
            self.watchdog(agent)
            self.check_startup(agent)
        self.mirror()

    def run_forever(self) -> int:
        def _stop(signum: int, _frame: Any) -> None:
            self.log.info("signal %d: stopping", signum)
            self.stopping = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        self.start()
        while not self.stopping:
            try:
                self.run_once()
            except Exception:
                self.log.exception("iteration failed")
            self.wait_tick()
        self.shutdown()
        return 0

    def wait_tick(self) -> None:
        end = time.monotonic() + self.cfg.delivery.tick_ms / 1000
        while not self.stopping and time.monotonic() < end:
            mtime = self._poke()
            if mtime != self._poke_mtime:
                self._poke_mtime = mtime
                return
            time.sleep(POKE_POLL_S)

    def shutdown(self) -> None:
        for agent in AGENTS:
            agent_state.transition(self.rt, agent, "shutdown")
        self.mirror()
        self.log.info("supervisor %d: shutdown (all agents down(shutdown))", os.getpid())
        self.close()

    # --- requests -----------------------------------------------------------------------------
    def process_requests(self) -> None:
        for path in sorted(self.rt.requests.glob("*.json")):
            try:
                req = json.loads(path.read_text())
            except (OSError, ValueError) as e:
                self.log.warning("bad request %s: %s", path.name, e)
                req = None
            path.unlink(missing_ok=True)
            if not isinstance(req, dict):
                continue
            op = req.get("op")
            if op == "stop":
                self.log.info("stop requested")
                self.stopping = True
                return
            if op != "restart":
                self.log.warning("unknown request op %r (%s)", op, path.name)
                continue
            target = req.get("agent") or req.get("target")
            resume = bool(req.get("resume"))
            if target in AGENTS:
                self.launch(target, resume=resume, reason="restart")
            elif target == HUMAN:
                self.launch_human()
            elif target == SUPERVISOR:
                self.restart_self()
            else:
                self.log.warning("restart: unknown target %r", target)

    # --- alerts (StopFailure, unrecoverable) ---------------------------------------------------
    def process_alerts(self) -> None:
        for path in sorted(self.rt.alerts.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("notified"):
                continue
            agent = data.get("agent")
            text = f"{data.get('error_type')}: {data.get('error_message')}"
            ids = ledger.api_error_notify(self.rt, agent, text) if agent in AGENTS else []
            data["notified"] = True
            tmp = path.with_name("." + path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            self.log.warning("%s: API error %s → %d api-error message(s)", agent, text, len(ids))

    def _alert(self, agent: str, error_type: str, message: str) -> None:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        path = self.rt.alerts / f"{stamp}-{agent}.json"
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(json.dumps({"agent": agent, "error_type": error_type,
                                   "error_message": message, "notified": True,
                                   "source": "supervisor",
                                   "ts": datetime.now().astimezone().isoformat()}) + "\n")
        os.replace(tmp, path)

    # --- down / restarting ------------------------------------------------------------------------
    def check_dead(self, agent: str) -> None:
        mem = self.mem[agent]
        if not self.pane_dead(agent):
            mem.dead_since = None
            return
        now = time.monotonic()
        if mem.dead_since is None:
            mem.dead_since = now
            self.log.info("%s: pane dead; grace %ss", agent, self.cfg.delivery.dead_grace_s)
        st = agent_state.read_state(self.rt, agent)
        if st["state"] == "down" and st.get("reason") in ("pane_dead", "shutdown"):
            return
        if now - mem.dead_since < self.cfg.delivery.dead_grace_s:
            return
        agent_state.transition(self.rt, agent, "pane_dead")
        mem.inflight = None
        self._requeue_delivering(agent)  # non-task messages wait for the restart
        ids = ledger.agent_down_cascade(self.rt, agent)
        self.log.warning("%s: down(pane_dead); %d agent-down message(s); `ads restart %s`",
                         agent, len(ids), agent)

    def check_restarting(self) -> None:
        for agent in AGENTS:
            st = agent_state.read_state(self.rt, agent)
            if st["state"] == "restarting" and _age_s(st.get("since")) > RESTARTING_TIMEOUT_S:
                agent_state.transition(self.rt, agent, "restarting_timeout")
                self.log.warning("%s: no SessionStart %ds after SessionEnd(%s) → down(no-restart)",
                                 agent, RESTARTING_TIMEOUT_S, st.get("reason"))

    def check_startup(self, agent: str) -> None:
        mem = self.mem[agent]
        st = agent_state.read_state(self.rt, agent)
        if st["state"] != "starting":
            mem.startup_warned = False
            return
        if not mem.startup_warned and _age_s(st.get("since")) > self.cfg.delivery.startup_timeout_s:
            mem.startup_warned = True
            self.log.warning("%s: still starting after %ss (not fatal); see `ads status`",
                             agent, self.cfg.delivery.startup_timeout_s)

    # --- delivery (§5) ------------------------------------------------------------------------------
    def _clear_inflight(self, agent: str, msg_id: str) -> None:
        self.mem[agent].inflight = None
        st = agent_state.read_state(self.rt, agent)
        if st.get("inflight_msg") == msg_id:
            agent_state.transition(self.rt, agent, "inflight", {"msg_id": None})

    def _fail(self, agent: str, msg_id: str, why: str) -> None:
        try:
            store.update(self.rt, msg_id, status="failed", log_extra={"why": why})
        except (store.TransitionError, store.MessageNotFound) as e:
            self.log.warning("%s: cannot fail %s: %s", agent, msg_id, e)
        text = f"delivery of {msg_id} to {agent} failed ({why}); `ads send --requeue {msg_id}`"
        self._alert(agent, "delivery_failed", text)
        self.log.error("%s", text)

    def deliver(self, agent: str) -> None:
        mem = self.mem[agent]
        st = agent_state.read_state(self.rt, agent)
        inflight = mem.inflight or st.get("inflight_msg")
        if inflight:
            self._check_inflight(agent, inflight, st)
            return
        if st["state"] != "idle" or self.pane_dead(agent):
            return
        queue = store.queued_for(self.rt, agent)
        if not queue:
            return
        msg = queue[0]
        cap = self.capture(agent)
        if not dialogs.has_input_box(cap):
            return
        d = self.cfg.delivery
        if msg.id not in (dialogs.input_box_text(cap) or ""):
            try:
                pointer = envelope.pointer_line(msg, self.rt)
            except ValueError as e:
                self._fail(agent, msg.id, f"bad pointer: {e}")
                return
            self.tmux.paste_line(self.panes[agent], pointer)
            time.sleep(d.paste_settle_ms / 1000)
            cap = self.capture(agent, fresh=True)
            if msg.id not in (dialogs.input_box_text(cap) or ""):
                msg = store.bump(self.rt, msg.id, "pastes")
                self.log.warning("%s: paste of %s not visible (attempt %d/%d)", agent, msg.id,
                                 msg.pastes, d.max_paste_retries)
                if msg.pastes >= d.max_paste_retries:
                    self._fail(agent, msg.id, "paste not visible")
                return
        with ledger.ledger_lock(self.rt):
            current = store.get(self.rt, msg.id)
            if current.status != "queued":  # superseded meanwhile
                self.log.info("%s: %s became %s before Enter", agent, msg.id, current.status)
                return
            store.update(self.rt, msg.id, status="delivering")
        agent_state.transition(self.rt, agent, "inflight", {"msg_id": msg.id})
        mem.inflight = msg.id
        self.keys(agent, "Enter")
        mem.last_enter = time.monotonic()
        self.log.info("%s: pasted %s (%s from %s) + Enter", agent, msg.id, msg.type, msg.from_)

    def _check_inflight(self, agent: str, msg_id: str, st: dict[str, Any]) -> None:
        mem = self.mem[agent]
        try:
            msg = store.get(self.rt, msg_id)
        except store.MessageNotFound:
            self._clear_inflight(agent, msg_id)
            return
        if msg.status != "delivering":
            if msg.status == "delivered":
                self.log.info("%s: %s confirmed by hook", agent, msg_id)
            self._clear_inflight(agent, msg_id)
            return
        if mem.inflight is None:  # adopted from a previous supervisor: start the clock now
            mem.inflight, mem.last_enter = msg_id, time.monotonic()
        d = self.cfg.delivery
        if time.monotonic() - mem.last_enter < d.confirm_timeout_s:
            return
        if self.pane_dead(agent):
            return
        cap = self.capture(agent)
        if dialogs.match_dialog(cap) or dialogs.is_unknown_dialog(cap):
            return  # the watchdog answers it; retry Enter afterwards
        if st["state"] in ("busy", "continuing") or dialogs.is_busy(cap):
            ledger.mark_delivered(self.rt, msg_id)
            store.update(self.rt, msg_id, unconfirmed=True)
            self._clear_inflight(agent, msg_id)
            self.log.warning("%s: %s delivered UNCONFIRMED (agent busy, no prompt-submit hook)",
                             agent, msg_id)
            return
        box = dialogs.input_box_text(cap) or ""
        if msg_id in box and msg.enters < d.max_enter_retries:
            store.bump(self.rt, msg_id, "enters")
            self.keys(agent, "Enter")
            mem.last_enter = time.monotonic()
            self.log.warning("%s: %s still in the input box; Enter retry %d/%d", agent, msg_id,
                             msg.enters + 1, d.max_enter_retries)
            return
        if msg_id in box:
            self.keys(agent, "C-c")  # one C-c clears Claude's input box (spike §3)
        self._fail(agent, msg_id, "no prompt-submit confirmation")
        self._clear_inflight(agent, msg_id)

    # --- nudges / stale -----------------------------------------------------------------------------
    def nudge_exhaustion(self) -> None:
        for t in ledger.exhausted_tasks(self.rt, self.cfg):
            if t["to"] not in AGENTS:
                continue
            if agent_state.read_state(self.rt, t["to"])["state"] != "idle":
                continue
            mid = ledger.fail_missing_report(self.rt, t)
            if mid:
                self.log.warning("%s: no reply to %s after nudges → failed; missing-report %s to %s",
                                 t["to"], t["id"], mid, t["from"])

    def stale_guard(self, agent: str) -> None:
        mem = self.mem[agent]
        st = agent_state.read_state(self.rt, agent)
        if st["state"] not in ("busy", "continuing") or \
                _age_s(st.get("since")) < self.cfg.delivery.stale_busy_s:
            mem.stale_ok, mem.stale_last = 0, 0.0
            return
        now = time.monotonic()
        if mem.stale_ok and now - mem.stale_last < STALE_SAMPLE_S:
            return
        cap = self.capture(agent, fresh=True)
        mem.stale_last = now
        if dialogs.has_input_box(cap) and not dialogs.is_busy(cap):
            mem.stale_ok += 1
        else:
            mem.stale_ok = 0
            return
        if mem.stale_ok >= STALE_SAMPLES:
            agent_state.transition(self.rt, agent, "stale_idle")
            mem.stale_ok = 0
            self.log.warning("%s: %s for %.0fs but the screen looks idle (%d captures) → idle(stale)",
                             agent, st["state"], _age_s(st.get("since")), STALE_SAMPLES)

    # --- dialog watchdog (§4.3, gated) ---------------------------------------------------------------
    def watchdog_gated(self, agent: str, st: dict[str, Any]) -> bool:
        mem = self.mem[agent]
        if st["state"] in ("starting", "dialog"):
            return True
        return bool(mem.inflight) and \
            time.monotonic() - mem.last_enter >= self.cfg.delivery.confirm_timeout_s

    def watchdog(self, agent: str) -> None:
        st = agent_state.read_state(self.rt, agent)
        if not self.watchdog_gated(agent, st) or self.pane_dead(agent):
            return
        mem = self.mem[agent]
        cap = self.capture(agent)
        d = dialogs.match_dialog(cap)
        if d is not None:
            hits = mem.dialog_hits.setdefault(d.name, deque())
            now = time.monotonic()
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= d.max_hits_per_min:
                if st["state"] != "dialog":
                    agent_state.transition(self.rt, agent, "dialog_unknown",
                                           {"name": f"{d.name}-stuck"})
                    self.log.error("%s: %s dialog answered %d times in a minute; giving up "
                                   "(state dialog)", agent, d.name, len(hits))
                return
            hits.append(now)
            ok = self.answer(agent, d)
            self.log.info("%s: %s dialog %s", agent, d.name, "answered" if ok else
                          "NOT answered (accept option never selected)")
            return
        if dialogs.is_unknown_dialog(cap):
            if st["state"] != "dialog":
                agent_state.transition(self.rt, agent, "dialog_unknown", {"name": "unknown"})
                self.log.warning("%s: unknown dialog on screen → state dialog; tail:\n%s", agent,
                                 "\n".join(dialogs.tail(cap)))
            return
        if st["state"] == "dialog" and dialogs.has_input_box(cap):
            agent_state.transition(self.rt, agent, "dialog_cleared")
            self.log.info("%s: dialog cleared", agent)

    def answer(self, agent: str, d: dialogs.Dialog) -> bool:
        if d.accept is None:
            self.keys(agent, *d.keys)
            return True
        for _ in range(DIALOG_TRIES):
            cap = self.capture(agent, fresh=True)
            current = dialogs.match_dialog(cap)
            if current is None or current.name != d.name:
                return False
            if dialogs.accept_selected(d, cap):
                self.keys(agent, "Enter")
                return True
            self.keys(agent, "Down")
            time.sleep(KEY_SETTLE_S)
        cap = self.capture(agent, fresh=True)
        if dialogs.match_dialog(cap) is d and dialogs.accept_selected(d, cap):
            self.keys(agent, "Enter")
            return True
        return False

    # --- border mirror --------------------------------------------------------------------------------
    def mirror(self) -> None:
        for agent in AGENTS:
            st = agent_state.read_state(self.rt, agent)
            label = st["state"]
            if st["state"] in ("down", "dialog", "restarting") and st.get("reason"):
                label = f"{st['state']}:{st['reason']}"
            if st.get("inflight_msg"):
                label += " ⇢"
            mem = self.mem[agent]
            if label != mem.mirrored:
                try:
                    self.tmux.set_pane_opt(self.panes[agent], "@ads_state", label)
                    mem.mirrored = label
                except TmuxError:
                    pass


# --- `ads supervisor` -------------------------------------------------------------------------------

def main(state: StateLike, cfg: Config | None = None) -> int:
    rt = _rt(state)
    try:
        sup = Supervisor(rt, cfg, log_stdout=True)
    except SupervisorError as e:
        print(f"ads supervisor: {e}", file=sys.stderr)
        return 1
    if not sup.acquire():
        print(f"ads supervisor: already running (pid {sup.lock.holder()}, "
              f"lock {rt.supervisor_pid})", file=sys.stderr)
        sup.close()
        return 1
    try:
        return sup.run_forever()
    except BaseException:
        sup.log.exception("supervisor crashed")
        sup.close()
        raise
