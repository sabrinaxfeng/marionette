#!/usr/bin/env python3
"""Atomically submit a research-loop task and nudge the supervisor."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import (  # noqa: E402
    clear_graph_hold,
    load_config,
    load_json,
    locate_task,
    queue_dirs,
    reviewer_events_dir,
    reviewer_task_events_dir,
    supervisor_is_healthy,
    touch_queue_nudge,
    write_task,
)


def _lock_path(config: dict) -> Path:
    return queue_dirs(config)["pending"].parent / "dispatch.lock"


def _print_result(payload: dict) -> None:
    print(json.dumps(payload, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Atomically queue one research-loop task.")
    parser.add_argument(
        "--task-file",
        type=Path,
        default=None,
        help="Path to a task JSON to enqueue atomically.",
    )
    parser.add_argument(
        "--task-id",
        type=str,
        default=None,
        help="Existing pending task ID to re-nudge without rewriting the queue entry.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "automation" / "research_loop" / "config.json",
        help="Path to the loop configuration JSON.",
    )
    parser.add_argument(
        "--allow-without-supervisor",
        action="store_true",
        help="Allow queue submission even if the supervisor heartbeat is missing/stale.",
    )
    args = parser.parse_args()

    if bool(args.task_file) == bool(args.task_id):
        parser.error("pass exactly one of --task-file or --task-id")

    config = load_config(args.config.resolve(), ROOT)
    pending_dir = queue_dirs(config)["pending"]
    lock_path = _lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        supervisor_ok = supervisor_is_healthy(config)
        if not supervisor_ok and not args.allow_without_supervisor:
            _print_result(
                {
                    "accepted": False,
                    "reason": "supervisor_unavailable",
                    "task_id": None,
                    "pending_path": None,
                    "reviewer_events_dir": str(reviewer_events_dir(config)),
                    "hint": "start the supervisor with: bash automation/research_loop/supervisor_ctl.sh start",
                }
            )
            sys.exit(1)

        if args.task_file is not None:
            task = load_json(args.task_file.resolve())
            try:
                pending_path = write_task(config, task)
                clear_graph_hold(config, str(task["graph_id"]))
            except Exception as exc:  # noqa: BLE001
                _print_result(
                    {
                        "accepted": False,
                        "reason": "queue_write_failed",
                        "error": str(exc),
                        "task_id": task.get("task_id"),
                        "pending_path": None,
                        "reviewer_events_dir": str(reviewer_events_dir(config)),
                    }
                )
                sys.exit(1)
            task_id = str(task["task_id"])
            action = "queued"
        else:
            task_id = str(args.task_id)
            located = locate_task(config, task_id)
            if located is None:
                _print_result(
                    {
                        "accepted": False,
                        "reason": "task_not_found",
                        "task_id": task_id,
                        "pending_path": None,
                        "reviewer_events_dir": str(reviewer_events_dir(config)),
                    }
                )
                sys.exit(1)
            queue_name, pending_path = located
            if queue_name != "pending":
                _print_result(
                    {
                        "accepted": False,
                        "reason": "task_not_pending",
                        "task_id": task_id,
                        "queue": queue_name,
                        "pending_path": str(pending_path),
                        "reviewer_events_dir": str(reviewer_events_dir(config)),
                    }
                )
                sys.exit(1)
            try:
                pending_task = load_json(Path(pending_path))
                graph_id = str(pending_task.get("graph_id") or "")
                if graph_id:
                    clear_graph_hold(config, graph_id)
            except Exception:  # noqa: BLE001
                pass
            action = "nudged"

        nudge_path = touch_queue_nudge(config, reason=f"launch:{task_id}")
        task_events_dir = reviewer_task_events_dir(config, task_id)
        task_latest_event = task_events_dir / "latest_event.json"

    _print_result(
        {
            "accepted": True,
            "action": action,
            "task_id": task_id,
            "pending_path": str(pending_path if isinstance(pending_path, Path) else pending_dir / f"{task_id}.json"),
            "supervisor_status": "healthy" if supervisor_ok else "unavailable",
            "nudge_path": str(nudge_path),
            "reviewer_events_dir": str(reviewer_events_dir(config)),
            "reviewer_task_events_dir": str(task_events_dir),
            "reviewer_task_latest_event": str(task_latest_event),
            "watch_command": f"bash automation/research_loop/watch_reviewer_events.sh --task-id {task_id} --once",
        }
    )


if __name__ == "__main__":
    main()
