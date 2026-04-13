#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import (
    blocked_pending_tasks,
    graph_summaries,
    load_config,
    load_json,
    load_tasks,
    queue_task_counts,
    runnable_pending_tasks,
    running_tasks,
    task_group_summaries,
    utc_now_iso,
)

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"


def _load_optional_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _load_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _loop_root(config: dict) -> Path:
    return Path(config["paths"]["cycles_dir"]).parent


def _task_job_dir(task: dict) -> Path | None:
    task_path = Path(task["_path"])
    if task["_queue"] == "pending":
        return None
    return task_path.parent


def _task_sort_timestamp(task: dict) -> float:
    task_path = Path(task["_path"])
    target = task_path.parent if task["_queue"] != "pending" else task_path
    try:
        return target.stat().st_mtime
    except OSError:
        return 0.0


def _job_record(task: dict) -> dict:
    job_dir = _task_job_dir(task)
    heartbeat = _load_optional_json(job_dir / "heartbeat.json") if job_dir else None
    job_status = _load_optional_json(job_dir / "job_status.json") if job_dir else None
    analysis = _load_optional_json(job_dir / "analysis.json") if job_dir else None
    review_status = _load_optional_json(job_dir / "review_status.json") if job_dir else None
    finished_utc = job_status.get("finished_at_utc") if isinstance(job_status, dict) else None
    started_utc = job_status.get("started_at_utc") if isinstance(job_status, dict) else None
    queue_status = task["_queue"]
    derived_status = (
        heartbeat.get("status")
        if isinstance(heartbeat, dict)
        else job_status.get("status")
        if isinstance(job_status, dict)
        else queue_status
    )
    return {
        "task_id": task["task_id"],
        "queue": queue_status,
        "status": derived_status,
        "graph_id": task["graph_id"],
        "task_group_id": task["task_group_id"],
        "task_group_title": task["task_group_title"],
        "objective": task["objective"],
        "hypothesis": task["hypothesis"],
        "priority": int(task.get("priority", 0) or 0),
        "timeout_minutes": int(task.get("timeout_minutes", 0) or 0),
        "reasoning_effort": (
            (heartbeat or {}).get("reasoning_effort")
            or task.get("analyst_reasoning_effort")
            or "unknown"
        ),
        "conflict_keys": list(task.get("conflict_keys") or []),
        "depends_on": list(task.get("depends_on") or []),
        "started_at_utc": started_utc,
        "finished_at_utc": finished_utc,
        "duration_seconds": (job_status or {}).get("duration_seconds"),
        "elapsed_minutes": (heartbeat or {}).get("elapsed_minutes"),
        "subagent_count": (heartbeat or {}).get("subagent_count"),
        "decision": (analysis or {}).get("decision"),
        "summary": (analysis or {}).get("summary"),
        "metric_name": (analysis or {}).get("metric_name"),
        "metric_value": (analysis or {}).get("metric_value"),
        "baseline_value": (analysis or {}).get("baseline_value"),
        "delta": (analysis or {}).get("delta"),
        "review_status": review_status,
        "has_stdout": bool(job_dir and (job_dir / "codex_stdout.txt").exists()),
        "has_stderr": bool(job_dir and (job_dir / "codex_stderr.txt").exists()),
        "has_analysis": bool(job_dir and (job_dir / "analysis.json").exists()),
        "has_heartbeat": bool(job_dir and (job_dir / "heartbeat.json").exists()),
        "job_dir": str(job_dir) if job_dir else None,
        "sort_ts": _task_sort_timestamp(task),
    }


def collect_jobs(config: dict, *, limit: int = 30) -> list[dict]:
    tasks = (
        load_tasks(config, "running")
        + load_tasks(config, "completed")
        + load_tasks(config, "failed")
    )
    records = [_job_record(task) for task in tasks]
    status_rank = {"running": 0, "timed_out": 1, "failed": 1, "completed": 2}
    records.sort(
        key=lambda item: (
            status_rank.get(str(item["status"]), 9),
            -float(item["sort_ts"] or 0.0),
            item["task_id"],
        )
    )
    return records[:limit]


