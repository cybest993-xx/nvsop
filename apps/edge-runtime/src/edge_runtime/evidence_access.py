"""通过中心逐次授权读取已登记的本机证据媒体。

浏览器只从产生证据的推理机读取字节;本服务每次 GET/HEAD 都拿短票据回查中心,中心只回
引用与摘要、绝不代理字节(ADR-0012)。证据 ID 是中心的不透明标识,可能含斜杠与非 ASCII,
因此路由按百分号编码的单段路径接收,解码后再转发,客户端不能借此传入本机路径。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

from nvsop_contracts.retention_impact import ReportedImpactQuery

from edge_runtime.center_client import CenterClient, CenterUnreachableError
from edge_runtime.local_state.retention import (
    ReportedImpactUnavailableError,
    read_reported_impact,
)

# 中心签发票据的长度上限(见中心 check_access_context);超过即非本服务产生的票据。
_CONTEXT_LIMIT = 4096
# 中心 410 的 detail 与浏览器可见的过期/失效语义一一对应,逐字保留,不与 403/404 混同。
_EXPIRED = {"context_expired", "reference_expired", "reference_unavailable"}


class _AccessError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code


def _open_reference(root: Path, reference: str) -> int:
    """只打开根目录下已登记的普通文件;拒绝绝对路径、穿越、空段与符号链接。"""
    parts = reference.split("/")
    if (
        not reference
        or "\\" in reference
        or PurePosixPath(reference).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise _AccessError(404, "media_missing")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(root, flags | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise _AccessError(404, "media_missing") from error
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], flags, dir_fd=directory)
    except OSError as error:
        raise _AccessError(404, "media_missing") from error
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _AccessError(404, "media_missing")
    return fd


def _valid_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
            and "?" not in value
            and "#" not in value
        )
    except ValueError:
        return False


def _center_refusal(status: int, body: bytes) -> tuple[int, str]:
    """把中心的拒绝映射为来源侧稳定错误码。

    401 是主机身份认证失败,不能报成浏览器用户权限不足;403 区分会话/权限与一般拒绝;
    410 逐字保留过期与引用失效的区别;404 是中心未登记,与来源缺文件是两种事实。
    """
    record: dict[str, object] = {}
    with suppress(ValueError, TypeError):
        decoded = json.loads(body)
        if isinstance(decoded, dict):
            record = decoded
    detail = record.get("detail")
    if status == 401:
        return 401, "source_auth_failed"
    if record.get("error_code") == "PERMISSION_DENIED":
        return 403, "permission_denied"
    if status == 404:
        return 404, "not_registered"
    if status == 410 and detail in _EXPIRED:
        return 410, str(detail)
    if status == 403:
        return 403, "session_invalid" if detail == "session_invalid" else "access_denied"
    return 503, "authorization_unavailable"


def create_server(
    *,
    address: tuple[str, int],
    client: CenterClient,
    directory: Path,
    web_origin: str,
    local_state_path: Path | None = None,
) -> ThreadingHTTPServer:
    if not _valid_origin(web_origin):
        raise ValueError("web_origin must be one HTTP(S) origin without credentials or suffixes")

    class Handler(BaseHTTPRequestHandler):
        # 视频回看靠多次 Range 请求,HTTP/1.1 保活避免每段重连。
        protocol_version = "HTTP/1.1"

        def _cors(self) -> bool:
            self.origin = self.headers.get("Origin")
            self.allowed_origin = self.origin == web_origin
            if self.origin is not None and not self.allowed_origin:
                self._error(403, "origin_denied")
                return False
            return True

        def _common_headers(self) -> None:
            # 撤销后不能由浏览器缓存继续供片:所有响应都禁止缓存。
            self.send_header("Cache-Control", "no-store")
            if self.allowed_origin:
                self.send_header("Access-Control-Allow-Origin", web_origin)
                self.send_header("Vary", "Origin")
                self.send_header(
                    "Access-Control-Expose-Headers",
                    "Content-Range, Accept-Ranges, X-Evidence-Error",
                )

        def do_OPTIONS(self) -> None:
            if not self._cors():
                return
            self.send_response(204)
            self._common_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self) -> None:
            self._serve(True)

        def do_HEAD(self) -> None:
            self._serve(False)

        def _error(self, status: int, code: str) -> None:
            body = json.dumps({"error": code}).encode()
            self.send_response(status)
            self._common_headers()
            self.send_header("X-Evidence-Error", code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                with suppress(OSError):
                    self.wfile.write(body)

        def _authorize(self) -> tuple[int, str, str]:
            parsed = urlsplit(self.path)
            match = re.fullmatch(r"/evidence/([^/?#]+)", parsed.path)
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                query = {}
            if not match or set(query) != {"context"} or len(query["context"]) != 1:
                raise _AccessError(400, "invalid_request")
            try:
                evidence_id = unquote(match.group(1), errors="strict")
            except UnicodeDecodeError as error:
                raise _AccessError(400, "invalid_request") from error
            context = query["context"][0]
            if not evidence_id or not context or len(context) > _CONTEXT_LIMIT:
                raise _AccessError(400, "invalid_request")
            try:
                grant = client.post(
                    "/api/v1/evidence/access-check",
                    {"evidence_id": evidence_id, "context": context},
                )
            except CenterUnreachableError as error:
                raise _AccessError(503, "center_unavailable") from error
            if grant.status != 200:
                raise _AccessError(*_center_refusal(grant.status, grant.body))
            try:
                record = json.loads(grant.body)
                reference, size = record["reference"], record["size"]
                kind, digest = record["kind"], record["sha256"]
                if (
                    not isinstance(reference, str)
                    or type(size) is not int
                    or size < 0
                    or kind not in ("clip", "keyframe")
                    or not isinstance(digest, str)
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise _AccessError(503, "invalid_authorization") from error
            return size, reference, kind

        def _reported_impact(self) -> None:
            # 独立统计授权作用域;媒体读取授权不能提供统计请求。
            try:
                if self.command != "GET":
                    raise _AccessError(405, "method_not_allowed")
                parsed = urlsplit(self.path)
                try:
                    values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                except ValueError as error:
                    raise _AccessError(400, "invalid_request") from error
                if set(values) != {"context"} or len(values["context"]) != 1:
                    raise _AccessError(400, "invalid_request")
                context = values["context"][0]
                if not context or len(context) > _CONTEXT_LIMIT:
                    raise _AccessError(400, "invalid_request")
                if local_state_path is None:
                    raise _AccessError(503, "local_state_unconfigured")
                try:
                    grant = client.post(
                        "/api/v1/retention/reported-impact/access-check", {"context": context}
                    )
                except CenterUnreachableError as error:
                    raise _AccessError(503, "center_unavailable") from error
                if grant.status != 200:
                    raise _AccessError(*_center_refusal(grant.status, grant.body))
                try:
                    record = json.loads(grant.body)
                    if not isinstance(record, dict):
                        raise ValueError
                    query = ReportedImpactQuery.from_wire(record)
                except (ValueError, TypeError) as error:
                    raise _AccessError(503, "invalid_authorization") from error
                try:
                    result = read_reported_impact(local_state_path, query=query)
                except ReportedImpactUnavailableError as error:
                    raise _AccessError(409, str(error)) from error
                except (sqlite3.Error, OSError) as error:
                    raise _AccessError(503, "local_state_unavailable") from error
            except _AccessError as error:
                self._error(error.status, error.code)
                return
            payload = {
                "query": query.to_wire(),
                "bytes_basis": result.bytes_basis,
                "exposed": asdict(result.exposed),
                "protected": asdict(result.protected),
            }
            encoded = json.dumps(payload, default=lambda value: value.isoformat()).encode()
            self.send_response(200)
            self._common_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            with suppress(OSError):
                self.wfile.write(encoded)

        def _serve(self, body: bool) -> None:
            if not self._cors():
                return
            if urlsplit(self.path).path == "/retention/reported-impact":
                self._reported_impact()
                return
            try:
                size, reference, kind = self._authorize()
                fd = _open_reference(directory, reference)
            except _AccessError as error:
                self._error(error.status, error.code)
                return
            try:
                if os.fstat(fd).st_size != size:
                    self._error(503, "media_changed")
                    return
                start, end, status = 0, size - 1, 200
                rng = self.headers.get("Range")
                if rng:
                    # 限定位宽: 超长数字串在 Python int() 会抛 ValueError, 应回 416 而非断连。
                    parsed_range = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", rng)
                    if not parsed_range or not (parsed_range.group(1) or parsed_range.group(2)):
                        self._error(416, "invalid_range")
                        return
                    first, last = parsed_range.groups()
                    if not size or (not first and int(last) == 0):
                        self._error(416, "range_unsatisfiable")
                        return
                    if first:
                        start = int(first)
                        end = min(int(last) if last else size - 1, size - 1)
                    else:
                        count = min(int(last), size)
                        start, end = size - count, size - 1
                    if start >= size or start > end:
                        self._error(416, "range_unsatisfiable")
                        return
                    status = 206
                length = max(0, end - start + 1)
                self.send_response(status)
                self._common_headers()
                self.send_header("Content-Type", "video/mp4" if kind == "clip" else "image/jpeg")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if body:
                    os.lseek(fd, start, os.SEEK_SET)
                    remaining = length
                    while remaining:
                        try:
                            chunk = os.read(fd, min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                        except OSError:
                            break
                        remaining -= len(chunk)
            finally:
                os.close(fd)

        def log_message(self, message_format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(address, Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Edge evidence playback service")
    parser.add_argument("--center-url", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--private-key-file", required=True, type=Path)
    parser.add_argument("--evidence-directory", required=True, type=Path)
    parser.add_argument("--web-origin", required=True)
    parser.add_argument("--local-state", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    key = args.private_key_file.read_text(encoding="utf-8").strip()
    client = CenterClient(
        center_url=args.center_url, host_id=args.host_id, host_private_key=key, timeout=args.timeout
    )
    server = create_server(
        address=(args.bind, args.port),
        client=client,
        directory=args.evidence_directory,
        web_origin=args.web_origin,
        local_state_path=args.local_state,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
