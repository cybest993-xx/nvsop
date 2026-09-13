"""固定开发实例的 Git/LFS 快照实现。"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol

OPTIONAL_LFS_PATHS = frozenset(
    {
        "vendor/sop-monitoring-blueprints/agentic/ds-sop-skills/assets/DeepStream-SOP-Inference-Agentic-Workflow.png",
        (
            "vendor/sop-monitoring-blueprints/agentic/vss-sop-skills/"
            "vss-sop-build/references/diagrams/SOP Blueprint - VSS SOP building flow.png"
        ),
        (
            "vendor/sop-monitoring-blueprints/agentic/vss-sop-skills/"
            "vss-sop-build/references/diagrams/VSS SOP Blueprint Architecture.png"
        ),
        "vendor/sop-monitoring-blueprints/assets/SOP-FT-Inference-Agentic-Workflow.png",
        "vendor/sop-monitoring-blueprints/microservices/sop-inference-bp/docs/deepstream-sop-architecture.png",
        "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/ddm-training-ms/ddm/DDM-Net/config/downsample-temporal_stride.png",
        "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/evaluation-ms/ddm/DDM-Net/config/downsample-temporal_stride.png",
        "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/tutorials/SOP_Training_BP_User_Guide.pdf",
    }
)


class SnapshotPaths(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def snapshots(self) -> Path: ...

    @property
    def logs(self) -> Path: ...


RunChecked = Callable[..., subprocess.CompletedProcess[bytes]]


class SnapshotError(RuntimeError):
    """快照基础设施无法继续。"""


class SnapshotInterrupted(KeyboardInterrupt):
    """快照建立收到停止请求。"""


class MissingLfsError(SnapshotError):
    """Git-LFS 对象缺失且不能安全地建立完整快照。"""

    def __init__(
        self,
        sha: str,
        missing_lfs_paths: list[dict[str, str]],
        unapproved_lfs_paths: list[str] | None = None,
    ) -> None:
        self.sha = sha
        self.missing_lfs_paths = missing_lfs_paths
        self.unapproved_lfs_paths = unapproved_lfs_paths or []
        missing = ", ".join(f"{entry['path']} ({entry['oid']})" for entry in missing_lfs_paths)
        detail = f"main 快照 {sha} 缺少 Git-LFS 对象：{missing}"
        if self.unapproved_lfs_paths:
            detail += "; 不能启用可选资源降级，快照包含未列入白名单的 LFS 路径：" + ", ".join(
                self.unapproved_lfs_paths
            )
        super().__init__(detail)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def archive_environment(
    environ: Mapping[str, str], *, allow_missing_optional_lfs: bool = False
) -> dict[str, str]:
    """为 git archive 构造显式环境，默认禁止全局 LFS 降级。"""
    environment = dict(environ)
    environment.pop("GIT_LFS_SKIP_SMUDGE", None)
    if allow_missing_optional_lfs:
        environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    return environment


def lfs_media_directory(
    root: Path, *, run_checked: RunChecked, stop_event: Event | None = None
) -> Path | None:
    result = run_checked(
        ["git", "lfs", "env"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stop_event=stop_event,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(f"无法定位 Git-LFS 对象目录：{detail}")
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("LocalMediaDir="):
            path = Path(line.partition("=")[2])
            return path if path.is_absolute() else root / path
    return None


def lfs_files(
    root: Path,
    sha: str,
    *,
    run_checked: RunChecked,
    stop_event: Event | None = None,
) -> list[dict[str, str]]:
    result = run_checked(
        ["git", "lfs", "ls-files", "--long", sha],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stop_event=stop_event,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(f"无法检查 main 快照 {sha} 的 Git-LFS 对象：{detail}")
    media_directory = lfs_media_directory(root, run_checked=run_checked, stop_event=stop_event)
    entries: list[dict[str, str]] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3 or parts[1] not in {"*", "-"}:
            raise SnapshotError(f"无法解析 git lfs ls-files 输出：{line!r}")
        available = parts[1]
        if available == "-" and media_directory is not None:
            object_path = media_directory / parts[0][:2] / parts[0][2:4] / parts[0]
            if object_path.is_file():
                available = "*"
        entries.append({"oid": parts[0], "available": available, "path": parts[2]})
    return entries


def hydrate_lfs_files(
    snapshot: Path,
    entries: list[dict[str, str]],
    *,
    media_directory: Path | None,
) -> None:
    """用本地 LFS 对象替换归档中的 pointer，保留可用文件的真实内容。"""
    available = [entry for entry in entries if entry["available"] == "*"]
    if not available:
        return
    if media_directory is None:
        raise SnapshotError("Git-LFS 报告对象可用，但没有 LocalMediaDir，无法建立完整快照")
    root = snapshot.resolve()
    for entry in available:
        oid = entry["oid"]
        object_path = media_directory / oid[:2] / oid[2:4] / oid
        if not object_path.is_file():
            raise SnapshotError(f"Git-LFS 对象已报告可用但本地文件不存在：{entry['path']} ({oid})")
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError(f"Git-LFS 路径无效，拒绝写入快照外部：{entry['path']}")
        destination = (snapshot / relative).resolve()
        if root != destination and root not in destination.parents:
            raise SnapshotError(f"Git-LFS 路径越出快照目录：{entry['path']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(object_path, destination)


def snapshot_manifest_path(snapshot: Path) -> Path:
    return snapshot / ".nvsop-snapshot.json"


def read_snapshot_manifest(snapshot: Path) -> dict[str, object] | None:
    path = snapshot_manifest_path(snapshot)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def snapshot_manifest(
    *,
    sha: str,
    missing_lfs_paths: list[dict[str, str]],
    allow_missing_optional_lfs: bool,
    warning: str | None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "source_sha": sha,
        "complete": not missing_lfs_paths,
        "missing_lfs_paths": missing_lfs_paths,
        "allow_missing_optional_lfs": allow_missing_optional_lfs,
        "warning": warning,
        "created_at": _utc_now(),
    }


def write_snapshot_audit(item: SnapshotPaths, sha: str, manifest: dict[str, object]) -> None:
    item.logs.mkdir(parents=True, exist_ok=True)
    audit = item.logs / f"snapshot-{sha}.json"
    audit.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def archive_main(
    item: SnapshotPaths,
    sha: str,
    *,
    run_checked: RunChecked,
    allow_missing_optional_lfs: bool = False,
    stop_event: Event | None = None,
) -> Path:
    item.snapshots.mkdir(parents=True, exist_ok=True)
    item.logs.mkdir(parents=True, exist_ok=True)
    destination = item.snapshots / sha
    marker = destination / ".nvsop-source-sha"
    manifest = read_snapshot_manifest(destination)
    if (
        marker.is_file()
        and marker.read_text(encoding="ascii").strip() == sha
        and manifest is not None
        and manifest.get("source_sha") == sha
        and manifest.get("complete") is True
        and manifest.get("missing_lfs_paths") == []
    ):
        return destination

    entries = lfs_files(item.root, sha, run_checked=run_checked, stop_event=stop_event)
    missing = [entry for entry in entries if entry["available"] == "-"]
    unapproved = sorted(
        {entry["path"] for entry in missing if entry["path"] not in OPTIONAL_LFS_PATHS}
    )
    if missing and (not allow_missing_optional_lfs or unapproved):
        failure_manifest = snapshot_manifest(
            sha=sha,
            missing_lfs_paths=missing,
            allow_missing_optional_lfs=allow_missing_optional_lfs,
            warning=(
                "缺少 LFS 对象；未建立快照。"
                if not allow_missing_optional_lfs
                else "缺少 LFS 对象，且存在未列入可选白名单的路径；未建立快照。"
            ),
        )
        write_snapshot_audit(item, sha, failure_manifest)
        raise MissingLfsError(sha, missing, unapproved)

    warning = None
    if missing:
        warning = (
            "这是显式允许的可选文档资源降级；快照中的 missing_lfs_paths 保留为 Git-LFS pointer。"
        )
    environment = archive_environment(
        os.environ,
        allow_missing_optional_lfs=bool(missing and allow_missing_optional_lfs),
    )
    temporary = item.snapshots / f".{sha}.tmp-{os.getpid()}"
    remove_path(temporary)
    temporary.mkdir(parents=True)
    try:
        process = subprocess.Popen(
            ["git", "archive", "--format=tar", sha],
            cwd=item.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    except OSError as popen_error:
        remove_path(temporary)
        raise SnapshotError(f"无法执行 git archive：{popen_error}") from popen_error
    assert process.stdout is not None
    completed = False
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if stop_event is not None and stop_event.is_set():
                    raise SnapshotInterrupted("收到停止请求")
                archive.extract(member, temporary, filter="data")
        process.stdout.close()
        return_code = process.wait()
        stderr_text = (
            process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        )
        if return_code != 0:
            raise SnapshotError(f"无法建立 main 源码快照 {sha}：{stderr_text.strip()}")
        if any(entry["available"] == "*" for entry in entries):
            hydrate_lfs_files(
                temporary,
                entries,
                media_directory=lfs_media_directory(
                    item.root, run_checked=run_checked, stop_event=stop_event
                ),
            )
        manifest = snapshot_manifest(
            sha=sha,
            missing_lfs_paths=missing,
            allow_missing_optional_lfs=allow_missing_optional_lfs,
            warning=warning,
        )
        snapshot_manifest_path(temporary).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if missing:
            (temporary / ".nvsop-source-sha.partial").write_text(sha + "\n", encoding="ascii")
        else:
            (temporary / ".nvsop-source-sha").write_text(sha + "\n", encoding="ascii")
        remove_path(destination)
        temporary.replace(destination)
        write_snapshot_audit(item, sha, manifest)
        completed = True
        return destination
    except (OSError, EOFError, tarfile.TarError, ValueError) as error:
        _terminate_process(process)
        process.wait()
        detail = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        raise SnapshotError(
            f"无法建立 main 源码快照 {sha}：Git archive 流失败：{error}"
            + (f"；{detail.strip()}" if detail.strip() else "")
        ) from error
    finally:
        if process.poll() is None:
            _terminate_process(process)
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if not completed:
            remove_path(temporary)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover - 目标开发环境是 WSL2
        process.terminate()
