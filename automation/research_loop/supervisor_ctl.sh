#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_FILE="$REPO_ROOT/automation/research_loop/config.json"
DAEMON_TIMEOUT_SECONDS=60
START_WAIT_SECONDS=10
COMMAND="${1:-status}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: supervisor_ctl.sh <start|stop|restart|status|logs> [--config PATH] [--timeout SECONDS] [--wait SECONDS]

Control the research-loop supervisor daemon.
- start: daemonize supervisor_daemon.sh and wait for a healthy heartbeat
- stop: stop the running supervisor daemon if present
- restart: stop then start
- status: print heartbeat/pid summary
- logs: print recent supervisor logs
EOF
    exit 0
fi

if [[ $# -gt 0 ]]; then
    shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --timeout)
            DAEMON_TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        --wait)
            START_WAIT_SECONDS="$2"
            shift 2
            ;;
        -h|--help)
            cat <<'EOF'
Usage: supervisor_ctl.sh <start|stop|restart|status|logs> [--config PATH] [--timeout SECONDS] [--wait SECONDS]

Control the research-loop supervisor daemon.
- start: daemonize supervisor_daemon.sh and wait for a healthy heartbeat
- stop: stop the running supervisor daemon if present
- restart: stop then start
- status: print heartbeat/pid summary
- logs: print recent supervisor logs
EOF
            exit 0
            ;;
        *)
            echo "[supervisor-ctl] unknown argument: $1" >&2
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
    supervisor_dir,
    supervisor_heartbeat_path,
    supervisor_is_healthy,
    supervisor_pid_path,
)

