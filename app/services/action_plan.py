"""Structured Action Plan compiler.

V1 is advisory only: it classifies final recommended actions, annotates them
with permission and policy status, and returns deterministic steps. It never
executes actions, creates approvals, pauses tasks, or drops blocked steps.
"""

from __future__ import annotations

from typing import Any

from app.protocol.validators import (
    ActionPlanStep,
    ActionType,
    Permissions,
    PolicyStatus,
    RecommendedAction,
)


_INSTALL_MARKERS = (
    "pip install",
    "python -m pip install",
    "uv pip install",
    "npm install",
    "npm i ",
    "pnpm install",
    "yarn add",
    "bun add",
    "go get",
    "cargo install",
)

_NETWORK_MARKERS = (
    "curl ",
    "wget ",
    "http://",
    "https://",
    "npm install",
    "pip install",
    "go get",
)

_DEPLOYMENT_MARKERS = (
    "git push",
    "docker run",
    "docker compose up",
    "kubectl ",
    "terraform apply",
    "pulumi up",
    "wails build",
    "deploy",
)

_HARD_BLOCK_MARKERS = (
    "rm -rf",
    "rm -fr",
    "rm --recursive --force",
    "del /s",
    "del /q /s",
    "rmdir /s",
    "format ",
    "diskpart",
    "mkfs",
    "dd if=",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "| bash",
    "| sh",
    "sudo ",
    "doas ",
    "su -",
    "runas ",
    "chmod -r 777",
    "chmod 777 -r",
    "icacls /grant everyone",
    ":(){:|:&};:",
    "eval ",
    "exec ",
    "c:\\windows\\system32\\",
    "/etc/",
)

_SECRET_MARKERS = (
    ".env",
    ".envrc",
    ".pem",
    ".key",
    "id_rsa",
    "credentials",
    "secrets.",
    "secret",
)


def compile_action_plan(
    actions: list[RecommendedAction],
    permissions: Permissions,
) -> list[ActionPlanStep]:
    """Compile recommended actions into typed, policy-annotated steps."""
    steps: list[ActionPlanStep] = []
    for idx, action in enumerate(actions, 1):
        action_type = _classify_action_type(action)
        target = _target_for(action, action_type)
        required_permissions = _required_permissions(action, action_type, target)
        policy_status, policy_reasons = _policy_for(
            action=action,
            action_type=action_type,
            permissions=permissions,
            required_permissions=required_permissions,
            target=target,
        )
        steps.append(ActionPlanStep(
            step_number=idx,
            action_type=action_type,
            summary=action.description,
            target=target,
            source_action_kind=action.kind,
            required_permissions=required_permissions,
            policy_status=policy_status,
            policy_reasons=policy_reasons,
            payload=dict(action.payload or {}),
        ))
    return steps


def _classify_action_type(action: RecommendedAction) -> ActionType:
    kind = (action.kind or "").strip().lower()
    payload = action.payload or {}
    command = str(payload.get("command") or "").lower()
    target = str(_payload_target(payload) or "").lower()

    if kind in {"read_file", "read_files", "file_read"}:
        return ActionType.READ_FILE
    if kind in {"write_file", "edit_file", "create_file", "file_write"}:
        return ActionType.WRITE_FILE
    if kind in {"apply_patch", "patch"}:
        return ActionType.APPLY_PATCH
    if kind in {"install_package", "package_install"}:
        return ActionType.INSTALL_PACKAGE
    if kind in {"run_command", "command", "shell"}:
        if _looks_like_install(command):
            return ActionType.INSTALL_PACKAGE
        if _looks_like_deployment(command):
            return ActionType.DEPLOYMENT_CHANGE
        if _looks_like_network(command):
            return ActionType.NETWORK_ACCESS
        return ActionType.RUN_COMMAND
    if kind in {"network_access", "fetch_url", "http_request", "download"}:
        return ActionType.NETWORK_ACCESS
    if kind in {"deployment_change", "deploy", "release"}:
        return ActionType.DEPLOYMENT_CHANGE
    if kind in {"secret_access", "read_secret", "read_env"}:
        return ActionType.SECRET_ACCESS
    if kind in {"human_decision", "ask_user", "manual_step", "verify", "review"}:
        return ActionType.HUMAN_DECISION

    if _looks_like_secret(target) or _looks_like_secret(command):
        return ActionType.SECRET_ACCESS
    return ActionType.UNKNOWN


def _target_for(action: RecommendedAction, action_type: ActionType) -> str | None:
    payload = action.payload or {}
    if action_type in {
        ActionType.RUN_COMMAND,
        ActionType.INSTALL_PACKAGE,
        ActionType.NETWORK_ACCESS,
        ActionType.DEPLOYMENT_CHANGE,
    }:
        command = payload.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    target = _payload_target(payload)
    return str(target) if target not in (None, "") else None


