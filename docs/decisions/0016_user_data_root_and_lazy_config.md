# Decision Record 0016 — User-data root abstraction + lazy config resolution

**Date**: 2026-05-17 (drafted) / 2026-05-18 (ratified)
**Mode**: Glen-directed (ratified after a conclave ratification review)
**Keeper**: claude-code
**Related**: [DR0004](0004_sandbox_not_layer2.md), [DR0005](0005_db_concurrency_and_tier2_archive.md), [DR0006](0006_charter_v1_2_operability_before_capability.md), [DR0012](0012_inline_sandbox_for_api_adapters.md).
**Source thread**: `tsk_01KRVCF7QD1DHJ97TKZ8ZNRN97` → `tsk_01KRVN7VT7NEQ8VNR6NV8CFYFZ` (six-task packaging-architecture consult; five independent consultants converged on the same symbols).
**Ratification thread**: `tsk_01KRWQ6GRWBZJ6WDYFR4E0XCCJ` (conclave-mode pressure-test of the draft DR; codex / gemini / claude-code converged on three concrete fixes, all folded in before ratification).

### Revisions folded in at ratification (2026-05-18)

Three independent reviewers (codex / gemini / claude-code) converged on the same three surgical fixes; design itself was found sound. All three were applied to this record before ratification:

1. **Scope expansion**: added `uploads_root()` and `exports_root()` helpers; added `app/api/uploads.py` and `app/api/tasks.py` to the refactor scope so `data/uploads/` and `data/exports/` references are not left as residual CWD-relative leaks.
2. **Migration hardening**: added an active-`./data/switchboard.pid` check before any copy; switched DB transfer to `VACUUM INTO` for WAL/SHM coherence; required atomic `.tmp`-rename batch with partial-copy cleanup; enforced strict ordering so migration is the first awaitable in `lifespan`, before any DB initializer, retention worker, or other writer.
3. **Operability claims narrowed**: observability dropped from *positive* to *neutral* (no file-logging change actually lands with this commit; `logs_root()` is reserved for a later record); recoverability qualified as *positive within a single root, neutral across the cutover* with the cross-root pidlock limitation explicitly named and pointed at the migration's active-pid check as the actual mitigation.

## What Was Chosen

All writable runtime state — the SQLite database, sandboxes, pidlock, logs, exports, uploads, and (when not running from a repo) the user's `config.yaml` — moves behind a single platform-aware primitive, `user_data_root()`. Module-import-time config loading is replaced with explicit lazy resolution. A first-run migration copies (not moves) repo-relative state into the new root.

Concretely:

- **New module** `app/utils/paths.py`:
  - `user_data_root() -> Path` — pure resolver, cached after first call. Precedence: `SWITCHBOARD_DATA_DIR` env var → platform-specific user-data directory → repo-relative `./data/` if a `pyproject.toml` + `config.example.yaml` are detected in the cwd ancestor chain (dev-mode fallback).
  - Platform defaults: Windows `%LOCALAPPDATA%\AI Switchboard`, macOS `~/Library/Application Support/AI Switchboard`, Linux `$XDG_DATA_HOME/ai-switchboard` (or `~/.local/share/ai-switchboard`).
  - Derived helpers: `user_config_path()`, `default_db_path()`, `sandboxes_root()`, `uploads_root()`, `exports_root()`, `logs_root()`. All create their directory lazily on first use; none execute side effects at import.
- **`app/config.py` refactor**:
  - `DatabaseConfig.path` default flips from `"data/switchboard.db"` to `None`. `None` resolves at load time to `default_db_path()`. An explicit string in `config.yaml` still wins.
  - `load_config()` search order becomes: `SWITCHBOARD_CONFIG` env var → `user_config_path()` → dev-mode `./config.yaml` → `./config.example.yaml` → built-in defaults.
  - First-run seed: if `user_config_path()` doesn't exist and we're not in dev-mode, copy the packaged `config.example.yaml` (resolved via `importlib.resources`) to `user_config_path()` once, log the action, and proceed.
  - New `get_config()` accessor providing the memoized singleton; callers stop importing `config` from `app.main`.
- **`app/main.py` refactor**:
  - Delete `config = load_config()` at module top.
  - `lifespan` calls `get_config()` first, then derives every path through the new helpers.
  - `pidlock.acquire(...)` now receives `user_data_root()` directly (today: `Path(config.database.path).parent` — wrong primitive).
