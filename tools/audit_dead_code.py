"""Dead-code and inert-config audit for the AI Conclave Switchboard.

Run it directly (`python tools/audit_dead_code.py`) or let
`tests/test_dead_code_audit.py` run it as part of the suite.

WHY THIS EXISTS
---------------
A manual sweep found a dead 121-line module, an uncalled id helper, two
config fields that no code reads, and 11 unused imports. Every one of those
had been invisible for months. This makes the sweep repeatable so the next
one is a command, not an archaeology session.

WHY IT USES AST AND NOT GREP
----------------------------
The first hand-rolled version of this check stripped import statements with a
line regex and then text-searched the remainder. That silently missed names on
the *continuation lines* of a multi-line `from x import (\n a,\n b,\n)` block —
`AgentRole` and `ResolutionStatus` in codex_adapter.py were both reported clean
while being genuinely unused. Usage detection here resolves real `ast.Name` /
`ast.Attribute` nodes instead, which cannot make that mistake.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not fail on dynamic reachability. Static analysis cannot see through
`getattr(obj, name)`, string->callable tables, or `importlib`. Rather than
guess, the audit reports those constructs separately as CAVEAT findings: they
mark the files where a "unused" verdict is not trustworthy and a human has to
look. Today the repo has exactly one such site (orchestrator's
`_call_adapter_method`), and it dispatches only hardcoded literals.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_ROOTS = ("app", "tests", "tools", "clients")
APP_ROOT = "app"

# Names that are legitimately defined without any in-repo reference.
ALLOWLIST: set[str] = {
    # `from __future__ import annotations` is a compiler directive; it never
    # appears as a runtime name.
    "annotations",
}

# Inert config fields that have been REPORTED to the maintainer but not yet
# ruled on. They are real findings, not false positives — listing them here
# keeps the audit actionable on new drift instead of failing on a known,
# already-surfaced backlog. Removing a config field changes the documented
# config surface, so each needs an explicit decision before deletion.
PENDING_REVIEW: set[str] = {
    # Config.defaults is NOT dead scaffolding — it is an unimplemented feature,
    # and deleting it would erase the intent. Limits has no field defaults, the
    # API applies no fallback, and dashboard.js hardcodes its own values that
    # CONTRADICT the documented ones (5/360/1200 vs 50/180/600). Fixing it means
    # having the API apply these defaults when a client omits limits, which is a
    # behavior change, not a cleanup. Left declared until that call is made.
    "defaults",
}

# Modules that exist to be imported by something outside this repo, or that
# are entry points rather than libraries.
ALLOWLIST_MODULES: set[str] = {
    "app/main.py",          # uvicorn entry point, imported by name on the CLI
    "tools/audit_dead_code.py",
}


def python_files() -> Iterator[Path]:
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def used_names(tree: ast.Module) -> set[str]:
    """Every identifier the module actually *uses*, excluding import bindings.

    Collects `ast.Name` loads plus the attribute half of `ast.Attribute`, so
    both `Foo(...)` and `mod.Foo` register as uses. Import statements are
    skipped so that importing a name does not count as using it.
    """
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A bare string can name a symbol for dynamic lookup; count it as a
            # weak use so string-dispatched code is not reported as dead.
            used.add(node.value)
    return used


def imported_bindings(tree: ast.Module) -> dict[str, int]:
    bindings: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = node.lineno
    return bindings


def is_decorated(node: ast.AST) -> bool:
    """Decorated defs are referenced by the decorator, not by name.

    FastAPI route handlers are the whole reason this exists: `@router.get(...)`
    registers the function, so nothing ever names it again.
    """
    return bool(getattr(node, "decorator_list", []))


def toplevel_defs(tree: ast.Module) -> Iterator[tuple[str, int]]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("__") and not is_decorated(node):
                yield node.name, node.lineno


def dynamic_sites(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Constructs that make a static 'unused' verdict untrustworthy."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id
            if fn in ("eval", "exec"):
                yield node.lineno, f"{fn}()"
            elif fn in ("getattr", "setattr", "hasattr") and len(node.args) >= 2:
                if not isinstance(node.args[1], ast.Constant):
                    yield node.lineno, f"{fn}() with a computed attribute name"
            elif fn in ("globals", "locals", "vars"):
                yield node.lineno, f"{fn}() lookup"
        elif isinstance(node, ast.Attribute) and node.attr in (
            "import_module", "spec_from_file_location",
        ):
            yield node.lineno, f"importlib.{node.attr}"


def config_model_fields(tree: ast.Module) -> Iterator[tuple[str, str, int]]:
    """Pydantic model fields, as (model, field, lineno)."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            isinstance(b, ast.Name) and b.id.endswith("Model")
            or isinstance(b, ast.Name) and b.id == "BaseModel"
            for b in node.bases
        ):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                yield node.name, stmt.target.id, stmt.lineno


