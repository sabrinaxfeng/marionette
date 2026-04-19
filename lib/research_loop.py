from __future__ import annotations

import json
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path, root: Path) -> dict[str, Any]:
    data = load_json(path)
    stop_defaults = {
        "max_consecutive_inconclusive_cycles": 2,
        "max_consecutive_stagnant_cycles": 2,
        "max_consecutive_regressions": 1,
    }
    path_defaults = {
        "current_cycle_markdown": "CURRENT_CYCLE.md",
        "state_json": "results/research_loop/state.json",
        "cycles_dir": "results/research_loop/cycles",
    }
    reviewer_defaults = {
        "provider": "claude",
    }
    claude_defaults = {"model": "opus", "effort": "medium"}
    codex_defaults = {
        "model": "gpt-5.4",
        "reasoning_effort": "medium",
        "sandbox": "workspace-write",
        "dangerously_bypass_approvals_and_sandbox": False,
    }
    data.setdefault("max_cycles", 3)
    data.setdefault("max_parallel_jobs", 1)
    data.setdefault("stale_graph_minutes", 120)
    data.setdefault("headline_metric", "primary_score")
    data.setdefault("target_value", None)
    data["stop_conditions"] = {**stop_defaults, **data.get("stop_conditions", {})}
    data["paths"] = {**path_defaults, **data.get("paths", {})}
    data["reviewer"] = {**reviewer_defaults, **data.get("reviewer", {})}
    data["claude"] = {**claude_defaults, **data.get("claude", {})}
    data["codex"] = {**codex_defaults, **data.get("codex", {})}
    resolved_paths = {}
    for key, raw in data["paths"].items():
        resolved_paths[key] = str(_as_path(root, raw))
    data["paths"] = resolved_paths
    data["_config_path"] = str(path.resolve())
    data["_repo_root"] = str(root.resolve())
    return data


def initial_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal": config["goal"],
        "status": "idle",
        "history": [],
        "last_updated_utc": utc_now_iso(),
        "stop_reason": None,
    }


