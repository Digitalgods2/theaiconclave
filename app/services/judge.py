"""Convergence judge — semantic-equivalence pass for conclave weak-convergence.

Background: when all conclave participants signal `i_am_done` but their position
texts differ in wording, the orchestrator's `_normalize` function (word-set
Jaccard) currently flags this as `minor_disagreement`. The synthesis round
already runs once; this judge pass runs AFTER synthesis as a final cheap check.

The judge is one of the conclave's participants, invoked outside the conclave
loop with a minimal prompt asking it to rate semantic equivalence. The judge's
turn does NOT appear in the transcript as a participant turn — it's a
post-deliberation arbitration call, recorded separately as a synthetic
"judge_verdict" message so users can see what happened.

If the judge says positions are substantively equivalent, the orchestrator
upgrades `agreement_level` from `minor_disagreement` to `consensus`. If the
judge call fails or is inconclusive, the original `minor_disagreement` stands.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.agents.base import AdapterContext, BaseAdapter
from app.protocol.validators import TaskRequest
from app.utils.json_tools import extract_json_object

logger = logging.getLogger(__name__)


_JUDGE_PROMPT_TEMPLATE = """You are judging whether AI participants in a conclave have produced \
**substantively equivalent** answers, OR **materially different** answers.

Two positions are **substantively equivalent** if they recommend the same action, reach the same conclusion, \
or convey the same answer — even when the words differ. Surface-level differences (phrasing, level of \
detail, framing emphasis) are NOT material difference.

Two positions are **materially different** if they recommend different actions, contradict each other, \
disagree on a load-bearing fact, or focus on substantively different aspects of the question.

## The original question
{question}

## Positions to judge
{positions}

## Your output

Return a single JSON object with this exact shape, and nothing else:

{{
  "equivalent": true | false,
  "reasoning": "<one or two sentences explaining your judgment>"
}}

If you are uncertain, err on the side of `false` (preserve the disagreement signal for the user).
"""


def _format_positions_for_judge(positions: list[dict[str, str]]) -> str:
    lines = []
    for i, p in enumerate(positions, 1):
        agent = p.get("agent", f"agent-{i}")
        text = p.get("position", "")
        lines.append(f"### Position {i} — from {agent}\n{text}\n")
    return "\n".join(lines)


async def judge_convergence(
    positions: list[dict[str, str]],
    task: TaskRequest,
    task_id: str,
    judge_adapter: BaseAdapter,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Ask one adapter to rate semantic equivalence of the given positions.

    Returns:
        {"equivalent": True/False, "reasoning": "...", "judge": "<agent name>"} on success
        {"equivalent": None, "reasoning": "<error>", "judge": "<agent name>"} on failure
    """
    if len(positions) < 2:
        return {"equivalent": True, "reasoning": "Single position; trivially equivalent.",
                "judge": judge_adapter.name}

    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        question=task.user_request,
        positions=_format_positions_for_judge(positions),
    )

    try:
        # We use the adapter's _invoke directly rather than going through one of
        # the role-specific methods, because the judge is not a participant role.
        text = await judge_adapter._invoke(prompt, timeout_seconds)
    except Exception as e:  # noqa: BLE001
        logger.warning("judge_convergence: adapter failed: %s", e)
        return {"equivalent": None, "reasoning": f"judge_failed: {e}",
                "judge": judge_adapter.name}

    try:
        data = extract_json_object(text)
    except ValueError as e:
        logger.warning("judge_convergence: could not parse judge JSON: %s", e)
        return {"equivalent": None, "reasoning": f"judge_parse_failed: {e}",
                "judge": judge_adapter.name}

    equivalent = data.get("equivalent")
    reasoning = data.get("reasoning", "")
    if isinstance(equivalent, bool):
        return {"equivalent": equivalent, "reasoning": reasoning, "judge": judge_adapter.name}

    return {"equivalent": None, "reasoning": "judge returned non-bool 'equivalent'",
            "judge": judge_adapter.name}
