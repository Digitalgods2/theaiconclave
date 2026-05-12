"""Install Switchboard slash-command parity across Claude Code, Codex, and Gemini.

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

import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS = REPO_ROOT / "clients"
HOME = Path.home()


CLAUDE_TARGETS = HOME / ".claude" / "commands"
CODEX_TARGET = HOME / ".codex" / "skills" / "switchboard-conclave"
GEMINI_SOURCE = CLIENTS / "gemini-extension"


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
    print(f"[claude] copying 8 commands -> {CLAUDE_TARGETS}")
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
    print("=== claude commands ===")
    for md in sorted((CLIENTS / "claude-code-commands").glob("*.md")):
        target = CLAUDE_TARGETS / md.name
        marker = "OK" if target.exists() else "MISSING"
        print(f"  [{marker}] {target}")
    print("=== codex skill ===")
    target = CODEX_TARGET / "SKILL.md"
    print(f"  [{'OK' if target.exists() else 'MISSING'}] {target}")
    print("=== gemini extensions ===")
    gemini_bin = shutil.which("gemini")
    if gemini_bin is None:
        print("  (gemini binary not on PATH)")
    else:
        result = subprocess.run([gemini_bin, "extensions", "list"], capture_output=True, text=True)
        sys.stdout.write(result.stdout)


def main() -> None:
    args = sys.argv[1:]
    if not args or args == ["all"]:
        install_claude()
        install_codex()
        install_gemini()
        return
    if "--check" in args:
        check()
        return
    if "claude" in args:
        install_claude()
    if "codex" in args:
        install_codex()
    if "gemini" in args:
        install_gemini()


if __name__ == "__main__":
    main()
