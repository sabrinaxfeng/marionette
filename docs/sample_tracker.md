# Project — Improvement Tracker

> Active proposals only. Completed and parked items live in `BACKLOG.md`.

## 1. Current Benchmark Status

| Metric | Current | Target | Notes |
|--------|---------|--------|-------|
| primary_score | 0.620 | 0.850 | n=100, strict evaluation |
| secondary_score | 0.446 | — | different test distribution |

## 2. Scope Rules

An improvement is in scope when it:
- improves the primary evaluation metric in a dataset-agnostic way
- is measurable on an existing benchmark
- does not introduce hard-coded special cases

## 3. Active Proposals

### P1 — Feature name here
**Status:** Proposed.

**Problem:** Describe the specific failure mode or bottleneck this addresses. Include concrete examples from evaluation traces if available.

**Design:**
1. Step one of the approach
2. Step two
3. Step three

**Expected gain:** +3-5pp on primary_score based on trace analysis of failure bucket X.

**Files to touch:**
- `src/module.py` — main implementation
- `tests/test_module.py` — validation

**Validation sequence:**
1. Manual trace on 3 known failure cases
2. Focus slice n=20 on the target failure bucket
3. Full benchmark n=100 — must not regress

**Non-goals:**
- No hard-coded per-example rules
- No replacement of the existing architecture

### P2 — Another feature
**Status:** Implemented, needs benchmark confirmation.

**Current implementation:**
- `src/other.py` now does X
- Unit tests pass: `pytest tests/test_other.py` -> 5 passed

**Remaining:** Run full n=100 benchmark to confirm. If primary_score improves without regression on secondary_score, update this status to Validated.

### P3 — Third proposal
**Status:** Active — first slice implemented.

**Problem:** [...]

**Implemented so far:**
- Phase 1 landed: [description]
- Pilot result: n=8, shows +1pp but below statistical significance

**Still missing:**
- Phase 2: [description]
- Larger-n validation

**Current decision:** Keep phase 1, defer phase 2 until P1 and P2 are settled.

## 4. Evaluation Plan

### Required final runs
1. Primary benchmark n=100
2. Secondary benchmark n=100
3. Ablation: feature P1 on vs off
4. Ablation: feature P2 on vs off

### Ablation rationale
Each ablation tests a specific thesis claim. They are more important than squeezing extra benchmark points.

## 5. Operational Run Checklist

```bash
# Primary benchmark
python -m eval.benchmark --n 100 --output /tmp/primary_final.json

# Secondary benchmark
python -m eval.benchmark --dataset secondary --n 100 --output /tmp/secondary_final.json

# P1 ablation
python -m eval.benchmark --n 100 --disable-feature p1 --output /tmp/ablation_p1.json
```
