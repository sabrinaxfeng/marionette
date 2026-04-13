#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.research_loop import graph_summaries, graph_summary, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize research-loop dependency graphs.")
    parser.add_argument(
        "--config",
        default="automation/research_loop/config.json",
        help="Path to research-loop config.json",
    )
    parser.add_argument("--graph-id", help="Summarize one graph only")
    parser.add_argument("--stale-only", action="store_true", help="Only show stale graphs")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    return parser


def render_text(summary: dict) -> str:
    lines = [
        f"{summary['graph_id']} [{summary['status']}]",
        f"  groups: {', '.join(summary['task_group_ids']) or '(none)'}",
        f"  running: {', '.join(summary['running']) or '(none)'}",
        f"  runnable: {', '.join(summary['runnable_pending']) or '(none)'}",
        f"  completed: {', '.join(summary['completed']) or '(none)'}",
        f"  failed: {', '.join(summary['failed']) or '(none)'}",
        f"  last updated: {summary['last_updated_utc'] or '(unknown)'}",
        f"  age_minutes: {summary['age_minutes'] if summary['age_minutes'] is not None else '(unknown)'}",
        f"  stale: {summary['stale']}",
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
    stale_after_minutes = int(config.get("stale_graph_minutes", 120) or 120)

    if args.graph_id:
        summaries = [graph_summary(config, args.graph_id, stale_after_minutes=stale_after_minutes)]
    else:
        summaries = graph_summaries(config, stale_after_minutes=stale_after_minutes)

    if args.stale_only:
        summaries = [summary for summary in summaries if summary["stale"]]

    if args.json:
        print(json.dumps(summaries if not args.graph_id else (summaries[0] if summaries else {}), indent=2))
        return 0

    if not summaries:
        print("No graphs found.")
        return 0

    print("\n\n".join(render_text(summary) for summary in summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
