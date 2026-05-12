# Decision Record 0006 — Charter v1.2: Operability before capability

**Date**: 2026-05-11
**Mode**: conclave (3 participants: codex, gemini, claude-code) + Glen ratification
**Task**: `tsk_01KRC85N3VN6810GD9KDJBADEJ`
**Keeper**: claude-code

## What Was Chosen

The Conclave Charter is amended from v1.1 to **v1.2** with two changes:

### A. New principle: "Operability before capability"

A new section is added to the charter (placed between *Minor Difference Resolution* / *Multimodal Disagreement* and *User Escalation*) reading:

> When deliberating whether to add a new feature, mode, agent type, permission layer, or heavy infrastructure to Switchboard, the conclave must first evaluate its impact on the operability and trust foundations of the existing system: **observability, durability, recoverability, audit trail, retention, and export.**
>
> A **material tension** exists when the proposal would:
> - degrade those foundations,
> - create direct architectural conflict with them,
> - increase state/permission/coordination complexity beyond what they can support, or
> - displace a named operability gap in the same bounded priority decision where that gap materially affects trust, recovery, auditability, or retained/exportable evidence.
>
> In material tension, unresolved conflict is resolved in favor of operability **unless Glen explicitly approves a bounded exception**. Capability changes that directly strengthen operability may proceed under this principle, but their claimed operational benefit must be stated and verifiable.

### B. Decision Records section amended

The existing *Decision Records* section gains a new requirement: for significant Switchboard capability or infrastructure decisions, the record must additionally include an **Operability Impact** field documenting effects on observability, durability, recoverability, audit trail, retention/export, complexity, accepted risks, mitigations, exceptions to the principle (if Glen approved one), and follow-up review points.

### C. Multimodal Disagreement section unchanged

The *Multimodal Disagreement* section (v1.1, decision 0002) is not modified. The new principle reinforces it indirectly by prioritizing audit trail and retained evidence, but does not alter its rules.

## Why It Was Chosen

The conclave that produced retention (0003), sandbox (0004), and concurrency + Tier 2 export (0005) twice noted in its meta-recommendations that Switchboard's *operability* foundations — observability, retention, export, durability — should be solidified before more *capability* (new modes, agents, permission layers) is added. The principle had been operating informally in Glen's prioritization without being recorded.

Ratifying it explicitly:

- Makes the rule available to future deliberations as binding rather than implicit, so the same argument doesn't have to be re-litigated every time someone proposes a new agent type or mode.
- Gives the conclave a concrete tension test rather than a vibe.
- Forces capability proposals to surface their operability impact in their own Decision Record.
- Preserves Glen's final authority via the bounded-exception clause: this is not a veto on capability work, only a default ordering.

## What Was Rejected

- **Narrower "direct degradation or architectural conflict only" definition** (Gemini and Claude Code's original framing). Rejected in favor of Codex's four-part *material tension* definition after Claude Code reversed in the judge round. Reason: the narrower form would have allowed evasion ("this isn't degradation, it's just timing/competition"), undermining the principle.
- **Absolute veto on capability work**. Considered as a strong reading of the original draft text; rejected unanimously. Operability must win in *unresolved material tension*, not block all capability work.
- **Modifying the Multimodal Disagreement section** to reference the new principle. Rejected as unnecessary — the two principles are independently load-bearing.
- **Skipping the Decision Records amendment** and only adding the new section. Rejected by all three participants because without the Operability Impact field the principle has no enforcement teeth in the record.
- **Keeper unilateral redraft of the conclave's converged wording before recording**. The keeper (claude-code) offered to redraft for Glen's edit preferences before `/decide`; Glen correctly identified this as a single-agent bypass of the deliberation and rejected the offer. Decision recorded with the conclave's verbatim wording.

## Operability Impact

(First decision to use the new field. Self-applying.)

- **Observability**: no impact — adds a record-keeping requirement, not new state.
- **Durability**: no impact.
- **Recoverability**: no impact.
- **Audit trail**: strengthens it. Future capability decisions must now state their Operability Impact in the Decision Record, so the audit trail captures the trade reasoning explicitly.
- **Retention/export**: no impact.
- **Complexity**: minimal — adds one section to the charter file and one optional field to the Decision Record template.
- **Accepted risks**: see *Known Risks* below.
- **Exceptions approved**: none.
- **Follow-up review point**: re-evaluate after the next 3 substantive Switchboard feature decisions whether the principle is being applied evenly or whether it is producing the stagnation Gemini warned about.

## Known Risks

- **Stagnation risk** (raised by Gemini in round 1, addressed by the bounded-priority-window clause): if "tension" is read too broadly, capability work could be permanently blocked. Mitigation: the four-part definition narrows tension to material cases; Glen's bounded-exception authority is explicit.
- **Definitional drift**: "named operability gap" requires that operability gaps actually get named (in the roadmap, in decision records). If they aren't named, the fourth tension condition becomes a vague trump card. Mitigation: the ROADMAP "Next" section already names operability gaps explicitly; keeper should keep doing so.
- **Coupling judgment**: distinguishing a capability that "directly strengthens operability" (allowed to proceed) from one that merely *claims* to (subject to the principle) requires judgment. The "stated and verifiable" clause is the safeguard, but it depends on enforcement during deliberation.
- **The principle is not retroactive**: it does not invalidate prior capability decisions (e.g., conclave mode, threading, multimodal). Those stand under v1.0/v1.1 governance.

## Open Questions

- **What counts as "significant" for the Decision Record Operability Impact requirement?** The charter says "significant Switchboard capability or infrastructure decisions" — same word that gates the existing decision record requirement. Currently a judgment call. Could be tightened later if it becomes a gray area.
- **Should the principle apply to changes outside Switchboard the product** (e.g., new skills, dashboard polish)? Charter scope says Switchboard specifically. Adjacent tooling decisions are not bound.
- **Re-review cadence**: when the follow-up review (above) fires, who triggers it? The keeper's responsibility, but the trigger should be tracked.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Changes applied:

- `skills/generic/conclave_charter.md` — version bumped to v1.2; new *Operability before capability* section added; *Decision Records* section amended to require *Operability Impact* field
- `docs/decisions/0006_charter_v1_2_operability_before_capability.md` — this file
- `docs/decisions/INDEX.md` — 0006 row added
- `docs/ROADMAP.md` — Charter v1.2 entry moved from Next to Shipped

The charter file is embedded in every prompt to every participant, so v1.2 takes effect on the next conclave/resolve/consult task.
