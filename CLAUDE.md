# ads runtime (Audiso Development System)

This directory is both the **ads runtime** (`ads.toml`, and `projects/<name>/` holding each
project's state: its own `CLAUDE.md` with Lab Notes, `plan/`, `work/`, `project.json`) and the
**source** of ads itself (`src/ads/`, `tests/`). Projects developed with ads live in their own
directories, never inside this one; several of them can run concurrently, each on its own tmux
server `ads-<name>`.

This file is for developers of ads. ads agents do NOT load it: they get
`--add-dir <runtime>/projects/<name>` only, so each cell reads its project's own CLAUDE.md.

- Spec: `plan/2026-10-05-init.txt`. Implementation plan: `~/.claude/plans/read-plan-2026-10-05-init-txt-and-sprightly-melody.md`.
- Python 3.14, venv at `.venv/` (`.venv/bin/pip install -e '.[dev]'`), entry point `.venv/bin/ads`.
- Layout: `src/ads/` (src layout, setuptools), tests in `tests/unit` and `tests/integration` (`-m tmux`).
- `projects/` (all per-project state) is gitignored. `plan/` here holds only ads's own spec
  (`2026-10-05-init.txt`, `aux.txt`). Agents must never edit `src/ads/` or `projects/` unless
  the project being developed is ads itself.

## Conventions
- Small, typed, readable modules; brief docstrings; stdlib only in `hooks.py` and on the `ads hook` path.
- Shared JSON state is written only through `ads.paths.locked_json` (flock + atomic replace).
- Run `.venv/bin/pytest -q` before reporting; `.venv/bin/ads doctor` checks the environment.

## Developer notes (building ads itself)
<!-- Lessons from developing and E2E-testing ads. Not loaded by ads agents; `ads note` now
     writes to the selected project's projects/<name>/CLAUDE.md, never here. -->
- [2026-10-05 human] System /usr/bin/python3 has no pytest; agent panes get the ads venv first on PATH, so python3 -m pytest works there. In docs and repro commands, write the interpreter explicitly (/home/dev1/workspace/vibe-coding/.venv/bin/python3 -m pytest -q) so a human shell can rerun them.
- [2026-10-05 human] After delegating or asking (ads send instruct/review-request/question) END YOUR TURN; never sleep/poll for the reply. It arrives as a new [ADS-MSG] turn, and a message pasted while you are busy would be injected mid-turn.
- [2026-10-05 human] --re is only for replies (report/review/answer) and must name a task addressed to you; to refer to a task in an info, put its id in the subject/body. Check open task ids with ads status.
- [2026-10-05 human] planner: the final plan must be the exact draft the evaluator reviewed; applying minor findings after a pass means a new draft + another review round (E2E 2026-10-05: v2 was finalized unreviewed).
- [2026-10-05 human] Claude Code input box = '❯' + U+00A0 under a ─ rule; dialog options and echoed prompts use '❯' + ASCII space. Never whitespace-normalize before the input-box test, and never answer an unknown dialog by guessing.
- [2026-10-05 human] Fake agents (ADS_CLAUDE_BIN=tests/fake_agent.py, knobs in $ADS_RUNTIME/fake.json incl. reply/delegate) exercise the whole cell without API calls: bash tests/e2e/smoke_fake.sh before touching delivery/ledger code.
