#!/usr/bin/env python3
"""Launch runnable queued tasks up to the configured parallelism cap."""

from __future__ import annotations

import argparse
import fcntl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import (  # noqa: E402
    claim_task,
    held_graph_ids,
    load_config,
    queue_dirs,
    runnable_pending_tasks,
    running_tasks,
)


def _lock_path(config: dict) -> Path:
    return queue_dirs(config)["pending"].parent / "dispatch.lock"


def _launch_chain(*, repo_root: Path, job_dir: Path, timeout_minutes: int) -> None:
    runner_path = repo_root / "automation" / "research_loop" / "run_claimed_job.sh"
    log_path = job_dir / "dispatch.log"
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            ["bash", str(runner_path), "--job-dir", str(job_dir), "--timeout", str(timeout_minutes)],
            cwd=repo_root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch runnable DAG tasks in the research loop.")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "automation" / "research_loop" / "config.json",
        help="Path to the loop configuration JSON.",
    )
    parser.add_argument("--graph-id", type=str, default=None, help="Only dispatch tasks from one graph.")
    parser.add_argument(
        "--exclude-graph-id",
        type=str,
        default=None,
        help="Never dispatch tasks from this graph in the current round.",
    )
    parser.add_argument("--max-launch", type=int, default=None, help="Optional cap for this dispatch round.")
    parser.add_argument("--dry-run", action="store_true", help="Print runnable tasks without launching them.")
    args = parser.parse_args()

    config = load_config(args.config.resolve(), ROOT)
    max_parallel_jobs = max(1, int(config.get("max_parallel_jobs", 1) or 1))

    lock_path = _lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        # Recompute launch capacity under the dispatch lock so concurrent
        # dispatchers do not race on a stale running-task count.
        available_slots = max_parallel_jobs - len(running_tasks(config))
        if args.max_launch is not None:
            available_slots = min(available_slots, max(0, args.max_launch))

        runnable = runnable_pending_tasks(config, graph_id=args.graph_id)
        held_graphs = held_graph_ids(config)
        if held_graphs:
            runnable = [task for task in runnable if task["graph_id"] not in held_graphs]
        if args.exclude_graph_id:
            runnable = [task for task in runnable if task["graph_id"] != args.exclude_graph_id]
        if available_slots <= 0 or not runnable:
            print(f"[dispatch] launched=0 available_slots={available_slots} runnable={len(runnable)}", flush=True)
            return

        launched = 0
        for task in runnable[:available_slots]:
            try:
                job_dir = claim_task(config, task["task_id"])
            except FileNotFoundError:
                continue
            launched += 1
            print(f"[dispatch] claimed {task['task_id']} graph={task['graph_id']} priority={task.get('priority', 0)}", flush=True)
            if args.dry_run:
                continue
            _launch_chain(repo_root=ROOT, job_dir=job_dir, timeout_minutes=int(task["timeout_minutes"]))

        print(f"[dispatch] launched={launched} available_slots={available_slots} runnable={len(runnable)}", flush=True)


if __name__ == "__main__":
    main()
