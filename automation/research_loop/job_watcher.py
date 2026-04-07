#!/usr/bin/env python3
"""Dumb, deterministic job watcher. Tracks PID, log freshness, timeout. No research judgment.

The watcher launches the analyst (gpt-5.4, xhigh) directly — the analyst is the smart
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
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _build_analyst_prompt(task: dict, job_dir: Path) -> str:
    instructions = "\n".join(f"- {i}" for i in task.get("instructions", []))
    files = "\n".join(task.get("files_to_read", []))
    queue_base = job_dir.parents[1]
    return f"""You are the research analyst for an autonomous Claude-Codex research loop.
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

1. **Read** the task and relevant project files
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
6. If continuing, write the next task to {queue_base}/pending/<task_id>.json

## Decision framework

- **accept**: experiment succeeded, metrics held or improved → queue next task
- **reject**: regression or bad code → revert, propose fix
- **rerun**: methodology issue → fix it yourself or via subagent, then rerun
- **diagnose**: need more data → spawn diagnostic subagent, then decide
- **escalate**: architectural decision or ambiguity → explain why Claude should review

## Review signaling

If you reach a natural checkpoint and want Claude to review before you continue,
write REVIEW_REQUESTED.json in {job_dir} with:
  {{"reason": "checkpoint"|"blocked"|"progress", "message": "...", "artifacts": [...], "stage": "..."}}
- checkpoint: you'll be paused until review completes
- blocked: you need a decision to proceed
- progress: informational, you keep running

## Output

Return structured JSON matching the codex_post_run schema with your decision,
metrics comparison, diagnosis, and next_task if applicable."""


def _kill_gracefully(pid: int, grace: int = 10) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


REVIEW_SIGNAL = "REVIEW_REQUESTED.json"


def _check_review_signal(job_dir: Path, pid: int, task_id: str) -> None:
    """Check if the analyst dropped a review signal. Handle by type."""
    signal_path = job_dir / REVIEW_SIGNAL
    if not signal_path.exists():
        return
    try:
        sig = json.loads(signal_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    reason = sig.get("reason", "checkpoint")
    message = sig.get("message", "")
    print(f"[watcher] {task_id}: review signal ({reason}): {message}", flush=True)

    if reason == "progress":
        # Informational only — rename so we don't re-trigger, keep running
        signal_path.rename(job_dir / f"review_progress_{_utc_now_iso().replace(':', '-')}.json")
        return

    # checkpoint or blocked — pause analyst, invoke Claude, then decide
    try:
        os.kill(pid, signal.SIGSTOP)
    except OSError:
        pass

    _write_json(job_dir / "review_status.json", {
        "job_id": task_id,
        "review_type": reason,
        "message": message,
        "artifacts": sig.get("artifacts", []),
        "stage": sig.get("stage", "unknown"),
        "paused_at_utc": _utc_now_iso(),
    })

    # Try to notify Claude via session resume
    session_path = job_dir.parents[2] / "claude_session.json"
    if session_path.exists():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
            sid = session.get("session_id", "")
            if sid:
                review_payload = json.dumps(sig, indent=2)
                subprocess.run(
                    ["claude", "-p", "--resume", sid,
                     f"Codex analyst requests review for job {task_id} ({reason}): {message}\n\n"
                     f"Artifacts: {sig.get('artifacts', [])}\n"
                     f"Stage: {sig.get('stage', 'unknown')}\n\n"
                     f"The analyst is paused. Reply with the result in "
                     f"{job_dir / 'REVIEW_RESPONSE.json'} then the watcher will resume.\n\n"
                     f"Signal payload:\n{review_payload}"],
                    capture_output=True, text=True, timeout=120,
                )
        except Exception as exc:
            print(f"[watcher] {task_id}: claude resume failed: {exc}", flush=True)

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
            if action == "abort":
                _kill_gracefully(pid)
                return
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
            return
        time.sleep(5)

    # Review timed out — resume and continue
    print(f"[watcher] {task_id}: review timed out, resuming", flush=True)
    signal_path.unlink(missing_ok=True)
    try:
        os.kill(pid, signal.SIGCONT)
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch a Codex analyst job. Dumb PID/log monitor.")
    parser.add_argument("--task", type=Path, required=True, help="Path to codex_task.json")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout in minutes")
    parser.add_argument("--poll-interval", type=int, default=10, help="Poll interval in seconds")
    args = parser.parse_args()

    task = json.loads(args.task.read_text(encoding="utf-8"))
    task_id = task["task_id"]

    # Job directory is parent of the task file (queue/running/{task_id}/)
    job_dir = args.task.parent
    result_path = job_dir / "analysis.json"
    stdout_path = job_dir / "codex_stdout.txt"
    stderr_path = job_dir / "codex_stderr.txt"
    status_path = job_dir / "job_status.json"
    schema_path = SCHEMA_DIR / "codex_post_run.schema.json"

    prompt = _build_analyst_prompt(task, job_dir)

    # Launch the analyst (gpt-5.4, xhigh) — it spawns codex-mini subagents as needed
    command = [
        "codex", "exec", "--skip-git-repo-check",
        "-m", "gpt-5.4",
        "--config", 'model_reasoning_effort="high"',
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
        )
        proc.stdin.write(prompt.encode())
        proc.stdin.close()

    pid = proc.pid
    timeout_s = args.timeout * 60
    status = "completed"
    heartbeat_path = job_dir / "heartbeat.json"

    while proc.poll() is None:
        time.sleep(args.poll_interval)
        elapsed = time.monotonic() - t0

        # Write heartbeat so external tools can see we're alive
        subagent_count = 0
        try:
            import subprocess as _sp
            ps_out = _sp.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
            subagent_count = len(ps_out.stdout.strip().splitlines()) if ps_out.stdout.strip() else 0
        except Exception:
            pass
        _write_json(heartbeat_path, {
            "job_id": task_id,
            "status": "running",
            "pid": pid,
            "elapsed_minutes": round(elapsed / 60, 1),
            "timeout_minutes": args.timeout,
            "subagent_count": subagent_count,
            "last_heartbeat_utc": _utc_now_iso(),
        })

        if elapsed > timeout_s:
            status = "timed_out"
            _kill_gracefully(pid)
            break

        _check_review_signal(job_dir, pid, task_id)

    exit_code = proc.wait()
    if status == "completed" and exit_code != 0:
        status = "failed"

    duration = time.monotonic() - t0
    finished_at = _utc_now_iso()

    _write_json(status_path, {
        "job_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 1),
        "output_path": str(result_path) if result_path.exists() else None,
        "log_path": str(stdout_path),
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
    })

    print(f"[watcher] {task_id}: {status} (exit={exit_code}, {duration:.0f}s)", flush=True)
    sys.exit(0 if status == "completed" else 1)


if __name__ == "__main__":
    main()