config_path = Path(sys.argv[1]).resolve()
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
print(json.dumps({
    "supervisor_dir": str(supervisor_dir(config)),
    "heartbeat": str(supervisor_heartbeat_path(config)),
    "pid": str(supervisor_pid_path(config)),
    "healthy": supervisor_is_healthy(config),
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

SUPERVISOR_DIR="$(get_path supervisor_dir)"
HEARTBEAT_PATH="$(get_path heartbeat)"
PID_PATH="$(get_path pid)"
DAEMON_LOG_PATH="$SUPERVISOR_DIR/daemon.log"
OUT_LOG_PATH="$SUPERVISOR_DIR/supervisor.out.log"
DAEMON_SCRIPT="$REPO_ROOT/automation/research_loop/supervisor_daemon.sh"

mkdir -p "$SUPERVISOR_DIR"

supervisor_healthy() {
    PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE"
from pathlib import Path
import sys

from lib.research_loop import load_config, supervisor_is_healthy

config_path = Path(sys.argv[1]).resolve()
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
raise SystemExit(0 if supervisor_is_healthy(config) else 1)
PY
}

print_status() {
    PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$HEARTBEAT_PATH" "$PID_PATH" "$DAEMON_LOG_PATH" "$OUT_LOG_PATH"
import json
import os
import sys
from pathlib import Path

from lib.research_loop import load_config, supervisor_is_healthy

config_path = Path(sys.argv[1]).resolve()
heartbeat_path = Path(sys.argv[2])
pid_path = Path(sys.argv[3])
daemon_log = Path(sys.argv[4])
out_log = Path(sys.argv[5])
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)

payload = {
    "healthy": supervisor_is_healthy(config),
    "heartbeat_path": str(heartbeat_path),
    "pid_path": str(pid_path),
    "daemon_log": str(daemon_log),
    "stdout_log": str(out_log),
}
if heartbeat_path.exists():
    payload["heartbeat"] = json.loads(heartbeat_path.read_text(encoding="utf-8"))
if pid_path.exists():
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        payload["pid_alive"] = True
        payload["pid"] = pid
    except Exception:
        payload["pid_alive"] = False
print(json.dumps(payload, indent=2))
PY
}

start_supervisor() {
    if supervisor_healthy; then
        echo "[supervisor-ctl] supervisor already healthy"
        print_status
        return 0
    fi

    if [[ -f "$PID_PATH" ]]; then
        local old_pid=""
        old_pid="$(tr -d '[:space:]' < "$PID_PATH" || true)"
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" >/dev/null 2>&1; then
            echo "[supervisor-ctl] supervisor pid file exists and process is alive; waiting for heartbeat" >&2
        fi
    fi

    if command -v setsid >/dev/null 2>&1; then
        nohup setsid bash "$DAEMON_SCRIPT" --config "$CONFIG_FILE" --timeout "$DAEMON_TIMEOUT_SECONDS" \
            </dev/null >>"$OUT_LOG_PATH" 2>&1 &
    else
        nohup bash "$DAEMON_SCRIPT" --config "$CONFIG_FILE" --timeout "$DAEMON_TIMEOUT_SECONDS" \
            </dev/null >>"$OUT_LOG_PATH" 2>&1 &
    fi
    local spawned_pid=$!

    local max_checks=$(( START_WAIT_SECONDS * 10 ))
    local i
    for i in $(seq 1 "$max_checks"); do
        if supervisor_healthy; then
            echo "[supervisor-ctl] supervisor started"
            print_status
            return 0
        fi
        if ! kill -0 "$spawned_pid" >/dev/null 2>&1 && [[ ! -f "$PID_PATH" ]]; then
            break
        fi
        sleep 0.1
    done

    echo "[supervisor-ctl] supervisor failed to reach healthy state" >&2
    if [[ -f "$OUT_LOG_PATH" ]]; then
        echo "[supervisor-ctl] recent stdout/stderr:" >&2
        tail -n 20 "$OUT_LOG_PATH" >&2 || true
    fi
    if [[ -f "$DAEMON_LOG_PATH" ]]; then
        echo "[supervisor-ctl] recent daemon log:" >&2
        tail -n 20 "$DAEMON_LOG_PATH" >&2 || true
    fi
    return 1
}

stop_supervisor() {
    if [[ ! -f "$PID_PATH" ]]; then
        echo "[supervisor-ctl] no supervisor pid file"
        print_status
        return 0
    fi

    local pid=""
    pid="$(tr -d '[:space:]' < "$PID_PATH" || true)"
    if [[ -z "$pid" ]]; then
        rm -f "$PID_PATH"
        echo "[supervisor-ctl] cleared empty pid file"
        print_status
        return 0
    fi

    if ! kill -0 "$pid" >/dev/null 2>&1; then
        rm -f "$PID_PATH"
        echo "[supervisor-ctl] removed stale pid file"
        print_status
        return 0
    fi

    kill -- "-$pid" >/dev/null 2>&1 || kill "$pid" >/dev/null 2>&1 || true
    local i
    for i in $(seq 1 50); do
        if ! kill -0 "$pid" >/dev/null 2>&1; then
            echo "[supervisor-ctl] supervisor stopped"
            print_status
            return 0
        fi
        sleep 0.1
    done

    echo "[supervisor-ctl] supervisor did not stop after SIGTERM; sending SIGKILL" >&2
    kill -9 -- "-$pid" >/dev/null 2>&1 || kill -9 "$pid" >/dev/null 2>&1 || true
    sleep 0.2
    print_status
}

show_logs() {
    if [[ -f "$OUT_LOG_PATH" ]]; then
        echo "=== supervisor.out.log ==="
        tail -n 50 "$OUT_LOG_PATH"
    else
        echo "=== supervisor.out.log missing ==="
    fi
    if [[ -f "$DAEMON_LOG_PATH" ]]; then
        echo "=== daemon.log ==="
        tail -n 50 "$DAEMON_LOG_PATH"
    else
        echo "=== daemon.log missing ==="
    fi
}

case "$COMMAND" in
    start)
        start_supervisor
        ;;
    stop)
        stop_supervisor
        ;;
    restart)
        stop_supervisor
        start_supervisor
        ;;
    status)
        print_status
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "[supervisor-ctl] unknown command: $COMMAND" >&2
        exit 2
        ;;
esac
