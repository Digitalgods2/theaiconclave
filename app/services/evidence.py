"""Immutable, provider-neutral web evidence snapshots.

Remote content is treated as untrusted data. URL validation blocks local/private
targets, redirects are revalidated, response sizes are capped, and the exact
text shown to agents is hashed and persisted for later citation audits.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from pypdf import PdfReader

from app.config import Config, EvidenceConfig
from app.services.public_http import PublicAddressTransport
from app.database import connect, now_iso
from app.utils.ids import evidence_id


class EvidenceError(ValueError):
    pass


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[dict[str, str]]: ...


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored += 1
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        value = " ".join(data.split())
        if value:
            self.parts.append(value)
            if self._in_title:
                self.title_parts.append(value)


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise EvidenceError(f"could not resolve evidence host {host!r}") from e
    addresses = sorted({str(record[4][0]).split("%", 1)[0] for record in records})
    if not addresses:
        raise EvidenceError(f"evidence host {host!r} resolved to no addresses")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as e:
            raise EvidenceError(f"invalid resolved address for {host!r}") from e
        if not parsed.is_global:
            raise EvidenceError(f"evidence URL resolves to a non-public address: {address}")
    return addresses


async def validate_public_url(url: str, config: EvidenceConfig) -> str:
    parsed = urlparse(url.strip())
    allowed = {"https"} if config.require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed:
        raise EvidenceError(f"evidence URL must use {'HTTPS' if config.require_https else 'HTTP or HTTPS'}")
    if not parsed.hostname or parsed.username or parsed.password:
        raise EvidenceError("evidence URL must have a public host and no embedded credentials")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    await asyncio.to_thread(_public_addresses, parsed.hostname, port)
    return parsed.geturl()


def _extract_text(data: bytes, content_type: str) -> tuple[str, str | None]:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages), None
        except Exception as e:  # noqa: BLE001
            raise EvidenceError(f"could not extract PDF evidence: {e}") from e
    decoded = data.decode("utf-8", errors="replace")
    if mime in {"text/html", "application/xhtml+xml"}:
        parser = _TextExtractor()
        parser.feed(decoded)
        return "\n".join(parser.parts), " ".join(parser.title_parts) or None
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return decoded, None
    raise EvidenceError(f"unsupported evidence content type: {mime or '(missing)'}")


_INSTRUCTION_PATTERN = re.compile(
    r"\b(ignore (all|any|the|previous)|system prompt|developer message|follow these instructions)\b",
    re.IGNORECASE,
)


async def fetch_url(url: str, config: EvidenceConfig) -> dict[str, Any]:
    current = url
    async with httpx.AsyncClient(
        timeout=config.timeout_seconds,
        headers={"User-Agent": "AI-Conclave-Evidence/1.0", "Accept": "text/html,text/plain,application/pdf,application/json"},
        follow_redirects=False, trust_env=False,
        transport=PublicAddressTransport(config.require_https),
    ) as client:
        for _hop in range(4):
            current = await validate_public_url(current, config)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise EvidenceError("evidence redirect omitted Location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                declared = int(response.headers.get("content-length") or 0)
                if declared > config.max_document_bytes:
                    raise EvidenceError("evidence response exceeds max_document_bytes")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > config.max_document_bytes:
                        raise EvidenceError("evidence response exceeds max_document_bytes")
                    chunks.append(chunk)
                data = b"".join(chunks)
                text, title = await asyncio.to_thread(_extract_text, data, response.headers.get("content-type", ""))
                text = text.strip()
                if not text:
                    raise EvidenceError("evidence source contained no extractable text")
                truncated = len(text) > config.max_document_chars
                text = text[:config.max_document_chars]
                injection_hits = len(_INSTRUCTION_PATTERN.findall(text))
                parsed = urlparse(current)
                quality = {
                    "score": round(min(1.0, 0.35 + (0.25 if title else 0) + min(len(text) / 20_000, 0.3)
                                       + (0.1 if parsed.scheme == "https" else 0)), 2),
                    "https": parsed.scheme == "https",
                    "has_title": bool(title),
                    "content_chars": len(text),
                    "truncated": truncated,
                    "prompt_injection_signals": injection_hits,
                }
                return {
                    "url": current, "title": title, "publisher": parsed.hostname,
                    "content_text": text, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "quality": quality,
                    "metadata": {"content_type": response.headers.get("content-type"), "bytes": total},
                }
    raise EvidenceError("evidence URL exceeded the redirect limit")


def save_snapshot(document: dict[str, Any], *, task_id: str | None, project_id: str | None) -> dict[str, Any]:
    eid = evidence_id()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO evidence_snapshots
               (id, task_id, project_id, url, title, publisher, retrieved_at,
                content_sha256, content_text, quality_json, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, task_id, project_id, document["url"], document.get("title"),
             document.get("publisher"), now, document["content_sha256"],
             document["content_text"], json.dumps(document["quality"], sort_keys=True),
             json.dumps(document.get("metadata", {}), sort_keys=True), now),
        )
    return {"id": eid, "retrieved_at": now, **document}


async def capture_urls(
    urls: list[str], config: EvidenceConfig, *, task_id: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    if not config.enabled:
        raise EvidenceError("evidence acquisition is disabled")
    unique = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
    if len(unique) > config.max_urls_per_request:
        raise EvidenceError(f"at most {config.max_urls_per_request} URLs may be fetched at once")
    documents = await asyncio.gather(*(fetch_url(url, config) for url in unique))
    return [save_snapshot(doc, task_id=task_id, project_id=project_id) for doc in documents]


def list_snapshots(*, task_id: str | None = None, project_id: str | None = None,
                   ids: list[str] | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if ids is not None:
        if not ids:
            return []
        clauses.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_snapshots" + where + " ORDER BY created_at, id", params
        ).fetchall()
    return [{
        "id": row["id"], "url": row["url"], "title": row["title"],
        "publisher": row["publisher"], "retrieved_at": row["retrieved_at"],
        "content_sha256": row["content_sha256"], "content_text": row["content_text"],
        "quality": json.loads(row["quality_json"]), "metadata": json.loads(row["metadata_json"]),
        "task_id": row["task_id"], "project_id": row["project_id"],
    } for row in rows]


class ConfiguredJsonSearchProvider:
    """Generic search API expecting ``{"results": [{"url", "title", "snippet"}]}``."""

    def __init__(self, config: Config):
        self.config = config

    async def search(self, query: str, limit: int) -> list[dict[str, str]]:
        cfg = self.config.evidence
        if not cfg.enabled:
            raise EvidenceError("evidence acquisition is disabled")
        if not cfg.search_endpoint:
            raise EvidenceError("no evidence.search_endpoint is configured")
        await validate_public_url(cfg.search_endpoint, cfg)
        headers: dict[str, str] = {"Accept": "application/json"}
        if cfg.search_api_key_env:
            key = os.environ.get(cfg.search_api_key_env)
            if not key:
                raise EvidenceError(f"missing search key environment variable {cfg.search_api_key_env}")
            headers[cfg.search_api_key_header] = f"Bearer {key}" if cfg.search_api_key_header.lower() == "authorization" else key
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds, trust_env=False,
                                     transport=PublicAddressTransport(cfg.require_https)) as client:
            async with client.stream("GET", cfg.search_endpoint, params={"q": query, "limit": limit},
                                     headers=headers) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > cfg.max_document_bytes:
                        raise EvidenceError("search response exceeds max_document_bytes")
        payload = json.loads(data)
        raw = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise EvidenceError("search endpoint must return a JSON object with a results list")
        out = []
        for item in raw[:limit]:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                out.append({k: str(item.get(k) or "") for k in ("url", "title", "snippet")})
        return out


__all__ = [
    "ConfiguredJsonSearchProvider", "EvidenceError", "SearchProvider", "capture_urls",
    "fetch_url", "list_snapshots", "save_snapshot", "validate_public_url",
]
