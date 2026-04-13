#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import load_config, load_session, save_json, stale_graph_summaries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect and escalate stale research-loop graphs.")
    parser.add_argument(
        "--config",
        default="automation/research_loop/config.json",
        help="Path to research-loop config.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report stale graphs without notifying Claude")
    return parser


def _alert_cache_path(config: dict) -> Path:
    return Path(config["paths"]["cycles_dir"]).parent / "stale_graph_alerts.json"


def _loop_completion_path(config: dict) -> Path:
    return Path(config["paths"]["cycles_dir"]).parent / "loop_completion.txt"


def _load_alert_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _notify_claude(config: dict, message: str) -> None:
    session = load_session(config)
    if not session:
        _loop_completion_path(config).write_text(message + "\n", encoding="utf-8")
        print("[stale-check] no session file; wrote loop_completion.txt", flush=True)
        return

    mode = str(session.get("mode") or "")
    session_id = str(session.get("session_id") or "")
    if mode == "interactive":
        _loop_completion_path(config).write_text(message + "\n", encoding="utf-8")
        print("=== Research Loop Result ===", flush=True)
        print(message, flush=True)
        print("============================", flush=True)
        return

    if session_id:
        subprocess.run(["claude", "-p", "--resume", session_id, message], check=False, timeout=120)
        print(f"[stale-check] resumed Claude session {session_id}", flush=True)
        return

    _loop_completion_path(config).write_text(message + "\n", encoding="utf-8")
    print("[stale-check] no session_id; wrote loop_completion.txt", flush=True)


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).resolve()
    repo_root = config_path.parents[2]
    config = load_config(config_path, repo_root)
    stale_graphs = stale_graph_summaries(config)
    report_path = Path(config["paths"]["cycles_dir"]).parent / "stale_graphs.json"
    save_json(report_path, {"stale_graphs": stale_graphs})

    if not stale_graphs:
        cache_path = _alert_cache_path(config)
        save_json(cache_path, {})
        print("[stale-check] no stale graphs", flush=True)
        return 0

    cache_path = _alert_cache_path(config)
    alert_cache = _load_alert_cache(cache_path)
    newly_stale = [
        summary for summary in stale_graphs
        if alert_cache.get(summary["graph_id"]) != str(summary.get("last_updated_utc"))
    ]
    save_json(
        cache_path,
        {summary["graph_id"]: str(summary.get("last_updated_utc")) for summary in stale_graphs},
    )

    if not newly_stale:
        print(f"[stale-check] stale graphs already alerted: {len(stale_graphs)}", flush=True)
        return 0

    message_lines = [
        "Stale research-loop graphs need review.",
        "These graphs have had no running or runnable tasks for longer than the configured stale threshold:",
    ]
    for summary in newly_stale:
        blocked = ", ".join(
            f"{item['task_id']} ({item['reason']})" for item in summary["blocked_pending"]
        ) or "none"
        message_lines.append(
            f"- {summary['graph_id']} [{summary['status']}] groups={','.join(summary['task_group_ids']) or 'none'} "
            f"age={summary['age_minutes']}m completed={len(summary['completed'])} failed={len(summary['failed'])} blocked={blocked}"
        )
    message_lines.append(
        "Review with `python3 automation/research_loop/graph_summary.py --stale-only` and decide whether to add follow-up tasks, mark the task group complete, or clean up a stuck dependency."
    )
    message = "\n".join(message_lines)

    print(message, flush=True)
    if not args.dry_run:
        _notify_claude(config, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
