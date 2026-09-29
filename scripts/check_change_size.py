#!/usr/bin/env python3
"""报告变更与源文件规模；行数仅供评审，规则见 harness §5。

Usage: check_change_size.py [--check --allow PATH --max-lines N] BASE [HEAD]

省略 HEAD 时包含工作区与未跟踪文件；指定 HEAD 时只读取该提交。
默认输出仅供评审的 advisory 报告；--check 对固定基线执行范围与规模门禁。
"""

from __future__ import annotations

import argparse
import codecs
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

CHANGE_REVIEW_TARGET = 800
DEFAULT_MAX_LINES = 800
FILE_REVIEW_TARGET = 500
FILE_EXTRACTION_PROMPT = 800
JUDGMENT_REVIEW_TARGET = 500
JUDGMENT_SOURCE = Path("apps/edge-runtime/src/edge_runtime/judgment")
GENERATED_SOURCE = Path("apps/control-web/src/api/generated")
SOURCE_SUFFIXES = {".py", ".ts", ".vue"}
PY_SUFFIXES = {".py"}
WEB_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue"}

# 审查线索只匹配本次 diff 的实际增删行，是有限的文本/语法线索：命中不表示测试确实变弱，
# 无提示也不表示安全；同一处新增不会被另一处删除抵消，每条线索独立列出。
ADDED_HINT_PATTERNS = (
    (PY_SUFFIXES, re.compile(r"#\s*noqa\b"), "suppression (noqa)"),
    (PY_SUFFIXES, re.compile(r"#\s*type:\s*ignore\b"), "suppression (type: ignore)"),
    (WEB_SUFFIXES, re.compile(r"eslint-disable\b"), "suppression (eslint-disable)"),
    (WEB_SUFFIXES, re.compile(r"@ts-(?:ignore|expect-error|nocheck)\b"), "suppression (TS ignore)"),
    (PY_SUFFIXES, re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b"), "skip/focus marker"),
    (PY_SUFFIXES, re.compile(r"pytest\.(?:skip|xfail)\s*\("), "skip/focus marker"),
    (
        WEB_SUFFIXES,
        re.compile(r"\b(?:test|it|describe)\.(?:skip|only|todo)\b"),
        "skip/focus marker",
    ),
)
REMOVED_HINT_PATTERNS = (
    (PY_SUFFIXES, re.compile(r"^\s*def\s+test_\w+"), "test definition"),
    (PY_SUFFIXES, re.compile(r"^\s*assert\b"), "assertion"),
    (PY_SUFFIXES, re.compile(r"^\s*self\.assert\w+\s*\("), "assertion"),
    (WEB_SUFFIXES, re.compile(r"^\s*(?:it|test|describe)\s*\("), "test definition"),
    (WEB_SUFFIXES, re.compile(r"^\s*expect\s*\("), "assertion"),
)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

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


def merge_change_counts(
    existing: tuple[int, int] | None, incoming: tuple[int, int] | None
) -> tuple[int, int] | None:
    """累加同一路径的多段变更；任一段为二进制则整体按未知处理。"""
    if existing is None or incoming is None:
        return None
    return (existing[0] + incoming[0], existing[1] + incoming[1])


def collect_changes(
    root: Path, base: str, revision: list[str], worktree: bool
) -> tuple[dict[Path, tuple[int, int] | None], set[Path]]:
    """读取 base 到目标的变更；重命名保留旧/新路径，未跟踪文件按新增计入。"""
    changes: dict[Path, tuple[int, int] | None] = {}
    paths: set[Path] = set()
    text = git(root, "diff", "--numstat", "--find-renames", "-z", base, *revision, "--")
    records = iter(text.split("\0"))
    for record in records:
        if not record:
            continue
        added, deleted, name = record.split("\t", 2)
        if not name:
            paths.add(Path(next(records)))
            name = next(records)
        changes[Path(name)] = None if added == "-" else (int(added), int(deleted))
        paths.add(Path(name))
    if worktree:
        for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if not name:
                continue
            content = (root / name).read_bytes()
            path = Path(name)
            counts = None if b"\0" in content[:8192] else (len(content.splitlines()), 0)
            if path in changes:
                # 同一路径可能先被暂存删除再以未跟踪新文件重建；两段都计入，删除量不能丢。
                counts = merge_change_counts(changes[path], counts)
            changes[path] = counts
            paths.add(path)
    return changes, paths


def decode_git_path(raw: str) -> Path | None:
    """解码 Git 补丁头路径：去掉分隔 TAB，处理 C 引用与 a/、b/ 前缀。"""
    raw = raw[:-1] if raw.endswith("\t") else raw
    if raw.startswith('"') and raw.endswith('"'):
        raw = codecs.escape_decode(raw[1:-1].encode("utf-8"))[0].decode("utf-8")
    if raw == "/dev/null":
        return None
    return Path(raw[2:] if raw.startswith(("a/", "b/")) else raw)


def collect_diff_lines(
    root: Path, base: str, revision: list[str], worktree: bool
) -> tuple[dict[Path, list[tuple[int, str]]], dict[Path, list[tuple[int, str]]]]:
    """读取 base 到目标的实际增删行及行号；删除文件归旧路径，未跟踪文件整体按新增处理。"""
    added: dict[Path, list[tuple[int, str]]] = defaultdict(list)
    removed: dict[Path, list[tuple[int, str]]] = defaultdict(list)
    patch = git(root, "diff", "--no-color", "--unified=0", "--find-renames", base, *revision, "--")
    old_path: Path | None = None
    new_path: Path | None = None
    old_line = new_line = 0
    in_hunk = False
    for line in patch.splitlines():
        if in_hunk and line.startswith("+"):
            added[new_path].append((new_line, line[1:]))
            new_line += 1
            continue
        if in_hunk and line.startswith("-"):
            removed[old_path].append((old_line, line[1:]))
            old_line += 1
            continue
        if in_hunk and line.startswith("\\"):
            continue
        in_hunk = False
        if line.startswith("--- "):
            old_path = decode_git_path(line[4:])
        elif line.startswith("+++ "):
            new_path = decode_git_path(line[4:])
        elif line.startswith("@@"):
            match = HUNK_RE.match(line)
            if match and (old_path is not None or new_path is not None):
                old_line, new_line = int(match.group(1)), int(match.group(2))
                in_hunk = True
    if worktree:
        for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if not name:
                continue
            path = Path(name)
            content = (root / path).read_bytes()
            if b"\0" in content[:8192]:
                continue
            for number, text in enumerate(content.decode("utf-8", "replace").splitlines(), 1):
                added[path].append((number, text))
    return added, removed


def review_hints(
    added: dict[Path, list[tuple[int, str]]], removed: dict[Path, list[tuple[int, str]]]
) -> list[str]:
    """从实际 diff 行提取有限文本线索；跳过生成物，删除测试/断言只查测试文件。"""
    hints: list[str] = []
    for path in sorted(added):
        if category_of(path) == "Generated":
            continue
        for number, text in added[path]:
            for suffixes, pattern, label in ADDED_HINT_PATTERNS:
                if path.suffix in suffixes and pattern.search(text):
                    hints.append(f"  new {label} at {path}:{number}: {text.strip()[:100]}")
                    break
    for path in sorted(removed):
        if category_of(path) != "Tests":
            continue
        for number, text in removed[path]:
            for suffixes, pattern, label in REMOVED_HINT_PATTERNS:
                if path.suffix in suffixes and pattern.search(text):
                    hints.append(f"  removed {label} at {path}:{number}: {text.strip()[:100]}")
                    break
    return hints


def print_review_hints(
    added: dict[Path, list[tuple[int, str]]], removed: dict[Path, list[tuple[int, str]]]
) -> None:
    """输出供人工语义审查的线索；不自动判失败，也不改变范围/预算退出码。"""
    hints = review_hints(added, removed)
    print("Review hints (limited text clues for human semantic review; not proof, not approval)")
    for hint in hints:
        print(hint)
    print(f"Review hints: {len(hints)} textual clue(s); no hint does not prove safety.")


def parse_allow_entry(raw: str) -> str:
    """校验允许路径：只接受仓库内字面文件或以 / 结尾的目录前缀。"""
    if not raw or raw.startswith("/"):
        raise ValueError(f"allow path must be repo-relative: {raw!r}")
    if any(char in raw for char in "*?[]{}!"):
        raise ValueError(f"allow path must not use globs: {raw!r}")
    body = raw[:-1] if raw.endswith("/") else raw
    if any(part in {"", ".", ".."} for part in body.split("/")):
        raise ValueError(f"allow path must be a literal file or dir/: {raw!r}")
    return raw


def allowed_path(path: Path, allow: list[str]) -> bool:
    """目录前缀以 / 结尾精确匹配，避免 scripts/ 误放行 scripts-old/。"""
    text = path.as_posix()
    return any(text.startswith(entry) if entry.endswith("/") else text == entry for entry in allow)


def git(root: Path, *args: str) -> str:
    """Git 失败直接传给命令入口，不能变成空报告。"""
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def run_advisory(args: argparse.Namespace) -> int:
    """原有 advisory 报告：默认用 merge-base，行数仅供评审，不作门禁。"""
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        worktree = args.head is None
        head = git(
            root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{args.head if not worktree else 'HEAD'}^{{commit}}",
        ).strip()
        base = git(root, "merge-base", args.base, head).strip()
        revision = [] if worktree else [head]
        changes, _ = collect_changes(root, base, revision, worktree)
        hint_lines = collect_diff_lines(root, base, revision, worktree)
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
        print_review_hints(*hint_lines)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"Cannot report change size: {detail}", file=sys.stderr)
        return 1
    return 0


