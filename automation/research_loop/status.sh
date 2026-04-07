#!/usr/bin/env bash
# Quick status check for the research loop. Run anytime.
QUEUE_BASE="$(cd "$(dirname "$0")/../.." && pwd)/results/research_loop/queue"

echo "=== Research Loop Status ==="
echo ""

# Running jobs
for dir in "$QUEUE_BASE/running"/*/; do
    [ -d "$dir" ] || continue
    task_id=$(basename "$dir")
    echo "RUNNING: $task_id"
    if [ -f "$dir/heartbeat.json" ]; then
        python3 -c "
import json
h = json.load(open('$dir/heartbeat.json'))
print(f'  elapsed: {h[\"elapsed_minutes\"]}m / {h[\"timeout_minutes\"]}m')
print(f'  pid: {h[\"pid\"]}, subagents: {h[\"subagent_count\"]}')
print(f'  last heartbeat: {h[\"last_heartbeat_utc\"]}')
" 2>/dev/null
    elif [ -f "$dir/job_status.json" ]; then
        python3 -c "import json; s=json.load(open('$dir/job_status.json')); print(f'  status: {s[\"status\"]}, duration: {s[\"duration_seconds\"]}s')" 2>/dev/null
    else
        echo "  (just started, no heartbeat yet)"
    fi
    # Show stderr tail for activity
    if [ -f "$dir/codex_stderr.txt" ]; then
        last_line=$(tail -1 "$dir/codex_stderr.txt" 2>/dev/null)
        [ -n "$last_line" ] && echo "  last activity: $last_line"
    fi
    echo ""
done

# Count queues
pending=$(ls "$QUEUE_BASE/pending/" 2>/dev/null | grep -c '\.json$' || echo 0)
completed=$(ls -d "$QUEUE_BASE/completed"/*/ 2>/dev/null | wc -l || echo 0)
failed=$(ls -d "$QUEUE_BASE/failed"/*/ 2>/dev/null | wc -l || echo 0)

echo "Pending: $pending | Completed: $completed | Failed: $failed"
