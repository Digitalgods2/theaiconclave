"""Trace analyzer — rule-based classification of why a deliberation was hard.

Inspects an already-completed task (its messages, agent_runs, final_result row)
and stamps a small list of `FailureCause` tags onto the record. Pure post-hoc:
no LLM calls, no extra tokens, deterministic and cheap. Runs after the
orchestrator has written the final_results row so a tagging failure cannot
break a successful task.

Why rule-based, not an AI judge: tags need to be cheap (run on every task),
stable across runs (so retrieval is deterministic), and inspectable (so the
user trusts what they're filtering by). An LLM judge would be slower, more
expensive, and less reproducible — and the signals we care about (timeouts,
parse errors, repetition backstops, denied approvals) are mechanical events the
orchestrator already records.

Design rules:
- Public surface is one function: `classify_failure_causes(...)`.
- Defensive on missing keys / None / unexpected shapes — older rows must not
  raise.
- Wraps the whole classification in try/except and returns [] on unexpected
  failure. The trace analyzer MUST NOT turn a successful task into a failed
  one.
- Order of tags in the returned list is stable (insertion order via the rule
  evaluation sequence) and de-duped.

Add new rules by appending an `_apply_*` helper that mutates the `causes` list
and wiring it into `classify_failure_causes`. Each rule should be cheap, read
only the inputs it needs, and explain WHY it fires in its docstring.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping, Optional

from app.protocol.validators import FailureCause


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — defensive accessors so the rules can stay readable.
# ---------------------------------------------------------------------------

def _get(row: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a dict or sqlite3.Row-like object, returning `default`
    on any failure. Both shapes appear in callers; this lets the rules treat
    them uniformly without sprinkling try/except everywhere."""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _parse_json(value: Any, default: Any) -> Any:
    """Parse a JSON-encoded column. Returns `default` if value is empty/invalid.

    Final results carry several JSON columns (errors_json, action_plan_json,
    confidence_aggregate_json, failure_cause_tags_json). Rules typically want
    the decoded list/dict, not the raw text.
    """
    if value is None:
        return default
    if not isinstance(value, str):
        # Already decoded (e.g., caller passed a dict).
        return value
    if not value.strip():
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _iter_errors(final_result: Any) -> list[dict]:
    """Return the errors list off `final_result`, regardless of whether the
    caller passed a row, a dict, or the FinalResult model dumped to JSON."""
    if final_result is None:
        return []
    # Try the common shapes in priority order.
    raw = _get(final_result, "errors")
    if raw is None:
        raw = _parse_json(_get(final_result, "errors_json"), default=[])
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _iter_messages(messages: Optional[Iterable[Any]]) -> Iterable[dict]:
    """Normalize each message into a dict view, decoding `structured_json`
    on read. Skips anything we can't read."""
    for m in messages or ():
        if m is None:
            continue
        if isinstance(m, dict):
            yield m
            continue
        # sqlite3.Row: build a shallow dict from its keys.
        try:
            keys = m.keys()
        except AttributeError:
            continue
        yield {k: m[k] for k in keys}


def _structured(message: dict) -> dict:
    """Extract a message's structured payload. Newer flows already decode it
    into `structured`; older rows keep it as a JSON string in `structured_json`."""
    s = message.get("structured")
    if isinstance(s, dict):
        return s
    return _parse_json(message.get("structured_json"), default={}) or {}


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

_TIMEOUT_CODES = {"timeout", "adapter_timeout", "timed_out", "agent_timeout", "resolve_timeout"}
_PERMISSION_TOKENS = ("permission", "denied", "forbidden")
_JSON_TOKENS = ("json", "extract", "parse")
_REPETITION_TOKENS = ("repetition", "loop_backstop", "loop_detected", "loop detected")
_VISUAL_TOKENS = ("image", "visual", "picture", "chart", "screenshot", "diagram", "photo")


def _apply_dissent_rule(causes: list[FailureCause], task_row: Any, final_result: Any) -> None:
    """`unresolved_dissent` when the deliberation ended without genuine
    convergence. Sources: agreement_level on the final row, or a task that
    landed in `cannot_resolve` without producing a decision."""
    level = _get(final_result, "agreement_level")
    if level in ("unresolved", "major_disagreement"):
        causes.append(FailureCause.UNRESOLVED_DISSENT)
        return
    status = _get(task_row, "status")
    resolution = _get(final_result, "resolution_status")
    decision = _get(task_row, "user_decision")
    if (status == "cannot_resolve" or resolution == "cannot_resolve") and not decision:
        causes.append(FailureCause.UNRESOLVED_DISSENT)


def _apply_bad_json_rule(causes: list[FailureCause], final_result: Any) -> None:
    """`bad_json_output` when any error code references JSON parse / extract.
    Common failure mode: a CLI update changes output shape and the adapter
    parser breaks (see INSTALL.md troubleshooting)."""
    for err in _iter_errors(final_result):
        code = str(err.get("code") or "").lower()
        message = str(err.get("message") or "").lower()
        if any(t in code for t in _JSON_TOKENS) or any(t in message for t in _JSON_TOKENS):
            causes.append(FailureCause.BAD_JSON_OUTPUT)
            return


