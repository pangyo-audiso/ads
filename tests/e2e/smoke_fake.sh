#!/usr/bin/env bash
# M7 smoke test: a full ads cell with fake agents (tests/fake_agent.py), no real Claude.
#
#   bash tests/e2e/smoke_fake.sh          (from the repo root; needs .venv and tmux)
#
# Uses a throw-away runtime + two projects under mktemp and private tmux sockets (prefix
# ads-smoke-<pid>, one server per project), so the real runtime (projects/) and the real
# `ads-*` tmux servers are never touched. Checks:
#   1. `ads <project> --yes --no-attach` brings up 7 idle agents + editor + supervisor, then a
#      SECOND project starts concurrently from the same runtime on its own socket
#   2. a human instruct runs the full delegation chain
#      human -> orchestrator -> planner -> evaluator (review) -> planner -> orchestrator -> human
#   3. a second human instruct sent while the first is open is NOT held (human exempt), but the
#      orchestrator's second instruct to planner IS held behind the first and released later
#   4. typing into the editor pane (window 0, pane 3) sends an instruct from human
#   4b. the second project runs its own delegation chain; its bus never sees project 1's messages
#   5. `ads status --json -p …`: 0 open tasks, nothing failed/queued/held, supervisor alive (both)
#      and `ads list` shows both running
#   6. `ads stop -p project` leaves project2 running; `ads stop --all` stops it; agents
#      down(shutdown), supervisors gone, no tmux server on either socket
# Exit 0 on success; non-zero with a FAIL line otherwise (the temp dir is kept for debugging).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ADS="$REPO/.venv/bin/ads"
PY="$REPO/.venv/bin/python"
FAKE="$REPO/tests/fake_agent.py"
[[ -x "$ADS" ]] || { echo "FAIL: $ADS not found (create the venv: python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]')"; exit 2; }
command -v tmux >/dev/null || { echo "FAIL: tmux not installed"; exit 2; }

TMP="$(mktemp -d /tmp/ads-smoke-XXXXXX)"
RT="$TMP/runtime"
PROJECT="$TMP/project"
PROJECT2="$TMP/project2"
PREFIX="ads-smoke-$$"
SOCK="$PREFIX-project"     # [ads] tmux_socket is a prefix: <prefix>-<project name>
SOCK2="$PREFIX-project2"
ST="$RT/projects/project"
ST2="$RT/projects/project2"
mkdir -p "$RT"
cp "$REPO/ads.toml" "$RT/ads.toml"

# private socket, fast ticks, short startup timeout
"$PY" - "$RT/ads.toml" "$PREFIX" <<'EOF'
import re, sys
path, sock = sys.argv[1], sys.argv[2]
text = open(path).read()
for key, val in (("tmux_socket", f'"{sock}"'), ("tick_ms", "100"), ("startup_timeout_s", "30"),
                 ("attach", "false"), ("confirm_timeout_s", "5")):
    text, n = re.subn(rf"(?m)^{key} = [^#\n]*", f"{key} = {val}   ", text, count=1)
    assert n == 1, key
open(path, "w").write(text)
EOF

# fake agents: every agent auto-replies; orchestrator delegates to planner, planner asks
# evaluator for a review. planner/evaluator are slow enough for the hold check.
cat > "$RT/fake.json" <<'EOF'
{"*": {"reply": "1", "busy_s": "0.3"},
 "orchestrator": {"delegate": "planner"},
 "planner": {"delegate": "evaluator:review-request", "busy_s": "1.5"},
 "evaluator": {"busy_s": "1.5"}}
EOF

export ADS_RUNTIME="$RT" ADS_CLAUDE_BIN="$FAKE"
unset TMUX ADS_AGENT ADS_PROJECT ADS_STATE_DIR || true

ok=0
cleanup() {
    "$ADS" stop --all >/dev/null 2>&1 || true
    tmux -L "$SOCK" kill-server >/dev/null 2>&1 || true
    tmux -L "$SOCK2" kill-server >/dev/null 2>&1 || true
    if [[ $ok == 1 ]]; then rm -rf "$TMP"; else echo "(kept $TMP for debugging: runtime/projects/*/work/logs/*)"; fi
}
trap cleanup EXIT

