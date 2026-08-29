"""Shared response coercion for adapters that parse a JSON envelope.

This lives in its own module rather than on `CliAdapterBase` because
`OpenRouterAdapter` needs it too and is deliberately NOT a subclass of that
base (different `_invoke` signature, tool-loop dispatch, no image support).
Importing a base class's private helper into an unrelated adapter would imply
an inheritance relationship that does not exist.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import AdapterError
from app.protocol.validators import ErrorCode
from app.utils.json_tools import extract_json_object


def parse_and_coerce(
    text: str,
    task_id: str,
    agent_name: str,
    *,
    role: str,
    default_message_type: str,
) -> dict[str, Any]:
    """Extract the JSON envelope and overwrite the identity fields.

    Identity is overwritten rather than validated: models routinely echo a
    stale task_id, name themselves something other than their registered seat
    name, or claim the wrong role. Those fields are facts the orchestrator
    already knows, so the adapter asserts them instead of trusting the model
    and failing validation on a response that was otherwise fine.

    `resolution_status` gets the same treatment for the specific case of a
    model emitting the *string* "null" (or "None") instead of JSON null, which
    would otherwise fail enum validation.
    """
    try:
        data = extract_json_object(text)
    except ValueError as e:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            f"could not extract JSON from {agent_name} response: {e}",
            details={"text_tail": text[-2000:]},
        )
    data["protocol_version"] = "1.0"
    data["task_id"] = task_id
    data["agent"] = agent_name
    data["role"] = role
    data.setdefault("message_type", default_message_type)
    if data.get("resolution_status") in ("null", "None", ""):
        data["resolution_status"] = None
    return data


__all__ = ["parse_and_coerce"]
