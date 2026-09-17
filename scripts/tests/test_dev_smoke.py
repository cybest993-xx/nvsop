from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "control-api" / "src"))
SPEC = importlib.util.spec_from_file_location("nvsop_dev_smoke", ROOT / "scripts" / "dev_smoke.py")
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"x" * 32) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class DevSmokeTest(unittest.TestCase):
    def test_media_probe_requires_partial_content_and_content_range(self) -> None:
        response = Response(
            206,
            {
                "Content-Range": "bytes 0-31/128",
                "Content-Type": "video/mp4",
                "Content-Length": "32",
            },
        )
        with patch.object(SMOKE, "urlopen", return_value=response):
            result = SMOKE.media_probe(
                "http://localhost:8444/media", "sop_session=token", Path("ca")
            )

        self.assertEqual(
            {
                "status": 206,
                "content_type": "video/mp4",
                "content_range": "bytes 0-31/128",
                "bytes_read": 32,
            },
            result,
        )

    def test_media_probe_rejects_a_full_response_to_a_range_request(self) -> None:
        response = Response(200, {})
        with (
            patch.object(SMOKE, "urlopen", return_value=response),
            self.assertRaises(SMOKE.DatasetImportError),
        ):
            SMOKE.media_probe("http://localhost:8444/media", "sop_session=token", Path("ca"))

    def test_media_probe_rejects_inconsistent_range_semantics(self) -> None:
        cases = (
            ({"Content-Range": "bytes 0-30/128", "Content-Type": "video/mp4"}, b"x" * 31),
            ({"Content-Range": "bytes 0-31/16", "Content-Type": "video/mp4"}, b"x" * 32),
            ({"Content-Range": "bytes 0-31/128", "Content-Type": "video/mp4"}, b"x" * 31),
            (
                {
                    "Content-Range": "bytes 0-31/128",
                    "Content-Type": "video/mp4",
                    "Content-Length": "31",
                },
                b"x" * 32,
            ),
        )
        for headers, body in cases:
            with self.subTest(headers=headers, body_length=len(body)):
                response = Response(206, headers, body)
                with (
                    patch.object(SMOKE, "urlopen", return_value=response),
                    self.assertRaises(SMOKE.DatasetImportError),
                ):
                    SMOKE.media_probe(
                        "http://localhost:8444/media", "sop_session=token", Path("ca")
                    )

    def test_media_probe_rejects_html_partial_content(self) -> None:
        response = Response(
            206,
            {"Content-Range": "bytes 0-31/516", "Content-Type": "text/html"},
        )
        with (
            patch.object(SMOKE, "urlopen", return_value=response),
            self.assertRaises(SMOKE.DatasetImportError),
        ):
            SMOKE.media_probe("http://localhost:8444/media", "sop_session=token", Path("ca"))


if __name__ == "__main__":
    unittest.main()
