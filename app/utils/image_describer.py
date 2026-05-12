"""Generate text descriptions of image attachments via Codex's vision capability.

We use Codex (which has a first-class `-i, --image` flag) as the transcriber.
The description is cached on disk next to the image so it's generated only
once per image, no matter how many tasks reference it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_DESCRIBE_PROMPT = (
    "Describe this image in thorough, neutral detail so a reader who cannot see "
    "the image can reason about its content effectively. Include:\n"
    "1. Any text content reproduced verbatim (every word visible in the image).\n"
    "2. Overall layout and composition.\n"
    "3. Visual elements: people, objects, colors, styles, charts, diagrams.\n"
    "4. Any numbers, prices, dates, identifiers, or other concrete data.\n"
    "5. The apparent purpose or domain (e.g. invoice, screenshot, photograph, diagram, UI mockup).\n\n"
    "Return only the description. No preamble like 'Here is the description'. "
    "No conclusion or commentary. Plain prose, no markdown fences."
)

_DESCRIPTION_FILENAME = "description.txt"


def cached_description(image_path: Path) -> Optional[str]:
    """Return the cached description for an image, or None if it doesn't exist."""
    cache = image_path.parent / _DESCRIPTION_FILENAME
    if cache.exists():
        try:
            return cache.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
    return None


async def describe_image(image_path: Path, timeout: int = 180) -> str:
    """
    Generate (or return cached) text description for an image using Codex.
    Caches to `<image_dir>/description.txt`.
    Raises RuntimeError if Codex is unavailable or fails.
    """
    cached = cached_description(image_path)
    if cached is not None:
        return cached

    cmd_path = shutil.which("codex")
    if cmd_path is None:
        raise RuntimeError("codex CLI not on PATH; cannot describe image")

    args = [
        cmd_path, "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s", "read-only",
        "-i", str(image_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=_DESCRIBE_PROMPT.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(f"codex image description timed out after {timeout}s")

    if proc.returncode != 0:
        raise RuntimeError(
            f"codex exited {proc.returncode}: "
            + stderr_bytes.decode("utf-8", errors="replace")[-500:]
        )

    description = _extract_agent_message(stdout_bytes.decode("utf-8", errors="replace"))
    description = description.strip()

    cache = image_path.parent / _DESCRIPTION_FILENAME
    cache.write_text(description, encoding="utf-8")
    logger.info("image description cached for %s (%d chars)", image_path.name, len(description))
    return description


def _extract_agent_message(stdout: str) -> str:
    """Pull the last agent_message item from Codex's JSONL event stream."""
    last: Optional[str] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "item.completed":
            item = evt.get("item", {})
            if item.get("type") == "agent_message":
                t = item.get("text")
                if isinstance(t, str):
                    last = t
    if last is None:
        raise RuntimeError("codex output contained no agent_message event")
    return last
