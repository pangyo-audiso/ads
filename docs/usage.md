# Using ads

ads (Audiso Development System) runs seven interactive Claude Code agents and one human editor in a private tmux server. A file-backed message bus connects them. This page covers day-to-day use. The design is in the plan; the Claude Code behaviour that ads relies on is in `docs/spike-claude.md`.

## Install

Requirements: Python 3.14, tmux 3.2 or newer, and Claude Code (`claude`) installed and logged in.

```bash
cd /home/dev1/workspace/vibe-coding        # the runtime: ads.toml, CLAUDE.md, work/
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ads doctor                       # every row should be PASS (WARNs are advisory)
```

ads locates its runtime from `--runtime`, then `$ADS_RUNTIME`, then the nearest `ads.toml` found by walking up from the current directory. If you run ads from somewhere else, put `.venv/bin` on your `PATH` and `export ADS_RUNTIME=/home/dev1/workspace/vibe-coding`.

## Start

```bash
ads ~/code/myproject                # same as: ads start ~/code/myproject
```

Startup runs these steps:

1. **Check the project path.** The project and the runtime must not be the same directory or nested inside each other.
2. **Create a missing project folder.** ads asks `Project folder … does not exist. Create it? [y/N]`. `--yes` answers yes without asking. When stdin is not a terminal and `--yes` is not given, the answer is no. A newly created folder gets `git init` (set `[ads] git_init = false` to skip this). ads also makes sure that:
   - `<project>/docs/` exists;
   - the runtime has `plan/`, `plan/drafts/` and `work/`;
   - the runtime `CLAUDE.md` has a `## Lab Notes` section;
   - the runtime `.gitignore` ignores `work/` and `.venv/`.
3. **Preflight.** ads runs the same checks as `ads doctor`. A FAIL aborts the start. WARNs are printed and startup continues.
4. **Handle an existing session.** If this project already has a running session, ads behaves as follows:
   - `--attach`: attach to it.
   - `--restart`: stop it, then start fresh.
   - Neither flag: ask `[a]ttach / [r]estart / [q]uit`. Without a terminal, ads exits 1 instead.

   A runtime runs one cell at a time. Starting a second project while another cell runs is refused; run `ads stop` first.
5. **Launch.** ads renders the agent files, writes `work/run/session.json` and builds the tmux layout. Window 2 runs `ads supervisor`, which launches the agents and the editor. ads waits up to 15 s for the supervisor. It then prints each agent as it becomes idle, until all seven are idle or `[delivery] startup_timeout_s` (180 s) passes. That timeout is not fatal. Press Ctrl+C to stop waiting.
6. **Attach.** ads selects window 0 and the human pane, then attaches. `--no-attach` (or `[ads] attach = false`) skips this; attach later with `ads attach`.

Other flags: `--resume` restarts every agent with `claude --resume <its previous session>` (an agent with no previous session starts fresh), `--config FILE` and `--runtime DIR`.

On the first launch in a folder, each agent shows Claude Code's **trust dialog**. On a user's very first bypass-mode launch it also shows the **bypass-permissions dialog**. The supervisor's watchdog answers both (log: `work/logs/supervisor.log`, e.g. `planner: trust dialog answered`). Claude Code then saves these choices itself: folder trust in `~/.claude.json` and `skipDangerousModePermissionPrompt` in `~/.claude/settings.json`, so later launches show no dialog. **ads never writes either file.**

## Panes

The cell runs on tmux socket `ads` (`tmux -L ads`). Its session is named `ads-<folder>-<hash>`.

| window | pane 0 | pane 1 | pane 2 | pane 3 |
|---|---|---|---|---|
| 0 `agents` | orchestrator (Opus 5.5) | planner (Fable 5.1) | tester (Sonnet 5.5) | **human editor** |
| 1 `team` | evaluator (Fable 5.1) | developer (Sonnet 5.5) | coder-1 (Sonnet 5.5) | coder-2 (Sonnet 5.5) |
| 2 `supervisor` | `ads supervisor` (delivery loop, its log on screen) | | | |

Each pane border shows `role [state]`, for example `planner [idle]` or `developer [busy ⇢]`. `⇢` means a message is being delivered to that pane.

## The human editor (window 0, pane 3)

Type an instruction and press **Enter**. It goes to the orchestrator as an `instruct`. If the orchestrator has asked you a question, Enter sends your text as the answer instead, and the toolbar shows `answering Q m-…`. Start the text with `!` to send an instruct anyway.

