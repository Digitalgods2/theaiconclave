"""Tests for Decision Memory retrieval (Phase 2.5 of post-DR plan
tsk_01KRSW6AS3M66B4RRJE3JFAPRV).

Covers:
- TF-IDF tokenization (stopwords, lowercasing, min-length)
- Decision file parsing (number, title, date, summary)
- Retrieval ranks the obvious match first
- Empty query / no corpus → empty result
- min_score filter excludes weak matches
- top_k caps the result count
- format_for_prompt produces the expected markdown shape
- Real corpus sanity: known queries hit known decisions
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from app.services import decision_memory as dm


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a clean index cache."""
    dm.clear_cache()
    yield
    dm.clear_cache()


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

def test_tokenize_lowercases_and_drops_stopwords():
    toks = dm._tokenize("The AI Conclave deliberates with FOUR agents.")
    assert "conclave" in toks
    assert "deliberates" in toks
    assert "agents" in toks
    assert "the" not in toks  # stopword
    assert "with" not in toks  # stopword


def test_tokenize_drops_short_tokens():
    toks = dm._tokenize("a b ai of go to")
    # Length < 3 after the regex pattern excludes everything
    assert toks == []


# ---------------------------------------------------------------------------
# Decision file parsing
# ---------------------------------------------------------------------------

def test_parse_decision_extracts_metadata(tmp_path):
    p = tmp_path / "0042_test_decision.md"
    p.write_text(dedent("""\
        # Decision Record 0042 — Test Title For Parsing

        **Date**: 2026-05-16
        **Mode**: Glen-directed

        ## What Was Chosen

        We picked option A because option B was worse. This is the summary paragraph.

        Second paragraph that should not be in summary.

        ## Why It Was Chosen

        Some reasons.
    """), encoding="utf-8")
    out = dm._parse_decision(p)
    assert out["number"] == "0042"
    assert out["title"] == "Test Title For Parsing"
    assert out["date"]  == "2026-05-16"
    assert "option A" in out["summary"]
    assert "Second paragraph" not in out["summary"]
    assert out["path"].endswith("0042_test_decision.md")


def test_parse_decision_handles_missing_what_was_chosen(tmp_path):
    p = tmp_path / "0099_minimal.md"
    p.write_text("# Decision Record 0099 — Minimal\n\n**Date**: 2026-01-01\n", encoding="utf-8")
    out = dm._parse_decision(p)
    assert out["number"] == "0099"
    assert out["summary"] == ""


def test_parse_decision_returns_none_for_malformed_filename(tmp_path):
    p = tmp_path / "not-a-decision.md"
    p.write_text("# Whatever", encoding="utf-8")
    assert dm._parse_decision(p) is None


# ---------------------------------------------------------------------------
# Retrieval against the real corpus
# ---------------------------------------------------------------------------

def test_real_corpus_openrouter_query_hits_openrouter_record():
    matches = dm.find_relevant("OpenRouter API key storage", top_k=3)
    assert len(matches) >= 1
    assert matches[0]["number"] == "0011"


def test_real_corpus_multimodal_query_hits_multimodal_record():
    matches = dm.find_relevant("multimodal disagreement when agents see different images", top_k=3)
    assert len(matches) >= 1
    assert matches[0]["number"] == "0002"


def test_real_corpus_export_query_hits_export_record():
    matches = dm.find_relevant("export task as pdf docx", top_k=3)
    assert len(matches) >= 1
    assert matches[0]["number"] == "0008"


def test_real_corpus_returns_empty_for_unrelated_query():
    matches = dm.find_relevant("how do I bake a chocolate cake", top_k=3)
    # Stopwords drop "how do I", leaving "bake chocolate cake" — none of which
    # appear meaningfully in the technical corpus.
    assert matches == []


# ---------------------------------------------------------------------------
# Behavior edge cases
# ---------------------------------------------------------------------------

def test_empty_query_returns_empty():
    assert dm.find_relevant("") == []
    assert dm.find_relevant("   ") == []
    assert dm.find_relevant(None) == []  # type: ignore[arg-type]


def test_min_score_threshold_filters_weak_matches():
    # Same query, but require near-perfect match — should drop everything.
    matches = dm.find_relevant("OpenRouter API key", top_k=10, min_score=0.99)
    assert matches == []


def test_top_k_caps_result_count():
    matches = dm.find_relevant("openrouter sandbox decision charter", top_k=2, min_score=0.0)
    assert len(matches) <= 2


def test_results_sorted_by_score_descending():
    matches = dm.find_relevant("OpenRouter API key storage settings", top_k=5, min_score=0.0)
    if len(matches) >= 2:
        scores = [m["score"] for m in matches]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def test_format_for_prompt_empty_returns_empty_string():
    assert dm.format_for_prompt([]) == ""


