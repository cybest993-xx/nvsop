#!/usr/bin/env python3
"""报告变更与源文件规模；行数仅供评审，规则见 harness §5。

Usage: check_change_size.py BASE [HEAD]

省略 HEAD 时包含工作区与未跟踪文件；指定 HEAD 时只读取该提交。
"""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path

CHANGE_REVIEW_TARGET = 800
FILE_REVIEW_TARGET = 500
FILE_EXTRACTION_PROMPT = 800
JUDGMENT_REVIEW_TARGET = 500
JUDGMENT_SOURCE = Path("apps/edge-runtime/src/edge_runtime/judgment")
GENERATED_SOURCE = Path("apps/control-web/src/api/generated")
SOURCE_SUFFIXES = {".py", ".ts", ".vue"}

# 分组只帮助定位变更；私有文件与适配器仍归原业务模块。
MODULE_ROOTS = (
    ("factory_sop", Path("apps/control-api/src/factory_sop")),
    ("control-web", Path("apps/control-web/src")),
    ("edge_runtime", Path("apps/edge-runtime/src/edge_runtime")),
    ("contracts", Path("packages/contracts/src/nvsop_contracts")),
    ("scripts", Path("scripts")),
)


def category_of(path: Path) -> str:
    """把测试、生成物和其他评审材料单列，避免混入实现规模。"""
    if path.parts[0] == "vendor":
        return "Vendor"
    if path.is_relative_to(GENERATED_SOURCE) or path == Path("packages/contracts/openapi.json"):
        return "Generated"
    if "tests" in path.parts or path.name.endswith((".spec.ts", ".test.ts")):
        return "Tests"
    if "migrations" in path.parts:
        return "Migrations"
    if path.parts[0] == "scripts" or (
        path.parts[0] in {"apps", "packages"} and "src" in path.parts
    ):
        return "Implementation"
    return "Other"


def module_of(path: Path) -> str:
    """按现有所有者展示变更，不把目录当作预算账户。"""
    for name, root in MODULE_ROOTS:
        if not path.is_relative_to(root):
            continue
        rest = path.relative_to(root).parts
        if len(rest) <= 1:
            return name
        if rest[0] == "modules" and len(rest) > 2:
            return f"{name}/modules/{rest[1]}"
        return f"{name}/{rest[0]}"
    return "unassigned"


def parse_numstat(text: str) -> dict[Path, tuple[int, int] | None]:
    """解析 Git 的 NUL 分隔输出；重命名归新路径，二进制保留为未知行数。"""
    changes: dict[Path, tuple[int, int] | None] = {}
    records = iter(text.split("\0"))
    for record in records:
        if not record:
            continue
        added, deleted, name = record.split("\t", 2)
        if not name:
            next(records)
            name = next(records)
        changes[Path(name)] = None if added == "-" else (int(added), int(deleted))
    return changes


def git(root: Path, *args: str) -> str:
    """Git 失败直接传给命令入口，不能变成空报告。"""
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        print("usage: check_change_size.py BASE [HEAD]", file=sys.stderr)
        return 2
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        worktree = len(argv) == 2
        head = git(
            root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{argv[2] if not worktree else 'HEAD'}^{{commit}}",
        ).strip()
        base = git(root, "merge-base", argv[1], head).strip()
        revision = [] if worktree else [head]
        changes = parse_numstat(
            git(root, "diff", "--numstat", "--find-renames", "-z", base, *revision, "--")
        )
        if worktree:
            for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
                if name:
                    content = (root / name).read_bytes()
                    changes[Path(name)] = (
                        None if b"\0" in content[:8192] else (len(content.splitlines()), 0)
                    )
        implementation = {
            path: counts
            for path, counts in changes.items()
            if counts is not None and category_of(path) == "Implementation"
        }
        added = sum(counts[0] for counts in implementation.values())
        deleted = sum(counts[1] for counts in implementation.values())
        print("Change size report (advisory)")
        target = "worktree (staged, unstaged and untracked)" if worktree else head
        print(f"Range: {base} -> {target}")
        print(f"Implementation: +{added} -{deleted} ({added + deleted} changed lines)")
        for category in ("Tests", "Generated", "Migrations", "Vendor", "Other"):
            rows = [
                counts
                for path, counts in changes.items()
                if counts is not None and category_of(path) == category
            ]
            if rows:
                print(f"{category}: +{sum(row[0] for row in rows)} -{sum(row[1] for row in rows)}")
        binary_files = sum(counts is None for counts in changes.values())
        if binary_files:
            print(f"Binary files: {binary_files} (no line count)")
        by_module: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for path, counts in implementation.items():
            totals = by_module[module_of(path)]
            totals[0] += counts[0]
            totals[1] += counts[1]
        for module, (module_added, module_deleted) in sorted(by_module.items()):
            print(f"  {module}: +{module_added} -{module_deleted}")
        if added + deleted >= CHANGE_REVIEW_TARGET:
            print("Review scope and cohesion; split only independently deliverable stages.")
        judgment = sum(
            sum(counts)
            for path, counts in implementation.items()
            if path.is_relative_to(JUDGMENT_SOURCE)
        )
        if judgment >= JUDGMENT_REVIEW_TARGET:
            print(f"Judgment: {judgment} changed lines; review its invariants together.")
        present = (
            {str(path) for path in implementation if (root / path).is_file()}
            if worktree
            else set(git(root, "ls-tree", "-r", "--name-only", "-z", head).split("\0"))
        )
        for path in sorted(implementation):
            if (
                path.parts[0] in {"apps", "packages"}
                and path.suffix in SOURCE_SUFFIXES
                and str(path) in present
            ):
                content = (
                    (root / path).read_text(encoding="utf-8")
                    if worktree
                    else git(root, "show", f"{head}:{path}")
                )
                lines = len(content.splitlines())
                if lines >= FILE_REVIEW_TARGET:
                    prompt = (
                        "assess cohesive private extraction"
                        if lines >= FILE_EXTRACTION_PROMPT
                        else "review readability"
                    )
                    print(f"  {path}: {lines} physical lines; {prompt}.")
        print(
            "Size alone does not block delivery; record cohesion decisions in the review handoff."
        )
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"Cannot report change size: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