- **`app/services/sandbox.py` refactor**:
  - `SANDBOXES_ROOT = Path("data/sandboxes")` (module-top constant) is replaced with `sandboxes_root()` invoked at every use site (~5–10 call sites).
- **`app/api/uploads.py` + `app/api/tasks.py` refactor**:
  - Every reference to `data/uploads/` and `data/exports/` routes through `uploads_root()` and `exports_root()` respectively. Includes upload write paths, export write paths, and any download/serve endpoints that resolve files by relative path.
- **First-run migration** (in `app/services/migration.py`):
  - Triggers on first lifespan startup where `user_data_root()` is not the dev-mode fallback.
  - **Ordering — strict**: migration is the **first awaitable in `lifespan`**, running before `init_database()`, the orphan reaper, the retention worker, and any other writer. No service in the new root may open the destination DB before migration completes.
  - **Active-instance safety check (cross-root race)**: before copying any SQLite file, the migration inspects `./data/switchboard.pid`. If the lockfile exists AND its PID is alive (via the existing `pidlock` liveness + creation-time check), migration **refuses to run** and surfaces a clear startup error: *"A Switchboard instance appears to be running against ./data/ (PID N). Stop it before launching the packaged build, or delete ./data/switchboard.pid if you're sure the process is gone."* This prevents copying a hot WAL/SHM or an inconsistent DB snapshot. The packaged app cannot reach a partial cross-root state by accident.
  - **SQLite consistency**: the migration uses SQLite's `VACUUM INTO <user_data_root>/switchboard.db.tmp` to produce a consistent single-file snapshot, then atomic-renames to the final name. This avoids the WAL/SHM-coherence trap of file-level `copy` and produces a clean, checkpointed DB even if the source had a non-empty WAL.
  - **Atomic batch + partial-copy cleanup**: every destination file (DB + sandbox tree + exports + uploads) is written to a `.tmp` sibling and atomically renamed only after the whole batch succeeds. On any failure mid-migration, the partial `.tmp` artifacts are removed; the original `./data/` is unaffected; the migration is retried on the next launch.
  - **What gets migrated**: `./data/switchboard.db` (via `VACUUM INTO`), and the contents of `./data/sandboxes/`, `./data/exports/`, `./data/uploads/` (each only if present). The old `switchboard.pid` is **not** migrated (a fresh pidlock is acquired against `user_data_root()`).
  - Originals are **never deleted**. A `data_migration` log line records source paths, destination, file count, and the VACUUM INTO bytes-written. A one-time stderr message instructs the user that the originals can be removed manually.
  - Idempotent: subsequent launches see the destination DB exists and skip the entire migration block (no per-file re-check, which would be a footgun if the user intentionally deleted some sandbox).
- **Tests**:
  - `tests/test_paths.py` — per-platform resolution, env override, dev-mode walk-up detection, caching.
  - `tests/test_config_resolution.py` — discovery order, first-run example-config copy.
  - `tests/test_migration.py` — copy-not-move semantics, idempotence, partial-source cases.
  - Existing tests get a `conftest.py` fixture pinning `SWITCHBOARD_DATA_DIR` to `tmp_path`. Suite expected to flush out a handful of tests that hard-code `Path("data/...")`; those are fixed in the same PR.

## Why It Was Chosen

