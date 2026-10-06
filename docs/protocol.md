# ads message protocol

This page describes the bus as built in `src/ads/bus/` (`envelope.py`, `store.py`, `ledger.py`, `state.py`, `log.py`), `src/ads/hooks.py` and `src/ads/supervisor.py`. For day-to-day use, see `docs/usage.md`. Where the code differs from the plan, this page follows the code.

## Parties and channels

There are seven agents: `orchestrator`, `planner`, `tester`, `evaluator`, `developer`, `coder-1` and `coder-2`. The `human` party is the editor in window 0, pane 3, or anything that runs `ads send --from human`. A third sender, `ads`, is used only for synthesized `system` messages.

Messages travel in two halves:

- **Write side.** `ads send` (`ledger.send`) writes the message files and the task. Agents run it through Bash, and the editor calls the Python API.
- **Pane side.** The supervisor, the only process that writes to tmux, pastes a one-line **pointer** into the recipient's Claude Code input box and presses Enter.

Delivery is confirmed only by the recipient's `UserPromptSubmit` hook, never by reading the screen.

## Files

Every project has its own bus. All paths below are under the project's state dir `<state> = <runtime>/projects/<name>/`, in `<state>/work/`. Nothing is shared between projects except `ads.toml`: messages, tasks, agent states, the sequence counter, the supervisor and its lock, and the tmux server (`ads-<name>`) are all per project, so cells of different projects run concurrently without seeing each other. Processes of a cell (agents, hooks, editor, supervisor, `ads send` run by an agent) find their state through `$ADS_STATE_DIR=<state>` (with `$ADS_RUNTIME`, `$ADS_PROJECT`, `$ADS_AGENT`). A message id is unique only within its project.

| Path (under `<state>/work/`) | Content |
|---|---|
| `msgs/<id>.json` | the envelope (below) |
| `msgs/<id>.md` | the message body (summary + absolute paths; deliverables live in files) |
| `tasks/<id>.json` | the ledger task for a task-creating message (same id) |
| `state/<agent>.json` | the agent state machine |
| `run/seq` | the project's monotonic sequence counter (flock; never resets) |
| `run/session.json` | socket, session, project, name, state_dir, resume flag of the running cell |
| `run/panes.json` | role → tmux pane id |
| `run/supervisor.pid` | flock'ed by the project's single supervisor |
| `run/poke` | touched after every bus change; wakes the supervisor before its next tick |
| `run/ledger.lock` | re-entrant flock around every ledger read-check-write |
| `run/requests/*.json` | `restart` / `stop` requests for the supervisor |
| `run/alerts/*.json` | unrecoverable API errors and failed deliveries |
| `logs/bus.jsonl` | one JSON line per message/task transition |
| `reviews/`, `agents/<agent>/` | evaluator reviews; each agent's reply bodies, chunk files and `session.json` (Claude session uuid used by `--resume`) |

Next to `work/`: `<state>/project.json` (`{name, path, created}`), `<state>/CLAUDE.md` (Lab Notes) and `<state>/plan/` + `plan/drafts/`.

Shared JSON is written with `paths.locked_json`: a flock on `<path>.lock`, then read, modify, and an atomic `os.replace`.

## Envelope

```json
{"id": "m-20261005-000012", "seq": 12, "from": "orchestrator", "to": "planner",
 "type": "instruct", "re": null, "parent": "m-20261005-000011", "supersedes": null,
 "subject": "Plan: wordcount", "created": "2026-10-05T19:49:20+09:00", "expects_reply": false,
 "status": "queued", "held_by": null, "result": null, "attachments": [],
 "delivered_at": null, "enters": 0, "pastes": 0, "unconfirmed": false}
```

- **`id`** has the form `m-<YYYYMMDD>-<seq>`, with seq zero-padded to at least 6 digits (`ID_RE = ^m-\d{8}-\d{6,}$`). Order is decided by `seq`, never by the date.
- **`subject`** is sanitized to one line of at most 200 characters, with control and format characters removed.
- **`enters` / `pastes`** count the supervisor's retries.
- **`unconfirmed`** is set when a message was marked delivered without a hook confirmation (see Delivery).
- A late reply to a cancelled task also carries `late_reply_to`.

### Types and results

| type | creates a task | reply to it | `--result` |
|---|---|---|---|
| `instruct` | yes | `report` | — |
| `review-request` | yes | `review` | — |
| `question` | yes | `answer` | — |
| `report` | no (closes an `instruct`) | — | **required**: `success` \| `partial` \| `failure` |
| `review` | no (closes a `review-request`) | — | **required**: `pass` \| `revise` |
| `answer` | no (closes a `question`) | — | none |
| `info` | no | — | none |
| `system` | no (sent by `ads` only) | — | `agent-down` \| `missing-report` \| `api-error` \| `superseded` |

