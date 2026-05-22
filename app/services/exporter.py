"""Decision-record markdown exporter.

Pure formatting module: takes structured task data (already loaded by the API
route) and returns a single markdown string. Does not touch the database or
filesystem - the caller is responsible for I/O.

The export is intended to be the "preserve this for the long term" path for a
conclave task: it must include every field a future reader would need to
reconstruct the discussion (question, decision, final result, full transcript,
usage), and it must preserve agent prose verbatim (no truncation of analysis,
critique, etc.) so that nuance survives the snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_to_markdown(
    task: dict,
    messages: list[dict],
    final_result: dict | None,
    agent_runs: list[dict],
    artifacts: list[dict] | None = None,
) -> str:
    """Render a single task as a markdown decision record.

    Args:
        task: Task envelope - same shape returned by GET /api/tasks/{id}["task"].
        messages: List of agent message dicts (same shape as ["messages"]).
        final_result: final_result dict or None.
        agent_runs: List of agent_runs dicts (same shape as ["agent_runs"]).

    Returns:
        A markdown string. Always non-empty; safe to write to a .md file.
    """
    parts: list[str] = []
    parts.append(_render_header(task))
    parts.append(_render_question(task))
    parts.append(_render_decision(task))
    parts.append(_render_final_result(final_result))
    parts.append(_render_artifacts(artifacts or []))
    parts.append(_render_transcript(task, messages))
    parts.append(_render_usage(agent_runs))
    parts.append(_render_footer())
    # Join sections with a blank line separator; trailing newline for POSIX-friendliness.
    return "\n\n".join(p.rstrip() for p in parts) + "\n"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_header(task: dict) -> str:
    tid = task.get("id") or "(unknown)"
    lines: list[str] = [f"# Task {tid}", ""]
    rows: list[tuple[str, str]] = []
    if task.get("status"):
        rows.append(("Status", str(task["status"])))
    if task.get("mode"):
        rows.append(("Mode", str(task["mode"])))
    if task.get("task_type"):
        rows.append(("Task type", str(task["task_type"])))
    agents = _agents_summary(task)
    if agents:
        rows.append(("Agents", agents))
    if task.get("created_at"):
        rows.append(("Created at", str(task["created_at"])))
    if task.get("updated_at"):
        rows.append(("Updated at", str(task["updated_at"])))
    if task.get("parent_task_id"):
        rows.append(("Parent task", str(task["parent_task_id"])))
    if task.get("error_message"):
        rows.append(("Error", str(task["error_message"])))
    for k, v in rows:
        lines.append(f"- **{k}:** {v}")
    return "\n".join(lines)


def _render_artifacts(artifacts: list[dict]) -> str:
    lines: list[str] = ["## Draft artifacts", ""]
    if not artifacts:
        lines.append("_(none)_")
        return "\n".join(lines)
    for i, artifact in enumerate(artifacts, 1):
        metadata = artifact.get("metadata") or {}
        lines.append(f"### {i}. {artifact.get('title') or artifact.get('filename') or artifact.get('id')}")
        lines.append("")
        lines.append(f"- **ID:** {artifact.get('id')}")
        lines.append(f"- **Kind:** {artifact.get('kind')}")
        lines.append(f"- **Filename:** {artifact.get('filename')}")
        if metadata.get("target_path"):
            lines.append(f"- **Target path:** {metadata['target_path']}")
        if metadata.get("apply_mode"):
            lines.append(f"- **Apply mode:** {metadata['apply_mode']}")
        if metadata.get("applied_at"):
            lines.append(f"- **Applied at:** {metadata['applied_at']}")
        content = artifact.get("content")
        if isinstance(content, str) and content:
            lines.append("")
            lines.append(_fenced(content, lang="text"))
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_question(task: dict) -> str:
    text = task.get("user_request") or ""
    return "## Question\n\n" + _fenced(text, lang="text")


def _render_decision(task: dict) -> str:
    decision = task.get("user_decision")
    decided_at = task.get("user_decided_at")
    lines: list[str] = ["## Decision", ""]
    if isinstance(decision, str) and decision.strip():
        if decided_at:
            lines.append(f"_Recorded at {decided_at}._")
            lines.append("")
        lines.append("> " + decision.strip().replace("\n", "\n> "))
    else:
        lines.append("_(none recorded)_")
    return "\n".join(lines)


def _render_final_result(final_result: dict | None) -> str:
    lines: list[str] = ["## Final result", ""]
    if not final_result:
        lines.append("_(no final result was produced)_")
        return "\n".join(lines)

    answer = final_result.get("final_answer") or ""
    lines.append("### Final answer")
    lines.append("")
    lines.append(_fenced(answer, lang="text"))
    lines.append("")

    if final_result.get("agreement_level"):
        lines.append(f"- **Agreement level:** {final_result['agreement_level']}")
    if final_result.get("resolution_status"):
        lines.append(f"- **Resolution status:** {final_result['resolution_status']}")

    action_plan = final_result.get("action_plan") or []
    if action_plan:
        lines.append("")
        lines.append("### Structured action plan")
        lines.append("")
        for step in action_plan:
            if isinstance(step, dict):
                number = step.get("step_number", "?")
                summary = step.get("summary") or "(no summary)"
                lines.append(f"**{number}. {summary}**")
                lines.append("")
                details: list[str] = []
                if step.get("action_type"):
                    details.append(f"action type: {step['action_type']}")
                if step.get("target"):
                    details.append(f"target: {step['target']}")
                if step.get("policy_status"):
                    details.append(f"policy: {step['policy_status']}")
                if step.get("required_permissions"):
                    details.append("required permissions: " + ", ".join(map(str, step["required_permissions"])))
                for detail in details:
                    lines.append(f"- {detail}")
                reasons = step.get("policy_reasons") or []
                for reason in reasons:
                    lines.append(f"- reason: {reason}")
                lines.append("")
            else:
                lines.append("- " + _value_as_inline(step))

    disagreements = final_result.get("disagreements") or []
    if disagreements:
        lines.append("")
        lines.append("### Disagreements")
        lines.append("")
        for i, d in enumerate(disagreements, 1):
            lines.append(f"**{i}. {d.get('topic', '(untitled)')}**")
            lines.append("")
            for key in ("primary_position", "consultant_position"):
                if d.get(key):
                    lines.append(f"- _{_prettify(key)}:_")
                    lines.append("")
                    lines.append(_indented_block(_value_as_text(d[key]), prefix="  "))
                    lines.append("")
            for k, v in d.items():
                if k in ("topic", "primary_position", "consultant_position"):
                    continue
                if _empty(v):
                    continue
                lines.append(f"- _{_prettify(k)}:_ {_value_as_inline(v)}")
            lines.append("")

    for label_key in ("recommended_actions", "risks", "commands_requiring_approval",
                      "patches_requiring_approval"):
        items = final_result.get(label_key) or []
        if items:
            lines.append("")
            lines.append(f"### {_prettify(label_key)}")
            lines.append("")
            for item in items:
                lines.append("- " + _value_as_inline(item))

    errors = final_result.get("errors") or []
    if errors:
        lines.append("")
        lines.append("### Errors")
        lines.append("")
        for err in errors:
            lines.append("- " + _value_as_inline(err))

    return "\n".join(lines)


def _render_transcript(task: dict, messages: list[dict]) -> str:
    lines: list[str] = ["## Transcript", ""]
    msgs = list(messages or [])
    if not msgs:
        lines.append("_(no messages recorded)_")
        return "\n".join(lines)

    mode = (task.get("mode") or "").lower()
    grouped = _group_by_round(msgs) if mode == "conclave" else None
    if grouped is not None and len(grouped) > 0:
        for round_num in sorted(grouped.keys(), key=_round_sort_key):
            label = f"Round {round_num}" if round_num is not None else "Unsorted"
            lines.append(f"### {label}")
            lines.append("")
            for m in grouped[round_num]:
                lines.append(_render_message(m))
                lines.append("")
    else:
        for m in msgs:
            lines.append(_render_message(m))
            lines.append("")

    return "\n".join(lines).rstrip()


def _render_message(m: dict) -> str:
    # DR0015: tool-loop events render compactly as one-liners (with optional
    # collapsible payload). Otherwise a 5-round conclave with 4 file reads
    # per round would balloon the export to hundreds of lines of repeated
    # structured-field dumps.
    mtype = m.get("message_type") or ""
    if mtype in ("tool_call", "tool_result"):
        return _render_tool_message(m)

    agent = m.get("agent_name") or "?"
    role = m.get("role") or ""
    created_at = m.get("created_at") or ""

    head = f"#### {agent}"
    bits: list[str] = []
    if role:
        bits.append(f"role: {role}")
    if mtype:
        bits.append(f"type: {mtype}")
    if m.get("direction"):
        bits.append(f"direction: {m['direction']}")
    if created_at:
        bits.append(f"time: {created_at}")
    meta = " _(" + ", ".join(bits) + ")_" if bits else ""

    lines: list[str] = [head + meta, ""]

    structured = m.get("structured")
    if isinstance(structured, dict) and structured:
        for key, value in structured.items():
            if _empty(value):
                continue
            label = _prettify(key)
            lines.append(f"**{label}:**")
            lines.append("")
            lines.append(_value_as_block(value))
            lines.append("")
    else:
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            lines.append(_fenced(content, lang="text"))

    return "\n".join(lines).rstrip()


def _render_tool_message(m: dict) -> str:
    """One-line summary of a tool_call / tool_result message for the export."""
    structured = m.get("structured") or {}
    agent = m.get("agent_name") or "?"
    mtype = m.get("message_type") or ""
    fn = structured.get("function", "?")
    if mtype == "tool_call":
        args_raw = structured.get("arguments", "{}")
        try:
            import json as _json
            args = _json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            arg_str = ", ".join(f"{k}={_json.dumps(v)}" for k, v in args.items())
        except (ValueError, TypeError):
            arg_str = str(args_raw)
        return f"- _{agent}_ → **{fn}**({arg_str})"
    # tool_result
    ok = bool(structured.get("ok"))
    result = structured.get("result") or {}
    bytes_n = structured.get("bytes")
    if not ok:
        detail = result.get("error", "")
        body = f"error — {detail}"
    elif isinstance(result.get("content"), str):
        truncated = " (truncated)" if result.get("truncated") else ""
        body = f"ok — {len(result['content'])} chars{truncated}"
    elif isinstance(result.get("entries"), list):
        body = f"ok — {len(result['entries'])} entries"
    elif isinstance(result.get("paths"), list):
        cap = " (cap hit)" if result.get("truncated") else ""
        body = f"ok — {len(result['paths'])} paths{cap}"
    else:
        body = "ok"
    bytes_tag = f" [{bytes_n} B]" if isinstance(bytes_n, (int, float)) else ""
    return f"- _{agent}_ ← **{fn}**: {body}{bytes_tag}"


def _render_usage(agent_runs: list[dict]) -> str:
    lines: list[str] = ["## Usage", ""]
    runs = list(agent_runs or [])
    if not runs:
        lines.append("_(no run accounting recorded)_")
        return "\n".join(lines)

    total_in = 0
    total_out = 0
    total_cost = 0.0
    have_tokens = False
    have_cost = False

    for r in runs:
        it = r.get("input_tokens")
        ot = r.get("output_tokens")
        cu = r.get("cost_usd")
        if isinstance(it, (int, float)):
            total_in += int(it)
            have_tokens = True
        if isinstance(ot, (int, float)):
            total_out += int(ot)
            have_tokens = True
        if isinstance(cu, (int, float)):
            total_cost += float(cu)
            have_cost = True

    if have_tokens:
        lines.append(f"- **Total input tokens:** {total_in}")
        lines.append(f"- **Total output tokens:** {total_out}")
    if have_cost:
        lines.append(f"- **Total cost (USD):** ${total_cost:.4f}")
    if not have_tokens and not have_cost:
        lines.append("_(no token/cost accounting available for this task)_")
        lines.append("")

    lines.append("")
    lines.append("### Per run")
    lines.append("")
    lines.append("| Agent | Role | Round | Status | Duration (ms) | Input tok | Output tok | Cost USD |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in runs:
        lines.append(
            "| "
            + _table_cell(r.get("agent_name"))
            + " | " + _table_cell(r.get("role"))
            + " | " + _table_cell(r.get("round_number"))
            + " | " + _table_cell(r.get("status"))
            + " | " + _table_cell(r.get("duration_ms"))
            + " | " + _table_cell(r.get("input_tokens"))
            + " | " + _table_cell(r.get("output_tokens"))
            + " | " + _table_cell(_fmt_cost(r.get("cost_usd")))
            + " |"
        )
    return "\n".join(lines)


def _render_footer() -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return f"---\n\n_Exported on {ts} by The AI Conclave._"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agents_summary(task: dict) -> str:
    parts: list[str] = []
    if task.get("primary_agent"):
        parts.append(f"{task['primary_agent']} (primary)")
    consultants = task.get("consultants") or []
    if isinstance(consultants, list):
        for c in consultants:
            parts.append(str(c))
    return ", ".join(parts)


def _group_by_round(messages: list[dict]) -> dict[Any, list[dict]]:
    """Group messages by structured.round if present; else into a single bucket."""
    grouped: dict[Any, list[dict]] = {}
    for m in messages:
        structured = m.get("structured")
        rnd: Any = None
        if isinstance(structured, dict):
            rnd = structured.get("round") or structured.get("round_number")
        grouped.setdefault(rnd, []).append(m)
    # If literally every message has rnd=None, return a single 'None' bucket so
    # the caller falls back to flat ordering.
    if list(grouped.keys()) == [None]:
        return {}
    return grouped


def _round_sort_key(r: Any) -> tuple[int, Any]:
    # None sorts last; numeric rounds sort numerically; strings sort lexically.
    if r is None:
        return (2, 0)
    if isinstance(r, (int, float)):
        return (0, r)
    try:
        return (0, int(str(r)))
    except (TypeError, ValueError):
        return (1, str(r))


def _prettify(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if isinstance(v, (list, dict, tuple, set)) and len(v) == 0:
        return True
    return False


def _fenced(text: str, lang: str = "") -> str:
    """Wrap text in a fenced code block, picking a fence longer than any embedded fence."""
    body = "" if text is None else str(text)
    fence = "```"
    # If body itself contains backtick fences, lengthen ours to avoid breaking out.
    while fence in body:
        fence += "`"
    opener = fence + (lang or "")
    return f"{opener}\n{body}\n{fence}"


def _value_as_text(value: Any) -> str:
    """Render an arbitrary structured value as plain text (no markdown wrapping)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, (str, int, float, bool)):
                out.append(f"- {item}")
            else:
                out.append("- " + _value_as_text(item).replace("\n", "\n  "))
        return "\n".join(out)
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            inner = _value_as_text(v)
            if "\n" in inner:
                out.append(f"{_prettify(str(k))}:")
                out.append(inner)
            else:
                out.append(f"{_prettify(str(k))}: {inner}")
        return "\n".join(out)
    return str(value)


