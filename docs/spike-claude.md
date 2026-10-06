# M0.5 spike: real Claude Code in a private tmux server

Date: 2026-10-05. Claude Code **2.1.289**, tmux 3.4, model `claude-sonnet-5-5`, pane 200x50.
Server: `tmux -L ads-spike -f src/ads/tmux/ads.tmux.conf`. Launch: `ads.tmux.Tmux.respawn(pane, launcher.claude_argv(...), launcher.agent_env(...), project)`, cwd = fresh `mktemp -d /tmp/ads-spike-XXXXXX`.

argv used (from `claude_argv`):
```
claude --model claude-sonnet-5-5 --dangerously-skip-permissions --add-dir /home/dev1/workspace/vibe-coding
  --settings <rt>/work/agents/planner/settings.json --append-system-prompt-file <rt>/work/agents/planner/system-prompt.md
  --disallowedTools AskUserQuestion EnterPlanMode ExitPlanMode --name ads-planner --session-id <uuid4>
```
All flags accepted. `--name ads-planner` is shown at the right end of the input box's top rule and in the hook payload as `session_title`.

## 1. Startup dialogs (fixtures in `tests/fixtures/dialogs/`)

Order on a never-trusted dir with a user who never accepted bypass mode: **trust → bypass → idle**. No theme / onboarding dialog appeared (this account has `hasCompletedOnboarding: true`; we may not edit `~/.claude.json`, so the theme picker could not be reproduced — no `theme.txt`).

Options are **not numbered** any more, and in both dialogs the **default (❯) is "No, exit"**. The option marker is `❯` + ASCII space.

### trust (`trust.txt`, `trust_selected.txt`)
```
 Accessing workspace:

 /tmp/ads-spike-mJMA8a

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source project, or work from your team). If not, take a moment to review what's in this
 folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
```
Keys: **`Down`, (re-capture: ` ❯ Yes, I trust this folder`), `Enter`**. Claude Code then persists `projects[<dir>].hasTrustDialogAccepted = true` in `~/.claude.json` itself.
(`Escape` did *not* dismiss it within 3.5 s when sent via `send-keys Escape`.)

### bypass (`bypass.txt`, `bypass_selected.txt`)
```
  WARNING: Claude Code running in Bypass Permissions mode

  In Bypass Permissions mode, Claude Code will not ask for your approval before running potentially dangerous commands.
  This mode should only be used in a sandboxed container/VM that has restricted internet access and can easily be restored if damaged.

  By proceeding, you accept all responsibility for actions taken while running in Bypass Permissions mode.

  https://code.claude.com/docs/en/security

  ❯ No, exit
    Yes, I accept

  Enter to confirm · Esc to cancel
```
Keys: **`Down`, (re-capture: `  ❯ Yes, I accept`), `Enter`**.
On accept, Claude Code **writes `"skipDangerousModePermissionPrompt": true` into `~/.claude/settings.json`** (file mtime = accept time). Consequence: the bypass dialog appears **once per user**, not per agent/launch; a second fresh dir showed only the trust dialog. ads itself never writes it.

### `--resume` of a trusted session
No dialog at all; idle < 1 s; the previous transcript is re-rendered on screen (so old agent output that contains dialog text is visible again — see §6).

## 2. Input box, glyph (m7) and busy markers

Idle (`idle_input.txt`, `idle_input_startup.txt`), bottom of screen:
```
                                                                     ◐ medium · /effort      (only right after start)
──────────────────────────────────────────────────────────── ads-planner ─
❯ Try "how do I log an error?"          <- placeholder only on a fresh session; otherwise "❯ " + blanks
────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
```
- **Input-box glyph: `❯` followed by U+00A0 (NO-BREAK SPACE)** at column 0, between two `─` rules. Everywhere else `❯` is followed by an ASCII space: dialog option markers (` ❯ No, exit`, `  ❯ Yes, I accept`) and the echo of past user prompts in the transcript (`❯ [ADS-MSG id=…`).
  ⇒ **Do not whitespace-normalize before the input-box test** (`\s` matches `\xa0` in Python). Suggested: `INPUT_BOX_RE = r'^❯\xa0'` on raw lines plus a rule line `^─{20,}` above it; dialog screens have neither and end with `Enter to confirm · Esc to cancel`.
