"""Deterministic evidence excerpts shared by prompts and audit records."""
from __future__ import annotations

import hashlib


def evidence_views(snapshots: list[dict]) -> list[dict]:
    remaining = 60_000
    views = []
    for snapshot in snapshots:
        raw = str(snapshot.get("content_text") or "")
        content = raw[:15_000].replace("</untrusted-evidence>", "&lt;/untrusted-evidence&gt;")[:remaining]
        remaining -= len(content)
        views.append({
            "evidence_id": snapshot["id"],
            "content_sha256": snapshot.get("content_sha256"),
            "presented_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "presented_chars": len(content), "stored_chars": len(raw),
            "omitted": not bool(content), "truncated": content != raw,
            "presented_text": content,
        })
    return views


def evidence_manifest(snapshots: list[dict]) -> list[dict]:
    return [{key: value for key, value in view.items() if key != "presented_text"}
            for view in evidence_views(snapshots)]
