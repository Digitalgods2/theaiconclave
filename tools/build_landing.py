"""Build/check the static landing pages from the editable landing/src tree."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def build(check: bool = False) -> int:
    root = Path(__file__).resolve().parent.parent / "landing"
    stale = []
    for source in sorted((root / "src").iterdir()):
        if source.suffix not in {".html", ".css"}:
            continue
        text = source.read_text(encoding="utf-8")
        # Source HTML uses one tag per line; deployment retains the original
        # compact HTML. Text-node whitespace and CSS are left untouched.
        if source.suffix == ".html":
            text = re.sub(r">\n(?=<)", ">", text)
        target = root / source.name
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                stale.append(source.name)
        else:
            target.write_text(text, encoding="utf-8", newline="\n")
    if stale:
        print("Landing outputs are stale: " + ", ".join(stale))
        return 1
    print("Landing outputs match source." if check else "Landing pages built.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    raise SystemExit(build(parser.parse_args().check))