- Status bar: idle `  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents`; **busy adds `· esc to interrupt`** — the most reliable busy marker.
- Spinner line just above the input box (`busy.txt`): `<glyph> <Verb>…` optionally `(2s · ↓ 74 tokens)`; glyphs seen: `* · ✢ ✶ ✽` (cycling); verbs random ("Coalescing…", "Dilly-dallying…"). Turn-done line: `✻ Sautéed for 2s · done 6:52 PM`. Tool lines: `● Reading 1 file…`, `● pong` (assistant text starts with `●`), `  ⎿  …`.
- The input box stays visible while busy (empty `❯\xa0`), so "input box visible" alone does not mean idle.
- Typing `/` opens a slash-command menu above the box (pointer lines start with `[`, so not affected).

## 3. Paste behaviour
`Tmux.paste_line` (`load-buffer -b ads-<id>` + `paste-buffer -p -r -d`) of the ~150-char pointer:
- Text appears **literally** in the box (`pasted_pointer.txt`: `❯\xa0[ADS-MSG id=m-20261005-000001 from=orchestrator type=instruct] Read …md and follow the ADS protocol.`), no `[Pasted text]` collapse; the buffer is deleted.
- `send-keys Enter` 0.3 s later submitted it; `UserPromptSubmit.prompt` equals the pasted line byte-for-byte.
- Busy began within 0.25 s of Enter; this tiny turn (Read + "pong") ended ~2.8 s after.
- Newlines in the input box: **`C-j` works**, **`M-Enter` (Alt+Enter) works** (both give a second line, no submit). `C-c` clears the box (shows "Press Ctrl-C again to exit").

## 4. Hooks (stub `ads hook <event>` → `work/logs/hooks.log`)
Order observed (interactive): `SessionStart(source=startup)` fires **only after the dialogs are accepted** (≈0.5–1 s after the last Enter) → `UserPromptSubmit` → `Stop` per turn → `SessionEnd(reason=prompt_input_exit)` on `/exit`. `--resume` → `SessionStart(source=resume)`. `claude -p` fires all four, `SessionEnd.reason = other`.
Local slash commands (`/context`, `/exit`) do **not** fire UserPromptSubmit/Stop. `kill-server` killing claude produced no SessionEnd line.

Payload keys actually observed:
| Event | keys |
|---|---|
| SessionStart (startup) | `session_id, transcript_path, cwd, scratchpad_dir, hook_event_name, source, model, session_title` |
| SessionStart (resume) | `session_id, transcript_path, cwd, scratchpad_dir, hook_event_name, source, session_title, context_tokens, estimated_cache_write_usd, prompt_cache_likely_expired, seconds_since_last_response` (no `model`) |
| UserPromptSubmit | `session_id, transcript_path, cwd, scratchpad_dir, prompt_id, permission_mode, hook_event_name, prompt, session_title` |
| Stop | `session_id, transcript_path, cwd, scratchpad_dir, prompt_id, permission_mode, effort {level}, hook_event_name, stop_hook_active, last_assistant_message, background_tasks, session_crons` |
| SessionEnd | `session_id, transcript_path, cwd, scratchpad_dir, prompt_id, hook_event_name, reason` |
(`-p` mode omits `scratchpad_dir`, `session_title`, `model`.) `permission_mode` = `bypassPermissions`. StopFailure not triggered. additionalContext: not tested (optional).

## 5. CLAUDE.md loading
`/context all` → Memory files:
```
├ ~/.claude/CLAUDE.md: 180 tokens
└ ~/workspace/vibe-coding/CLAUDE.md: 494 tokens
```
So the runtime CLAUDE.md **is loaded** via `--add-dir` + `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`. Note the **user's global `~/.claude/CLAUDE.md` is loaded too** (smoke run answered "OK, 형님."): agents inherit the user's personal instructions (language, form of address).

## 6. Negative fixture
`agent_output_dialog_text.txt`: the agent printed `Do you trust the files in this folder?`, `❯ 1. Yes, proceed`, `❯ No, exit`, `Yes, I trust this folder`, `Yes, I accept`, `Enter to confirm · Esc to cancel` as output (indented 2 spaces under `●`), with the idle input box below. A naive "`Enter to confirm` anywhere on screen" check misfired on the resumed session in the spike; dialog matching must require the last non-blank line to be the `Enter to confirm · Esc to cancel` footer **and** no `^❯\xa0` input box.

## 7. Timings
- launch → trust dialog on screen: ≈0.2 s.
- accept trust → idle (bypass already accepted): ≈0.5 s; SessionStart at about the same moment.
- `--resume` → idle: < 1 s.
- `claude -p "say ok"` smoke: ≈5 s total.
