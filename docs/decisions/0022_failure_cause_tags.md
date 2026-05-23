# Decision Record 0022 — Failure-Cause Tags

**Date**: 2026-05-23  
**Status**: Ratified by Glen  
**Mode**: Glen-directed

## What was chosen

Switchboard gains a deterministic, rule-based **failure-cause classifier** that tags every terminal task with zero or more `FailureCause` values, persists them on the `final_results` row, and surfaces them on the API.

A new `FailureCause` enum in `app/protocol/validators.py` defines nine members: `missing_evidence`, `tool_timeout`, `bad_json_output`, `premise_conflict`, `multimodal_perception_split`, `unresolved_dissent`, `repetition_loop_backstop`, `clarification_unanswered`, `permission_denied`. `FinalResult` gains a `failure_cause_tags: list[FailureCause]` field, default `[]`.

`app/services/trace_analyzer.py::classify_failure_causes(...)` inspects the just-completed task — status, agreement level, `errors_json`, per-run error codes, multimodal-disagreement messages, approval denials, repetition-guard signals — and emits the tag list. The orchestrator calls it from a new `_post_finalize_hooks(task_id)` that runs **after** the terminal status flip and **after** `_save_final_result` writes the row. The hook is defensively wrapped: a classifier bug cannot fail a successful task.

Tags persist in an additive `final_results.failure_cause_tags_json` column and are returned from `_row_to_final_result`. Decision Memory's prior-art entries gain a `failure_cause_tags` field (empty for now) so the response shape is forward-compatible with future past-task pairing. `PROTOCOL_VERSION` is bumped 1.1 → 1.2.

Coverage: 23 unit tests in `tests/test_trace_analyzer.py`, 3 round-trip tests in `tests/test_failure_cause_tags_end_to_end.py`, and 3 added tests in `tests/test_decision_memory.py`.

The inspiration is the GEPA pattern in NousResearch/hermes-agent-self-evolution (read execution traces, propose targeted mutations). Ours is rule-based rather than LLM-based, so it costs nothing per task. Architecture not adopted; pattern referenced as inspiration only.

## Why

The conclave generates rich execution traces but, until now, terminal tasks carried no structured "why this ended the way it did" signal. A rule-based classifier turns existing trace material (status, error codes, disagreement messages, backstop firings) into an inspectable handoff — useful immediately for the dashboard inbox, and forward-compatible with Decision Memory pairing once enough tagged history exists.

Rule-based was preferred over LLM-based because the signals we can detect today are already deterministic, and a per-task LLM call would add cost and latency for no marginal precision.

## What was rejected

- **LLM-based failure-cause classification.** Cost per task, latency, and the rule-based version covers what we can detect cleanly today.
- **Boost Decision Memory ranking on shared tags in v1.** Only the response shape was enriched; ranking changes are deferred until we see whether the tag signal actually distinguishes useful retrievals.
- **`missing_evidence` and `premise_conflict` rules in v1.** Hard to detect rule-based without false positives. Both remain as inert enum members for future implementation.

## Known risks

- Rule-based detection has gaps — the two inert members above, and cases where orchestrator signals do not carry through to a recognizable error code.
- Tag taxonomy may evolve. The protocol bump 1.1 → 1.2 already absorbed the schema change so future additions are additive.

## Open questions

- Should Decision Memory's TF-IDF be augmented with tag-similarity once enough tagged history exists to evaluate it?
- Should the dashboard expose a "show only tasks tagged X" inbox filter? (Yes — shipped in the same branch.)

## Who is keeping continuity

Claude Code, as keeper.

## Operability Impact

- **DB**: one additive column on `final_results`, no migration risk.
- **Audit trail**: enriched. Tags are append-only on the final_result row.
- **Recoverability**: unchanged.
- **Retention**: unchanged — Tier 1 protection still keys on decisions and unresolved dissent.
- **Cost**: zero per task (rule-based, no LLM).
- **Risk**: classifier is wrapped in defensive try/except in the post-finalize hook; a buggy rule cannot fail a task.
