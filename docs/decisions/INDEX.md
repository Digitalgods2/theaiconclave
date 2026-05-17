# Decision Records — Index

Every ratified decision lives in this directory. The keeper (`claude-code`) maintains both the individual records and this index. Per the Conclave Charter §Decision Records, *"significant work closes with a record of what was chosen, why, what was rejected, known risks, open questions, and who is keeping continuity."*

## All decisions, newest first

| # | Date | Title | Mode | Outcome |
|---|---|---|---|---|
| **[0015](0015_tool_loop_api_seats.md)** | 2026-05-17 | Tool-loop architecture for the API-based council seats | Glen-directed (draft; ratification pending) | Adds an OpenAI-style tool-call loop (`read_file` + `list_dir`) to `OpenRouterAdapter` so the open-weight seats can ask for specific files on demand instead of getting everything inlined. Per-seat opt-in via `openrouter.models[].tool_loop: false` default. New `tool_call` / `tool_result` message types. Bounds: 8 iterations / 256 KiB cumulative read per turn. Implementation gated on this record being ratified |
| **[0014](0014_remove_ollama_cloud_adapter.md)** | 2026-05-16 | Remove the Ollama Cloud adapter (supersedes DR0009 in part) | Glen-directed | `OllamaCloudAdapter`, `OllamaCloudConfig`/`OllamaCloudModel`, the `OLLAMA_API_KEY` settings slot, the Ollama Cloud Settings panel row, and Ollama mentions across user-facing docs all removed. DR0009/DR0011 narrative preserved as audit history; DR0009 carries a supersession banner. OpenRouter is now the sole pluggable open-weight backing |
| **[0013](0013_prefetched_url_attachments.md)** | 2026-05-16 | Pre-fetched URL attachments — bounded live-web access for the conclave (shared-snapshot shape, not per-agent web tools) | Glen-directed (spec; ratification pending) | New `Context.urls[]` field; `app/services/url_fetcher.py` fetches each URL once, server-side, before dispatch; all participants see the same inlined bytes. Per-agent independent web tool access rejected for v1; may be revisited |
| **[0012](0012_inline_sandbox_for_api_adapters.md)** | 2026-05-12 | Inline the project sandbox for the API-based council seats | Glen-directed | OpenRouter / Ollama seats now get a read-only file tree + file contents inlined into their prompt when a task has a sandbox (they can't browse files). `app/utils/sandbox_inline.py` |
| **[0011](0011_openrouter_council_seats.md)** | 2026-05-12 | OpenRouter council seats (pay-per-token open-weight models) | Glen-directed | `deepseek` / `glm` / `qwen` seats now OpenRouter-backed (no subscription); Ollama Cloud adapter kept but disabled by default; `OPENROUTER_API_KEY` added to the Settings panel |
| **[0010](0010_settings_panel_and_db_api_keys.md)** | 2026-05-12 | Left-rail Settings panel + DB-stored API keys (env-fallback) | Glen-directed | Dashboard left rail → gear → Settings → API Keys; Ollama key stored in DB with eyeball reveal; rule: env var wins, else DB |
| **[0009](0009_ollama_cloud_council_seats.md)** | 2026-05-12 | Ollama Cloud council seats (open-weight models: deepseek / glm / qwen) — **superseded by DR0014 (2026-05-16)** | Glen-directed | New `OllamaCloudAdapter` (pluggable, config-driven); seats appear in the dashboard checkbox list; auth via `OLLAMA_API_KEY`. Established the pluggable-seat pattern. Adapter and config removed in DR0014; OpenRouter (DR0011) is now the sole open-weight backing |
| **[0008](0008_export_detail_pdf_docx_text.md)** | 2026-05-12 | Export task detail as PDF / DOCX / Markdown / Text from the dashboard | Glen-directed | New `GET /api/tasks/{id}/download?format=...` endpoint + dashboard control; browser Save dialog for destination; `reportlab` + `python-docx` added |
| **[0007](0007_codex_gemini_slash_command_parity.md)** | 2026-05-11 | Codex + Gemini slash-command parity + provenance tracking | Glen-directed | 8 slash commands now invokable from Codex and Gemini sessions; `source_agent` round-trips through API; first capability decision under Charter v1.2 |
| **[0006](0006_charter_v1_2_operability_before_capability.md)** | 2026-05-11 | Charter v1.2: *Operability before capability* principle + Decision Records *Operability Impact* field | conclave 3-AI + Glen ratified | Charter amended; new principle binding on every future capability/infrastructure deliberation |
| **[0005](0005_db_concurrency_and_tier2_archive.md)** | 2026-05-11 | DB concurrency hardening (busy_timeout + with_retry) + Tier 2 export/archive tracking | Glen-directed | `busy_timeout=30s` + retry wrapper; `exported_at` columns + bulk export endpoint + dashboard surface |
| **[0004](0004_sandbox_not_layer2.md)** | 2026-05-11 | Project Sandbox shipped; Layer 2 (in-conclave write/execute) deferred | Glen ↔ Claude (keeper) | Sandbox shipped; Layer 2 explicitly not built |
| **[0003](0003_retention_policy.md)** | 2026-05-11 | DB retention policy: two-part metric (operational trigger + tier-based selection) | conclave 3-AI + Glen ratified | Implemented in `app/services/retention.py`; 2 GB / 1,000 task budget |
| **[0002](0002_multimodal_disagreement_policy.md)** | 2026-05-11 | Charter v1.1: Multimodal Disagreement section (do not synthesize visual perception disputes) | conclave 3-AI + Glen ratified | Charter amended; binding on every prompt |
| **[0001](0001_charter_adoption.md)** | 2026-05-10 | Initial adoption of Conclave Charter v1.0 | conclave 3-AI + Glen ratified | Charter embedded in every prompt via `skills/generic/conclave_charter.md` |

## How to read this index

Each row shows the decision's date, title, the deliberation mode that produced it, and the operational outcome. Click into any record for the full structured record with rejected alternatives, known risks, and open questions.

## How a new decision lands here

Per the Charter's amendment process:

1. **Propose**: a conclave-mode task on Switchboard deliberates the proposed change
2. **Ratify**: Glen records a `/decide` decision on the task
3. **File**: the keeper writes `docs/decisions/<NNNN>_<slug>.md` with the structured record
4. **Index**: the keeper appends a row to this file
5. **Cross-link**: relevant docs that change as a result of the decision get updated and reference the decision number

Decisions are append-only. Earlier decisions can be superseded by later ones (e.g., a v1.2 amendment to the Charter would supersede the v1.1 section but the v1.1 record stays in place as audit history).

## The conclave's own dogfooding

Four of these decision records came out of the conclave deliberating about its own design (charter adoption, multimodal disagreement, retention, charter v1.2). The rest (sandbox, db concurrency + Tier 2 export, slash-command parity, detail export, Ollama Cloud seats, settings panel, OpenRouter seats, inline-sandbox for API seats, the DR0013 pre-fetched-URL spec, and the DR0014 Ollama Cloud removal) were Glen-directed. The Switchboard product is, in part, a tool for designing itself. This index is part of the audit trail of that process.
