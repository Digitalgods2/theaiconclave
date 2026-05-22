from __future__ import annotations

from app.protocol.validators import (
    AgentRole,
    MessageType,
    Permissions,
    PrimaryResponse,
    RecommendedAction,
    TaskContext,
    TaskMode,
    TaskRequest,
    TaskSource,
    TaskType,
    Limits,
)
from app.services.orchestrator import _assemble_final


def test_assemble_final_includes_action_plan_and_legacy_arrays():
    task = TaskRequest(
        protocol_version="1.0",
        source=TaskSource.API,
        mode=TaskMode.CONSULT,
        task_type=TaskType.DEBUG,
        user_request="debug",
        primary_agent="fake",
        consultants=["critic"],
        context=TaskContext(),
        permissions=Permissions(
            can_read_files=True,
            can_write_files=False,
            can_run_commands=False,
            can_access_network=False,
            can_install_packages=False,
            can_apply_patches=False,
            can_read_env_files=False,
            can_read_secrets=False,
        ),
        limits=Limits(max_rounds=3, timeout_seconds=30),
    )
    primary = PrimaryResponse(
        protocol_version="1.0",
        task_id="tsk_test",
        agent="fake",
        role=AgentRole.PRIMARY,
        message_type=MessageType.PRIMARY_FINAL,
        summary="Summary",
        analysis="Analysis",
        recommended_actions=[
            RecommendedAction(
                kind="run_command",
                description="Run tests",
                requires_approval=False,
                payload={"command": "pytest"},
            ),
            RecommendedAction(
                kind="apply_patch",
                description="Apply fix",
                requires_approval=True,
                payload={"patch": "diff --git a/x b/x"},
            ),
        ],
    )

    result = _assemble_final(
        task=task,
        task_id="tsk_test",
        primary_resp=primary,
        critiques=[],
        errors=[],
        resolution_status=None,
    )

    assert [s.action_type for s in result.action_plan] == ["run_command", "apply_patch"]
    assert result.action_plan[0].policy_status == "needs_approval"
    assert result.commands_requiring_approval == ["pytest"]
    assert result.patches_requiring_approval == ["diff --git a/x b/x"]
