#!/usr/bin/env python3
"""只读汇总 PR 的机器可核实交付状态，不代替人工合并授权。"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

REQUIRED_CHECK = "CI required"


@dataclass(frozen=True)
class Readiness:
    ready: bool
    lines: tuple[str, ...]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def gh_json(*args: str) -> dict[str, Any]:
    result = run("gh", *args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("gh JSON response must be an object")
    return payload


def check_state(item: dict[str, Any]) -> str:
    name = item.get("name") or item.get("context")
    if name != REQUIRED_CHECK:
        return "missing"
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if conclusion == "SUCCESS":
        return "success"
    if conclusion in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return "failed"
    if status in {"IN_PROGRESS", "QUEUED", "PENDING"} or conclusion in {"PENDING", "EXPECTED"}:
        return "pending"
    return "unknown"


def evaluate(pr: dict[str, Any], protection: str, local_branch: str, local_head: str) -> Readiness:
    checks = pr.get("statusCheckRollup") or []
    ci = next((state for item in checks if (state := check_state(item)) != "missing"), "missing")
    state = str(pr.get("state") or "unknown").lower()
    base = str(pr.get("baseRefName") or "")
    head_branch = str(pr.get("headRefName") or "")
    head_sha = str(pr.get("headRefOid") or "")
    review = str(pr.get("reviewDecision") or "none").lower()

    if local_branch == head_branch:
        local = "matches" if local_head == head_sha else "mismatch"
    else:
        local = "different-branch"

    blockers = []
    if state != "open":
        blockers.append(f"state={state}")
    if base != "main":
        blockers.append(f"base={base or 'missing'}")
    if ci != "success":
        blockers.append(f"{REQUIRED_CHECK}={ci}")
    if protection not in {"protected", "unsupported"}:
        blockers.append(f"branch_protection={protection}")
    if local != "matches":
        blockers.append(f"local_candidate={local}")

    if protection == "protected":
        merge_guard = "server-protected"
    elif protection == "unsupported":
        merge_guard = "manual-ci-confirmation-required"
    else:
        merge_guard = "unverified"

    lines = (
        f"state={state}",
        f"base={base or 'missing'}",
        f"head={head_branch}@{head_sha or 'missing'}",
        f"ci_required={ci}",
        f"branch_protection={protection}",
        f"merge_guard={merge_guard}",
        f"local_candidate={local}",
        f"github_review_decision={review}",
        "independent_review=manual-confirmation-required",
        f"automated_readiness={'ready' if not blockers else 'blocked'}",
        f"blockers={','.join(blockers) if blockers else 'none'}",
    )
    return Readiness(not blockers, lines)


def protection_state(repository: str, branch: str) -> str:
    result = run("gh", "api", f"repos/{repository}/branches/{branch}/protection")
    if result.returncode == 0:
        return "protected"
    message = result.stderr.lower()
    plan_capability_error = (
        "upgrade to github pro or make this repository public to enable this feature"
    )
    if plan_capability_error in message and "http 403" in message:
        return "unsupported"
    if "404" in message or "not found" in message:
        return "absent"
    return "unknown"


def local_identity() -> tuple[str, str]:
    branch = run("git", "branch", "--show-current")
    head = run("git", "rev-parse", "HEAD")
    if branch.returncode != 0 or head.returncode != 0:
        return "", ""
    return branch.stdout.strip(), head.stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: check_pr_readiness.py PR_NUMBER", file=sys.stderr)
        return 2
    try:
        pr = gh_json(
            "pr",
            "view",
            argv[1],
            "--json",
            "state,baseRefName,headRefName,headRefOid,statusCheckRollup,reviewDecision",
        )
        repository = gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    except (KeyError, RuntimeError, json.JSONDecodeError) as error:
        print(f"PR readiness query failed: {error}", file=sys.stderr)
        return 1

    local_branch, local_head = local_identity()
    result = evaluate(
        pr,
        protection_state(repository, str(pr.get("baseRefName") or "main")),
        local_branch,
        local_head,
    )
    print("\n".join(result.lines))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
