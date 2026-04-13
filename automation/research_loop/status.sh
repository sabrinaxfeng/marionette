#!/usr/bin/env bash
# Quick status check for the research loop. Run anytime.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_FILE="$REPO_ROOT/automation/research_loop/config.json"

PYTHONPATH="$REPO_ROOT" python3 - <<'PY' "$CONFIG_FILE"
import json
import sys
from pathlib import Path

from lib.research_loop import (
    blocked_pending_tasks,
    graph_summaries,
    load_config,
    queue_task_counts,
    runnable_pending_tasks,
    task_group_summaries,
    running_tasks,
)

config_path = Path(sys.argv[1]).resolve()
repo_root = config_path.parents[2]
config = load_config(config_path, repo_root)

print("=== Research Loop Status ===\n")

for task in running_tasks(config):
    task_path = Path(task["_path"])
    job_dir = task_path.parent
    print(f"RUNNING: {task['task_id']} (graph={task['graph_id']}, priority={task.get('priority', 0)})")
    heartbeat_path = job_dir / "heartbeat.json"
    status_path = job_dir / "job_status.json"
    if heartbeat_path.exists():
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        print(f"  elapsed: {heartbeat['elapsed_minutes']}m / {heartbeat['timeout_minutes']}m")
        print(f"  pid: {heartbeat['pid']}, subagents: {heartbeat['subagent_count']}")
        print(f"  reasoning effort: {heartbeat.get('reasoning_effort', 'unknown')}")
        print(f"  last heartbeat: {heartbeat['last_heartbeat_utc']}")
    elif status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        print(f"  status: {status['status']}, duration: {status['duration_seconds']}s")
    else:
        print("  (just started, no heartbeat yet)")
    stderr_path = job_dir / "codex_stderr.txt"
    if stderr_path.exists():
        lines = [line.strip() for line in stderr_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
        if lines:
            print(f"  last activity: {lines[-1]}")
    if task.get("conflict_keys"):
        print(f"  conflict_keys: {', '.join(task['conflict_keys'])}")
    print("")

runnable = runnable_pending_tasks(config)
blocked = blocked_pending_tasks(config)

if runnable:
    print("RUNNABLE PENDING:")
    for task in runnable:
        print(f"  - {task['task_id']} (graph={task['graph_id']}, priority={task.get('priority', 0)})")
    print("")

if blocked:
    print("BLOCKED PENDING:")
    for task in blocked:
        print(f"  - {task['task_id']} (graph={task['graph_id']}): {task['_block_reason']}")
    print("")

counts = queue_task_counts(config)
print(
    "Pending: {pending} | Running: {running} | Completed: {completed} | Failed: {failed}".format(
        **counts
    )
)

graphs = graph_summaries(config, stale_after_minutes=int(config.get("stale_graph_minutes", 120) or 120))
if graphs:
    print("\nGRAPHS:")
    for graph in graphs:
        stale_suffix = " stale" if graph["stale"] else ""
        print(
            f"  - {graph['graph_id']} [{graph['status']}{stale_suffix}] "
            f"(groups={','.join(graph['task_group_ids']) or 'none'}, completed={len(graph['completed'])}, "
            f"running={len(graph['running'])}, runnable={len(graph['runnable_pending'])}, "
            f"blocked={len(graph['blocked_pending'])}, failed={len(graph['failed'])}, "
            f"age={graph['age_minutes'] if graph['age_minutes'] is not None else 'unknown'}m)"
        )

groups = task_group_summaries(config)
if groups:
    print("\nTASK GROUPS:")
    for group in groups:
        print(
            f"  - {group['task_group_id']} [{group['status']}] "
            f"(graphs={','.join(group['graph_ids']) or 'none'}, completed={len(group['completed'])}, "
            f"running={len(group['running'])}, runnable={len(group['runnable_pending'])}, "
            f"blocked={len(group['blocked_pending'])}, failed={len(group['failed'])})"
        )
PY
