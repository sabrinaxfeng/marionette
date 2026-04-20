#!/usr/bin/env python3
"""Dumb, deterministic job watcher. Tracks PID, log freshness, timeout. No research judgment.

The watcher launches the analyst directly — the analyst is the smart
coordinator that reads the task, spawns codex-mini subagents for implementation/eval,
and writes structured results. The watcher only monitors the process lifecycle.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import load_config, notify_reviewer

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _build_analyst_prompt(task: dict, job_dir: Path, reviewer_name: str) -> str:
    instructions = "\n".join(f"- {i}" for i in task.get("instructions", []))
    files = "\n".join(task.get("files_to_read", []))
    queue_base = job_dir.parents[1]
    return f"""You are the research analyst for an autonomous research loop.
You have deep reasoning capability and can spawn subagents for implementation work.

## Your task

Task ID: {task['task_id']}
Objective: {task['objective']}
Hypothesis: {task['hypothesis']}

Instructions:
{instructions}

Validation command: {task.get('validation_command', 'none')}
Acceptance gate: {task['acceptance_gate']}

Files to read first:
{files}

## How to work

1. **Read** the task and relevant project files first
2. **Plan** how to break the task into concrete steps
3. **Spawn codex-mini subagents** for implementation, eval, and diagnostic work:

```bash
codex exec --skip-git-repo-check -m gpt-5.1-codex-mini \\
  --config 'model_reasoning_effort="medium"' \\
  --sandbox workspace-write --full-auto \\
  -C {ROOT} \\
  "<concrete implementation task>" 2>/dev/null
```

4. **Review** subagent output and validate results
5. **Decide**: accept | reject | rerun | diagnose | escalate
6. If continuing, emit `next_task` or `next_tasks` in your structured JSON output. The glue layer will queue and dispatch them.

## Decision framework

- **accept**: experiment succeeded, metrics held or improved → queue next task
- **reject**: regression or bad code → revert, propose fix
- **rerun**: methodology issue → fix it yourself or via subagent, then rerun
- **diagnose**: need more data → spawn diagnostic subagent, then decide
- **escalate**: architectural decision or ambiguity → explain why {reviewer_name} should review

## Review signaling

If you reach a natural checkpoint and want {reviewer_name} to review before you continue,
write REVIEW_REQUESTED.json in {job_dir} with:
  {{"reason": "checkpoint"|"blocked"|"progress", "message": "...", "artifacts": [...], "stage": "..."}}
- checkpoint: you'll be paused until review completes
- blocked: you need a decision to proceed
- progress: informational, you keep running

## Dependency graph discipline

- All follow-up tasks in this task frame must share one `graph_id`
- Reuse one `task_group_id` / `task_group_title` for tasks that belong to the same research question so the reviewer can synthesize the whole group later
- Use `depends_on` to express ordering edges
- Use `conflict_keys` to serialize tasks that touch the same write surface
- Emit multiple tasks in `next_tasks` when independent branches can run in parallel
- Keep `next_task` for a single follow-up if only one branch is needed
- Choose `timeout_minutes` deliberately for each follow-up task instead of reusing one default everywhere
- Keep bounded diagnostics or small verification runs relatively short; use longer timeouts only for tasks that truly need longer implementation/eval loops or benchmark runtime
- If two tasks might edit the same files, docs, or benchmark artifacts, give them the same `conflict_keys`
- When uncertain, be conservative: prefer broader keys like `code`, `docs`, `eval:webqsp`, or `eval:benchmark` instead of over-fragmenting
- If you are not confident two branches are independent, serialize them rather than parallelizing them

## Output

