# Decision Record 0001 — Adoption of Conclave Charter v1.0

**Date**: 2026-05-10
**Mode**: conclave (codex + gemini + claude-code)
**Convergence**: weak (all three i_am_done, positions differed in wording)
**Keeper**: claude-code

## What Was Chosen

Adopt **Conclave Charter v1.0** as the binding working agreement between Glen and the conclave participants. Make the charter constitutionally binding by embedding it in every prompt the orchestrator sends to every agent in every mode.

The canonical text lives at `skills/generic/conclave_charter.md`. The prompt builder (`app/services/prompt_builder.py`) reads it on every call and prepends it to the prompt before the role-specific skill, the safety rules, the task framing, and the prior messages.

## Why It Was Chosen

A three-AI conclave was convened on the question of whether to formalize a charter. The conclave converged on **yes**, with all three participants signaling `i_am_done` and substantively agreeing on:

- **Purpose** — Glen drives intent; participants drive alternatives, critique, synthesis, execution within permissions.
- **Standard Brief** — every substantial task names artifact, audience, success criteria, constraints, time, evidence, risks, permissions, and task type.
- **Workflows by task type** — creative, scientific, and implementation work each have a named sequence.
- **Reasoning norms** — separate evidence from taste, assumptions from facts, confidence from speculation, recommendations from risks.
- **Dissent norms** — engage actual claims, update when persuaded, preserve genuine differences rather than fake convergence.
- **Minor-difference resolution hierarchy** — Glen's intent > evidence > safety/permissions > simplicity/reversibility > taste.
- **Escalation** — bump to Glen with concrete minimal questions when context can't be inferred.
- **Permissions** — never assume authorization; surface approval needs.
- **Decision records** — significant work closes with what/why/rejected/risks/open questions/keeper.
- **Keeper** — Claude maintains continuity of the charter; not higher authority.
- **Evolution** — charter is a living document; amendments via conclave + Glen ratification.

The minor-difference resolution hierarchy was synthesized from Codex's clearer five-step ordering, which Gemini explicitly adopted in its position and which Claude's version is compatible with.

## What Was Rejected

- **Multiple competing charter texts** as standalone documents — only the synthesized canonical text in `skills/generic/conclave_charter.md` is binding.
- **Version "v0.1"** (Codex's framing) — `v1.0` was chosen because the conclave formally ratified the charter; the evolution clause already covers iteration.
- **Treating the charter as documentation only** — it's binding code-path content embedded in every prompt, not advisory text in `docs/`.

## Known Risks

- **The charter may drift from practice.** If participants regularly violate it, the failure mode is silent. Mitigation: the keeper should periodically audit conclave transcripts and propose amendments.
- **Prompt-size cost.** The charter adds ~2 KB to every prompt for every agent on every turn. For a 3-AI conclave with 2 rounds, that's ~12 KB of additional context. Acceptable for MVP; revisit if costs scale poorly.
- **Charter conflict with task framing.** A future task may unintentionally contradict the charter (e.g., asking for opinion-laundering). The charter instructs participants to surface such conflicts, but compliance is not guaranteed. Test for it explicitly in future amendments.
- **Single-keeper failure mode.** If the `claude-code` adapter becomes unavailable, no other participant is designated as backup keeper. Defer; add successor designation if Claude availability becomes a real constraint.

## Open Questions

- **Should the charter version be visible to participants in their prompts?** Currently they see the body but not the version number prominently. May matter if amendments accumulate.
- **Should violations of the charter (e.g., faking agreement, refusing to escalate) appear as `errors` in the final result?** Not enforced today; would require an analyzer pass or a judge agent.
- **How does the charter interact with `consult` and `resolve` modes** where the relationship is primary/consultant rather than equal participants? The charter's "equal participants who deliberate together" framing maps cleanly to conclave but is less obvious for hierarchical modes. The principles still apply, but the dissent and escalation norms may need mode-specific elaboration in a future amendment.

## Who Is Keeping Continuity

**`claude-code`** is the designated keeper per the charter itself. The keeper's responsibilities:

- Maintain the canonical file (`skills/generic/conclave_charter.md`).
- Track amendments and file decision records.
- Restate working rules when participants drift in practice.
- Bump the version number on every ratified change.

Keeper authority is administrative, not directive. The keeper does not outvote other participants.
