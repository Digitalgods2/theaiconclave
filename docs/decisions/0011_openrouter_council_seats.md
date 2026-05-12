# Decision Record 0011 — OpenRouter council seats (pay-per-token open-weight models)

**Date**: 2026-05-12
**Mode**: Glen-directed (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

The three open-weight council seats — `deepseek`, `glm`, `qwen` — are now backed by **OpenRouter** (a pay-per-token OpenAI-compatible gateway) instead of Ollama Cloud. The Ollama Cloud adapter stays in the codebase but is **disabled by default**.

Why the switch: Ollama Cloud gates the frontier-class open-weight models (DeepSeek 671B, GLM-5, …) behind a **paid subscription** — that was the HTTP 403 (`"this model requires a subscription, upgrade for access"`) on task `tsk_01KRF44FFRKCJZJ19A1P7CAYHR`, where only the free-tier `qwen3-coder:480b-cloud` responded. Glen said no to another subscription. OpenRouter carries the same models **pay-per-token, no subscription**, at trivial cost (~$0.001–$0.02 per conclave turn), with `:free` rate-limited variants available too. The role of these seats is unchanged: a different pair of eyes trained on non-frontier (well, somewhat) data — perspective diversity, not raw capability.

Concretely:

- **New adapter** `app/agents/openrouter_adapter.py` — `OpenRouterAdapter(name, model_slug, max_context_chars, endpoint, data_collection)`. OpenAI-compatible: `POST {endpoint}/chat/completions` with `Authorization: Bearer …`, `response_format: {type: "json_object"}`, `provider: {data_collection: "deny"}`, an `X-Title` header for attribution. Parses `choices[0].message.content`, strips `<think>…</think>`, stashes `usage.prompt_tokens` / `usage.completion_tokens` (and `usage.cost` as `cost_usd` when OpenRouter returns it). Handles the nested-error-in-200 case; `_http_error_message` turns 401/402/404/429 into actionable text ("out of credits — top up", "model not found — check the slug", etc.). Near-clone of the Ollama adapter — the project's one-file-per-adapter style; some shared helper duplication (`_parse_and_coerce`, `_THINK_BLOCK_RE`) is consistent with how `claude_adapter.py` and `ollama_adapter.py` already are.
- **Config** — `OpenRouterModel` / `OpenRouterConfig` in `app/config.py`; an `openrouter:` section in `config.example.yaml` (`enabled: true` by default) with the three default seats:
  - `deepseek` → `deepseek/deepseek-chat` (DeepSeek V3.x — strongest open reasoner; verify slug)
  - `glm` → `z-ai/glm-4.6` (Z.ai GLM — reasoning + agentic; verify slug)
  - `qwen` → `qwen/qwen3-coder` (Alibaba Qwen3 — best JSON discipline of the bunch; verify slug)
  Plus `data_collection: deny` (sent on every request — keeps prompts off providers that retain/train on them; appropriate for code review; set `allow` to opt back in).
- **Ollama Cloud** — `OllamaCloudConfig.enabled` default flipped to `false`; the `config.example.yaml` section reduced to a single `ollama-qwen` example seat (the one that works on the free tier) with a comment explaining the subscription situation and that names must not collide with the OpenRouter ones if re-enabled.
- **Registry** — `agent_registry.register_openrouter_models(config)`, called from `main.py` after `register_ollama_cloud_models(config)`. Same lazy-import pattern; not part of `init_registry()` so tests don't pull in network-backed adapters.
- **Auth** — `OPENROUTER_API_KEY` env var, else the database-stored `openrouter_api_key` (via the Settings panel) — the env-over-DB rule from decision 0010, now applied to a second key. Added to the settings API's `_API_KEYS` map.
- **Dashboard** — the Settings → API Keys section gains an **OpenRouter** key field (input + eyeball + Save + Clear), above the (now secondary) Ollama Cloud one. The settings JS was refactored from Ollama-specific functions to a `name`-parameterized form (`API_KEY_FIELDS` map + `onSaveKey(name)` / `onClearKey(name)` / `onToggleKeyVisibility(name)`), addressing decision 0010's own follow-up note ("factor the eyeball/save/clear JS into a reusable component when a second key is added").
- **Polish** — the Ollama adapter now detects the 403-subscription body and surfaces *"requires an Ollama Cloud paid plan … Tip: OpenRouter carries the same models pay-per-token"* instead of the raw `HTTP 403`.
- **Tests** — `tests/test_openrouter_adapter.py` (18 tests, all HTTP mocked: content/usage/cost extraction, `<think>`-stripping, error/timeout/nested-error/empty-content paths, `_http_error_message` cases, API-key gating, `run_conclave_turn`, `register_openrouter_models`); plus additions to `tests/test_settings_api.py` for the `openrouter` key slot, the OpenRouter adapter's env→DB fallback, and the Ollama 403-subscription message. 170 tests total, all pass.

## Why It Was Chosen

OpenRouter fits Glen's three constraints exactly: (1) **no subscription** — pay-per-token, and pennies at that; (2) **the actual top-tier Chinese models** — DeepSeek, GLM (Z.ai), Qwen, Kimi, MiniMax are all live, unlike Ollama Cloud's free tier; (3) **on-demand use** — you only pay for the turns you run, and a 3-AI-round conclave with all three OpenRouter seats costs a few cents. The OpenAI-compatible API made the adapter a near-clone of the Ollama one (~30 min of work). Keeping the Ollama adapter (disabled) preserves the option for anyone who has the subscription.

## What Was Rejected

- **Replacing Ollama Cloud entirely.** Kept it (config-disabled) — it's working code, and the free Qwen seat there is a viable redundant path. Re-enabling requires renaming the seats to avoid collision with the OpenRouter ones; documented in the config comment.
- **Defaulting the seats to OpenRouter's `:free` variants.** Considered (truly $0) but rejected — the free variants are heavily rate-limited (~20/min, ~200/day) and get deprecated/swapped frequently. The paid slugs are stable and cost ~nothing for the described usage. (A user who wants zero cost can point a seat at a `:free` slug.)
- **A shared `HttpChatAdapter` base class** for the Ollama + OpenRouter adapters. Rejected — the differences (endpoint, key names, `format` vs `response_format`, `message.content` vs `choices[0].message.content`, usage field names) make it a leaky abstraction, and the codebase's convention is one-file-per-adapter with only `BaseAdapter` shared. Per "don't introduce abstractions beyond what the task requires."
- **A cost-cap backstop.** Still out of scope per Glen — but now *two* metered seats exist (OpenRouter is metered; Ollama Cloud would be too if enabled). The existing `max_rounds` / `max_seconds` backstops bound the loop. The `limits.max_total_tokens` cost-cap remains the obvious companion if usage ever grows; tracked, not built.

## Operability Impact

(Sixth decision under Charter v1.2 §Decision Records.)

- **Observability**: neutral-to-positive — OpenRouter returns per-call `cost` in `usage`, which the adapter stashes as `cost_usd` on `agent_runs`, so the existing cost-tracking surface now actually has data for these seats (the CLI subscription adapters and Ollama Cloud don't report cost).
- **Durability**: neutral — no schema change, no new persistent state (the `openrouter_api_key` lives in the same `settings` table as the Ollama one).
- **Recoverability**: neutral-to-positive — failure modes (no credits, bad slug, rate limit, no key) now surface as clear `agent_error` messages with the response body in details, and the Ollama 403-subscription case is no longer a cryptic `HTTP 403`.
- **Audit trail**: neutral. Participant names (`deepseek` etc.) flow through threading / `/decide` / `/continue` / exports as plain strings.
- **Retention/export**: neutral.
- **Complexity**: low-moderate. One new adapter module (~290 lines, mirroring the Ollama one), config additions, one registry function, one line in `main.py`, the dashboard JS refactor (net simpler — one parameterized path instead of Ollama-specific functions), the new HTML field, and the Ollama 403 polish. New dependency: none (`httpx` already required). No new processes, no new persistence layer.
- **Accepted risks**:
  - **New metered third-party dependency** (OpenRouter). Mitigated by the trivial per-turn cost, on-demand usage, the option to set a hard spend cap in the OpenRouter account, and the `:free` variant escape hatch.
  - **Data residency / routing.** OpenRouter routes through whatever provider it picks; mitigated by `provider.data_collection: "deny"` on every request (no retain/train). Glen's tasks are also sandboxed reads, not secrets.
  - **JSON discipline.** Open-weight models are a notch below the frontier CLIs at strict structured output. Mitigated by `response_format: {type: "json_object"}`, `<think>`-stripping, the tolerant extractor, and the orchestrator's existing malformed-output → `agent_error` handling.
  - **Catalog churn.** OpenRouter slugs change; the `config.example.yaml` defaults carry "verify slug" comments. The pluggable-slot design absorbs this — re-point `model_slug`, restart.
  - **Two near-identical adapters to maintain** (Ollama + OpenRouter). Accepted — both are thin, and the duplication is consistent with the codebase's existing per-adapter style.
- **Exceptions to "Operability before capability"**: **none.** Capability addition touching no operability foundation; the metered-dependency risk is the only downside and it's bounded.
- **Follow-up review point**: if usage volume grows, build the `limits.max_total_tokens` cost-cap (would now apply to both metered seats). Also: when a third API key is ever added, the parameterized settings JS handles it with just a config-map entry + an HTML block — no further refactor needed.

## Known Risks

(Operability Impact covers the categories. One UI note carries over from 0010.)

- **The dashboard rendering of the new OpenRouter key field and the refactored settings JS was not visually verified** (no browser in the build environment). The markup is confirmed present in the served page; the settings API and adapter wiring are verified end-to-end. If the new field or the eyeball misbehaves visually, it's a CSS/JS fix.

## Open Questions

- **Exact OpenRouter slugs.** The defaults (`deepseek/deepseek-chat`, `z-ai/glm-4.6`, `qwen/qwen3-coder`) are best-guesses against the live catalog as of 2026-05-12 — verify at `https://openrouter.ai/models` and adjust. The architecture is slug-agnostic.
- **Should the Settings view show a per-seat connection test?** (e.g. "Test deepseek" → `POST /api/agents/deepseek/test`.) Useful for confirming a freshly-pasted key + slug actually work. Not built; easy follow-up.
- **Cost-cap.** Out of scope per Glen; the metered-dependency reality makes it the obvious next companion if usage grows.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `app/agents/openrouter_adapter.py` — `OpenRouterAdapter`, `_http_error_message`, `_parse_and_coerce`
- `app/config.py` — `OpenRouterConfig` / `OpenRouterModel`; `OllamaCloudConfig.enabled` default → `false`
- `app/services/agent_registry.py` — `register_openrouter_models(config)`
- `app/main.py` — calls `register_openrouter_models(config)` after the Ollama one
- `app/api/settings.py` — `_API_KEYS` gains `"openrouter"`
- `app/agents/ollama_adapter.py` — 403-subscription error-message polish
- `config.example.yaml` — `openrouter:` section (3 default seats, `data_collection: deny`); `ollama_cloud:` reduced to a single disabled example seat with explanatory comment
- `app/dashboard/index.html` — OpenRouter key field added to Settings → API Keys (above the Ollama one)
- `app/dashboard/dashboard.js` — settings JS refactored to `API_KEY_FIELDS`-parameterized form (`loadApiKeysSettings`, `onSaveKey` / `onClearKey` / `onToggleKeyVisibility`); `init()` wires each key from the map
- `tests/test_openrouter_adapter.py` — 18 tests
- `tests/test_settings_api.py` — added: openrouter key slot, openrouter env→DB fallback, ollama 403 message
