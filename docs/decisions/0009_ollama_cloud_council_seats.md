# Decision Record 0009 — Ollama Cloud council seats (open-weight models)

**Date**: 2026-05-12
**Mode**: Glen-directed (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

The council can now include open-weight frontier-class models hosted on **Ollama Cloud** — added as pluggable seats that appear in the same dashboard checkbox list as the CLI agents (codex / gemini / claude-code). Three are enabled by default:

| Council name | Ollama Cloud model id (verify at build time) | Lab / lineage |
|---|---|---|
| `deepseek` | `deepseek-v3.1:671b-cloud` | DeepSeek (China) — strongest open-weight reasoner |
| `glm` | `glm-5:cloud` | Z.ai / Zhipu (China) — reasoning + agentic |
| `qwen` | `qwen3-coder:480b-cloud` | Alibaba (China) — strong instruction-following, best JSON discipline of the open bunch |

Glen's intent: these are an *on-demand* third/fourth voice for hard problems, not always-on. You tick the ones you want for a given task, same as you already tick codex/gemini/claude-code.

Concretely:

- **New adapter** `app/agents/ollama_adapter.py` — `OllamaCloudAdapter(name, model_id, max_context_chars, endpoint)`. One class, instantiated once per enabled model. Talks to `POST {endpoint}/api/chat` with `Authorization: Bearer $OLLAMA_API_KEY`, `format: "json"`, `think: false`. Strips any `<think>…</think>` block the model emits anyway, then parses with the same tolerant JSON extractor the CLI adapters use. Stashes `prompt_eval_count` / `eval_count` as input/output tokens for `agent_runs`. It is the simplest of the four adapters — clean async HTTP via `httpx`, no subprocess.
- **Config** — new `ollama_cloud:` section in `config.example.yaml` (and `OllamaCloudConfig` / `OllamaCloudModel` in `app/config.py`): `enabled`, `endpoint`, and a `models` list of `{name, model_id, max_context_chars}`. Adding/removing a seat is a config edit — no code change.
- **Registry** — `agent_registry.register_ollama_cloud_models(config)` registers one adapter per enabled model. Called from `main.py` after `init_registry()`. Deliberately *not* part of `init_registry()` so tests (which call `init_registry()` with no config) never pull in network-backed adapters.
- **Dashboard** — *no change needed.* The "New Task" checkbox list is populated from `GET /api/agents`, so the Ollama seats appear automatically once registered.
- **Auth** — `OLLAMA_API_KEY` env var (created at ollama.com). If unset, the seats register but `is_available()` returns false and the orchestrator surfaces `agent_unavailable` — same pattern as a CLI adapter whose binary isn't on PATH. The key is never stored in config files.
- **Tests** — 16 new in `tests/test_ollama_adapter.py` (all HTTP mocked): content extraction, `<think>`-stripping, usage stash, error/timeout/empty-content paths, API-key gating, constructor validation, `run_conclave_turn` end-to-end, and `register_ollama_cloud_models` add/no-op cases. 137 tests total, all pass.

## Why It Was Chosen

The council was three frontier models from three Western labs — Codex (OpenAI), Gemini (Google), Claude (Anthropic). They're trained on overlapping data with similar post-training cultures; they often agree because they *think alike*. The strongest argument for an open-weight seat isn't cost (it's a paid cloud service — see below) — it's **lineage diversity**: DeepSeek / GLM / Qwen are non-Western labs with genuinely different training, and a dissenting voice from outside the OpenAI/Google/Anthropic axis is worth more to a deliberation than a fourth model that mostly nods.

Ollama Cloud specifically (vs. each provider's own API) gives **one integration that exposes many open-weight models** — the `ollama` adapter is a *pluggable slot*. Swapping the council's open seat between DeepSeek, GLM, Qwen, Kimi-K2.x, gpt-oss, etc. is a config edit, not a new adapter each time. And it's cloud-hosted, so latency is competitive with the CLI agents (no consumer-GPU bottleneck) and there's no local setup.

## What Was Rejected

- **Local Ollama (run the model on Glen's GPU).** Considered first; rejected once Glen clarified he meant the *cloud* offering. Local would be free-per-token but slow on consumer hardware, capability-limited by VRAM, and a setup burden. Cloud trades "free" for "fast + big + zero setup."
- **A cost-cap backstop bundled with this.** Ollama Cloud is metered, so a `limits.max_total_tokens` ceiling would be the natural companion — but Glen explicitly scoped costs out ("my plan is not to use these APIs all the time, only when I need a third or fourth voice on a difficult problem"). The existing `max_rounds` / `max_seconds` backstops already prevent infinite loops. If usage patterns change, the cost-cap is a small follow-up.
- **Direct provider APIs (DeepSeek's own API, Mistral, etc.) instead of Ollama Cloud.** Rejected for the council use case — Ollama Cloud's one-integration-many-models property is the point. (You can still add a direct-provider adapter later if you want a model Ollama doesn't carry.)
- **Image-attachment support in the Ollama adapter.** Skipped for v1 — these are text-reasoning models. An image-heavy task with an Ollama participant: the Ollama seat reasons on the text context only; the frontier participants handle the visual part. Documented as a known limitation.
- **Full JSON-Schema-constrained output** (passing the Pydantic models' schema to Ollama's `format` parameter). Started with the simpler `format: "json"` mode + the prompt builders' existing instructions + the tolerant parser. If a particular model malforms output too often, tightening to a schema is a localized change.
- **Reading `config.agents.ollama` (the old local stub).** Removed from `config.example.yaml` — superseded by the new `ollama_cloud:` section. Nothing consumed the old stub.

## Operability Impact

(Fourth decision under Charter v1.2 §Decision Records.)

- **Observability**: neutral. Ollama-backed `agent_runs` record input/output tokens like the others (no cost field, intentionally).
- **Durability**: neutral. No new persistent state, no schema change. The seats are config + in-memory registry entries.
- **Recoverability**: neutral. A failed Ollama call surfaces as `agent_error` / `agent_timeout` / `agent_unavailable` exactly like a CLI adapter; the orchestrator handles it the same way.
- **Audit trail**: neutral. Participant names (`deepseek` etc.) flow through threading, `/decide`, `/continue`, and the PDF/DOCX/markdown exports as plain strings — verified.
- **Retention/export**: neutral.
- **Complexity**: low-moderate. One new adapter module (~280 lines), `OllamaCloudConfig`/`OllamaCloudModel` in config, one registry function, one line in `main.py`. New dependency: none — `httpx` was already required. No new processes, no new persistence.
- **Accepted risks**:
  - **New metered third-party dependency.** Ollama Cloud is paid; if these seats get used heavily, they accrue cost. Mitigated by Glen's stated usage pattern (on-demand only) and the existing round/time backstops. The cost-cap remains an available follow-up if the pattern changes.
  - **JSON discipline.** Open-weight models are a notch below the frontier CLIs at strict structured output. Mitigated by `format: "json"`, `<think>`-stripping, the tolerant extractor, and the orchestrator's existing malformed-output → `agent_error` handling. Will fail more often than the CLIs; not catastrophically.
  - **Capability dilution as a co-equal participant.** A weaker peer can anchor a worse position or conform under pressure. Mitigated by treating these as *trial* peers — verify each on a few real questions before trusting it co-equal; keep it in the consultant role if it conforms or malforms.
  - **Catalog churn.** Ollama's model ids change as the catalog evolves; the defaults in `config.example.yaml` carry "verify exact tag" comments. The pluggable-slot design absorbs this — re-point `model_id`, restart.
- **Exceptions to "Operability before capability"**: **none.** Capability addition that touches no operability foundation. Bounded-priority-window test satisfied — no named operability gap was displaced.
- **Follow-up review point**: after Glen has used an Ollama seat on a handful of real conclaves, decide (a) whether any of them earns a permanent co-equal slot vs. consultant-only, and (b) whether usage volume now warrants the `limits.max_total_tokens` cost-cap.

## Known Risks

(Operability Impact covers the categories. One additional note.)

- **Three seats default to enabled** in `config.example.yaml`, so a fresh checkout shows six checkboxes. Without `OLLAMA_API_KEY` set, the three Ollama ones are inert (selecting one → `agent_unavailable`). A user who wants them hidden entirely sets `ollama_cloud.enabled: false`. Acceptable — surfacing them is the point of Glen's checkbox vision; the unavailable-without-key behavior is the same as the existing CLI adapters.

## Open Questions

- **Which exact model ids?** The `config.example.yaml` defaults (`deepseek-v3.1:671b-cloud`, `glm-5:cloud`, `qwen3-coder:480b-cloud`) are best-guesses against the live catalog as of 2026-05-12 — verify on `https://ollama.com/search?c=cloud` and adjust. The architecture doesn't care which ids; the pluggable slot is the point.
- **Should Ollama seats get their own conclave behavior tuning** (e.g. a shorter `timeout_seconds`, or a different prompt preamble)? Not yet — they use the same prompt builders and timeouts as everyone. Revisit if a model consistently runs long or needs different framing.
- **Cost-cap.** Out of scope per Glen, but the metered-dependency risk makes it the obvious next companion if usage grows. Tracked, not built.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `app/agents/ollama_adapter.py` — `OllamaCloudAdapter`, `_parse_and_coerce`
- `app/config.py` — `OllamaCloudConfig`, `OllamaCloudModel`, `Config.ollama_cloud`
- `app/services/agent_registry.py` — `register_ollama_cloud_models(config)`
- `app/main.py` — calls `register_ollama_cloud_models(config)` after `init_registry()`
- `config.example.yaml` — `ollama_cloud:` section with the three default seats; old local `agents.ollama` stub removed
- `tests/test_ollama_adapter.py` — 16 tests
