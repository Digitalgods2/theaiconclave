"""The dead-code audit runs as part of the suite.

Keeping it here rather than in CI-only config means a newly orphaned module,
an uncalled helper, an unused import, or an inert config field fails the same
`pytest` run everyone already does. See tools/audit_dead_code.py for what it
checks and — importantly — what it deliberately refuses to decide.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_dead_code():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "audit_dead_code.py")],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "tools/audit_dead_code.py reported findings:\n\n" + proc.stdout + proc.stderr
    )
