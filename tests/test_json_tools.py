"""Tests for app.utils.json_tools.extract_json_object.

Covers the basic shapes plus the failure mode that lost claude-code's
critique on tsk_01KRVFFCN6WZ6KMFBDAWV4AN8Y: a response with one or more
prose code-fence blocks (file citations, code snippets) BEFORE the
structured-turn ```json block. The original non-greedy first-fence regex
returned the wrong block, the JSON parse failed, and the agent's best
output was discarded.
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from app.utils.json_tools import extract_json_object


def test_bare_json_object():
    out = extract_json_object('{"a": 1, "b": "x"}')
    assert out == {"a": 1, "b": "x"}


def test_json_fenced():
    out = extract_json_object('```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_untagged_fence():
    out = extract_json_object('```\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_prose_before_fenced_json():
    text = dedent("""\
        Here is my analysis. The key issue is X.

        ```json
        {"summary": "X is the issue", "confidence": 0.9}
        ```
    """)
    out = extract_json_object(text)
    assert out["summary"] == "X is the issue"


def test_regression_code_fence_before_json_fence(tmp_path):
    """The original bug: claude-code emitted a Python code snippet in a ```python
    fence before its structured ```json fence. The non-greedy first-fence
    matcher grabbed the Python block, failed to parse as JSON, then the
    fallbacks couldn't recover because the JSON was at the end of a long
    response with incidental {} earlier in prose. Fix: try all fences,
    preferring ```json-tagged, then untagged in reverse order."""
    text = dedent("""\
        Reading the orchestrator, I see this pattern:

        ```python
        async def run_task(task_id):
            task = _load_task(task_id)
            if task is None:
                return
        ```

        The issue is at line 1033 — there's no check for sandbox availability.

        ```json
        {"agreement": "partial", "critique": "load_task should validate sandbox", "confidence": 0.85}
        ```
    """)
    out = extract_json_object(text)
    assert out["agreement"] == "partial"
    assert out["confidence"] == 0.85


def test_multiple_code_fences_then_json_fence():
    """Even with multiple language-tagged fences before the JSON, extraction
    must find the json-tagged block."""
    text = dedent("""\
        Let me check:

        ```python
        x = 1
        ```

        ```yaml
        key: value
        ```

        ```bash
        echo hello
        ```

        Final result:

        ```json
        {"verdict": "ok"}
        ```
    """)
    out = extract_json_object(text)
    assert out == {"verdict": "ok"}


def test_no_json_tag_but_fence_at_end():
    """If the model didn't tag the fence as `json`, the LAST untagged fence
    is the structured turn — try those in reverse order."""
    text = dedent("""\
        Analysis goes here.

        ```python
        def foo(): return 1
        ```

        ```
        {"verdict": "ok", "confidence": 0.8}
        ```
    """)
    out = extract_json_object(text)
    assert out == {"verdict": "ok", "confidence": 0.8}


def test_prose_with_incidental_braces_before_json_at_end():
    """Critical regression: the response has incidental {} in PROSE before
    the actual JSON (e.g., the agent quotes a config snippet). The
    last-balanced-object fallback must find the structured turn at the END,
    not the prose example at the beginning."""
    text = dedent("""\
        The config currently has:

            extra: {"include_sandbox": true}

        which is set but never read. The actual answer:

        {"summary": "incidental braces", "verdict": "found"}
    """)
    out = extract_json_object(text)
    # The {"include_sandbox": true} dict IS valid JSON, but the LAST
    # balanced object is the answer we want.
    assert out["summary"] == "incidental braces"
    assert out["verdict"] == "found"


def test_braces_inside_string_literals_dont_break_parser():
    text = dedent("""\
        ```json
        {"note": "this string contains { and } characters", "ok": true}
        ```
    """)
    out = extract_json_object(text)
    assert out["ok"] is True
    assert "{" in out["note"]


def test_escaped_quotes_in_strings():
    text = '{"q": "she said \\"hi\\" then left"}'
    out = extract_json_object(text)
    assert "hi" in out["q"]


def test_empty_raises():
    with pytest.raises(ValueError):
        extract_json_object("")


def test_no_json_at_all_raises():
    with pytest.raises(ValueError):
        extract_json_object("just prose, no braces anywhere.")


def test_malformed_json_in_only_fence_raises():
    with pytest.raises(ValueError):
        extract_json_object('```json\n{"unterminated": tru\n```')


def test_array_not_object_raises():
    """A JSON array at the top level is not a valid 'object' — we want dicts."""
    with pytest.raises(ValueError):
        extract_json_object('[1, 2, 3]')


def test_long_response_real_world_shape():
    """Approximates the failure mode on tsk_01KRVFFCN6WZ6KMFBDAWV4AN8Y:
    long analysis with multiple cited code snippets, structured turn at end."""
    text = (
        "I read the orchestrator.\n\n"
        "Section 1: startup\n\n"
        "```python\nasync def lifespan(app):\n    init_database(...)\n```\n\n"
        "This calls init at line 53.\n\n"
        "Section 2: sandbox\n\n"
        "```python\nSANDBOXES_ROOT = Path('data/sandboxes')\n```\n\n"
        "Note this is a relative path.\n\n"
        "Section 3: registry\n\n"
        "```python\ndef register(adapter):\n    _REG[adapter.name] = adapter\n```\n\n"
        "And here's the conclusion as a structured turn:\n\n"
        '```json\n'
        '{\n'
        '  "agent": "claude-code",\n'
        '  "agreement": "partial",\n'
        '  "critique": "Three issues found...",\n'
        '  "missed_risks": ["A", "B"],\n'
        '  "suggested_questions": ["Q1?"],\n'
        '  "confidence": 0.92,\n'
        '  "wants_continuation": false\n'
        '}\n'
        '```\n'
    )
    out = extract_json_object(text)
    assert out["agent"] == "claude-code"
    assert out["agreement"] == "partial"
    assert out["confidence"] == 0.92
