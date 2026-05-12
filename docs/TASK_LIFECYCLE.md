# Task Lifecycle

Defines the state machine every task moves through, including who flips each transition and under what conditions. The protocol enum (`TaskStatus`) names the states; this document defines the transitions.

## States

| State | Meaning |
|---|---|
| `pending` | Task created (or resumed from user input), waiting for the worker to claim it. |
| `running` | Worker has claimed the task; orchestrator is calling agents. |
| `waiting_for_user` | Orchestrator paused for **action approval**; one or more `approvals` rows in `pending` status. |
| `awaiting_user_input` | Resolve-mode primary asked the user a **question**; pauses until the user POSTs an answer. |
| `completed` | Final result built and persisted. |
| `failed` | Unrecoverable error; no usable final result. |
| `cancelled` | User cancelled before completion. |

## Diagram

```
                ┌─────────────┐
                │   pending   │
                └──┬───────┬──┘
         cancel    │       │   worker claim
        ┌─────────┘        ▼
        ▼              ┌──────────┐
  ┌───────────┐        │ running  │
  │ cancelled │◄───────┤          │
  └───────────┘        └──┬─────┬─┘
                          │     │
              approval    │     │   final result
              required    │     │   built / fatal error
                          ▼     ▼
              ┌────────────────┐  ┌─────────────┐
              │waiting_for_user│  │ completed   │
              └────────┬───────┘  │     or      │
                       │          │   failed    │
                       │ all      └─────────────┘
                       │ approvals
                       │ resolved
                       ▼
                  ┌─────────┐
                  │ running │
                  └─────────┘
```

## Transitions

### `pending → running`
- **Trigger**: worker claim (atomic `UPDATE tasks SET status='running' ... RETURNING id`).
- **Side effects**: `tasks.status = 'running'`, `tasks.updated_at = now`.

### `pending → cancelled`
- **Trigger**: `POST /api/tasks/{id}/cancel`.
- **Side effects**: `tasks.status = 'cancelled'`. No agents are called.

### `running → waiting_for_user`
- **Trigger**: orchestrator wrote one or more `approvals` rows with `status = 'pending'` for the current task.
- **Side effects**: orchestrator suspends agent calls until resolution.

### `running → completed`
- **Trigger**: orchestrator finished all rounds and built a `FinalResult` with no fatal errors.
- **Side effects**: `final_results` row inserted; `tasks.status = 'completed'`.

### `running → failed`
- **Trigger**: orchestrator hit an unrecoverable error (exception escaped the orchestrator, all agents unavailable, schema validation impossible).
- **Side effects**: `tasks.error_message` set; `tasks.status = 'failed'`. A partial `final_results` row may exist with `errors` populated.

### `running → cancelled`
- **Trigger**: `POST /api/tasks/{id}/cancel` while running.
- **Side effects**: in MVP the in-flight adapter call is allowed to finish; no further calls are made; `tasks.status = 'cancelled'`.

### `waiting_for_user → running`
- **Trigger**: every `pending` approval for the task has been resolved (approved or rejected) via `POST /api/approvals/{id}/{approve|reject}`.
- **Side effects**: orchestrator resumes from where it paused.

### `waiting_for_user → cancelled`
- **Trigger**: `POST /api/tasks/{id}/cancel` while waiting.
- **Side effects**: outstanding approvals stay in `pending` for audit; task moves to `cancelled`.

### `running → awaiting_user_input`
- **Trigger**: resolve-mode primary returned `resolution_status: needs_user_input` with a `user_input_question`.
- **Side effects**: question persisted as a `user_input_request` message; orchestrator returns without writing a final result.

### `awaiting_user_input → pending`
- **Trigger**: `POST /api/tasks/{id}/answer` with the user's answer.
- **Side effects**: answer persisted as a `user_input_response` message; task status reset to `pending` so the worker re-claims it. The orchestrator's resolve loop seeds itself from the full message history, so it picks up where it left off.

### `awaiting_user_input → cancelled`
- **Trigger**: `POST /api/tasks/{id}/cancel` while awaiting input.
- **Side effects**: pending question remains in the transcript; task moves to `cancelled`.

## Terminal States

`completed`, `failed`, and `cancelled` are terminal. No transitions out. A task that needs to be re-run must be re-submitted as a new task — this preserves the audit trail.

## Approval Sub-Lifecycle

Approvals have their own three-state lifecycle: `pending → approved` or `pending → rejected`. Rows are append-only — resolution updates the same row, it does not create a new one.

A task in `waiting_for_user` may have multiple approvals. The task only resumes when **every** approval is resolved (any combination of approved/rejected). Rejected approvals do not block resumption; the orchestrator drops the corresponding action from the recommendation list and continues.

## What MVP Does Not Implement

- **No retry transitions.** A failed task is failed. The protocol mentions `POST /api/tasks/{id}/retry` for the future.
- **No partial cancellation.** Cancelling cancels the whole task, not a single round.
- **No transitions out of terminal states.** Re-submission is the only recovery path.
- **No streaming progress.** Status changes are visible only on poll. SSE/WebSocket are deferred.
