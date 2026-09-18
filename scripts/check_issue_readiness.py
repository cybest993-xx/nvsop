#!/usr/bin/env python3
"""只读检查 GitHub Issue 是否满足当前 agent 派发的机械前置条件。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

REQUIRED_SECTIONS = (
    "Task type",
    "Priority",
    "Plan ID",
    "Parent map",
    "Outcome",
    "Authority and current evidence",
    "Architecture impact",
    "Architecture authority",
    "Architecture validation baseline",
    "Existing seam and reuse",
    "Scope",
    "Acceptance criteria",
    "Dependencies and blockers",
    "Evidence plan",
    "Out of scope",
    "Dispatch readiness",
)
MAP_REQUIRED_SECTIONS = (
    "Delivery map",
    "Acceptance criteria",
    "Dependencies and blockers",
    "Evidence plan",
    "Out of scope",
    "Map readiness",
)
TASK_TYPES = {"implementation", "validation", "decision", "needs-info"}
ARCHITECTURE_IMPACTS = {"none", "architecture-sensitive"}
EMPTY_RESPONSES = {"", "_No response_", "No response"}
ARCHITECTURE_BASELINE = re.compile(r"^main@([0-9a-f]{40})$")


@dataclass(frozen=True)
class IssueReadiness:
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


def sections(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        heading = re.match(r"^#{2,3}\s+(.+?)\s*$", line)
        if heading:
            if current is not None:
                result[current] = "\n".join(lines).strip()
            current = heading.group(1).strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        result[current] = "\n".join(lines).strip()
    return result


def first_value(value: str) -> str:
    for line in value.splitlines():
        item = line.strip().strip("`*_")
        if item:
            return item
    return ""


def issue_labels(issue: dict[str, Any]) -> set[str]:
    return {
        str(label.get("name"))
        for label in (issue.get("labels") or [])
        if isinstance(label, dict) and label.get("name")
    }


def architecture_paths(value: str) -> tuple[str, ...]:
    paths = [item.strip() for item in re.findall(r"`([^`]+)`", value)]
    return tuple(
        dict.fromkeys(path for path in paths if path and not path.startswith(("#", "http")))
    )


def architecture_freshness(issue: dict[str, Any]) -> str:
    body = sections(str(issue.get("body") or ""))
    if first_value(body.get("Architecture impact", "")) != "architecture-sensitive":
        return "not-applicable"

    baseline = ARCHITECTURE_BASELINE.fullmatch(
        first_value(body.get("Architecture validation baseline", ""))
    )
    paths = architecture_paths(body.get("Architecture authority", ""))
    if baseline is None:
        return "invalid-baseline"
    if not paths:
        return "missing-authority-paths"

    base_sha = baseline.group(1)
    if run("git", "cat-file", "-e", f"{base_sha}^{{commit}}").returncode != 0:
        return "unknown"
    current = run("git", "rev-parse", "origin/main")
    if current.returncode != 0:
        return "unknown"
    current_sha = current.stdout.strip()
    if run("git", "merge-base", "--is-ancestor", base_sha, current_sha).returncode != 0:
        return "unknown"

    diff = run("git", "diff", "--quiet", base_sha, current_sha, "--", *paths)
    if diff.returncode == 0:
        return "unchanged"
    if diff.returncode == 1:
        return "changed"
    return "unknown"


def evaluate_map(issue: dict[str, Any]) -> IssueReadiness:
    body = sections(str(issue.get("body") or ""))
    labels = issue_labels(issue)
    assignees = issue.get("assignees") or []
    missing = [
        name for name in MAP_REQUIRED_SECTIONS if body.get(name, "").strip() in EMPTY_RESPONSES
    ]

    blockers: list[str] = []
    state = str(issue.get("state") or "unknown").lower()
    if state != "open":
        blockers.append(f"state={state}")
    if missing:
        blockers.append("missing_sections=" + ",".join(missing))
    if assignees:
        blockers.append("map_assigned")
    if "ready-for-agent" in labels:
        blockers.append("map_has_ready_label")
    if "wayfinder:task" in labels:
        blockers.append("conflicting_wayfinder_labels")

    unique_blockers = tuple(dict.fromkeys(blockers))
    lines = (
        f"state={state}",
        "issue_kind=map",
        f"assignees={len(assignees)}",
        f"required_sections={'complete' if not missing else 'missing'}",
        f"map_structure={'valid' if not unique_blockers else 'invalid'}",
        "dispatchable=no",
        f"automated_readiness={'map-valid' if not unique_blockers else 'blocked'}",
        f"blockers={','.join(unique_blockers) if unique_blockers else 'none'}",
        "semantic_quality=manual-review-required",
    )
    return IssueReadiness(not unique_blockers, lines)


def evaluate(
    issue: dict[str, Any],
    native_blockers: list[dict[str, Any]] | None,
    freshness: str | None = None,
) -> IssueReadiness:
    labels = issue_labels(issue)
    if "wayfinder:map" in labels:
        return evaluate_map(issue)

    body = sections(str(issue.get("body") or ""))
    assignees = issue.get("assignees") or []
    task_type = first_value(body.get("Task type", ""))
    architecture_impact = first_value(body.get("Architecture impact", ""))
    missing = [name for name in REQUIRED_SECTIONS if body.get(name, "").strip() in EMPTY_RESPONSES]
    open_blockers = (
        []
        if native_blockers is None
        else [
            item for item in native_blockers if str(item.get("state") or "OPEN").upper() != "CLOSED"
        ]
    )

    blockers: list[str] = []
    state = str(issue.get("state") or "unknown").lower()
    if state != "open":
        blockers.append(f"state={state}")
    if missing:
        blockers.append("missing_sections=" + ",".join(missing))
    if task_type not in TASK_TYPES:
        blockers.append(f"task_type={task_type or 'missing'}")
    if architecture_impact not in ARCHITECTURE_IMPACTS:
        blockers.append(f"architecture_impact={architecture_impact or 'missing'}")
    if assignees:
        blockers.append("already_assigned")
    if native_blockers is None:
        blockers.append("native_blockers=unknown")
    elif open_blockers:
        blockers.append(f"native_blockers={len(open_blockers)}")

    if task_type in {"implementation", "validation"} and "wayfinder:task" not in labels:
        blockers.append("missing_label=wayfinder:task")
    if task_type == "needs-info":
        if "needs-info" not in labels:
            blockers.append("missing_label=needs-info")
        blockers.append("needs_info")
    if "needs-info" in labels:
        blockers.append("needs_info")
    if "ready-for-agent" not in labels:
        blockers.append("missing_label=ready-for-agent")
    if "needs-info" in labels and "ready-for-agent" in labels:
        blockers.append("conflicting_readiness_labels")

    architecture_state = "not-applicable"
    seam_revalidation = "not-required"
    if architecture_impact == "architecture-sensitive":
        if not architecture_paths(body.get("Architecture authority", "")):
            blockers.append("architecture_authority=missing-paths")
        if (
            ARCHITECTURE_BASELINE.fullmatch(
                first_value(body.get("Architecture validation baseline", ""))
            )
            is None
        ):
            blockers.append("architecture_baseline=invalid")
        architecture_state = freshness or "unknown"
        if architecture_state == "changed":
            blockers.append("manual-revalidation-required")
            seam_revalidation = "required"
        elif architecture_state != "unchanged":
            blockers.append(f"architecture_freshness={architecture_state}")

    unique_blockers = tuple(dict.fromkeys(blockers))
    lines = (
        f"state={state}",
        "issue_kind=task",
        f"task_type={task_type or 'missing'}",
        f"assignees={len(assignees)}",
        f"required_sections={'complete' if not missing else 'missing'}",
        f"native_blockers={'unknown' if native_blockers is None else len(open_blockers)}",
        f"architecture_impact={architecture_impact or 'missing'}",
        f"architecture_freshness={architecture_state}",
        f"existing_seam_revalidation={seam_revalidation}",
        "dispatchable=yes",
        f"automated_readiness={'ready' if not unique_blockers else 'blocked'}",
        f"blockers={','.join(unique_blockers) if unique_blockers else 'none'}",
        "semantic_quality=manual-review-required",
    )
    return IssueReadiness(not unique_blockers, lines)


def native_dependencies(repository: str, number: str) -> list[dict[str, Any]] | None:
    result = run(
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{repository}/issues/{number}/dependencies/blocked_by",
    )
    if result.returncode != 0:
        return None
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    return [item for page in pages for item in page if isinstance(item, dict)]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: check_issue_readiness.py ISSUE_NUMBER", file=sys.stderr)
        return 2
    try:
        issue = gh_json(
            "issue",
            "view",
            argv[1],
            "--json",
            "number,state,title,body,labels,assignees",
        )
        repository = gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    except (KeyError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Issue readiness query failed: {error}", file=sys.stderr)
        return 1

    result = evaluate(
        issue,
        native_dependencies(repository, argv[1]),
        architecture_freshness(issue),
    )
    print("\n".join(result.lines))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
