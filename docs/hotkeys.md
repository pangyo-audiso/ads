# Hot keys

A cheat sheet for working inside an ads cell. For background, see [usage.md](usage.md).

## Where things are

```
window 0 "agents"   orchestrator | planner
                    tester       | ads>  (you: the editor pane)
window 1 "team"     evaluator    | developer
                    coder-1      | coder-2
window 2            supervisor log
```

Type only in the `ads>` pane (window 0, bottom-right). Anything typed into an agent pane goes straight to that Claude session.

## tmux keys (prefix `Ctrl+a`)

The ads tmux server uses **`Ctrl+a`** as its prefix, not tmux's default `Ctrl+b`. Press `Ctrl+a`, release it, then press the key. The prefix is set by `[ads] tmux_prefix` in `ads.toml`.

| Keys | Action |
|---|---|
| `Ctrl+a` `0` / `1` / `2` | go to window 0 (agents + you), 1 (team), 2 (supervisor log) |
| `Ctrl+a` `n` / `p` | next / previous window |
| `Ctrl+a` arrow key | move to the pane in that direction |
| `Ctrl+a` `o` | next pane |
| `Ctrl+a` `q` | show pane numbers; press a number to jump there |
| `Ctrl+a` `z` | zoom the current pane in or out |
| `Ctrl+a` `[` | scroll mode: arrows / PgUp / PgDn to read history, `q` to leave |
| `Ctrl+a` `d` | **detach**: back to your shell; agents keep running |
| `Ctrl+a` `Ctrl+a` | send a literal `Ctrl+a` to the pane |

The mouse is on: click a pane to focus it, click a window name in the status bar to switch, and scroll with the wheel.

## Editor keys (the `ads>` pane)

| Keys | Action |
|---|---|
| `Enter` | send to the orchestrator. If the orchestrator has asked you a question, Enter sends your text as the answer (toolbar: `answering Q m-…`). |
| `!` at the start of the text | send as a new instruction even while a question is open |
| `Ctrl+J` or `Alt+Enter` | newline (always works) |
| `Shift+Enter` | newline, where the terminal reports it (see [Nested tmux](#nested-tmux-and-shiftenter)) |
| `Esc` then `Enter` within 0.3 s | same as `Alt+Enter` (newline); after a short pause, `Enter` sends |
| `Up` / `Down` | move within the text; at the first or last line, walk history |
| `Ctrl+R` | reverse history search (also works in vi insert mode) |
| `Ctrl+C` | clear the text; press twice within 1 s to exit the editor |
| `Ctrl+D` | does nothing on an empty line (so the pane isn't closed by accident) |

History is per project (`projects/<name>/work/input_history`). With text before the cursor, `Up` recalls only entries that start with it.

### vi mode

vi mode is on by default (`[editor] vi_mode = true`).

| Keys | Action |
|---|---|
| `Esc` | normal (navigation) mode |
| `i` / `a` | back to insert mode (before / after the cursor) |
| `h` `j` `k` `l` | move; `k` / `j` at the first / last line walk history |
| `/` `?` then `n` / `N` | search history forward / backward, next / previous match |
| `Enter` | send, from normal mode as well |

### `/ads` commands

Type these as the whole input and press `Enter`. They act on the editor's own project.

| Command | Action |
|---|---|
| `/ads status` | agent states, open tasks, phase |
| `/ads inbox` | messages sent to you; open questions are marked |
| `/ads keys` | show the raw bytes your next key sends (5 s) |
| `/ads restart <agent>` | restart an agent (`--resume` keeps its conversation) |
| `/ads restart supervisor` | restart the supervisor |
| `/ads instruct <text>` | send an instruction even while a question is open |
| `/ads help` | list the commands |

## Leaving and stopping

These are shell commands, run from the ads folder outside the ads tmux:

| Command | Action |
|---|---|
| `Ctrl+a` `d`, then `ads attach` | leave and come back later (`-p <name>` if several projects run) |
| `ads list` | projects and whether they are running |
| `ads stop -p <name>` | stop one project (plain `ads stop` if only one runs) |
| `ads stop --all` | stop every running project |
| `ads <name> --resume` | start again, continuing each agent's conversation |

Prefer `ads stop` to `tmux kill-server`: it shuts the supervisor and agents down cleanly, so `--resume` works later. ads servers are not on tmux's default socket. Raw tmux commands need `-L ads-<name>`, e.g. `tmux -L ads-<name> ls`.

## Nested tmux and Shift+Enter

If you start ads inside your own tmux:

- `Ctrl+b` goes to your outer tmux and `Ctrl+a` goes to ads.
- Shift+Enter reaches the editor only if the **outer** tmux forwards extended keys. Add this to `~/.tmux.conf` and restart the outer tmux server:
  ```
  set -s extended-keys on
  set -as terminal-features 'xterm*:extkeys'
  ```
- `Ctrl+J` and `Alt+Enter` work without this. To see what your terminal sends, run `ads doctor --key-probe` or `/ads keys`.
