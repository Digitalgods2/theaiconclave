"""JSON extraction helpers for parsing agent output that may include prose or fences."""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def extract_json_object(text: str) -> dict:
    """
    Extract a JSON object from text that may include prose or markdown fences.
    Tries, in order:
      1. A fenced ```json ... ``` or ``` ... ``` block
      2. The entire text as JSON
      3. The first balanced {...} substring

    Raises ValueError if no parseable JSON object is found.
    """
    if not text:
        raise ValueError("empty response")

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        try:
            result = json.loads(fence_match.group(1).strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    stripped = text.strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start:i + 1]
                try:
                    result = json.loads(candidate)
                    if isinstance(result, dict):
                        return result
                except json.JSONDecodeError:
                    break

    raise ValueError("no parseable JSON object in response")