The six-task packaging-architecture consult thread (May 2026) surfaced one dominant blocker for desktop distribution: every writable path in the codebase is implicitly relative to the process current working directory. When the service is launched by `python -m uvicorn` from the repo root, this works fine — the cwd *is* `./data/`'s parent. When the same service is launched from a frozen executable on a user's machine (double-click on Windows, Finder on macOS), the cwd is whatever the OS handed the launcher — typically the user's home, the install directory, or in macOS's case essentially arbitrary. The result is silent state-splitting: sandboxes get created next to the executable, the database opens in the install dir (and then can't write because the install dir is read-only), pidlock fails to enforce single-instance because each launch directory gets its own lockfile, and OpenRouter API keys appear to vanish because the DB they were stored in is now in a different location than the new instance is reading from.

Five separate consultants (codex, claude-code, gemini, deepseek, qwen) — three with CLI-native file access, two with our new read-file tool-loop — independently read the sandbox and converged on the same four symbols: `app/config.py:load_config`'s relative default, `app/database.py`'s `data/switchboard.db` default, `app/services/sandbox.py:SANDBOXES_ROOT`, and `app/main.py`'s import-time `config = load_config()`. The import-time call is the load-bearing one: by the time `lifespan` runs and could choose a stable root, every dependent path has already been computed against the wrong cwd.

The fix is small in scope but architectural in commitment — it picks where the app's state lives and binds future packaging work to that decision. Doing it now (before any packaging effort starts) keeps the change to ~250 LOC across ~6 files plus tests. Doing it later (after PyInstaller or Briefcase is wired up) requires the same fix plus a packaging-config migration plus a more complicated cutover, because by then users have state distributed across multiple inadvertent roots.

The dev-mode fallback (repo detection preserves today's `./data/` behavior when running from source) is deliberate: contributors and Glen running from `~/Desktop/Conclave AI` should see no change in where their state lives. The migration runs *only* in the packaged-app case.

## What Was Rejected

- **Unconditional move to `user_data_root()`, no dev-mode fallback.** Rejected — would break the current developer workflow on every checkout. Contributors with local state in `./data/` would have to manually copy it or set `SWITCHBOARD_DATA_DIR=./data/` every shell session. The dev-mode walk-up detection costs ~10 lines and preserves the existing experience exactly.
- **Hard-cut migration (move, not copy).** Rejected — too dangerous, no undo path. A copy-not-move policy means a botched migration produces a duplicated tree, not a lost one. Disk cost is bounded (single-user local app, typical DB <50 MB); the safety margin is worth it.
- **Skip migration entirely; let users start fresh.** Rejected — the database contains the full audit trail of every prior deliberation, including the decision records and OpenRouter API keys. Losing that on a packaging upgrade is unacceptable under the Charter's audit-trail and durability requirements.
- **Per-task path override** (a `task.context.extra["data_root"]` field). Rejected — sprawls the protocol for a problem that's resolved once globally. If a real need surfaces (e.g., separate council histories per project), revisit.
- **Symlinks from `./data/` → `user_data_root()` during migration.** Rejected — Windows symlink behavior requires Developer Mode or admin elevation, which a packaged app shouldn't demand. Copy is universally supported.
- **Reuse the `SWITCHBOARD_CONFIG` env var to cover both config and state.** Rejected — they're different concerns and packagers may want to bundle a default config inside the app while keeping state user-writable. Separate `SWITCHBOARD_CONFIG` (config file path) and `SWITCHBOARD_DATA_DIR` (mutable-state root) env vars; either can be set independently.
- **A single grand "platform host layer" refactor** (codex's three-layer proposal from the consult thread: `switchboard-core` / `local-server-host` / `desktop-launcher`). Rejected for this record — that's good architectural framing once packaging is on the implementation roadmap, but the path primitive is the only piece that has to land before packaging begins. The rest is a label without code consequence until then.

## Operability Impact

(Eighth decision under Charter v1.2 §Decision Records.)

- **Observability**: **neutral**. The DR reserves `logs_root()` as a derived path so a later record can wire up durable file logging without further plumbing, but **no file-logging change lands with this commit** — the running service continues to log to stderr. The observability improvement from "logs in a known place" is deferred and must not be claimed as a benefit of this record. Adopting `logs_root()` for real logging is tracked as an open question below.
- **Durability**: **positive**. State has a single resolved root inside `user_data_root()`. Two launches from two different shells no longer create two divergent state trees once migration has run.
- **Recoverability**: **positive within a single root, neutral across the cutover.** The migration is non-destructive (copy-not-move) and logged; if anything goes wrong with the new root, the originals at `./data/` are intact for inspection or rollback. Pidlock enforces single-instance correctly **within `user_data_root()`** after migration. It does **not** enforce single-instance across roots — an old service running against `./data/` would have its own lockfile there, and a packaged build starting against `user_data_root()` would not see it. The migration's active-pid check (above) is what closes that cutover-window race, not pidlock itself; the claim is narrowed accordingly.
- **Audit trail**: **positive**. Sandboxes, exports, and decision records consistently land in the same place across launches, simplifying support and post-hoc review.
- **Retention/export**: **neutral**. The retention worker operates on whatever DB it opens — no policy change. The retention budget is now correctly applied to one DB instead of being silently fragmented across multiple cwd-shaped roots.
- **Complexity**: **moderate negative**. ~250 LOC of new code (paths module + migration + accessor refactor) plus test coverage. The new resolution layer is well-bounded; reads as a single helper function plus a one-time migration with idempotent semantics.
- **Cost**: neutral.
- **Accepted risks**:
  - **Migration bug orphaning state.** Mitigated by copy-not-move; the original tree is always intact.
  - **Dev-mode walk-up detection misfires** (e.g., a user runs Switchboard from inside an unrelated repo that happens to have a `pyproject.toml`). Mitigated by also requiring `config.example.yaml` to be present in the same ancestor and by the explicit `SWITCHBOARD_DATA_DIR` env var override.
  - **Two simultaneous installations sharing `user_data_root()`.** Mitigated by pidlock — single-instance enforcement now works correctly across launches. Two parallel installs that want separate state can use `SWITCHBOARD_DATA_DIR`.
  - **Hard-coded `Path("data/...")` lurking in tests or scripts.** Mitigated by running the test suite as part of this PR and fixing every flush-out in the same commit.
- **Exceptions to "Operability before capability"**: **none**. This is purely an operability change with no new user-facing capability.

## Known Risks

- **Path migration is per-machine, not portable.** A user who copies their `./data/` to another machine cannot drop it directly under `user_data_root()` on the new machine; they must use `SWITCHBOARD_DATA_DIR` or copy-then-launch. Acceptable for the single-user-local-app posture; documented in README.
- **Future packaging tooling may want to inject its own root** (e.g., a Briefcase-style "managed user data path" hook). The `SWITCHBOARD_DATA_DIR` env var gives any packager an injection point without code changes.
- **The dev-mode heuristic is informal.** It's good enough for the current contributor workflow but not a hard contract. If it becomes load-bearing, promote it to an explicit `SWITCHBOARD_MODE=dev` env var with documented semantics.

## Open Questions

- **Logs:** should `logs_root()` start being used by `logging.basicConfig()` in `lifespan` now, or wait for a follow-up logging-hardening pass? Recommendation: write the helper and reserve the directory now; wire up file logging in a separate record so the logging-config decisions (rotation, level routing, audit-event separation) get their own deliberation.
- **First-run UX:** should the migration's "originals can be removed manually" stderr message also appear as a one-time dashboard toast? Defer to dashboard implementation; the log line and the stderr message are sufficient for the initial PR.
- **OpenRouter API keys after migration:** the keys are stored in the SQLite `settings` table, so they ride along with the DB copy. Verify in the migration test. (Expected to pass; flag if not.)

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lands at:

- `app/utils/paths.py` — new module, all the resolvers
- `app/config.py` — `load_config()` search order; `DatabaseConfig.path` default flips to `None`; new `get_config()` accessor
- `app/main.py` — delete import-time `config = load_config()`; pidlock now receives `user_data_root()`; every caller of the old global `config` migrated to `get_config()`
- `app/services/sandbox.py` — `SANDBOXES_ROOT` constant removed; `sandboxes_root()` called per use
- `app/api/uploads.py` — every `data/uploads/` reference routes through `uploads_root()`
- `app/api/tasks.py` — every `data/exports/` reference routes through `exports_root()`; download/serve endpoints resolve files relative to that root
- `app/services/migration.py` — first-run copy logic with active-pid check, `VACUUM INTO`-based DB snapshot, atomic `.tmp` rename batch, partial-copy cleanup, and strict ordering (migration runs as the first awaitable in `lifespan`, before any writer)
- `tests/test_paths.py`, `tests/test_config_resolution.py`, `tests/test_migration.py` — new
- `tests/conftest.py` — `SWITCHBOARD_DATA_DIR` pin fixture
- Test suite-wide audit for hard-coded `Path("data/...")` references — fixed in the same PR
- `README.md` / `INSTALL.md` — note the new platform-specific data path; document `SWITCHBOARD_DATA_DIR` override; mention the one-time migration on first packaged launch

The implementation lands as **a single commit** (no two-phase split — the helpers, refactor, migration, and tests have circular dependencies and can't merge independently). Ratification of this record gates the commit.
