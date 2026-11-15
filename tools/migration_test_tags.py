"""Find test classes that reverse or reapply migrations without the migration_rollback tag.

Issue #158: these tests dominate run time, so day-to-day runs may skip them with
`manage.py test --exclude-tag migration_rollback`, while handoff and CI still run them. That
is only safe if every such class carries the tag. A class "migrates" when its body, or a
module-level function it names, uses MigrationExecutor, calls `.migrate(`, or calls
call_command("migrate", ...). Reads source only (no Django); run as a script to list
offenders, or through tools/tests/test_migration_test_tags.py.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

TAG = "migration_rollback"


def _migrates(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "MigrationExecutor":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "MigrationExecutor":
            return True
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr == "migrate":
                return True
            if (
                isinstance(func, ast.Name)
                and func.id == "call_command"
                and child.args
                and isinstance(child.args[0], ast.Constant)
                and child.args[0].value == "migrate"
            ):
                return True
    return False


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _tagged(cls: ast.ClassDef) -> bool:
    for decorator in cls.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "tag"
            and any(isinstance(a, ast.Constant) and a.value == TAG for a in decorator.args)
        ):
            return True
    return False


def untagged_classes(source: str) -> list[str]:
    """Names of classes in one module that migrate but lack the tag."""
    tree = ast.parse(source)
    helpers = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _migrates(node)
    }
    found = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if (_migrates(node) or _names(node) & helpers) and not _tagged(node):
            found.append(node.name)
    return found


def scan(root: Path) -> list[str]:
    """`path::Class` for every untagged migrating test class under root's test modules."""
    offenders = []
    for path in sorted(root.glob("**/tests/**/test_*.py")):
        for name in untagged_classes(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(root)}::{name}")
    return offenders


def main(argv: list[str]) -> int:
    """Exit 0 when every migrating test class is tagged, 1 when some are not, 2 on a read error."""
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "cya"
    try:
        offenders = scan(root)
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        print(f"{root}: cannot read test modules ({error})")
        return 2
    for offender in offenders:
        path, name = offender.split("::")
        print(f'{path}: class {name} reverses or reapplies migrations; add @tag("{TAG}")')
    print(f"migration test tags: {len(offenders)} untagged")
    return 1 if offenders else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
