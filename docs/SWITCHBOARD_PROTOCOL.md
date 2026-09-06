# The AI Conclave Switchboard Protocol

The wire format for messages flowing through The AI Conclave Switchboard. Every task request, agent message, and final result conforms to the schemas below. Storage layout lives in `DATABASE_SCHEMA.md`; HTTP routes live in `API_REFERENCE.md`. This file defines only the *shape* of the data on the wire.

## 1. Design Principles

- **Structured, not free-form.** Every message is JSON with named fields. No agent should ever return prose-only output that the orchestrator has to reverse-engineer.
- **Explicit role labels.** Every message names its sender, its role for this task, and its message type. The orchestrator never has to guess "is this a critique or a final answer?"
- **Disagreement is a first-class value.** The final result contains a structured `disagreements` list. The orchestrator must not flatten it into a single sentence.
- **Permissions travel with the task.** Agents do not infer what they're allowed to do. Permissions are declared on the task request and inherited by every downstream message.
- **The protocol is versioned.** See section 2.

## 2. Versioning

Every top-level message carries `protocol_version` as a `MAJOR.MINOR` string. Current version: `1.2`.

- **MINOR bump** — additive only (new optional fields). Older clients ignore unknown fields.
- **MAJOR bump** — breaking. The AI Conclave Switchboard rejects mismatched majors with error `protocol_version_mismatch`.

## 3. Common Enums

### Status (task)
`pending` · `running` · `waiting_for_user` (reserved; not currently reached) · `awaiting_user_input` (info needed from the user) · `completed` · `failed` · `cancelled`

### Mode (task)
`resolve` — **default for non-trivial tasks.** Open-ended primary-driven loop until the primary signals `resolved` or `cannot_resolve`, with cost/time/repetition backstops. The primary may pause to ask the user a question (`needs_user_input`) and resume after the user answers.
`consult` — bounded second opinion: primary proposes, consultants critique, primary finalizes. If the primary or consultants surface clarification questions, the orchestrator may pause once with a numbered questionnaire and resume after the user answers. Use when you want a quick review, not full deliberation.
`conclave` — **N equal participants, full-mesh visibility.** No primary. Each round, every participant contributes one `ConclaveTurn` with their current `position` and a `convergence` signal. Terminates when at least `convergence_threshold` fraction of participants signal `i_am_done` (default 1.0 = unanimous). The orchestrator never picks a winner; on weak convergence it surfaces every position to the user.

### Role (per agent on a task)
`primary` · `consultant`

### Message type
`primary_proposal` · `consultant_critique` · `primary_final` · `conclave_turn` · `user_input_request` · `user_input_response` · `error`

### Role
`primary` · `consultant` · `participant` (conclave only)

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
    "notify_after_seconds": 180,
    "max_context_tokens": null
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `protocol_version` | yes | `MAJOR.MINOR` |
| `source` | yes | Origin channel: `dashboard`, `api`, `webhook`, `cli`, `watcher` |
| `source_agent` | no | The AI agent that submitted the task, if any |
| `mode` | yes | One of `resolve`, `consult`, `conclave` |
| `task_type` | yes | `debug`, `code_review`, `architecture_review`, `security_review`, `deployment_help`, `documentation`, `general_consultation` |
| `user_request` | yes | The verbatim question or instruction |
| `primary_agent` | conditional | Required for `resolve` and `consult`. Omitted for `conclave`. |
| `consultants` | conditional | Array of agent names. Required for `consult` (≥1) and `conclave` (≥2). Optional in `resolve`. |
| `project_path` | no | Absolute path; gates file access |
| `decision_project_id` | no | Persistent Decision Project that contributes instructions and frozen project evidence |
| `judge_agent` | no | Optional semantic-equivalence judge; must not be a participant |
| `synthesis_agent` | no | Optional final synthesizer; must not be a participant |
| `context` | no | Compact, relevant context. Free-form sub-object; the orchestrator does not interpret `extra`. |
| `permissions` | yes | All eight booleans must be present and explicit |
| `limits` | yes | `max_rounds` (round cap), `notify_after_seconds` (preferred elapsed-time notification threshold; legacy `timeout_seconds` / `max_seconds` are accepted aliases) |

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

