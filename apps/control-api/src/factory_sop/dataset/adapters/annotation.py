"""复用 NVIDIA 标注基座的 HTTP adapter；中心只做服务端工作副本传输。"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit

from factory_sop.dataset.annotation import (
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    PreparedAnnotationVideo,
)
from factory_sop.dataset.model import AnnotationMode, AnnotationSegment
from factory_sop.settings import Settings


class HttpAnnotationBackend:
    """把产品数据集映射到一个基座数据集，再复用基座上传和切片。"""

    def __init__(self, *, base_url: str, timeout_seconds: int, data_root: Path) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AnnotationBackendUnavailableError("标注基座地址必须是 HTTP(S) 主机")
        if parsed.username is not None or parsed.password is not None:
            raise AnnotationBackendUnavailableError("标注基座地址不得包含 userinfo")
        if parsed.query or parsed.fragment:
            raise AnnotationBackendUnavailableError("标注基座地址不得包含 query 或 fragment")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._timeout = timeout_seconds
        self._data_root = data_root.resolve()

    @classmethod
    def from_settings(cls, settings: Settings) -> HttpAnnotationBackend:
        """从显式部署配置创建基座 adapter；缺配置不伪造成功。"""
        if settings.annotation_backend_url is None:
            raise AnnotationBackendUnavailableError("标注基座尚未配置")
        if settings.annotation_data_root is None:
            raise AnnotationBackendUnavailableError("标注基座数据卷尚未配置")
        return cls(
            base_url=settings.annotation_backend_url,
            timeout_seconds=settings.annotation_http_timeout_seconds,
            data_root=Path(settings.annotation_data_root),
        )

    def prepare_video(
        self,
        *,
        source: BinaryIO,
        filename: str,
        actions: Sequence[str],
    ) -> PreparedAnnotationVideo:
        actions_payload = json.dumps(
            {"actions": list(actions)}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        action_result = self._post_multipart_bytes(
            path="/api/v1/actions/upload",
            filename="actions.json",
            content_type="application/json",
            content=actions_payload,
        )
        data_id = _required_string(action_result, "data_id")
        video_result = self._post_multipart_file(
            path=f"/api/v1/upload?target_data_id={quote(data_id, safe='')}",
            filename=filename,
            content_type="video/mp4",
            source=source,
        )
        return PreparedAnnotationVideo(
            data_id=data_id,
            video_id=_required_string(video_result, "file_id"),
        )

    def discard_prepared_video(self, *, data_id: str) -> None:
        """删除取消执行留下的未引用基座数据集。"""
        deadline = monotonic() + self._timeout
        connection = self._connection()
        try:
            connection.connect()
            self._set_remaining_timeout(connection, deadline)
            connection.request(
                "DELETE",
                f"{self._base_path}/api/v1/videos/clear-dataset/{quote(data_id, safe='')}",
                headers={"Connection": "close"},
            )
            self._set_remaining_timeout(connection, deadline)
            response_socket = self._connected_socket(connection)
            response = connection.getresponse()
            body = self._read_response_body(
                response_socket=response_socket,
                response=response,
                deadline=deadline,
            )
            if not 200 <= response.status < 300:
                raise AnnotationBackendExecutionError(
                    f"标注基座清理工作副本失败（HTTP {response.status}）"
                )
            result = _json_object(body)
            if result.get("files_deleted") != 1 and self._prepared_dataset_exists(data_id):
                raise AnnotationBackendExecutionError("标注基座未确认工作副本文件删除")
        except AnnotationBackendExecutionError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise AnnotationBackendUnavailableError("无法清理标注基座工作副本") from error
        finally:
            connection.close()

    def _prepared_dataset_exists(self, data_id: str) -> bool:
        """用部署的只读数据卷确认 HTTP 清理结果，区分已删除与 vendor 假成功。"""
        if (
            not data_id
            or data_id in {".", ".."}
            or "/" in data_id
            or "\\" in data_id
            or os.path.isabs(data_id)
        ):
            raise AnnotationBackendExecutionError("标注基座工作副本身份非法")
        path = self._data_root / data_id
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self._data_root)
            root_stat = os.lstat(self._data_root)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise AnnotationBackendUnavailableError("标注基座数据卷根路径不可用")
            try:
                path_stat = os.lstat(path)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
                raise AnnotationBackendExecutionError("标注基座工作副本路径不是普通目录")
            return True
        except (AnnotationBackendExecutionError, AnnotationBackendUnavailableError):
            raise
        except (OSError, ValueError) as error:
            raise AnnotationBackendUnavailableError("无法确认标注基座工作副本清理结果") from error

    def download_video(self, *, video_id: str, destination: BinaryIO) -> None:
        """读取基座转码副本到服务端临时文件，不向浏览器中继字节。"""
        deadline = monotonic() + self._timeout
        connection = self._connection()
        try:
            connection.connect()
            self._set_remaining_timeout(connection, deadline)
            connection.putrequest(
                "GET",
                f"{self._base_path}/api/v1/videos/{quote(video_id, safe='')}/download",
            )
            connection.putheader("Connection", "close")
            connection.endheaders()
            self._set_remaining_timeout(connection, deadline)
            response_socket = self._connected_socket(connection)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                self._read_response_body(
                    response_socket=response_socket,
                    response=response,
                    deadline=deadline,
                )
                raise AnnotationBackendExecutionError(
                    f"标注基座读取视频失败（HTTP {response.status}）"
                )
            self._read_response_body(
                response_socket=response_socket,
                response=response,
                deadline=deadline,
                destination=destination,
            )
        except AnnotationBackendExecutionError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise AnnotationBackendUnavailableError("无法读取标注基座派生视频") from error
        finally:
            connection.close()

    def split_video(
        self,
        *,
        video_id: str,
        segments: Sequence[AnnotationSegment],
        mode: AnnotationMode,
    ) -> Sequence[Mapping[str, Any]]:
        result = self._post_json(
            path=f"/api/v1/videos/{quote(video_id, safe='')}/split",
            payload={
                "timestamps": [segment.as_wire() for segment in segments],
                "twoOperatorMode": mode is AnnotationMode.TWO_OPERATOR,
            },
        )
        clips = result.get("clips")
        if not isinstance(clips, list) or not all(isinstance(item, dict) for item in clips):
            raise AnnotationBackendExecutionError("标注基座未返回 clip 集合")
        return [item for item in clips if isinstance(item, dict)]

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _connected_socket(self, connection: http.client.HTTPConnection) -> socket.socket:
        sock = connection.sock
        if sock is None:
            raise AnnotationBackendUnavailableError("标注基座连接未建立")
        return sock

    def _set_socket_remaining_timeout(self, sock: socket.socket, deadline: float) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AnnotationBackendUnavailableError("标注基座请求超过允许时限")
        sock.settimeout(remaining)

    def _set_remaining_timeout(
        self,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> None:
        """把已连接 socket timeout 收紧到本次 HTTP 操作剩余的端到端预算。"""
        self._set_socket_remaining_timeout(self._connected_socket(connection), deadline)

    def _read_response_body(
        self,
        *,
        response_socket: socket.socket,
        response: http.client.HTTPResponse,
        deadline: float,
        destination: BinaryIO | None = None,
    ) -> bytes:
        """分段读取响应并在每次底层读取前重新收紧剩余 wall-clock 预算。"""
        chunks: list[bytes] = []
        while True:
            self._set_socket_remaining_timeout(response_socket, deadline)
            chunk = response.read1(1024 * 1024)
            if not chunk:
                break
            if destination is None:
                chunks.append(chunk)
            else:
                destination.write(chunk)
        return b"".join(chunks)

    def _post_json(self, *, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        def send(connection: http.client.HTTPConnection, deadline: float) -> None:
            self._set_remaining_timeout(connection, deadline)
            connection.send(body)

        response_body = self._request(
            path=path,
            content_type="application/json",
            content_length=len(body),
            send_body=send,
        )
        return _json_object(response_body)

    def _post_multipart_bytes(
        self,
        *,
        path: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        boundary = f"nvsop-{uuid.uuid4().hex}"
        prefix, suffix = _multipart_parts(
            boundary=boundary,
            filename=filename,
            content_type=content_type,
        )
        total = len(prefix) + len(content) + len(suffix)

        def send(connection: http.client.HTTPConnection, deadline: float) -> None:
            self._set_remaining_timeout(connection, deadline)
            connection.send(prefix)
            self._set_remaining_timeout(connection, deadline)
            connection.send(content)
            self._set_remaining_timeout(connection, deadline)
            connection.send(suffix)

        response_body = self._request(
            path=path,
            content_type=f"multipart/form-data; boundary={boundary}",
            content_length=total,
            send_body=send,
        )
        return _json_object(response_body)

    def _post_multipart_file(
        self,
        *,
        path: str,
        filename: str,
        content_type: str,
        source: BinaryIO,
    ) -> dict[str, Any]:
        boundary = f"nvsop-{uuid.uuid4().hex}"
        prefix, suffix = _multipart_parts(
            boundary=boundary,
            filename=filename,
            content_type=content_type,
        )
        try:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(0)
        except (OSError, ValueError) as error:
            raise AnnotationBackendUnavailableError("标注源文件不可读取") from error
        total = len(prefix) + size + len(suffix)

        def send(connection: http.client.HTTPConnection, deadline: float) -> None:
            self._set_remaining_timeout(connection, deadline)
            connection.send(prefix)
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                self._set_remaining_timeout(connection, deadline)
                connection.send(chunk)
            self._set_remaining_timeout(connection, deadline)
            connection.send(suffix)

        response_body = self._request(
            path=path,
            content_type=f"multipart/form-data; boundary={boundary}",
            content_length=total,
            send_body=send,
        )
        return _json_object(response_body)

    def _request(
        self,
        *,
        path: str,
        content_type: str,
        content_length: int,
        send_body: Callable[[http.client.HTTPConnection, float], None],
    ) -> bytes:
        deadline = monotonic() + self._timeout
        connection = self._connection()
        try:
            connection.connect()
            self._set_remaining_timeout(connection, deadline)
            connection.putrequest("POST", self._base_path + path)
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(content_length))
            connection.putheader("Connection", "close")
            connection.endheaders()
            send_body(connection, deadline)
            self._set_remaining_timeout(connection, deadline)
            response_socket = self._connected_socket(connection)
            response = connection.getresponse()
            body = self._read_response_body(
                response_socket=response_socket,
                response=response,
                deadline=deadline,
            )
            if not 200 <= response.status < 300:
                raise AnnotationBackendExecutionError(f"标注基座请求失败（HTTP {response.status}）")
            return body
        except AnnotationBackendExecutionError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise AnnotationBackendUnavailableError("无法连接标注基座") from error
        finally:
            connection.close()


def _multipart_parts(*, boundary: str, filename: str, content_type: str) -> tuple[bytes, bytes]:
    # multipart 头只使用可打印 ASCII，避免文件名中的引号或 CR/LF 注入新头字段。
    candidate = PurePosixPath(filename).name or "upload.bin"
    safe_filename = "".join(
        character if 0x20 <= ord(character) <= 0x7E and character not in {'"', "\\"} else "_"
        for character in candidate
    )
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix, suffix


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnnotationBackendExecutionError("标注基座返回了非 JSON 响应") from error
    if not isinstance(value, dict):
        raise AnnotationBackendExecutionError("标注基座返回了非对象响应")
    return value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise AnnotationBackendExecutionError(f"标注基座响应缺少 {key}")
    return result
