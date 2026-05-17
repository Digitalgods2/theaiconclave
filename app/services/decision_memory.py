"""Decision Memory — retrieve relevant past decision records for a new task.

Phase 2.5 of the post-DR plan on tsk_01KRSW6AS3M66B4RRJE3JFAPRV. Operationalizes
the Conclave Charter's "decision records are reusable context for future work"
mandate by making the historical record actively observable instead of passively
stored.

Algorithm: TF-IDF cosine similarity over `docs/decisions/*.md`. Zero external
dependencies. Index is rebuilt on demand when the directory mtime changes.

Why not embeddings? For v1, lexical matching over ~15 highly-technical docs is
plenty — the corpus is small, dense with terminology, and the user can read the
matches and judge. If lexical retrieval starts missing semantically-related
docs in practice, swapping in sentence-transformers (or an LLM rerank) is a
localized change to this module only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional


# Module-level cache. Rebuilt when any decision file's mtime exceeds last build.
_CACHE: dict = {"corpus": None, "mtime": 0.0, "df": None, "vectors": None, "N": 0}

_DECISIONS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "decisions"

# INDEX.md is not a decision record itself; the historical/ folder is not either.
_SKIP_FILES = {"INDEX.md"}

# Compact English stopword list — sufficient for technical decision docs.
# Deliberately small: terms like "decision", "record", "task" are real signal.
_STOPWORDS = set("""
a an the and or but if so as of at by for from in into on to with about against
between under over is are was were be been being have has had do does did doing
it its this that these those there here i you we they he she them us our your
their my mine yours hers ours theirs not no than too very can could should would
will may might must shall just also only own same other some any all each every
who what when where why how which whose
""".split())


# ---------------------------------------------------------------------------
# Tokenization + indexing
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, length >= 3, stopwords removed."""
    tokens = re.findall(r"\b[a-z][a-z0-9_]{2,}\b", (text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _parse_decision(path: Path) -> Optional[dict]:
    """Read one decision .md and extract metadata + full text for indexing."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^(\d+)_(.+)\.md$", path.name)
    if not m:
        return None
    number = m.group(1)
    title_match = re.search(
        r"^#\s+Decision Record\s+\d+\s+[—–-]\s+(.+)$", text, re.MULTILINE
    )
    title = title_match.group(1).strip() if title_match else (
        m.group(2).replace("_", " ").title()
    )
    date_match = re.search(r"\*\*Date\*\*:\s*([\d-]+)", text)
    date = date_match.group(1) if date_match else ""

    # Supersession detection. A record is considered superseded when its head
    # carries an explicit `**Status: SUPERSEDED**` banner (the convention
    # established by DR0014 superseding DR0009). The banner names the
    # superseding DR number(s), which we pull out so the UI can link to them.
    head = text[:2500]  # only the top of the file
    superseded = bool(re.search(r"\*\*Status:\s*SUPERSEDED", head, re.IGNORECASE))
    superseded_by: list[str] = []
    if superseded:
        for ref in re.finditer(r"DR(\d{3,5})", head):
            num = ref.group(1).lstrip("0") or ref.group(1)
            if num != (number.lstrip("0") or number) and num not in superseded_by:
                superseded_by.append(num)

    # Summary: first paragraph after "## What Was Chosen", trimmed. For superseded
    # records, prepend a SUPERSEDED marker so any leakage to the agent prompt is
    # immediately visible — see the filter in find_relevant which excludes these
    # by default.
    chosen = re.search(
        r"^##\s+What Was Chosen\s*\n+(.+?)(?=\n##|\Z)", text, re.MULTILINE | re.DOTALL
    )
    if chosen:
        para = chosen.group(1).strip().split("\n\n")[0]
        para = re.sub(r"\*\*", "", para)
        para = re.sub(r"\s+", " ", para).strip()
        summary = para[:240] + ("..." if len(para) > 240 else "")
    else:
        summary = ""
    return {
        "number":        number,
        "title":         title,
        "date":          date,
        "summary":       summary,
        "path":          "docs/decisions/" + path.name,
        "full_text":     text,
        "superseded":    superseded,
        "superseded_by": superseded_by,
    }


def _build_index_if_stale() -> None:
    """Rebuild the TF-IDF index if any file's mtime exceeds the cached one."""
    if not _DECISIONS_DIR.exists():
        _CACHE.update({"corpus": [], "df": Counter(), "vectors": [], "N": 0, "mtime": 0.0})
        return
    files = sorted(p for p in _DECISIONS_DIR.glob("*.md") if p.name not in _SKIP_FILES)
    latest_mtime = max((f.stat().st_mtime for f in files), default=0.0)
    if _CACHE["corpus"] is not None and latest_mtime <= _CACHE["mtime"]:
        return  # Fresh

    corpus = [d for d in (_parse_decision(p) for p in files) if d is not None]
    df: Counter = Counter()
    doc_tokens = []
    for d in corpus:
        tokens = _tokenize(d["full_text"])
        doc_tokens.append(tokens)
        for t in set(tokens):
            df[t] += 1
    N = len(corpus)
    vectors = []
    for tokens in doc_tokens:
        if not tokens:
            vectors.append({})
            continue
        tf = Counter(tokens)
        # +1 smoothing so a term in every doc still has nonzero idf.
        vec = {
            t: (count / len(tokens)) * math.log((N + 1) / df[t])
            for t, count in tf.items()
        }
        vectors.append(vec)
    _CACHE.update(
        {"corpus": corpus, "df": df, "vectors": vectors, "N": N, "mtime": latest_mtime}
    )


def _cosine(a: dict, b: dict) -> float:
    keys = set(a).intersection(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (norm_a * norm_b) if (norm_a and norm_b) else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_relevant(
    query_text: str,
    top_k: int = 3,
    min_score: float = 0.05,
    include_superseded: bool = False,
) -> list[dict]:
    """Return the top-K matching past decisions for the given query.

    Each dict: {number, title, date, summary, path, score, superseded,
    superseded_by}. Score is the cosine similarity (0.0–1.0) rounded to 3
    decimals. Empty list if no matches exceed min_score or if the corpus
    is empty.

    By default, decisions with a `**Status: SUPERSEDED**` banner are
    excluded — they're history, not guidance, and feeding their
    "What Was Chosen" text to agents misleads them. Pass
    include_superseded=True to surface them anyway (useful for an
    explicit "history" view).
    """
    if not query_text or not query_text.strip():
        return []
    _build_index_if_stale()
    if _CACHE["N"] == 0:
        return []
    q_tokens = _tokenize(query_text)
    if not q_tokens:
        return []
    q_tf = Counter(q_tokens)
    q_vec = {
        t: (count / len(q_tokens))
           * math.log((_CACHE["N"] + 1) / max(_CACHE["df"].get(t, 0), 1))
        for t, count in q_tf.items()
    }
    scored: list[tuple[float, dict]] = []
    for d, v in zip(_CACHE["corpus"], _CACHE["vectors"]):
        if not include_superseded and d.get("superseded"):
            continue
        s = _cosine(q_vec, v)
        if s >= min_score:
            scored.append((s, d))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "number":        d["number"],
            "title":         d["title"],
            "date":          d["date"],
            "summary":       d["summary"],
            "path":          d["path"],
            "score":         round(s, 3),
            "superseded":    d.get("superseded", False),
            "superseded_by": d.get("superseded_by", []),
        }
        for s, d in scored[:top_k]
    ]


def enrich_with_supersession(matches: list[dict]) -> list[dict]:
    """Annotate prior-art entries with *current* supersession state.

    Historical tasks have `prior_art_json` frozen from before supersession
    tracking existed. This helper re-checks each entry against the live
    corpus so the dashboard can show a "superseded by Decision X" badge on
    matches that were valid at task-creation time but have since been
    superseded. Pure read; does not mutate stored data.
    """
    if not matches:
        return matches
    _build_index_if_stale()
    by_num = {d["number"]: d for d in (_CACHE["corpus"] or [])}
    out = []
    for m in matches:
        d = by_num.get(m.get("number"))
        if d is None:
            out.append(m)
            continue
        out.append({
            **m,
            "superseded":    d.get("superseded", m.get("superseded", False)),
            "superseded_by": d.get("superseded_by", m.get("superseded_by", [])),
        })
    return out


def format_for_prompt(matches: list[dict]) -> str:
    """Markdown 'Prior Art' section to inject into agent prompts."""
    if not matches:
        return ""
    lines = [
        "# Prior Art — relevant past decisions",
        "",
        "The keeper has surfaced these past decision records as potentially relevant",
        "to this task (TF-IDF similarity over `docs/decisions/`). Consult them — "
        "don't re-litigate what's already been decided. If a prior record settles "
        "the question, cite it. If it doesn't apply, say so explicitly.",
        "",
    ]
    for m in matches:
        date_str = f", {m['date']}" if m.get("date") else ""
        # Strip leading zeros: "0011" -> "11" for readability.
        num = str(m["number"]).lstrip("0") or m["number"]
        lines.append(
            f"## Decision {num} — {m['title']}{date_str} "
            f"(relevance {m['score']:.2f})"
        )
        if m.get("summary"):
            lines.append(m["summary"])
        lines.append(f"*Full record: `{m['path']}`*")
        lines.append("")
    return "\n".join(lines)


def clear_cache() -> None:
    """Test helper — force the next call to rebuild the index from disk."""
    _CACHE.update({"corpus": None, "mtime": 0.0, "df": None, "vectors": None, "N": 0})
