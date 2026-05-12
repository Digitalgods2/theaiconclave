# Decision Record 0007 — Codex + Gemini slash-command parity

**Date**: 2026-05-11
**Mode**: Glen-directed (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

Switchboard's slash commands (`/conclave`, `/decide`, `/decision`, `/continue`, `/answer`, `/thread`, `/consult`, `/secondopinion`) are now invokable from inside Codex and Gemini CLI sessions, with full feature parity to the existing Claude Code integration. All eight commands hit the same Switchboard service (`127.0.0.1:8787`) via the same `switchboard.py` client. Provenance is captured: every invocation records which CLI submitted it.

Concretely:

- **Source-of-truth directory** `clients/` in the repo, holding three subdirectories:
  - `clients/claude-code-commands/` — 8 markdown files (one per slash command)
  - `clients/codex-skill/SKILL.md` — single skill file with trigger-phrase activation (Codex has no literal slash commands; the skill's `description` field is matched against user free-text)
  - `clients/gemini-extension/` — Gemini custom-commands extension with `gemini-extension.json` + 8 TOML files in `commands/`
- **`clients/install.py`** — deploy script that copies/links the files into each tool's home directory.
- **`switchboard.py` learns `--invoked-by <name>`** — a new global flag accepted anywhere in argv. Default chain: explicit flag > `SWITCHBOARD_INVOKED_BY` env var > `"claude-code"` (back-compat). Every command/skill installed by `clients/` bakes the correct value into its bash invocation.
- **API surfaces `source_agent`** on both `GET /api/tasks` and `GET /api/tasks/{id}` responses (the column has existed since the original schema; the API just wasn't reading it back).
- **Test coverage**: 10 new tests in `tests/test_provenance.py` covering API round-trip for codex/gemini/claude-code/NULL source_agent values, and the `_pop_invoked_by` flag parser including space form, equals form, env fallback, default fallback, and flag-wins-over-env precedence.

## Why It Was Chosen

The conclave originally only had one client entry point: Claude Code. That meant Glen could only invoke the system from inside a Claude Code session — turning Switchboard into an asymmetric tool where one of the three "equal participants" (per Charter §Purpose) is also the only place the user can drive it from. Putting the same slash commands inside Codex and Gemini sessions restores symmetry: any of the three CLIs Glen is currently working in can convene the conclave or record a decision.

The provenance plumbing (`source_agent` reflecting actual caller) closes a long-standing gap. Until today every task was tagged `source_agent: "claude-code"` regardless of where it came from — because that was the hardcoded default in the client. With multiple real entry points, that tag had to become accurate.

## What Was Rejected

- **Auto-consulting (Codex/Gemini invoking conclaves mid-task without user permission)**. Scope-confirmed out by Glen. The current change is slash-command parity only; the user is always the one initiating a conclave.
- **Mounting `switchboard.py` independently in each tool's home dir**. Considered for isolation; rejected because three copies of the same script means three things to keep in sync. All three skill systems reference the existing path at `~/.claude/skills/switchboard-conclave/switchboard.py` — Claude Code happens to host the canonical script, but its location is incidental. Could be migrated to `clients/switchboard.py` in a future cleanup.
- **A separate Gemini *skill* (`SKILL.md`) in addition to the custom-commands extension**. Considered as "natural-language trigger" parity with Codex. Deferred: the user asked for slash-command parity only. Easy to add later.
- **Symlinking the installed files instead of copying**. Windows symlinks require admin or developer mode; copying is portable and lets the install script work on any account. The Gemini extension uses `gemini extensions link` (which IS a symlink/link in Gemini's own model), which is fine because Gemini manages the link.
- **Adding a new `invoked_by` column distinct from `source_agent`**. Considered; rejected because `source_agent` already existed and means the right thing. No schema migration needed.

## Operability Impact

(Second decision to use the new field, per Charter v1.2 §Decision Records. This is also the first capability-expansion decision since v1.2 was ratified, so the impact analysis is load-bearing — it's the test of whether the v1.2 amendment is actually useful in practice.)

- **Observability**: **positive**. The inbox can now distinguish a task submitted from a Codex session vs. a Gemini session vs. a Claude Code session vs. the dashboard vs. a raw API POST. Future audit questions like "did Codex initiate this deliberation, or was it Glen via Claude Code?" become answerable.
- **Durability**: neutral. No new state. The new column wasn't actually new — the existing `tasks.source_agent` column just wasn't being read out by the API.
- **Recoverability**: neutral.
- **Audit trail**: **positive**. Every task now has accurate provenance. The Decision Record audit trail (which references task IDs) is more useful because tasks' origins are no longer all conflated as "claude-code."
- **Retention/export**: neutral. The export markdown could optionally include the source_agent in the header — deferred as a tiny cosmetic enhancement, not part of this decision.
- **Complexity**: low. ~9 small client files per non-Claude tool (1 SKILL.md for Codex; 1 manifest + 8 TOMLs for Gemini), one new flag-parser helper in `switchboard.py`, two new fields in two API responses, one install script. No new dependencies. No new processes. No new persistence.
- **Accepted risks**:
  - Gemini's `!{...}` shell substitution runs `switchboard.py` with `{{args}}` interpolated; if the user types a double-quote in their question, the inline shell command could mis-parse. Same risk as exists for Claude Code's `$ARGUMENTS` and acceptable at current scale.
  - Codex's trigger-phrase matching is fuzzier than literal slash commands; the skill might activate unintendedly on conversational phrases like "I'd like a second opinion on dinner." The skill's `description` field tries to scope the triggers to AI-coding contexts, but no formal guard exists.
- **Exceptions to "Operability before capability"**: **none**. This change adds entry points to an existing service without degrading any operability foundation. The bounded-priority-window test from §Operability before capability is satisfied: provenance tracking (an operability item) was bundled with the capability addition rather than displaced by it.
- **Follow-up review point**: after Glen has used the Codex and Gemini paths for ~10 real tasks each, re-evaluate whether the inbox needs a "filter by source_agent" view. Not building it yet — wait for evidence of use.

## Known Risks

(Operability Impact covers the major categories. Additional non-operability risks below.)

- **Subscription cost asymmetry**. Conclaves invoked from Codex still cost Codex + Gemini + Claude subscription quota. If Glen makes invocation easier across tools, total quota burn could rise. Mitigation: the `clients/codex-skill/SKILL.md` and `clients/gemini-extension/commands/*.toml` files all explicitly recommend not invoking the conclave for trivial questions.
- **Skill description regex collisions in Codex**. The Codex skill's trigger phrases overlap with general "second opinion" or "what do you think" phrasing that might apply to non-deliberative contexts. Acceptable to leave for now; tighten if it produces false-positive activations.
- **Self-call recursion in Codex**. When invoked from Codex, the conclave includes Codex as a participant — meaning Codex spawns a headless Codex subprocess. The subprocess does not see the parent's conversation (verified). No infinite recursion; each subprocess is bounded by the orchestrator's round limit.

## Open Questions

- **Should `switchboard.py` live in the repo's `clients/` directory rather than `~/.claude/skills/`?** Currently it's coincidentally hosted under the Claude Code skill dir. Moving it would be a small cleanup; the slash commands' invocation paths would change. Defer until the location actually causes friction.
- **Should the dashboard inbox show `source_agent` as a column or filter?** Not yet. Per the v1.2 follow-up review point above, wait for evidence of value.
- **Should Codex get literal slash commands via a `plugin` (its other extensibility path)?** Codex's `plugin marketplace` is its newer story. Current SKILL.md is sufficient for trigger-phrase activation; if Codex evolves to support literal slash commands as cleanly as Claude Code or Gemini, we can add a thin wrapper later.
- **Should Gemini get a paired SKILL.md** for natural-language trigger activation, mirroring the Codex skill? Cheap to add; left out of this decision because Glen asked specifically for slash-command parity, not skill parity.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `clients/install.py` — deploy script
- `clients/README.md` — source-of-truth documentation, layout explanation, deployment instructions
- `clients/claude-code-commands/*.md` (8 files) — Claude Code slash-command source
- `clients/codex-skill/SKILL.md` — Codex skill source
- `clients/gemini-extension/gemini-extension.json` + `commands/*.toml` (8 files) — Gemini extension source
- `app/api/tasks.py` — list_tasks + get_task now return `source` and `source_agent`
- `~/.claude/skills/switchboard-conclave/switchboard.py` — `_INVOKED_BY` global, `_pop_invoked_by` parser, threaded through `submit()`
- `tests/test_provenance.py` — 10 tests

Installed deployment targets:

- `~/.claude/commands/` — 8 markdown slash-command files
- `~/.codex/skills/switchboard-conclave/SKILL.md` — Codex skill
- Gemini extension `switchboard-conclave` (linked from the repo source via `gemini extensions link --consent`)
