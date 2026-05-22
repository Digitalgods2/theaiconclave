# The AI Conclave Switchboard Protocol

The wire format for messages flowing through The AI Conclave Switchboard. Every task request, agent message, and final result conforms to the schemas below. Storage layout lives in `DATABASE_SCHEMA.md`; HTTP routes live in `API_REFERENCE.md`. This file defines only the *shape* of the data on the wire.

## 1. Design Principles

- **Structured, not free-form.** Every message is JSON with named fields. No agent should ever return prose-only output that the orchestrator has to reverse-engineer.
- **Explicit role labels.** Every message names its sender, its role for this task, and its message type. The orchestrator never has to guess "is this a critique or a final answer?"
- **Disagreement is a first-class value.** The final result contains a structured `disagreements` list. The orchestrator must not flatten it into a single sentence.
- **Permissions travel with the task.** Agents do not infer what they're allowed to do. Permissions are declared on the task request and inherited by every downstream message.
- **The protocol is versioned.** See section 2.

## 2. Versioning

Every top-level message carries `protocol_version` as a `MAJOR.MINOR` string. Current version: `1.1`.

- **MINOR bump** — additive only (new optional fields). Older clients ignore unknown fields.
- **MAJOR bump** — breaking. The AI Conclave Switchboard rejects mismatched majors with error `protocol_version_mismatch`.

## 3. Common Enums

### Status (task)
`pending` · `running` · `waiting_for_user` (action approval) · `awaiting_user_input` (info needed from the user) · `completed` · `failed` · `cancelled`

### Mode (task)
`resolve` — **default for non-trivial tasks.** Open-ended primary-driven loop until the primary signals `resolved` or `cannot_resolve`, with cost/time/repetition backstops. The primary may pause to ask the user a question (`needs_user_input`) and resume after the user answers.
`consult` — bounded second opinion: primary proposes, consultants critique, primary finalizes. If the primary or consultants surface clarification questions, the orchestrator may pause once with a numbered questionnaire and resume after the user answers. Use when you want a quick review, not full deliberation.
`conclave` — **N equal participants, full-mesh visibility.** No primary. Each round, every participant contributes one `ConclaveTurn` with their current `position` and a `convergence` signal. Terminates when at least `convergence_threshold` fraction of participants signal `i_am_done` (default 1.0 = unanimous). The orchestrator never picks a winner; on weak convergence it surfaces every position to the user.
`handoff` — named agent is primary; the calling agent is consultant or absent.
`poll` — each agent answers independently. No critique loop, no primary.

### Role (per agent on a task)
`primary` · `consultant` · `peer` (poll mode only)

### Message type
`primary_proposal` · `consultant_critique` · `primary_final` · `peer_answer` · `conclave_turn` · `user_input_request` · `user_input_response` · `error`

### Role
`primary` · `consultant` · `peer` · `participant` (conclave only)

### Resolution status (resolve mode primary)
`resolved` · `needs_more_rounds` · `needs_user_input` · `cannot_resolve`

### Conclave convergence (conclave mode participant)
`i_am_done` · `still_thinking` · `need_user_input`

### Confidence
A float in `[0.0, 1.0]`. Agents may also send `null` if unable to estimate.

### Agreement level (final result only)
`consensus` · `minor_disagreement` · `major_disagreement` · `unresolved`

### Action type (structured action plan)
`read_file` - `write_file` - `run_command` - `install_package` - `apply_patch` - `network_access` - `deployment_change` - `secret_access` - `human_decision` - `unknown`

### Policy status (structured action plan)
`allowed` - `needs_approval` - `blocked` - `unknown`

## 4. Task Request

Sent by a caller (dashboard, agent, webhook) to create a new task.

