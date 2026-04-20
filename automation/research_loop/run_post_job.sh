#!/usr/bin/env bash
# Post-job glue: queue management + selective reviewer resume.
#
# The analyst self-iterates through a dependency graph:
# - completed jobs may fan out into multiple next tasks
# - runnable tasks are dispatched up to the parallelism cap
# - The reviewer is only resumed when the graph is done, blocked, or explicitly escalated
#
# Usage: run_post_job.sh JOB_DIR
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
JOB_DIR="${1:?Usage: run_post_job.sh JOB_DIR}"
QUEUE_BASE="$REPO_ROOT/results/research_loop/queue"
CONFIG_FILE="$REPO_ROOT/automation/research_loop/config.json"
DISPATCHER="$REPO_ROOT/automation/research_loop/dispatch_ready_tasks.py"

STATUS=$(python3 -c "import json; print(json.load(open('$JOB_DIR/job_status.json'))['status'])")
JOB_ID=$(python3 -c "import json; print(json.load(open('$JOB_DIR/job_status.json'))['job_id'])")
TASK_META=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$JOB_DIR"
import json
import sys
from pathlib import Path

from lib.research_loop import normalize_task

job_dir = Path(sys.argv[1])
task = normalize_task(json.loads((job_dir / "task.json").read_text(encoding="utf-8")))
print(json.dumps({"graph_id": task["graph_id"], "task_group_id": task["task_group_id"]}))
PY
)
GRAPH_ID=$(python3 -c "import json; print(json.loads('''$TASK_META''')['graph_id'])")
TASK_GROUP_ID=$(python3 -c "import json; print(json.loads('''$TASK_META''')['task_group_id'])")

notify_reviewer() {
    local message="$1"
    local trigger="${2:-job_update}"
    local event_job_dir="${3:-}"
    local output
    if ! output=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$message" "$trigger" "$event_job_dir"
import json
import sys
from pathlib import Path

from lib.research_loop import load_config, notify_reviewer

config_path = Path(sys.argv[1]).resolve()
message = sys.argv[2]
trigger = sys.argv[3]
job_dir_raw = sys.argv[4]
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
job_dir = Path(job_dir_raw).resolve() if job_dir_raw else None
result = notify_reviewer(config, message, trigger=trigger, job_dir=job_dir)
print(json.dumps(result))
PY
    ); then
        echo "[post-job] reviewer notify failed for trigger=$trigger" >&2
        return 0
    fi
    echo "[post-job] reviewer notify: $output"
}

hold_graph() {
    local graph_id="$1"
    local reason="$2"
    local output
    if ! output=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$graph_id" "$reason" "$JOB_ID"
from pathlib import Path
import json
import sys

from lib.research_loop import load_config, set_graph_hold

config_path = Path(sys.argv[1]).resolve()
graph_id = sys.argv[2]
reason = sys.argv[3]
job_id = sys.argv[4]
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
path = set_graph_hold(config, graph_id, reason=reason, job_id=job_id)
print(json.dumps({"graph_id": graph_id, "reason": reason, "path": str(path)}))
PY
    ); then
        echo "[post-job] failed to hold graph $graph_id" >&2
        return 0
    fi
    echo "[post-job] graph hold: $output"
}

normalize_bundle_metadata() {
    local old_dir="$1"
    local new_dir="$2"
    local target_status="$3"
    if ! PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$old_dir" "$new_dir" "$target_status"
from pathlib import Path
import sys

from lib.research_loop import relocate_job_bundle_metadata

old_dir = Path(sys.argv[1]).resolve()
new_dir = Path(sys.argv[2]).resolve()
target_status = sys.argv[3] or None
relocate_job_bundle_metadata(old_dir, new_dir, target_status=target_status)
PY
    then
        echo "[post-job] bundle metadata normalization failed for $new_dir" >&2
    fi
}

