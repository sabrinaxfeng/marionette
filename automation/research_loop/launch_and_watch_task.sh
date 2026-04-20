#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LAUNCHER="$REPO_ROOT/automation/research_loop/launch_task.py"
WATCHER="$REPO_ROOT/automation/research_loop/watch_reviewer_events.sh"
CONFIG_ARG=""

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: launch_and_watch_task.sh [launch_task.py args...]

Atomically submit one task, then watch its per-task reviewer-event pointer until
the first event arrives. This is the reviewer-facing convenience entrypoint.

Examples:
  bash automation/research_loop/launch_and_watch_task.sh --task-file /abs/path/to/codex_task.json
  bash automation/research_loop/launch_and_watch_task.sh --task-id EXISTING_PENDING_TASK
EOF
    exit 0
fi

ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
    arg="${ARGS[$i]}"
    if [[ "$arg" == "--config" && $((i + 1)) -lt ${#ARGS[@]} ]]; then
        CONFIG_ARG="${ARGS[$((i + 1))]}"
    elif [[ "$arg" == --config=* ]]; then
        CONFIG_ARG="${arg#--config=}"
    fi
done

RESULT_JSON="$(mktemp)"
cleanup() {
    rm -f "$RESULT_JSON"
}
trap cleanup EXIT

if ! python3 "$LAUNCHER" "$@" >"$RESULT_JSON"; then
    cat "$RESULT_JSON"
    exit 1
fi

cat "$RESULT_JSON"

TASK_ID="$(python3 - <<'PY' "$RESULT_JSON"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not payload.get("accepted"):
    raise SystemExit(1)
print(payload["task_id"])
PY
)"

echo "[launch-and-watch] watching reviewer events for task_id=$TASK_ID" >&2
if [[ -n "$CONFIG_ARG" ]]; then
    exec bash "$WATCHER" --config "$CONFIG_ARG" --task-id "$TASK_ID" --once
fi
exec bash "$WATCHER" --task-id "$TASK_ID" --once