def _apply_timeout_rule(causes: list[FailureCause], runs: Optional[Iterable[Any]], final_result: Any) -> None:
    """`tool_timeout` when an agent_run timed out OR a top-level error reports
    a timeout. Resolve mode's max_seconds backstop also shows up as
    `resolve_timeout` on the final result; we catch both."""
    for run in runs or ():
        ec = (_get(run, "error_code") or "")
        st = (_get(run, "status") or "")
        if str(ec).lower() in _TIMEOUT_CODES or str(st).lower() in _TIMEOUT_CODES:
            causes.append(FailureCause.TOOL_TIMEOUT)
            return
    for err in _iter_errors(final_result):
        code = str(err.get("code") or "").lower()
        if code in _TIMEOUT_CODES:
            causes.append(FailureCause.TOOL_TIMEOUT)
            return


def _apply_multimodal_rule(causes: list[FailureCause], messages: Optional[Iterable[Any]]) -> None:
    """`multimodal_perception_split` when a participant emitted
    `need_user_input` AND the accompanying question mentions a visual artifact.
    This is the cheapest signal we have for "agents saw the picture
    differently"; a semantic detector would do better but we don't pay for one."""
    for m in _iter_messages(messages):
        s = _structured(m)
        if not s:
            continue
        if s.get("convergence") != "need_user_input":
            continue
        question = str(s.get("user_input_question") or "").lower()
        if any(t in question for t in _VISUAL_TOKENS):
            causes.append(FailureCause.MULTIMODAL_PERCEPTION_SPLIT)
            return


def _apply_clarification_unanswered_rule(
    causes: list[FailureCause],
    task_row: Any,
    messages: Optional[Iterable[Any]],
) -> None:
    """`clarification_unanswered` when the task asked for user input via
    a `user_input_request` message but never received a `user_input_response`,
    AND the task ultimately died (cancelled / failed). The orchestrator does
    not finalize tasks that are still legitimately paused for input, so a
    final_result with an unanswered question implies the user gave up."""
    status = _get(task_row, "status")
    # Only meaningful at terminal status — a still-paused task has not failed
    # to receive an answer, it's still waiting.
    if status not in ("cancelled", "failed", "completed"):
        return
    msg_list = list(_iter_messages(messages))
    had_request = any(m.get("message_type") == "user_input_request" for m in msg_list)
    had_response = any(m.get("message_type") == "user_input_response" for m in msg_list)
    if had_request and not had_response:
        causes.append(FailureCause.CLARIFICATION_UNANSWERED)


def _apply_permission_denied_rule(causes: list[FailureCause], final_result: Any) -> None:
    """`permission_denied` when any error code carries permission / denied,
    or when the orchestrator-side approval flow surfaced a denial. (We do not
    join the approvals table here — that adds a DB hit per task. Errors are
    the canonical inline signal.)"""
    for err in _iter_errors(final_result):
        code = str(err.get("code") or "").lower()
        message = str(err.get("message") or "").lower()
        haystack = code + " " + message
        if any(t in haystack for t in _PERMISSION_TOKENS):
            causes.append(FailureCause.PERMISSION_DENIED)
            return


def _apply_repetition_rule(causes: list[FailureCause], final_result: Any) -> None:
    """`repetition_loop_backstop` when the orchestrator fired its
    repetition / loop backstop. The code surfaces as `LOOP_DETECTED` /
    `repetition_loop`; we cast a wide net so a future rename doesn't silently
    stop tagging."""
    for err in _iter_errors(final_result):
        code = str(err.get("code") or "").lower()
        message = str(err.get("message") or "").lower()
        haystack = code + " " + message
        if any(t in haystack for t in _REPETITION_TOKENS):
            causes.append(FailureCause.REPETITION_LOOP_BACKSTOP)
            return


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify_failure_causes(
    task_row: Any,
    messages: Optional[Iterable[Any]],
    runs: Optional[Iterable[Any]],
    final_result: Any,
) -> list[FailureCause]:
    """Return the failure-cause tags that apply to this finished task.

    Args:
        task_row: tasks-table row (dict or sqlite3.Row) — at minimum carries
            `status` and (optionally) `user_decision`.
        messages: agent_messages rows for the task. Each may already be
            decoded (dict) or a raw sqlite3.Row.
        runs: agent_runs rows for the task. Used for tool-level timeouts.
        final_result: final_results row OR the just-built FinalResult dict.
            We accept either so the orchestrator can call this both before and
            after the row hits SQLite.

    Returns:
        A de-duplicated list of FailureCause values, preserving the order in
        which the rules fired. On unexpected error returns [] — tagging
        failure must never cascade into the task itself failing.
    """
    try:
        causes: list[FailureCause] = []
        _apply_dissent_rule(causes, task_row, final_result)
        _apply_bad_json_rule(causes, final_result)
        _apply_timeout_rule(causes, runs, final_result)
        _apply_repetition_rule(causes, final_result)
        _apply_multimodal_rule(causes, messages)
        _apply_clarification_unanswered_rule(causes, task_row, messages)
        _apply_permission_denied_rule(causes, final_result)
        # De-dup while preserving first-seen order.
        seen: set[FailureCause] = set()
        out: list[FailureCause] = []
        for c in causes:
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out
    except Exception as e:  # noqa: BLE001 — never let tagging break finalization
        _log.warning("classify_failure_causes failed: %s", e)
        return []
