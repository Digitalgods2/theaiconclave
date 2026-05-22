# Safety Model

Default behavior: **deny**. Every action that touches the filesystem, runs a command, or reaches the network is blocked unless the task's permissions explicitly allow it. This file defines what each permission gates, what the dangerous-command blocklist contains, when the AI Conclave Switchboard pauses for user approval, and how violations are handled.

This document is the contract that adapters and the orchestrator must enforce. The protocol (`SWITCHBOARD_PROTOCOL.md`) describes the *shape* of permissions on the wire; this document describes their *meaning*.

## 1. Default-Deny Philosophy

The default permission set on every task:

| Permission | Default | Scope when granted |
|---|---|---|
| `can_read_files` | `true` | Only inside `project_path` and its subdirectories |
| `can_write_files` | `false` | Only inside `project_path`; never to system paths |
| `can_run_commands` | `false` | Subject to the dangerous-command blocklist (section 3) |
| `can_access_network` | `false` | Outbound HTTP/HTTPS only; no raw sockets |
| `can_install_packages` | `false` | Implies `can_run_commands` and `can_access_network` |
| `can_apply_patches` | `false` | Patches are surfaced for approval; never auto-applied in MVP |
| `can_read_env_files` | `false` | `.env`, `.env.*` — read access blocked even when `can_read_files` is true |
| `can_read_secrets` | `false` | Files matching `*.key`, `*.pem`, `id_rsa*`, `credentials*`, `secrets.*` |

The defaults stand even when the agent says "this is safe." Permissions are granted at the task layer by the user, never by the agent.

## 2. File Access Rules

- **Allowed roots.** Reads and writes are scoped to `project_path` from the task request. Paths outside this root are denied.
- **Symlink handling.** Symlinks that resolve outside the project root are treated as outside the root and denied.
- **Hidden files.** `.git/`, `.vscode/`, `.idea/` are readable but are not sent as default context (the context manager filters them).
- **Always blocked unless explicitly allowed.**
  - `.env`, `.env.*`, `.envrc` (gated by `can_read_env_files`)
  - `*.key`, `*.pem`, `id_rsa*`, `*.p12`, `*.pfx` (gated by `can_read_secrets`)
  - `credentials*`, `secrets.*`, `aws/credentials`, `gcp/*.json` (gated by `can_read_secrets`)
- **Logging.** Every file read and write is logged to the `logs` table with task ID, agent name, and full path.

## 3. Command Execution Rules

Commands run only when `can_run_commands` is true. Even then, the dangerous-command blocklist applies and is **non-overridable**. There is no "force" flag in the MVP.

### Hard blocklist — never executable, even with `can_run_commands`

Pattern-matched against the full command string before execution:

