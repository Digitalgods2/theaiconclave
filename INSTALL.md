# Install & First-Run Guide

This is the consolidated setup path for AI Switchboard. If you've never run it before, follow this top-to-bottom. If something breaks, the "Troubleshooting" section at the bottom covers the common failure modes.

## Prerequisites

- **Python 3.13+** (we use 3.13.2 in development; older versions may work but aren't tested)
- **PowerShell** on Windows, or **bash/zsh** on Mac/Linux
- One or more of the AI CLIs you want to use for real conclaves:
  - **Codex CLI** — `npm install -g @openai/codex-cli` (or whatever the current install path is), then `codex login`
  - **Gemini CLI** — `npm install -g @google/gemini-cli`, then `gemini /auth`
  - **Claude Code CLI** — install via the Claude Code installer, then `claude /login`

All three CLIs default to your provider subscription (ChatGPT Plus/Pro, Gemini Advanced, Claude Pro/Max). API-key auth also works but isn't required.

You can run AI Switchboard with **zero** real CLIs installed — the `fake` adapter exists for testing the orchestrator without burning subscription quota.

## Setup (one time)

From the project directory:

```powershell
# Install Python dependencies
pip install -r requirements.txt

# Run the test suite to confirm everything wires up
python -m pytest
```

Expected: all tests pass (currently 75+). If pytest reports failures, see Troubleshooting below.

## Start the service

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Expected startup log lines:
```
Switchboard service started on 127.0.0.1:8787
Worker started; polling every 2 seconds.
Retention worker started. Budget: 2048 MB / 1000 tasks.
Uvicorn running on http://127.0.0.1:8787
```

Open **http://127.0.0.1:8787/** in your browser. You should see the AI Switchboard dashboard with a "New Task" form.

## First task (smoke test — no real CLI required)

The fastest way to confirm the orchestrator works is to submit a task using the `fake` adapter. The fake adapter is hidden from the dashboard UI by default but reachable from the API.

### Option A — From a terminal

```powershell
curl.exe -X POST http://127.0.0.1:8787/api/tasks `
  -H "Content-Type: application/json" `
  --data "@examples/task_request_fake.json"
```

You'll get back `{"task_id": "tsk_...", ...}`. Within 2 seconds, the worker claims it; within another 1–2 seconds, the fake adapter produces a deterministic response. Open the dashboard's Inbox tab and you'll see the task complete.

### Option B — From Claude Code

If you have Claude Code installed and configured with the Switchboard skill:

```
/conclave Should v1 ship with feature flags or skip them?
```

(Real CLIs required for this — see "Real conclave" below.)

## Real conclave (requires real CLIs)

Once Codex, Gemini, and Claude are all installed and authenticated:

1. **Verify each is reachable**:
   ```powershell
   curl.exe -X POST http://127.0.0.1:8787/api/agents/codex/test
   curl.exe -X POST http://127.0.0.1:8787/api/agents/gemini/test
   curl.exe -X POST http://127.0.0.1:8787/api/agents/claude-code/test
   ```
   Each should return `{"available": true, "version": "...", ...}`.

2. **Submit a 3-AI conclave** from the dashboard:
   - Open http://127.0.0.1:8787/
   - Click **New Task**
   - Mode: `conclave`
   - Agents: check `codex`, `gemini`, `claude-code`
   - Question: your prompt
   - (Optional) Set `Project path` and check `Provide a read-only sandbox copy` for code-review tasks
   - Submit

3. **Watch live activity**: open the task in the Detail view. You'll see *"Calling codex (round 1) — started 3s ago"* and the transcript populating as agents respond.

4. **Record your decision** once the conclave finishes (the "Your Decision" panel below the final result).

5. **Export to a decision record** if the task is worth keeping: click *"Export to decision record"* in the post-task bar. Writes to `data/exports/<task_id>.md`.

For the full workflow including thread continuation, see `docs/CODING_WORKFLOW.md`.

## Claude Code integration (recommended)

If you primarily code in Claude Code, install the slash commands so you can invoke the conclave by talking to me:

```
/conclave Should we use Postgres or MongoDB for v1?
/consult codex Is this refactor safe?
/decide latest "Going with Postgres. Decision recorded."
/decision latest
/continue latest "What about read replicas?"
/thread latest
/answer latest "ConnectionRefused on port 5432"
```

The slash commands live in `~/.claude/commands/`. The skill that triggers on natural-language phrases ("ask the conclave", "get a second opinion") lives in `~/.claude/skills/switchboard-conclave/`. Both are installed automatically if you've run the setup from this repo before; verify with `ls ~/.claude/skills/switchboard-conclave/` and `ls ~/.claude/commands/`.

## Configuration

- **`config.yaml`** at the project root — copy from `config.example.yaml` and edit. The service falls back to `config.example.yaml` if no `config.yaml` exists, so for default settings you don't need to copy anything.
- Common tweaks:
  - `retention.max_db_size_mb` — increase if you don't want the auto-trimmer running
  - `defaults.max_seconds` — total task time budget for resolve/conclave modes
  - `agents.<name>.command` — full path to a CLI if it's not on PATH

## One-click desktop launcher (optional)

Once the manual setup works, you can install a double-clickable launcher so you don't have to type the uvicorn command every time. The launcher starts the service in the background (no terminal window) and opens the dashboard tab once `/api/health` responds. Subsequent launches while the service is up just open a new tab — the pidlock prevents a second instance.

**Windows:**

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install-desktop-shortcut.ps1
```

Creates `AI Switchboard.lnk` on your Desktop pointing at `pythonw.exe launch.pyw` with the AI Conclave logo as the icon. Right-click the shortcut → *Pin to taskbar* if you want it on the taskbar.

**macOS:**

```bash
bash tools/install-desktop-app.sh
# Or install to ~/Applications instead of the Desktop:
INSTALL_DIR="$HOME/Applications" bash tools/install-desktop-app.sh
```

Builds an `AI Switchboard.app` bundle on your Desktop. Drag onto the Dock to pin. First launch on a Mac may show macOS's "Apple cannot check this app for malicious software" warning — right-click → *Open* → *Open* dismisses it permanently for your user.

Both installers are idempotent. The repo path is baked into the resulting shortcut/bundle, so if you move the repo, re-run the installer.

To stop the service: kill the python process (Task Manager on Windows, `pkill -f 'uvicorn app.main'` on macOS/Linux), or delete `<repo>/data/switchboard.pid`. Launcher events plus uvicorn's stdout/stderr land in `<repo>/data/launcher.log`.

## Where Switchboard stores its state

Per [DR0016](docs/decisions/0016_user_data_root_and_lazy_config.md), all writable runtime state — the SQLite database, sandboxes, uploads, exports, logs, and pidlock — resolves through a single `user_data_root()` primitive:

| Launch mode | Resolved root |
|---|---|
| **Dev / running from the repo** (cwd or ancestor has `pyproject.toml` + `config.example.yaml`) | `<repo>/data/` — same as before; nothing changes for contributors |
| **Packaged build on Windows** | `%LOCALAPPDATA%\AI Switchboard\` |
| **Packaged build on macOS** | `~/Library/Application Support/AI Switchboard/` |
| **Packaged build on Linux** | `$XDG_DATA_HOME/ai-switchboard/` (or `~/.local/share/ai-switchboard/`) |
| **Explicit override** | Whatever you set in `SWITCHBOARD_DATA_DIR` (test/CI/packager hook; wins over everything else) |

When the packaged app launches for the first time and finds a populated `./data/` directory next to it, a one-time non-destructive migration runs: the SQLite DB is transferred via `VACUUM INTO` (handles the WAL/SHM coherence trap), and `sandboxes/`, `exports/`, `uploads/` are copied verbatim. The originals at `./data/` are preserved — you can delete them manually once you've confirmed everything moved cleanly. If an old Switchboard instance is still running against `./data/` when the new build launches, migration refuses to start and prints a clear error; stop the old instance and retry.

Two env vars control this behavior:

- `SWITCHBOARD_DATA_DIR=<path>` — override the resolved root entirely (handy in tests, CI, and packager build scripts).
- `SWITCHBOARD_CONFIG=<path>` — override config-file discovery; otherwise the resolver looks at `<user_data_root>/config.yaml` (packaged) or `./config.yaml` / `./config.example.yaml` (dev).

## Troubleshooting

**`pytest` fails on first run**
- Check Python version: `python --version` (need 3.13+)
- Confirm dependencies installed: `pip list | grep -E "fastapi|pydantic|pypdf"`
- Check for stale `__pycache__/` directories — `python -m pytest --cache-clear`

**Service starts but `/api/health` returns connection refused**
- Confirm port 8787 isn't already in use: `Get-NetTCPConnection -LocalPort 8787`
- If something else is holding it, either stop that process or pass `--port 8788`

**An agent test endpoint returns `available: false`**
- For `codex` / `gemini` / `claude-code`: confirm the CLI is on PATH (`where.exe codex` on Windows, `which codex` on Unix)
- If the CLI is on PATH but the adapter still reports unavailable, check authentication: run `codex --version` (or equivalent) directly and confirm it doesn't prompt for login

**Codex/Gemini/Claude calls fail with `agent_error: could not extract JSON`**
- Most often: the CLI updated and changed its output format. Open the agent's adapter file (`app/agents/codex_adapter.py` etc.) and verify the parsing logic still matches what the CLI emits
- Less commonly: the prompt is so long the model gave up — try with a smaller `project_path` scope or shorter question

**Claude Code says "Not logged in" when invoked via the adapter**
- The Switchboard adapter uses your OAuth session by default. If you're seeing this error, confirm `claude /login` is current
- The adapter does NOT use `--bare` (which would require `ANTHROPIC_API_KEY`); it relies on OAuth

**Dashboard loads but shows no agents**
- Check `GET /api/agents` directly. If it returns `{"agents": []}`, none of your real CLIs are reachable — install at least one or use the `fake` adapter via API directly

**A task is stuck in `running` indefinitely**
- The worker probably crashed. Restart the service. On startup, the orphan sweep cleans up sandboxes, but the task itself stays in `running` — you'd have to manually update SQLite or just leave it as historical noise
- A planned future feature (see `docs/ROADMAP.md`) handles stuck-task recovery

**Sandboxed task fails because the project is huge**
- The sandbox cap is 200 MiB. If your project exceeds that after standard ignore patterns, scope `project_path` to a subdirectory
- Check the service logs: a "cap reached" line shows when the sandbox was truncated

## Where to go next

- **`README.md`** — the elevator-pitch overview and quick start
- **`docs/CODING_WORKFLOW.md`** — the canonical four-step loop for coding work
- **`docs/SWITCHBOARD_PROTOCOL.md`** — wire-format / mode definitions
- **`docs/CONCLAVE_CHARTER.md`** — the binding agreement (v1.1) embedded in every prompt
- **`docs/decisions/INDEX.md`** — every ratified design decision with context
- **`docs/ROADMAP.md`** — shipped, next, and intentionally not built