Return structured JSON matching the codex_post_run schema with your decision,
metrics comparison, diagnosis, and `next_task` / `next_tasks` if applicable."""


def _load_default_reasoning_effort() -> str:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "medium"
    return str(config.get("codex", {}).get("reasoning_effort") or "medium")


def _signal_process_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def _kill_gracefully(pgid: int, grace: int = 10) -> None:
    if not _signal_process_group(pgid, signal.SIGTERM):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.5)
    _signal_process_group(pgid, signal.SIGKILL)


def _process_group_subagent_count(*, leader_pid: int, pgid: int) -> int:
    try:
        cp = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True, check=False)
    except OSError:
        return 0
    pids = {
        int(line.strip())
        for line in cp.stdout.splitlines()
        if line.strip().isdigit()
    }
    pids.discard(leader_pid)
    return len(pids)


REVIEW_SIGNAL = "REVIEW_REQUESTED.json"


def _check_review_signal(job_dir: Path, pgid: int, task_id: str, config: dict) -> str | None:
    """Check if the analyst dropped a review signal. Handle by type."""
    signal_path = job_dir / REVIEW_SIGNAL
    if not signal_path.exists():
        return None
    try:
        sig = json.loads(signal_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    reason = sig.get("reason", "checkpoint")
    message = sig.get("message", "")
    print(f"[watcher] {task_id}: review signal ({reason}): {message}", flush=True)

    if reason == "progress":
        # Informational only — rename so we don't re-trigger, keep running
        signal_path.rename(job_dir / f"review_progress_{_utc_now_iso().replace(':', '-')}.json")
        return None

    # checkpoint or blocked — pause analyst, notify reviewer, then decide
    try:
        _signal_process_group(pgid, signal.SIGSTOP)
    except OSError:
        pass

    review_status_path = job_dir / "review_status.json"
    review_status = {
        "job_id": task_id,
        "review_type": reason,
        "message": message,
        "artifacts": sig.get("artifacts", []),
        "stage": sig.get("stage", "unknown"),
        "paused_at_utc": _utc_now_iso(),
    }
    _write_json(review_status_path, review_status)

    try:
        review_payload = json.dumps(sig, indent=2)
        review_message = (
            f"Analyst requests review for job {task_id} ({reason}): {message}\n\n"
            f"Artifacts: {sig.get('artifacts', [])}\n"
            f"Stage: {sig.get('stage', 'unknown')}\n"
            f"Review response path: {job_dir / 'REVIEW_RESPONSE.json'}\n\n"
            f"The analyst is paused. Write REVIEW_RESPONSE.json with "
            '{"action": "continue"} or {"action": "abort"} to let the watcher decide next steps.\n\n'
            f"Signal payload:\n{review_payload}"
        )
        notify_result = notify_reviewer(
            config,
            review_message,
            trigger=f"review_{reason}",
            job_dir=job_dir,
            extra={
                "reason": reason,
                "stage": sig.get("stage", "unknown"),
                "artifacts": sig.get("artifacts", []),
            },
        )
        _write_json(job_dir / "review_notify.json", notify_result)
        print(
            f"[watcher] {task_id}: reviewer notify status={notify_result.get('status')} "
            f"provider={notify_result.get('provider')}",
            flush=True,
        )
    except Exception as exc:
        print(f"[watcher] {task_id}: reviewer notify failed: {exc}", flush=True)

    # Wait for review response
    response_path = job_dir / "REVIEW_RESPONSE.json"
    review_timeout = 600  # 10 minutes max
    review_start = time.monotonic()
    while time.monotonic() - review_start < review_timeout:
        if response_path.exists():
            try:
                resp = json.loads(response_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(5)
                continue
            action = resp.get("action", "continue")
            print(f"[watcher] {task_id}: review response: {action}", flush=True)
            signal_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)
            review_status["review_action"] = action
            review_status["review_response"] = resp
            review_status["reviewed_at_utc"] = _utc_now_iso()
            _write_json(review_status_path, review_status)
            if action == "abort":
                _kill_gracefully(pgid)
                return "abort"
            try:
                _signal_process_group(pgid, signal.SIGCONT)
            except OSError:
                pass
            return action
        time.sleep(5)

    # Review timed out — resume and continue
    print(f"[watcher] {task_id}: review timed out, resuming", flush=True)
    signal_path.unlink(missing_ok=True)
    review_status["review_action"] = "timeout_resume"
    review_status["reviewed_at_utc"] = _utc_now_iso()
    _write_json(review_status_path, review_status)
    try:
        _signal_process_group(pgid, signal.SIGCONT)
    except OSError:
        pass
    return "timeout_resume"


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch a Codex analyst job. Dumb PID/log monitor.")
    parser.add_argument("--task", type=Path, required=True, help="Path to codex_task.json")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout in minutes")
    parser.add_argument("--poll-interval", type=int, default=10, help="Poll interval in seconds")
    args = parser.parse_args()

    config = load_config(CONFIG_PATH, ROOT)
    task = json.loads(args.task.read_text(encoding="utf-8"))
    task_id = task["task_id"]
    reasoning_effort = str(task.get("analyst_reasoning_effort") or _load_default_reasoning_effort())
    reviewer_provider = str(config.get("reviewer", {}).get("provider") or "reviewer").strip().lower()
    reviewer_name = {"claude": "Claude", "codex": "Codex"}.get(reviewer_provider, "the reviewer")

    # Job directory is parent of the task file (queue/running/{task_id}/)
    job_dir = args.task.parent
    result_path = job_dir / "analysis.json"
    stdout_path = job_dir / "codex_stdout.txt"
    stderr_path = job_dir / "codex_stderr.txt"
    status_path = job_dir / "job_status.json"
    schema_path = SCHEMA_DIR / "codex_post_run.schema.json"

    prompt = _build_analyst_prompt(task, job_dir, reviewer_name)

    # Launch the analyst with task- or config-selected reasoning effort.
    command = [
        "codex", "exec", "--skip-git-repo-check",
        "-m", "gpt-5.4",
        "--config", f'model_reasoning_effort="{reasoning_effort}"',
        "--sandbox", "workspace-write",
        "--full-auto",
        "-C", str(ROOT),
        "-o", str(result_path),
        "--output-schema", str(schema_path),
        "-",  # read prompt from stdin
    ]

    started_at = _utc_now_iso()
    t0 = time.monotonic()

    with open(stdout_path, "w") as fout, open(stderr_path, "w") as ferr:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=fout,
            stderr=ferr,
            cwd=ROOT,
            start_new_session=True,
        )
        proc.stdin.write(prompt.encode())
        proc.stdin.close()

    pid = proc.pid
    pgid = proc.pid
    timeout_s = args.timeout * 60
    status = "completed"
    failure_reason = None
    heartbeat_path = job_dir / "heartbeat.json"

    while proc.poll() is None:
        time.sleep(args.poll_interval)
        elapsed = time.monotonic() - t0

        # Write heartbeat so external tools can see we're alive
        subagent_count = _process_group_subagent_count(leader_pid=pid, pgid=pgid)
        _write_json(heartbeat_path, {
            "job_id": task_id,
            "status": "running",
            "pid": pid,
            "pgid": pgid,
            "elapsed_minutes": round(elapsed / 60, 1),
            "timeout_minutes": args.timeout,
            "reasoning_effort": reasoning_effort,
            "subagent_count": subagent_count,
            "last_heartbeat_utc": _utc_now_iso(),
        })

        if elapsed > timeout_s:
            status = "timed_out"
            failure_reason = "watcher_timeout"
            _kill_gracefully(pgid)
            break

        review_action = _check_review_signal(job_dir, pgid, task_id, config)
        if review_action == "abort":
            status = "failed"
            failure_reason = "review_abort"
            break

    exit_code = proc.wait()
    if status == "completed" and exit_code != 0:
        status = "failed"
        failure_reason = failure_reason or "process_exit_nonzero"

    duration = time.monotonic() - t0
    finished_at = _utc_now_iso()

    job_status = {
        "job_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 1),
        "output_path": str(result_path) if result_path.exists() else None,
        "log_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
    }
    if failure_reason is not None:
        job_status["failure_reason"] = failure_reason
    _write_json(status_path, job_status)

    print(f"[watcher] {task_id}: {status} (exit={exit_code}, {duration:.0f}s)", flush=True)
    sys.exit(0 if status == "completed" else 1)


if __name__ == "__main__":
    main()