## 7. Final Result

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
  "errors": [],
  "failure_cause_tags": ["clarification_unanswered"]
}
```

`disagreements` MUST contain every disagreement raised by any consultant that the primary did not explicitly accept. Do not summarize. Do not omit "minor" disagreements. The user reads this list to decide whether the consensus is real.

`failure_cause_tags` (since protocol 1.2 / DR0022) is a structured list of `FailureCause` values stamped by the orchestrator's post-finalize hook describing *why the deliberation was hard*. Members: `missing_evidence`, `tool_timeout`, `bad_json_output`, `premise_conflict`, `multimodal_perception_split`, `unresolved_dissent`, `repetition_loop_backstop`, `clarification_unanswered`, `permission_denied`. Empty list = the deliberation ran clean. Classification is rule-based (no LLM call) and is performed by `app/services/trace_analyzer.py` after the terminal-status flip.

`action_plan` is the Structured Action Plan. It is compiled from the final synthesized response's `recommended_actions` in `consult` and `resolve` modes. In v1 it is advisory only: it makes the operational handoff legible and permission-aware, but it does not execute actions, create approvals, pause tasks, or remove blocked steps. `conclave` mode returns an empty action plan until a future protocol revision gives the final synthesized answer structured recommended actions.

`recommended_actions`, `commands_requiring_approval`, and `patches_requiring_approval` remain in the final result for backward compatibility. New clients should present `action_plan` as the primary user-facing action artifact when it is non-empty.

## 8. Draft Artifacts

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

Application requires a prior `GET .../apply-preview`, then a POST body containing `confirm: true`, the returned `expected_target_sha256`, and `allow_overwrite: true` only for a reviewed existing-file overwrite. A target change invalidates the preview.

## Frozen evidence and citations

Task context may contain `extra.evidence_snapshot_ids`, populated by the evidence API or inherited from a Decision Project. The orchestrator resolves these to immutable snapshots and presents the same bounded content to every seat. Primary, consultant, and conclave-turn responses may return `citation_ids`. The final result resolves valid IDs to citation records and reports unavailable, uncited, and invalid IDs in `citation_coverage`.

## 9. Errors

Errors are objects, not strings.

```json
{
  "code": "agent_timeout",
  "message": "Consultant 'gemini' has been running for 180 seconds.",
  "details": {"agent": "gemini", "elapsed_ms": 180000, "notification": true}
}
```

Stable error codes:

| Code | Meaning |
|---|---|
| `protocol_version_mismatch` | Major version not supported |
| `agent_unavailable` | Named agent is disabled or failed connection test |
| `agent_timeout` | An individual adapter call timed out at the transport level; elapsed-time notification thresholds do not auto-fail tasks |
| `agent_error` | Adapter returned a non-zero exit or unparseable output |
| `permission_denied` | Action requires a permission that the task does not grant |
| `approval_required` | Reserved compatibility code for a future executable approval gate |
| `rounds_exhausted` | Debate hit `limits.max_rounds` without convergence |
| `loop_detected` | Agents repeating prior content; orchestrator stopped |
| `invalid_request` | Task request failed schema validation |
| `resolve_timeout` | Legacy compatibility code for an explicit resolve timeout; current elapsed-time thresholds only notify and never auto-fail a job |

## 10. Status Transitions

```
pending → running → completed
                 ↘ failed
                 ↘ cancelled
                 ↘ waiting_for_user      → running → ...    (reserved; not implemented)
                 ↘ awaiting_user_input   → pending → running → ...  (user clarification)
```

`waiting_for_user` and `awaiting_user_input` are both reachable from `running` only. They differ in semantics:
- `waiting_for_user` — reserved protocol state; current production code does not create approval rows or enter it.
- `awaiting_user_input` — the primary or consultants asked the user a clarifying question; resumes when the user POSTs an answer to `/api/tasks/{id}/answer`, which moves the task back to `pending` for the worker to re-claim.


## Decision-value and project workflow additions

- `PUT /api/tasks/{id}/feedback`: full idempotent upsert of nullable booleans
  `decision_changed`, `material_risk_found`, `extra_review_worth_it`, plus `note` (max
  4,000 chars). Existing terminal task required (404 missing; 409 active; 422 invalid).
  This operation does not enqueue work or alter task status.
- `GET /api/tasks/{id}` adds `feedback` and computed `compute_summary`.
- `GET /api/metrics` returns aggregates for each mode and denominator definitions.
  It contains no task content, identifiers, decisions, or feedback notes. Completion
  rate uses terminal tasks; value rate uses feedback rows; unknown costs are not zero.
- `POST /api/tasks/{id}/retry`: failed/cancelled and fully stopped tasks only. Creates
  a pending linked task with preserved input context, permissions, and review seats.
  The original task is unchanged; 409 when retry would race active execution.
- `context.extra.evidence_snapshot_ids` explicitly selects evidence. An explicit empty
  array selects none; omission retains the legacy latest-project-evidence behavior.
- Project PATCH fields may be omitted but cannot be null. Names are trimmed and
  cannot be blank. Archived projects remain inspectable and can be restored.
- Citation coverage includes `presented_excerpts` with per-source hashes, sizes,
  truncation and omission flags. Stored text and presented text have distinct hashes.

All API operations use the configured token through `X-Conclave-Token` or Bearer auth.
