#!/usr/bin/env python3
"""Fail a change that exceeds harness §5's size budget, using only the standard library.

Usage: check_change_size.py BASE HEAD

Counts the implementation lines a change adds — authored production source and repository
automation, not tests, generated clients, docs, lockfiles or the vendored base — and compares
them with the budget the harness sets: 800 for a change, 500 for one that touches judgment
logic. The budget is per landed stage, so a pull request that trips it is asked to split, not
to argue: the reviewer is the one who has to hold the change in their head.
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
    total = sum(counted.values())
    touches_tight = [
        path
        for path in counted
        if any(path.parts[: len(root.parts)] == root.parts for root in TIGHT_BUDGET_PATHS)
    ]
    budget = TIGHT_LINE_BUDGET if touches_tight else CHANGE_LINE_BUDGET
    if total < budget:
        return []
    reason = (
        f"it touches judgment logic ({', '.join(str(p) for p in touches_tight)})"
        if touches_tight
        else "harness §5"
    )
    largest = sorted(counted.items(), key=lambda item: item[1], reverse=True)[:5]
    return [
        f"change adds {total} implementation lines; the budget is {budget} because {reason}. "
        "Split it into stages that each stand on their own and land the smallest first. "
        "Largest files: " + ", ".join(f"{path} (+{lines})" for path, lines in largest)
    ]


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
