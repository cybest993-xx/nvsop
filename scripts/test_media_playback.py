#!/usr/bin/env python3
"""启动隔离的 MediaMTX Compose 夹具并运行真实录像/回放测试。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

PATHS = ("synthetic", "cpu-transcoded")


def free_port(sock_type: socket.SocketKind) -> int:
    with socket.socket(socket.AF_INET, sock_type) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def playback_list_url(playback: str, path: str, start: str, end: str) -> str:
    return f"{playback}/list?{urlencode({'path': path, 'start': start, 'end': end})}"


def playback_get_url(playback: str, path: str, interval: dict[str, Any]) -> str:
    query = {
        "path": path,
        "start": interval["start"],
        "duration": interval["duration"],
        "format": "mp4",
    }
    return f"{playback}/get?{urlencode(query)}"


def read_intervals(url: str) -> list[dict[str, Any]]:
    with urlopen(url, timeout=3) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and item.get("start")]


def wait_for_recordings(
    playback: str, start: str, end: str, timeout: float
) -> dict[str, tuple[str, str]]:
    deadline = time.monotonic() + timeout
    last_error = "recording not finalized yet"
    while time.monotonic() < deadline:
        urls: dict[str, tuple[str, str]] = {}
        try:
            for path in PATHS:
                list_url = playback_list_url(playback, path, start, end)
                intervals = read_intervals(list_url)
                if not intervals:
                    break
                urls[path] = (list_url, playback_get_url(playback, path, intervals[0]))
            if len(urls) == len(PATHS):
                return urls
        except (OSError, HTTPError, URLError, json.JSONDecodeError, KeyError, TypeError) as error:
            last_error = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"MediaMTX recordings were not ready: {last_error}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--startup-timeout", type=float, default=45.0)
    result.add_argument("command", nargs=argparse.REMAINDER)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    if not command:
        print("test_media_playback: provide a test command after --", file=sys.stderr)
        return 2

    compose = [
        "docker",
        "compose",
        "-p",
        f"nvsop-media-test-{os.getpid()}",
        "-f",
        "deploy/media/compose.yaml",
    ]
    compose_environment = os.environ.copy()
    compose_environment.update(
        {
            "NVSOP_MEDIA_RTSP_HOST_PORT": str(free_port(socket.SOCK_STREAM)),
            "NVSOP_MEDIA_WEBRTC_HOST_PORT": str(free_port(socket.SOCK_STREAM)),
            "NVSOP_MEDIA_ICE_HOST_PORT": str(free_port(socket.SOCK_DGRAM)),
            "NVSOP_MEDIA_PLAYBACK_HOST_PORT": str(free_port(socket.SOCK_STREAM)),
        }
    )
    playback = f"http://127.0.0.1:{compose_environment['NVSOP_MEDIA_PLAYBACK_HOST_PORT']}"
    start = (datetime.now(UTC) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    try:
        subprocess.run([*compose, "up", "-d", "--wait"], check=True, env=compose_environment)
        urls = wait_for_recordings(
            playback,
            start,
            (datetime.now(UTC) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            arguments.startup_timeout,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "NVSOP_MEDIA_PLAYBACK_LIST_URL": urls["synthetic"][0],
                "NVSOP_MEDIA_PLAYBACK_GET_URL": urls["synthetic"][1],
                "NVSOP_MEDIA_CPU_PLAYBACK_LIST_URL": urls["cpu-transcoded"][0],
                "NVSOP_MEDIA_CPU_PLAYBACK_GET_URL": urls["cpu-transcoded"][1],
                "NVSOP_MEDIA_EXPECTED_PATH": "synthetic",
                "NVSOP_MEDIA_CPU_EXPECTED_PATH": "cpu-transcoded",
            }
        )
        completed = subprocess.run(command, env=environment, check=False)
        return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"test_media_playback: {error}", file=sys.stderr)
        subprocess.run(
            [*compose, "logs", "--no-color", "--tail", "100"],
            check=False,
            env=compose_environment,
        )
        return 1
    finally:
        subprocess.run(
            [*compose, "down", "-v", "--remove-orphans"],
            check=False,
            env=compose_environment,
        )


if __name__ == "__main__":
    raise SystemExit(main())
