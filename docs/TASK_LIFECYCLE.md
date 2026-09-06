# Task Lifecycle

Defines the state machine every task moves through, including who flips each transition and under what conditions. The protocol enum (`TaskStatus`) names the states; this document defines the transitions.

## States

| State | Meaning |
|---|---|
| `pending` | Task created (or resumed from user input), waiting for the worker to claim it. |
| `running` | Worker has claimed the task; orchestrator is calling agents. |
| `waiting_for_user` | Reserved for a future executable approval workflow; current code never enters this state. |
| `awaiting_user_input` | Resolve-mode primary asked the user a **question**; pauses until the user POSTs an answer. |
| `completed` | Final result built and persisted. |
| `failed` | Unrecoverable error; no usable final result. |
| `cancelled` | User cancelled before completion. |

## Diagram

The `waiting_for_user` branch shown below is reserved protocol shape, not a
currently reachable transition. Current action plans are advisory and require
the user to act separately.

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
- **Reserved, not implemented.** There is currently no trigger or side effect.

### `running → completed`
- **Trigger**: orchestrator finished all rounds and built a `FinalResult` with no fatal errors.
- **Side effects**: `final_results` row inserted; `tasks.status = 'completed'`.

### `running → failed`
- **Trigger**: orchestrator hit an unrecoverable error (exception escaped the orchestrator, all agents unavailable, schema validation impossible).
- **Side effects**: `tasks.error_message` set; `tasks.status = 'failed'`. A partial `final_results` row may exist with `errors` populated.

### `running → cancelled`
- **Trigger**: `POST /api/tasks/{id}/cancel` while running.
- **Side effects**: Abort immediately interrupts the active adapter call; no further calls are made; `tasks.status = 'cancelled'`.

### `waiting_for_user → running`
- **Reserved, not implemented.** No approval resolution endpoint exists.

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

The database retains a schema-only `approvals` table and the protocol retains
approval models for compatibility with the original design. Production code
does not create approval rows, expose approve/reject endpoints, or resume tasks
from them. Structured action plans are advisory; explicit artifact application
is a separate user-initiated API operation.

## What MVP Does Not Implement

- **No executable approval gate.** `waiting_for_user` and approval resolution endpoints are reserved.
- **No retry transitions.** A failed task is failed. The protocol mentions `POST /api/tasks/{id}/retry` for the future.
- **No partial cancellation.** Cancelling cancels the whole task, not a single round.
- **No transitions out of terminal states.** Re-submission is the only recovery path.
- **No streaming progress.** Status changes are visible only on poll. SSE/WebSocket are deferred.


## Restart and explicit retry

After acquiring the exclusive instance lock, startup marks every previous `running`
claim failed, regardless of age, and closes its running agent-run records. This is
transactional and preserves transcripts. Pending and paused tasks retain their status.
Retry is explicit: `POST /api/tasks/{id}/retry` creates a new linked task from a stopped
failed/cancelled attempt. Deletion is refused while a cancelled coroutine still cleans up.
Initialization failures release the instance lock.

Feedback on a terminal task updates only `task_feedback`; it cannot resume the worker.