fail() { echo "FAIL: $*"; exit 1; }
step() { echo "== $*"; }

# check [--state DIR] <name>: assertions on a project's bus/ledger state (default: project 1)
check() {
    local st="$ST"
    if [[ $1 == --state ]]; then st="$2"; shift 2; fi
    "$PY" - "$st" "$@" <<'EOF'
import json, sys, time
from ads.paths import ProjectState
from ads.bus import ledger, store
from ads.bus.log import read_events
rt = ProjectState.at(sys.argv[1]); what = sys.argv[2]; args = sys.argv[3:]

def wait(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    return pred()

def die(msg):
    print(msg); sys.exit(1)

if what == "closed":   # closed <task id> <timeout>
    t = wait(lambda: (ledger.get_task(rt, args[0]) or {}).get("state") == "closed", float(args[1]))
    if not t:
        die(f"task {args[0]} not closed: {ledger.get_task(rt, args[0])}; open: "
            f"{[(x['id'], x['from'], x['to'], x['state']) for x in ledger.open_tasks(rt)]}")
elif what == "chain":  # chain <human task id>: the full delegation chain under it
    msgs = store.all_messages(rt)
    by = {m.id: m for m in msgs}
    t0 = args[0]
    def one(pred, label):
        hits = [m for m in msgs if pred(m)]
        if len(hits) != 1:
            die(f"chain {t0}: expected 1 {label}, got {[(m.id, m.from_, m.to, m.type) for m in hits]}")
        return hits[0]
    t1 = one(lambda m: m.type == "instruct" and m.parent == t0 and m.from_ == "orchestrator"
             and m.to == "planner", "orchestrator->planner instruct")
    t2 = one(lambda m: m.type == "review-request" and m.parent == t1.id and m.to == "evaluator",
             "planner->evaluator review-request")
    rv = one(lambda m: m.type == "review" and m.re == t2.id and m.result == "pass", "review pass")
    r1 = one(lambda m: m.type == "report" and m.re == t1.id and m.to == "orchestrator",
             "planner report")
    r0 = one(lambda m: m.type == "report" and m.re == t0 and m.from_ == "orchestrator"
             and m.to == "human", "orchestrator report to human")
    for m in (t1, t2, rv, r1):
        if m.status != "delivered":
            die(f"{m.id} ({m.type} {m.from_}->{m.to}) status {m.status}, expected delivered")
    if r0.status != "delivered":
        die(f"report to human {r0.id} status {r0.status}")
    order = [by[t0].seq, t1.seq, t2.seq, rv.seq, r1.seq, r0.seq]
    if order != sorted(order):
        die(f"chain out of order: {order}")
    for tid in (t0, t1.id, t2.id):
        if ledger.get_task(rt, tid)["state"] != "closed":
            die(f"task {tid} not closed")
    print(f"{t0} -> {t1.id} -> {t2.id} -> {rv.id}(pass) -> {r1.id} -> {r0.id}")
elif what == "held-then-released":   # some orchestrator->planner instruct was held and released
    ev = read_events(rt)
    held = [e for e in ev if e["event"] == "created" and e["status"] == "held"]
    if not held:
        die("no message was ever held (expected orchestrator's 2nd instruct to planner)")
    for h in held:
        if (h["from"], h["to"], h["type"]) != ("orchestrator", "planner", "instruct"):
            die(f"unexpected held message {h}")
        if not any(e["id"] == h["id"] and e["event"] == "status:queued" for e in ev):
            die(f"held message {h['id']} was never released")
    print(",".join(h["id"] for h in held))
elif what == "status-clean":
    st = json.loads(open(args[0]).read())
    if st["tasks"]:
        die(f"open tasks remain: {st['tasks']}")
    if st["messages"]:
        die(f"undelivered messages remain: {st['messages']}")
    if not st["supervisor"]["alive"]:
        die("supervisor not alive")
    bad = {a: s["state"] for a, s in st["agents"].items() if s["state"] != "idle"}
    if bad:
        die(f"agents not idle: {bad}")
    failed = [m.id for m in store.all_messages(rt) if m.status in ("failed", "ignored")]
    if failed:
        die(f"failed/ignored messages: {failed}")
    if st["alerts"]:
        die(f"alerts: {st['alerts']}")
    nudged = [t["id"] for t in ledger.all_tasks(rt) if t.get("nudges")]
    if nudged:
        die(f"tasks were nudged (missing reply): {nudged}")
elif what == "subjects":   # all message subjects of this project, comma separated
    print(",".join(sorted({m.subject for m in store.all_messages(rt)})))
elif what == "human-instruct":   # newest instruct from human whose subject contains args[0]
    hits = [m for m in store.all_messages(rt)
            if m.from_ == "human" and m.type == "instruct" and args[0] in m.subject]
    v = wait(lambda: [m for m in store.all_messages(rt)
                      if m.from_ == "human" and m.type == "instruct" and args[0] in m.subject],
             float(args[1]))
    if not v:
        die(f"no instruct from human with {args[0]!r} in the subject")
    print(v[-1].id)
else:
    die(f"unknown check {what}")
EOF
}

status_json() { "$ADS" status --json -p "${1:-project}"; }
A1=(-p project)
A2=(-p project2)

# --- 1. start -----------------------------------------------------------------------------
step "start cell (runtime $RT, socket $SOCK)"
out="$("$ADS" "$PROJECT" --yes --no-attach 2>&1)" || fail "ads start exited $?: $out"
grep -q "all 7 agents idle" <<<"$out" || fail "agents not all idle after start: $out"
[[ -d "$PROJECT/.git" && -d "$PROJECT/docs" ]] || fail "project not initialised (.git/docs)"
[[ -f "$ST/project.json" && -f "$ST/CLAUDE.md" && -d "$ST/plan/drafts" ]] || fail "no state dir $ST"
[[ ! -e "$RT/work" && ! -e "$RT/CLAUDE.md" ]] || fail "runtime-level work/ or CLAUDE.md created"
SESSION="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["session"])' "$ST/work/run/session.json")"
npanes="$(tmux -L "$SOCK" list-panes -s -t "=$SESSION" | wc -l)"
[[ $npanes == 9 ]] || fail "expected 9 panes (8 + supervisor), got $npanes"

step "start a second project concurrently (socket $SOCK2)"
out="$("$ADS" "$PROJECT2" --yes --no-attach 2>&1)" || fail "ads start project2 exited $?: $out"
grep -q "all 7 agents idle" <<<"$out" || fail "project2 agents not all idle: $out"
SESSION2="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["session"])' "$ST2/work/run/session.json")"
npanes="$(tmux -L "$SOCK2" list-panes -s -t "=$SESSION2" | wc -l)"
[[ $npanes == 9 ]] || fail "project2: expected 9 panes, got $npanes"
tmux -L "$SOCK" has-session -t "=$SESSION" 2>/dev/null || fail "project 1 session gone after starting project2"
"$ADS" status >/dev/null 2>&1 && fail "ads status without -p must be ambiguous with two running cells"

# --- 2+3. two human instructs back to back -------------------------------------------------
step "human instructs (delegation chain + hold rules)"
T0="$("$ADS" send "${A1[@]}" --from human --to orchestrator --type instruct --subject "Plan A" --body "plan something")"
T3out="$("$ADS" send "${A1[@]}" --from human --to orchestrator --type instruct --subject "Plan B" --body "plan more")"
[[ "$T3out" =~ ^m-[0-9]{8}-[0-9]{6,}$ ]] || fail "second human instruct was held or rejected: $T3out (human must be exempt)"
T3="$T3out"
msg="$(check closed "$T0" 60)" || fail "first instruct: $msg"
msg="$(check closed "$T3" 60)" || fail "second instruct: $msg"
chain="$(check chain "$T0")" || fail "$chain"
echo "   chain A: $chain"
chain="$(check chain "$T3")" || fail "$chain"
echo "   chain B: $chain"
held="$(check held-then-released)" || fail "hold: $held"
echo "   held+released: $held"

# --- 4. editor pane -------------------------------------------------------------------------
step "type into the human editor pane"
HUMAN_PANE="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["human"])' "$ST/work/run/panes.json")"
tmux -L "$SOCK" send-keys -t "$HUMAN_PANE" -l "Plan C from the editor"
tmux -L "$SOCK" send-keys -t "$HUMAN_PANE" Enter
T5="$(check human-instruct "Plan C" 15)" || fail "editor: $T5"
msg="$(check closed "$T5" 60)" || fail "editor instruct: $msg"
echo "   editor instruct $T5 closed"

# --- 4b. second project ---------------------------------------------------------------------
step "project2: its own delegation chain, isolated bus"
U0="$("$ADS" send "${A2[@]}" --from human --to orchestrator --type instruct --subject "Plan Z" --body "plan in project2")"
msg="$(check --state "$ST2" closed "$U0" 60)" || fail "project2 instruct: $msg"
chain="$(check --state "$ST2" chain "$U0")" || fail "project2: $chain"
echo "   chain Z: $chain"
subj2="$(check --state "$ST2" subjects)"
grep -q "Plan A" <<<"$subj2" && fail "project 1 message visible in project2: $subj2"
subj1="$(check subjects)"
grep -q "Plan Z" <<<"$subj1" && fail "project2 message visible in project 1: $subj1"

# --- 5. status ---------------------------------------------------------------------------------
step "ads status (both projects) + ads list"
sleep 1   # let the last Stop hooks land
status_json project > "$TMP/status.json"
msg="$(check status-clean "$TMP/status.json")" || fail "status not clean: $msg"
status_json project2 > "$TMP/status2.json"
msg="$(check --state "$ST2" status-clean "$TMP/status2.json")" || fail "project2 status not clean: $msg"
"$ADS" status "${A1[@]}" | sed 's/^/   /'
"$ADS" list | sed 's/^/   /'
"$PY" -c '
import json, sys
rows = {r["name"]: r for r in json.loads(sys.argv[1])}
assert set(rows) == {"project", "project2"}, rows
assert all(r["running"] for r in rows.values()), rows
assert rows["project"]["socket"] != rows["project2"]["socket"], rows
' "$("$ADS" list --json)" || fail "ads list"

# --- 6. stop -----------------------------------------------------------------------------------
step "ads stop -p project (project2 keeps running), then ads stop --all"
"$ADS" stop "${A1[@]}" || fail "ads stop -p project exited $?"
if tmux -L "$SOCK" list-sessions >/dev/null 2>&1; then fail "tmux server on $SOCK still running"; fi
tmux -L "$SOCK2" has-session -t "=$SESSION2" 2>/dev/null || fail "stopping project 1 stopped project2"
status_json project2 > "$TMP/status2.json"
msg="$(check --state "$ST2" status-clean "$TMP/status2.json")" || fail "project2 after stop -p project: $msg"
"$ADS" stop --all || fail "ads stop --all exited $?"
if tmux -L "$SOCK2" list-sessions >/dev/null 2>&1; then fail "tmux server on $SOCK2 still running"; fi
for p in project project2; do
st="$(status_json $p)"
"$PY" -c '
import json, sys
st = json.loads(sys.argv[1])
assert not st["supervisor"]["alive"], "supervisor still alive"
bad = {a: (s["state"], s["reason"]) for a, s in st["agents"].items() if (s["state"], s["reason"]) != ("down", "shutdown")}
assert not bad, f"agents not down(shutdown): {bad}"
' "$st" || fail "post-stop status ($p)"
done

ok=1
echo "PASS: smoke_fake (two concurrent cells, fake agents)"