- `rm -rf` (any variant), `rm -fr`, `rm --recursive --force`
- `del /s`, `del /q /s`, `rmdir /s`
- `format`, `diskpart`, `mkfs`, `dd if=`
- `shutdown`, `reboot`, `halt`, `poweroff`
- `curl ... | bash`, `curl ... | sh`, `wget ... | bash`, `wget ... | sh`
- `sudo`, `doas`, `su -`, `runas`
- `chmod -R 777`, `chmod 777 -R`, `icacls /grant Everyone`
- Fork-bomb shapes (e.g. `:(){:|:&};:`)
- `eval` or `exec` invoked on agent-supplied content
- Direct edits to `/etc/`, `C:\Windows\System32\`, registry keys outside `HKCU`

A blocked command does not produce a clarifying error to the agent; it produces `permission_denied` to the orchestrator and is surfaced to the user.

### Soft list — allowed but **always** require approval, even when `can_run_commands` is true

- Package installs (`pip install`, `npm install`, `bun add`, `go get`, `cargo install`)
- Git operations that mutate remote state (`push`, `push --force`, `pull --rebase`)
- `docker run`, `docker compose up`, container starts
- Any command writing outside `project_path`
- Any command reaching the network when `can_access_network` is false

## 4. Approval Gate

The AI Conclave Switchboard pauses tasks (status → `waiting_for_user`) when an agent's recommended action requires approval. Triggers:

- Any action with `requires_approval: true` in `recommended_actions`
- Any command on the soft list above
- Any patch application (always — MVP rule)
- Any package install (always — MVP rule)
- Any deletion of files in version control
- Any modification of CI/CD config, deployment scripts, or `.github/`

The pause writes an `approvals` row with `status: pending`. The user resolves it via dashboard or `POST /api/approvals/{id}/approve|reject`. On approve: the task resumes and the action becomes runnable. On reject: the task continues with the action removed from the recommendation list and the rejection logged.

### Structured Action Plan advisory pass

The Structured Action Plan is a policy-checked operational handoff compiled from the final synthesized `recommended_actions`. In v1 it is advisory only. It annotates each step with an action type, required permissions, policy status, and reasons so the user can see what would be allowed, require approval, or be blocked before acting.

This pass does not execute commands, apply patches, access the network, read secrets, create `approvals` rows, pause tasks, or remove blocked steps. The approval gate above remains authoritative for any future executable workflow.

### Draft artifacts and explicit apply

Agents still do not write to the user's project in v1. When a final recommendation contains a draft file, search/replace edit, or patch, the AI Conclave Switchboard may store it under the app-owned runtime artifact directory for review. This is a product handoff surface, not an execution grant and not a bypass of task permissions.

The dashboard/API can explicitly apply supported artifacts after the task completes. That apply action is user-initiated, constrained to the task's `project_path`, and rejects paths that escape the project root. Patch artifacts are review/download-only in v1; direct patch application remains governed by the patch rules below.

## 5. Patch Handling

In MVP, the AI Conclave Switchboard **never applies patches**. It surfaces them as text in `patches_requiring_approval` on the final result. Future versions may support apply-after-approval with these guards:

- Mandatory `git stash` or branch creation before apply
- Mandatory dry-run (`git apply --check`) before commit
- Mandatory rollback path recorded before apply
- Refusal to apply across version-control boundaries

## 6. Network Rules

When `can_access_network` is false, agents may still propose network actions in their recommendations — but the AI Conclave Switchboard does not execute them and does not let any subprocess it spawns reach the network. The MVP enforces this by:

- Not spawning HTTP-using subprocesses
- Not setting proxy env vars from the host into the subprocess env
- Blocking adapter calls that themselves require network access (the agent's CLI may still reach its own provider — that is the agent's authority, not the AI Conclave Switchboard's)

When `can_access_network` is true, only outbound HTTP/HTTPS is allowed. Raw sockets, SMTP, and direct DB protocols are out of scope for MVP.

## 7. Context Sanitization

The context manager strips before sending to any agent:

- Lines matching common secret patterns: `AKIA[A-Z0-9]{16}`, `xox[baprs]-...`, `ghp_...`, `sk-...`, JWT-shaped tokens, anything in `*.env`
- Files matching the secret blocklist in section 2 (regardless of `can_read_secrets` — the agent never sees raw secrets even when authorized; the user's intent in granting `can_read_secrets` is to allow the agent to *reason about their existence*, not to receive their values)
- Output of git commands that would include `.env` deltas

If sanitization removes content, the context manager records that fact in the task's logs and replaces the content with `[REDACTED: matched secret pattern]`.

## 8. Audit Trail

Every safety-relevant event is logged to the `logs` table with `event_type` from this fixed set:

- `file_read`, `file_write`, `file_blocked`
- `command_attempted`, `command_blocked`, `command_executed`
- `network_attempted`, `network_blocked`
- `approval_requested`, `approval_granted`, `approval_rejected`
- `secret_redacted`
- `permission_denied`

Logs are never auto-deleted in MVP.

## 9. Failure Behavior

When a safety check fails, the task does **not** silently degrade. The orchestrator emits an `error` message with code `permission_denied` and either:

- Marks the task `failed` if the denied action was load-bearing for the recommendation, or
- Marks the task `waiting_for_user` if the user can grant the missing permission, or
- Continues with the action removed and surfaces the omission in `errors` on the final result.

The agent never receives a "successful" signal for a denied action.

## 10. Round and Time Limits as a Safety Mechanism

Although they live in `limits` rather than `permissions`, the round count and timeout are safety controls:

- `max_rounds` prevents agents from looping or accumulating cost without bound.
- `timeout_seconds` per agent call prevents a hung adapter from holding the worker.
- Repetition detection (>80% n-gram overlap between consecutive primary responses) terminates the debate with `loop_detected` even when `max_rounds` is not exhausted.

These limits cannot be raised by an agent, only by the user submitting the task.
