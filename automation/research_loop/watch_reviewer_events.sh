#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_FILE="$REPO_ROOT/automation/research_loop/config.json"
ONCE=0
TASK_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --task-id)
            TASK_ID="$2"
            shift 2
            ;;
        --once)
            ONCE=1
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Usage: watch_reviewer_events.sh [--config PATH] [--task-id TASK_ID] [--once]

Watch the reviewer-events drop zone and print a short summary for each new event bundle.
If --task-id is set, watch the stable per-task latest-event pointer instead of the global stream.
EOF
            exit 0
            ;;
        *)
            echo "[watch-reviewer-events] unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

PATHS_JSON="$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$TASK_ID"
from pathlib import Path
import json
import sys

from lib.research_loop import (
    load_config,
    reviewer_events_dir,
    reviewer_latest_event_path,
    reviewer_task_events_dir,
)

config_path = Path(sys.argv[1]).resolve()
task_id = sys.argv[2]
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
if task_id:
    latest_path = reviewer_task_events_dir(config, task_id) / "latest_event.json"
else:
    latest_path = reviewer_latest_event_path(config)
print(json.dumps({
    "events_dir": str(reviewer_events_dir(config)),
    "latest_event_json": str(latest_path),
}))
PY
)"

get_path() {
    local key="$1"
    python3 - <<'PY' "$PATHS_JSON" "$key"
import json
import sys

payload = json.loads(sys.argv[1])
print(payload[sys.argv[2]])
PY
}

EVENTS_DIR="$(get_path events_dir)"
LATEST_EVENT_JSON="$(get_path latest_event_json)"
LATEST_EVENT_DIR="$(dirname "$LATEST_EVENT_JSON")"

mkdir -p "$EVENTS_DIR"
mkdir -p "$LATEST_EVENT_DIR"

render_latest() {
    local latest_json="$1"
    python3 - <<'PY' "$latest_json"
import json
import sys
from pathlib import Path

latest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
event_json = Path(latest["event_json"])
payload = json.loads(event_json.read_text(encoding="utf-8"))
task = payload.get("task") or {}
print(
    f"[reviewer-event] trigger={payload.get('trigger')} "
    f"job_id={payload.get('job_id')} "
    f"task_id={task.get('task_id')} "
    f"message={payload.get('message')}"
)
print(f"[reviewer-event] bundle={event_json.parent}")
PY
}

latest_mtime() {
    local path="$1"
    python3 - <<'PY' "$path"
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print("")
else:
    print(path.stat().st_mtime_ns)
PY
}

LAST_RENDERED_MTIME=""

if [[ -f "$LATEST_EVENT_JSON" ]]; then
    render_latest "$LATEST_EVENT_JSON"
    LAST_RENDERED_MTIME="$(latest_mtime "$LATEST_EVENT_JSON")"
    if [[ "$ONCE" -eq 1 ]]; then
        exit 0
    fi
fi

while true; do
    inotifywait -qq --format '%f %e' -e close_write -e create -e moved_to "$LATEST_EVENT_DIR" >/dev/null
    for _ in $(seq 1 20); do
        if [[ -f "$LATEST_EVENT_JSON" ]]; then
            break
        fi
        sleep 0.1
    done
    if [[ -f "$LATEST_EVENT_JSON" ]]; then
        CURRENT_MTIME="$(latest_mtime "$LATEST_EVENT_JSON")"
        if [[ -n "$LAST_RENDERED_MTIME" && "$CURRENT_MTIME" == "$LAST_RENDERED_MTIME" ]]; then
            continue
        fi
        render_latest "$LATEST_EVENT_JSON"
        LAST_RENDERED_MTIME="$CURRENT_MTIME"
        if [[ "$ONCE" -eq 1 ]]; then
            exit 0
        fi
    fi
done
