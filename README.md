# Marionette

A multi-agent research loop harness-lite where a shared tracker document pulls the strings.

Powered by an LLM-executed state machine that coordinates **Claude** (director) and **Codex** (analyst + workers) to iteratively implement, evaluate, and refine code changes toward a measurable goal. Intended as show-and-tell of a tested working pattern on research repos, but anything with a **clearly defined exit target** should work well.

[!WARNING]
Like any PAYG cloud service, token usage of LLM may exceed your budget. Even though this repo is built to save on context window, spawning multiple subagents may burn many tokens in a short frame of time. Watch your bill if you are not using a subscription-based token supplier, and manage scope of your project in the tracker doc. 

## Core Idea: LLM-Executed State Machine

Traditional automation scripts encode transitions in code. This framework encodes transitions in **natural language documents** and lets LLMs execute them.

```
                          ┌─────────────────────────────────┐
                          │     Tracker Doc (IMPROVEMENTS.md)│
                          │     = shared world model         │
                          │                                  │
                          │  Proposals → Active → Validated  │
                          │  with status, evidence, metrics  │
                          └──────────┬──────────────────────┘
                                     │ grounds
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
              ▼                      ▼                      ▼
    ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
    │  Claude (director)│  │ Analyst (Codex)  │  │ Workers (mini)   │
    │  reads tracker    │  │ reads task frame │  │ read instructions │
    │  picks next work  │──│ breaks it down   │──│ write code/tests │
    │  writes task frame│  │ spawns workers   │  │ run benchmarks   │
    │  reviews results  │  │ self-iterates    │  │ report results   │
    └──────────────────┘  └──────────────────┘  └──────────────────┘
```

The state machine has five states:

| State | Owner | Transition |
|-------|-------|------------|
| **idle** | — | Claude reads tracker, picks work |
| **planning** | Claude | Writes task frame from tracker context |
| **executing** | Analyst | Self-iterates through sub-tasks silently |
| **reviewing** | Claude | Reads results, updates tracker |
| **stopped** | — | Target reached or stop condition hit |

The key insight: **the tracker document is the control surface, not the code**. Claude reads it to decide what to work on, the analyst reads the task frame derived from it, and results flow back into the tracker. Every agent at every layer is grounded by the same document.

## Why a Tracker Document Matters

Without a shared tracker, multi-agent loops degrade quickly:

- **Claude** loses context across sessions and picks redundant or low-value work
- **Analyst** doesn't know what's been tried before, what's parked, or what the acceptance bar is
- **Workers** implement changes that conflict with earlier decisions

The tracker doc (we use `IMPROVEMENTS.md` but any structured document works) solves this by serving as:

1. **Kanban board** — proposals move through statuses: `Proposed → Active → Implemented → Validated → Parked`
2. **Decision log** — why things were tried, what the evidence showed, why they were accepted or rejected
3. **Grounding context** — Claude reads it before every planning step; the analyst receives relevant sections in its task frame
4. **Backlog** — stale or completed items get swept to a separate file, keeping the active tracker lean

This is arguably the most important piece of the framework. The automation (watcher, glue, schemas) is mechanical plumbing. The tracker is what makes the loop *intelligent* — it's the shared memory that turns independent LLM calls into a coherent research program.

### The human owns the tracker

The tracker document is **not** managed by the agents autonomously. The human user is the curator:

- **You** decide what proposals enter the tracker, what gets prioritized, and what gets parked
- **Claude** proposes status updates based on results, but you approve or edit them
- **You** sweep stale items to the backlog when the active tracker gets long
- **You** set acceptance gates and non-goals that constrain what the agents can do

This is by design. The tracker is the human's control surface over an otherwise autonomous loop. If the agents owned the tracker, you'd have no steering wheel. In practice, this means spending 5-10 minutes per session reviewing and grooming the tracker before launching a task frame — the same way you'd groom a sprint board before a planning meeting.

### Sample tracker structure

