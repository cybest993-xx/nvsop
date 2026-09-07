#!/usr/bin/env python3
"""Fail a change that exceeds harness §5's size budget, using only the standard library.

Usage: check_change_size.py BASE HEAD

Counts the implementation lines a change adds — authored production source and repository
automation, not tests, generated clients, docs, lockfiles or the vendored base — and holds
each module the change touches to its own review budget: 800 added implementation lines, 500
when the module is judgment logic. This is a pull-request size budget, not a cap on the total
size of a product module. The per-module adaptation lets one vertical change cross several
independent product seams without charging a reviewer for their unrelated totals. A module's
`adapters/` subpackage is budgeted separately because harness §3 keeps adapters outside the
behavior they adapt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHANGE_LINE_BUDGET = 800
TIGHT_LINE_BUDGET = 500
# Harness §5 names judgment, boundary-solving and retention logic; boundary solving lives
# inside the judgment package. Add retention's package here when it lands.
TIGHT_BUDGET_PATHS = (Path("apps/edge-runtime/src/edge_runtime/judgment"),)
IMPLEMENTATION_ROOTS = (Path("apps"), Path("packages"), Path("scripts"))
GENERATED_OUTPUTS = (Path("apps/control-web/src/api/generated"),)

# Harness §3 fixes what a module is; this table says where each application's modules live,
# so the per-module budget stays decidable by path. The first directory under a root is the
# module; a file directly under a root belongs to the root itself; a web slice under
# src/modules/ is a module of its own. Adding a source root starts here.
MODULE_ROOTS: tuple[tuple[str, Path], ...] = (
    ("factory_sop", Path("apps/control-api/src/factory_sop")),
    ("control-web", Path("apps/control-web/src")),
    ("edge_runtime", Path("apps/edge-runtime/src/edge_runtime")),
    ("contracts", Path("packages/contracts/src/nvsop_contracts")),
    ("scripts", Path("scripts")),
)


def is_implementation(path: Path) -> bool:
    """Authored production source or repository automation; never generated output or tests."""
    if "tests" in path.parts:
        return False
    if any(path.parts[: len(root.parts)] == root.parts for root in GENERATED_OUTPUTS):
        return False
    if path.parts[:1] == ("scripts",):
        return True
    return any(path.parts[: len(root.parts)] == root.parts for root in IMPLEMENTATION_ROOTS) and (
        "src" in path.parts
    )


def module_of(path: Path) -> str:
    """The module a counted file belongs to: harness §3's seams, made decidable by path."""
    for name, root in MODULE_ROOTS:
        if path.parts[: len(root.parts)] != root.parts:
            continue
        rest = path.parts[len(root.parts) :]
        if len(rest) <= 1:
            return name
        if rest[0] == "modules" and len(rest) > 2:
            return f"modules/{rest[1]}"
        if len(rest) > 2 and rest[1] == "adapters":
            return f"{rest[0]}/adapters"
        return rest[0]
    # A counted file outside every declared root must not hide its growth: everything
    # undeclared accumulates in one bucket until its root is declared here.
    return "unassigned"


def parse_numstat(text: str) -> dict[Path, int]:
    """Lines added per file from `git diff --numstat`; binary files report `-` and count 0."""
    added: dict[Path, int] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        additions, _, name = line.split("\t", 2)
        added[Path(name)] = 0 if additions == "-" else int(additions)
    return added


def budget_violations(added: dict[Path, int]) -> list[str]:
    counted = {path: lines for path, lines in added.items() if is_implementation(path)}
    by_module: dict[str, dict[Path, int]] = {}
    for path, lines in counted.items():
        by_module.setdefault(module_of(path), {})[path] = lines
    errors: list[str] = []
    for module, files in sorted(by_module.items()):
        total = sum(files.values())
        touches_tight = [
            path
            for path in files
            if any(path.parts[: len(root.parts)] == root.parts for root in TIGHT_BUDGET_PATHS)
        ]
        budget = TIGHT_LINE_BUDGET if touches_tight else CHANGE_LINE_BUDGET
        if total < budget:
            continue
        reason = (
            f"it touches judgment logic ({', '.join(str(p) for p in touches_tight)})"
            if touches_tight
            else "harness §5"
        )
        largest = sorted(files.items(), key=lambda item: item[1], reverse=True)[:5]
        listing = ", ".join(f"{path} (+{lines})" for path, lines in largest)
        errors.append(
            f"module {module} adds {total} implementation lines; the budget is {budget} "
            f"because {reason}. Identify the smallest coherent, independently verifiable "
            f"stage from the actual diff, dependencies and affected call sites. Do not create "
            f"a product module solely to move lines into another bucket; if no safe stage exists, "
            f"record why the change needs a reviewed exception. Largest files: {listing}"
        )
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_change_size.py BASE HEAD", file=sys.stderr)
        return 2
    base, head = argv[1], argv[2]
    result = subprocess.run(
        ["git", "diff", "--numstat", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    errors = budget_violations(parse_numstat(result.stdout))
    if errors:
        print("Change size budget failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Change size budget passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
