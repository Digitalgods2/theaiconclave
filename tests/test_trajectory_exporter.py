"""Tests for `services.trajectory_exporter`.

The exporter is a read-only-on-DB / write-only-on-disk sweep. These tests:
  - round-trip a fixture task with messages + runs + final_result, assert the
    JSONL file lands at the expected path and is parseable;
  - assert idempotency — re-exporting overwrites cleanly with the current
    DB state;
  - assert older tasks (no `failure_cause_tags_json` value yet) export with
    an empty `failure_cause_tags` list rather than a missing field.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.database import connect, init_database, now_iso
from app.protocol.validators import Limits, Permissions
from app.services.trajectory_exporter import (
    export_all_terminal,
    export_trajectory,
)
from app.utils.ids import message_id, run_id, result_id, task_id as new_task_id
from app.utils.paths import trajectories_root


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        yield db_path


def _seed_terminal_task(
    *,
    mode: str = "conclave",
    status: str = "completed",
    agreement_level: str = "consensus",
    tags: list[str] | None = None,
    write_tags_column: bool = True,
) -> str:
    """Build a minimal terminal task end-to-end. Optionally skip writing the
    tags column so we can simulate a row that predates the migration."""
    tid = new_task_id()
    now = now_iso()
    permissions = Permissions(
        can_read_files=True, can_write_files=False, can_run_commands=False,
        can_access_network=False, can_install_packages=False,
        can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
    )
    limits = Limits(max_rounds=3, timeout_seconds=30, max_seconds=60)
    context = {"files": [], "error": None, "git_diff": None, "extra": {}}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, created_at, updated_at, status, source, source_agent, mode,
                task_type, user_request, primary_agent, consultants, project_path,
                context_json, permissions_json, limits_json, user_decision, user_decided_at)
               VALUES (?, ?, ?, ?, 'api', NULL, ?, 'general_consultation',
                       'Postgres or Mongo for v1?', NULL, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                tid, now, now, status, mode,
                json.dumps(["alpha", "beta"]),
                json.dumps(context, sort_keys=True),
                json.dumps(permissions.model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
                "Going with Postgres.", now,
            ),
        )
        # One round of conclave messages.
        for agent in ("alpha", "beta"):
            rid = run_id()
            conn.execute(
                """INSERT INTO agent_runs
                   (id, task_id, agent_name, role, round_number, started_at,
                    finished_at, status, duration_ms, input_tokens, output_tokens, cost_usd)
                   VALUES (?, ?, ?, 'participant', 1, ?, ?, 'completed', 1234, 100, 200, 0.001)""",
                (rid, tid, agent, now, now),
            )
            conn.execute(
                """INSERT INTO agent_messages
                   (id, task_id, agent_run_id, agent_name, role, message_type,
                    direction, content, structured_json, created_at)
                   VALUES (?, ?, ?, ?, 'participant', 'conclave_turn', 'from_agent',
                           NULL, ?, ?)""",
                (
                    message_id(), tid, rid, agent,
                    json.dumps({
                        "agent": agent, "role": "participant",
                        "message_type": "conclave_turn",
                        "summary": "Postgres.", "analysis": "Mature.",
                        "position": "Postgres.",
                        "convergence": "i_am_done", "confidence": 0.9,
                    }, sort_keys=True),
                    now,
                ),
            )
        # Final result row.
        if write_tags_column:
            conn.execute(
                """INSERT INTO final_results
                   (id, task_id, final_answer, agreement_level, resolution_status,
                    disagreements_json, recommended_actions_json, action_plan_json, risks_json,
                    commands_requiring_approval_json, patches_requiring_approval_json,
                    errors_json, confidence_aggregate_json, failure_cause_tags_json, created_at)
                   VALUES (?, ?, 'Postgres.', ?, NULL, '[]', '[]', '[]', '[]', '[]', '[]',
                           '[]', NULL, ?, ?)""",
                (result_id(), tid, agreement_level, json.dumps(tags or []), now),
            )
        else:
            # Simulate an older row: tags column defaults to '[]' via migration.
            conn.execute(
                """INSERT INTO final_results
                   (id, task_id, final_answer, agreement_level, resolution_status,
                    disagreements_json, recommended_actions_json, action_plan_json, risks_json,
                    commands_requiring_approval_json, patches_requiring_approval_json,
                    errors_json, confidence_aggregate_json, created_at)
                   VALUES (?, ?, 'Postgres.', ?, NULL, '[]', '[]', '[]', '[]', '[]', '[]',
                           '[]', NULL, ?)""",
                (result_id(), tid, agreement_level, now),
            )
    return tid


# ---------------------------------------------------------------------------
# Round-trip basics
# ---------------------------------------------------------------------------

def test_export_writes_jsonl_at_expected_path(temp_db):
    tid = _seed_terminal_task(tags=["unresolved_dissent"])
    path = export_trajectory(tid)

    assert path.exists()
    assert path.name == f"{tid}.jsonl"
    assert path.parent == trajectories_root()


def test_export_jsonl_is_parseable_one_line(temp_db):
    tid = _seed_terminal_task(tags=["unresolved_dissent", "multimodal_perception_split"])
    path = export_trajectory(tid)

    text = path.read_text(encoding="utf-8")
    # One JSON object, one line, newline-terminated.
    assert text.endswith("\n")
    assert text.count("\n") == 1
    record = json.loads(text)

    # Required keys / shape (the contract downstream consumers depend on).
    for key in (
        "task_id", "created_at", "completed_at", "mode", "task_type", "status",
        "source", "source_agent", "parent_task_id", "question", "agents",
        "rounds", "runs", "final_result", "decision", "confidence_aggregate",
        "failure_cause_tags", "protocol_version", "exported_at",
    ):
        assert key in record, f"missing key: {key}"
    assert record["task_id"] == tid
    assert record["mode"] == "conclave"
    assert record["question"] == "Postgres or Mongo for v1?"
    assert record["agents"]["consultants"] == ["alpha", "beta"]
    # Tags surface at BOTH the top level (for quick scan) and inside final_result.
    assert set(record["failure_cause_tags"]) == {
        "unresolved_dissent", "multimodal_perception_split",
    }
    assert set(record["final_result"]["failure_cause_tags"]) == {
        "unresolved_dissent", "multimodal_perception_split",
    }


def test_export_preserves_round_transcript(temp_db):
    tid = _seed_terminal_task()
    path = export_trajectory(tid)
    record = json.loads(path.read_text(encoding="utf-8"))

    # Both participants contributed one conclave_turn each.
    rounds = record["rounds"]
    assert len(rounds) == 2
    assert {r["agent_name"] for r in rounds} == {"alpha", "beta"}
    assert all(r["message_type"] == "conclave_turn" for r in rounds)
    # structured_json was decoded into a dict, not left as a string.
    assert all(isinstance(r["structured_json"], dict) for r in rounds)


def test_export_preserves_run_metrics(temp_db):
    tid = _seed_terminal_task()
    path = export_trajectory(tid)
    record = json.loads(path.read_text(encoding="utf-8"))

    assert len(record["runs"]) == 2
    for r in record["runs"]:
        assert r["duration_ms"] == 1234
        assert r["input_tokens"] == 100
        assert r["output_tokens"] == 200
        assert r["cost_usd"] == 0.001


def test_export_preserves_decision(temp_db):
    tid = _seed_terminal_task()
    path = export_trajectory(tid)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["decision"]["text"] == "Going with Postgres."
    assert record["decision"]["decided_at"] is not None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_export_twice_overwrites_cleanly(temp_db):
    tid = _seed_terminal_task(tags=[])
    path1 = export_trajectory(tid)
    first = path1.read_text(encoding="utf-8")

    # Add a failure tag directly and re-export.
    with connect() as conn:
        conn.execute(
            "UPDATE final_results SET failure_cause_tags_json = ? WHERE task_id = ?",
            (json.dumps(["unresolved_dissent"]), tid),
        )
    path2 = export_trajectory(tid)
    second = path2.read_text(encoding="utf-8")

    assert path1 == path2  # same path
    assert first != second  # content actually changed
    record = json.loads(second)
    assert record["failure_cause_tags"] == ["unresolved_dissent"]


# ---------------------------------------------------------------------------
# Backward compatibility — older row without failure_cause_tags_json populated
# ---------------------------------------------------------------------------

def test_older_task_without_tags_column_value_exports_empty_list(temp_db):
    """The migration sets a DEFAULT '[]' for the column, so even a 'pre-tags'
    row reads as `[]`. The exporter must surface that as an empty list, not
    None / missing."""
    tid = _seed_terminal_task(write_tags_column=False)
    path = export_trajectory(tid)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["failure_cause_tags"] == []
    assert record["final_result"]["failure_cause_tags"] == []


def test_export_unknown_task_raises_value_error(temp_db):
    with pytest.raises(ValueError):
        export_trajectory("tsk_does_not_exist_xxxxx")


# ---------------------------------------------------------------------------
# Bulk export
# ---------------------------------------------------------------------------

def test_export_all_terminal_iterates_terminal_tasks(temp_db):
    tid1 = _seed_terminal_task(status="completed", tags=[])
    tid2 = _seed_terminal_task(status="failed", tags=["unresolved_dissent"])
    # A non-terminal task should be skipped.
    tid_pending = _seed_terminal_task(status="completed")  # seed normally then flip:
    with connect() as conn:
        conn.execute("UPDATE tasks SET status = 'pending' WHERE id = ?", (tid_pending,))

    summary = export_all_terminal()
    exported_ids = {e["task_id"] for e in summary["exported"]}
    assert tid1 in exported_ids
    assert tid2 in exported_ids
    assert tid_pending not in exported_ids
    assert summary["error_count"] == 0
