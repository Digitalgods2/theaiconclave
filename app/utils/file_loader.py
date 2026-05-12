"""Attachment file loading and text extraction.

Supported text types:
- .txt, .md, .json, .yaml, .yml, .py, .js, .ts, .go, .rs, .css, .html, .sql — read as UTF-8
- .pdf — extract text via pypdf

Images (.png, .jpg, .jpeg, .gif, .webp) are stored but not extracted in MVP.
The prompt builder notes their presence without inlining content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb",
    ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt",
    ".css", ".scss", ".html", ".xml", ".toml", ".ini", ".cfg",
    ".sql", ".sh", ".ps1", ".log",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_PDF_EXTENSIONS = {".pdf"}

_MAX_TEXT_BYTES = 256 * 1024   # 256 KiB per file ceiling; truncate beyond


def is_image(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_EXTENSIONS


def extract_text(path: Path) -> Optional[str]:
    """
    Return extracted text for the given file path, or None if the file type
    isn't supported as text (e.g. images). Raises FileNotFoundError if missing.
    """
    if not path.exists():
        raise FileNotFoundError(f"attachment not found: {path}")

    ext = path.suffix.lower()
    if ext in _PDF_EXTENSIONS:
        return _extract_pdf_text(path)
    if ext in _TEXT_EXTENSIONS:
        return _read_text_capped(path)
    # Unknown extension — best-effort attempt at UTF-8 read for small files
    if not is_image(path) and path.stat().st_size <= _MAX_TEXT_BYTES:
        try:
            return _read_text_capped(path)
        except UnicodeDecodeError:
            return None
    return None


def _read_text_capped(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > _MAX_TEXT_BYTES:
        truncated = raw[:_MAX_TEXT_BYTES].decode("utf-8", errors="replace")
        return truncated + f"\n\n[TRUNCATED at {_MAX_TEXT_BYTES} bytes; file is {len(raw)} bytes total]"
    return raw.decode("utf-8", errors="replace")


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF using pypdf. Truncates at _MAX_TEXT_BYTES."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks: list[str] = []
    total_chars = 0
    for i, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            page_text = f"[page {i + 1}: extraction failed: {e}]"
        chunks.append(f"--- page {i + 1} ---\n{page_text}")
        total_chars += len(page_text)
        if total_chars > _MAX_TEXT_BYTES:
            chunks.append(f"\n[TRUNCATED at {_MAX_TEXT_BYTES} characters]")
            break
    return "\n\n".join(chunks)
