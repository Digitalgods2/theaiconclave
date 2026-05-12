"""Tests for app/utils/sandbox_inline.build_sandbox_section."""

from __future__ import annotations

from pathlib import Path

from app.utils.sandbox_inline import build_sandbox_section


def _make_project(root: Path) -> None:
    (root / "main.py").write_text("print('hello from main')\n# entry point\n", encoding="utf-8")
    (root / "README.md").write_text("# My Project\nDoes things.\n", encoding="utf-8")
    sub = root / "app" / "deep"
    sub.mkdir(parents=True)
    (sub / "util.py").write_text("def helper():\n    return 42\n", encoding="utf-8")
    # A binary file (NUL bytes) — must be skipped.
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binarystuff\x00")
    # An image extension — skipped by extension.
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    # An empty file — skipped.
    (root / "empty.txt").write_text("", encoding="utf-8")


def test_build_includes_tree_and_contents(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_project(proj)
    section = build_sandbox_section(str(proj), char_budget=200_000)
    assert "## PROJECT FILES" in section
    # Tree lists the text files...
    assert "main.py" in section
    assert "README.md" in section
    assert "app/deep/util.py" in section
    # ...but not the binary / image / empty ones.
    assert "blob.bin" not in section
    assert "logo.png" not in section
    assert "empty.txt" not in section
    # Contents are inlined.
    assert "hello from main" in section
    assert "Does things." in section
    assert "def helper()" in section
    # Each file content block has its path delimiter.
    assert "--- main.py ---" in section


def test_nonexistent_path_returns_empty(tmp_path):
    assert build_sandbox_section(str(tmp_path / "nope"), 100_000) == ""


def test_empty_dir_returns_empty(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert build_sandbox_section(str(d), 100_000) == ""


def test_tiny_budget_yields_tree_only(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_project(proj)
    section = build_sandbox_section(str(proj), char_budget=300)  # smaller than any file content block
    assert "## PROJECT FILES" in section
    assert "### File tree" in section
    # Contents not inlined; the "too small" note is present.
    assert "too small to inline file contents" in section
    assert "hello from main" not in section


def test_priority_files_inlined_first_when_budget_tight(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # main.py / README.md are priority names; the deep obscure.py is not.
    (proj / "main.py").write_text("MAIN\n", encoding="utf-8")
    (proj / "README.md").write_text("README\n", encoding="utf-8")
    deep = proj / "z_deep" / "more"
    deep.mkdir(parents=True)
    # Big enough that it won't fit alongside the small priority files in a tight budget.
    (deep / "obscure.py").write_text("OBSCURE_CONTENT_MARKER\n" * 200, encoding="utf-8")
    section = build_sandbox_section(str(proj), char_budget=2_000)
    assert "MAIN" in section
    assert "README" in section
    assert "OBSCURE_CONTENT_MARKER" not in section
    assert "omitted to stay within the context budget" in section


def test_per_file_cap_skips_huge_file(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "small.py").write_text("ok\n", encoding="utf-8")
    (proj / "huge.py").write_text("x" * 250_000, encoding="utf-8")  # over _PER_FILE_CAP
    section = build_sandbox_section(str(proj), char_budget=2_000_000)
    assert "small.py" in section
    # huge.py is excluded entirely (not even in the tree, since _walk drops it).
    assert "huge.py" not in section