### `ads send` validation (`ledger._validate`)

- `from` and `to` must be an agent or `human`, and must differ. `system` cannot be sent by hand.
- `--re` is accepted only on replies. It must name an existing task that is:
  - addressed to the sender;
  - of the matching type;
  - not already closed.

  The reply must go to that task's `from`.
- `--parent` is accepted only on task-creating types, and must name an existing task.
- `--supersede T` is accepted only on task-creating types. `T` must be the sender's own **open** task.
- Output: the new id; `<id> held behind <task>` when the message was held; `<id> ignored (late reply …)` when the reply's task was already superseded or failed.

### Message status

```
queued ──► delivering ──► delivered
  │  ▲          │  └──► queued   (supervisor restart / agent relaunch: stale paste requeued)
  │  │          └─────► failed ──► queued  (`ads send --requeue <id>`)
  ▼  │
 held ──► superseded | failed | ignored
```

`delivered`, `superseded` and `ignored` are terminal. A message addressed to `human` is created as `delivered`, since the editor toolbar shows it. A late reply is created as `ignored`.

## Pointer

This is the only text ever pasted into an agent's pane. It is one line of at most 400 characters, which stays below Claude Code's paste-collapse threshold:

```
[ADS-MSG id=m-20261005-000012 from=orchestrator type=instruct] Read /home/dev1/workspace/vibe-coding/projects/wordcount/work/msgs/m-20261005-000012.md and follow the ADS protocol.
```

- A superseding message gets the suffix ` SUPERSEDES <T>: abort that task first.`
- Matching regex: `^\[ADS-MSG id=(?P<id>m-\d{8}-\d{6,}) from=(?P<from>[\w-]+) type=(?P<type>[\w-]+)\]`.
- The prompt-submit hook adds front matter as `additionalContext`: id, from, type, re, parent, result, subject, supersedes, and the body path. The body itself is never pasted, so message content can't be mistaken for keystrokes and long content doesn't collapse.

## Ledger (`bus/ledger.py`)

A task is created for every `instruct`, `review-request` and `question`. Its id is the creating message's id.

```json
{"id", "seq", "from", "to", "type", "subject", "state", "parent", "reply_id", "nudges", "created", "closed_at"}
```

Task states are `queued`, `delivered`, `closed`, `superseded` and `failed`. A task is **open** while it is `queued` or `delivered`. The task becomes `delivered` together with its message.

### Closing

A valid reply closes its task (`state=closed`, `reply_id=<reply id>`). After any close, supersede or fail, the held messages are re-checked.

### Hold (`hold_reason`)

A new message is created `held` instead of `queued` when **all** of these are true:

1. its type is `instruct` or `review-request`;
2. it is not from `human`;
3. it has no `--supersede`;
4. one of the rules below applies. Tasks whose own message is still held are ignored, and so is the message's own task.
   - **Rule 1:** the same sender→recipient pair already has an open task.
   - **Rule 2:** the sender is `orchestrator` and it has any open outgoing `instruct`, to any recipient. Effect: one phase at a time.

`held_by` records the blocking task id.

Effects:
- **Human instructs are never held.** A second human instruction goes straight to the orchestrator.
- If the orchestrator then delegates while a phase is open, its delegation is held. The orchestrator prompt tells it to send the human an `info` saying "Queued: … held behind <task>".
- A developer can keep one open chunk per coder; a second `instruct` to the same coder waits behind the first.

### Release (`release_held`)

Release runs after every close, supersede and fail, and on every supervisor tick. It repeatedly takes the **oldest** held message (by seq) whose `hold_reason` is now `None`, queues it, and re-checks the rest, so releasing one message can keep the others held. For a message that stays held, `held_by` is updated if the blocking task changed.

### Supersede (`--supersede T`)

1. `T` becomes `superseded`. If its message was still `queued` or `held`, the message becomes `superseded` and is never pasted.
2. The children of `T` (tasks whose `parent` is `T`) are superseded **recursively**, including grandchildren.
   - A child whose message was already delivering or delivered is not cancelled silently. Its assignee gets `system(superseded)` with `re=<child>`, telling it to stop.
   - The direct assignee of `T` learns about the cancel from the ` SUPERSEDES T` pointer suffix of the new message instead.
3. The new message bypasses hold, but it is still **idle-gated**. It is pasted only when the assignee's current turn ends. `ads status` and the editor toolbar show `supersede pending` until then.
4. A reply that arrives later for `T` or one of its children is stored as `ignored`.