def test_format_for_prompt_includes_each_match():
    matches = [
        {"number": "0011", "title": "OpenRouter seats", "date": "2026-05-12",
         "summary": "We picked OpenRouter.", "path": "docs/decisions/0011.md", "score": 0.42},
        {"number": "0010", "title": "Settings panel",   "date": "2026-05-12",
         "summary": "Built the panel.",     "path": "docs/decisions/0010.md", "score": 0.21},
    ]
    out = dm.format_for_prompt(matches)
    assert "Decision 11" in out
    assert "Decision 10" in out
    assert "OpenRouter seats" in out
    assert "Prior Art" in out
    assert "relevance 0.42" in out
    assert "docs/decisions/0011.md" in out


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Supersession detection + filtering
# ---------------------------------------------------------------------------

def test_parse_detects_superseded_banner(tmp_path):
    p = tmp_path / "0009_old.md"
    p.write_text(dedent("""\
        # Decision Record 0009 — Old Choice

        > **Status: SUPERSEDED.** Demoted to optional in [DR0011](0011_x.md)
        > and removed entirely in [DR0014](0014_y.md).

        **Date**: 2026-05-12

        ## What Was Chosen

        We chose option X back then.
    """), encoding="utf-8")
    out = dm._parse_decision(p)
    assert out["superseded"] is True
    # superseded_by lists DRs referenced in the banner, leading zeros stripped,
    # and the record's own number excluded.
    assert "11" in out["superseded_by"]
    assert "14" in out["superseded_by"]
    assert "9" not in out["superseded_by"]


def test_parse_non_superseded_returns_false(tmp_path):
    p = tmp_path / "0001_fresh.md"
    p.write_text("# Decision Record 0001 — Fresh\n\n**Date**: 2026-05-12\n\n## What Was Chosen\n\nWe chose X.\n",
                 encoding="utf-8")
    out = dm._parse_decision(p)
    assert out["superseded"] is False
    assert out["superseded_by"] == []


def test_real_corpus_dr0009_is_superseded():
    """DR0009 (Ollama Cloud seats) was superseded by DR0014 in May 2026."""
    matches = dm.find_relevant("Ollama Cloud council seats", top_k=10,
                                min_score=0.0, include_superseded=True)
    by_num = {m["number"]: m for m in matches}
    assert "0009" in by_num, "DR0009 should be findable when include_superseded=True"
    assert by_num["0009"]["superseded"] is True
    assert "14" in by_num["0009"]["superseded_by"]


def test_default_retrieval_filters_superseded():
    """Default behavior: DR0009 must NOT appear in retrieval results."""
    matches = dm.find_relevant("Ollama Cloud council seats", top_k=10, min_score=0.0)
    for m in matches:
        assert m["number"] != "0009", (
            "DR0009 leaked into default retrieval — superseded records are "
            "history, not guidance, and shouldn't be fed to agents."
        )


def test_include_superseded_escape_hatch():
    """Explicit opt-in surfaces superseded records anyway."""
    nums_no_sup  = {m["number"] for m in dm.find_relevant(
        "Ollama Cloud council seats", top_k=20, min_score=0.0)}
    nums_with_sup = {m["number"] for m in dm.find_relevant(
        "Ollama Cloud council seats", top_k=20, min_score=0.0, include_superseded=True)}
    assert "0009" not in nums_no_sup
    assert "0009" in nums_with_sup


def test_enrich_with_supersession_annotates_historical_entries():
    """Historical prior_art entries (no superseded field) get annotated from
    the live corpus on read."""
    stale = [{"number": "0009", "title": "Old", "date": "2026-05-12",
              "summary": "...", "path": "docs/decisions/0009_x.md", "score": 0.12}]
    enriched = dm.enrich_with_supersession(stale)
    assert enriched[0]["superseded"] is True
    assert "14" in enriched[0]["superseded_by"]


def test_enrich_preserves_non_superseded_entries():
    fresh = [{"number": "0011", "title": "OpenRouter", "date": "2026-05-12",
              "summary": "...", "path": "docs/decisions/0011_x.md", "score": 0.13}]
    enriched = dm.enrich_with_supersession(fresh)
    assert enriched[0]["superseded"] is False


def test_index_cache_rebuilds_when_file_added(tmp_path, monkeypatch):
    # Point the module at an isolated decisions dir.
    monkeypatch.setattr(dm, "_DECISIONS_DIR", tmp_path)
    dm.clear_cache()
    # Initial state: empty corpus
    assert dm.find_relevant("anything") == []
    # Add a file
    p = tmp_path / "0001_synthetic.md"
    p.write_text(dedent("""\
        # Decision Record 0001 — Synthetic Test Topic

        **Date**: 2026-05-16

        ## What Was Chosen

        We chose synthetic test topic because it was synthetic.
    """), encoding="utf-8")
    # Index should rebuild and find it
    matches = dm.find_relevant("synthetic test topic", top_k=1)
    assert len(matches) == 1
    assert matches[0]["number"] == "0001"
