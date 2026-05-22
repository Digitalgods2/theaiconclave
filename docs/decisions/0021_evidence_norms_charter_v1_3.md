# Decision Record 0021 — Charter v1.3: Evidence Norms

**Date**: 2026-05-21  
**Status**: Ratified by Glen  
**Mode**: conclave 3-AI + Glen ratified  
**Source task**: `tsk_01KS60637QSAK97T0HAF8GFR98`

## What was chosen

Amend the Conclave Charter from v1.2 to **v1.3** by adding an **Evidence Norms** section after Reasoning Norms.

Participants must cite or identify the basis for load-bearing factual claims. A load-bearing claim is a factual assertion that materially affects a recommendation, risk assessment, rejection, convergence signal, or decision record.

Valid evidence is constrained to material already available to the task: explicit user statements, uploaded attachments, sandbox files, command results, prior transcript messages, prior decision records, protocol/context metadata, URL snapshots, and Switchboard-managed artifacts. The amendment grants no new permissions and does not authorize new reads, network access, commands, writes, or external system changes.

Unsupported but useful claims must be labeled as assumptions, interpretations, taste judgments, speculation, or recommendations. Final synthesis and decision records should preserve material evidence gaps and unresolved factual uncertainty.

## Why

The conclave converged that Switchboard's deliberations are more useful when the factual basis for recommendations is visible. The system already preserves transcripts and decision records; v1.3 makes the reasoning trail more inspectable by requiring participants to distinguish evidence-backed factual claims from assumptions or judgment calls.

This improves auditability without changing the execution boundary. The amendment is a norm for deliberation quality, not a new tool, permission, or enforcement layer.

## What was rejected

- **Treat evidence citation as a firewall.** Rejected because v1.3 is advisory and deliberative. It does not block outputs, execute checks, or expand agent authority.
- **Require evidence for every sentence.** Rejected because it would create noisy, performative citations. The requirement applies to load-bearing factual claims.
- **Allow participants to fetch new evidence automatically.** Rejected for this amendment. Evidence norms operate only over task-available material unless separate permissions and product work are approved.
- **Implement protocol-level `evidence[]` immediately as part of the charter amendment.** Deferred. The conclave recommended an optional `evidence` array on `ConclaveTurn`, but that is a wire-contract implementation step and should be handled separately.

## Known risks

- Participants may under-identify which claims are load-bearing.
- Evidence references may be uneven until prompts and protocol affordances are improved.
- Without a structured protocol field, evidence may remain embedded in prose and be harder to render or export consistently.

## Open questions

- Should `ConclaveTurn` gain an optional structured `evidence` array with fields such as `source_type`, `source_ref`, `claim`, `relevance`, `locator`, `excerpt`, `confidence`, and `notes`?
- Should the dashboard render evidence citations as first-class transcript metadata once the protocol supports them?
- Should final synthesis receive a stricter instruction to preserve evidence gaps in every completed task, or only for high-stakes and decision-record tasks?

## Who is keeping continuity

Claude Code, as charter keeper, maintains the canonical prompt-embedded charter and this decision record. Future protocol/schema work should reference this decision before adding structured evidence fields.

## Operability Impact

Positive for audit trail and decision memory: factual bases and evidence gaps should be easier to inspect later. No database, API, dashboard, worker, retention, export, permission, or recovery behavior changes are made by this charter-only amendment.

Complexity added is limited to prompt-bound norms and documentation. The deferred protocol-level evidence array would require a separate operability review before implementation.
