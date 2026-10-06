# Troubleshooting ads

Start every investigation with `ads status`, which shows each agent's state and reason, inflight message, queued/held counts, open tasks with their nudges, supersede pending, phase, supervisor liveness, and alerts. Then look at the logs. The mechanics behind each state are described in `docs/protocol.md`.

## Where to look

| What | Where |
|---|---|
| Delivery, dialogs, restarts, warnings | `work/logs/supervisor.log`; it is also shown live in tmux window 2 (`C-a 2`) |
| Every Claude Code hook call with its payload, plus hook tracebacks (`"error"` records) | `work/logs/hooks.log` |
| Every message and task transition | `work/logs/bus.jsonl` |
| The human editor | `work/logs/editor.log` |
| A message | `work/msgs/<id>.json` (envelope), `work/msgs/<id>.md` (body) |
| A task | `work/tasks/<id>.json` (`state`, `nudges`, `reply_id`) |
| An agent's state | `work/state/<agent>.json` |
| An agent's reply drafts and chunk files | `work/agents/<agent>/` |
| Unrecoverable errors and failed deliveries | `work/run/alerts/*.json` |
| Screen of an agent, without attaching | `tmux -L ads capture-pane -p -t <pane id>` (ids in `work/run/panes.json`) |

Useful one-liners:

```bash
ads status                                   # or --json
tail -f work/logs/supervisor.log
grep '"error"' work/logs/hooks.log | tail     # hook tracebacks
jq -c 'select(.event=="created") | [.id,.from,.to,.type,.status]' work/logs/bus.jsonl
tmux -L ads capture-pane -p -t "$(jq -r .planner work/run/panes.json)" | tail -30
```

## An agent is stuck

**`busy` for a long time.** Real work can take many minutes, so check the pane first: a spinner line (`✶ Thinking…`) and `esc to interrupt` in the status bar mean it is still working. If the screen is idle but the state still says `busy`, the Stop hook was lost. The stale guard fixes this by itself after `stale_busy_s` (600 s) plus three idle-looking captures, and logs `→ idle(stale)`. To get it unstuck sooner, run `ads restart <agent> --resume`.

**`starting` that never ends.** The SessionStart hook has not fired. Check the following:
- Is a dialog on screen? (see below)
- Did `claude` print an error, such as an unknown flag or a model it cannot access? Capture the pane.
- Does `hooks.log` contain a `SessionStart` for that agent?

`ads doctor` checks `claude --version` and `claude auth status`. The startup timeout (`startup_timeout_s`, 180 s) only logs a warning and is never fatal.

**`dialog` / `dialog(unknown)` / `dialog(trust-stuck)`.** The watchdog answers only the dialogs it knows: trust, bypass and theme, matched on their option markers. It answers only while the agent is `starting`/`dialog` or a paste is unconfirmed, and at most 3 times a minute. A dialog it does not know is left alone; ads never guesses an answer. To clear it:
1. Attach (`ads attach`) and go to the pane (`C-a 1` for window 1, `C-a o` to cycle panes).
2. Answer the dialog by hand.

The state clears on the next hook or watchdog pass. A `*-stuck` reason means the same known dialog came back 3 times in a minute. This usually happens because the answer did not persist, for example when `~/.claude.json` is not writable.

**`continuing`.** The Stop hook blocked because the agent has an open task without a reply (a nudge). This is normal. The agent continues and should send its reply. After `max_report_nudges` (2) without a reply, the task becomes `failed` and the sender gets `system(missing-report)`.

**`down(pane_dead)`.** The Claude process exited. Its open incoming tasks were failed, and their senders got `system(agent-down)`. Run `ads restart <agent>`. Add `--resume` to keep the agent's conversation.

**`restarting(clear)` / `down(no-restart)`.** Someone ran `/clear` or `/resume` inside the pane. A SessionStart is expected within 60 s; otherwise the state becomes `down(no-restart)`. No tasks are failed. Run `ads restart <agent>` if the agent does not come back.

## A message is not delivered

