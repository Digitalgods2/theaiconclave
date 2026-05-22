from __future__ import annotations

from app.protocol.validators import Permissions, RecommendedAction
from app.services.action_plan import compile_action_plan


def _perms(**overrides) -> Permissions:
    base = {
        "can_read_files": True,
        "can_write_files": False,
        "can_run_commands": False,
        "can_access_network": False,
        "can_install_packages": False,
        "can_apply_patches": False,
        "can_read_env_files": False,
        "can_read_secrets": False,
    }
    base.update(overrides)
    return Permissions(**base)


def _action(kind: str, payload=None, requires_approval=False) -> RecommendedAction:
    return RecommendedAction(
        kind=kind,
        description=f"Do {kind}",
        requires_approval=requires_approval,
        payload=payload or {},
    )


def test_run_command_without_permission_needs_approval():
    steps = compile_action_plan(
        [_action("run_command", {"command": "pytest"})],
        _perms(can_run_commands=False),
    )

    assert steps[0].action_type == "run_command"
    assert steps[0].policy_status == "needs_approval"
    assert steps[0].required_permissions == ["can_run_commands"]


def test_dangerous_command_is_blocked_even_with_permission():
    steps = compile_action_plan(
        [_action("run_command", {"command": "rm -rf /tmp/example"})],
        _perms(can_run_commands=True),
    )

    assert steps[0].policy_status == "blocked"


def test_package_install_requires_install_run_and_network_and_approval():
    steps = compile_action_plan(
        [_action("install_package", {"command": "python -m pip install pydantic"})],
        _perms(can_run_commands=True, can_access_network=True, can_install_packages=True),
    )

    assert steps[0].action_type == "install_package"
    assert steps[0].required_permissions == [
        "can_install_packages",
        "can_run_commands",
        "can_access_network",
    ]
    assert steps[0].policy_status == "needs_approval"


def test_patch_application_maps_to_apply_patch_and_needs_approval():
    steps = compile_action_plan(
        [_action("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"})],
        _perms(can_apply_patches=True),
    )

    assert steps[0].action_type == "apply_patch"
    assert steps[0].policy_status == "needs_approval"


def test_file_write_requires_write_permission():
    steps = compile_action_plan(
        [_action("write_file", {"path": "app/main.py"})],
        _perms(can_write_files=False),
    )

    assert steps[0].action_type == "write_file"
    assert steps[0].required_permissions == ["can_write_files"]
    assert steps[0].policy_status == "blocked"


def test_env_and_secret_targets_require_specific_permissions():
    env_step, secret_step = compile_action_plan(
        [
            _action("read_file", {"path": ".env"}),
            _action("read_file", {"path": "deploy.pem"}),
        ],
        _perms(),
    )

    assert "can_read_env_files" in env_step.required_permissions
    assert "can_read_secrets" in secret_step.required_permissions


def test_unknown_action_kind_produces_unknown_without_crashing():
    steps = compile_action_plan([_action("teleport", {"where": "moon"})], _perms())

    assert steps[0].action_type == "unknown"
    assert steps[0].policy_status == "unknown"
