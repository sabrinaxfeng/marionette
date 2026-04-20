#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_FILE="$REPO_ROOT/automation/research_loop/config.json"
TIMEOUT_SECONDS=60

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --timeout)
            TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        -h|--help)
            cat <<'EOF'
Usage: supervisor_daemon.sh [--config PATH] [--timeout SECONDS]

Run the research-loop supervisor daemon. It:
- writes a heartbeat file
- dispatches runnable tasks
- waits on queue changes with inotifywait
EOF
            exit 0
            ;;
        *)
            echo "[supervisor] unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

PATHS_JSON="$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE"
import json
import sys
from pathlib import Path

from lib.research_loop import (
    load_config,
    queue_dirs,
    supervisor_dir,
    supervisor_heartbeat_path,
    supervisor_lock_path,
    supervisor_pid_path,
)

config_path = Path(sys.argv[1]).resolve()
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
dirs = queue_dirs(config)
print(json.dumps({
    "pending": str(dirs["pending"]),
    "running": str(dirs["running"]),
    "completed": str(dirs["completed"]),
    "failed": str(dirs["failed"]),
    "supervisor_dir": str(supervisor_dir(config)),
    "heartbeat": str(supervisor_heartbeat_path(config)),
    "lock": str(supervisor_lock_path(config)),
    "pid": str(supervisor_pid_path(config)),
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

PENDING_DIR="$(get_path pending)"
RUNNING_DIR="$(get_path running)"
COMPLETED_DIR="$(get_path completed)"
FAILED_DIR="$(get_path failed)"
SUPERVISOR_DIR="$(get_path supervisor_dir)"
HEARTBEAT_PATH="$(get_path heartbeat)"
LOCK_PATH="$(get_path lock)"
PID_PATH="$(get_path pid)"
LOG_PATH="$SUPERVISOR_DIR/daemon.log"

mkdir -p "$SUPERVISOR_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
    echo "[supervisor] another supervisor daemon is already running" >&2
    exit 1
fi

echo "$$" > "$PID_PATH"

write_heartbeat() {
    local state="$1"
    local event="$2"
    PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$HEARTBEAT_PATH" "$state" "$event" "$$"
import json
import sys
from pathlib import Path

heartbeat_path = Path(sys.argv[1])
payload = {
    "pid": int(sys.argv[4]),
    "state": sys.argv[2],
    "last_event": sys.argv[3],
}
heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
tmp = heartbeat_path.with_name(f"{heartbeat_path.name}.tmp")
tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
tmp.replace(heartbeat_path)
PY
}

cleanup() {
    write_heartbeat "stopped" "shutdown" || true
    rm -f "$PID_PATH"
}
trap cleanup EXIT INT TERM

dispatch_once() {
    local output rc
    output="$(PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/automation/research_loop/dispatch_ready_tasks.py" --config "$CONFIG_FILE" 2>&1)" || rc=$?
    rc="${rc:-0}"
    printf '[%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$output" >> "$LOG_PATH"
    return "$rc"
}

LAST_EVENT="startup"
write_heartbeat "dispatching" "$LAST_EVENT"
dispatch_once || true

while true; do
    write_heartbeat "waiting" "$LAST_EVENT"
    EVENT_OUTPUT="$(inotifywait -qq -t "$TIMEOUT_SECONDS" --format '%w%f %e' \
        -e close_write -e create -e moved_to -e delete \
        "$PENDING_DIR" "$RUNNING_DIR" "$COMPLETED_DIR" "$FAILED_DIR" 2>/dev/null || true)"
    if [[ -z "$EVENT_OUTPUT" ]]; then
        LAST_EVENT="timeout"
    else
        LAST_EVENT="$EVENT_OUTPUT"
    fi
    write_heartbeat "dispatching" "$LAST_EVENT"
    dispatch_once || true
done