def build_dashboard_payload(config: dict) -> dict:
    loop_root = _loop_root(config)
    state_path = Path(config["paths"]["state_json"])
    stale_report_path = loop_root / "stale_graphs.json"
    state = _load_optional_json(state_path) or {}
    session = _load_optional_json(loop_root / "claude_session.json") or {}
    stale_report = _load_optional_json(stale_report_path) or {}
    completion_message = _load_optional_text(loop_root / "loop_completion.txt")
    graphs = graph_summaries(config, stale_after_minutes=int(config.get("stale_graph_minutes", 120) or 120))
    task_groups = task_group_summaries(config)
    running = running_tasks(config)
    runnable = runnable_pending_tasks(config)
    blocked = blocked_pending_tasks(config)
    queue_counts = queue_task_counts(config)

    latest_cycle = None
    history = state.get("history") or []
    if history:
        latest_cycle = history[-1]

    return {
        "generated_at_utc": utc_now_iso(),
        "goal": config.get("goal"),
        "headline_metric": config.get("headline_metric"),
        "target_value": config.get("target_value"),
        "loop": {
            "status": state.get("status", "unknown"),
            "stop_reason": state.get("stop_reason"),
            "last_updated_utc": state.get("last_updated_utc"),
            "latest_cycle": latest_cycle,
        },
        "session": session,
        "queue_counts": queue_counts,
        "overview": {
            "running_jobs": len(running),
            "runnable_tasks": len(runnable),
            "blocked_tasks": len(blocked),
            "stale_graphs": len([graph for graph in graphs if graph["stale"]]),
            "ready_for_synthesis_groups": len([group for group in task_groups if group["status"] == "ready_for_synthesis"]),
        },
        "latest_completion_message": completion_message,
        "graphs": graphs,
        "task_groups": task_groups,
        "runnable_pending": [
            {
                "task_id": task["task_id"],
                "graph_id": task["graph_id"],
                "task_group_id": task["task_group_id"],
                "objective": task["objective"],
                "priority": int(task.get("priority", 0) or 0),
            }
            for task in runnable
        ],
        "blocked_pending": [
            {
                "task_id": task["task_id"],
                "graph_id": task["graph_id"],
                "task_group_id": task["task_group_id"],
                "objective": task["objective"],
                "reason": task["_block_reason"],
            }
            for task in blocked
        ],
        "jobs": collect_jobs(config),
        "stale_report": stale_report,
    }


def _artifact_path(config: dict, task_id: str, kind: str) -> Path | None:
    path_map = {
        "stdout": "codex_stdout.txt",
        "stderr": "codex_stderr.txt",
        "analysis": "analysis.json",
        "task": "task.json",
        "status": "job_status.json",
        "heartbeat": "heartbeat.json",
        "review": "review_status.json",
    }
    filename = path_map.get(kind)
    if filename is None:
        return None
    for queue_name in ("running", "completed", "failed"):
        for task in load_tasks(config, queue_name):
            if task["task_id"] != task_id:
                continue
            job_dir = _task_job_dir(task)
            if job_dir is None:
                return None
            artifact = job_dir / filename
            if artifact.exists():
                return artifact
            return None
    return None


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ResearchLoopDashboard/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/dashboard":
            self._serve_json(build_dashboard_payload(self.server.config))
            return
        if parsed.path == "/api/artifact":
            params = parse_qs(parsed.query)
            task_id = (params.get("task_id") or [None])[0]
            kind = (params.get("kind") or [None])[0]
            if not task_id or not kind:
                self.send_error(HTTPStatus.BAD_REQUEST, "Missing task_id or kind")
                return
            artifact = _artifact_path(self.server.config, task_id, kind)
            if artifact is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
                return
            content_type = "application/json" if artifact.suffix == ".json" else "text/plain; charset=utf-8"
            self._serve_bytes(artifact.read_bytes(), content_type)
            return
        self._serve_static(parsed.path)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _serve_json(self, payload: dict) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self._serve_bytes(data, "application/json; charset=utf-8")

    def _serve_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, raw_path: str) -> None:
        path = raw_path if raw_path not in {"", "/"} else "/index.html"
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Invalid path")
            return
        if not candidate.exists() or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        mime_type, _ = mimetypes.guess_type(candidate.name)
        self._serve_bytes(candidate.read_bytes(), mime_type or "application/octet-stream")


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_cls, config: dict):
        super().__init__(server_address, handler_cls)
        self.config = config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local research-loop dashboard.")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "automation" / "research_loop" / "config.json",
        help="Path to research-loop config.json",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config.resolve(), ROOT)
    server = DashboardHTTPServer((args.host, args.port), DashboardHandler, config)
    print(f"Research loop dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
