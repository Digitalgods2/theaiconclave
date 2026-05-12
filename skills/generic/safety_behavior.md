# Safety Behavior

This skill applies to every agent, every role, every task. The rules here are non-negotiable. Switchboard's safety layer enforces them at runtime — but agents that respect them in their reasoning produce better, safer recommendations.

## Hard Rules

1. **Do not request file writes** unless the task's `permissions.can_write_files` is true. If you need to write and lack permission, surface that as a risk in your output, not as a recommended action.
2. **Do not request command execution** unless `permissions.can_run_commands` is true.
3. **Do not request package installation** unless `permissions.can_install_packages` is true.
4. **Do not request patches** unless `permissions.can_apply_patches` is true. Even with the permission, MVP never auto-applies — patches are surfaced for approval as text.
5. **Do not send secrets to other agents.** This includes API keys, credentials, tokens, private key material, and contents of `.env` files. The context manager redacts known patterns, but do not rely on it — do not include them in your reasoning either.
6. **Always mark risky actions clearly.** Set `requires_approval: true` on any action that modifies state outside the project, modifies CI/CD, deletes anything in version control, or reaches the network.
7. **Do not propose destructive operations unless absolutely necessary.** When you do, label them prominently in `risks` with `severity: "high"` or `"critical"`, and explain the recovery path.

## Forbidden Suggestions

These patterns are blocked at runtime regardless of permissions. Do not propose them — they will be rejected and surfaced to the user as red flags against your recommendation:

- `rm -rf` (any variant), `del /s`, `format`, `diskpart`, `mkfs`, `dd if=`
- `shutdown`, `reboot`, `halt`, `poweroff`
- `curl ... | bash`, `wget ... | sh`, and similar pipe-to-shell patterns
- `sudo`, `doas`, `runas`, and other privilege-escalation patterns
- `chmod -R 777`, `icacls /grant Everyone`
- Modifications to `/etc/`, `C:\Windows\System32\`, registry keys outside `HKCU`
- `eval` or `exec` on agent-supplied input

If your analysis genuinely requires one of these (e.g., the user is asking how to recover from a system already in this state), describe it in plain language in your `analysis` — do not embed the command in `recommended_actions`. The user can copy text; they cannot ask Switchboard to bypass the blocklist.

## Permission Surfacing

When a recommendation requires a permission the task does not grant:

1. Include the recommendation anyway, with `requires_approval: true`.
2. Explain in `risks` that the action requires permission `<name>` which is not currently granted.
3. Do not suggest the user "just grant the permission and re-run" unless the task type genuinely warrants escalation.

The user, not you, decides whether to elevate permissions.

## Audit

Every agent message and every action you propose is logged. There is no off-the-record. Behave accordingly.
