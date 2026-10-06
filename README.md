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

Agents talk over a file-backed message bus in the project's state dir, `projects/<name>/work/` inside the ads folder. A single supervisor pastes a one-line `[ADS-MSG …]` pointer into a recipient's pane, and only when that recipient is idle. Claude Code hooks confirm each delivery and track agent state. A ledger enforces the rules: replies are required, phases are held until the open one finishes, and work can be cancelled by superseding it.

One ads folder runs **several projects at once**. Each project gets its own state (`projects/<name>/`: Lab Notes, plans, messages, agent sessions) and its own tmux server (`ads-<name>`), so cells never see each other.

## Install

Requirements: Python 3.14, tmux 3.2 or newer, and Claude Code (`claude`) installed and logged in.

Log in to Claude Code once per OS user, at any point before the first `ads` run, with `claude` (then `/login`) or `claude auth login`. The login is stored in `~/.claude/`, not in this repo, so it survives re-cloning and reinstalling ads. `ads doctor` checks it.

```bash
ROOT_DIR=$HOME/workspace/lab; mkdir -p $ROOT_DIR && cd $ROOT_DIR
git clone https://github.com/pangyo-audiso/ads.git
cd ads                     # this repo is also the ads runtime (ads.toml; per-project state in projects/<name>/)
python3.14 -m venv .venv
```

Then choose one way to install the dependencies:

```bash
# A) Pinned versions from requirements.txt, then the ads package itself (provides the `ads` command)
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps

# B) Let pip resolve the dependencies from pyproject.toml
.venv/bin/pip install -e '.[dev]'     # or: pip install -e .   (without pytest)
```

`requirements.txt` only lists the Python packages (prompt_toolkit for the editor, pytest for the tests). You still need `pip install -e .` to get the `ads` command. tmux and Claude Code are system tools and are installed separately.

```bash
.venv/bin/ads doctor
```

## Quickstart

A **bare name** creates (or reuses) the project **next to the ads folder**. With ads cloned at `~/workspace/ads`:

```bash
.venv/bin/ads audiso-rag              # → ~/workspace/audiso-rag (asks before creating it: git init + docs/)
# type in the bottom-right pane, e.g. "Plan a CLI that …", then "Implement the plan.", then "Test it."
```

Anything containing `/`, or starting with `~` or `.`/`..`, is a path relative to your current directory, as usual: `ads ~/code/myproject`, `ads ../other`, `ads /abs/path`. ads prints the resolved absolute path before it asks to create a missing folder. The project folder must be **outside** the ads folder (and must not contain it).

Several projects can run side by side from the same ads folder:

```bash
ads audiso-rag --no-attach            # cell 1 on tmux server ads-audiso-rag
ads ~/code/myproject --no-attach      # cell 2 on tmux server ads-myproject
ads list                              # NAME, PATH, RUNNING, SOCKET, PHASE, OPEN TASKS
ads attach -p audiso-rag              # open one of them
```

Commands that act on one project (`status`, `send`, `note`, `stop`, `attach`, `restart`, `doctor`) pick it in this order: `-p/--project <name|path>`, then the project of the cell you are in (`$ADS_STATE_DIR`/`$ADS_PROJECT`), then the project containing the current directory, then the only running project. Otherwise they list the projects and ask for `-p`.

```bash
ads status -p audiso-rag              # agents, open tasks, held messages, phase
ads note -p audiso-rag "lesson"       # that project's Lab Notes (projects/audiso-rag/CLAUDE.md)
ads stop -p audiso-rag                # stop one cell; its state is kept
ads stop --all                        # stop every running cell
ads audiso-rag --resume               # come back with every agent's conversation (per project)
```

Outputs (state dir = `projects/<name>/` in the ads folder; `<name>` is the project folder's name, plus a short hash if another project already uses it):

| Output | Location |
|---|---|
| Plans | `projects/<name>/plan/<date>-<slug>.md` (with `evaluator_status`) |
| Plan drafts and reviews | `projects/<name>/plan/drafts/` and `projects/<name>/work/reviews/` |
| Shared memory (Lab Notes) | `projects/<name>/CLAUDE.md`, loaded by that project's agents only |
| Dev and test reports | `<project>/docs/` |

Models and timing are configured in `ads.toml` (shared by all projects).

## Docs

- [docs/usage.md](docs/usage.md): starting, several projects, panes, editor keys, nested tmux, stop/resume
- [docs/protocol.md](docs/protocol.md): envelope, pointer, ledger rules (hold, supersede, down, nudge), state machine, delivery algorithm
- [docs/troubleshooting.md](docs/troubleshooting.md): stuck agents, failed deliveries, restarts, logs, keyboard
- [docs/spike-claude.md](docs/spike-claude.md): the Claude Code behaviour ads relies on (dialogs, input box, hooks)
- [docs/e2e-report-2026-10-05.md](docs/e2e-report-2026-10-05.md): a real end-to-end run (plan → dev → test)

## Tests

```bash
.venv/bin/pytest -q                   # unit + tmux integration tests (fake agents)
bash tests/e2e/smoke_fake.sh          # two concurrent cells with fake agents, no real Claude
bash tests/smoke_claude.sh            # real `claude -p` + hooks (uses the API)
```
