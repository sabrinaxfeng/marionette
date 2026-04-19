---
name: research-loop
description: Run a Claude-Codex autonomous research loop. Claude sets large task frames, the Codex analyst autonomously implements via subagents and self-iterates until done, then hands back to Claude. Use to start, resume, or inspect the loop.
argument-hint: [--auto N] [--resume] [--status]
---

# Research Loop Skill

You are the reviewer/director for the research loop. Claude is the default reviewer, but another reviewer instance such as Codex can temporarily take over queue grooming and bounded review work when needed.

## Architecture

```
Claude (you)  →  Analyst (Codex, variable effort)  →  [self-iterates via mini subagents]  →  Claude
                      ↑                                    |
                      └── next_task / next_tasks DAG (silent loop) ─────────┘
```

- **You**: set research direction, write large task frames, review final results
- **Analyst** (Codex, usually `medium` effort): the autonomous executor. Reads the task, plans the breakdown, spawns coding subagents for implementation/eval/diagnostics, reviews their output, queues follow-up tasks, and self-iterates until the task frame is complete.
- **Watcher**: dumb PID/log/timeout monitor on the analyst process
- **Glue script + dispatcher**: manages the queue. If the analyst emits `next_task` or `next_tasks`, they are queued into a dependency graph and any runnable tasks are launched silently up to the configured parallelism cap. You are only notified when the task frame is done, blocked, or the analyst escalates.

**You are only resumed when:**
1. The analyst completes the whole task frame (no next_task)
2. The analyst explicitly escalates (architectural decision, ambiguity)
3. A job fails/stalls/times out

## Task frame sizing

A task should be **large** — a coherent unit of work with clear acceptance criteria:
- "Implement and validate the new caching layer end-to-end"
- "Run the full evaluation suite and produce results tables"
- "Implement cross-module refactoring for the data pipeline"

NOT small tasks like "bump top_k from 3 to 5" — that's a sub-step the analyst decides on internally.

## Invocation modes

- `/research-loop` — plan and launch one task frame, wait for results
- `/research-loop --auto N` — launch up to N task frames sequentially
- `/research-loop --resume` — pick up from the last saved state or completed job
- `/research-loop --status` — show current loop state without running anything

Arguments: $ARGUMENTS

## What to do

### 1. Load state

Read these files:
- `automation/research_loop/config.json` — goal, limits, stop conditions
- `results/research_loop/state.json` — loop state (if exists)
- `results/research_loop/queue/` — pending/running/completed/failed jobs
- `python3 automation/research_loop/graph_summary.py` — graph-level status, including stale graphs
- `python3 automation/research_loop/task_group_summary.py` — task-group status, including groups ready for synthesis
- Your tracker document (e.g. `IMPROVEMENTS.md`) — the shared world model that grounds all planning

### 2. If --status, report and stop

Show: current queue state, last completed job + analysis, active job if any.

### 3. Plan the task frame

Read the tracker document. Pick the highest-value proposal that is ready for implementation.
Write a `codex_task.json`:
- **objective**: the full section goal, not a micro-step
- **hypothesis**: what we expect to validate
- **instructions**: high-level steps — the analyst will expand these
- **acceptance_gate**: what "done" looks like for the whole section
- **timeout_minutes**: set this deliberately for the task instead of defaulting to one fixed number
  - use shorter limits for bounded diagnostics, schema/docs edits, or small eval slices
  - use longer limits for benchmark runs, multi-step implementation+eval loops, or tasks likely to fan out through several analyst sub-steps
  - 60 minutes is not a default; it is only appropriate when the task genuinely needs that long
- **graph_id**: stable ID for this task frame
- **task_group_id**: research-management group for related tasks; reuse it across one investigative thread
- **task_group_title**: short human-readable label for the investigative thread
- **depends_on**: `[]` for the root task
- **conflict_keys**: write-surface locks, e.g. `["src", "docs"]`
- **analyst_reasoning_effort**: choose `medium` by default; use `high` for non-trivial synthesis/debugging; use `xhigh` only when clearly necessary

### 4. Dispatch

```python
from lib.research_loop import load_config, write_task, claim_task
config = load_config(...)
write_task(config, task)
claim_task(config, task_id)
```

Launch the watcher + glue chain using `run_in_background`:

```bash
python3 automation/research_loop/job_watcher.py \
  --task results/research_loop/queue/running/TASK_ID/task.json \
  --timeout TIMEOUT_MINUTES && \
bash automation/research_loop/run_post_job.sh results/research_loop/queue/running/TASK_ID
```

**Use `run_in_background` for this command.** When it finishes, Claude Code will automatically notify you with the command's stdout, which includes the glue script's summary. This is how results return to your interactive session — no separate resume needed.

Save the reviewer session bridge before dispatching. Use `mode: "interactive"` for in-session background notifications when the current harness supports them, or a non-interactive mode such as `mode: "cli"` when you want the glue layer to resume Claude/Codex explicitly via the provider CLI. Then return control to the user.

### 5. On resume — review the arc of work

When resumed:
- Read the chain of completed jobs in `queue/completed/` (there may be several from self-iteration)
- Read sibling branches too when a graph fan-outs into parallel tasks
- Review the final `analysis.json`
- Check what files changed, what metrics moved
- Update `state.json` and `CURRENT_CYCLE.md`
- Decide: launch the next task frame, or stop

If you are acting as a temporary reviewer rather than the usual primary reviewer:
- read `results/research_loop/planner_handoff.json` first
- prefer bounded queue grooming over large strategic replans
- add short synthesis/report tasks when completed work has produced useful result lines but has not yet been turned into stable artifacts
- leave a clear grooming note if you materially reorder the queue

### 5a. Dependency graph rules

When reviewing or launching work, treat the queue as a DAG:

- Tasks in one frame share a `graph_id`
- Related investigative branches may also share a `task_group_id`
- `depends_on` controls when a task becomes runnable
- `conflict_keys` serialize tasks that should not edit the same surface at once
- The dispatcher launches any runnable task up to `max_parallel_jobs`
- Use `python3 automation/research_loop/task_group_summary.py` to see which groups are active, blocked, or ready for synthesis
- Use `python3 automation/research_loop/graph_summary.py` to inspect graph-level status, especially for blocked/failed/stale graphs
- Use `python3 automation/research_loop/check_stale_graphs.py --dry-run` if the queue feels quiet and you want to see whether anything has gone stale enough to require review

When you instruct the analyst, tell it explicitly:

- emit `next_tasks` when downstream branches are independent
- reuse the same `graph_id` across the frame
- reuse the same `task_group_id` for tasks that belong to one research question, even if they fan out across multiple graphs later
- use conservative `conflict_keys` for overlapping code/docs/eval artifacts
- if overlap is uncertain, choose broader conflict keys and serialize rather than assuming branches are safe
- reasonable broad keys include `code`, `docs`, `eval:webqsp`, `eval:benchmark`, or a feature/bucket-level key like `grounding`
- choose `timeout_minutes` intentionally:
  - keep bounded diagnostics tight
  - extend time for full benchmark runs or multi-step tasks
  - do not mechanically assign `60`; match the limit to the expected wall-clock need plus some safety margin
- choose `analyst_reasoning_effort` intentionally:
  - default: `medium`
  - escalate to `high` only for genuinely hard coordination or failure analysis
  - reserve `xhigh` for rare cases where lower effort is unlikely to be enough
- if unsure whether branches are independent, serialize them instead
- parallelize launch according to the DAG whenever branches are independent and conflict keys do not overlap
- own join/synthesis behavior yourself: when a task group becomes `ready_for_synthesis`, review the whole group and decide whether to add more tasks or mark the group complete

### 6. Stop conditions

- Target metric reached
- Max cycles hit
- Consecutive failures/stagnation exceed config limits
- Analyst escalated with something you need to decide
- User intervened

## Review signals

The analyst can pause itself and request your review by writing `REVIEW_REQUESTED.json`:
```json
{"reason": "checkpoint|blocked|progress", "message": "...", "artifacts": [...], "stage": "..."}
```

This should be rare — the analyst should handle most decisions autonomously.

## Session bridge

Save before dispatching:
```python
save_session(config, {
    "provider": "claude",  # or "codex"
    "session_id": "<current session>",
    "mode": "interactive",  # or "cli" to force provider resume
    "status": "waiting",
    "current_cycle": N
})
```

Every reviewer notification also writes `results/research_loop/planner_handoff.json`. Treat that file as the portable handoff bundle for temporary reviewer takeover.

## Guardrails

- Never modify files outside the repo without explicit user approval
- Never force-push or delete branches
- If final results show regression, revert before continuing
- If unsure whether to continue to the next task frame, ask the user
