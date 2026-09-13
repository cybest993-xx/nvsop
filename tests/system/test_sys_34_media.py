"""Issue #34 的真实 MediaMTX 证据；此处不接受伪造服务。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest

_PLAYBACK_LIST_URL = os.environ.get("NVSOP_MEDIA_PLAYBACK_LIST_URL")
_PLAYBACK_GET_URL = os.environ.get("NVSOP_MEDIA_PLAYBACK_GET_URL")
_CPU_PLAYBACK_LIST_URL = os.environ.get("NVSOP_MEDIA_CPU_PLAYBACK_LIST_URL")
_CPU_PLAYBACK_GET_URL = os.environ.get("NVSOP_MEDIA_CPU_PLAYBACK_GET_URL")
_EXPECTED_PATH = os.environ.get("NVSOP_MEDIA_EXPECTED_PATH", "")
_CPU_EXPECTED_PATH = os.environ.get("NVSOP_MEDIA_CPU_EXPECTED_PATH", "cpu-transcoded")


def _assert_playable_video(url: str) -> None:
    request = Request(url, headers={"Accept": "video/mp4"})
    with urlopen(request, timeout=20) as response:
        assert response.headers.get_content_type() == "video/mp4"
        with tempfile.NamedTemporaryFile(suffix=".mp4") as sample_file:
            sample_file.write(response.read())
            sample_file.flush()
            probe = subprocess.run(
                [
                    os.environ.get("NVSOP_FFPROBE", "ffprobe"),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    sample_file.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    streams = json.loads(probe.stdout).get("streams", [])
    assert any(stream.get("codec_type") == "video" for stream in streams)


@pytest.mark.skipif(
    _PLAYBACK_LIST_URL is None,
    reason="requires a live MediaMTX playback /list URL",
)
def test_live_mediamtx_returns_intervals_for_the_expected_stable_path() -> None:
    assert _PLAYBACK_LIST_URL is not None
    request = Request(_PLAYBACK_LIST_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    assert isinstance(payload, list)
    assert payload
    assert all(
        isinstance(item, dict)
        and isinstance(item.get("start"), str)
        and isinstance(item.get("duration"), (int, float))
        and item["duration"] >= 0
        for item in payload
    )
    if _EXPECTED_PATH:
        assert parse_qs(urlsplit(_PLAYBACK_LIST_URL).query).get("path") == [_EXPECTED_PATH]


@pytest.mark.skipif(
    _PLAYBACK_GET_URL is None,
    reason="requires a live MediaMTX playback /get URL",
)
def test_live_mediamtx_get_returns_playable_video() -> None:
    assert _PLAYBACK_GET_URL is not None
    _assert_playable_video(_PLAYBACK_GET_URL)


@pytest.mark.skipif(
    _CPU_PLAYBACK_LIST_URL is None,
    reason="requires a live CPU-transcoded MediaMTX playback /list URL",
)
def test_live_mediamtx_cpu_transcode_returns_intervals_for_the_expected_path() -> None:
    assert _CPU_PLAYBACK_LIST_URL is not None
    request = Request(_CPU_PLAYBACK_LIST_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    assert isinstance(payload, list)
    assert payload
    assert all(
        isinstance(item, dict)
        and isinstance(item.get("start"), str)
        and isinstance(item.get("duration"), (int, float))
        and item["duration"] >= 0
        for item in payload
    )
    assert parse_qs(urlsplit(_CPU_PLAYBACK_LIST_URL).query).get("path") == [_CPU_EXPECTED_PATH]


@pytest.mark.skipif(
    _CPU_PLAYBACK_GET_URL is None,
    reason="requires a live CPU-transcoded MediaMTX playback /get URL",
)
def test_live_mediamtx_cpu_transcode_get_returns_playable_video() -> None:
    assert _CPU_PLAYBACK_GET_URL is not None
    _assert_playable_video(_CPU_PLAYBACK_GET_URL)
