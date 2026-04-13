#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import load_config, task_group_snapshot, task_group_summaries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize research-loop task groups.")
    parser.add_argument(
        "--config",
        default="automation/research_loop/config.json",
        help="Path to research-loop config.json",
    )
    parser.add_argument("--group-id", help="Summarize one task group only")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    return parser


def render_text(summary: dict) -> str:
    lines = [
        f"{summary['task_group_id']} [{summary['status']}]",
        f"  title: {summary['task_group_title']}",
        f"  graphs: {', '.join(summary['graph_ids']) or '(none)'}",
        f"  running: {', '.join(summary['running']) or '(none)'}",
        f"  runnable: {', '.join(summary['runnable_pending']) or '(none)'}",
        f"  completed: {', '.join(summary['completed']) or '(none)'}",
        f"  failed: {', '.join(summary['failed']) or '(none)'}",
    ]
    if summary["blocked_pending"]:
        blocked = ", ".join(f"{item['task_id']} ({item['reason']})" for item in summary["blocked_pending"])
        lines.append(f"  blocked: {blocked}")
    else:
        lines.append("  blocked: (none)")
    return "\n".join(lines)


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).resolve()
    repo_root = config_path.parents[2]
    config = load_config(config_path, repo_root)

    if args.group_id:
        summaries = [task_group_snapshot(config, args.group_id)]
    else:
        summaries = task_group_summaries(config)

    if args.json:
        print(json.dumps(summaries if not args.group_id else summaries[0], indent=2))
        return 0

    if not summaries:
        print("No task groups found.")
        return 0

    print("\n\n".join(render_text(summary) for summary in summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
