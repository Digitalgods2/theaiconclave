"""ULID generation with table-prefix prefixes.

ULID format: 26 base32 characters (Crockford alphabet).
- First 10 chars encode a 48-bit millisecond timestamp.
- Last 16 chars encode 80 bits of cryptographic randomness.

Lexicographic sort order matches creation order at millisecond granularity.
"""

from __future__ import annotations

import os
import time
from typing import Final

# Crockford's Base32 alphabet (no I, L, O, U)
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out: list[str] = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _ulid() -> str:
    ts_ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts_ms, 10) + _encode(rand, 16)


def task_id() -> str:
    return f"tsk_{_ulid()}"


def run_id() -> str:
    return f"run_{_ulid()}"


def message_id() -> str:
    return f"msg_{_ulid()}"


def result_id() -> str:
    return f"res_{_ulid()}"


def approval_id() -> str:
    return f"apr_{_ulid()}"


def log_id() -> str:
    return f"log_{_ulid()}"


def artifact_id() -> str:
    return f"art_{_ulid()}"
