"""JSON extraction helpers for parsing agent output that may include prose or fences."""

from __future__ import annotations

import json
import re

# Match every fenced block in the response, with capture groups for the language
# tag (so we can prefer ```json over plain ```) and the body. Non-greedy.
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\s*\n?(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict:
    """
    Extract a JSON object from text that may include prose or markdown fences.
    Robust to responses where the agent emits multiple fenced blocks (e.g.,
    code citations) before the final ```json block carrying the structured turn.

    Tries, in order:
      1. Every ```json-tagged fence, in order
      2. Every untagged ``` fence, in REVERSE order (the structured turn is
         almost always the LAST thing the model emits)
      3. The entire text as JSON
      4. The LAST balanced {...} substring (the structured turn is at the end,
         not the beginning — prose-citing examples often include {} in
         passing earlier)
      5. The first balanced {...} substring (final fallback)

    Raises ValueError if no parseable JSON object is found.
    """
    if not text:
        raise ValueError("empty response")

    fences = list(_FENCE_RE.finditer(text))
    # JSON-tagged fences first, in order.
    for m in fences:
        lang = (m.group(1) or "").lower()
        if lang != "json":
            continue
        parsed = _try_parse_dict(m.group(2).strip())
        if parsed is not None:
            return parsed
    # Untagged or non-JSON-tagged fences, in REVERSE order.
    for m in reversed(fences):
        lang = (m.group(1) or "").lower()
        if lang == "json":
            continue  # already tried
        parsed = _try_parse_dict(m.group(2).strip())
        if parsed is not None:
            return parsed

    stripped = text.strip()
    parsed = _try_parse_dict(stripped)
    if parsed is not None:
        return parsed

    # Walk every balanced {...} block and try each. We try the LAST one
    # first because that's where the structured turn lives — prose
    # examples and citations earlier in the response often contain
    # incidental {...} that aren't the real turn.
    candidates = _find_balanced_objects(stripped)
    for candidate in reversed(candidates):
        parsed = _try_parse_dict(candidate)
        if parsed is not None:
            return parsed

    raise ValueError("no parseable JSON object in response")


def _try_parse_dict(s: str):
    """json.loads + isinstance check, returning the dict or None on any failure."""
    if not s:
        return None
    try:
        result = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _find_balanced_objects(stripped: str) -> list[str]:
    """Return every top-level balanced {...} substring in `stripped`, in
    source order. String contents are skipped (so braces inside string
    literals don't affect depth)."""
    out: list[str] = []
    i = 0
    n = len(stripped)
    while i < n:
        if stripped[i] != "{":
            i += 1
            continue
        start = i
        depth = 0
        in_string = False
        escape = False
        j = i
        while j < n:
            ch = stripped[j]
            if escape:
                escape = False
            elif ch == "\\" and in_string:
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(stripped[start:j + 1])
                        i = j + 1
                        break
            j += 1
        else:
            # Hit end of string without closing — move past this `{` and continue.
            i = start + 1
            continue
    return out
