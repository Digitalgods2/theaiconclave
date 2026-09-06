"""Cross-platform syntax check for all shipped dashboard JavaScript."""
from pathlib import Path
import subprocess


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    for path in sorted((root / "app" / "dashboard").rglob("*.js")):
        subprocess.run(["node", "--check", str(path)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