def main() -> int:
    trees: dict[Path, ast.Module] = {}
    for path in python_files():
        tree = parse(path)
        if tree is not None:
            trees[path] = tree

    all_used: set[str] = set()
    for tree in trees.values():
        all_used |= used_names(tree)

    findings: list[str] = []
    caveats: list[str] = []

    # --- 1. top-level defs in app/ that nothing references -----------------
    for path, tree in sorted(trees.items()):
        if not rel(path).startswith(APP_ROOT + "/"):
            continue
        for name, lineno in toplevel_defs(tree):
            if name not in all_used and name not in ALLOWLIST:
                findings.append(f"unreferenced def   {rel(path)}:{lineno}  {name}")

    # --- 2. modules in app/ that nothing imports ---------------------------
    module_refs: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    module_refs.update(node.module.split("."))
                # `from app.api import git` binds the submodule as an alias
                # name; without this the module reads as an orphan.
                for alias in node.names:
                    module_refs.update(alias.name.split("."))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_refs.update(alias.name.split("."))
    for path in sorted(trees):
        r = rel(path)
        if not r.startswith(APP_ROOT + "/") or path.name == "__init__.py":
            continue
        if r in ALLOWLIST_MODULES:
            continue
        if path.stem not in module_refs:
            findings.append(f"orphan module      {r}  ({len(trees[path].body)} top-level stmts)")

    # --- 3. unused imports -------------------------------------------------
    for path, tree in sorted(trees.items()):
        used = used_names(tree)
        for name, lineno in sorted(imported_bindings(tree).items(), key=lambda kv: kv[1]):
            if name in ALLOWLIST:
                continue
            if name not in used:
                findings.append(f"unused import      {rel(path)}:{lineno}  {name}")

    # --- 4. config fields no code reads ------------------------------------
    config_path = REPO_ROOT / "app" / "config.py"
    if config_path in trees:
        consumers: set[str] = set()
        for path, tree in trees.items():
            if path == config_path:
                continue
            consumers |= used_names(tree)
        for model, field, lineno in config_model_fields(trees[config_path]):
            if field in PENDING_REVIEW:
                continue
            if field not in consumers and field not in ALLOWLIST:
                findings.append(
                    f"inert config field app/config.py:{lineno}  {model}.{field}"
                )

    # --- 5. dynamic-dispatch caveats (reported, never fatal) ---------------
    for path, tree in sorted(trees.items()):
        for lineno, what in dynamic_sites(tree):
            caveats.append(f"{rel(path)}:{lineno}  {what}")

    print(f"Scanned {len(trees)} Python files under {', '.join(SEARCH_ROOTS)}.\n")

    if caveats:
        print("CAVEATS - static results are not authoritative in these files:")
        for c in caveats:
            print(f"  {c}")
        print()

    if findings:
        print(f"FINDINGS ({len(findings)}):")
        for f in findings:
            print(f"  {f}")
        return 1

    print("No dead code found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
