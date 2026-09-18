#!/usr/bin/env python3
"""只读汇总 PR 的机器可核实交付状态，不代替人工合并授权。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

REQUIRED_CHECK = "CI required"
DISPATCH_AUTHORITY_FILES = {
    ".github/ISSUE_TEMPLATE/task.yml",
    "docs/design/solution-and-roadmap.md",
    "docs/engineering/architecture.md",
    "docs/engineering/issues.md",
    "pyproject.toml",
}
DISPATCH_AUTHORITY_PREFIXES = (
    "docs/adr/",
    "docs/design/mechanisms/",
    "packages/contracts/src/",
)
ARCHITECTURE_AUTHORITY_FILES = {
    "docs/design/solution-and-roadmap.md",
    "docs/engineering/architecture.md",
    "pyproject.toml",
}
ARCHITECTURE_AUTHORITY_PREFIXES = (
    "docs/adr/",
    "docs/design/mechanisms/",
    "packages/contracts/src/",
)
ARCHITECTURE_COMPOSITION_FILES = {
    "apps/control-api/src/factory_sop/app.py",
    "apps/edge-runtime/src/edge_runtime/runtime.py",
}
CENTER_PUBLIC_SEAM = re.compile(r"^apps/control-api/src/factory_sop/[^/]+/api\.py$")
CENTER_STATE_OWNER = re.compile(
    r"^apps/control-api/src/factory_sop/[^/]+/(?:model|repository)\.py$"
)
CENTER_MIGRATIONS = "apps/control-api/migrations/versions/"
EDGE_STATE_OWNER_PREFIX = "apps/edge-runtime/src/edge_runtime/local_state/"


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


def requires_dispatch_impact(changed_files: list[str] | tuple[str, ...]) -> bool:
    return any(
        path in DISPATCH_AUTHORITY_FILES
        or any(path.startswith(prefix) for prefix in DISPATCH_AUTHORITY_PREFIXES)
        for path in changed_files
    )


def requires_architecture_review(changed_files: list[str] | tuple[str, ...]) -> bool:
    return any(
        path in ARCHITECTURE_AUTHORITY_FILES
        or path in ARCHITECTURE_COMPOSITION_FILES
        or path.startswith(CENTER_MIGRATIONS)
        or path.startswith(EDGE_STATE_OWNER_PREFIX)
        or CENTER_PUBLIC_SEAM.fullmatch(path) is not None
        or CENTER_STATE_OWNER.fullmatch(path) is not None
        or any(path.startswith(prefix) for prefix in ARCHITECTURE_AUTHORITY_PREFIXES)
        for path in changed_files
    )


def body_field(body: str, name: str) -> str:
    match = re.search(rf"(?im)^{re.escape(name)}:\s*`?([^`\n]+)`?\s*$", body)
    return "" if match is None else match.group(1).strip()


def dispatch_evidence(body: str) -> bool:
    if body_field(body, "Dispatch impact") != "reviewed":
        return False
    empty = {"", "n/a", "not-required"}
    return (
        body_field(body, "Affected open/ready Issues").lower() not in empty
        and body_field(body, "Actions").lower() not in empty
    )


def architecture_review_evidence(body: str) -> bool:
    if body_field(body, "Architecture review") != "reviewed":
        return False
    authority = body_field(body, "Architecture authority checked").lower()
    return authority not in {"", "n/a", "not-required"}


def evaluate(
    pr: dict[str, Any],
    protection: str,
    local_branch: str,
    local_head: str,
    changed_files: list[str] | tuple[str, ...] | None = (),
) -> Readiness:
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

    if changed_files is None:
        dispatch_impact = "unknown"
        dispatch_impact_evidence = "unknown"
        architecture_review = "unknown"
        architecture_evidence = "unknown"
        blockers.append("dispatch_impact_files=unknown")
    elif requires_dispatch_impact(changed_files):
        dispatch_impact = "required"
        dispatch_impact_evidence = (
            "present" if dispatch_evidence(str(pr.get("body") or "")) else "missing"
        )
        if dispatch_impact_evidence == "missing":
            blockers.append("dispatch_impact_evidence=missing")
    else:
        dispatch_impact = "not-required"
        dispatch_impact_evidence = "not-required"

    if changed_files is not None:
        if requires_architecture_review(changed_files):
            architecture_review = "required"
            architecture_evidence = (
                "present" if architecture_review_evidence(str(pr.get("body") or "")) else "missing"
            )
            if architecture_evidence == "missing":
                blockers.append("architecture_review_evidence=missing")
        else:
            architecture_review = "not-required"
            architecture_evidence = "not-required"

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
        f"architecture_review={architecture_review}",
        f"architecture_review_evidence={architecture_evidence}",
        f"dispatch_impact_review={dispatch_impact}",
        f"dispatch_impact_evidence={dispatch_impact_evidence}",
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
    if "404" not in message and "not found" not in message:
        return "unknown"

    rules = run("gh", "api", f"repos/{repository}/rules/branches/{branch}")
    if rules.returncode != 0:
        rules_message = rules.stderr.lower()
        if plan_capability_error in rules_message and "http 403" in rules_message:
            return "unsupported"
        if "404" in rules_message or "not found" in rules_message:
            return "absent"
        return "unknown"

    try:
        payload = json.loads(rules.stdout)
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(payload, list):
        return "unknown"

    pull_request_rule = next(
        (rule for rule in payload if isinstance(rule, dict) and rule.get("type") == "pull_request"),
        None,
    )
    required_status_rules = [
        rule
        for rule in payload
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks"
    ]
    if not isinstance(pull_request_rule, dict) or not required_status_rules:
        return "absent"

    for required_status_rule in required_status_rules:
        parameters = required_status_rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        if not parameters.get("strict_required_status_checks_policy"):
            continue
        checks = parameters.get("required_status_checks")
        if not isinstance(checks, list):
            continue
        if any(
            isinstance(check, dict) and check.get("context") == REQUIRED_CHECK for check in checks
        ):
            return "protected"
    return "absent"


def local_identity() -> tuple[str, str]:
    branch = run("git", "branch", "--show-current")
    head = run("git", "rev-parse", "HEAD")
    if branch.returncode != 0 or head.returncode != 0:
        return "", ""
    return branch.stdout.strip(), head.stdout.strip()


def pr_files(repository: str, number: str) -> list[str] | None:
    result = run(
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{repository}/pulls/{number}/files",
    )
    if result.returncode != 0:
        return None
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    files: list[str] = []
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            previous_filename = item.get("previous_filename")
            if isinstance(filename, str):
                files.append(filename)
            if isinstance(previous_filename, str):
                files.append(previous_filename)
    return list(dict.fromkeys(files))


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
            "state,baseRefName,headRefName,headRefOid,statusCheckRollup,reviewDecision,body",
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
        pr_files(repository, argv[1]),
    )
    print("\n".join(result.lines))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
