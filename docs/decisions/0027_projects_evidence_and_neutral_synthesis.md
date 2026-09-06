# Decision Record 0027 — Projects, frozen evidence, and neutral synthesis

**Date**: 2026-09-01  
**Mode**: Glen-directed implementation after repository and competitive audit  
**Keeper**: Codex

## What Was Chosen

The AI Conclave remains a governed deliberation system rather than becoming a broad search or browser-automation product. It gains three connected capabilities:

1. Persistent Decision Projects group tasks and carry user-authored instructions and reusable evidence.
2. A provider-neutral evidence layer captures user-named public URLs once, persists the exact extracted text and SHA-256 hash, and presents identical source bytes to every participant.
3. Optional judge and synthesis seats must be independent from the participant set. The final synthesizer is explicitly required to preserve material dissent and cite only captured evidence IDs.

No search vendor is bundled. Operators may configure a generic JSON search endpoint that returns URLs, or use URL capture directly.

## Safety and Evidence Controls

- HTTPS is required by default. DNS results are checked for public addresses, every redirect is revalidated, credentials in URLs are rejected, and byte/time/redirect limits are enforced.
- Remote content is labelled untrusted and isolated from prompt instructions. Prompt-injection-like phrases are counted as quality signals.
- Citation records include the canonical URL, retrieval time, content hash, publisher, and quality metadata. Invalid model-supplied citation IDs are exposed rather than silently accepted.
- The application still does not grant agents general browser access or mid-deliberation network tools.

## Operability Impact

- Numbered transactional migrations replace the unversioned additive-column block.
- `schema_migrations`, `decision_projects`, and `evidence_snapshots` make state inspectable and exportable.
- CI now runs compilation, critical Ruff checks, incremental Pyright analysis, JavaScript syntax checks, dead-code auditing, and the full test suite against pinned direct dependencies.
- Evidence acquisition failures occur before deliberation and do not create divergent per-agent source state.

## Rejected

- Integrating Perplexity or any other fixed research vendor.
- Letting each participant independently search the web during a round.
- Reusing a participant as its own convergence judge.
- Treating generated artifacts as safe to overwrite without a hash precondition and backup.
