from __future__ import annotations

import json
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
    claude_defaults = {"model": "opus", "effort": "medium"}
    codex_defaults = {
        "model": "gpt-5.4",
        "sandbox": "workspace-write",
        "dangerously_bypass_approvals_and_sandbox": False,
    }
    data.setdefault("max_cycles", 3)
    data.setdefault("headline_metric", "primary_score")
    data.setdefault("target_value", None)
    data["stop_conditions"] = {**stop_defaults, **data.get("stop_conditions", {})}
    data["paths"] = {**path_defaults, **data.get("paths", {})}
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


def write_task(config: dict[str, Any], task: dict[str, Any]) -> Path:
    """Validate and write a task to queue/pending/. Returns the file path."""
    schema = _load_task_schema()
    if schema:
        required = schema.get("required", [])
        missing = [k for k in required if k not in task]
        if missing:
            raise ValueError(f"Task missing required fields: {missing}")
    dirs = queue_dirs(config)
    task_id = task["task_id"]
    path = dirs["pending"] / f"{task_id}.json"
    save_json(path, task)
    return path


def claim_task(config: dict[str, Any], task_id: str) -> Path:
    """Move task from pending/ to running/{task_id}/, return the job directory."""
    dirs = queue_dirs(config)
    src = dirs["pending"] / f"{task_id}.json"
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


def _session_path(config: dict[str, Any]) -> Path:
    return Path(config["paths"]["cycles_dir"]).parent / "claude_session.json"


def load_session(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read claude_session.json if it exists."""
    path = _session_path(config)
    if not path.exists():
        return None
    return load_json(path)


def save_session(config: dict[str, Any], session: dict[str, Any]) -> None:
    """Write claude_session.json."""
    save_json(_session_path(config), session)


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
