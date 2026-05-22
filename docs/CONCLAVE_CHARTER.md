# Conclave Charter

The canonical, prompt-embedded charter is at **`skills/generic/conclave_charter.md`** — that file is the single source of truth and is loaded into every prompt sent to every participant by the orchestrator's prompt builder.

This doc exists so the charter is discoverable from the docs/ tree alongside the other design documents. To read or amend the charter, edit `skills/generic/conclave_charter.md` — changes there take effect on the next service restart.

## How the charter is enforced

Every AI Conclave Switchboard prompt to every agent (in every mode — resolve, consult, conclave, peer, final) is assembled by `app/services/prompt_builder.py` in this order:

1. **Conclave Charter** (loaded from `skills/generic/conclave_charter.md`)
2. **Role skill** (resolution / consultant / conclave / primary behavior)
3. **Safety skill**
4. **Task framing** (user request, mode, permissions)
5. **Prior messages** (transcript so far)
6. **Output schema demand**

The charter is the **constitutional layer**: it applies always, regardless of mode or role. Role-specific skills tell the agent what *kind* of contribution to make; the charter tells the agent how to *behave* across all contributions.

## Amendments

Per the charter's own evolution rule:

1. Any participant or Glen proposes an amendment (via a conclave-mode task on the AI Conclave Switchboard).
2. The amendment is debated.
3. Glen ratifies or rejects.
4. The keeper (`claude-code`) bumps the version number and updates `skills/generic/conclave_charter.md`.
5. A decision record is filed at `docs/decisions/<NNNN>_<slug>.md`.

The current version is **v1.3**, ratified 2026-05-21 (originally v1.0 on 2026-05-10). Decision records:

- `docs/decisions/0001_charter_adoption.md` — initial adoption (v1.0)
- `docs/decisions/0002_multimodal_disagreement_policy.md` — Multimodal Disagreement section added (v1.1)
- `docs/decisions/0006_charter_v1_2_operability_before_capability.md` — Operability before capability principle and Decision Records Operability Impact field added (v1.2)
- `docs/decisions/0021_evidence_norms_charter_v1_3.md` — Evidence Norms for load-bearing factual claims added (v1.3)
