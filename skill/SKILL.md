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
- **Supervisor + dispatcher**: long-lived scheduler runtime. Tasks are submitted as intent first; the supervisor/dispatcher claims runnable tasks deterministically from the DAG and launches them up to the configured parallelism cap.
- **Reviewer event drop**: decoupled reviewer-facing surface under `results/research_loop/reviewer_events/`. Post-job and review-pause logic copy the relevant artifacts there so you do not need to watch queue internals directly.

Important: the loop only becomes a real DAG when the analyst emits chained `next_task` / `next_tasks` with meaningful `depends_on` edges. Reusing `graph_id` or `task_group_id` without actual follow-up task chaining is just bookkeeping, not graph execution.

When parallel branches need to reconverge, make that reconvergence explicit as a **join task**: a normal successor task whose `depends_on` lists the parent branch tasks and whose objective is to synthesize their outputs into one next step.

**You are only resumed when:**
1. The analyst completes the whole task frame (no follow-up tasks such as `next_task` or `next_tasks`)
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

The task spec should encode execution **requirements** like timeout, reasoning effort, dependency shape, and write-surface conflicts. It should not pick a concrete worker identity; claiming and executor assignment belong to the scheduler.

For this loop, the most important thing to preserve is **task chaining**. If the work is likely to take several iterations, the analyst should emit successor tasks instead of trying to cram the whole arc into one oversized job. A `graph_id` matters because multiple chained tasks share it; a lone task with a `task_group_id` is still just one task.

This includes reconvergence. If you fan work out into independent branches, you should usually expect to fan it back in with an explicit join task unless the branches are truly standalone and can terminate independently.

### 4. Dispatch

Conceptually, the planner should submit task intent and let the scheduler own deterministic claiming. The reviewer should not hold a long-lived launcher process open while one task runs; launch should be atomic, and the supervisor should own the queue after that.

Preferred mental model:

```python
from lib.research_loop import load_config, write_task
config = load_config(...)
write_task(config, task)
```

Do not manually call `claim_task(...)` from the skill. Use the launcher entry point to atomically submit the task, and let the supervisor/dispatcher claim runnable work from queue state.

Before submitting work, make sure the supervisor service is running:

```bash
bash automation/research_loop/supervisor_ctl.sh start
```

Treat `supervisor_ctl.sh` as the stable interface:
- `bash automation/research_loop/supervisor_ctl.sh start`
- `bash automation/research_loop/supervisor_ctl.sh status`
- `bash automation/research_loop/supervisor_ctl.sh logs`
- `bash automation/research_loop/supervisor_ctl.sh stop`

Do not launch `supervisor_daemon.sh` directly from the skill unless you are debugging the daemon implementation itself.

Then submit the root task atomically:

```bash
bash automation/research_loop/launch_task.sh --task-file /abs/path/to/codex_task.json
```

`launch_task.sh` is an atomic submit/nudge boundary:
- it either queues the task successfully and nudges the supervisor, or fails without queueing it
- it returns immediately with `accepted: true|false`
- it prints the stable reviewer-event paths for that task

For the default reviewer-facing path, glue submission and watching together so you do not forget the watch step:

```bash
bash automation/research_loop/launch_and_watch_task.sh --task-file /abs/path/to/codex_task.json
```

Use `run_in_background` for `launch_and_watch_task.sh` if you want Claude Code to notify you when the task first needs review attention. That first event may be:
- a `checkpoint` / `blocked` review pause
- a failure/escalation
- a terminal completion signal

Keep the standalone watcher too. It is the right tool when the analyst has already queued a repair/follow-up task and you need to watch that task without resubmitting it:

```bash
bash automation/research_loop/watch_reviewer_events.sh --task-id TASK_ID --once
```

If you intentionally want submit-only behavior, use `launch_task.sh` directly and then either:
- use the returned `watch_command`
- or run `watch_reviewer_events.sh --task-id TASK_ID --once` yourself

Save the reviewer session bridge before dispatching. Use `mode: "interactive"` when the current reviewer is Claude in the same interactive session and you want the decoupled reviewer-event drop plus local artifacts. Use a non-interactive mode such as `mode: "cli"` when the glue layer should actively resume the reviewer via the provider CLI.

### 5. On resume — review the arc of work

