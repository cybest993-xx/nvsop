"""Edge 证据读取 HTTP 集成: 授权边界、范围读取与本机文件约束。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast

from edge_runtime.center_client import CenterClient, CenterResponse
from edge_runtime.evidence_access import create_server


class EvidenceAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media.mp4"
        self.media.write_bytes(b"0123456789")
        self.body: object = {
            "reference": "media.mp4",
            "size": 10,
            "kind": "clip",
            "sha256": "unused",
        }
        self.status = 200
        self.calls: list[tuple[str, dict[str, object]]] = []
        owner = self

        class Client:
            def post(self, path: str, body: dict[str, object]) -> CenterResponse:
                owner.calls.append((path, body))
                payload = owner.body
                encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return CenterResponse(owner.status, encoded)

        self.server = create_server(
            address=("127.0.0.1", 0),
            client=cast(CenterClient, Client()),
            directory=self.root,
            web_origin="http://web.local",
        )
        self.assertIsInstance(self.server, ThreadingHTTPServer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(
        self,
        method: str = "GET",
        path: str = "/evidence/host-1?context=short",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_range_head_and_no_store_are_authorized_and_stream_exact_bytes(self) -> None:
        status, headers, body = self.request(
            headers={"Origin": "http://web.local", "Range": "bytes=2-5"}
        )
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://web.local")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(self.request("GET", headers={"Range": "bytes=-3"})[2], b"789")
        self.assertEqual(self.request("GET", headers={"Range": "bytes=8-"})[2], b"89")
        status, headers, body = self.request("HEAD")
        self.assertEqual(
            (status, headers["Content-Type"], headers["Content-Length"]), (200, "video/mp4", "10")
        )
        self.assertEqual(body, b"")
        self.assertEqual(
            self.calls[0],
            ("/api/v1/evidence/access-check", {"evidence_id": "host-1", "context": "short"}),
        )

    def test_opaque_percent_encoded_ids_reach_the_center_unchanged(self) -> None:
        for encoded, decoded in (
            ("host-1%2Fclip.mp4", "host-1/clip.mp4"),
            ("%E8%AF%81%E6%8D%AE.mp4", "证据.mp4"),
        ):
            self.request(path=f"/evidence/{encoded}?context=short")
            self.assertEqual(self.calls[-1][1]["evidence_id"], decoded)
        self.assertEqual(self.request(path="/evidence/host-1/extra?context=short")[0], 400)
        self.assertEqual(self.request(path="/evidence/%FF?context=short")[0], 400)

    def test_context_is_bounded_to_the_signed_limit(self) -> None:
        self.assertEqual(self.request(path=f"/evidence/host-1?context={'a' * 4096}")[0], 200)
        status, headers, _ = self.request(path=f"/evidence/host-1?context={'a' * 4097}")
        self.assertEqual((status, headers["X-Evidence-Error"]), (400, "invalid_request"))

    def test_center_refusals_keep_permission_expiry_and_registration_distinct(self) -> None:
        cases = (
            (401, {"error_code": "INFERENCE_HOST_AUTHENTICATION_FAILED"}, "source_auth_failed"),
            (403, {"detail": "session_invalid"}, "session_invalid"),
            (403, {"error_code": "PERMISSION_DENIED"}, "permission_denied"),
            (403, {"detail": "access_denied"}, "access_denied"),
            (404, {"detail": "evidence_unregistered"}, "not_registered"),
            (410, {"detail": "context_expired"}, "context_expired"),
            (410, {"detail": "reference_expired"}, "reference_expired"),
            (410, {"detail": "reference_unavailable"}, "reference_unavailable"),
        )
        for status, payload, code in cases:
            self.status, self.body = status, payload
            got_status, headers, _ = self.request()
            self.assertEqual((got_status, headers["X-Evidence-Error"]), (status, code), code)

    def test_missing_local_media_differs_from_unregistered_evidence(self) -> None:
        self.status, self.body = 404, {"detail": "evidence_unregistered"}
        self.assertEqual(self.request()[1]["X-Evidence-Error"], "not_registered")
        self.status, self.body = (
            200,
            {
                "reference": "absent.mp4",
                "size": 10,
                "kind": "clip",
                "sha256": "x",
            },
        )
        self.assertEqual(self.request()[1]["X-Evidence-Error"], "media_missing")

    def test_paths_symlinks_changed_size_and_unknown_origin_are_rejected(self) -> None:
        for ref in ("../outside", "/etc/passwd", "a//b", "missing", "a\\b"):
            self.body = {"reference": ref, "size": 10, "kind": "clip", "sha256": "x"}
            status, headers, _ = self.request()
            self.assertEqual((status, headers["X-Evidence-Error"]), (404, "media_missing"), ref)
        link = self.root / "link"
        link.symlink_to(self.media)
        self.body = {"reference": "link", "size": 10, "kind": "clip", "sha256": "x"}
        self.assertEqual(self.request()[0], 404)
        self.body = {"reference": "media.mp4", "size": 99, "kind": "clip", "sha256": "x"}
        self.assertEqual(self.request()[1]["X-Evidence-Error"], "media_changed")
        self.body = {"reference": "media.mp4", "size": 10, "kind": "clip", "sha256": "x"}
        status, headers, _ = self.request(headers={"Origin": "https://evil.example"})
        self.assertEqual(
            (status, headers["X-Evidence-Error"], headers["Cache-Control"]),
            (403, "origin_denied", "no-store"),
        )
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_range_failures_are_bounded(self) -> None:
        self.assertEqual(self.request(headers={"Range": "bytes=20-"})[0], 416)
        self.assertEqual(self.request(headers={"Range": "bytes=1-2,3-4"})[0], 416)
        self.assertEqual(self.request(headers={"Range": "bytes=-0"})[0], 416)
        self.assertEqual(self.request(headers={"Range": f"bytes={'9' * 5000}-"})[0], 416)
        self.media.write_bytes(b"")
        self.body = {"reference": "media.mp4", "size": 0, "kind": "clip", "sha256": "x"}
        self.assertEqual(self.request(headers={"Range": "bytes=0-"})[0], 416)

    def test_invalid_authorization_shape_and_origin_configuration(self) -> None:
        self.body = {"reference": "media.mp4", "size": "ten", "kind": "clip", "sha256": "x"}
        self.assertEqual(self.request()[1]["X-Evidence-Error"], "invalid_authorization")
        for origin in (
            "http://user@web.local",
            "http://web.local/path",
            "http://web.local?x=1",
            "http://web.local#frag",
        ):
            with self.assertRaises(ValueError):
                create_server(
                    address=("127.0.0.1", 0),
                    client=cast(CenterClient, object()),
                    directory=self.root,
                    web_origin=origin,
                )