def load_state(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return initial_state(config)
    state = load_json(path)
    state.setdefault("goal", config["goal"])
    state.setdefault("status", "idle")
    state.setdefault("history", [])
    state.setdefault("last_updated_utc", utc_now_iso())
    state.setdefault("stop_reason", None)
    return state


def latest_cycle(state: dict[str, Any]) -> dict[str, Any] | None:
    history = state.get("history") or []
    if not history:
        return None
    return history[-1]


def ensure_cycle(state: dict[str, Any]) -> dict[str, Any]:
    cycle = latest_cycle(state)
    if cycle and cycle.get("status") != "completed":
        return cycle
    next_number = len(state.get("history", [])) + 1
    cycle = {
        "cycle": next_number,
        "status": "planning",
        "started_at_utc": utc_now_iso(),
        "claude_plan": None,
        "codex_result": None,
        "artifact_paths": {},
    }
    state["history"].append(cycle)
    state["status"] = "planning"
    state["last_updated_utc"] = utc_now_iso()
    return cycle


def completed_cycles(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [cycle for cycle in state.get("history", []) if cycle.get("codex_result")]


def latest_result(state: dict[str, Any]) -> dict[str, Any] | None:
    cycles = completed_cycles(state)
    if not cycles:
        return None
    return cycles[-1]["codex_result"]


def _consecutive_count(cycles: list[dict[str, Any]], predicate) -> int:
    count = 0
    for cycle in reversed(cycles):
        result = cycle.get("codex_result") or {}
        if predicate(result):
            count += 1
            continue
        break
    return count


def should_stop_from_results(config: dict[str, Any], state: dict[str, Any]) -> str | None:
    cycles = completed_cycles(state)
    if not cycles:
        return None
    latest = cycles[-1]["codex_result"]
    if latest.get("recommendation") == "stop":
        return "codex_recommended_stop"
    target_value = config.get("target_value")
    latest_value = latest.get("headline_value")
    if target_value is not None and isinstance(latest_value, (int, float)) and latest_value >= target_value:
        return "target_value_reached"
    stop_cfg = config["stop_conditions"]
    if _consecutive_count(cycles, lambda r: r.get("outcome") == "inconclusive") >= stop_cfg["max_consecutive_inconclusive_cycles"]:
        return "consecutive_inconclusive_limit"
    if _consecutive_count(cycles, lambda r: (r.get("delta") is None) or float(r.get("delta") or 0.0) <= 0.0) >= stop_cfg["max_consecutive_stagnant_cycles"]:
        return "consecutive_stagnation_limit"
    if _consecutive_count(cycles, lambda r: r.get("outcome") == "rejected" or float(r.get("delta") or 0.0) < 0.0) >= stop_cfg["max_consecutive_regressions"]:
        return "consecutive_regression_limit"
    if len(cycles) >= int(config.get("max_cycles", 0) or 0):
        return "max_cycles_reached"
    return None


def mark_stopped(state: dict[str, Any], reason: str) -> None:
    state["status"] = "stopped"
    state["stop_reason"] = reason
    state["last_updated_utc"] = utc_now_iso()


def _bullet_block(items: list[str] | None) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item}" for item in items]


def render_current_cycle_markdown(config: dict[str, Any], state: dict[str, Any]) -> str:
    cycle = latest_cycle(state)
    lines = [
        "# Current Cycle",
        "",
        "> Managed by `PYTHONPATH=. .venv/bin/python -m scripts.research_cycle_supervisor`.",
        "",
        f"- Goal: {config['goal']}",
        f"- Status: {state.get('status', 'idle')}",
        f"- Last updated (UTC): {state.get('last_updated_utc', '')}",
        f"- Stop reason: {state.get('stop_reason') or 'active'}",
    ]
    if cycle:
        lines.extend(
            [
                f"- Active cycle: {cycle['cycle']}",
                "",
                "## Latest Claude Plan",
            ]
        )
        plan = cycle.get("claude_plan") or {}
        lines.extend(
            [
                f"- Decision: {plan.get('decision', 'pending')}",
                f"- Objective: {plan.get('objective', '')}",
                f"- Failure bucket: {plan.get('failure_bucket', '')}",
                f"- Hypothesis: {plan.get('hypothesis', '')}",
                f"- Academic rationale: {plan.get('academic_rationale', '')}",
                f"- Smallest valid experiment: {plan.get('smallest_valid_experiment', '')}",
                f"- Acceptance gate: {plan.get('acceptance_gate', '')}",
                "- Evaluation plan:",
            ]
        )
        lines.extend(_bullet_block(plan.get("evaluation_plan")))
        lines.extend(["- Non-goals:"])
        lines.extend(_bullet_block(plan.get("non_goals")))
        lines.extend(["- Risks:"])
        lines.extend(_bullet_block(plan.get("risks")))
        result = cycle.get("codex_result") or {}
        lines.extend(["", "## Latest Codex Result"])
        lines.extend(
            [
                f"- Outcome: {result.get('outcome', 'pending')}",
                f"- Summary: {result.get('summary', '')}",
                f"- Headline metric: {result.get('headline_metric')}",
                f"- Headline value: {result.get('headline_value')}",
                f"- Baseline value: {result.get('baseline_value')}",
                f"- Delta: {result.get('delta')}",
                f"- Recommendation: {result.get('recommendation', '')}",
                f"- Next focus: {result.get('next_focus', '')}",
                "- Files changed:",
            ]
        )
        lines.extend(_bullet_block(result.get("files_changed")))
        lines.extend(["- Artifacts:"])
        lines.extend(_bullet_block(result.get("artifacts")))
    history = state.get("history") or []
    if history:
        lines.extend(["", "## History"])
        for entry in history[-5:]:
            result = entry.get("codex_result") or {}
            lines.append(
                f"- Cycle {entry['cycle']}: {result.get('outcome', entry.get('status', 'planning'))}; "
                f"delta={result.get('delta')}; recommendation={result.get('recommendation', 'pending')}"
            )
    lines.append("")
    return "\n".join(lines)


# --- Task queue helpers ---


def queue_dirs(config: dict[str, Any]) -> dict[str, Path]:
    """Return queue subdirectory paths, creating them if needed."""
    base = Path(config["paths"]["cycles_dir"]).parent / "queue"
    dirs = {}
    for name in ("pending", "running", "completed", "failed"):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        dirs[name] = d
    return dirs


def _load_task_schema() -> dict[str, Any] | None:
    schema_path = Path(__file__).resolve().parent.parent / "automation" / "research_loop" / "schemas" / "codex_task.schema.json"
    if schema_path.exists():
        return json.loads(schema_path.read_text(encoding="utf-8"))
    return None


def normalize_task(task: dict[str, Any], *, parent_task: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = dict(task)
    parent_graph_id = parent_task.get("graph_id") if parent_task else None
    parent_task_id = parent_task.get("task_id") if parent_task else None
    parent_group_id = parent_task.get("task_group_id") if parent_task else None
    parent_group_title = parent_task.get("task_group_title") if parent_task else None

    normalized["graph_id"] = normalized.get("graph_id") or parent_graph_id or normalized["task_id"]
    depends_on = list(normalized.get("depends_on") or [])
    if parent_task_id and parent_task_id not in depends_on:
        depends_on.insert(0, parent_task_id)
    normalized["depends_on"] = list(dict.fromkeys(depends_on))
    normalized["conflict_keys"] = list(dict.fromkeys(normalized.get("conflict_keys") or []))
    normalized["priority"] = int(normalized.get("priority", 0) or 0)
    normalized["task_group_id"] = normalized.get("task_group_id") or parent_group_id or normalized["graph_id"]
    normalized["task_group_title"] = normalized.get("task_group_title") or parent_group_title or normalized["objective"]
    return normalized


def task_graph_id(task: dict[str, Any]) -> str:
    return normalize_task(task)["graph_id"]


def task_dependency_ids(task: dict[str, Any]) -> list[str]:
    return normalize_task(task)["depends_on"]


def task_conflict_keys(task: dict[str, Any]) -> list[str]:
    return normalize_task(task)["conflict_keys"]


def task_group_id(task: dict[str, Any]) -> str:
    return normalize_task(task)["task_group_id"]


def task_group_title(task: dict[str, Any]) -> str:
    return normalize_task(task)["task_group_title"]


def _iter_queue_task_paths(config: dict[str, Any], queue_name: str) -> list[Path]:
    dirs = queue_dirs(config)
    queue_dir = dirs[queue_name]
    if queue_name == "pending":
        return sorted(queue_dir.glob("*.json"))
    return sorted(path for path in queue_dir.glob("*/task.json") if path.is_file())


def load_tasks(config: dict[str, Any], queue_name: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in _iter_queue_task_paths(config, queue_name):
        task = normalize_task(load_json(path))
        task["_queue"] = queue_name
        task["_path"] = str(path)
        tasks.append(task)
    return tasks


def locate_task(config: dict[str, Any], task_id: str) -> tuple[str, Path] | None:
    for queue_name in ("pending", "running", "completed", "failed"):
        for task in load_tasks(config, queue_name):
            if task["task_id"] == task_id:
                return queue_name, Path(task["_path"])
    return None


def queue_task_counts(config: dict[str, Any]) -> dict[str, int]:
    return {queue_name: len(load_tasks(config, queue_name)) for queue_name in ("pending", "running", "completed", "failed")}


def completed_task_ids(config: dict[str, Any]) -> set[str]:
    return {task["task_id"] for task in load_tasks(config, "completed")}


def failed_task_ids(config: dict[str, Any]) -> set[str]:
    return {task["task_id"] for task in load_tasks(config, "failed")}


def running_tasks(config: dict[str, Any]) -> list[dict[str, Any]]:
    return load_tasks(config, "running")


def active_conflict_keys(config: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for task in running_tasks(config):
        keys.update(task_conflict_keys(task))
    return keys


def task_block_reason(
    task: dict[str, Any],
    *,
    completed_ids: set[str],
    failed_ids: set[str],
    running_conflicts: set[str],
) -> str | None:
    failed_dependencies = [dep for dep in task_dependency_ids(task) if dep in failed_ids]
    if failed_dependencies:
        return f"failed_dependencies:{','.join(sorted(failed_dependencies))}"
    missing_dependencies = [dep for dep in task_dependency_ids(task) if dep not in completed_ids]
    if missing_dependencies:
        return f"waiting_on:{','.join(sorted(missing_dependencies))}"
    conflicting_keys = sorted(set(task_conflict_keys(task)) & running_conflicts)
    if conflicting_keys:
        return f"conflicts_with_running:{','.join(conflicting_keys)}"
    return None


def runnable_pending_tasks(config: dict[str, Any], *, graph_id: str | None = None) -> list[dict[str, Any]]:
    completed_ids = completed_task_ids(config)
    failed_ids = failed_task_ids(config)
    running_conflicts = active_conflict_keys(config)
    tasks = load_tasks(config, "pending")
    runnable: list[dict[str, Any]] = []
    for task in tasks:
        if graph_id and task_graph_id(task) != graph_id:
            continue
        if task_block_reason(task, completed_ids=completed_ids, failed_ids=failed_ids, running_conflicts=running_conflicts) is None:
            runnable.append(task)
    return sorted(runnable, key=lambda task: (-int(task.get("priority", 0)), task["task_id"]))


def blocked_pending_tasks(config: dict[str, Any], *, graph_id: str | None = None) -> list[dict[str, Any]]:
    completed_ids = completed_task_ids(config)
    failed_ids = failed_task_ids(config)
    running_conflicts = active_conflict_keys(config)
    blocked: list[dict[str, Any]] = []
    for task in load_tasks(config, "pending"):
        if graph_id and task_graph_id(task) != graph_id:
            continue
        reason = task_block_reason(task, completed_ids=completed_ids, failed_ids=failed_ids, running_conflicts=running_conflicts)
        if reason is None:
            continue
        blocked.append({**task, "_block_reason": reason})
    return sorted(blocked, key=lambda task: (-int(task.get("priority", 0)), task["task_id"]))


def graph_snapshot(config: dict[str, Any], graph_id: str) -> dict[str, Any]:
    running = [task["task_id"] for task in running_tasks(config) if task_graph_id(task) == graph_id]
    completed = [task["task_id"] for task in load_tasks(config, "completed") if task_graph_id(task) == graph_id]
    failed = [task["task_id"] for task in load_tasks(config, "failed") if task_graph_id(task) == graph_id]
    runnable = [task["task_id"] for task in runnable_pending_tasks(config, graph_id=graph_id)]
    blocked = [
        {"task_id": task["task_id"], "reason": task["_block_reason"]}
        for task in blocked_pending_tasks(config, graph_id=graph_id)
    ]
    return {
        "graph_id": graph_id,
        "running": running,
        "completed": completed,
        "failed": failed,
        "runnable_pending": runnable,
        "blocked_pending": blocked,
    }


def _task_artifact_paths(task: dict[str, Any]) -> list[Path]:
    task_path = Path(task["_path"])
    if task["_queue"] == "pending":
        return [task_path]
    job_dir = task_path.parent
    return [path for path in job_dir.rglob("*") if path.is_file()]


def _path_timestamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def graph_last_updated_epoch(config: dict[str, Any], graph_id: str) -> float | None:
    timestamps: list[float] = []
    for queue_name in ("pending", "running", "completed", "failed"):
        for task in load_tasks(config, queue_name):
            if task_graph_id(task) != graph_id:
                continue
            timestamps.extend(_path_timestamp(path) for path in _task_artifact_paths(task))
    return max(timestamps) if timestamps else None


def graph_summary(config: dict[str, Any], graph_id: str, *, stale_after_minutes: int | None = None) -> dict[str, Any]:
    snapshot = graph_snapshot(config, graph_id)
    tasks = [
        task
        for queue_name in ("pending", "running", "completed", "failed")
        for task in load_tasks(config, queue_name)
        if task_graph_id(task) == graph_id
    ]
    task_groups = sorted({task_group_id(task) for task in tasks})
    last_updated_epoch = graph_last_updated_epoch(config, graph_id)
    age_minutes = None
    if last_updated_epoch is not None:
        age_minutes = max(0.0, (datetime.now(timezone.utc).timestamp() - last_updated_epoch) / 60.0)

    if snapshot["running"] or snapshot["runnable_pending"]:
        status = "active"
    elif snapshot["blocked_pending"]:
        status = "blocked"
    elif snapshot["failed"]:
        status = "failed"
    elif snapshot["completed"]:
        status = "ready_for_review"
    else:
        status = "empty"

    stale = bool(
        stale_after_minutes is not None
        and age_minutes is not None
        and age_minutes >= stale_after_minutes
        and not snapshot["running"]
        and not snapshot["runnable_pending"]
        and status in {"blocked", "failed", "ready_for_review"}
    )

    return {
        **snapshot,
        "status": status,
        "task_group_ids": task_groups,
        "last_updated_epoch": last_updated_epoch,
        "last_updated_utc": (
            datetime.fromtimestamp(last_updated_epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if last_updated_epoch is not None else None
        ),
        "age_minutes": None if age_minutes is None else round(age_minutes, 1),
        "stale": stale,
    }


def graph_summaries(config: dict[str, Any], *, stale_after_minutes: int | None = None) -> list[dict[str, Any]]:
    graph_ids = {
        task_graph_id(task)
        for queue_name in ("pending", "running", "completed", "failed")
        for task in load_tasks(config, queue_name)
    }
    status_rank = {"active": 0, "blocked": 1, "failed": 2, "ready_for_review": 3, "empty": 4}
    summaries = [graph_summary(config, graph_id, stale_after_minutes=stale_after_minutes) for graph_id in sorted(graph_ids)]
    return sorted(summaries, key=lambda item: (status_rank.get(item["status"], 99), item["graph_id"]))


def stale_graph_summaries(config: dict[str, Any], *, stale_after_minutes: int | None = None) -> list[dict[str, Any]]:
    threshold = stale_after_minutes if stale_after_minutes is not None else int(config.get("stale_graph_minutes", 120) or 120)
    return [summary for summary in graph_summaries(config, stale_after_minutes=threshold) if summary["stale"]]


def task_group_snapshot(config: dict[str, Any], task_group_id_value: str) -> dict[str, Any]:
    running = [task for task in running_tasks(config) if task_group_id(task) == task_group_id_value]
    completed = [task for task in load_tasks(config, "completed") if task_group_id(task) == task_group_id_value]
    failed = [task for task in load_tasks(config, "failed") if task_group_id(task) == task_group_id_value]
    runnable = [task for task in runnable_pending_tasks(config) if task_group_id(task) == task_group_id_value]
    blocked = [task for task in blocked_pending_tasks(config) if task_group_id(task) == task_group_id_value]
    all_tasks = running + completed + failed + runnable + blocked
    graphs = sorted({task_graph_id(task) for task in all_tasks})
    title = task_group_title(all_tasks[0]) if all_tasks else task_group_id_value

    if running or runnable:
        status = "active"
    elif failed or blocked:
        status = "blocked"
    elif completed:
        status = "ready_for_synthesis"
    else:
        status = "empty"

    return {
        "task_group_id": task_group_id_value,
        "task_group_title": title,
        "status": status,
        "graph_ids": graphs,
        "running": [task["task_id"] for task in running],
        "completed": [task["task_id"] for task in completed],
        "failed": [task["task_id"] for task in failed],
        "runnable_pending": [task["task_id"] for task in runnable],
        "blocked_pending": [
            {"task_id": task["task_id"], "reason": task["_block_reason"]}
            for task in blocked
        ],
    }


def task_group_summaries(config: dict[str, Any]) -> list[dict[str, Any]]:
    task_groups = {
        task_group_id(task)
        for queue_name in ("pending", "running", "completed", "failed")
        for task in load_tasks(config, queue_name)
    }
    summaries = [task_group_snapshot(config, group_id) for group_id in sorted(task_groups)]
    status_rank = {"active": 0, "blocked": 1, "ready_for_synthesis": 2, "empty": 3}
    return sorted(summaries, key=lambda item: (status_rank.get(item["status"], 99), item["task_group_id"]))


def write_task(config: dict[str, Any], task: dict[str, Any]) -> Path:
    """Validate and write a task to queue/pending/. Returns the file path."""
    task = normalize_task(task)
    schema = _load_task_schema()
    if schema:
        required = schema.get("required", [])
        missing = [k for k in required if k not in task]
        if missing:
            raise ValueError(f"Task missing required fields: {missing}")
    existing = locate_task(config, task["task_id"])
    if existing is not None:
        raise ValueError(f"Task ID already exists in queue: {task['task_id']}")
    dirs = queue_dirs(config)
    task_id = task["task_id"]
    path = dirs["pending"] / f"{task_id}.json"
    save_json(path, task)
    return path


def claim_task(config: dict[str, Any], task_id: str) -> Path:
    """Move task from pending/ to running/{task_id}/, return the job directory."""
    dirs = queue_dirs(config)
    src = dirs["pending"] / f"{task_id}.json"
    if not src.exists():
        raise FileNotFoundError(f"Pending task not found: {task_id}")
    job_dir = dirs["running"] / task_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dst = job_dir / "task.json"
    shutil.move(str(src), str(dst))
    return job_dir


def complete_job(config: dict[str, Any], job_dir: Path, target: str = "completed") -> Path:
    """Move job directory from running/ to completed/ or failed/. Returns new path."""
    if target not in ("completed", "failed"):
        raise ValueError(f"target must be 'completed' or 'failed', got {target!r}")
    dirs = queue_dirs(config)
    dest = dirs[target] / job_dir.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(job_dir), str(dest))
    return dest


# --- Session bridge ---


def loop_root(config: dict[str, Any]) -> Path:
    return Path(config["paths"]["cycles_dir"]).parent


def completion_marker_path(config: dict[str, Any]) -> Path:
    return loop_root(config) / "loop_completion.txt"


def planner_handoff_path(config: dict[str, Any]) -> Path:
    return loop_root(config) / "planner_handoff.json"


def _legacy_session_path(config: dict[str, Any]) -> Path:
    return loop_root(config) / "claude_session.json"


def _session_path(config: dict[str, Any]) -> Path:
    return loop_root(config) / "reviewer_session.json"


def _normalize_session(config: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(session)
    normalized.setdefault("provider", str(config.get("reviewer", {}).get("provider") or "claude"))
    normalized.setdefault("mode", "interactive")
    normalized.setdefault("status", "waiting")
    return normalized


def load_session(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read reviewer_session.json if it exists, else fall back to legacy claude_session.json."""
    path = _session_path(config)
    legacy_path = _legacy_session_path(config)
    if path.exists():
        return _normalize_session(config, load_json(path))
    if legacy_path.exists():
        session = load_json(legacy_path)
        session.setdefault("provider", "claude")
        return _normalize_session(config, session)
    return None


def save_session(config: dict[str, Any], session: dict[str, Any]) -> None:
    """Write reviewer_session.json with normalized provider metadata."""
    save_json(_session_path(config), _normalize_session(config, session))


def queue_task_counts(config: dict[str, Any]) -> dict[str, int]:
    return {
        queue_name: len(load_tasks(config, queue_name))
        for queue_name in ("pending", "running", "completed", "failed")
    }


def _latest_job_in_queue(config: dict[str, Any], queue_name: str) -> dict[str, Any] | None:
    tasks = load_tasks(config, queue_name)
    if not tasks:
        return None
    tasks.sort(key=lambda task: Path(task["_path"]).stat().st_mtime, reverse=True)
    task = tasks[0]
    task_path = Path(task["_path"])
    job_dir = task_path.parent if task["_queue"] != "pending" else None
    payload: dict[str, Any] = {
        "task_id": task["task_id"],
        "graph_id": task["graph_id"],
        "task_group_id": task["task_group_id"],
        "objective": task["objective"],
        "queue": queue_name,
        "task_path": str(task_path),
    }
    if job_dir is not None:
        payload["job_dir"] = str(job_dir)
        for name in ("analysis.json", "job_status.json", "review_status.json"):
            artifact = job_dir / name
            if artifact.exists():
                payload[name[:-5]] = load_json(artifact)
    return payload


def build_planner_handoff(config: dict[str, Any], *, trigger: str, message: str) -> dict[str, Any]:
    state = load_state(Path(config["paths"]["state_json"]), config)
    graphs = graph_summaries(config, stale_after_minutes=int(config.get("stale_graph_minutes", 120) or 120))
    task_groups = task_group_summaries(config)
    current_cycle_path = Path(config["paths"]["current_cycle_markdown"])
    return {
        "generated_at_utc": utc_now_iso(),
        "trigger": trigger,
        "message": message,
        "goal": config.get("goal"),
        "session": load_session(config),
        "paths": {
            "state_json": config["paths"]["state_json"],
            "current_cycle_markdown": str(current_cycle_path),
            "planner_handoff_json": str(planner_handoff_path(config)),
            "completion_marker": str(completion_marker_path(config)),
        },
        "loop": {
            "status": state.get("status", "unknown"),
            "stop_reason": state.get("stop_reason"),
            "last_updated_utc": state.get("last_updated_utc"),
            "latest_cycle": latest_cycle(state),
        },
        "queue_counts": queue_task_counts(config),
        "latest_completed_job": _latest_job_in_queue(config, "completed"),
        "latest_failed_job": _latest_job_in_queue(config, "failed"),
        "stale_graphs": [graph for graph in graphs if graph.get("stale")],
        "blocked_graphs": [graph for graph in graphs if graph.get("status") == "blocked"],
        "ready_for_review_graphs": [graph for graph in graphs if graph.get("status") == "ready_for_review"],
        "ready_for_synthesis_groups": [group for group in task_groups if group.get("status") == "ready_for_synthesis"],
        "reviewer_takeover_notes": [
            "Read CURRENT_CYCLE.md and the tracker document before queuing new work.",
            "Review blocked graphs, stale graphs, and ready_for_synthesis task groups first.",
            "If taking over temporarily, preserve graph_id/task_group_id continuity rather than minting ad hoc replacements.",
            "If no session resume path works, write the next decision to the completion marker and queue explicit follow-up tasks.",
        ],
    }


def write_planner_handoff(config: dict[str, Any], *, trigger: str, message: str) -> Path:
    path = planner_handoff_path(config)
    save_json(path, build_planner_handoff(config, trigger=trigger, message=message))
    return path


def notify_reviewer(config: dict[str, Any], message: str, *, trigger: str, timeout_s: int = 120) -> dict[str, Any]:
    marker_path = completion_marker_path(config)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(message + "\n", encoding="utf-8")
    handoff_path = write_planner_handoff(config, trigger=trigger, message=message)

    session = load_session(config)
    if not session:
        return {
            "status": "marker_only",
            "provider": None,
            "session_id": None,
            "completion_marker": str(marker_path),
            "planner_handoff": str(handoff_path),
        }

    mode = str(session.get("mode") or "")
    provider = str(session.get("provider") or config.get("reviewer", {}).get("provider") or "claude")
    session_id = str(session.get("session_id") or "")
    if mode == "interactive":
        return {
            "status": "interactive_marker",
            "provider": provider,
            "session_id": session_id or None,
            "completion_marker": str(marker_path),
            "planner_handoff": str(handoff_path),
        }

    if not session_id:
        return {
            "status": "marker_only",
            "provider": provider,
            "session_id": None,
            "completion_marker": str(marker_path),
            "planner_handoff": str(handoff_path),
        }

    if provider == "claude":
        command = ["claude", "-p", "--resume", session_id, message]
    elif provider == "codex":
        command = ["codex", "exec", "resume", session_id, message]
    else:
        return {
            "status": "unsupported_provider",
            "provider": provider,
            "session_id": session_id,
            "completion_marker": str(marker_path),
            "planner_handoff": str(handoff_path),
        }

    try:
        cp = subprocess.run(command, check=False, timeout=timeout_s, capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "resume_timeout",
            "provider": provider,
            "session_id": session_id,
            "command": command,
            "timeout_s": timeout_s,
            "stdout": (exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "").strip() if isinstance(exc.stderr, str) else "",
            "completion_marker": str(marker_path),
            "planner_handoff": str(handoff_path),
        }
    except OSError as exc:
        return {
            "status": "resume_error",
            "provider": provider,
            "session_id": session_id,
            "command": command,
            "error": str(exc),
            "completion_marker": str(marker_path),
            "planner_handoff": str(handoff_path),
        }
    return {
        "status": "resumed" if cp.returncode == 0 else "resume_failed",
        "provider": provider,
        "session_id": session_id,
        "command": command,
        "returncode": cp.returncode,
        "stdout": cp.stdout.strip(),
        "stderr": cp.stderr.strip(),
        "completion_marker": str(marker_path),
        "planner_handoff": str(handoff_path),
    }


def analysis_follow_up_tasks(analysis: dict[str, Any], *, parent_task: dict[str, Any]) -> list[dict[str, Any]]:
    follow_ups: list[dict[str, Any]] = []
    if analysis.get("next_task"):
        follow_ups.append(analysis["next_task"])
    follow_ups.extend(analysis.get("next_tasks") or [])
    return [normalize_task(task, parent_task=parent_task) for task in follow_ups]


def build_codex_prompt(config: dict[str, Any], state: dict[str, Any], plan: dict[str, Any]) -> str:
    return f"""You are Codex, the implementation and evaluation agent for an autonomous research loop.

Global goal:
{config["goal"]}

Claude has approved this bounded cycle plan:
{json.dumps(plan, indent=2)}

Working rules:
- execute the smallest meaningful experiment that tests the hypothesis
- preserve unrelated working-tree changes
- do not modify `CURRENT_CYCLE.md`; the supervisor owns that file
- prefer measurable evidence over speculative changes
- if the hypothesis does not justify a code patch, run a bounded diagnostic instead
- do not ask the user questions; decide, execute, and report

Return only JSON matching the provided schema after you finish the experiment.
"""
