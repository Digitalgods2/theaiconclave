# Decision Record 0003 — DB Retention Policy

**Date**: 2026-05-11
**Mode**: conclave (codex + gemini + claude-code) → Glen ratified
**Convergence**: weak by `_normalize` text-match (substantively unanimous after the synthesis round)
**Keeper**: claude-code

## What Was Chosen

Adopt a **two-part retention metric** for the Switchboard SQLite database:

1. **Operational trigger** — when cleanup runs
   - SQLite file + WAL exceed **2 GB** (configurable), **OR**
   - Completed-task count exceeds **1,000** (configurable)
2. **Semantic selection** — what gets trimmed
   - **Tier 1 (never auto-trim)**: the task row itself (carries `user_decision`, `parent_task_id` linkage), tasks with unresolved dissent (`agreement_level ∈ {major_disagreement, unresolved}` AND no `user_decision`)
   - **Tier 2 (retain indefinitely until export)**: `final_results` rows
   - **Tier 3 (trim first)**: `agent_messages` rows for tasks that are terminal, summarized (have a `final_results`), older than 90 days, not referenced by any other task's `parent_task_id`, and either `consensus` or have a `user_decision`

Implementation: `app/services/retention.py`, with a `retention_loop` running on FastAPI lifespan and re-running every 6 hours. VACUUM after a successful trim.

## Why It Was Chosen

The conclave deliberated over 4 rounds. Initial positions diverged genuinely:

- **Claude** opened with *age as primary metric*, size as safeguard.
- **Codex** rejected age-as-primary, advocated *future retrieval value* as the deciding metric: "DB size, age, and task count should not decide the value of a row. They decide when maintenance runs."
- **Gemini** proposed *rolling completed-task count*, citing bursty AI workloads.

By round 3 all three independently arrived at the same insight — **the trigger and the selection metric are different things**. The synthesis round produced the unified policy above. The key load-bearing reasons:

- **A blunt metric (age, size, or count) cannot decide what matters** because the database holds two kinds of content: durable institutional memory (ratified decisions, charter amendments, unresolved questions) and raw transcript exhaust (per-agent turns). Treating all rows like equal log lines would damage continuity.
- **AI workloads are bursty** — a time-based metric (Claude's initial position) can be violated catastrophically by a single intensive week of work. A size or task-count budget directly bounds operational cost.
- **The Conclave Charter explicitly protects decision records and unresolved dissent.** The selection rule must respect this — Tier 1 protection is the operational expression of the Charter's §Decision Records and §Dissent Norms sections.
- **Old summarized transcripts are exhaust.** Once a task has a `final_results` row, the raw per-agent turns are reconstructable in spirit from the summary. The transcript's value decays sharply once the conclave has concluded.

## What Was Rejected

- **Age as the primary deciding metric.** Rejected because some 2-year-old ratified decisions are more important than yesterday's duplicated raw turns. Age survives only as a tie-breaker *within* Tier 3 (oldest-eligible-first).
- **Bytes-only or rows-only metric.** Rejected as semantically blunt: it tells you when, not what.
- **Codex's `keep_score` weighted formula.** Rejected by Gemini for operational complexity in SQLite; the synthesis used structural tiers instead, which capture the same intent without the join/scoring overhead.
- **Single trigger.** Both DB-size and task-count triggers are kept — they each catch a different failure mode (slow steady growth vs. burst).
- **No retention at all.** The original design didn't auto-delete anything. Rejected because unbounded growth is a real cost over months/years.

## Known Risks

- **The 2 GB / 1,000 task / 90 day defaults are operational guesses.** The size cap was raised from an initial 500 MB on Glen's call after recognizing real conclave usage produces a non-trivial transcript per task. May still turn out to be too aggressive (trimming history users want) or too lax (DB still grows past 2 GB). Mitigation: all thresholds are config knobs in `config.yaml > retention.*` and the worker logs every trim event.
- **VACUUM is heavy.** On a large DB, VACUUM can take seconds and rewrite the entire file. Running it after every trim is fine at MVP scale; revisit if VACUUM dominates wall time.
- **Tier 3 trimming is irreversible.** Once raw agent_messages are deleted, they're gone unless the user restores from backup. Mitigation: the trim is conservative (90 day age + summarized + unreferenced + resolved), and the task row + final_results survive. The audit trail of *what was decided* persists; only the raw deliberation is lost.
- **The unresolved-dissent check is binary.** A task with `agreement_level: major_disagreement` is protected forever unless Glen records a decision. If Glen never records and the task is genuinely abandoned, it stays as a "hanging" question. This is by design — surface unfinished business rather than burying it — but could become noise at scale.
- **The retention worker runs every 6 hours.** If the DB grows past budget within that window, the system tolerates the overage briefly. Not a real concern at MVP scale; revisit if rapid bursts make this matter.

## Open Questions

- **Should we add an export step before trimming?** E.g., dump trimmed transcripts to `data/archive/<task_id>.jsonl` before deleting them, giving the user a recovery path. Deferred — adds complexity, not required by the charter, and the user can always backup the DB.
- **Should Tier 2 (final_results) ever be trimmable?** Currently no — they're tiny and they carry the verdict. At very large scale (millions of tasks) this assumption may need revisiting.
- **Should the retention worker emit an event/notification when a trim happens?** Currently logs only. Dashboard could show a "last retention pass: N rows trimmed" status. Deferred until users start asking.
- **What about non-task content** (logs, settings, approvals)? Decision 0003 covers only the `tasks` / `final_results` / `agent_messages` chain. The `logs` and `approvals` tables grow on their own schedule; separate retention is a future decision.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Responsibilities for this decision:

- Canonical implementation at `app/services/retention.py`.
- Defaults documented at `config.example.yaml > retention.*`.
- Tier definitions canonical here (this file) and in the module's docstring.
- Future amendments via conclave + Glen ratification per the Charter.
