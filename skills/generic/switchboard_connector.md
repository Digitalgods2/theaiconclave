# Switchboard Connector

Use AI Switchboard when a task would benefit from another AI agent's review, critique, or alternate reasoning. This skill tells you how to call it.

## When to Use

Invoke Switchboard when:

- The user explicitly asks to consult another AI ("ask Codex", "second opinion", "what would Gemini say").
- The problem is complex, high-stakes, or affects code, files, deployment, security, or data.
- You are uncertain and a second opinion would reduce risk.
- You have just produced an answer that the user pushed back on, and re-thinking alone has not resolved it.

Do **not** invoke Switchboard for trivial tasks (typo fixes, formatting, single-variable renames). The latency cost outweighs the value.

For trigger phrases and disambiguation rules, see `user_invocation_triggers.md` and `role_disambiguation.md`.

## Endpoint

Default local endpoint:

```
POST http://127.0.0.1:8787/api/tasks
```

No authentication is required in MVP — Switchboard binds to localhost only.

## Request Format

Send a JSON body conforming to the `TaskRequest` schema in `docs/SWITCHBOARD_PROTOCOL.md`. Required fields:

- `protocol_version` — `"1.0"`
- `source` — your channel: `api`, `cli`, etc.
- `source_agent` — your canonical agent name
- `mode` — `consult`, `handoff`, or `poll`
- `task_type` — `debug`, `code_review`, `architecture_review`, `security_review`, `general_consultation`, etc.
- `user_request` — the user's actual question, verbatim where possible
- `primary_agent` — required for `consult` and `handoff`; omit for `poll`
- `consultants` — array of agent names; non-empty for `consult` and `poll`
- `permissions` — all eight booleans, explicit. **Inherit from the current session; do not escalate.**
- `limits` — at minimum `max_rounds` and `timeout_seconds`

Pass the user's `project_path` if the task is grounded in a specific codebase. Switchboard's context manager will collect file content; you do not need to send raw file bodies.

## Response Format

`POST /api/tasks` returns immediately:

```json
{"task_id": "tsk_...", "status": "pending"}
```

Poll `GET /api/tasks/{task_id}` until `status` is one of `completed`, `failed`, `cancelled`, or `waiting_for_user`. The full result lives in the `final_result` field of the GET response, conforming to the `FinalResult` schema.

If `status` is `waiting_for_user`, surface the pending approvals (their IDs are in the response) and wait for the user to resolve them via the dashboard.

## Required Behavior

- Send a structured task request. Never send free-form prose.
- **Do not send secrets** unless the user has explicitly granted `can_read_secrets`. The context manager strips known patterns regardless.
- **Do not request file writes** unless the task carries `can_write_files: true`.
- **Do not request command execution** unless the task carries `can_run_commands: true`.
- When you receive a `final_result`, surface it to the user in full. Do not silently substitute the consultant's answer for your own — name who said what.
- If the result includes `disagreements`, show them. Do not flatten them into "we agreed."

## Authority

The calling agent or the user-selected primary agent has final authority on the answer. Switchboard mediates; it does not arbitrate. Consultants critique; primaries decide. The user overrides either at any time.
