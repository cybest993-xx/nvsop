#!/usr/bin/env python3
"""Check that each migration only touches tables its own module owns.

Physical table names carry their owning module's prefix (`device_camera`,
`template_version`, `auth_session`), and a migration filename carries the same module as
its second field. Together those two conventions make migration ownership decidable
without a database — see `docs/design/solution-and-roadmap.md` §六 and §七.

Standard library only, so it runs in the same gate as `check_repo_policy.py` without
Alembic or SQLAlchemy installed.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

MIGRATION_FILENAME = re.compile(
    r"^(?P<sequence>\d{4})_(?P<module>[a-z]+)_(?P<slug>[a-z0-9_]+)\.py$"
)

# Alembic operations that modify one table, mapped to where that table is named: the
# positional index, and the keyword that carries it instead. Operations whose target is a
# referenced rather than a modified table (`create_foreign_key`'s referent) are not listed;
# a cross-module foreign key is legal and does not transfer ownership.
TABLE_ARGUMENT: dict[str, tuple[int, str]] = {
    "create_table": (0, "table_name"),
    "drop_table": (0, "table_name"),
    "rename_table": (0, "old_table_name"),
    "add_column": (0, "table_name"),
    "alter_column": (0, "table_name"),
    "drop_column": (0, "table_name"),
    "create_index": (1, "table_name"),
    "drop_index": (1, "table_name"),
    "create_unique_constraint": (1, "table_name"),
    "create_check_constraint": (1, "table_name"),
    "create_foreign_key": (1, "source_table"),
    "drop_constraint": (1, "table_name"),
    "create_primary_key": (1, "table_name"),
    "bulk_insert": (0, "table"),
}


def module_of(table: str, center_modules: frozenset[str]) -> str | None:
    prefix = table.split("_", 1)[0]
    return prefix if prefix in center_modules else None


def tables_and_raw_sql(tree: ast.Module) -> tuple[set[str], bool]:
    """Return modified tables and whether the migration runs non-read-only raw SQL.

    A data-dependent backfill may need a read-only SELECT against a table it owns. That query
    does not change ownership; raw DDL/DML remains rejected because its target cannot be checked
    statically.
    """
    tables: set[str] = set()
    uses_raw_sql = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        operation = node.func.attr
        if operation == "execute":
            query = node.args[0] if node.args else None
            if _is_read_only_select(query):
                continue
            uses_raw_sql = True
            continue
        position_keyword = TABLE_ARGUMENT.get(operation)
        if position_keyword is None:
            continue
        position, keyword = position_keyword
        argument: ast.expr | None = None
        if len(node.args) > position:
            argument = node.args[position]
        else:
            argument = next((item.value for item in node.keywords if item.arg == keyword), None)
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            tables.add(argument.value)
    return tables, uses_raw_sql


def _is_read_only_select(query: ast.expr | None) -> bool:
    """Accept only a literal SELECT passed directly or through `sa.text`."""
    if isinstance(query, ast.Constant) and isinstance(query.value, str):
        sql = query.value
    elif (
        isinstance(query, ast.Call)
        and isinstance(query.func, ast.Attribute)
        and query.func.attr == "text"
        and query.args
        and isinstance(query.args[0], ast.Constant)
        and isinstance(query.args[0].value, str)
    ):
        sql = query.args[0].value
    else:
        return False
    return bool(re.match(r"^\s*SELECT\b", sql, flags=re.IGNORECASE))


def assigned_frozenset_of_strings(tree: ast.Module, name: str) -> frozenset[str] | None:
    """Return a non-empty literal ``frozenset`` assignment, or ``None`` if it is malformed."""
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if (
            not isinstance(value, ast.Call)
            or not isinstance(value.func, ast.Name)
            or value.func.id != "frozenset"
            or len(value.args) != 1
            or not isinstance(value.args[0], ast.Set)
            or not value.args[0].elts
        ):
            return None
        members = [
            element.value
            for element in value.args[0].elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if len(members) != len(value.args[0].elts):
            return None
        return frozenset(members)
    return None


def assigned_string(tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def check_migrations(versions: Path, center_modules: frozenset[str] | None = None) -> list[str]:
    """检查迁移文件; 目录不存在时明确视为通过。"""
    if not versions.is_dir():
        return []
    if center_modules is None:
        center_modules = declared_center_modules(Path(__file__).resolve().parents[1])

    errors: list[str] = []
    by_sequence: dict[str, list[str]] = {}
    by_down_revision: dict[str, list[str]] = {}

    for path in sorted(versions.glob("*.py")):
        if path.name == "__init__.py":
            continue
        match = MIGRATION_FILENAME.match(path.name)
        if match is None:
            errors.append(
                f"{path.name} does not follow <sequence>_<module>_<slug>.py; "
                "the filename prefix is what makes migration ownership statically decidable"
            )
            continue
        module = match["module"]
        if module not in center_modules:
            errors.append(
                f"{path.name} names a module that does not exist: {module}; "
                "declare the module in the harness before it owns a table"
            )
            continue

        by_sequence.setdefault(match["sequence"], []).append(path.name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        down_revision = assigned_string(tree, "down_revision")
        if down_revision is not None:
            by_down_revision.setdefault(down_revision, []).append(path.name)

        tables, uses_raw_sql = tables_and_raw_sql(tree)
        if uses_raw_sql:
            # The one sanctioned escape: a migration may declare the tables its raw SQL
            # touches (a trigger's target, for instance — a write-time rule no declarative
            # constraint can carry). The declaration is checked against the filename's module
            # by the same ownership rule as every `op` call below, so the gate keeps its
            # invariant; what it cannot do is read the SQL itself, which is why the
            # declaration, not the string, is the thing verified.
            declared = assigned_frozenset_of_strings(tree, "RAW_SQL_TABLES")
            if declared is None:
                errors.append(
                    f"{path.name} uses op.execute; table ownership cannot be read from raw "
                    "SQL, so express the change with Alembic operations or declare the "
                    "touched tables in RAW_SQL_TABLES"
                )
            else:
                tables |= declared
        for table in sorted(tables):
            owner = module_of(table, center_modules)
            if owner is None:
                errors.append(
                    f"{path.name} migrates a table with no module prefix: {table}; "
                    "physical table names carry their owning module's prefix"
                )
            elif owner != module:
                errors.append(
                    f"{path.name} migrates a table it does not own: {table}; "
                    f"the {owner}_ prefix belongs to {owner}, not to {module}"
                )

    for sequence, names in sorted(by_sequence.items()):
        if len(names) > 1:
            errors.append(
                f"duplicate migration sequence {sequence}: {', '.join(sorted(names))}; "
                "the history is a single linear one"
            )
    for down_revision, names in sorted(by_down_revision.items()):
        if len(names) > 1:
            errors.append(
                f'two migrations share down_revision "{down_revision}": '
                f"{', '.join(sorted(names))}; the history is a single linear one"
            )

    return errors


def declared_center_modules(root: Path) -> frozenset[str]:
    """读取迁移归属检查使用的中心模块声明。"""
    with (root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    raw_modules = document["tool"]["nvsop"]["center_modules"]
    if not isinstance(raw_modules, list) or not all(
        isinstance(module, str) and module for module in raw_modules
    ):
        raise ValueError("pyproject.toml [tool.nvsop].center_modules must be non-empty strings")
    return frozenset(raw_modules)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_migrations(
        root / "apps/control-api/migrations/versions",
        declared_center_modules(root),
    )
    if errors:
        print("Migration ownership failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Migration ownership passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
