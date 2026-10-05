#!/usr/bin/env python3
"""安全清理一个已合并或被明确取代的本地任务工作树和分支。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from bind_task_session import (
    BINDING_RELATIVE_PATH,
    BindingError,
    canonical_worktree,
    load_binding,
)

FULL_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class CleanupTaskError(RuntimeError):
    """表示清理前检查失败，或清理命令未完成。"""


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str | None


@dataclass(frozen=True)
class CleanupTarget:
    task_ref: str
    task_tip: str
    worktree: Worktree | None
    local_config: bool


def command_name(arguments: Sequence[str]) -> str:
    return " ".join(arguments)


def run_raw(arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(arguments),
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CleanupTaskError(f"cannot run {command_name(arguments)}: {exc}") from exc


def output_text(value: bytes) -> str:
    return value.decode(sys.getfilesystemencoding(), errors="surrogateescape")


def command_error(
    arguments: Sequence[str], result: subprocess.CompletedProcess[bytes]
) -> CleanupTaskError:
    detail = output_text(result.stderr).strip() or output_text(result.stdout).strip()
    suffix = f": {detail}" if detail else ""
    return CleanupTaskError(f"{command_name(arguments)} failed ({result.returncode}){suffix}")


def run_checked(arguments: Sequence[str]) -> bytes:
    result = run_raw(arguments)
    if result.returncode != 0:
        raise command_error(arguments, result)
    return result.stdout


def git(*arguments: str) -> bytes:
    return run_checked(["git", *arguments])


def parse_object_id(value: object, name: str) -> str:
    if not isinstance(value, str) or FULL_OBJECT_ID.fullmatch(value) is None:
        raise CleanupTaskError(f"{name} must be a full Git object ID")
    return value


def validate_branch(branch: str) -> None:
    parts = branch.split("/")
    if len(parts) != 3 or parts[0] != "agent" or any(not part for part in parts[1:]):
        raise CleanupTaskError("--branch must name one local agent/<owner>/<task> branch")
    result = run_raw(["git", "check-ref-format", f"refs/heads/{branch}"])
    if result.returncode != 0:
        raise command_error(["git", "check-ref-format", f"refs/heads/{branch}"], result)


def local_tip(branch: str) -> str:
    ref = f"refs/heads/{branch}"
    run_checked(["git", "show-ref", "--verify", "--quiet", ref])
    symbolic = run_raw(["git", "symbolic-ref", "--quiet", ref])
    if symbolic.returncode == 0:
        raise CleanupTaskError(f"local branch ref is symbolic: {ref}")
    if symbolic.returncode != 1:
        raise command_error(["git", "symbolic-ref", "--quiet", ref], symbolic)
    tip = output_text(
        run_checked(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"]).rstrip()
    ).strip()
    return parse_object_id(tip, "local branch tip")


def parse_worktrees(payload: bytes) -> list[Worktree]:
    records: list[list[bytes]] = []
    current: list[bytes] = []
    for field in payload.split(b"\0"):
        if field:
            current.append(field)
        elif current:
            records.append(current)
            current = []
    if current:
        records.append(current)
    if not records:
        raise CleanupTaskError("git worktree list returned no worktree records")

    worktrees: list[Worktree] = []
    for fields in records:
        paths = [field[len(b"worktree ") :] for field in fields if field.startswith(b"worktree ")]
        branches = [field[len(b"branch ") :] for field in fields if field.startswith(b"branch ")]
        if len(paths) != 1 or len(branches) > 1 or not paths[0]:
            raise CleanupTaskError("git worktree list returned malformed porcelain data")
        branch = output_text(branches[0]) if branches else None
        worktrees.append(Worktree(output_text(paths[0]), branch))
    return worktrees


def read_worktrees() -> list[Worktree]:
    return parse_worktrees(git("worktree", "list", "--porcelain", "-z"))


def canonical_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def current_worktree() -> str:
    root = output_text(git("rev-parse", "--show-toplevel").rstrip()).strip()
    if not root:
        raise CleanupTaskError("current worktree path is empty")
    return canonical_path(root)


def check_ignored_binding(worktree_path: str, branch: str) -> None:
    """只允许唯一一个与本任务精确匹配的忽略状态：会话绑定文件。"""
    root = canonical_worktree(worktree_path)
    nvsop = Path(root) / BINDING_RELATIVE_PATH.parent
    if not nvsop.exists() and not nvsop.is_symlink():
        return
    if nvsop.is_symlink() or not nvsop.is_dir():
        raise CleanupTaskError(".nvsop must be a real directory")
    children = sorted(nvsop.iterdir(), key=lambda item: item.name)
    if not children:
        raise CleanupTaskError(
            "task worktree has an unknown ignored .nvsop root without a session binding"
        )
    if [child.name for child in children] != [BINDING_RELATIVE_PATH.name]:
        raise CleanupTaskError("task worktree has unexpected ignored state under .nvsop")
    binding_file = children[0]
    if binding_file.is_symlink() or not binding_file.is_file():
        raise CleanupTaskError("session binding must be a regular file")
    try:
        binding = load_binding(binding_file)
    except BindingError as exc:
        raise CleanupTaskError(f"invalid session binding: {exc}") from exc
    if binding["branch"] != branch:
        raise CleanupTaskError("session binding branch does not match the task branch")
    if binding["worktree"] != root:
        raise CleanupTaskError("session binding worktree does not match the task worktree")


def check_task_worktree(
    worktree: Worktree,
    task_ref: str,
    task_tip: str,
    branch: str,
    current_path: str,
    primary_path: str,
) -> None:
    path = canonical_path(worktree.path)
    if path == primary_path:
        raise CleanupTaskError("refusing to remove the primary worktree")
    if path == current_path:
        raise CleanupTaskError("refusing to remove the current worktree")

    checked_ref = output_text(
        git("-C", worktree.path, "symbolic-ref", "--quiet", "HEAD").rstrip()
    ).strip()
    if checked_ref != task_ref:
        raise CleanupTaskError("task worktree is no longer attached to the requested branch")
    checked_tip = output_text(
        git("-C", worktree.path, "rev-parse", "--verify", "HEAD").rstrip()
    ).strip()
    if checked_tip != task_tip:
        raise CleanupTaskError("task worktree tip changed during verification")
    status = git(
        "-C",
        worktree.path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    )
    for line in output_text(status).splitlines():
        if not line:
            continue
        if line[:2] != "!!" or line[3:] != f"{BINDING_RELATIVE_PATH.parent}/":
            raise CleanupTaskError("task worktree is not clean, including ignored files")
    check_ignored_binding(worktree.path, branch)


def release_binding(worktree_path: str) -> None:
    """只在已验证的清理中释放绑定；先删文件，再删空的 .nvsop 目录。"""
    nvsop = Path(canonical_worktree(worktree_path)) / BINDING_RELATIVE_PATH.parent
    if nvsop.is_symlink() or not nvsop.is_dir():
        return
    binding_file = nvsop / BINDING_RELATIVE_PATH.name
    if binding_file.is_file() and not binding_file.is_symlink():
        binding_file.unlink()
    if not any(nvsop.iterdir()):
        nvsop.rmdir()


def has_local_branch_config(branch: str) -> bool:
    names = git("config", "--local", "--null", "--name-only", "--list")
    prefix = f"branch.{branch}."
    for raw_name in names.split(b"\0"):
        if not raw_name:
            continue
        name = output_text(raw_name)
        suffix = name[len(prefix) :] if name.startswith(prefix) else ""
        if suffix and "." not in suffix:
            return True
    return False


def pull_request(pr: int) -> dict[str, object]:
    arguments = [
        "gh",
        "pr",
        "view",
        str(pr),
        "--json",
        "state,headRefName,headRefOid,baseRefName,mergeCommit",
    ]
    raw = run_checked(arguments)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupTaskError("gh returned invalid pull-request JSON") from exc
    if not isinstance(payload, dict):
        raise CleanupTaskError("gh pull-request response must be an object")
    return payload


def verify_pull_request(payload: dict[str, object], branch: str, candidate: str) -> str:
    if payload.get("state") != "MERGED":
        raise CleanupTaskError("pull request is not merged")
    if payload.get("baseRefName") != "main":
        raise CleanupTaskError("pull request base is not main")
    if payload.get("headRefName") != branch:
        raise CleanupTaskError("pull request head branch does not match --branch")
    if payload.get("headRefOid") != candidate:
        raise CleanupTaskError("pull request head does not match --candidate")
    merge_commit = payload.get("mergeCommit")
    if not isinstance(merge_commit, dict):
        raise CleanupTaskError("pull request has no recorded merge commit")
    return parse_object_id(merge_commit.get("oid"), "pull-request merge commit")


def verify_closed_pull_request(payload: dict[str, object], branch: str, candidate: str) -> None:
    if payload.get("state") != "CLOSED":
        raise CleanupTaskError("pull request is not closed without merge")
    if payload.get("baseRefName") != "main":
        raise CleanupTaskError("pull request base is not main")
    if payload.get("headRefName") != branch:
        raise CleanupTaskError("pull request head branch does not match --branch")
    if payload.get("headRefOid") != candidate:
        raise CleanupTaskError("pull request head does not match --candidate")
    if payload.get("mergeCommit") is not None:
        raise CleanupTaskError("closed pull request unexpectedly records a merge commit")


def verify_local_target(branch: str) -> CleanupTarget:
    validate_branch(branch)
    current_path = current_worktree()
    worktrees = read_worktrees()
    primary_path = canonical_path(worktrees[0].path)
    task_tip = local_tip(branch)

    task_ref = f"refs/heads/{branch}"
    task_worktrees = [worktree for worktree in worktrees if worktree.branch == task_ref]
    if len(task_worktrees) > 1:
        raise CleanupTaskError("requested branch is registered in multiple worktrees")
    task_worktree = task_worktrees[0] if task_worktrees else None
    if task_worktree is not None:
        check_task_worktree(task_worktree, task_ref, task_tip, branch, current_path, primary_path)

    return CleanupTarget(task_ref, task_tip, task_worktree, has_local_branch_config(branch))


def verify_merged_candidate(
    pr: int,
    candidate: str,
    target: CleanupTarget,
    accepted_main: str,
) -> None:
    if target.task_tip == candidate:
        return

    git("fetch", "--no-tags", "origin", f"pull/{pr}/head")
    fetched_head = parse_object_id(
        output_text(git("rev-parse", "--verify", "FETCH_HEAD^{commit}").rstrip()).strip(),
        "fetched pull-request head",
    )
    if fetched_head != candidate:
        raise CleanupTaskError("fetched pull-request head does not match --candidate")

    parents = output_text(git("show", "-s", "--format=%P", candidate).rstrip()).strip().split()
    if len(parents) != 2:
        raise CleanupTaskError("refreshed landing head must have exactly two parents")
    if parents[0] != target.task_tip:
        raise CleanupTaskError(
            "refreshed landing head first parent does not match the local task tip"
        )
    arguments = ["git", "merge-base", "--is-ancestor", parents[1], accepted_main]
    retained = run_raw(arguments)
    if retained.returncode == 1:
        raise CleanupTaskError(
            "refreshed landing head second parent is not retained by origin/main"
        )
    if retained.returncode != 0:
        raise command_error(arguments, retained)


def sync_primary_main(accepted_main: str) -> None:
    primary = read_worktrees()[0]
    primary_path = canonical_path(primary.path)
    checked_ref = output_text(
        git("-C", primary_path, "symbolic-ref", "--quiet", "HEAD").rstrip()
    ).strip()
    if checked_ref != "refs/heads/main":
        raise CleanupTaskError("primary worktree is not on main")

    status = git("-C", primary_path, "status", "--porcelain=v1", "--untracked-files=all")
    if status.strip():
        raise CleanupTaskError("primary main worktree is not clean")

    git("-C", primary_path, "merge", "--ff-only", "--no-overwrite-ignore", accepted_main)
    head = output_text(git("-C", primary_path, "rev-parse", "--verify", "HEAD").rstrip()).strip()
    if head != accepted_main:
        raise CleanupTaskError("primary main did not reach the accepted origin/main")


def remove_local_target(target: CleanupTarget) -> None:
    if target.worktree is not None:
        release_binding(target.worktree.path)
        git("worktree", "remove", "--", target.worktree.path)
    git("update-ref", "--no-deref", "-d", target.task_ref, target.task_tip)
    if target.local_config:
        branch = target.task_ref.removeprefix("refs/heads/")
        git("config", "--local", "--remove-section", f"branch.{branch}")


def cleanup_task(pr: int, branch: str, candidate: str) -> None:
    candidate = parse_object_id(candidate, "--candidate")
    target = verify_local_target(branch)
    merge_commit = verify_pull_request(pull_request(pr), branch, candidate)
    git("fetch", "origin", "main")
    accepted_main = parse_object_id(
        output_text(git("rev-parse", "--verify", "origin/main^{commit}").rstrip()).strip(),
        "origin/main",
    )
    git("merge-base", "--is-ancestor", merge_commit, accepted_main)
    verify_merged_candidate(pr, candidate, target, accepted_main)

    sync_primary_main(accepted_main)
    remove_local_target(target)
    print(f"synced main to {accepted_main}; cleaned {branch} at {target.task_tip}")


def cleanup_abandoned_task(pr: int | None, branch: str, candidate: str, replaced_by: str) -> None:
    candidate = parse_object_id(candidate, "--candidate")
    replaced_by = parse_object_id(replaced_by, "--replaced-by")
    target = verify_local_target(branch)
    if target.task_tip != candidate:
        raise CleanupTaskError("local branch tip does not match --candidate")
    if pr is not None:
        verify_closed_pull_request(pull_request(pr), branch, candidate)

    git("fetch", "origin", "main")
    resolved = output_text(
        git("rev-parse", "--verify", f"{replaced_by}^{{commit}}").rstrip()
    ).strip()
    if resolved != replaced_by:
        raise CleanupTaskError("--replaced-by does not resolve to the exact requested commit")
    git("merge-base", "--is-ancestor", replaced_by, "origin/main")

    remove_local_target(target)
    print(f"abandoned {branch} at {target.task_tip}; replaced by {replaced_by}")


def positive_pr(value: str) -> int:
    try:
        pr = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--pr must be a positive integer") from exc
    if pr <= 0:
        raise argparse.ArgumentTypeError("--pr must be a positive integer")
    return pr


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Clean one verified merged or explicitly superseded local task."
    )
    result.add_argument("--pr", type=positive_pr)
    result.add_argument("--branch", required=True)
    result.add_argument("--candidate", required=True)
    result.add_argument("--replaced-by")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.replaced_by is None:
            if arguments.pr is None:
                raise CleanupTaskError("--pr is required for merged task cleanup")
            cleanup_task(arguments.pr, arguments.branch, arguments.candidate)
        else:
            cleanup_abandoned_task(
                arguments.pr, arguments.branch, arguments.candidate, arguments.replaced_by
            )
    except CleanupTaskError as exc:
        print(f"cleanup_task: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
