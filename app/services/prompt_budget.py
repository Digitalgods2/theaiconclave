"""Centralized prompt-budget enforcement.

DR0017 follow-on (Batch C): a single source of truth for how many characters
of `prior_messages` are allowed to ride along on any given prompt, applied
uniformly across every public builder in `prompt_builder.py`.

Why this exists
---------------
Before this module, `prompt_builder.py` concatenated `prior_messages`
naively. In deep continue-threads with multiple participants, the cumulative
transcript could exceed an adapter's context window — silently, since CLI
adapters have no feedback loop. Gemini CLI was observed timing out at 240s
in a 5-task continue thread because the prompt size grew round-over-round.

What this does
--------------
- Computes a per-call budget from the adapter's `max_context_chars` (or
  OpenRouter's effective ceiling, which is `min(configured, learned)`).
- Reserves headroom for the schema-demand block and the model's response.
- Returns the largest tail of `prior_messages` (newest-first preserved
  order) that fits under the remaining budget, plus a count of dropped
  oldest messages. The caller emits a marker so the model knows the cut
  happened.

Trim policy
-----------
*Oldest-first.* The recent rounds are the most context-rich; the oldest
turns are usually scene-setting that the agent has already absorbed into
its own reasoning. A future record could revisit this if a real task
shows the older context being load-bearing.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

# Headroom reserved on top of any computed budget, regardless of adapter.
# Covers: schema demand block (~1 KB), the model's response (estimated by
# `_RESPONSE_RESERVE_CHARS`), and tokenizer slop.
_RESPONSE_RESERVE_CHARS = 32_000          # ~8K tokens for the agent's reply
_TOKENIZER_SLOP_CHARS = 4_000             # safety margin for char-to-token mismatch
_SCHEMA_DEMAND_CHARS = 4_000              # rough size of the schema demand block

# Minimum budget under which we don't bother including prior_messages at all.
# Below this, every message would be marker-dropped anyway.
_MIN_INCLUDE_BUDGET = 2_000


def reserved_overhead() -> int:
    """Total characters reserved out of the ceiling before prior_messages are
    considered. Exposed for callers that want to compute their own budget."""
    return _RESPONSE_RESERVE_CHARS + _TOKENIZER_SLOP_CHARS + _SCHEMA_DEMAND_CHARS


def prior_messages_budget(ceiling_chars: int, already_used_chars: int) -> int:
    """Return the byte budget remaining for prior_messages after subtracting
    overhead and what the rest of the prompt has already consumed."""
    raw = ceiling_chars - already_used_chars - reserved_overhead()
    return max(0, raw)


def trim_prior_messages(
    prior_messages: list[dict],
    formatter: Callable[[dict], str],
    budget_chars: int,
) -> tuple[list[str], int]:
    """Drop oldest messages until the formatted total fits under `budget_chars`.

    Returns `(formatted_lines, dropped_count)`. `formatted_lines` is the list
    of `formatter(m)` outputs that survived, in original chronological order
    (oldest of the survivors first). `dropped_count` is the number of
    oldest entries removed; if non-zero, the caller should inject a
    "[N earlier turns omitted...]" marker before the surviving block.

    If `budget_chars <= _MIN_INCLUDE_BUDGET`, *everything* is dropped — the
    marker alone is more honest than three half-truncated turns.
    """
    if not prior_messages:
        return [], 0
    if budget_chars <= _MIN_INCLUDE_BUDGET:
        return [], len(prior_messages)

    # Format once; accumulate from the newest end backwards until we'd exceed
    # the budget. Each formatted message includes a trailing blank line in the
    # builder; the per-message char-count below estimates that.
    formatted = [formatter(m) for m in prior_messages]
    sizes = [len(s) + 1 for s in formatted]  # +1 for the trailing newline marker the builder adds

    total = 0
    keep_start = len(formatted)  # index of first message we keep (exclusive at top, inclusive going down)
    for i in range(len(formatted) - 1, -1, -1):
        if total + sizes[i] > budget_chars:
            break
        total += sizes[i]
        keep_start = i

    dropped = keep_start
    return formatted[keep_start:], dropped


def omitted_marker(dropped_count: int) -> str:
    """Standard marker the builder injects when oldest messages were dropped.

    Phrasing tells the model what happened and why so it doesn't fabricate
    missing context. Singular form for the dropped=1 case.
    """
    if dropped_count == 1:
        return "[1 earlier turn omitted to fit the prompt budget.]"
    return f"[{dropped_count} earlier turns omitted to fit the prompt budget.]"


__all__ = [
    "prior_messages_budget",
    "reserved_overhead",
    "trim_prior_messages",
    "omitted_marker",
]
