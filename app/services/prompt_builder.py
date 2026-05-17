"""Prompt builder.

Assembles the text prompt sent to a real agent CLI by combining:
1. The relevant role skill (resolution / primary / consultant)
2. The safety skill
3. Task framing (user request, type, permissions)
4. Prior messages from the transcript
5. An explicit output-schema demand

Source-of-truth for skill content: skills/generic/*.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.protocol.validators import TaskMode, TaskRequest
from app.utils import file_loader

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills" / "generic"

_skill_cache: dict[str, str] = {}


def _load_skill(name: str) -> str:
    if name not in _skill_cache:
        path = _SKILLS_DIR / f"{name}.md"
        _skill_cache[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return _skill_cache[name]


# ---------------------------------------------------------------------------
# Output-schema demands
# ---------------------------------------------------------------------------

def _primary_schema_demand(task_id: str, agent: str) -> str:
    return (
        "# Required Output\n"
        "Return a single JSON object with this exact shape, and nothing else "
        "(no prose, no markdown fences, no trailing commentary):\n\n"
        "{\n"
        '  "protocol_version": "1.0",\n'
        f'  "task_id": "{task_id}",\n'
        f'  "agent": "{agent}",\n'
        '  "role": "primary",\n'
        '  "message_type": "primary_proposal",\n'
        '  "summary": "<one or two sentences>",\n'
        '  "analysis": "<your full reasoning>",\n'
        '  "recommended_actions": [\n'
        '    {"kind": "<verify|run_command|edit_file|...>", "description": "...",\n'
        '     "requires_approval": <true|false>, "payload": {}}\n'
        '  ],\n'
        '  "risks": [\n'
        '    {"severity": "<low|medium|high|critical>", "description": "..."}\n'
        '  ],\n'
        '  "confidence": <float 0.0-1.0 or null>,\n'
        '  "resolution_status": "<resolved|needs_more_rounds|needs_user_input|cannot_resolve>",\n'
        '  "user_input_question": "<required only when resolution_status=needs_user_input, else null>"\n'
        "}\n"
    )


def _conclave_schema_demand(task_id: str, agent: str) -> str:
    return (
        "# Required Output\n"
        "Return a single JSON object with this exact shape, and nothing else "
        "(no prose, no markdown fences, no trailing commentary):\n\n"
        "{\n"
        '  "protocol_version": "1.0",\n'
        f'  "task_id": "{task_id}",\n'
        f'  "agent": "{agent}",\n'
        '  "role": "participant",\n'
        '  "message_type": "conclave_turn",\n'
        '  "summary": "<one or two sentences>",\n'
        '  "analysis": "<your full reasoning, engaging with prior turns>",\n'
        '  "position": "<concrete answer you would give the user right now>",\n'
        '  "convergence": "<i_am_done|still_thinking|need_user_input>",\n'
        '  "user_input_question": "<required only when convergence=need_user_input>",\n'
        '  "confidence": <float 0.0-1.0 or null>\n'
        "}\n"
    )


def _consultant_schema_demand(task_id: str, agent: str) -> str:
    return (
        "# Required Output\n"
        "Return a single JSON object with this exact shape, and nothing else "
        "(no prose, no markdown fences, no trailing commentary):\n\n"
        "{\n"
        '  "protocol_version": "1.0",\n'
        f'  "task_id": "{task_id}",\n'
        f'  "agent": "{agent}",\n'
        '  "role": "consultant",\n'
        '  "message_type": "consultant_critique",\n'
        '  "agreement": "<agree|partial|disagree>",\n'
        '  "critique": "<your full critique>",\n'
        '  "missed_risks": ["..."],\n'
        '  "suggested_questions": ["..."],\n'
        '  "confidence": <float 0.0-1.0 or null>,\n'
        '  "wants_continuation": <true|false>\n'
        "}\n"
    )


# ---------------------------------------------------------------------------
# Prior message formatting
# ---------------------------------------------------------------------------

def _format_prior_message(m: dict) -> str:
    mt = m.get("message_type", "?")
    agent = m.get("agent", "?")
    if mt == "synthesis_directive":
        return (
            "## !!! ORCHESTRATOR DIRECTIVE !!!\n"
            f"{m.get('content', '')}\n"
            "## END DIRECTIVE"
        )
    if mt == "conclave_turn":
        # Surface peer confidence so participants can weight each other's positions
        # by stated certainty rather than treating all voices as equal.
        # Phase 2 of post-DR plan on tsk_01KRSW6AS3M66B4RRJE3JFAPRV.
        conf = m.get("confidence")
        conf_line = f"confidence: {conf}\n" if conf is not None else ""
        return (
            f"## {agent} (participant) — conclave turn\n"
            f"convergence: {m.get('convergence', '')}\n"
            f"{conf_line}"
            f"position: {m.get('position', '')}\n"
            f"summary: {m.get('summary', '')}\n"
            f"analysis: {m.get('analysis', '')}"
        )
    if mt == "primary_proposal" or mt == "primary_final":
        rs = m.get("resolution_status", "n/a")
        return (
            f"## {agent} (primary) — {mt}\n"
            f"resolution_status: {rs}\n"
            f"summary: {m.get('summary', '')}\n"
            f"analysis: {m.get('analysis', '')}\n"
            f"recommended_actions: {json.dumps(m.get('recommended_actions', []))}\n"
            f"risks: {json.dumps(m.get('risks', []))}\n"
            f"confidence: {m.get('confidence')}"
        )
    if mt == "consultant_critique":
        return (
            f"## {agent} (consultant) — critique\n"
            f"agreement: {m.get('agreement', '')}\n"
            f"critique: {m.get('critique', '')}\n"
            f"missed_risks: {json.dumps(m.get('missed_risks', []))}\n"
            f"suggested_questions: {json.dumps(m.get('suggested_questions', []))}\n"
            f"wants_continuation: {m.get('wants_continuation', False)}"
        )
    if mt == "user_input_request":
        return f"## (you previously asked the user)\n{m.get('content', '')}"
    if mt == "user_input_response":
        return f"## User answered\n{m.get('content', '')}"
    if mt == "peer_answer":
        return (
            f"## {agent} (peer) — answer\n"
            f"summary: {m.get('summary', '')}\n"
            f"analysis: {m.get('analysis', '')}"
        )
    return f"## {agent} — {mt}\n{m.get('content', '')}"


def _format_task_framing(task: TaskRequest) -> str:
    return (
        "# Task\n"
        f"Task type: {task.task_type.value}\n"
        f"Mode: {task.mode.value}\n"
        f"User request: {task.user_request}\n"
        f"Project path: {task.project_path or '(none)'}\n"
        f"Permissions (do not exceed): {json.dumps(task.permissions.model_dump())}\n"
    )


def _format_thread_ancestors(task: TaskRequest) -> str:
    """Render the prior-thread context for tasks that have ancestors.

    The orchestrator stashes ancestor summaries in context.extra.thread_ancestors
    (oldest first). This section is informational — agents should treat the
    ancestors' decisions as settled background and not re-litigate them unless
    the current task explicitly asks.
    """
    ancestors = task.context.extra.get("thread_ancestors") or []
    if not ancestors:
        return ""

    parts = ["# Prior Thread Context",
             ("This task continues an earlier thread of deliberation. The ancestors below are "
              "settled background — do not re-litigate them unless the current user request "
              "explicitly asks you to. Use them to understand what's already been concluded.")]
    for i, anc in enumerate(ancestors, 1):
        parts.append(f"\n## Ancestor {i} of {len(ancestors)} — {anc.get('id', '?')}")
        parts.append(f"Mode: {anc.get('mode', '?')}")
        if anc.get("user_request"):
            parts.append(f"Question asked: {anc['user_request']}")
        if anc.get("final_answer"):
            parts.append(f"Conclave's final answer ({anc.get('agreement_level', '?')}):")
            parts.append(_truncate_for_thread(anc["final_answer"], 1200))
        if anc.get("user_decision"):
            parts.append("Glen's recorded decision:")
            parts.append(_truncate_for_thread(anc["user_decision"], 800))
            if anc.get("user_decided_at"):
                parts.append(f"Decided at: {anc['user_decided_at']}")
        else:
            parts.append("Glen did not record a decision on this ancestor.")
    return "\n".join(parts)


def _truncate_for_thread(s: str, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + f"\n[truncated at {max_chars} chars]"


def _format_project_sandbox(task: TaskRequest) -> str:
    """Render the sandbox section: path + file manifest, when a sandbox is attached.

    Lazy import of sandbox.build_manifest to avoid a circular import at module
    load time (orchestrator -> prompt_builder, sandbox is loaded by both).
    """
    sandbox_path = task.context.extra.get("sandbox_path")
    if not sandbox_path:
        return ""
    from pathlib import Path
    from app.services.sandbox import build_manifest
    sandbox = Path(sandbox_path)
    manifest = build_manifest(sandbox)
    return (
        "# Project Sandbox\n"
        f"A read-only sandbox copy of the user's project is available at:\n"
        f"  {sandbox_path}\n\n"
        "This is a snapshot of the source tree, fixed at the moment this task started. "
        "You may use your read tools (Codex shell in `-s read-only`, Gemini included-directory, "
        "Claude Read tool) to enumerate the file tree, open files, and reason about the code. "
        "You CANNOT execute, modify, or run any code in the sandbox. Reasoning is static.\n\n"
        "## File Manifest\n"
        f"{manifest if manifest else '(empty)'}\n"
    )


def _format_attachments(task: TaskRequest) -> str:
    """Inline text content from attached files. Images are noted but not embedded."""
    attachments = task.context.extra.get("attachments") or []
    if not attachments:
        return ""

    # Lazy import to avoid a circular dependency at module load time.
    from app.api.uploads import resolve_attachment_path

    parts: list[str] = ["# Attached Files"]
    for att in attachments:
        if not isinstance(att, dict):
            continue
        file_id = att.get("file_id")
        filename = att.get("filename") or "(unknown)"
        if not file_id:
            parts.append(f"\n## {filename}\n[skipped: no file_id]\n")
            continue
        try:
            path = resolve_attachment_path(file_id)
        except (FileNotFoundError, ValueError) as e:
            parts.append(f"\n## {filename}\n[skipped: {e}]\n")
            continue

        if file_loader.is_image(path):
            parts.append(
                f"\n## {filename} (image)\n"
                f"Image file path: {path}\n"
                f"This image has been attached and will be made visible to you "
                f"by your adapter. Examine its actual content (colors, layout, "
                f"composition, text, etc.) as part of your reasoning.\n"
            )
            continue

        try:
            text = file_loader.extract_text(path)
        except Exception as e:  # noqa: BLE001
            parts.append(f"\n## {filename}\n[extraction failed: {e}]\n")
            continue

        if text is None:
            parts.append(
                f"\n## {filename}\n[unsupported file type for text extraction]\n"
            )
            continue

        parts.append(f"\n## {filename}\n```\n{text}\n```\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def build_primary_prompt(
    task: TaskRequest,
    task_id: str,
    agent_name: str,
    prior_messages: list[dict],
) -> str:
    """Prompt for run_primary (resolve and consult both call this)."""
    if task.mode == TaskMode.RESOLVE:
        role_skill = _load_skill("resolution_behavior")
    else:
        role_skill = _load_skill("primary_agent_behavior")
    safety_skill = _load_skill("safety_behavior")

    parts = [
        "# Conclave Charter (Binding)",
        _load_skill("conclave_charter"),
        "",
        "# Your Role",
        role_skill,
        "",
        "# Safety",
        safety_skill,
        "",
        _format_task_framing(task),
        _format_thread_ancestors(task),
        _format_project_sandbox(task),
        _format_attachments(task),
    ]
    if prior_messages:
        parts.append("# Prior Messages (chronological)")
        for m in prior_messages:
            parts.append(_format_prior_message(m))
            parts.append("")
    parts.append(_primary_schema_demand(task_id, agent_name))
    return "\n".join(parts)


def build_consultant_prompt(
    task: TaskRequest,
    task_id: str,
    agent_name: str,
    prior_messages: list[dict],
) -> str:
    """Prompt for run_consultant."""
    role_skill = _load_skill("consultant_behavior")
    safety_skill = _load_skill("safety_behavior")
    parts = [
        "# Conclave Charter (Binding)",
        _load_skill("conclave_charter"),
        "",
        "# Your Role",
        role_skill,
        "",
        "# Safety",
        safety_skill,
        "",
        _format_task_framing(task),
        _format_thread_ancestors(task),
        _format_project_sandbox(task),
        _format_attachments(task),
        "",
        "# Prior Messages (the primary's proposal and any earlier critiques)",
    ]
    for m in prior_messages:
        parts.append(_format_prior_message(m))
        parts.append("")
    parts.append(_consultant_schema_demand(task_id, agent_name))
    return "\n".join(parts)


def build_final_prompt(
    task: TaskRequest,
    task_id: str,
    agent_name: str,
    prior_messages: list[dict],
) -> str:
    """Prompt for consult-mode run_final. Same as primary, but message_type guidance differs."""
    prompt = build_primary_prompt(task, task_id, agent_name, prior_messages)
    # Nudge toward primary_final message_type. Adapter coerces this anyway.
    return prompt.replace(
        '"message_type": "primary_proposal"',
        '"message_type": "primary_final"',
    )


def build_peer_prompt(
    task: TaskRequest,
    task_id: str,
    agent_name: str,
) -> str:
    """Prompt for poll-mode peers. Just primary-shaped output, no critique loop."""
    return build_primary_prompt(task, task_id, agent_name, prior_messages=[])


def build_conclave_prompt(
    task: TaskRequest,
    task_id: str,
    agent_name: str,
    prior_messages: list[dict],
    other_participants: list[str],
) -> str:
    """Prompt for one participant's turn in a conclave round."""
    role_skill = _load_skill("conclave_behavior")
    safety_skill = _load_skill("safety_behavior")
    parts = [
        "# Conclave Charter (Binding)",
        _load_skill("conclave_charter"),
        "",
        "# Your Role",
        role_skill,
        "",
        "# Safety",
        safety_skill,
        "",
        _format_task_framing(task),
        _format_thread_ancestors(task),
        _format_project_sandbox(task),
        _format_attachments(task),
        "",
        f"# Other Participants in This Conclave\n{', '.join(other_participants)}",
        "",
    ]
    if prior_messages:
        parts.append("# Prior Rounds (chronological — every participant's contributions)")
        for m in prior_messages:
            parts.append(_format_prior_message(m))
            parts.append("")
    else:
        parts.append("# Prior Rounds\n(this is round 1; no prior turns yet)")
        parts.append("")
    parts.append(_conclave_schema_demand(task_id, agent_name))
    return "\n".join(parts)
