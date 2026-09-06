"""Shared persisted result contract for HTTP and every export format."""
from __future__ import annotations
import json
from typing import Any


def column_or_none(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def safe_json(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default


def serialize_final_result(row) -> dict[str, Any]:
    agg_raw = column_or_none(row, "confidence_aggregate_json")
    action_plan_raw = column_or_none(row, "action_plan_json")
    tags_raw = column_or_none(row, "failure_cause_tags_json")
    tags = safe_json(tags_raw, default=[])
    if not isinstance(tags, list):
        tags = []
    return {
        "task_id": row["task_id"],
        "final_answer": row["final_answer"],
        "agreement_level": row["agreement_level"],
        "resolution_status": row["resolution_status"],
        "disagreements": safe_json(column_or_none(row, "disagreements_json"), default=[]),
        "action_plan": safe_json(action_plan_raw, default=[]),
        "recommended_actions": safe_json(column_or_none(row, "recommended_actions_json"), default=[]),
        "risks": safe_json(column_or_none(row, "risks_json"), default=[]),
        "commands_requiring_approval": safe_json(column_or_none(row, "commands_requiring_approval_json"), default=[]),
        "patches_requiring_approval": safe_json(column_or_none(row, "patches_requiring_approval_json"), default=[]),
        "errors": safe_json(column_or_none(row, "errors_json"), default=[]),
        "confidence_aggregate": safe_json(agg_raw, default=None),
        # Rule-based labels describing why this deliberation was hard, stamped
        # by services.trace_analyzer after finalization. Empty list for older
        # rows (pre-migration) or quick-converging tasks where no rule fired.
        "failure_cause_tags": [str(t) for t in tags],
        "citations": safe_json(column_or_none(row, "citations_json"), default=[]),
        "citation_coverage": safe_json(
            column_or_none(row, "citation_coverage_json"), default=None
        ),
        "synthesis_agent": column_or_none(row, "synthesis_agent"),
        "created_at": row["created_at"],
    }

