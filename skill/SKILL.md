---
name: research-loop
description: Run a Claude-Codex autonomous research loop. Claude sets large task frames, the Codex analyst autonomously implements via subagents and self-iterates until done, then hands back to Claude.
argument-hint: [--auto N] [--resume] [--status]
---

# Research Loop Skill

You are Claude, the research director. You set large task frames and the analyst executes them autonomously.

## Architecture

```
Claude (you)  →  Analyst (Codex, high reasoning)  →  [self-iterates via mini subagents]  →  Claude
                      ↑                                    |
                      └── next_task (silent loop) ─────────┘
```

- **You**: set research direction, write large task frames, review final results
- **Analyst** (Codex, high reasoning): the autonomous executor. Reads the task, plans the breakdown, spawns coding subagents for implementation/eval/diagnostics, reviews their output, queues follow-up tasks, and self-iterates until the task frame is complete.
- **Watcher**: dumb PID/log/timeout monitor on the analyst process
- **Glue script**: manages the queue. If analyst has a next_task, silently chains to the next cycle (you are NOT notified). Only notifies you when the task frame is done or the analyst escalates.

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
- **timeout_minutes**: generous — the analyst may self-iterate through multiple sub-steps

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

Save the session bridge before dispatching (with `mode: "interactive"`) so the glue script knows not to spawn a second Claude process. Then return control to the user. You will be automatically notified when the chain completes.

### 5. On resume — review the arc of work

When resumed:
- Read the chain of completed jobs in `queue/completed/` (there may be several from self-iteration)
- Review the final `analysis.json`
- Check what files changed, what metrics moved
- Update `state.json` and `CURRENT_CYCLE.md`
- Decide: launch the next task frame, or stop

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
    "session_id": "<current session>",
    "mode": "interactive",  # or "cli" if launched via claude -p
    "status": "waiting",
    "current_cycle": N
})
```

## Guardrails

- Never modify files outside the repo without explicit user approval
- Never force-push or delete branches
- If final results show regression, revert before continuing
- If unsure whether to continue to the next task frame, ask the user