| `ads status` / envelope shows | Meaning and fix |
|---|---|
| `queued`, recipient `busy` | Normal. Messages are pasted only into idle agents; Claude Code would otherwise inject the text into the running turn. |
| `held`, `held_by=<task>` | A hold rule applies (same pair already has an open task, or orchestrator has a phase open). It is released automatically when `<task>` closes. To replace the open work, supersede it (see `docs/usage.md`). |
| `delivering` for a long time | The prompt-submit confirmation is pending. After `confirm_timeout_s` the supervisor retries Enter up to 2 times, then fails the message. If the supervisor died, `ads restart supervisor` requeues it. |
| `failed` | The paste was not visible 3 times, or Enter never submitted. An alert names the message. Fix the cause (usually a dialog or an unexpected screen in the pane), then run **`ads send --requeue <id>`**. |
| `delivered` + `"unconfirmed": true` | The agent went busy but no prompt-submit hook arrived. Check `hooks.log` for errors. The `settings.json` hooks must point at an executable `ads` (`ads doctor`: "hook binary"). |
| `superseded` / `ignored` | The task was cancelled, or this was a late reply to a cancelled task. Nothing to do. |

The supervisor itself shows `supervisor: not running (stale pid …)` in `ads status`. Run `ads restart supervisor`. It respawns tmux window 2, adopts the running agents without relaunching them, and requeues stale `delivering` messages.

## Restarting things

```bash
ads restart planner              # fresh Claude session (new session id)
ads restart planner --resume     # same conversation (claude --resume <uuid>)
ads restart human                # the editor in window 0 pane 3 (e.g. after C-c C-c)
ads restart supervisor           # window 2; agents are adopted, not relaunched
ads stop && ads <project> --resume   # whole cell, keeping every agent's conversation
```

The editor offers the same commands: `/ads restart <agent>|supervisor [--resume]`.

Agent files (`work/agents/<agent>/settings.json`, `system-prompt.md`) are re-rendered on each launch by the **supervisor process**. Prompt files (`src/ads/prompts/*.md`, `.claude/ads/prompts/*.md`) are read from disk at render time, so `ads restart <agent>` picks up their changes. Changes to Python code, such as `launcher.py`, take effect only in a new supervisor. Run `ads restart supervisor` first, then `ads restart <agent>`.

A restarted agent's open tasks are not failed; only a dead pane (`down(pane_dead)`) fails them. If the agent lost the work (fresh session), the sender must resend it, or you can supersede the open task.

## Startup problems

- **"this runtime already runs a cell":** a runtime runs one project at a time. Run `ads stop`, or `ads <project> --attach`.
- **"existing session … use --attach or --restart":** without a terminal, ads will not prompt. Pass one of the two flags.
- **Trust dialog on every new project folder:** this is expected the first time for each folder. The watchdog answers it (`supervisor.log`: `<agent>: trust dialog answered`), and Claude Code then saves `hasTrustDialogAccepted` for that folder in `~/.claude.json`.
- **Bypass-permissions dialog:** this appears only on a user's very first `--dangerously-skip-permissions` launch. After the watchdog accepts it, Claude Code saves `skipDangerousModePermissionPrompt: true` in `~/.claude/settings.json`, so the dialog never appears again for that user.
- **ads never writes `~/.claude.json` or `~/.claude/settings.json` itself.** Both choices are persisted by Claude Code when the dialog is answered. If you clear them, the dialogs come back and are answered again.
- **`claude auth status` WARN:** log in with `claude` once, outside ads.

## Keyboard (window 0, pane 3)

- **Shift+Enter does nothing, or sends.** Use **Ctrl+J** or **Alt+Enter**, which always insert a newline. Shift+Enter needs a terminal that reports it distinctly. ads's tmux translates it to `C-j` in the editor pane only.
- **Running inside your own tmux (nested):** the **outer** tmux must forward extended keys. Add this to `~/.tmux.conf` and restart the outer server:
  ```
  set -s extended-keys on
  set -as terminal-features 'xterm*:extkeys'
  ```
  Run `ads doctor --key-probe`, or `/ads keys` in the editor, to see which bytes your keys send. `ads doctor` warns when nested, and when the running ads server lacks `extended-keys on`.
- **Prefix keys:** the ads server uses `C-a`, so your outer tmux keeps `C-b`. Change it with `[ads] tmux_prefix`.
- **C-c in the editor** clears the text. Pressing it twice within 1 s exits the editor; `ads restart human` brings it back.

## Agents behave oddly

- **Agents answer in Korean, or call you by a nickname:** they load your global `~/.claude/CLAUDE.md` as well as the runtime `CLAUDE.md`. This is expected.
- **An agent repeats a mistake:** record the lesson with `ads note "…"`. Every agent re-reads `## Lab Notes` at the start of each task.
- **An agent edits files it should not:** the role prompts forbid `src/ads/` and `work/` of the runtime and, for developer, all code edits. Check `bus.jsonl` for who did what, and add a Lab Note. Per-role prompt overrides go in `<runtime>/.claude/ads/prompts/<role>.md`.
