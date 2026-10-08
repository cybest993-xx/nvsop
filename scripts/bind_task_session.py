#!/usr/bin/env python3
"""把任务 worktree 绑定到拥有它的实现会话，供未来队列/调度器做本地上下文查找。"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

BINDING_VERSION = 1
BINDING_RELATIVE_PATH = Path(".nvsop") / "session-binding.json"
BINDING_KEYS = ("version", "session_id", "branch", "worktree")
MAX_BINDING_BYTES = 4096
# 分支命名空间与仓库的 agent/<owner>/<task> 约定一致，且只允许小写 ASCII 组件。
BRANCH_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# 会话标识是不透明值，只约束字符集和长度，不绑定任何 provider/model 语义。
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class BindingError(RuntimeError):
    """表示绑定前置条件或绑定文件校验失败。"""


def command_name(arguments: Sequence[str]) -> str:
    return " ".join(arguments)


def run_raw(arguments: Sequence[str], cwd: str | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(list(arguments), cwd=cwd, capture_output=True, check=False)
    except OSError as exc:
        raise BindingError(f"cannot run {command_name(arguments)}: {exc}") from exc


def output_text(value: bytes) -> str:
    return value.decode(sys.getfilesystemencoding(), errors="surrogateescape")


def git_text(arguments: Sequence[str], cwd: str | None = None) -> str:
    command = ["git", *arguments]
    result = run_raw(command, cwd=cwd)
    if result.returncode != 0:
        detail = output_text(result.stderr).strip() or output_text(result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise BindingError(f"{command_name(command)} failed ({result.returncode}){suffix}")
    return output_text(result.stdout)


def canonical_worktree(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def current_worktree() -> str:
    root = canonical_worktree(git_text(["rev-parse", "--show-toplevel"]).strip())
    if not root:
        raise BindingError("current worktree path is empty")
    return root


def current_branch() -> str:
    result = run_raw(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
    if result.returncode != 0:
        raise BindingError("current worktree is not attached to a branch")
    branch = output_text(result.stdout).strip()
    if not branch:
        raise BindingError("current branch name is empty")
    return branch


def validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise BindingError("session id must be a non-empty opaque identifier")
    return session_id


def validate_branch(branch: object) -> str:
    if not isinstance(branch, str):
        raise BindingError("branch must be a string")
    parts = branch.split("/")
    if (
        len(parts) != 3
        or parts[0] != "agent"
        or any(BRANCH_COMPONENT.fullmatch(part) is None for part in parts[1:])
    ):
        raise BindingError("branch must name one agent/<owner>/<task> branch")
    return branch


def binding_path(worktree_root: str) -> Path:
    return Path(worktree_root) / BINDING_RELATIVE_PATH


def is_ignored(worktree_root: str) -> bool:
    result = run_raw(
        ["git", "check-ignore", "--quiet", "--", BINDING_RELATIVE_PATH.as_posix()],
        cwd=worktree_root,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = output_text(result.stderr).strip() or output_text(result.stdout).strip()
    suffix = f": {detail}" if detail else ""
    raise BindingError(f"git check-ignore failed ({result.returncode}){suffix}")


def validate_binding(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise BindingError("session binding must be a JSON object")
    if set(payload) != set(BINDING_KEYS):
        raise BindingError(
            "session binding must have exactly version, session_id, branch and worktree"
        )
    version = payload["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != BINDING_VERSION:
        raise BindingError(f"session binding version must be the integer {BINDING_VERSION}")
    session_id = validate_session_id(payload["session_id"])
    branch = validate_branch(payload["branch"])
    worktree = payload["worktree"]
    if not isinstance(worktree, str) or not worktree:
        raise BindingError("session binding worktree must be a non-empty path")
    if canonical_worktree(worktree) != worktree:
        raise BindingError("session binding worktree must be the canonical absolute task worktree")
    return {
        "version": BINDING_VERSION,
        "session_id": session_id,
        "branch": branch,
        "worktree": worktree,
    }


def load_binding(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise BindingError("session binding must not be a symlink")
    try:
        info = path.stat()
    except OSError as exc:
        raise BindingError(f"cannot read session binding: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BindingError("session binding must be a regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BindingError(f"cannot read session binding: {exc}") from exc
    if len(raw) > MAX_BINDING_BYTES:
        raise BindingError("session binding is unreasonably large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BindingError("session binding is not valid UTF-8 JSON") from exc
    return validate_binding(payload)


def write_binding(path: Path, binding: dict[str, object]) -> bool:
    """独占创建绑定文件；已存在时返回 False，绝不覆盖。"""
    payload = (json.dumps(binding, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise BindingError(f"cannot create session binding: {exc}") from exc
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def bind(session_id: str) -> tuple[str, dict[str, object]]:
    session_id = validate_session_id(session_id)
    root = current_worktree()
    branch = validate_branch(current_branch())
    if not is_ignored(root):
        raise BindingError(f"{BINDING_RELATIVE_PATH.as_posix()} must be ignored by Git")
    path = binding_path(root)
    if path.parent.is_symlink():
        raise BindingError(".nvsop must not be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    binding: dict[str, object] = {
        "version": BINDING_VERSION,
        "session_id": session_id,
        "branch": branch,
        "worktree": root,
    }
    if write_binding(path, binding):
        return "created", binding
    existing = load_binding(path)
    if existing == binding:
        return "unchanged", existing
    raise BindingError("a session binding already exists for another session, branch or worktree")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bind one task worktree to its implementation session."
    )
    result.add_argument("--session", required=True, help="resumable implementation-session id")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        state, binding = bind(arguments.session)
    except BindingError as exc:
        print(f"bind_task_session: {exc}", file=sys.stderr)
        return 1
    print(f"state={state}")
    print(f"branch={binding['branch']}")
    print(f"worktree={binding['worktree']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