| key | effect |
|---|---|
| Enter | send |
| **Ctrl+J**, **Alt+Enter** | newline (always work) |
| Shift+Enter | newline where the terminal reports it (tmux turns it into C-j in this pane) |
| Up/Down, vi `k`/`j` | move within the text; at its first or last line, walk history (`work/input_history`) |
| **C-r** | reverse history search (also in vi insert mode); vi `/ ? n N` |
| Esc | vi navigation mode (`vi_mode = true` by default) |
| C-c | clear the text; press it twice within 1 s to exit the editor (`ads restart human` brings it back) |

Commands typed in the editor: `/ads status`, `/ads inbox`, `/ads keys` (shows the raw bytes your keys send), `/ads restart <agent>|supervisor [--resume]`, `/ads instruct <text>`, `/ads help`.

The toolbar shows each agent's state, the current phase (`plan`/`dev`/`test`), queued and held message counts, `supersede pending`, and the last message addressed to you.

New instructions wait (`held`) while the orchestrator still has an instruct open. To cancel the running work and replace it, ask the orchestrator to supersede the open task.

## tmux keys and nested tmux

The ads server uses prefix **C-a**, not tmux's default C-b. This lets ads run inside your own tmux, which keeps C-b. Change it with `[ads] tmux_prefix`. Useful keys: `C-a 0` / `C-a 1` / `C-a 2` switch windows, `C-a o` and arrow keys move between panes, `C-a z` zooms a pane, `C-a d` detaches (the cell keeps running), and mouse selection and scrolling are on.

When `ads` is started from inside tmux, it unsets `$TMUX` and attaches as a nested client. `switch-client` cannot move between tmux servers, so nesting is the only option. In that case:

- `C-a …` goes to ads and `C-b …` goes to your outer tmux.
- Shift+Enter only reaches the editor if the **outer** tmux forwards extended keys. Add this to `~/.tmux.conf` and restart the outer server:
  ```
  set -s extended-keys on
  set -as terminal-features 'xterm*:extkeys'
  ```
  Ctrl+J and Alt+Enter work without this. To see what your terminal actually sends, run `ads doctor --key-probe` (5 s raw key dump) or `/ads keys` in the editor.

## Status, messages, notes

```bash
ads status            # agents (state, reason, model, inflight, queued, held), open tasks, held, alerts
ads status --json
ads send --from human --to planner --type info --subject hi --body "no action needed"
ads note "one-line lesson"      # appended to ## Lab Notes in the runtime CLAUDE.md
```

`--re` is only accepted on replies (`report`, `review`, `answer`). To refer to a task in an `info`, put its id in the subject or body.

## Stop, resume, restart

```bash
ads stop                      # no project argument needed: uses work/run/session.json
ads ~/code/myproject --resume # bring the agents back with their Claude conversations
ads restart planner [--resume]  # respawn one agent (also: human, supervisor)
ads restart supervisor
```

`ads stop` asks the supervisor to shut down. The supervisor marks every agent `down(shutdown)` and exits. ads then kills the tmux session, and kills the `ads` server too if no other session is left. Everything under `work/` (messages, tasks, logs, agent session ids) is kept.

## Troubleshooting

The full guide is in `docs/troubleshooting.md`, and the bus rules are in `docs/protocol.md`. In short:

- **Logs:** `work/logs/supervisor.log` (delivery, dialogs, restarts), `hooks.log` (every Claude hook event), `bus.jsonl` (message and task transitions), `editor.log`.
- **An agent is stuck in `starting` or `dialog`:** check its pane. An unknown dialog is never answered by guessing; answer it by hand (`C-a 1`, select the pane), and its state clears on the next hook.
- **`down(pane_dead)`:** the agent's process exited. Run `ads restart <agent>` (add `--resume` to keep its conversation). Tasks it held are failed, and their senders are notified.
- **A message is `failed`:** run `ads send --requeue <id>` after fixing the cause.
- **The supervisor is not running** (`ads status` says so): run `ads restart supervisor`. Running agents are adopted, not relaunched.
- **Shift+Enter does nothing:** see the nested tmux section above. `ads doctor` warns if the running ads server lacks `extended-keys on`.
- **A start was refused because a cell is running:** run `ads stop`, or `ads <project> --attach`.
- Agents load your global `~/.claude/CLAUDE.md` as well as the runtime `CLAUDE.md`, so personal instructions (language, form of address) apply to them too.