```json
{
  "protocol_version": "1.0",
  "source": "dashboard",
  "source_agent": "claude-code",
  "mode": "consult",
  "task_type": "debug",
  "user_request": "Find out why this FastAPI app crashes on startup.",
  "primary_agent": "codex",
  "consultants": ["claude-code"],
  "project_path": "C:/projects/myapp",
  "context": {
    "files": ["app/main.py", "requirements.txt"],
    "error": "ModuleNotFoundError: No module named 'pydantic'",
    "git_diff": null,
    "extra": {}
  },
  "permissions": {
    "can_read_files": true,
    "can_write_files": false,
    "can_run_commands": false,
    "can_access_network": false,
    "can_install_packages": false,
    "can_apply_patches": false,
    "can_read_env_files": false,
    "can_read_secrets": false
  },
  "limits": {
    "max_rounds": 50,
    "timeout_seconds": 180,
    "max_seconds": 600,
    "max_context_tokens": null
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `protocol_version` | yes | `MAJOR.MINOR` |
| `source` | yes | Origin channel: `dashboard`, `api`, `webhook`, `cli`, `watcher` |
| `source_agent` | no | The AI agent that submitted the task, if any |
| `mode` | yes | One of `resolve`, `consult`, `handoff`, `poll` |
| `task_type` | yes | `debug`, `code_review`, `architecture_review`, `security_review`, `deployment_help`, `documentation`, `general_consultation` |
| `user_request` | yes | The verbatim question or instruction |
| `primary_agent` | conditional | Required for `resolve`, `consult`, `handoff`. Omitted for `poll`. |
| `consultants` | conditional | Array of agent names. Required for `consult` (≥1) and `poll` (≥2). Optional in `resolve` and `handoff`. |
| `project_path` | no | Absolute path; gates file access |
| `context` | no | Compact, relevant context. Free-form sub-object; the orchestrator does not interpret `extra`. |
| `permissions` | yes | All eight booleans must be present and explicit |
| `limits` | yes | `max_rounds` (backstop in resolve, primary cap in consult), `timeout_seconds` (per agent call), `max_seconds` (total task time, used by resolve mode) |

## 5. Agent Response (Primary)

Returned by the primary agent in response to the initial task or to a consultant critique.

```json
{
  "protocol_version": "1.0",
  "task_id": "tsk_01HX...",
  "agent": "codex",
  "role": "primary",
  "message_type": "primary_proposal",
  "summary": "Likely missing dependency: pydantic.",
  "analysis": "The traceback indicates...",
  "recommended_actions": [
    {
      "kind": "install_package",
      "description": "Install pydantic in the active virtualenv",
      "requires_approval": true,
      "payload": {"command": "python -m pip install pydantic"}
    }
  ],
  "risks": [
    {"severity": "low", "description": "May install into wrong interpreter if venv is not active."}
  ],
  "confidence": 0.7,
  "resolution_status": "needs_more_rounds",
  "user_input_question": null
}
```

In **consult mode**, `message_type` is `primary_proposal` for the first response and `primary_final` for the final after consultation. `resolution_status` is optional and ignored.

In **resolve mode**, `message_type` stays as `primary_proposal` for every primary turn and `resolution_status` is **required** — it drives the loop:
- `resolved` — primary believes the task is done (consultants get one more round to push back)
- `needs_more_rounds` — primary explicitly wants another iteration
- `needs_user_input` — primary cannot proceed without info from the user; `user_input_question` is then required
- `cannot_resolve` — primary determined the task cannot be solved with available tools/permissions/info; loop terminates immediately

## 6. Consultant Critique

Returned by a consultant after seeing the primary's proposal.

```json
{
  "protocol_version": "1.0",
  "task_id": "tsk_01HX...",
  "agent": "claude-code",
  "role": "consultant",
  "message_type": "consultant_critique",
  "agreement": "partial",
  "critique": "The fix addresses the symptom but not the cause. The traceback suggests the wrong Python interpreter is active.",
  "missed_risks": [
    "Installing pydantic globally instead of in the project venv would mask the underlying environment issue."
  ],
  "suggested_questions": [
    "Is the project's virtualenv currently activated?",
    "Does requirements.txt pin pydantic to a specific version?"
  ],
  "confidence": 0.8,
  "wants_continuation": true
}
```

`agreement`: `agree` · `partial` · `disagree`. Required.

`wants_continuation` (resolve mode): `true` if this consultant believes another primary round would meaningfully improve the answer. When the primary returns `resolved` and any consultant sets `wants_continuation: true`, the orchestrator runs another primary round. Defaults to `false`.

In **consult mode**, `suggested_questions` are also used by the clarification gate. The orchestrator deduplicates the primary's `user_input_question` and all consultant `suggested_questions`, records one numbered `user_input_request`, sets the task to `awaiting_user_input`, and resumes final synthesis after `/api/tasks/{id}/answer`.

## 7. Peer Answer (Poll Mode)

Returned by each peer in poll mode. No critique, no primary, no rounds.

```json
{
  "protocol_version": "1.0",
  "task_id": "tsk_01HX...",
  "agent": "gemini",
  "role": "peer",
  "message_type": "peer_answer",
  "summary": "...",
  "analysis": "...",
  "recommended_actions": [],
  "risks": [],
  "confidence": 0.6
}
```

## 8. Final Result

Built by the result builder and returned to the caller.

```json
{
  "protocol_version": "1.0",
  "task_id": "tsk_01HX...",
  "status": "completed",
  "mode": "consult",
  "primary_agent": "codex",
  "consultants": ["claude-code"],
  "final_answer": "Activate the project venv, then run python -m pip install -r requirements.txt, then verify pydantic exists in the same interpreter.",
  "agreement_level": "minor_disagreement",
  "disagreements": [
    {
      "topic": "Scope of fix",
      "primary_position": "Install the missing package directly.",
      "consultant_position": "First confirm the correct interpreter is active; the missing package is a symptom."
    }
  ],
  "recommended_actions": [
    {
      "kind": "run_command",
      "description": "Install dependencies into the project venv.",
      "requires_approval": true,
      "payload": {"command": "python -m pip install -r requirements.txt"}
    }
  ],
  "action_plan": [
    {
      "step_number": 1,
      "action_type": "install_package",
      "summary": "Install dependencies into the project venv.",
      "target": "python -m pip install -r requirements.txt",
      "source_action_kind": "run_command",
      "required_permissions": ["can_install_packages", "can_run_commands", "can_access_network"],
      "policy_status": "needs_approval",
      "policy_reasons": ["Package installation always requires approval."],
      "payload": {"command": "python -m pip install -r requirements.txt"}
    }
  ],
  "commands_requiring_approval": ["python -m pip install -r requirements.txt"],
  "patches_requiring_approval": [],
  "risks": [
    {"severity": "low", "description": "May install into wrong interpreter if venv is not active."}
  ],
  "errors": []
}
```

`disagreements` MUST contain every disagreement raised by any consultant that the primary did not explicitly accept. Do not summarize. Do not omit "minor" disagreements. The user reads this list to decide whether the consensus is real.

`action_plan` is the Structured Action Plan. It is compiled from the final synthesized response's `recommended_actions` in `consult` and `resolve` modes. In v1 it is advisory only: it makes the operational handoff legible and permission-aware, but it does not execute actions, create approvals, pause tasks, or remove blocked steps. `conclave` mode returns an empty action plan until a future protocol revision gives the final synthesized answer structured recommended actions.

`recommended_actions`, `commands_requiring_approval`, and `patches_requiring_approval` remain in the final result for backward compatibility. New clients should present `action_plan` as the primary user-facing action artifact when it is non-empty.

## 9. Draft Artifacts

When final `recommended_actions` include draftable file operations, The AI Conclave Switchboard may preserve them as task-scoped artifacts under the runtime data root. These are operational handoff material, not agent writes to the user's project.

Supported v1 captures:

- `create_file` / `write_file` with `payload.path` and `payload.content` become `file` artifacts.
- `edit_file` with `payload.path`, `payload.search`, and `payload.replace` becomes an `edit` artifact.
- Patch-like actions with `payload.patch` or `payload.diff` become review/download-only `patch` artifacts.

Task detail responses include `artifacts: [...]` with metadata and text previews. The task API also exposes:

- `GET /api/tasks/{task_id}/artifacts`
- `GET /api/tasks/{task_id}/artifacts/{artifact_id}/download`
- `POST /api/tasks/{task_id}/artifacts/{artifact_id}/apply`

Applying an artifact is explicit user action. It writes only inside the task's `project_path`; `file` artifacts write the target file, and `edit` artifacts perform one search/replace. Patch artifacts remain review/download-only in v1.

## 10. Errors

Errors are objects, not strings.

```json
{
  "code": "agent_timeout",
  "message": "Consultant 'gemini' did not respond within 180 seconds.",
  "details": {"agent": "gemini", "elapsed_ms": 180000}
}
```

Stable error codes:

| Code | Meaning |
|---|---|
| `protocol_version_mismatch` | Major version not supported |
| `agent_unavailable` | Named agent is disabled or failed connection test |
| `agent_timeout` | Agent exceeded `limits.timeout_seconds` |
| `agent_error` | Adapter returned a non-zero exit or unparseable output |
| `permission_denied` | Action requires a permission that the task does not grant |
| `approval_required` | Task is paused awaiting user approval |
| `rounds_exhausted` | Debate hit `limits.max_rounds` without convergence |
| `loop_detected` | Agents repeating prior content; orchestrator stopped |
| `invalid_request` | Task request failed schema validation |
| `resolve_timeout` | Resolve loop exceeded `limits.max_seconds` |

## 11. Status Transitions

```
pending → running → completed
                 ↘ failed
                 ↘ cancelled
                 ↘ waiting_for_user      → running → ...    (action approval)
                 ↘ awaiting_user_input   → pending → running → ...  (user clarification)
```

`waiting_for_user` and `awaiting_user_input` are both reachable from `running` only. They differ in semantics:
- `waiting_for_user` — orchestrator paused on an approval gate (action requires user OK).
- `awaiting_user_input` — the primary or consultants asked the user a clarifying question; resumes when the user POSTs an answer to `/api/tasks/{id}/answer`, which moves the task back to `pending` for the worker to re-claim.