def run_task_check(args: argparse.Namespace) -> int:
    """固定基线的范围与规模门禁；机械边界，不代表验收或审批。"""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.base):
        print(
            "task-check needs an explicit full 40-hex fixed base; "
            "origin/main, HEAD and other movable refs are not accepted",
            file=sys.stderr,
        )
        return 2
    if not args.allow:
        print("task-check needs at least one --allow path", file=sys.stderr)
        return 2
    try:
        allow = [parse_allow_entry(raw) for raw in args.allow]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    max_lines = DEFAULT_MAX_LINES if args.max_lines is None else args.max_lines
    if max_lines <= 0:
        print("--max-lines must be a positive integer", file=sys.stderr)
        return 2
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        worktree = args.head is None
        head = git(
            root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{args.head if not worktree else 'HEAD'}^{{commit}}",
        ).strip()
        base = git(
            root, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}"
        ).strip()
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if ancestor.returncode == 1:
            print("task-check base must be an ancestor of the target HEAD", file=sys.stderr)
            return 2
        ancestor.check_returncode()
        revision = [] if worktree else [head]
        changes, paths = collect_changes(root, base, revision, worktree)
        hint_lines = collect_diff_lines(root, base, revision, worktree)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"Cannot check task bounds: {detail}", file=sys.stderr)
        return 1
    budget = sum(
        counts[0] + counts[1]
        for path, counts in changes.items()
        if counts is not None and category_of(path) != "Generated"
    )
    generated = sum(
        1
        for path, counts in changes.items()
        if counts is not None and category_of(path) == "Generated"
    )
    binary = sorted(str(path) for path, counts in changes.items() if counts is None)
    out_of_scope = sorted(str(path) for path in paths if not allowed_path(path, allow))
    print("Task check (mechanical bounds only)")
    print(f"Base (fixed): {base}")
    print(f"Target: {'worktree (staged, unstaged and untracked)' if worktree else head}")
    print(f"Allowed: {', '.join(allow)}")
    print(f"Budget: {budget} / {max_lines} changed lines (added+deleted, excluding Generated)")
    if generated:
        print(f"Generated files excluded from budget: {generated}")
    if binary:
        print(
            f"Binary files ({len(binary)}) have no line count and need separate review: "
            + ", ".join(binary)
        )
    if out_of_scope:
        print(f"Out-of-scope paths ({len(out_of_scope)}): {', '.join(out_of_scope)}")
    print_review_hints(*hint_lines)
    reasons: list[str] = []
    if out_of_scope:
        reasons.append(f"out-of-scope paths: {', '.join(out_of_scope)}")
    if budget > max_lines:
        reasons.append(f"budget exceeded: {budget} > {max_lines} changed lines")
    if reasons:
        print("task_check=pause")
        for reason in reasons:
            print(f"reason: {reason}")
        return 3
    print("task_check=within-bounds")
    print("Mechanical bounds only; within-bounds is not acceptance or approval.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_change_size.py",
        description="Advisory size report; with --check, enforce fixed-base scope and budget.",
    )
    parser.add_argument("base", help="BASE revision, or the full 40-hex fixed base with --check")
    parser.add_argument("head", nargs="?", help="HEAD revision; omit to include the worktree")
    parser.add_argument("--check", action="store_true", help="enforce scope and line budget")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PATH",
        help="authorized repo-relative file or dir/ prefix; repeatable",
    )
    parser.add_argument(
        "--max-lines", type=int, metavar="N", help="added+deleted budget (default 800)"
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    if args.check:
        return run_task_check(args)
    if args.allow or args.max_lines is not None:
        print("--allow and --max-lines are only valid with --check", file=sys.stderr)
        return 2
    return run_advisory(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