```markdown
# Project — Improvement Tracker

## Current Status
| Metric | Value | Target |
|--------|-------|--------|
| primary_score | 0.62 | 0.85 |

## Active Proposals

### P1 — Feature name
**Status:** Implemented, needs benchmark confirmation.
**Expected gain:** +3-5pp on primary_score.
**Evidence so far:** Unit tests pass, n=20 pilot shows +2pp.
**Acceptance gate:** n=100 benchmark shows improvement without regression.
**Files touched:** src/feature.py, tests/test_feature.py

### P2 — Another feature
**Status:** Proposed.
**Design:** [detailed design here]
**Expected gain:** ...

## Parked
### P0 — Earlier attempt
**Status:** Parked after audit.
**Why:** Remaining misses are heterogeneous and low-ROI for further work.
```

The analyst reads the relevant section of this doc as part of its task frame. When it finishes, Claude updates the tracker with results. The cycle repeats.

## Architecture

```
Claude (director)  -->  Analyst (Codex, high reasoning)  -->  Workers (codex-mini)
       ^                        |                                    |
       |                        +-- spawns subagents for impl/eval --+
       |                        |
       |                        +-- self-iterates via next_task -----+
       |                                                             |
       +--- only notified on: completion / escalation / failure -----+
```

**Three layers:**

1. **Claude** reads the tracker doc, picks the highest-value work, and writes a large task frame. It does not micromanage sub-steps.

2. **Analyst** (Codex at high reasoning effort) is the autonomous coordinator. It receives a task frame, breaks it into sub-steps, spawns coding subagents, reviews their output, and self-iterates through follow-up tasks. Claude is only notified when the entire task frame completes or the analyst escalates.

3. **Watcher + Glue** is a dumb bash/Python process monitor. It watches the analyst's PID, enforces timeouts, writes heartbeats, and manages the file-backed task queue. When the analyst finishes, the glue script checks for a `next_task` and either silently chains to the next iteration or resumes Claude.

### State transitions in detail

```
                    ┌──────────┐
                    │   idle   │
                    └────┬─────┘
                         │ Claude reads tracker, picks work
                         ▼
                    ┌──────────┐
                    │ planning │  Claude writes codex_task.json
                    └────┬─────┘
                         │ dispatch to watcher
                         ▼
                    ┌──────────┐
              ┌────>│executing │  Analyst self-iterates
              │     └────┬─────┘
              │          │
              │     next_task?──yes──> (silent chain, stay in executing)
              │          │
              │          no / escalation / failure
              │          │
              │          ▼
              │     ┌──────────┐
              │     │reviewing │  Claude reads results, updates tracker
              │     └────┬─────┘
              │          │
              │     stop?───yes──> ┌─────────┐
              │          │         │ stopped  │
              │          no        └──────────┘
              │          │
              └──────────┘
```

## Key Design Decisions

- **File-backed queue** (`pending/` -> `running/` -> `completed/` | `failed/`) — no database, no daemon, fully inspectable.
- **Analyst self-iteration** — the glue script silently chains `next_task` / `next_tasks` without notifying the reviewer. This keeps the high-level review context free for direction instead of micro-steps.
- **Dependency graph + conflict keys** — tasks can fan out into parallel branches via `depends_on`, while `conflict_keys` serialize branches that touch the same write surface.
- **Task groups over graphs** — DAGs handle execution order; `task_group_id` lets the reviewer synthesize several related tasks as one research thread.
- **Operational visibility** — graph summaries, task-group summaries, stale-graph detection, and a local dashboard make the queue inspectable without reading raw JSON by hand.
- **Structured output contracts** — JSON schemas for tasks, results, and job status ensure the analyst's output is machine-parseable.
- **Review signals** — the analyst can pause itself and write `REVIEW_REQUESTED.json` to escalate to the reviewer on architectural decisions or ambiguities.
- **Dual-mode reviewer bridge** — works with a reviewer session recorded in `reviewer_session.json`. Claude can keep using the current interactive-background flow, while Codex or Claude CLI reviewers can be resumed through provider-specific `resume` commands when `mode` is non-interactive.
- **Tracker doc as shared world model** — all agents are grounded by the same living document rather than ephemeral prompt context.
- **Planner handoff artifact** — every notification writes `planner_handoff.json`, which gives another capable reviewer instance enough queue/state context to temporarily pick up scheduling work.

