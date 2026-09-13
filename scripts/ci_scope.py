#!/usr/bin/env python3
"""按两个提交之间的路径选择 CI 文档快线。"""

from __future__ import annotations

import subprocess
import sys

ROOT_DOCS = {"AGENTS.md", "CLAUDE.md", "CONTEXT.md", "README.md"}


def resolve_commit(reference: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: ci_scope.py BASE HEAD", file=sys.stderr)
        return 2

    try:
        head = resolve_commit(argv[2])
        try:
            base = resolve_commit(argv[1])
        except subprocess.CalledProcessError:
            print("No usable base commit; running all gates.", file=sys.stderr)
            print("docs_only=false\nforce_all=true")
            return 0
        changed = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        ).stdout
    except (subprocess.CalledProcessError, OSError) as error:
        print(f"CI scope failed: {error}", file=sys.stderr)
        return 1

    paths = [path for path in changed.split("\0") if path]
    docs_only = bool(paths) and all(
        path in ROOT_DOCS or (path.startswith("docs/") and path.endswith(".md")) for path in paths
    )
    force_all = any(
        path.startswith(".github/") or path in {"Makefile", "scripts/ci_scope.py"} for path in paths
    )
    print(f"docs_only={str(docs_only).lower()}")
    print(f"force_all={str(force_all).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
