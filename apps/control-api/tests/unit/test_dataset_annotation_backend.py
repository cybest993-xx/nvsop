"""标注基座 HTTP adapter 的真实请求形状与错误边界。"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

import factory_sop.dataset.adapters.annotation as annotation_module
from factory_sop.dataset.adapters.annotation import HttpAnnotationBackend
from factory_sop.dataset.annotation import (
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
)
from factory_sop.dataset.model import AnnotationMode, AnnotationSegment


class AnnotationBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: ClassVar[list[tuple[str, bytes]]] = []
    fail_split: ClassVar[bool] = False
    cleanup_files_deleted: ClassVar[int] = 1

    def do_GET(self) -> None:
        self.requests.append((self.path, b""))
        encoded = b"derived video bytes"
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_DELETE(self) -> None:
        self.requests.append((self.path, b""))
        response = {"deleted_count": 1, "files_deleted": self.cleanup_files_deleted}
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.requests.append((self.path, body))
        response: dict[str, object]
        if self.path == "/api/v1/actions/upload":
            response = {"data_id": "base-dataset"}
            status = 200
        elif self.path.startswith("/api/v1/upload?"):
            query = parse_qs(urlsplit(self.path).query)
            assert query == {"target_data_id": ["base-dataset"]}
            response = {"file_id": "base-video"}
            status = 200
        elif self.path == "/api/v1/videos/base-video/split":
            response = (
                {"detail": "upstream failed"}
                if self.fail_split
                else {"clips": [{"id": "clip-1", "filename": "clip.mp4"}]}
            )
            status = 500 if self.fail_split else 200
        else:
            response = {"detail": "unexpected path"}
            status = 404
        encoded = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: object) -> None:
        return


@contextmanager
def backend_server() -> Iterator[tuple[str, type[AnnotationBackendHandler]]]:
    AnnotationBackendHandler.requests = []
    AnnotationBackendHandler.fail_split = False
    AnnotationBackendHandler.cleanup_files_deleted = 1
    server = HTTPServer(("127.0.0.1", 0), AnnotationBackendHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", AnnotationBackendHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_prepare_and_split_use_explicit_target_and_preserve_wire_mode(tmp_path: Path) -> None:
    with backend_server() as (origin, handler):
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=tmp_path)
        prepared = backend.prepare_video(
            source=io.BytesIO(b"synthetic video"),
            filename="line.mp4",
            actions=["(1)拿取工件"],
        )
        derived = io.BytesIO()
        backend.download_video(video_id=prepared.video_id, destination=derived)
        clips = backend.split_video(
            video_id=prepared.video_id,
            segments=(
                AnnotationSegment(
                    start=0.0,
                    end=1.25,
                    action_index=0,
                    action_description="(1)拿取工件",
                ),
            ),
            mode=AnnotationMode.TWO_OPERATOR,
        )

    assert prepared.data_id == "base-dataset"
    assert prepared.video_id == "base-video"
    assert derived.getvalue() == b"derived video bytes"
    assert clips == [{"id": "clip-1", "filename": "clip.mp4"}]
    assert [path for path, _body in handler.requests] == [
        "/api/v1/actions/upload",
        "/api/v1/upload?target_data_id=base-dataset",
        "/api/v1/videos/base-video/download",
        "/api/v1/videos/base-video/split",
    ]
    split_body = json.loads(handler.requests[-1][1])
    assert split_body == {
        "timestamps": [
            {
                "start": 0.0,
                "end": 1.25,
                "actionIndex": 0,
                "actionDescription": "(1)拿取工件",
            }
        ],
        "twoOperatorMode": True,
    }


def test_discard_prepared_video_uses_vendor_cleanup_endpoint(tmp_path: Path) -> None:
    with backend_server() as (origin, handler):
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=tmp_path)
        backend.discard_prepared_video(data_id="base dataset")

    assert handler.requests == [
        ("/api/v1/videos/clear-dataset/base%20dataset", b""),
    ]


def test_discard_prepared_video_requires_confirmed_file_deletion(tmp_path: Path) -> None:
    (tmp_path / "base-dataset").mkdir()
    with backend_server() as (origin, handler):
        handler.cleanup_files_deleted = 0
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=tmp_path)
        with pytest.raises(AnnotationBackendExecutionError, match="未确认工作副本文件删除"):
            backend.discard_prepared_video(data_id="base-dataset")


def test_discard_prepared_video_accepts_already_absent_directory(tmp_path: Path) -> None:
    with backend_server() as (origin, handler):
        handler.cleanup_files_deleted = 0
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=tmp_path)
        backend.discard_prepared_video(data_id="base-dataset")


def test_discard_prepared_video_requires_available_data_root(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    with backend_server() as (origin, handler):
        handler.cleanup_files_deleted = 0
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=missing_root)
        with pytest.raises(AnnotationBackendUnavailableError, match="清理结果"):
            backend.discard_prepared_video(data_id="base-dataset")


def test_multipart_filename_cannot_inject_a_header(tmp_path: Path) -> None:
    with backend_server() as (origin, handler):
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=tmp_path)
        backend.prepare_video(
            source=io.BytesIO(b"synthetic video"),
            filename='unsafe"\r\nX-Injected: yes.mp4',
            actions=["(1)拿取工件"],
        )

    upload_body = handler.requests[1][1]
    assert b"\r\nX-Injected:" not in upload_body
    assert b'filename="unsafe___X-Injected: yes.mp4"' in upload_body

    with backend_server() as (origin, handler):
        handler.fail_split = True
        backend = HttpAnnotationBackend(base_url=origin, timeout_seconds=5, data_root=tmp_path)
        with pytest.raises(AnnotationBackendExecutionError, match="HTTP 500"):
            backend.split_video(
                video_id="base-video",
                segments=(),
                mode=AnnotationMode.SINGLE_OPERATOR,
            )


def test_download_video_enforces_end_to_end_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    class Socket:
        def settimeout(self, timeout: float) -> None:
            assert timeout > 0

    class Response:
        status = 200

        def __init__(self) -> None:
            self.reads = 0

        def read1(self, _size: int = -1) -> bytes:
            self.reads += 1
            if self.reads == 1:
                clock[0] = 2.0
                return b"first chunk"
            return b""

    class Connection:
        def __init__(self) -> None:
            self.sock = Socket()
            self.response = Response()

        def connect(self) -> None:
            return None

        def putrequest(self, *_args: object, **_kwargs: object) -> None:
            return None

        def putheader(self, *_args: object, **_kwargs: object) -> None:
            return None

        def endheaders(self) -> None:
            return None

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            return None

    backend = HttpAnnotationBackend(
        base_url="http://annotation.invalid",
        timeout_seconds=1,
        data_root=tmp_path,
    )
    connection = Connection()
    monkeypatch.setattr(annotation_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(backend, "_connection", lambda: connection)

    with pytest.raises(AnnotationBackendUnavailableError, match="超过允许时限"):
        backend.download_video(video_id="base-video", destination=io.BytesIO())
