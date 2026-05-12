# Decision Record 0010 — Left-rail Settings panel + DB-stored API keys (env-fallback)

**Date**: 2026-05-12
**Mode**: Glen-directed (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

The dashboard gains a **narrow fixed left rail** with a gear icon that opens a **Settings** view. The first settings section is **API Keys**, containing the **Ollama Cloud API key**: a password-masked input with an **eyeball toggle** to reveal/hide it, plus Save and "Clear stored key" actions. The key is persisted in the local database; an environment variable, if set, takes precedence.

Precedence rule (now consistent across the app):

> **`OLLAMA_API_KEY` environment variable, if set, wins. Otherwise the database-stored value is used. If neither, the Ollama seats are unavailable.**

Concretely:

- **Backend**
  - `app/services/settings_store.py` — a tiny key/value store over the existing `settings` table (`get_secret` / `set_secret` / `delete_secret`). `get_secret` returns `None` (doesn't raise) if the DB isn't initialised, so adapter call-sites degrade gracefully in unit tests.
  - `app/api/settings.py` — new router:
    - `GET /api/settings/api-keys` → `{"ollama": {"set": bool, "source": "env"|"db"|"none", "masked": "••••…WXYZ"|null}}`
    - `POST /api/settings/api-keys/ollama` → body `{"value": "sk-…"}` stores it; `{"value": ""}` or `null` clears it. Response includes the resulting effective `status`.
    - `GET /api/settings/api-keys/ollama/reveal` → `{"value": "<plaintext>"}` for a DB-stored key; `{"value": null, "note": "…environment variable…"}` if env-sourced (env-var secrets are never echoed back).
  - `app/main.py` — includes the new router.
  - `OllamaCloudAdapter._api_key()` — now resolves env var first, then `settings_store.get_secret("ollama_api_key")` (lazy import to keep the DB out of the adapter module's hard deps).
- **Dashboard**
  - `index.html` — a `<nav class="left-rail">` (fixed, 48 px, dark) with a single gear `<button id="settings-toggle">` pinned to the bottom; a new `view-settings` section with the API Keys block (masked input + `eyeball-btn` + Save + Clear + a hint explaining the env-precedence rule and where the key is stored).
  - `dashboard.css` — `.left-rail` / `.rail-btn`, `body { padding-left: 48px }`, and `.settings-view` / `.settings-section` / `.api-key-row` / `.api-key-input-group` / `.eyeball-btn` / `.api-key-status` / `.api-key-feedback` styling.
  - `dashboard.js` — `switchView()` now knows the `settings` view; the gear button routes to it; `loadApiKeysSettings()` populates the status line + placeholder; `onToggleOllamaKeyVisibility()` flips the input between `password` and `text`, fetching the plaintext from `/reveal` on first reveal *only if the key is DB-stored* (env-sourced keys show a "can't show this" note instead); `onSaveOllamaKey()` / `onClearOllamaKey()` POST and re-load.
- **Tests** — 11 new in `tests/test_settings_api.py`: set/get/reveal/clear round-trips, masking, the env-over-DB precedence (GET reports `env`, `/reveal` refuses), the `OllamaCloudAdapter._api_key()` env→DB fallback, adapter-unavailable-with-neither, and `settings_store.get_secret` returning `None` when the DB is uninitialised. 148 tests total, all pass.

## Why It Was Chosen

The Ollama Cloud seats (decision 0009) needed credentials, and `OLLAMA_API_KEY` env var was the only way to provide one. Glen wanted to manage it from the app: a place to paste, store, and reveal the key without touching the shell environment — while keeping the env var as an override for people who prefer that. The `settings` table already existed (unused since the original schema), so this is a use of existing infrastructure, not new infrastructure.

The left rail + gear is the entry point Glen specified ("a small, narrow left-hand panel … an icon with a settings section I can click on"). It's deliberately minimal — one icon today — but it's the natural home for future app-level settings (other API keys, default-agent preferences, theme) without crowding the top tab bar.

## What Was Rejected

- **Encrypting the stored key at rest.** Rejected — there's no key-management story for a single-user local tool, and the DB file (`data/switchboard.db`) is already gitignored and local-only: the same trust boundary as a `.env` file on the same machine. Adding encryption would be security theatre. The API does *not* echo the stored secret except via the explicit `/reveal` endpoint, and never logs it.
- **Returning the plaintext in `GET /api/settings/api-keys`** so the input could be pre-filled. Rejected — GET returns only a masked tail (`••••…WXYZ`) + `source`. The full value is fetched on demand via `/reveal` when the user clicks the eyeball, so it isn't in the DOM on every Settings page load.
- **Revealing the env-var value.** Rejected — env-var secrets aren't ours to leak. If the key is env-sourced, `/reveal` returns `value: null` with an explanatory note and the eyeball shows that note instead of a value.
- **Making Settings a top-bar tab.** Rejected in favour of the left-rail gear — Glen asked for the rail specifically, and it keeps the top bar focused on the task-flow views (New / Inbox / Detail).
- **A generic "any setting" editor.** Out of scope — the API-key store is purpose-built (`_API_KEYS` map gates which names are valid), so a typo'd key name can't write arbitrary rows.

## Operability Impact

(Fifth decision under Charter v1.2 §Decision Records. This one is mildly operability-*positive*.)

- **Observability**: neutral-to-positive — `GET /api/settings/api-keys` makes the credential state inspectable (set? from where? what tail?) without shell access.
- **Durability**: neutral — uses the existing `settings` table; no schema change. A stored key survives restarts.
- **Recoverability**: slightly positive — a misconfigured Ollama seat is now diagnosable and fixable from the dashboard (Settings → see "not set" → paste a key) instead of requiring an env-var edit + restart. (The env var is still honoured for those who prefer it.)
- **Audit trail**: neutral. The key is never written to logs, the task DB rows, or exports.
- **Retention/export**: neutral. The `settings` table is not part of the tier-based retention sweep (it holds config, not deliberation content).
- **Complexity**: low. One small service module (~55 lines), one small router (~85 lines), one line in `main.py`, a ~12-line change to the adapter, and a contained dashboard addition (rail + one view + ~150 lines of JS). No new dependencies. No new processes. No new persistence layer — reuses the `settings` table.
- **Accepted risks**:
  - **Plaintext secret in the DB.** Mitigated by the trust boundary (local, single-user, gitignored) and by never echoing/logging it. Documented in `settings_store.py`.
  - **`test_connection()` for Ollama seats checks reachability, not key validity** — it `GET /api/tags` on the endpoint, which can return 200 even for a bogus key. So a green connection-test doesn't guarantee the key works; the first real `/api/chat` call is the true test. Acceptable for a best-effort liveness probe; `is_available()` only checks key *presence* anyway.
  - **The eyeball reveal puts the plaintext in the DOM** while the field is in `text` mode. Standard behaviour for any "show password" toggle; the field reverts to `password` on the next click and on re-entering the Settings view. Not a meaningful risk increase over "the key is on disk in the DB."
- **Exceptions to "Operability before capability"**: **none.** Mostly a UX/operability improvement; the small capability surface (a settings store) doesn't degrade any foundation.
- **Follow-up review point**: when a second API key or app-level setting is added, factor the eyeball/save/clear JS into a reusable component rather than duplicating it.

## Known Risks

(Operability Impact covers the categories. One UI note.)

- **The dashboard rendering of the rail + settings view was not visually verified** (no browser in the build environment). The HTML/CSS/JS are wired and the markup is confirmed present in the served page; the API endpoints are verified end-to-end (set → adapter picks up the DB key → seat becomes available → reveal → clear). If the rail or the eyeball misbehaves visually, it's a CSS/JS fix, not an architectural one.

## Open Questions

- **Should the Settings view also surface a per-seat connection test?** ("Test deepseek" button → `POST /api/agents/deepseek/test`.) Useful for confirming a freshly-pasted key actually works against `/api/chat`. Not built — easy follow-up if Glen wants it.
- **Other API keys?** The `_API_KEYS` map is ready for more entries; nothing else needs one today (the CLI adapters use the host's logged-in CLI sessions, not API keys).
- **A general settings UX** (theme, default agents/mode)? Out of scope for now; the rail is the place for it if it ever happens.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `app/services/settings_store.py` — `get_secret` / `set_secret` / `delete_secret`
- `app/api/settings.py` — the settings router (`_API_KEYS` map, `_mask`, GET / POST / reveal)
- `app/main.py` — includes `settings_api.router`
- `app/agents/ollama_adapter.py` — `_api_key()` now does env → DB
- `app/dashboard/index.html` — `.left-rail` + `#settings-toggle`; `#view-settings` with the API Keys block
- `app/dashboard/dashboard.css` — left-rail and settings-view styles
- `app/dashboard/dashboard.js` — `switchView` settings case, gear wiring, `loadApiKeysSettings` / `onToggleOllamaKeyVisibility` / `onSaveOllamaKey` / `onClearOllamaKey`, `Api.getApiKeys` / `Api.setApiKey` / `Api.revealApiKey`
- `tests/test_settings_api.py` — 11 tests