def _payload_target(payload: dict[str, Any]) -> Any:
    for key in ("target", "path", "file", "url", "secret", "env_var", "name"):
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _required_permissions(
    action: RecommendedAction,
    action_type: ActionType,
    target: str | None,
) -> list[str]:
    required: list[str] = []
    if action_type == ActionType.READ_FILE:
        required.append("can_read_files")
    elif action_type == ActionType.WRITE_FILE:
        required.append("can_write_files")
    elif action_type == ActionType.RUN_COMMAND:
        required.append("can_run_commands")
    elif action_type == ActionType.INSTALL_PACKAGE:
        required.extend(["can_install_packages", "can_run_commands", "can_access_network"])
    elif action_type == ActionType.APPLY_PATCH:
        required.append("can_apply_patches")
    elif action_type == ActionType.NETWORK_ACCESS:
        required.append("can_access_network")
    elif action_type == ActionType.DEPLOYMENT_CHANGE:
        required.append("can_run_commands")
    elif action_type == ActionType.SECRET_ACCESS:
        if _looks_like_env(target or "") or (action.kind or "").lower() == "read_env":
            required.append("can_read_env_files")
        else:
            required.append("can_read_secrets")

    if target and _looks_like_env(target) and "can_read_env_files" not in required:
        required.append("can_read_env_files")
    if target and _looks_like_secret(target) and not (
        "can_read_secrets" in required or "can_read_env_files" in required
    ):
        required.append("can_read_secrets")
    return list(dict.fromkeys(required))


def _policy_for(
    *,
    action: RecommendedAction,
    action_type: ActionType,
    permissions: Permissions,
    required_permissions: list[str],
    target: str | None,
) -> tuple[PolicyStatus, list[str]]:
    reasons: list[str] = []
    command = target if action_type in {
        ActionType.RUN_COMMAND,
        ActionType.INSTALL_PACKAGE,
        ActionType.NETWORK_ACCESS,
        ActionType.DEPLOYMENT_CHANGE,
    } else str((action.payload or {}).get("command") or "")

    if action_type == ActionType.UNKNOWN:
        reasons.append("Action kind is not recognized by the v1 compiler.")
        return PolicyStatus.UNKNOWN, reasons

    if _is_hard_blocked(command):
        reasons.append("Command matches the non-overridable dangerous-command blocklist.")
        return PolicyStatus.BLOCKED, reasons

    missing = [name for name in required_permissions if not bool(getattr(permissions, name, False))]
    if missing:
        reasons.append("Missing permission(s): " + ", ".join(missing) + ".")
        if action_type in {
            ActionType.RUN_COMMAND,
            ActionType.INSTALL_PACKAGE,
            ActionType.APPLY_PATCH,
            ActionType.NETWORK_ACCESS,
            ActionType.DEPLOYMENT_CHANGE,
            ActionType.SECRET_ACCESS,
        }:
            return PolicyStatus.NEEDS_APPROVAL, reasons
        return PolicyStatus.BLOCKED, reasons

    if action.requires_approval:
        reasons.append("Source recommended action sets requires_approval=true.")
        return PolicyStatus.NEEDS_APPROVAL, reasons
    if action_type == ActionType.APPLY_PATCH:
        reasons.append("Patch application always requires approval in v1.")
        return PolicyStatus.NEEDS_APPROVAL, reasons
    if action_type == ActionType.INSTALL_PACKAGE:
        reasons.append("Package installation always requires approval.")
        return PolicyStatus.NEEDS_APPROVAL, reasons
    if action_type == ActionType.DEPLOYMENT_CHANGE:
        reasons.append("Deployment or remote-state change requires approval.")
        return PolicyStatus.NEEDS_APPROVAL, reasons
    if action_type == ActionType.NETWORK_ACCESS and not permissions.can_access_network:
        reasons.append("Network access is not granted for this task.")
        return PolicyStatus.NEEDS_APPROVAL, reasons
    if action_type == ActionType.HUMAN_DECISION:
        reasons.append("Human decision or manual verification step; no AI Conclave Switchboard permission is consumed.")
        return PolicyStatus.ALLOWED, reasons

    reasons.append("Required task permissions are present.")
    return PolicyStatus.ALLOWED, reasons


def _looks_like_install(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _INSTALL_MARKERS)


def _looks_like_network(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _NETWORK_MARKERS)


def _looks_like_deployment(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _DEPLOYMENT_MARKERS)


def _looks_like_env(text: str) -> bool:
    low = text.lower()
    return ".env" in low or low.endswith("env") or "env_var" in low


def _looks_like_secret(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


def _is_hard_blocked(command: str | None) -> bool:
    low = (command or "").lower()
    if not low:
        return False
    return any(marker in low for marker in _HARD_BLOCK_MARKERS)


__all__ = ["compile_action_plan"]
