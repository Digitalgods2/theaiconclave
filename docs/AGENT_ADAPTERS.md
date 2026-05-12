# Agent Adapters

Adapters are the *only* place in the codebase that knows how to invoke a specific AI tool. Everything else — the orchestrator, the result builder, the API — calls agents through the `BaseAdapter` interface defined here. If a feature requires the orchestrator to know which agent it's talking to, the adapter abstraction has leaked.

## 1. The Interface

```python
from app.protocol.validators import (
    PrimaryResponse, ConsultantCritique, PeerAnswer, TaskRequest, Permissions
)

class AdapterContext(BaseModel):
    task: TaskRequest
    task_id: str
    prior_messages: list[dict]   # serialized protocol messages, chronological
    permissions: Permissions
    timeout_seconds: int
    working_directory: str       # absolute path

class AdapterTestResult(BaseModel):
    available: bool
    version: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: int

class BaseAdapter:
    name: str  # canonical, e.g. "codex", "claude-code"

    async def is_available(self) -> bool: ...
    async def test_connection(self) -> AdapterTestResult: ...
    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse: ...
    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique: ...
    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse: ...
    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer: ...
```

`run_primary` returns a `PrimaryResponse` with `message_type=primary_proposal`.
`run_final` returns a `PrimaryResponse` with `message_type=primary_final`.
The shape is identical; only the type tag differs.

## 2. The Adapter Contract

### Inputs

Every method receives an `AdapterContext` carrying the validated task, prior messages, resolved permissions, the per-call timeout, and the working directory. Adapters must not reach for global state — everything they need is in the context.

### Outputs

Every method returns a fully-validated protocol message. Adapters that cannot produce a valid response raise `AdapterError`, never return malformed data.

### Hard rules

1. **Never write files or run host commands directly.** Adapters call CLI subprocesses; those subprocesses are governed by the safety layer.
2. **Never mutate `prior_messages` or `task`.** Treat them as read-only.
3. **Honor the timeout.** If the underlying CLI exceeds `timeout_seconds`, the adapter terminates the process group and raises `AdapterError(code="agent_timeout")`.
4. **Parse defensively.** If the CLI returns prose instead of structured JSON, the adapter either reformats it or raises `AdapterError(code="agent_error")`. Silently inventing structure is forbidden.
5. **Never retry.** The orchestrator owns retry policy.

## 3. Prompt Construction

Adapters assemble prompts from three pieces:

1. **Role frame** — system instructions describing the agent's role on this task. The skill files (`skills/generic/primary_agent_behavior.md`, `consultant_behavior.md`, etc.) are the source of truth; adapters embed the relevant content verbatim.
2. **Task framing** — `user_request`, `task_type`, and the relevant fields from `context`.
3. **Prior messages** — for consultant and final rounds, the prior proposal/critique so the agent can react to it.

Every prompt ends with: *"Return a single JSON object matching the schema. No prose before or after the JSON. No markdown code fences."*

If the CLI insists on wrapping output in fences anyway, the adapter strips them before parsing. The helper `app.utils.json_tools.extract_json_object(text)` handles fence-stripping and tolerates leading/trailing prose.

## 4. Subprocess Invocation

Adapters use `app.utils.subprocess_runner.run(...)` rather than calling `subprocess` or `asyncio.create_subprocess_exec` directly. The runner provides:

- Timeout enforcement that kills the *process group*, not just the parent
- Stdout/stderr capture with a 4 MiB cap and `[OUTPUT TRUNCATED]` marker
- Working-directory pinning (no CWD inheritance from the parent)
- Sanitized environment — no inherited proxy vars when `can_access_network` is false
- Structured exit-code result

This is non-negotiable. Direct subprocess calls bypass the safety layer.

## 5. Connection Test

Every adapter implements `test_connection()` returning an `AdapterTestResult`. It must be:

- **Cheap** (sub-second when possible)
- **Side-effect free** (no file writes, no network beyond the CLI's own startup)
- **Honest about failure** — populate `error` rather than swallowing exceptions

A typical implementation invokes the CLI's `--version` flag, parses the output, and returns. The orchestrator calls this on startup and surfaces unavailable agents to the dashboard.

## 6. Error Handling

Adapters raise `AdapterError(code, message, details)` where `code` is one of:

- `agent_timeout` — CLI exceeded `timeout_seconds`
- `agent_unavailable` — adapter is disabled or `is_available()` was false
- `agent_error` — CLI returned non-zero exit, unparseable output, or schema-invalid JSON

The orchestrator converts these to `ProtocolError` entries on the task.

## 7. Per-Adapter Notes

### `codex_adapter`
- Path configurable via `agents.codex.command`
- Invocation pattern: pass prompt via stdin, request JSON output via flags. Exact flag set must be confirmed against the installed Codex CLI version.
- Output: streamed JSON; adapter buffers and parses on completion.
- Known quirk: chunk markers may appear in output; `extract_json_object` strips them.

### `claude_adapter`
- Path configurable via `agents.claude-code.command`
- Invocation pattern: prompt via stdin, response via stdout.
- Requires the matching skill (`skills/claude-code/claude_switchboard_skill.md`) to be installed in the user's Claude Code config so the JSON output requirement is honored. If the response is prose, the adapter raises `agent_error` rather than guessing.
- Known quirk: reasoning text may precede the JSON; the adapter extracts the trailing JSON object.

### `gemini_adapter`
- Path configurable via `agents.gemini.command`
- MVP status: stub returning `agent_unavailable`
- Notes: JSON-mode behavior varies by model. The adapter must specify the model explicitly and not rely on defaults.

### `openclaw_adapter`
- MVP status: stub returning `agent_unavailable`
- Eventual support: gateway mode (OpenClaw routes to provider X), agent mode (OpenClaw runs a local agent), OAuth-backed providers, configured model aliases.

### `ollama_adapter`
- Targets a local Ollama HTTP endpoint (default `http://127.0.0.1:11434`)
- MVP status: stub returning `agent_unavailable`
- Use cases: cheap consult passes, privacy-sensitive tasks, offline operation.
- Notes: smaller models often produce malformed JSON. The adapter validates strictly and refuses to fabricate structure on parse failure.

### `fake_adapter`
- Returns canned, deterministic responses keyed by task type.
- Used by the test suite to exercise the orchestrator without real CLIs.
- Configurable via test-only env vars to simulate `agent_timeout`, malformed output, and loop conditions (identical responses on consecutive rounds).

## 8. Adding a New Adapter

1. Implement `BaseAdapter` in `app/agents/<tool>_adapter.py`.
2. Register it in `app/services/agent_registry.py` with supported task types and modes.
3. Add a config block under `agents.<tool>` in `config.example.yaml`.
4. Add a per-adapter section to this document.
5. Add an integration test in `tests/test_<tool>_adapter.py` using the fake CLI fixture.

The orchestrator must not need any changes to support a new adapter. If you find yourself touching the orchestrator to wire one in, the interface is leaking.