dispatch_scheduler() {
    local exclude_graph_id="${1:-}"
    local output
    if PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" >/dev/null 2>&1
from pathlib import Path
import sys

from lib.research_loop import load_config, supervisor_is_healthy

config_path = Path(sys.argv[1]).resolve()
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
raise SystemExit(0 if supervisor_is_healthy(config) else 1)
PY
    then
        if output=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$JOB_ID" "$exclude_graph_id"
import json
import sys
from pathlib import Path

from lib.research_loop import load_config, touch_queue_nudge

config_path = Path(sys.argv[1]).resolve()
job_id = sys.argv[2]
exclude_graph_id = sys.argv[3]
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
reason = f"post_job:{job_id}"
if exclude_graph_id:
    reason += f":exclude:{exclude_graph_id}"
path = touch_queue_nudge(config, reason=reason)
print(json.dumps({"mode": "nudge", "path": str(path), "reason": reason}))
PY
        ); then
            echo "[post-job] supervisor nudged: $output"
            return 0
        fi
        echo "[post-job] supervisor nudge failed; falling back to direct dispatch" >&2
    fi

    local -a cmd=(python3 "$DISPATCHER" --config "$CONFIG_FILE")
    if [ -n "$exclude_graph_id" ]; then
        cmd+=(--exclude-graph-id "$exclude_graph_id")
    fi
    if ! output=$("${cmd[@]}" 2>&1); then
        echo "[post-job] scheduler dispatch failed: $output" >&2
        return 0
    fi
    echo "[post-job] scheduler dispatch: $output"
}

if [ "$STATUS" != "completed" ]; then
    echo "[post-job] $JOB_ID: $STATUS — moving to failed, notifying reviewer"
    mkdir -p "$QUEUE_BASE/failed"
    mv "$JOB_DIR" "$QUEUE_BASE/failed/$JOB_ID" 2>/dev/null || true
    normalize_bundle_metadata "$JOB_DIR" "$QUEUE_BASE/failed/$JOB_ID" "$STATUS"
    hold_graph "$GRAPH_ID" "job_status:$STATUS"
    dispatch_scheduler "$GRAPH_ID"
    notify_reviewer "Job $JOB_ID finished with status: $STATUS. Check results/research_loop/queue/failed/$JOB_ID/ for details." "job_failed" "$QUEUE_BASE/failed/$JOB_ID"
    exit 0
fi

ANALYSIS_FILE="$JOB_DIR/analysis.json"
if [ ! -f "$ANALYSIS_FILE" ]; then
    echo "[post-job] $JOB_ID: no analysis.json — notifying reviewer"
    mkdir -p "$QUEUE_BASE/completed"
    mv "$JOB_DIR" "$QUEUE_BASE/completed/$JOB_ID" 2>/dev/null || true
    normalize_bundle_metadata "$JOB_DIR" "$QUEUE_BASE/completed/$JOB_ID" "completed"
    hold_graph "$GRAPH_ID" "missing_analysis"
    dispatch_scheduler "$GRAPH_ID"
    notify_reviewer "Job $JOB_ID completed but analyst produced no analysis. Check results/research_loop/queue/completed/$JOB_ID/" "missing_analysis" "$QUEUE_BASE/completed/$JOB_ID"
    exit 0
fi

DECISION=$(python3 -c "import json; print(json.load(open('$ANALYSIS_FILE'))['decision'])")
SUMMARY=$(python3 -c "import json; print(json.load(open('$ANALYSIS_FILE')).get('summary','no summary'))")

echo "[post-job] $JOB_ID: decision=$DECISION"

mkdir -p "$QUEUE_BASE/completed"
mv "$JOB_DIR" "$QUEUE_BASE/completed/$JOB_ID" 2>/dev/null || true
COMPLETED_DIR="$QUEUE_BASE/completed/$JOB_ID"
normalize_bundle_metadata "$JOB_DIR" "$COMPLETED_DIR" "completed"

QUEUE_OUTPUT=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$COMPLETED_DIR"
import json
import sys
from pathlib import Path

from lib.research_loop import (
    analysis_follow_up_tasks,
    load_config,
    normalize_task,
    write_task,
)

config_path = Path(sys.argv[1]).resolve()
job_dir = Path(sys.argv[2]).resolve()
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
analysis = json.loads((job_dir / "analysis.json").read_text(encoding="utf-8"))
parent_task = normalize_task(json.loads((job_dir / "task.json").read_text(encoding="utf-8")))
follow_ups = analysis_follow_up_tasks(analysis, parent_task=parent_task)
queued = []
error = None
try:
    for task in follow_ups:
        write_task(config, task)
        queued.append(task["task_id"])
except Exception as exc:  # noqa: BLE001
    error = str(exc)
print(json.dumps({"graph_id": parent_task["graph_id"], "queued_task_ids": queued, "error": error}))
PY
)

QUEUE_ERROR=$(python3 -c "import json; print(json.loads('''$QUEUE_OUTPUT''').get('error') or '')")
if [ -n "$QUEUE_ERROR" ]; then
    hold_graph "$GRAPH_ID" "queue_error"
    dispatch_scheduler "$GRAPH_ID"
    notify_reviewer "Graph $GRAPH_ID hit a queueing error after job $JOB_ID: $QUEUE_ERROR. Check results/research_loop/queue/completed/$JOB_ID/analysis.json" "queue_error" "$COMPLETED_DIR"
    exit 0
