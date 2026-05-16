# Decision Record 0014 — Remove the Ollama Cloud adapter

**Date**: 2026-05-16
**Mode**: Glen-directed
**Keeper**: claude-code
**Supersedes (partially)**: [DR0009](0009_ollama_cloud_council_seats.md). Related: [DR0011](0011_openrouter_council_seats.md), [DR0010](0010_settings_panel_and_db_api_keys.md).

## What Was Chosen

The Ollama Cloud adapter and all of its supporting code, config, settings-panel surface, tests, and documentation are **removed from the codebase**. The OpenRouter adapter (DR0011) is now the sole pluggable open-weight backing for council seats.

DR0009 and DR0011, where Ollama Cloud was introduced and then demoted to "kept but disabled by default," remain in `docs/decisions/` as audit history. DR0009 carries a supersession banner pointing here. The narrative bodies of DR0010 / DR0011 / DR0012 / DR0013 are unchanged — they accurately describe what was true at ratification time and the append-only Charter rule applies.

Concretely, this commit:

- **Deletes** `app/agents/ollama_adapter.py` and `tests/test_ollama_adapter.py`.
- **Removes** `OllamaCloudConfig`, `OllamaCloudModel`, and `Config.ollama_cloud` from `app/config.py`.
- **Removes** `register_ollama_cloud_models()` from `app/services/agent_registry.py` and its call from `app/main.py`.
- **Removes** the `OLLAMA_API_KEY` slot from `app/api/settings.py` (`_API_KEYS` map) and the `OllamaCloudAdapter` branch from `app/api/agents.py` (`/api/pricing` endpoint).
- **Removes** the `ollama_cloud:` section from `config.example.yaml`.
- **Removes** the Ollama Cloud API-key row from the Settings panel (`app/dashboard/index.html`), the `ollama` entry from the `API_KEY_FIELDS` map and one comment in `app/dashboard/dashboard.js`, and two `kind-ollama_cloud` CSS rules from `app/dashboard/dashboard.css`.
- **Rewrites** the Ollama-flavored tests in `tests/test_settings_api.py` to exercise the OpenRouter slot (which is now the only API-key slot the settings store manages).
- **Strips** Ollama references from `README.md`, `CLAUDE.md`, `docs/AGENT_ADAPTERS.md`, `docs/MVP_PLAN.md`, `docs/ROADMAP.md`, `app/dashboard/help.html` (doc version bumped 1.1.1 → 1.1.2), and `skills/generic/role_disambiguation.md`. Leaves `docs/historical/0000_original_plan.txt` alone (it is history by definition).

## Why It Was Chosen

DR0011 already demoted Ollama Cloud to *disabled by default* when OpenRouter became the canonical open-weight backing. Glen has not enabled it since, and there is no plan to. Keeping the adapter installed creates ongoing maintenance surface — a Settings panel row, two CSS pills, a config section, a test file, and scattered prose in user-facing docs — for a code path that is dormant by policy.

The "good reasons to keep dead code" Glen probed for did not survive scrutiny:

1. **Audit history** — preserved by leaving DR0009/DR0011 in `docs/decisions/`. Removing the code does not erase the record.
2. **Template for future adapters** — `OpenRouterAdapter` already serves that role and is the one in active use.
3. **Tests** — `tests/test_ollama_adapter.py` covers code that is being deleted; it goes with the code.
4. **Future local-Ollama adapter** — would be a different adapter (different endpoint, different auth model, different failure modes) and would not reuse this one.

Glen's direction was unambiguous: *"i do not want to use ollama, i am firm on this … there should be no stubs or references to ollama in the app or docs."*

## What Was Rejected

