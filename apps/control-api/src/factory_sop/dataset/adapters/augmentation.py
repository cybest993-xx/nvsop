"""NVIDIA 增强 HTTP 与只读共享卷；不实现增强算法或上游取消。"""

from __future__ import annotations

import hashlib
import http.client
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from factory_sop.dataset.adapters.annotation_volume import _safe_name
from factory_sop.dataset.media import MediaProbe
from factory_sop.dataset.model import AugmentationCommand
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usage import safe_media_key


class AugmentationUnavailableError(Exception):
    """请求结果或数据卷不可确认，不证明上游未执行。"""


class HttpAugmentationBackend:
    """只调用原 POST/GET；不重试、不跟随重定向、不猜测响应身份。"""

    def __init__(self, url: str, timeout_seconds: int) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("增强基座必须是无凭据、查询或片段的 HTTP(S) URL")
        self._url = parsed
        self._timeout = timeout_seconds
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.target = f"{parsed.scheme}://{parsed.hostname.lower()}:{port}{parsed.path.rstrip('/')}"

    def start(self, command: AugmentationCommand) -> str:
        parameters = urlencode(
            {
                "label_data_id": command.label_data_id,
                "two_operator_mode": str(command.input_snapshot["mode"] == "two_operator").lower(),
                "frame_drop_iterations": command.frame_drop_iterations,
            }
        )
        result = self._request("POST", f"/api/v1/augment?{parameters}")
        identity = result.get("dataset_id")
        if not isinstance(identity, str) or not identity:
            raise AugmentationUnavailableError("响应未返回增强身份")
        _safe_name(identity)
        return identity

    def status(self, identity: str) -> Mapping[str, Any]:
        return self._request("GET", f"/api/v1/augmentation_status/{quote(identity, safe='')}")

    def _request(self, method: str, path: str) -> Mapping[str, Any]:
        connection_type = (
            http.client.HTTPSConnection
            if self._url.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            self._url.hostname or "", self._url.port, timeout=self._timeout
        )
        try:
            connection.request(
                method, self._url.path.rstrip("/") + path, headers={"Content-Length": "0"}
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise AugmentationUnavailableError(f"增强基座 HTTP {response.status}")
            document = json.loads(response.read())
            if not isinstance(document, dict):
                raise AugmentationUnavailableError("增强基座响应不是对象")
            return document
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise AugmentationUnavailableError("增强基座响应不可确认") from error
        finally:
            connection.close()


class AugmentationVolume:
    """只从基座响应身份读取真实文件，复制到中心现有存储的独占代次。"""

    def __init__(self, root: Path, media_probe: MediaProbe) -> None:
        self._root = root.resolve()
        self._probe = media_probe

    def directory(self, identity: str) -> Path:
        path = self._root / _safe_name(identity)
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self._root:
            raise ValueError("增强共享卷身份不存在或越界")
        return path

    def verify_input(self, command: AugmentationCommand) -> list[dict[str, Any]]:
        directory = self.directory(command.label_data_id)
        snapshot = command.input_snapshot
        actions = directory / "actions.json"
        if actions.is_symlink():
            raise ValueError("基座动作清单不是普通文件")
        document = _json_file(actions)
        if not isinstance(document, dict) or document.get("actions") != snapshot["actions"]:
            raise ValueError("基座动作清单与冻结输入不一致")
        video_id = _safe_name(str(snapshot["upstream_video_id"]))
        sources = list(directory.glob(f"{video_id}_*.mp4"))
        if len(sources) != 1 or sources[0].is_symlink():
            raise ValueError("成功标注视频映射不唯一")
        source = sources[0]
        if (
            source.stat().st_size != snapshot["derived_video_size"]
            or _digest(source) != snapshot["derived_video_sha256"]
        ):
            raise ValueError("成功标注源媒体身份变化")
        # 原基座遍历 label 下所有含视频的子目录；不能夹带未授权的其他来源。
        video_folders = [
            p
            for p in directory.iterdir()
            if p.is_dir() and any(child.suffix.lower() == ".mp4" for child in p.iterdir())
        ]
        expected_clips = {_safe_name(clip["filename"]) for clip in snapshot["clips"]}
        if (
            video_folders != [directory / source.stem]
            or {p.name for p in (directory / source.stem).glob("*.mp4")} != expected_clips
        ):
            raise ValueError("基座将消费冻结范围以外的媒体")
        for clip in snapshot["clips"]:
            path = directory / source.stem / _safe_name(clip["filename"])
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise ValueError("成功标注的实际切片不可读")
        return self.inventory(command.label_data_id)

    def inventory(self, identity: str) -> list[dict[str, Any]]:
        directory = self.directory(identity)
        files: list[dict[str, Any]] = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise ValueError("共享卷不能含符号链接或越界文件")
            if path.is_dir():
                continue
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("共享卷文件为空或不是普通文件")
            files.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "sha256": _digest(path),
                    "size": path.stat().st_size,
                }
            )
        if not files:
            raise ValueError("基座没有真实输出")
        return files

    def prepare_result(self, command: AugmentationCommand) -> list[dict[str, Any]]:
        assert command.base_augmentation_id is not None
        directory = self.directory(command.base_augmentation_id)
        files = self.inventory(command.base_augmentation_id)
        json_count = video_count = 0
        for entry in files:
            path = directory / entry["path"]
            if path.suffix.lower() == ".json":
                document = _json_file(path)
                if not isinstance(document, list) or not document:
                    raise ValueError("增强产物 JSON 为空或不是记录集合")
                known_paths = {entry["path"] for entry in files}
                for record in document:
                    reference = record.get("video") if isinstance(record, dict) else None
                    if not isinstance(reference, str) or not safe_media_key(reference):
                        raise ValueError("增强记录没有受控的关联媒体引用")
                    media = path.parent / reference
                    if (
                        not media.resolve().is_relative_to(directory)
                        or media.relative_to(directory).as_posix() not in known_paths
                        or media.suffix.lower() != ".mp4"
                    ):
                        raise ValueError("增强记录引用的关联媒体不存在")
                json_count += 1
            if path.suffix.lower() == ".mp4":
                self._probe.probe(str(path))
                video_count += 1
        if not json_count or not video_count:
            raise ValueError("增强产物缺少记录或关联媒体")
        # 数据库已冻结的候选键在复制失败/发布权丢失后仍由下一 worker 接续，
        # 不为同一未登记结果每次生成一批无人可追溯的新对象。
        stored = command.result_manifest.get("files")
        if stored is not None:
            expected = [{key: entry[key] for key in ("path", "sha256", "size")} for entry in stored]
            if files != expected:
                raise ValueError("待登记增强产物与已冻结候选清单不一致")
            return [dict(entry) for entry in stored]
        generation = uuid4().hex
        return [
            {
                **entry,
                "object_key": f"augmentation-results/{command.id}/{generation}/{entry['path']}",
            }
            for entry in files
        ]

    def copy_result(
        self, command: AugmentationCommand, files: list[dict[str, Any]], storage: ObjectStorage
    ) -> None:
        assert command.base_augmentation_id is not None
        directory = self.directory(command.base_augmentation_id)
        for entry in files:
            path = directory / entry["path"]
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as source, storage.writing(object_key=entry["object_key"]) as sink:
                while chunk := source.read(1024 * 1024):
                    sink.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("增强产物在复制期间变化，拒绝登记")
        if self.inventory(command.base_augmentation_id) != [
            {key: value for key, value in entry.items() if key != "object_key"} for entry in files
        ]:
            raise ValueError("增强输出集合在登记期间变化")


def _json_file(path: Path) -> object:
    try:
        document: object = json.loads(path.read_bytes())
        return document
    except (OSError, ValueError) as error:
        raise AugmentationUnavailableError("共享卷 JSON 不可读取") from error


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
