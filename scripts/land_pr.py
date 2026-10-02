#!/usr/bin/env python3
"""用一次性 CAS 操作推进一个 GitHub PR 的落地主流程。"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from check_pr_readiness import REQUIRED_CHECK, required_check_state

FULL_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
PR_FIELDS = (
    "state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,"
    "mergeStateStatus,mergeable,statusCheckRollup,reviewDecision,mergeCommit"
)


class LandingError(RuntimeError):
    """表示 PR 状态无法安全判定，或请求的落地动作未完成。"""


@dataclass(frozen=True)
class PullRequestState:
    number: int
    state: str
    draft: bool
    base_name: str
    base_oid: str
    head_name: str
    head_oid: str
    merge_state: str
    mergeable: str
    ci_required: str
    review_decision: str
    merge_commit: str | None


def command_name(arguments: Sequence[str]) -> str:
    return shlex.join(arguments)


def run_raw(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LandingError(f"cannot run {command_name(arguments)}: {exc}") from exc


def run_checked(arguments: Sequence[str]) -> str:
    result = run_raw(arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise LandingError(f"{command_name(arguments)} failed ({result.returncode}){suffix}")
    return result.stdout


def json_object(arguments: Sequence[str]) -> dict[str, Any]:
    raw = run_checked(arguments)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LandingError(f"{command_name(arguments)} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise LandingError(f"{command_name(arguments)} must return a JSON object")
    return payload


def parse_object_id(value: object, name: str) -> str:
    if not isinstance(value, str) or FULL_OBJECT_ID.fullmatch(value) is None:
        raise LandingError(f"{name} must be a full Git object ID")
    return value


def checked_required_check_state(checks: object) -> str:
    if checks is None:
        return "missing"
    if not isinstance(checks, list):
        raise LandingError("statusCheckRollup must be a list")
    if not all(isinstance(item, dict) for item in checks):
        raise LandingError("statusCheckRollup entries must be objects")
    return required_check_state(checks)


def pull_request(number: int) -> PullRequestState:
    payload = json_object(["gh", "pr", "view", str(number), "--json", PR_FIELDS])
    state = payload.get("state")
    draft = payload.get("isDraft")
    base_name = payload.get("baseRefName")
    head_name = payload.get("headRefName")
    merge_state = payload.get("mergeStateStatus")
    mergeable = payload.get("mergeable")

    if not isinstance(state, str) or not state:
        raise LandingError("pull-request state is missing")
    if not isinstance(draft, bool):
        raise LandingError("pull-request draft state is missing")
    if not isinstance(base_name, str) or not base_name:
        raise LandingError("pull-request base branch is missing")
    if not isinstance(head_name, str) or not head_name:
        raise LandingError("pull-request head branch is missing")
    if not isinstance(merge_state, str) or not merge_state:
        raise LandingError("pull-request merge state is missing")
    if not isinstance(mergeable, str) or not mergeable:
        raise LandingError("pull-request mergeability is missing")

    merge_commit: str | None = None
    raw_merge_commit = payload.get("mergeCommit")
    if raw_merge_commit is not None:
        if not isinstance(raw_merge_commit, dict):
            raise LandingError("pull-request merge commit must be an object")
        merge_commit = parse_object_id(raw_merge_commit.get("oid"), "pull-request merge commit")

    return PullRequestState(
        number=number,
        state=state.upper(),
        draft=draft,
        base_name=base_name,
        base_oid=parse_object_id(payload.get("baseRefOid"), "pull-request base"),
        head_name=head_name,
        head_oid=parse_object_id(payload.get("headRefOid"), "pull-request head"),
        merge_state=merge_state.upper(),
        mergeable=mergeable.upper(),
        ci_required=checked_required_check_state(payload.get("statusCheckRollup")),
        review_decision=str(payload.get("reviewDecision") or "none").lower(),
        merge_commit=merge_commit,
    )


def repository_name() -> str:
    payload = json_object(["gh", "repo", "view", "--json", "nameWithOwner"])
    value = payload.get("nameWithOwner")
    if not isinstance(value, str) or "/" not in value:
        raise LandingError("repository nameWithOwner is missing")
    return value


def local_candidate(pr: PullRequestState) -> tuple[str, str | None]:
    ref = f"refs/heads/{pr.head_name}"
    result = run_raw(["git", "show-ref", "--verify", "--hash", ref])
    if result.returncode == 1:
        return "absent", None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise LandingError(f"cannot read local PR branch: {detail or result.returncode}")
    local_head = parse_object_id(result.stdout.strip(), "local PR branch")
    if local_head != pr.head_oid:
        return "mismatch", local_head

    worktrees = run_checked(["git", "worktree", "list", "--porcelain"])
    paths: list[str] = []
    path: str | None = None
    for line in worktrees.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line == f"branch {ref}" and path is not None:
            paths.append(path)
    if len(paths) > 1:
        return "multiple-worktrees", local_head
    if paths:
        status = run_checked(
            ["git", "-C", paths[0], "status", "--porcelain=v1", "--untracked-files=all"]
        )
        if status:
            return "dirty", local_head
    return "matches", local_head


def next_action(pr: PullRequestState, local_state: str) -> str:
    if local_state not in {"absent", "matches"}:
        return "local-work"
    if pr.state == "MERGED":
        return "cleanup"
    if pr.state != "OPEN" or pr.base_name != "main" or pr.draft:
        return "stop"
    if pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING":
        return "resolve-conflict"
    if pr.merge_state == "BEHIND":
        return "refresh"
    if pr.review_decision == "changes_requested":
        return "address-review"
    if pr.ci_required == "failed":
        return "repair-ci"
    if pr.ci_required == "pending":
        return "wait-ci"
    if pr.ci_required != "success":
        return "blocked-ci"
    if pr.merge_state == "UNKNOWN" or pr.mergeable == "UNKNOWN":
        return "wait-mergeability"
    if pr.merge_state == "CLEAN" and pr.mergeable == "MERGEABLE":
        return "merge"
    return "blocked"


def print_status(pr: PullRequestState) -> None:
    local_state, local_head = local_candidate(pr)
    print(f"pr={pr.number}")
    print(f"state={pr.state.lower()}")
    print(f"base={pr.base_name}@{pr.base_oid}")
    print(f"head={pr.head_name}@{pr.head_oid}")
    print(f"draft={'yes' if pr.draft else 'no'}")
    print(f"merge_state={pr.merge_state.lower()}")
    print(f"mergeable={pr.mergeable.lower()}")
    print(f"ci_required={pr.ci_required}")
    print(f"github_review_decision={pr.review_decision}")
    print(f"local_candidate={local_state}")
    print(f"local_head={local_head or 'none'}")
    print(f"merge_commit={pr.merge_commit or 'none'}")
    print(f"next_action={next_action(pr, local_state)}")


def verify_expected_head(pr: PullRequestState, expected_head: str) -> str:
    expected = parse_object_id(expected_head, "--expected-head")
    if pr.head_oid != expected:
        raise LandingError(f"pull-request head changed: expected {expected}, actual {pr.head_oid}")
    return expected


def require_local_candidate(pr: PullRequestState) -> None:
    state, head = local_candidate(pr)
    if state not in {"absent", "matches"}:
        suffix = f" at {head}" if head else ""
        raise LandingError(f"local PR candidate is {state}{suffix}")


def require_open_main(pr: PullRequestState) -> None:
    if pr.state != "OPEN":
        raise LandingError(f"pull request is not open: {pr.state.lower()}")
    if pr.base_name != "main":
        raise LandingError(f"pull request base is not main: {pr.base_name}")
    if pr.draft:
        raise LandingError("draft pull request cannot enter landing")


def refresh(number: int, expected_head: str) -> None:
    pr = pull_request(number)
    require_open_main(pr)
    expected = verify_expected_head(pr, expected_head)
    require_local_candidate(pr)
    if pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING":
        raise LandingError("pull request has merge conflicts; automatic resolution is forbidden")
    if pr.merge_state != "BEHIND":
        raise LandingError(
            f"pull request does not need a base refresh: merge_state={pr.merge_state.lower()}"
        )

    repository = repository_name()
    run_checked(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{repository}/pulls/{number}/update-branch",
            "-f",
            f"expected_head_sha={expected}",
        ]
    )
    print("action=refresh-requested")
    print(f"previous_head={expected}")
    print("evidence=stale")


def merge(number: int, expected_head: str) -> None:
    pr = pull_request(number)
    require_open_main(pr)
    expected = verify_expected_head(pr, expected_head)
    require_local_candidate(pr)
    if pr.review_decision == "changes_requested":
        raise LandingError("GitHub review has changes requested")
    if pr.ci_required != "success":
        raise LandingError(f"{REQUIRED_CHECK} is not successful: {pr.ci_required}")
    if pr.merge_state != "CLEAN" or pr.mergeable != "MERGEABLE":
        raise LandingError(
            "pull request is not cleanly mergeable: "
            f"merge_state={pr.merge_state.lower()} mergeable={pr.mergeable.lower()}"
        )

    repository = repository_name()
    payload = json_object(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{repository}/pulls/{number}/merge",
            "-f",
            f"sha={expected}",
            "-f",
            "merge_method=squash",
        ]
    )
    if payload.get("merged") is not True:
        message = payload.get("message")
        suffix = f": {message}" if isinstance(message, str) and message else ""
        raise LandingError(f"GitHub did not merge the pull request{suffix}")
    merge_commit = parse_object_id(payload.get("sha"), "merge response sha")
    print("action=merged")
    print(f"head={expected}")
    print(f"merge_commit={merge_commit}")


def positive_pr(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--pr must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("--pr must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Inspect or advance one PR through the serial landing flow."
    )
    commands = result.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="Read one PR landing state.")
    status.add_argument("--pr", required=True, type=positive_pr)

    refresh_command = commands.add_parser(
        "refresh", help="Request one conflict-free base refresh with a head CAS."
    )
    refresh_command.add_argument("--pr", required=True, type=positive_pr)
    refresh_command.add_argument("--expected-head", required=True)

    merge_command = commands.add_parser(
        "merge", help="Perform one exact-head squash merge attempt."
    )
    merge_command.add_argument("--pr", required=True, type=positive_pr)
    merge_command.add_argument("--expected-head", required=True)

    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "status":
            print_status(pull_request(arguments.pr))
        elif arguments.command == "refresh":
            refresh(arguments.pr, arguments.expected_head)
        elif arguments.command == "merge":
            merge(arguments.pr, arguments.expected_head)
        else:
            raise AssertionError(f"unexpected command: {arguments.command}")
    except LandingError as exc:
        print(f"land_pr: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
