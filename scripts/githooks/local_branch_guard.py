#!/usr/bin/env python3
"""Validate local branch ref transactions for the main/dev workflow."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

LOCAL_HEADS = "refs/heads/"
MAIN_REF = f"{LOCAL_HEADS}main"
DEV_REF = f"{LOCAL_HEADS}dev"
WORK_BRANCH = re.compile(r"agent/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


@dataclass(frozen=True)
class RefUpdate:
    old: str
    new: str
    ref: str


def is_zero_oid(value: str) -> bool:
    return bool(value) and set(value) == {"0"}


def parse_updates(lines: Iterable[str]) -> tuple[list[RefUpdate], list[str]]:
    updates: list[RefUpdate] = []
    errors: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            errors.append(f"malformed reference transaction line: {line}")
            continue
        updates.append(RefUpdate(old=parts[0], new=parts[1], ref=parts[2]))
    return updates, errors


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def repository_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"cannot locate the repository root: {detail}")
    return Path(result.stdout.strip())


def ref_oid(root: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def refs_with_suffix(root: Path, prefix: str, suffix: str) -> dict[str, str]:
    names = git(root, "for-each-ref", "--format=%(refname)", prefix).splitlines()
    result: dict[str, str] = {}
    for name in names:
        if name.endswith(f"/{suffix}"):
            oid = ref_oid(root, name)
            if oid is not None:
                result[name] = oid
    return result


def refs_with_prefix(root: Path, prefix: str) -> dict[str, str]:
    names = git(root, "for-each-ref", "--format=%(refname)", prefix).splitlines()
    result: dict[str, str] = {}
    for name in names:
        oid = ref_oid(root, name)
        if oid is not None:
            result[name] = oid
    return result


def commit_parents(root: Path, oid: str) -> list[str]:
    if not oid or is_zero_oid(oid):
        return []
    try:
        contents = git(root, "cat-file", "-p", oid)
    except RuntimeError:
        return []
    return [
        line.removeprefix("parent ") for line in contents.splitlines() if line.startswith("parent ")
    ]


def is_merge_from(root: Path, update: RefUpdate, source_oids: set[str]) -> bool:
    parents = commit_parents(root, update.new)
    return len(parents) == 2 and parents[0] == update.old and parents[1] in source_oids


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def branch_name(ref: str) -> str:
    return ref.removeprefix(LOCAL_HEADS)


def is_work_branch(name: str) -> bool:
    return WORK_BRANCH.fullmatch(name) is not None


def validate_main_update(root: Path, update: RefUpdate) -> list[str]:
    if is_zero_oid(update.old):
        return ["`main` is permanent and cannot be created by a branch operation."]
    if is_zero_oid(update.new):
        return ["`main` is permanent and cannot be deleted or renamed."]
    if update.new == update.old:
        return []

    remote_main_oids = set(refs_with_suffix(root, "refs/remotes", "main").values())
    if update.new in remote_main_oids:
        return []

    dev_oid = ref_oid(root, DEV_REF)
    if dev_oid is not None and (
        is_merge_from(root, update, {dev_oid})
        or (update.new == dev_oid and is_ancestor(root, update.old, update.new))
    ):
        return []

    return [
        "local `main` may change only by merging the current `dev` tip or by resetting it "
        "to a fetched remote-tracking `*/main` ref."
    ]


def validate_dev_update(root: Path, update: RefUpdate) -> list[str]:
    if is_zero_oid(update.old):
        main_oid = ref_oid(root, MAIN_REF)
        remote_dev_oids = set(refs_with_suffix(root, "refs/remotes", "dev").values())
        if update.new == main_oid or update.new in remote_dev_oids:
            return []
        return ["create local `dev` from local `main` or a fetched remote-tracking `*/dev` ref."]
    if is_zero_oid(update.new):
        return ["`dev` is the integration branch and cannot be deleted or renamed."]
    if update.new == update.old:
        return []

    source_oids = set(refs_with_prefix(root, f"{LOCAL_HEADS}agent/").values())
    main_oid = ref_oid(root, MAIN_REF)
    if main_oid is not None:
        source_oids.add(main_oid)
    if is_merge_from(root, update, source_oids) or any(
        update.new == source and is_ancestor(root, update.old, source) for source in source_oids
    ):
        return []

    return ["local `dev` may change only by merging an `agent/` branch or the current `main` tip."]


def validate_branch_name(update: RefUpdate) -> list[str]:
    name = branch_name(update.ref)
    if is_zero_oid(update.new):
        return []
    if name in {"main", "dev"} or is_work_branch(name):
        return []
    return [
        f"new local branch `{name}` is not allowed; use `dev` or "
        "`agent/<agent-id>/<task-slug>` with lowercase ASCII names."
    ]


def validate_transaction(root: Path, updates: list[RefUpdate]) -> list[str]:
    errors: list[str] = []
    for update in updates:
        if not update.ref.startswith(LOCAL_HEADS):
            continue
        name = branch_name(update.ref)
        if name == "main":
            errors.extend(validate_main_update(root, update))
        elif name == "dev":
            errors.extend(validate_dev_update(root, update))
        else:
            errors.extend(validate_branch_name(update))
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("reference-transaction requires Git's transaction phase argument", file=sys.stderr)
        return 1

    updates, parse_errors = parse_updates(sys.stdin)
    if sys.argv[1] != "prepared":
        return 0

    try:
        root = repository_root()
        errors = parse_errors + validate_transaction(root, updates)
    except RuntimeError as error:
        errors = [f"local branch guard could not verify the transaction: {error}"]

    if not errors:
        return 0
    print("reference-transaction refused this local ref update:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
