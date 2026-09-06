# Adaptive Deliberation and Value Measurement Plan

**Status:** Measurement milestone implemented 2026-09-05. Adaptive routing remains deferred; no automatic escalation is enabled.

## Objective

Determine whether additional deliberation earns its inference cost before automating escalation. Conclave should use the smallest review shape that can materially improve a decision, preserve the user's explicit mode choice, and record why every additional agent run occurred.

## Current Baseline

A read-only aggregate of the local database through 2026-08-29 shows:

| Mode | Completed tasks | Agent runs | Runs per completed task |
| --- | ---: | ---: | ---: |
| `resolve` | 315 | 419 | 1.33 |
| `consult` | 30 | 127 | 4.23 |
| `conclave` | 39 | 413 | 10.59 |

Across all 384 completed tasks, the observed mix averaged 2.50 agent runs per task. This is dogfooding evidence, not customer validation: the database represents one user, tasks were not randomized across modes, and USD cost is populated for only a minority of runs.

## Operating Rules

1. Every additional model call must answer a new question capable of changing the decision: find a material flaw, close an evidence gap, resolve a named conflict, or synthesize unresolved dissent.
2. No call is justified solely by a desire for reassurance or consensus.
3. Use recorded agent-run count as the first universal compute unit; token and USD totals remain secondary until coverage is reliable.
4. Confidence is a weak signal, never a sole escalation trigger.
5. A judge cannot erase dissent, and judge or synthesis calls occur only when unresolved material disagreement requires them.
6. Budget exhaustion, stagnation, and failure are explicit outcomes, never reported as successful convergence.

## Milestone 1 — Measure Value Without Added Inference

Milestone 1 changes persistence, APIs, and the dashboard only. It must not call an adapter, alter orchestration, change mode defaults, or auto-escalate a task.

### Persistence

Add an idempotent schema migration for a one-row-per-task `task_feedback` table:

- `task_id` — primary key and foreign key to `tasks`.
- `decision_changed` — nullable boolean.
- `material_risk_found` — nullable boolean.
- `extra_review_worth_it` — nullable boolean.
- `note` — optional local text with a conservative length cap.
- `created_at` and `updated_at` — UTC timestamps.

Do not duplicate agent-run totals in this table. Compute run count, token totals, known cost, cost coverage, and elapsed time from existing task and `agent_runs` records.

### API

- Add `PUT /api/tasks/{task_id}/feedback` as an idempotent upsert for terminal tasks.
- Include feedback and a computed `compute_summary` in task detail responses.
- Add a local aggregate endpoint grouped by mode with task count, completion rate, agent runs per task, feedback coverage, value signals, and token/USD coverage.
- Never expose task prompts, answers, notes, or other content through the aggregate endpoint.

### Dashboard

Place an optional feedback card beside the existing authoritative decision panel:

1. Did this change or materially refine your decision?
2. Did it identify a material risk you had missed?
3. Was the additional review worth its time and compute?

Show actual agent runs and available token/cost coverage. Saving or editing feedback must not resume a task or launch work.

### Acceptance Gates

- Migration is transactional and repeatable on legacy and current databases.
- Feedback can be created, read, and updated only for an existing terminal task.
- Aggregate calculations have regression tests for empty, partial-cost, cancelled, failed, and mixed-mode datasets.
- Feedback and metrics endpoints produce zero new `agent_runs`.
- Existing task request and result contracts remain backward compatible.
- API responses contain no unrequested task content or secrets.
- Full pytest, Ruff, Pyright, JavaScript syntax, and migration checks pass.

### Expected Files

- `app/schema_migrations.py`
- `app/protocol/validators.py`
- `app/api/task_feedback.py`
- `app/api/metrics.py`
- `app/main.py`
- `app/dashboard/index.html`
- `app/dashboard/dashboard.js`
- `app/dashboard/dashboard.css`
- `tests/test_task_feedback.py`
- `tests/test_deliberation_metrics.py`
- Protocol, schema, dashboard-help, and README documentation

## Milestone 2 — Make Compute Visible and Bounded

Begin only after Milestone 1 has captured a user-approved minimum sample of rated real tasks.

- Compute a deterministic expected and worst-case agent-run range before submission.
- Add an optional task-level `max_agent_runs` enforced at the adapter boundary, not merely at round boundaries.
- Show the budget before launch and actual consumption afterward.
- Preserve manual `resolve`, `consult`, and `conclave` selection.
- On exhaustion, retain the transcript and return a truthful budget-exhausted result with unresolved issues.

The budget formula must account for participant count, rounds, clarification, focused follow-up, judge, and synthesizer calls. Preset values require review against Milestone 1 evidence before adoption.

## Milestone 3 — Selective Escalation

Adaptive routing remains a separate, ratified change. It may proceed only if feedback shows that review depth predicts additional decision value.

Candidate ladder:

1. Start with one primary response.
2. Add one independent critic for high-impact actions, missing evidence, invalid citations, unresolved status, or an explicit user request.
3. Convene a full conclave only when the critic names a material disagreement that survives primary reconsideration.
4. Invoke an independent judge or synthesizer only when material dissent remains after the bounded participant round.

Routing should initially be deterministic. Do not spend an LLM call deciding whether to spend more LLM calls. Agent self-reported confidence may contribute to a rule but cannot control it alone. High-risk classification should rely on task type, permissions, proposed action types, citation state, explicit structured response fields, and user selection.

Open design decision: whether escalation remains within one task or creates a linked child task. This must be settled before implementation because it affects audit history, cancellation, budgets, exports, and protocol compatibility.

## Validation Loop

Use forthcoming real decisions rather than replaying the entire historical database. Before the pilot, approve a finite sample and compute allowance. For selected cases, preserve the initial strongest-agent answer, run only the authorized review depth, present outputs without provider identity when comparing them, and record the same outcome questions.

Primary measures:

- **Decision value rate:** rated tasks where the decision changed or a missed material risk was found.
- **Worthwhile escalation rate:** escalated tasks rated worth the additional review.
- **Compute per valuable decision:** agent runs divided by tasks with a positive value signal.

Guardrails:

- Completion and failure rates.
- Time to first useful result.
- Feedback coverage and cost/token coverage.
- Frequency of manual reruns at a deeper mode after a shallow result.

The review ends in one named state: **validated** (evidence supports the next milestone), **insufficient evidence** (continue ordinary use without routing changes), **rejected** (extra review does not earn its cost), or **blocked** (measurement or reliability prevents a conclusion).

## Deferred Until Evidence Exists

- Cloud or multi-user architecture
- Team administration, SSO, RBAC, and organizational reporting
- Additional agents or deliberation modes
- An LLM-based routing classifier
- Autonomous execution
- Broad search infrastructure

## Planning Reference

This plan adapts the evidence discipline of [The full product evaluation loop](https://signals.forwardfuture.ai/loop-library/loops/full-product-evaluation-loop/) but adds Conclave-specific inference budgets, value feedback, and escalation gates. The live Loop Library catalog could not be accessed during planning; the reviewed offline catalog was dated 2026-06-19.