- **Keep the adapter, just hide it from the dashboard.** Considered briefly; rejected. The maintenance surface (config schema, registry function, Settings panel row, two adapter files, prose in five docs) is what Glen is asking to be rid of, not the visual presence. Hiding without removing leaves all of it in place and adds a third state ("present but invisible").
- **Replace the Ollama Cloud row in Settings with a local-Ollama row.** Out of scope. A local-Ollama adapter would be a different decision record (different endpoint, no auth flow, different availability semantics, different failure modes). Tracked as a possible future item, not built here. The pluggable-slot pattern that DR0009 established is preserved — when/if a local-Ollama seat is wanted, the precedent is the OpenRouter adapter, not the deleted Ollama Cloud one.
- **Rewrite DR0009 / DR0011 narrative to remove Ollama mentions.** Rejected — that would violate the Charter's append-only principle for decision records. The supersession banner on DR0009 and this record together are the correct structural way to communicate "this decision is no longer in force." The narrative bodies stay as audit history.

## Operability Impact

(Fifth+ decision under Charter v1.2 §Decision Records.)

- **Observability**: neutral. No telemetry surface changes; the Ollama Cloud branch in `/api/pricing` was never exercised by Glen.
- **Durability**: neutral. No schema change. The `settings` table still stores `openrouter_api_key`; any stray `ollama_api_key` row in an existing DB becomes inert (no reader). Not auto-cleaned because dropping unrecognised settings keys is out of scope here.
- **Recoverability**: positive (small). One less code path means one less failure surface to debug. The "kimi not registered" diagnosis episode from 2026-05-16 turned in part on having to reason about which open-weight registration path was at fault; that ambiguity goes away.
- **Audit trail**: neutral. DR0009 + DR0011 + this record carry the full story; nothing is hidden.
- **Retention/export**: neutral. Existing exported tasks that mention Ollama participants by name remain valid — participant names are plain strings.
- **Complexity**: negative (reduction). ~290 lines of adapter, ~150 lines of test, ~30 lines of config/registry, ~30 lines of frontend, ~15 lines of doc removed net. The Settings panel is one row instead of two; the `_API_KEYS` map is one entry instead of two; the agent registry has one registration call instead of two.
- **Accepted risks**:
  - **A future change of heart on Ollama** means re-implementing the adapter rather than re-enabling a flag. Mitigated by (a) Git history preserves the deleted adapter exactly, and (b) the `OpenRouterAdapter` is a cleaner template anyway.
  - **A user with the now-deleted `ollama_cloud:` section in their local `config.yaml`** will get a Pydantic validation error on startup (extra fields are not allowed by default). Mitigated by `config.yaml` being a user-managed file; the example file in the repo no longer contains the section, and the error is clear ("ollama_cloud" not a known field). Glen's local `config.yaml` was verified to not contain the section at removal time.
- **Exceptions to "Operability before capability"**: **none.** This is a capability *removal*. It pays operability dividends (less code, less surface, less ambiguity) and costs no operability foundation.

## Known Risks

(Covered in Operability Impact. One additional note.)

- This change does not migrate any existing DB-stored `ollama_api_key` row to a different name; it is simply ignored on read. If Glen later wants the DB to be truly Ollama-free, a one-line `settings_store.delete_secret("ollama_api_key")` from a one-shot script or a manual `sqlite3` call is the cleanup. Not done in this commit because (a) Glen does not have one set, and (b) the row is harmless if present.

## Open Questions

- **Local Ollama (no cloud, no API key).** Out of scope here, but the conceptual slot exists: a `LocalOllamaAdapter` that talks to `http://localhost:11434` for users who do want truly local open-weight inference. If/when this is wanted, the precedent is the OpenRouter adapter (HTTP-based, lazy import, one-class-per-config-driven-pattern), not the deleted Ollama Cloud one. No commitment either way.

## Who Is Keeping Continuity

**`claude-code`** as keeper. The supersession is structural:

- This file — `docs/decisions/0014_remove_ollama_cloud_adapter.md`
- [`docs/decisions/0009_ollama_cloud_council_seats.md`](0009_ollama_cloud_council_seats.md) — banner at top points here
- [`docs/decisions/INDEX.md`](INDEX.md) — DR0009 row annotated as superseded; this record listed as the newest entry
