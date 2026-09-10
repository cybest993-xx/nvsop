"""通过正式控制面 API 导入训练视频，并把视频直传到预签名对象地址。

客户端只提交数据集和视频声明到 `/api/v1`。视频请求体只发往 API 返回的对象存储地址，
不会经过 FastAPI，也不会直接写入数据库。
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import secrets
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

API_PREFIX = "/api/v1"
SESSION_COOKIE = "sop_session"
CSRF_COOKIE = "sop_csrf"
CSRF_HEADER = "x-csrf-token"
MODIFYING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_JOB_IN_PROGRESS_STATUSES = frozenset({"pending", "enqueued", "running"})
_DEFAULT_JOB_POLL_INTERVAL_SECONDS = 1.0
_DEFAULT_JOB_POLL_TIMEOUT_SECONDS = 900.0

JsonObject = dict[str, object]


class _RawHeaders(Protocol):
    """urllib 和 `http.client` 响应头的共同最小接口。"""

    def items(self) -> Iterable[tuple[str, str]]:
        """遍历响应头。"""
        ...


class _RawHttpResponse(Protocol):
    """urllib 和 `HTTPError` 响应的共同最小接口。"""

    @property
    def status(self) -> int | None:
        """HTTP 状态码。"""
        ...

    @property
    def headers(self) -> _RawHeaders:
        """响应头。"""
        ...

    def read(self) -> bytes:
        """读取响应体。"""
        ...


class DatasetImportError(RuntimeError):
    """脚本调用或上传失败；错误中不包含会话凭据。"""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """传输层返回的最小 HTTP 事实。"""

    status: int
    headers: Mapping[str, str]
    body: bytes
    cookies: Mapping[str, str]


class HttpTransport(Protocol):
    """控制面 JSON 请求和对象存储直传共用的 HTTP seam。"""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        """发送一个控制面请求并返回原始响应。"""
        ...

    def upload_file(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        path: Path,
    ) -> HttpResponse:
        """把文件发往预签名地址；不得把文件改发给控制面。"""
        ...


class UrllibTransport:
    """标准库 HTTP 适配器；视频上传按文件流发送，不把内容交给 FastAPI。"""

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        self._timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        """发送 JSON 或无体请求，并把 HTTP 错误作为可检查响应返回。"""
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return _urllib_response(response)
        except HTTPError as error:
            return _urllib_response(error)
        except URLError as error:
            raise DatasetImportError(f"无法连接 HTTP 服务：{error.reason}") from error

    def upload_file(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        path: Path,
    ) -> HttpResponse:
        """按预签名说明直传文件；POST 使用表单，PUT 使用原始文件流。"""
        if not path.is_file():
            raise DatasetImportError(f"视频文件不存在：{path}")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DatasetImportError("预签名地址必须是 HTTP(S) 地址")
        request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection_type: type[http.client.HTTPConnection]
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_type(parsed.hostname, parsed.port, timeout=self._timeout_seconds)
        request_headers = dict(headers)
        body: _MultipartBody | BinaryIO
        if method.upper() == "POST":
            boundary = f"----nvsop-{secrets.token_hex(16)}"
            prefix, suffix = _multipart_edges(boundary, fields, path.name)
            body = _MultipartBody(prefix=prefix, path=path, suffix=suffix)
            request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            request_headers["Content-Length"] = str(len(prefix) + path.stat().st_size + len(suffix))
        elif method.upper() == "PUT":
            if fields:
                raise DatasetImportError("PUT 预签名地址不应携带表单字段")
            body = path.open("rb")
            request_headers.setdefault("Content-Type", "video/mp4")
            request_headers["Content-Length"] = str(path.stat().st_size)
        else:
            raise DatasetImportError(f"不支持的预签名上传方法：{method}")

        try:
            connection.request(method.upper(), request_target, body=body, headers=request_headers)
            response = connection.getresponse()
            return _http_response(
                status=response.status,
                headers=response.headers,
                body=response.read(),
            )
        except OSError as error:
            raise DatasetImportError(f"直传对象失败：{error}") from error
        finally:
            body.close()
            connection.close()


class _MultipartBody:
    """给 `http.client` 使用的流式 multipart 文件体。"""

    def __init__(self, *, prefix: bytes, path: Path, suffix: bytes) -> None:
        self._streams: list[BinaryIO] = [io.BytesIO(prefix), path.open("rb"), io.BytesIO(suffix)]
        self._current = 0

    def read(self, size: int = -1) -> bytes:
        """按块读取 multipart 内容，避免把视频整体载入内存。"""
        if size < 0:
            all_chunks: list[bytes] = []
            while True:
                chunk = self.read(1024 * 1024)
                if not chunk:
                    return b"".join(all_chunks)
                all_chunks.append(chunk)

        chunks: list[bytes] = []
        remaining = size
        while remaining > 0 and self._current < len(self._streams):
            stream = self._streams[self._current]
            chunk = stream.read(remaining)
            if chunk:
                chunks.append(chunk)
                remaining -= len(chunk)
                continue
            stream.close()
            self._current += 1
        return b"".join(chunks)

    def close(self) -> None:
        """关闭尚未读完的文件流。"""
        for stream in self._streams[self._current :]:
            stream.close()
        self._current = len(self._streams)


def _multipart_edges(
    boundary: str, fields: Mapping[str, str], filename: str
) -> tuple[bytes, bytes]:
    """构造表单头尾；文件本身仍由 `_MultipartBody` 流式读取。"""
    prefix = bytearray()
    for name, value in fields.items():
        prefix.extend(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{_header_value(name)}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    prefix.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{_header_value(filename)}"\r\n'
            "Content-Type: video/mp4\r\n\r\n"
        ).encode()
    )
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return bytes(prefix), suffix


def _header_value(value: str) -> str:
    """阻止服务端返回的字段值破坏 multipart 头。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _urllib_response(response: _RawHttpResponse) -> HttpResponse:
    """把 urllib 响应（包括 HTTPError）转成统一事实。"""
    if response.status is None:
        raise DatasetImportError("HTTP 响应没有状态码")
    return _http_response(
        status=response.status,
        headers=response.headers,
        body=response.read(),
    )


