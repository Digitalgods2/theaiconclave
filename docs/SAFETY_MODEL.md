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

## 4. Future Approval Gate (Reserved)

The original design reserved `waiting_for_user`, approval rows, and resolution
endpoints for an executable workflow. That workflow is not implemented. The
following remain policy requirements if execution is added later:

- Any action with `requires_approval: true` in `recommended_actions`
- Any command on the soft list above
- Any patch application (always — MVP rule)
- Any package install (always — MVP rule)
- Any deletion of files in version control
- Any modification of CI/CD config, deployment scripts, or `.github/`

Current production code does not create `approvals` rows, pause on recommended
actions, or expose approve/reject endpoints. It compiles an advisory action plan
and leaves execution to an explicit user action outside the deliberation loop.

### Structured Action Plan advisory pass

The Structured Action Plan is a policy-checked operational handoff compiled from the final synthesized `recommended_actions`. In v1 it is advisory only. It annotates each step with an action type, required permissions, policy status, and reasons so the user can see what would be allowed, require approval, or be blocked before acting.

This pass does not execute commands, apply patches, access the network, read secrets, create `approvals` rows, pause tasks, or remove blocked steps. The reserved policy above remains the requirement for any future executable workflow.

### Draft artifacts and explicit apply

Agents still do not write to the user's project in v1. When a final recommendation contains a draft file, search/replace edit, or patch, the AI Conclave Switchboard may store it under the app-owned runtime artifact directory for review. This is a product handoff surface, not an execution grant and not a bypass of task permissions.

The dashboard/API can explicitly apply supported artifacts after the task completes. Apply is two-phase: the server first returns a diff plus the current target SHA-256, then requires confirmation of that exact hash. Existing-file overwrites require a separate flag and create an app-owned backup. Writes use a same-directory temporary file plus atomic replace and emit an `artifact_applied` audit event. Patch artifacts remain review/download-only in v1.

### Evidence acquisition

Evidence fetching is a server-owned preprocessing action, not a network tool granted to each agent. HTTPS is required by default; URL credentials and non-public DNS results are rejected, redirects are revalidated, and time/byte/character limits are enforced. HTML scripts and styles are excluded, while PDF and text sources are extracted into immutable, hashed snapshots.

Remote source text is always marked as untrusted data in prompts. Instruction-like phrases are retained for audit but counted in quality metadata, and agents are explicitly prohibited from following them. Participants cite only snapshot IDs; invalid IDs are surfaced in final citation coverage.

### Service network boundary

The default bind host is loopback. A non-loopback host cannot start unless `server.allow_remote=true` and a 16-character-or-longer token is supplied through `CONCLAVE_API_TOKEN` or `server.api_token`. When a token is configured, all `/api/` requests require it as `Authorization: Bearer ...` or `X-Conclave-Token`.

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

## 10. Round Limits and User-Controlled Elapsed Time

Although it lives in `limits` rather than `permissions`, the round count is a safety control. Elapsed time is an observability signal controlled by the user:

- `max_rounds` prevents agents from looping or accumulating cost without bound.
- `notify_after_seconds` (preferred; legacy `timeout_seconds` / `max_seconds` aliases) notifies the user but never auto-fails a job. Abort interrupts the active coroutine and terminates the complete CLI process tree.
- Repetition detection (>80% n-gram overlap between consecutive primary responses) terminates the debate with `loop_detected` even when `max_rounds` is not exhausted.

These limits cannot be raised by an agent, only by the user submitting the task.


## Evidence and archive integrity updates

Evidence HTTP transport pins each connection to a validated global IP address and
preserves the original hostname for TLS certificate verification and Host routing.
Redirects receive the same validation; environment proxies are disabled. Connections
are not reused across rewritten IP origins. Search response bodies are capped too.
Snapshot text is untrusted source content. The task records which exact excerpts were
presented, including sources omitted due to the 60,000-character evidence budget.

Retention uses live SQLite pages instead of pre-vacuum physical size to stop trimming
once enough data is reclaimed. Tier-2 deletion additionally requires the recorded archive
to be a nonempty existing file. This is an existence check, not a full restore test.
