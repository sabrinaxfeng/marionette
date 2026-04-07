#!/usr/bin/env bash
# Post-job glue: queue management + selective Claude resume.
#
# The analyst (gpt-5.4) self-iterates: it spawns workers, reviews, and queues
# follow-up tasks autonomously. Claude is only resumed when:
# - The analyst escalates (needs human/architectural decision)
# - The task frame is complete (no next_task)
# - The job failed/stalled/timed_out
#
# Usage: run_post_job.sh JOB_DIR
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
JOB_DIR="${1:?Usage: run_post_job.sh JOB_DIR}"
QUEUE_BASE="$REPO_ROOT/results/research_loop/queue"
SESSION_FILE="$REPO_ROOT/results/research_loop/claude_session.json"

STATUS=$(python3 -c "import json; print(json.load(open('$JOB_DIR/job_status.json'))['status'])")
JOB_ID=$(python3 -c "import json; print(json.load(open('$JOB_DIR/job_status.json'))['job_id'])")

notify_claude() {
    local message="$1"
    if [ ! -f "$SESSION_FILE" ]; then
        echo "[post-job] no session file — writing completion marker only"
        echo "$message" > "$QUEUE_BASE/../loop_completion.txt"
        return
    fi
    MODE=$(python3 -c "import json; print(json.load(open('$SESSION_FILE')).get('mode',''))")
    SID=$(python3 -c "import json; print(json.load(open('$SESSION_FILE')).get('session_id',''))")

    if [ "$MODE" = "interactive" ]; then
        # Interactive mode: the watcher+glue chain was launched via run_in_background.
        # Claude Code will capture our stdout as the notification. Print the result
        # clearly so it shows up in the notification. Also write the marker as a
        # fallback for --resume.
        echo ""
        echo "=== Research Loop Result ==="
        echo "$message"
        echo "============================="
        echo "$message" > "$QUEUE_BASE/../loop_completion.txt"
    elif [ -n "$SID" ]; then
        # Non-interactive mode: safe to resume via CLI
        echo "[post-job] resuming Claude session $SID"
        claude -p --resume "$SID" "$message" 2>/dev/null || true
    else
        echo "[post-job] no session_id — writing completion marker only"
        echo "$message" > "$QUEUE_BASE/../loop_completion.txt"
    fi
}

# --- Failed/stalled/timed_out: move to failed, resume Claude ---
if [ "$STATUS" != "completed" ]; then
    echo "[post-job] $JOB_ID: $STATUS — moving to failed, notifying Claude"
    mkdir -p "$QUEUE_BASE/failed"
    mv "$JOB_DIR" "$QUEUE_BASE/failed/$JOB_ID" 2>/dev/null || true
    notify_claude "Job $JOB_ID finished with status: $STATUS. Check results/research_loop/queue/failed/$JOB_ID/ for details."
    exit 0
fi

# --- Completed: read analysis, manage queue ---
ANALYSIS_FILE="$JOB_DIR/analysis.json"
if [ ! -f "$ANALYSIS_FILE" ]; then
    echo "[post-job] $JOB_ID: no analysis.json — notifying Claude"
    mkdir -p "$QUEUE_BASE/completed"
    mv "$JOB_DIR" "$QUEUE_BASE/completed/$JOB_ID" 2>/dev/null || true
    notify_claude "Job $JOB_ID completed but analyst produced no analysis. Check results/research_loop/queue/completed/$JOB_ID/"
    exit 0
fi

DECISION=$(python3 -c "import json; print(json.load(open('$ANALYSIS_FILE'))['decision'])")
HAS_NEXT=$(python3 -c "import json; t=json.load(open('$ANALYSIS_FILE')).get('next_task'); print('yes' if t else 'no')")
SUMMARY=$(python3 -c "import json; print(json.load(open('$ANALYSIS_FILE')).get('summary','no summary'))")

echo "[post-job] $JOB_ID: decision=$DECISION has_next=$HAS_NEXT"

# Move current job to completed
mkdir -p "$QUEUE_BASE/completed"
mv "$JOB_DIR" "$QUEUE_BASE/completed/$JOB_ID" 2>/dev/null || true
COMPLETED_DIR="$QUEUE_BASE/completed/$JOB_ID"

# --- Self-iteration: analyst queued a follow-up, keep going silently ---
if [[ "$DECISION" =~ ^(accept|rerun|diagnose)$ ]] && [ "$HAS_NEXT" = "yes" ]; then
    NEXT_ID=$(python3 -c "import json; print(json.load(open('$COMPLETED_DIR/analysis.json'))['next_task']['task_id'])")
    mkdir -p "$QUEUE_BASE/running/$NEXT_ID"
    python3 -c "import json; t=json.load(open('$COMPLETED_DIR/analysis.json'))['next_task']; json.dump(t, open('$QUEUE_BASE/running/$NEXT_ID/task.json','w'), indent=2)"

    echo "[post-job] analyst self-iterating: launching $NEXT_ID (Claude NOT notified)"

    # Launch watcher for next task, then chain back to post-job
    python3 "$REPO_ROOT/automation/research_loop/job_watcher.py" \
        --task "$QUEUE_BASE/running/$NEXT_ID/task.json" \
        --timeout "$(python3 -c "import json; print(json.load(open('$QUEUE_BASE/running/$NEXT_ID/task.json')).get('timeout_minutes', 30))")"
    exec "$0" "$QUEUE_BASE/running/$NEXT_ID"

# --- Task frame complete or escalation: notify Claude ---
else
    notify_claude "Analyst completed job $JOB_ID (decision: $DECISION). Summary: $SUMMARY. Full analysis at results/research_loop/queue/completed/$JOB_ID/analysis.json"
fi