## Directory Structure

```
automation/research_loop/
  job_watcher.py          # Launches analyst, monitors PID, enforces timeout
  run_post_job.sh         # Glue: reads analysis.json, chains next_task or resumes the reviewer
  dispatch_ready_tasks.py # Scheduler: launches runnable DAG nodes up to max_parallel_jobs
  status.sh               # Quick status check for running jobs + queue counts
  graph_summary.py        # Graph-level summaries, including stale detection
  task_group_summary.py   # Task-group rollups for synthesis handoffs
  check_stale_graphs.py   # Escalates stale graphs back to Claude
  dashboard.py            # Local read-only dashboard server
  dashboard_static/       # Dashboard HTML/CSS/JS assets
  config.example.json     # Template configuration
  schemas/
    codex_task.schema.json       # Task contract
    codex_post_run.schema.json   # Analyst output contract
    job_status.schema.json       # Watcher output contract
    codex_result.schema.json     # Cycle result contract
    claude_plan.schema.json      # Claude planning contract

lib/
  research_loop.py        # Python helpers: config, state, queue, session bridge

skill/
  SKILL.md                # Claude Code skill definition (invoke via /research-loop)

docs/
  sample_tracker.md       # Example tracker document structure
```

## Setup

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed
- [Codex](https://platform.openai.com/docs/guides/codex) CLI installed (`codex exec`)
- Valid subscription for both 
- A git repository you want the loop to work on
- A tracker document in your repo (e.g. `IMPROVEMENTS.md`)

### Installation

Clone this repo and ask claude to install, or manually:

1. Copy the contents of this repo into your project:
   ```bash
   cp -r automation/ lib/ skill/ /path/to/your/project/
   ```

2. Create your config:
   ```bash
   cp automation/research_loop/config.example.json automation/research_loop/config.json
   # Edit config.json with your goal, metrics, and model preferences
   ```

3. Install the Claude Code skill:
   ```bash
   mkdir -p /path/to/your/project/.claude/skills/research-loop/
   cp skill/SKILL.md /path/to/your/project/.claude/skills/research-loop/SKILL.md
   ```

4. Create the results directory:
   ```bash
   mkdir -p results/research_loop/queue/{pending,running,completed,failed}
   ```

5. Set up your tracker document (or use the sample):
   ```bash
   cp docs/sample_tracker.md IMPROVEMENTS.md
   ```

### Configuration

Edit `automation/research_loop/config.json`:

```json
{
  "goal": "Your measurable research/engineering goal here",
  "headline_metric": "primary_score",
  "target_value": 0.85,
  "max_cycles": 5,
  "max_parallel_jobs": 2,
  "stale_graph_minutes": 120,
  "stop_conditions": {
    "max_consecutive_inconclusive_cycles": 2,
    "max_consecutive_stagnant_cycles": 2,
    "max_consecutive_regressions": 1
  },
  "codex": {
    "model": "gpt-5.4",
    "sandbox": "workspace-write"
  }
}
```

## Usage

### Via Claude Code skill

```
/research-loop              # Plan and launch one task frame
/research-loop --status     # Check current queue and job state
/research-loop --resume     # Resume from last completed job
/research-loop --auto 3     # Run up to 3 task frames sequentially
```

### Manual dispatch

```python
from lib.research_loop import load_config, write_task, claim_task

config = load_config(Path("automation/research_loop/config.json"), Path("."))

task = {
    "task_id": "my-task",
    "objective": "Implement feature X and validate with benchmark Y",
    "hypothesis": "Feature X should improve metric Z by ~5%",
    "instructions": ["Read the relevant code", "Implement the change", "Run the benchmark"],
    "acceptance_gate": "Benchmark Y shows improvement without regression on Z",
    "timeout_minutes": 45,
    "source": "claude"
}

write_task(config, task)
claim_task(config, "my-task")
```

Then launch the watcher:
```bash
python3 automation/research_loop/job_watcher.py \
  --task results/research_loop/queue/running/my-task/task.json \
  --timeout 45

bash automation/research_loop/run_post_job.sh \
  results/research_loop/queue/running/my-task
```

### Check status

```bash
bash automation/research_loop/status.sh
python3 automation/research_loop/graph_summary.py
python3 automation/research_loop/task_group_summary.py
python3 automation/research_loop/check_stale_graphs.py --dry-run
python3 automation/research_loop/dashboard.py --port 8765
```

## How the Loop Works

0. **You** initiates the loop via command or chatting with claude after installing this harness.

1. **Claude** reads the tracker doc, picks the highest-value proposal, and writes a `codex_task.json` with objective, instructions, and acceptance gate.

2. **Watcher** (`job_watcher.py`) launches the Codex analyst process, monitors its PID, writes heartbeats, and enforces the timeout.

3. **Analyst** (Codex with task-chosen reasoning effort) reads the task, breaks it down, and spawns coding subagents (`codex-mini`) for implementation and evaluation. It writes `analysis.json` with its decision (`accept`/`reject`/`escalate`) and optionally `next_task` or `next_tasks`.

4. **Glue** (`run_post_job.sh`) reads `analysis.json`:
   - If `next_task` / `next_tasks` exist: silently writes them to `pending/`, dispatches any runnable branches, and keeps Claude asleep while the graph is still active.
   - If a graph blocks/fails, or a task group reaches `ready_for_synthesis`: resumes Claude via the session bridge.

5. **Claude** reviews the chain of completed jobs, updates the tracker doc with results and new status, and decides whether to launch another task frame or stop. 

## Schemas

All inter-agent communication uses JSON schemas in `automation/research_loop/schemas/`:

| Schema | Purpose |
|--------|---------|
| `codex_task.schema.json` | Task contract: what to do, how to validate, which graph/task group it belongs to |
| `codex_post_run.schema.json` | Analyst output: decision, metrics, next_task / next_tasks |
| `job_status.schema.json` | Watcher output: exit code, duration, paths |
| `codex_result.schema.json` | Cycle result for state tracking |
| `claude_plan.schema.json` | Claude's planning output |

## Lessons Learned (for LLM reference)

From running this framework on a real research project:

- **The tracker doc is the most important component.** Without it, Claude picks redundant work, the analyst doesn't know what's been tried, and the loop degenerates into random exploration. With it, every agent at every layer shares the same understanding of what's done, what's active, and what's next.

- **Task frames should be large.** "Implement and validate the whole feature" not "bump parameter from 3 to 5". The analyst is smart enough to break down large tasks; Claude's context window is too valuable for micro-management.

- **The analyst should self-iterate silently.** Only resume Claude when the task frame is truly done. In practice, a single task frame may produce 3-4 self-iterated sub-tasks before handing back. Every unnecessary context switch wastes Claude's attention.

- **Sweep stale items aggressively.** The tracker doc must stay lean. Move completed, parked, and rejected items to a backlog file. If the active tracker exceeds ~500 lines, Claude's planning quality degrades.

- **Don't use `setsid` with Claude Code's `run_in_background`.** `setsid` creates a new session that decouples the child process — the background notification fires when the wrapper exits, not when the work finishes.

- **Stall detection creates false positives.** The analyst spawns subagents that run long benchmarks with no stdout for 10+ minutes. Timeout alone is sufficient; stall thresholds just kill healthy jobs.

- **Always pass explicit `model_reasoning_effort`** when using codex-mini, as global config may set an unsupported level.

## License

Apache 2.0
