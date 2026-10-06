#!/usr/bin/env bash
# M0.5 smoke: real `claude -p` with ads-generated settings must fire the ads hooks.
# Asserts work/logs/hooks.log gained session-start, prompt-submit and stop lines for this run
# (matched by the run's --session-id). Costs one tiny Sonnet request.
set -euo pipefail

RUNTIME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RUNTIME/.venv/bin/python"
CLAUDE="${ADS_CLAUDE_BIN:-claude}"
AGENT=planner
MODEL="${ADS_SMOKE_MODEL:-claude-sonnet-5-5}"
PROJECT="$(mktemp -d /tmp/ads-smoke-XXXXXX)"
trap 'rm -rf "$PROJECT"' EXIT
LOG="$RUNTIME/work/logs/hooks.log"
SID="$("$PY" -c 'import uuid; print(uuid.uuid4())')"

fail() { echo "smoke_claude: FAIL: $*" >&2; exit 1; }

SETTINGS="$("$PY" - "$RUNTIME" "$PROJECT" "$AGENT" <<'PYEOF'
import sys
from ads.config import load_config
from ads.launcher import render_agent_files
runtime, project, agent = sys.argv[1:]
settings, _prompt = render_agent_files(load_config(runtime=runtime), runtime, project, agent)
print(settings)
PYEOF
)"
[[ -f "$SETTINGS" ]] || fail "settings not rendered"

mkdir -p "$(dirname "$LOG")"
touch "$LOG"
before=$(wc -l < "$LOG")

echo "smoke_claude: project=$PROJECT session=$SID settings=$SETTINGS"
out="$(cd "$PROJECT" && env \
    ADS_AGENT="$AGENT" ADS_RUNTIME="$RUNTIME" ADS_PROJECT="$PROJECT" ADS_BIN="$RUNTIME/.venv/bin/ads" \
    CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    PATH="$RUNTIME/.venv/bin:$PATH" \
    timeout 180 "$CLAUDE" -p "say ok" --model "$MODEL" --settings "$SETTINGS" --add-dir "$RUNTIME" \
        --dangerously-skip-permissions --session-id "$SID" </dev/null 2>&1)" || fail "claude -p exited non-zero: $out"
echo "smoke_claude: claude said: $out"

new="$(tail -n +"$((before + 1))" "$LOG" | grep -F "\"session_id\": \"$SID\"" || true)"
missing=()
for ev in session-start prompt-submit stop; do
    grep -qF "\"event\": \"$ev\"" <<<"$new" || missing+=("$ev")
done
if ((${#missing[@]})); then
    echo "--- new hooks.log lines for this session:" >&2
    echo "$new" >&2
    fail "hooks.log lacks event(s): ${missing[*]} (session $SID)"
fi
echo "smoke_claude: events for this run: $(sed -E 's/.*"event": "([a-z-]+)".*/\1/' <<<"$new" | tr '\n' ' ')"
echo "smoke_claude: PASS"