fi

QUEUED_COUNT=$(python3 -c "import json; print(len(json.loads('''$QUEUE_OUTPUT''')['queued_task_ids']))")
if [ "$QUEUED_COUNT" -gt 0 ]; then
    echo "[post-job] queued $QUEUED_COUNT follow-up task(s) for graph $GRAPH_ID"
fi

dispatch_scheduler

GRAPH_STATE=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$GRAPH_ID"
import json
import sys
from pathlib import Path

from lib.research_loop import graph_snapshot, load_config

config_path = Path(sys.argv[1]).resolve()
graph_id = sys.argv[2]
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
print(json.dumps(graph_snapshot(config, graph_id)))
PY
)

GROUP_STATE=$(PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE" "$TASK_GROUP_ID"
import json
import sys
from pathlib import Path

from lib.research_loop import load_config, task_group_snapshot

config_path = Path(sys.argv[1]).resolve()
task_group_id = sys.argv[2]
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)
print(json.dumps(task_group_snapshot(config, task_group_id)))
PY
)

RUNNING_COUNT=$(python3 -c "import json; print(len(json.loads('''$GRAPH_STATE''')['running']))")
RUNNABLE_COUNT=$(python3 -c "import json; print(len(json.loads('''$GRAPH_STATE''')['runnable_pending']))")
BLOCKED_COUNT=$(python3 -c "import json; print(len(json.loads('''$GRAPH_STATE''')['blocked_pending']))")
FAILED_COUNT=$(python3 -c "import json; print(len(json.loads('''$GRAPH_STATE''')['failed']))")

if [[ "$DECISION" =~ ^(accept|rerun|diagnose)$ ]] && { [ "$RUNNING_COUNT" -gt 0 ] || [ "$RUNNABLE_COUNT" -gt 0 ]; }; then
    echo "[post-job] graph $GRAPH_ID still active: running=$RUNNING_COUNT runnable=$RUNNABLE_COUNT blocked=$BLOCKED_COUNT (reviewer NOT notified)"
    exit 0
fi

if [ "$BLOCKED_COUNT" -gt 0 ] && [ "$RUNNING_COUNT" -eq 0 ] && [ "$RUNNABLE_COUNT" -eq 0 ]; then
    notify_reviewer "Graph $GRAPH_ID / task group $TASK_GROUP_ID is blocked after job $JOB_ID (decision: $DECISION). Summary: $SUMMARY. Graph state: $GRAPH_STATE. Group state: $GROUP_STATE. Check results/research_loop/queue/completed/$JOB_ID/analysis.json" "graph_blocked" "$COMPLETED_DIR"
    exit 0
fi

if [ "$FAILED_COUNT" -gt 0 ] && [ "$RUNNING_COUNT" -eq 0 ] && [ "$RUNNABLE_COUNT" -eq 0 ]; then
    notify_reviewer "Graph $GRAPH_ID / task group $TASK_GROUP_ID has failed branches after job $JOB_ID (decision: $DECISION). Summary: $SUMMARY. Graph state: $GRAPH_STATE. Group state: $GROUP_STATE. Check results/research_loop/queue/completed/$JOB_ID/analysis.json" "graph_failed" "$COMPLETED_DIR"
    exit 0
fi

GROUP_STATUS=$(python3 -c "import json; print(json.loads('''$GROUP_STATE''')['status'])")
if [ "$GROUP_STATUS" = "ready_for_synthesis" ]; then
    notify_reviewer "Task group $TASK_GROUP_ID is ready for synthesis after job $JOB_ID in graph $GRAPH_ID (decision: $DECISION). Summary: $SUMMARY. Group state: $GROUP_STATE. Graph state: $GRAPH_STATE. Check results/research_loop/queue/completed/$JOB_ID/analysis.json" "ready_for_synthesis" "$COMPLETED_DIR"
    exit 0
fi

notify_reviewer "Analyst completed job $JOB_ID in graph $GRAPH_ID / task group $TASK_GROUP_ID (decision: $DECISION). Summary: $SUMMARY. Graph state: $GRAPH_STATE. Group state: $GROUP_STATE. Full analysis at results/research_loop/queue/completed/$JOB_ID/analysis.json" "job_completed" "$COMPLETED_DIR"
