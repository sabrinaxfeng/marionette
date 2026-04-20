#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WATCHER_PATH="$REPO_ROOT/automation/research_loop/job_watcher.py"
POST_JOB_PATH="$REPO_ROOT/automation/research_loop/run_post_job.sh"

usage() {
    cat <<'EOF'
Usage:
  run_claimed_job.sh --job-dir JOB_DIR [--timeout MINUTES]
  run_claimed_job.sh --task TASK_JSON [--timeout MINUTES]

Run one already-claimed research-loop job end-to-end:
- invoke the watcher for the claimed task
- always run post-job finalization afterward
- return the watcher exit code unless post-job itself fails
EOF
}

JOB_DIR=""
TASK_PATH=""
TIMEOUT_MINUTES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --job-dir)
            JOB_DIR="${2:-}"
            shift 2
            ;;
        --task)
            TASK_PATH="${2:-}"
            shift 2
            ;;
        --timeout)
            TIMEOUT_MINUTES="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[run-claimed-job] unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -n "$TASK_PATH" && -n "$JOB_DIR" ]]; then
    echo "[run-claimed-job] pass either --task or --job-dir, not both" >&2
    exit 2
fi

if [[ -n "$TASK_PATH" ]]; then
    TASK_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$TASK_PATH")"
    JOB_DIR="$(dirname "$TASK_PATH")"
elif [[ -n "$JOB_DIR" ]]; then
    JOB_DIR="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$JOB_DIR")"
    TASK_PATH="$JOB_DIR/task.json"
else
    echo "[run-claimed-job] one of --task or --job-dir is required" >&2
    usage >&2
    exit 2
fi

if [[ ! -f "$TASK_PATH" ]]; then
    echo "[run-claimed-job] task file not found: $TASK_PATH" >&2
    exit 2
fi

if [[ -z "$TIMEOUT_MINUTES" ]]; then
    TIMEOUT_MINUTES="$(
        python3 - <<'PY' "$TASK_PATH"
import json
import sys
from pathlib import Path

task = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(int(task.get("timeout_minutes", 30) or 30))
PY
    )"
fi

echo "[run-claimed-job] start job_dir=$JOB_DIR timeout=${TIMEOUT_MINUTES}m"

WATCHER_RC=0
python3 "$WATCHER_PATH" --task "$TASK_PATH" --timeout "$TIMEOUT_MINUTES" || WATCHER_RC=$?

POST_RC=0
bash "$POST_JOB_PATH" "$JOB_DIR" || POST_RC=$?

if [[ "$POST_RC" -ne 0 ]]; then
    echo "[run-claimed-job] post-job failed rc=$POST_RC job_dir=$JOB_DIR" >&2
    exit "$POST_RC"
fi

exit "$WATCHER_RC"