When resumed:
- Read the chain of completed jobs in `queue/completed/` (there may be several from self-iteration)
- Read sibling branches too when a graph fan-outs into parallel tasks
- Review the final `analysis.json`
- Check what files changed, what metrics moved
- Update `state.json` and `CURRENT_CYCLE.md`
- Decide: launch the next task frame, queue a join task, or stop

If you are acting as a temporary reviewer rather than the usual primary reviewer:
- read `results/research_loop/planner_handoff.json` first
- prefer bounded queue grooming over large strategic replans
- add short synthesis/report tasks when completed work has produced useful result lines but has not yet been turned into stable artifacts
- leave a clear grooming note if you materially reorder the queue

### 5a. Dependency graph rules

When reviewing or launching work, treat the queue as a DAG:

- Tasks in one frame share a `graph_id`
- The graph is created by `next_task` / `next_tasks` plus `depends_on`, not by labels alone
- Related investigative branches may also share a `task_group_id`
- `depends_on` controls when a task becomes runnable
- A join task is just a task with multiple `depends_on` parents; use it to reconverge parallel work explicitly
- `conflict_keys` serialize tasks that should not edit the same surface at once
- The dispatcher launches any runnable task up to `max_parallel_jobs`
- Claiming should be treated as deterministic scheduler behavior, not an analyst/reviewer choice
- Use `python3 automation/research_loop/task_group_summary.py` to see which groups are active, blocked, or ready for synthesis
- Use `python3 automation/research_loop/graph_summary.py` to inspect graph-level status, especially for blocked/failed/stale graphs
- Use `python3 automation/research_loop/check_stale_graphs.py --dry-run` if the queue feels quiet and you want to see whether anything has gone stale enough to require review

When you instruct the analyst, tell it explicitly:

- prefer explicit task chaining over one giant terminal task
- emit `next_task` for sequential follow-up work
- emit `next_tasks` when downstream branches are independent
- emit an explicit join task when parallel branches need to be synthesized back into one line of work
- reuse the same `graph_id` across the frame
- reuse the same `task_group_id` for tasks that belong to one research question, even if they fan out across multiple graphs later
- do not mistake `task_group_id` for the graph itself; if we are not using synthesis or grouped review behavior, it is mostly bookkeeping
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
- own join/synthesis behavior yourself: when a task group becomes `ready_for_synthesis`, review the whole group and either queue an explicit join task or mark the group complete if no reconvergence is needed

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

When this happens:
- `progress` is informational only; the analyst keeps running
- `checkpoint` and `blocked` pause the analyst and wait for `REVIEW_RESPONSE.json`
- the watcher writes `review_status.json` and `review_notify.json`
- the session bridge also writes `results/research_loop/loop_completion.txt` and `results/research_loop/planner_handoff.json`
- the reviewer event drop receives a copied task bundle under `results/research_loop/reviewer_events/...`
- the stable per-task pointer is updated at `results/research_loop/reviewer_events/tasks/<task_id>/latest_event.json`

Important: in `mode: "interactive"`, these files and reviewer-event bundles are the handoff surface, but they do **not** automatically re-open your session mid-run. If you need provider-driven wake-up for a paused review, use `mode: "cli"`.

## Session bridge

Save before dispatching:
```python
save_session(config, {
    "provider": "claude",  # or "codex" for temporary reviewer takeover
    "session_id": "<current session>",
    "mode": "interactive",  # or "cli" for provider-driven resume
    "status": "waiting",
    "current_cycle": N
})
```

Mode semantics:
- `mode: "interactive"`: write marker/handoff artifacts and reviewer-event bundles; this does not trigger provider CLI resume
- `mode: "cli"`: use the provider CLI to resume the saved reviewer session directly

Every reviewer notification also writes `results/research_loop/planner_handoff.json`. Treat that file as the portable handoff artifact if another capable reviewer instance needs to take over queue grooming or synthesis temporarily.

For temporary Codex reviewer takeover, prefer `provider: "codex"` with `mode: "cli"`. Do not assume Codex currently has the same in-session background notification behavior as Claude Code.

## Guardrails

- Never modify files outside the repo without explicit user approval
- Never force-push or delete branches
- If final results show regression, revert before continuing
- If unsure whether to continue to the next task frame, ask the user