def _value_as_block(value: Any) -> str:
    """Render an arbitrary structured value as a markdown block.

    Strings -> fenced code block (preserves whitespace, escaping not needed).
    Lists/dicts -> nested bullet list.
    """
    if value is None:
        return "_(empty)_"
    if isinstance(value, str):
        return _fenced(value, lang="text")
    if isinstance(value, (int, float, bool)):
        return f"`{value}`"
    if isinstance(value, list):
        if not value:
            return "_(empty list)_"
        lines: list[str] = []
        for item in value:
            if isinstance(item, str):
                lines.append("- " + _inline_str(item))
            elif isinstance(item, (int, float, bool)):
                lines.append(f"- `{item}`")
            else:
                # Render nested structures as text and indent under a bullet.
                txt = _value_as_text(item)
                lines.append("- " + txt.replace("\n", "\n  "))
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "_(empty)_"
        lines = []
        for k, v in value.items():
            inner = _value_as_text(v)
            if "\n" in inner or len(inner) > 80:
                lines.append(f"- **{_prettify(str(k))}:**")
                lines.append(_indented_block(inner, prefix="  "))
            else:
                lines.append(f"- **{_prettify(str(k))}:** {_inline_str(inner)}")
        return "\n".join(lines)
    return f"`{value}`"