A bare cancel is written as `instruct --subject Cancel --supersede <T>`.

### Agent down (`agent_down_cascade`)

This runs only when the supervisor sees `#{pane_dead}=1` for `dead_grace_s` (5 s); a `SessionEnd` alone never triggers it. It does the following:

1. Every open incoming task of the agent becomes `failed`. Its message, if still undelivered, becomes `failed` too.
2. Each sender gets `system(agent-down)`.
3. Held messages are released.

`SessionEnd(clear|resume)` and `ads stop` (`down(shutdown)`) never cascade.

### Nudge (`stop_decision`, run in the Stop hook)

Let `T` be the agent's open incoming tasks that are in state `delivered`. The Stop hook **blocks** (`{"decision": "block", "reason": …}`) only if all of these hold:

- `T` is non-empty;
- `progress_this_turn == 0`, meaning no reply and no task-creating send was made this turn;
- the agent has no open outgoing task (an agent that delegated or asked is "waiting");
- `min(nudges over T) < max_report_nudges` (2).

When it blocks, `nudges` is incremented on every task in `T`. The reason text lists, for each task, the exact `ads send … --re <id> --result … --body-file <state>/work/agents/<agent>/reply-<id>.md` command to run. Claude Code continues the turn, and the state becomes `continuing`.

Once the nudges are exhausted and the agent is idle, the supervisor's `nudge_exhaustion` step does the following:

1. marks the task `failed`;
2. sends `system(missing-report)` to the task's sender;
3. releases held messages.

### API errors

A `StopFailure` with an unrecoverable `error_type` writes an alert into `run/alerts/`. The unrecoverable types are `authentication_failed`, `oauth_org_not_allowed`, `account_on_hold`, `billing_error`, `model_not_found` and `invalid_request`; the hook also accepts the spellings `error`/`error_details`. The supervisor turns the alert into `system(api-error)` for the sender of each open incoming task of that agent.

### Phase

`ledger.current_phase` is derived from the orchestrator's oldest open, non-held outgoing `instruct`: planner → `plan`, developer → `dev`, tester → `test`. It is shown by `ads status` and in the editor toolbar.

## Agent state machine (`bus/state.py`)

The state file holds `state`, `reason`, `since`, `session_id`, `last_event`, `progress_this_turn`, `inflight_msg`, `last_error` and `seen_session_start`. **Only `idle` agents receive pastes.**

| Event | Source | Result |
|---|---|---|
| `session-start` source ∈ startup/resume/clear/fork | hook | `idle(<source>)`, progress = 0 |
| `session-start` source = compact | hook | unchanged |
| `prompt-submit` | hook | `busy`, progress = 0, inflight cleared |
| `stop` allowed / blocked | hook | `idle` / `continuing(stop-block)` |
| `stop-failure` | hook | `idle(stop-failure)`, `last_error` = {type, message, alert} |
| `session-end` reason ∈ logout/prompt_input_exit/other | hook | `down(<reason>)`; `down(shutdown)` is kept as is |
| `session-end` reason ∈ clear/resume | hook | `restarting(<reason>)` |
| `respawn` | supervisor launch/restart | `starting` (seen_session_start = false) |
| `pane_dead` (≥ dead_grace_s) | supervisor | `down(pane_dead)` from any state, then cascade |
| `shutdown` | supervisor (`ads stop`, SIGTERM) | `down(shutdown)`, no cascade |
| `dialog_unknown` | watchdog | `dialog(<name>)` |
| `dialog_cleared` | watchdog (only from `dialog`) | `idle` if a SessionStart was seen, else `starting` |
| `restarting_timeout` (60 s, only from `restarting`) | supervisor | `down(no-restart)`, no cascade |
| `stale_idle` (only from busy/continuing) | stale guard | `idle(stale)` |
| `inflight` | supervisor | sets `inflight_msg` only |
| `progress` | `ads send` (reply or task-creating) | `progress_this_turn += 1` |

The guarded supervisor events are no-ops outside their source state. Because of this, a hook that wins a race is never overwritten.

**Stale guard.** An agent that has been `busy`/`continuing` for longer than `stale_busy_s` (600 s) is captured 3 times, 10 s apart. If every capture shows the input box and no spinner line, the agent is set to `idle(stale)` and a warning is logged. This guards against a lost Stop hook.

## Delivery (`Supervisor.deliver`, every tick per agent)

The supervisor ticks every `tick_ms` (500 ms), or earlier when `run/poke` changes. Each tick runs these steps in order:

1. requests
2. alerts
3. dead-pane check and cascade
4. restarting timeout
5. hold release
6. **delivery**
7. nudge exhaustion
8. stale guard
9. gated dialog watchdog
10. startup warning
11. border mirror (`@ads_state`)

The delivery step, for each agent:

```
if the agent has an inflight message (memory or state.inflight_msg):
    message no longer `delivering` (hook confirmed, or superseded)      → clear inflight
    younger than confirm_timeout_s (10 s)                             → wait
    pane dead                                                         → wait (dead check handles it)
    a dialog on screen                                                → wait (watchdog answers it)
    state busy/continuing, or spinner visible                         → mark delivered + unconfirmed, warn
    id still in the input box and enters < max_enter_retries (2)      → press Enter again
    otherwise                                                         → C-c if the id is in the box; failed + alert
    (stop here)
if state != idle or pane dead                 → skip
m = oldest queued message for the agent (by seq); none → skip
no input box on screen (rule line + "❯\xa0")  → skip
if m.id is not already in the input box:
    paste_line(pointer)                        (tmux load-buffer + paste-buffer -p -r -d, bracketed)
    sleep paste_settle_ms (150 ms); re-capture
    id not visible in the input box            → pastes += 1; failed + alert at max_paste_retries (3)
under the ledger lock: message still queued?  (it may have been superseded meanwhile) else skip
status = delivering; state.inflight_msg = id; send Enter
```

The prompt-submit hook parses the pointer and calls `ledger.mark_delivered`, which sets the message (and its task) to `delivered` and the agent to `busy`. Claude Code accepts typed input in the middle of a turn and injects it, so a paste is attempted **only** when the agent is idle and its input box is visible.

The input-box test needs a `─` rule line followed by a line starting with `❯` + U+00A0. Dialog option markers and echoed prompts use `❯` + an ASCII space, so they never match (see `docs/spike-claude.md` §2).

### Failure matrix

| Failure | Handling |
|---|---|
| Recipient busy, starting, dialog or down | message stays `queued` |
| Hold rule applies | `held`; released oldest-first; `--supersede` bypasses |
| Paste not visible | up to 3 attempts, then `failed` + alert |
| Enter swallowed | up to 2 extra Enters, then C-c, `failed` + alert |
| No prompt-submit hook but agent went busy | `delivered`, `unconfirmed=true`, warning |
| `/clear` or `--resume` inside a pane | `restarting`; no cascade; `down(no-restart)` after 60 s |
| Claude process exits | `down(pane_dead)` after 5 s → cascade → `ads restart <agent>` |
| Unrecoverable API error | `system(api-error)` to the waiting senders |
| Agent ends a turn without replying | up to 2 Stop-hook nudges, then `failed` + `system(missing-report)` |
| Supervisor restarted | running agents adopted; their `delivering` messages requeued |

Recover a `failed` message with `ads send --requeue <id>`, which sets it back to `queued` with zero retry counts.

## Hooks (`ads hook <event>`, `hooks.py`)

The hooks are installed through `--settings <state>/work/agents/<agent>/settings.json`, whose `env` carries `ADS_STATE_DIR`, `ADS_RUNTIME`, `ADS_PROJECT` and `ADS_AGENT`. The hook finds its project from `$ADS_STATE_DIR` (falling back to the project `$ADS_PROJECT` registered in `$ADS_RUNTIME`); without a project or an agent it exits 0 silently. They use only the standard library, `ads.projects` and `ads.bus`. Every event is appended to `<state>/work/logs/hooks.log` first. A hook never fails the Claude session: errors are logged as traceback records, and the exit code is always 0.

| Event | Handler |
|---|---|
| SessionStart | state table; additionalContext `ADS: you are <agent>. <n> message(s) pending.` |
| UserPromptSubmit | pointer → `mark_delivered` + front matter as additionalContext; otherwise logged as `manual` |
| Stop | `stop_decision` first, then the state write; prints the block JSON or nothing; touches poke |
| StopFailure | state table; alert file if unrecoverable; touches poke |
| SessionEnd | one locked state write (1 s timeout) |

## Lab Notes

`ads note "text"` appends `- [YYYY-MM-DD <agent>] text` under `## Lab Notes` in the project's `<state>/CLAUDE.md` (the project is selected as for every command: `-p`, `$ADS_STATE_DIR`, cwd, the only running project). The author comes from `$ADS_AGENT`, or is `human`. The write takes a flock and replaces the file atomically. The section is created if it is missing, and a warning is printed above 80 entries. Every agent of the project loads this file through `--add-dir <state>` plus `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`, and is told to re-read the Lab Notes at the start of each task. Agents never load another project's notes, nor the ads repo's own `CLAUDE.md`.