def _http_response(*, status: int, headers: _RawHeaders, body: bytes) -> HttpResponse:
    """规范化大小写并保留 Set-Cookie 中的会话信息。"""
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    cookies = _cookies_from_headers(headers)
    return HttpResponse(status=status, headers=normalized, body=body, cookies=cookies)


def _cookies_from_headers(headers: _RawHeaders) -> dict[str, str]:
    """读取可能重复的 Set-Cookie 头，不记录 cookie 属性或凭据。"""
    get_all = cast(Callable[[str, object], object], getattr(headers, "get_all", None))
    if get_all is None:
        return {}
    values = get_all("Set-Cookie", [])
    if not isinstance(values, list):
        return {}
    jar = SimpleCookie()
    for value in values:
        if isinstance(value, str):
            jar.load(value)
    return {name: morsel.value for name, morsel in jar.items()}


class ControlPlaneClient:
    """面向正式 `/api/v1` 入口的会话客户端。"""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        base_url: str,
        session_cookie: str | None = None,
        csrf_token: str | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url 必须是带主机的 HTTP(S) 地址")
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self.session_cookie = session_cookie
        self.csrf_token = csrf_token

    def login(self, *, login_name: str, password: str) -> JsonObject:
        """通过正式会话入口登录，接收服务端下发的双 cookie。"""
        response = self._send_json(
            "POST",
            f"{API_PREFIX}/auth/session",
            {"login_name": login_name, "password": password},
            authenticated=False,
        )
        payload = _expect_json(response, expected={201})
        self.session_cookie = response.cookies.get(SESSION_COOKIE)
        self.csrf_token = response.cookies.get(CSRF_COOKIE)
        if self.session_cookie is None or self.csrf_token is None:
            raise DatasetImportError("登录响应没有返回完整会话 cookie")
        return payload

    def create_dataset(self, *, name: str) -> JsonObject:
        """调用创建训练数据集的正式资源入口。"""
        return _expect_json(
            self._send_json("POST", f"{API_PREFIX}/training-datasets", {"name": name}),
            expected={201},
        )

    def request_video_upload(
        self, *, dataset_id: str, declaration: JsonObject, idempotency_key: str
    ) -> JsonObject:
        """为一个视频调用逐成员申请入口，并带唯一幂等键。"""
        return _expect_json(
            self._send_json(
                "POST",
                f"{API_PREFIX}/training-datasets/{dataset_id}/members",
                declaration,
                idempotency_key=idempotency_key,
            ),
            expected={201},
        )

    def upload_file(self, *, instructions: JsonObject, path: Path) -> HttpResponse:
        """按 API 返回的说明把文件直传对象存储。"""
        return self._transport.upload_file(
            _required_string(instructions, "url"),
            method=_required_string(instructions, "method"),
            headers=_string_mapping(instructions.get("headers")),
            fields=_string_mapping(instructions.get("fields")),
            path=path,
        )

    def confirm_video_upload(
        self, *, dataset_id: str, member_id: str, attempt_id: str
    ) -> JsonObject:
        """通知正式控制面某个直传尝试可以校验；请求体只含尝试身份。"""
        return _expect_json(
            self._send_json(
                "POST",
                f"{API_PREFIX}/training-datasets/{dataset_id}/members/{member_id}/confirm",
                {"attempt_id": attempt_id},
            ),
            expected={202, 200},
        )

    def read_job(self, *, job_id: str) -> JsonObject:
        """通过正式任务资源入口查询一次视频校验任务。"""
        return _expect_json(
            self._send_json("GET", f"{API_PREFIX}/jobs/{job_id}"),
            expected={200},
        )

    def wait_for_job(
        self,
        *,
        job_id: str,
        poll_interval_seconds: float = _DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        poll_timeout_seconds: float = _DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> JsonObject:
        """轮询任务到终态；未知状态原样返回，不把它误判成成功。"""
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        if poll_timeout_seconds < 0:
            raise ValueError("poll_timeout_seconds 不能为负数")
        deadline = monotonic() + poll_timeout_seconds
        while True:
            job = self.read_job(job_id=job_id)
            status = job.get("status")
            if not isinstance(status, str) or status not in _JOB_IN_PROGRESS_STATUSES:
                return job
            if poll_timeout_seconds == 0:
                return job
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise DatasetImportError(f"任务轮询超时，最后状态：{status}")
            sleep(min(poll_interval_seconds, remaining))

    def _send_json(
        self,
        method: str,
        path: str,
        payload: JsonObject | None = None,
        *,
        authenticated: bool = True,
        idempotency_key: str | None = None,
    ) -> HttpResponse:
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if method not in {"GET", "HEAD"}:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if authenticated:
            headers["Cookie"] = self._cookie_header()
            if method in MODIFYING_METHODS:
                if self.csrf_token is None:
                    raise DatasetImportError("修改控制面资源需要 CSRF token")
                headers[CSRF_HEADER] = self.csrf_token
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return self._transport.request(
            method,
            f"{self._base_url}{path}",
            headers=headers,
            body=body,
        )

    def _cookie_header(self) -> str:
        if self.session_cookie is None or self.csrf_token is None:
            raise DatasetImportError("控制面请求需要会话 cookie 和 CSRF token")
        return f"{SESSION_COOKIE}={self.session_cookie}; {CSRF_COOKIE}={self.csrf_token}"


def _expect_json(response: HttpResponse, *, expected: set[int]) -> JsonObject:
    if response.status not in expected:
        detail = response.body.decode("utf-8", errors="replace")
        raise DatasetImportError(f"控制面返回 HTTP {response.status}：{detail}")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise DatasetImportError("控制面没有返回 JSON") from error
    if not isinstance(value, dict):
        raise DatasetImportError("控制面响应不是 JSON 对象")
    return cast(JsonObject, value)


def _required_string(value: JsonObject, key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise DatasetImportError(f"响应缺少字符串字段：{key}")
    return candidate


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise DatasetImportError("上传说明中的字段不是对象")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise DatasetImportError("上传说明字段必须是字符串键值")
        result[key] = item
    return result


def _require_successful_job(job: JsonObject) -> JsonObject:
    """只有校验成功才允许脚本以成功结果结束；未知终态也不能伪装成功。"""
    status = job.get("status")
    if status == "succeeded":
        return job
    failure_code = job.get("failure_code")
    suffix = f"（原因码：{failure_code}）" if isinstance(failure_code, str) else ""
    raise DatasetImportError(f"视频校验未成功，任务状态：{status!r}{suffix}")


@dataclass(frozen=True, slots=True)
class ImportedVideo:
    """一个视频的成员、尝试和最后一次任务响应。"""

    path: Path
    member: JsonObject
    attempt: JsonObject
    job: JsonObject


@dataclass(frozen=True, slots=True)
class DatasetImportResult:
    """脚本完成后可打印的控制面资源摘要。"""

    dataset: JsonObject
    videos: tuple[ImportedVideo, ...]


def import_training_dataset(
    client: ControlPlaneClient,
    *,
    dataset_name: str,
    source: str,
    videos: Sequence[Path],
    poll_interval_seconds: float = _DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    poll_timeout_seconds: float = _DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
) -> DatasetImportResult:
    """创建数据集后逐个申请、直传、确认并轮询视频校验任务。"""
    if not dataset_name.strip():
        raise ValueError("dataset_name 不能为空")
    if not source.strip():
        raise ValueError("source 不能为空")
    if not videos:
        raise ValueError("至少需要一个视频文件")

    dataset = client.create_dataset(name=dataset_name)
    dataset_id = _required_string(dataset, "id")
    imported: list[ImportedVideo] = []
    for index, path in enumerate(videos, start=1):
        size, digest = _file_facts(path)
        declaration: JsonObject = {
            "original_filename": path.name,
            "source": source,
            "declared_size": size,
            "declared_sha256": digest,
        }
        requested = client.request_video_upload(
            dataset_id=dataset_id,
            declaration=declaration,
            idempotency_key=f"dataset-{dataset_id}-video-{index}",
        )
        upload = requested.get("upload")
        if not isinstance(upload, dict):
            raise DatasetImportError("申请上传响应没有上传说明")
        upload_instructions = cast(JsonObject, upload)
        upload_response = client.upload_file(instructions=upload_instructions, path=path)
        if upload_response.status not in {200, 201, 204}:
            detail = upload_response.body.decode("utf-8", errors="replace")
            raise DatasetImportError(f"对象存储返回 HTTP {upload_response.status}：{detail}")

        member = requested.get("member")
        attempt = requested.get("attempt")
        if not isinstance(member, dict) or not isinstance(attempt, dict):
            raise DatasetImportError("申请上传响应缺少成员或尝试")
        member_object = cast(JsonObject, member)
        attempt_object = cast(JsonObject, attempt)
        confirmed = client.confirm_video_upload(
            dataset_id=dataset_id,
            member_id=_required_string(member_object, "id"),
            attempt_id=_required_string(attempt_object, "id"),
        )
        job_value = confirmed.get("job")
        if not isinstance(job_value, dict):
            raise DatasetImportError("确认响应没有校验任务")
        polled_job = client.wait_for_job(
            job_id=_required_string(cast(JsonObject, job_value), "id"),
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
        )
        if poll_timeout_seconds == 0 and polled_job.get("status") in _JOB_IN_PROGRESS_STATUSES:
            job = polled_job
        else:
            job = _require_successful_job(polled_job)
        imported.append(
            ImportedVideo(
                path=path,
                member=member_object,
                attempt=attempt_object,
                job=job,
            )
        )
    return DatasetImportResult(dataset=dataset, videos=tuple(imported))


def _file_facts(path: Path) -> tuple[int, str]:
    """流式读取视频大小和 SHA-256；不把媒体内容放进 API JSON。"""
    if not path.is_file():
        raise DatasetImportError(f"视频文件不存在：{path}")
    size = path.stat().st_size
    if size <= 0:
        raise DatasetImportError(f"视频文件为空：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return size, digest.hexdigest()
