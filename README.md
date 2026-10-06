# ads — Audiso Development System

`ads <project>` opens a private tmux cell in which seven interactive Claude Code agents work on your project as a team. You give instructions in one editor pane. The **orchestrator** routes each instruction to one phase:

- **plan:** the **planner** writes the plan and the **evaluator** reviews it, until the review passes.
- **dev:** the **developer** splits the work and delegates it to **coder-1** and **coder-2**.
- **test:** the **tester** tests the result.

Every agent reports back to whoever instructed it, and the orchestrator prints the result for you.

```
window 0 "agents":  orchestrator (Opus 5.5)  | planner (Fable 5.1)
                    tester (Sonnet 5.5)      | YOU: editor (Enter sends, Ctrl+J newline, vi mode, history)
window 1 "team":    evaluator (Fable 5.1)    | developer (Sonnet 5.5)
                    coder-1 (Sonnet 5.5)     | coder-2 (Sonnet 5.5)
window 2:           supervisor (delivery loop + log)
```

Agents talk over a file-backed message bus in `work/`. A single supervisor pastes a one-line `[ADS-MSG …]` pointer into a recipient's pane, and only when that recipient is idle. Claude Code hooks confirm each delivery and track agent state. A ledger enforces the rules: replies are required, phases are held until the open one finishes, and work can be cancelled by superseding it.

## Install

Requirements: Python 3.14, tmux 3.2 or newer, and Claude Code (`claude`) installed and logged in.

```bash
cd /path/to/ads            # this repo is also the ads runtime (ads.toml, CLAUDE.md, plan/, work/)
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'     # or: pip install -e .
.venv/bin/ads doctor
```

## Quickstart

```bash
.venv/bin/ads ~/code/myproject        # asks before creating a missing folder (git init + docs/)
# type in the bottom-right pane, e.g. "Plan a CLI that …", then "Implement the plan.", then "Test it."
ads status                            # agents, open tasks, held messages, phase
ads note "lesson learned"             # shared Lab Notes in CLAUDE.md
ads stop                              # stop the cell; work/ is kept
ads ~/code/myproject --resume         # come back with every agent's conversation
```

Outputs:

| Output | Location |
|---|---|
| Plans | `plan/<date>-<slug>.md` (with `evaluator_status`) |
| Plan drafts and reviews | `plan/drafts/` and `work/reviews/` |
| Dev and test reports | `<project>/docs/` |

Models and timing are configured in `ads.toml`.

## Docs

- [docs/usage.md](docs/usage.md): starting, panes, editor keys, nested tmux, stop/resume
- [docs/protocol.md](docs/protocol.md): envelope, pointer, ledger rules (hold, supersede, down, nudge), state machine, delivery algorithm
- [docs/troubleshooting.md](docs/troubleshooting.md): stuck agents, failed deliveries, restarts, logs, keyboard
- [docs/spike-claude.md](docs/spike-claude.md): the Claude Code behaviour ads relies on (dialogs, input box, hooks)
- [docs/e2e-report-2026-10-05.md](docs/e2e-report-2026-10-05.md): a real end-to-end run (plan → dev → test)

## Tests

```bash
.venv/bin/pytest -q                   # unit + tmux integration tests (fake agents)
bash tests/e2e/smoke_fake.sh          # full cell with fake agents, no real Claude
bash tests/smoke_claude.sh            # real `claude -p` + hooks (uses the API)
```
