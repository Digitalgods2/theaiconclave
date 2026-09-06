"""Install The AI Conclave Switchboard slash-command parity across Claude Code, Codex, and Gemini.

This script is the deploy step for the source-of-truth files under
clients/. It copies / links them into each tool's home directory so the
slash commands and skills become live.

Run from the repo root:

  python clients/install.py             # install all three
  python clients/install.py claude      # just Claude Code
  python clients/install.py codex       # just Codex
  python clients/install.py gemini      # just Gemini (also runs `gemini extensions link`)
  python clients/install.py --check     # report what's installed, change nothing
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS = REPO_ROOT / "clients"
HOME = Path.home()


CLAUDE_TARGETS = HOME / ".claude" / "commands"
CODEX_TARGET = HOME / ".codex" / "skills" / "switchboard-conclave"
GEMINI_SOURCE = CLIENTS / "gemini-extension"
HELPER_SOURCE = CLIENTS / "switchboard_conclave.py"
USER_SITE = Path(sysconfig.get_path("purelib", scheme="nt_user" if os.name == "nt" else "posix_user"))
HELPER_TARGET = USER_SITE / "switchboard_conclave.py"


def validate_helper() -> bool:
    source_ok = HELPER_SOURCE.is_file()
    installed_ok = (HELPER_TARGET.is_file() and source_ok
                    and HELPER_TARGET.read_bytes() == HELPER_SOURCE.read_bytes())
    print(f"[helper] [{'OK' if source_ok else 'MISSING'}] source: {HELPER_SOURCE}")
    print(f"[helper] [{'OK' if installed_ok else 'MISSING'}] installed: {HELPER_TARGET}")
    return source_ok and installed_ok


def _same_file(source: Path, target: Path) -> bool:
    """Return whether an installed client file exactly matches its source."""
    try:
        return source.is_file() and target.is_file() and source.read_bytes() == target.read_bytes()
    except OSError:
        return False


def install_helper() -> None:
    if not HELPER_SOURCE.is_file():
        print(f"[helper] source missing: {HELPER_SOURCE}")
        return
    USER_SITE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HELPER_SOURCE, HELPER_TARGET)
    print(f"  [helper] -> {HELPER_TARGET}")


def _copy_tree(src: Path, dst: Path, label: str) -> None:
    """Copy src → dst, replacing any existing files. Preserves dst directories not in src."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.is_dir():
            _copy_tree(entry, dst / entry.name, label)
        else:
            shutil.copy2(entry, dst / entry.name)
            print(f"  [{label}] -> {dst / entry.name}")


def install_claude() -> None:
    src = CLIENTS / "claude-code-commands"
    if not src.is_dir():
        print(f"[claude] source missing: {src}")
        return
    print(f"[claude] copying {len(list(src.glob('*.md')))} commands -> {CLAUDE_TARGETS}")
    CLAUDE_TARGETS.mkdir(parents=True, exist_ok=True)
    for md in sorted(src.glob("*.md")):
        shutil.copy2(md, CLAUDE_TARGETS / md.name)
        print(f"  [claude] -> {CLAUDE_TARGETS / md.name}")


def install_codex() -> None:
    src = CLIENTS / "codex-skill"
    if not src.is_dir():
        print(f"[codex] source missing: {src}")
        return
    print(f"[codex] copying SKILL.md -> {CODEX_TARGET}")
    CODEX_TARGET.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "SKILL.md", CODEX_TARGET / "SKILL.md")
    print(f"  [codex] -> {CODEX_TARGET / 'SKILL.md'}")


def install_gemini() -> None:
    if not GEMINI_SOURCE.is_dir():
        print(f"[gemini] source missing: {GEMINI_SOURCE}")
        return
    print(f"[gemini] linking extension from {GEMINI_SOURCE}")
    # On Windows the gemini binary is a .cmd shim; subprocess can't find it
    # without resolution. shutil.which honors PATHEXT so it finds the shim.
    gemini_bin = shutil.which("gemini")
    if gemini_bin is None:
        print("[gemini] 'gemini' not found on PATH. Install Gemini CLI first, then re-run.")
        return
    # Idempotent: if it's already linked, gemini will say so.
    # --consent skips the interactive trust prompt (we authored these files).
    result = subprocess.run(
        [gemini_bin, "extensions", "link", str(GEMINI_SOURCE), "--consent"],
        capture_output=True, text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(f"[gemini] link failed (exit {result.returncode}). If the extension is already installed, try: gemini extensions list")


def check() -> None:
    valid = validate_helper()
    print("=== claude commands ===")
    for md in sorted((CLIENTS / "claude-code-commands").glob("*.md")):
        target = CLAUDE_TARGETS / md.name
        current = _same_file(md, target)
        marker = "OK" if current else ("STALE" if target.exists() else "MISSING")
        valid = valid and current
        print(f"  [{marker}] {target}")
    print("=== codex skill ===")
    source = CLIENTS / "codex-skill" / "SKILL.md"
    target = CODEX_TARGET / "SKILL.md"
    current = _same_file(source, target)
    marker = "OK" if current else ("STALE" if target.exists() else "MISSING")
    valid = valid and current
    print(f"  [{marker}] {target}")
    print("=== gemini extensions ===")
    gemini_bin = shutil.which("gemini")
    if gemini_bin is None:
        print("  (gemini binary not on PATH)")
    else:
        result = subprocess.run([gemini_bin, "extensions", "list"], capture_output=True, text=True)
        sys.stdout.write(result.stdout)
    if not valid:
        raise SystemExit(1)


def main() -> None:
    args = sys.argv[1:]
    if not args or args == ["all"]:
        install_helper()
        install_claude()
        install_codex()
        install_gemini()
        return
    if "--check" in args:
        check()
        return
    if "claude" in args:
        install_helper()
        install_claude()
    if "codex" in args:
        install_helper()
        install_codex()
    if "gemini" in args:
        install_helper()
        install_gemini()


if __name__ == "__main__":
    main()