def _value_as_inline(value: Any) -> str:
    """Compact single-line representation - used inside bullet lists."""
    if value is None:
        return "_(none)_"
    if isinstance(value, str):
        return _inline_str(value)
    if isinstance(value, (int, float, bool)):
        return f"`{value}`"
    if isinstance(value, (list, dict)):
        # Flatten to a single-line text representation.
        return _inline_str(_value_as_text(value).replace("\n", "; "))
    return _inline_str(str(value))


def _inline_str(s: str) -> str:
    """Escape inline-breaking characters for safe rendering in a bullet/cell."""
    if s is None:
        return ""
    # Don't truncate; just collapse newlines so it stays on one line.
    s = str(s).replace("\r", "")
    if "\n" in s:
        # Multiline inline: surface as a soft-broken markdown string.
        s = s.replace("\n", " ")
    return s


def _indented_block(text: str, prefix: str = "  ") -> str:
    if text is None:
        return ""
    return "\n".join(prefix + line if line else "" for line in str(text).splitlines())


def _table_cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    # Escape pipes and collapse newlines so the row doesn't break.
    s = s.replace("|", "\\|").replace("\n", " ")
    return s


def _fmt_cost(c: Any) -> str:
    if isinstance(c, (int, float)):
        return f"{float(c):.4f}"
    return ""


__all__ = ["export_to_markdown"]
