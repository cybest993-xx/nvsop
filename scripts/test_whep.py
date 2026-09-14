#!/usr/bin/env python3
"""启动固定 MediaMTX 与合成 RTSP 源，为 WHEP 浏览器测试提供真实协议端点。"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

MEDIA_IMAGE = (
    "bluenviron/mediamtx@sha256:095da39dd94defa496d592e8b7a968bfd171491ea325d5176c6d32929ba4f1f9"
)
DEFAULT_PATH = "nvsop-whep-test"
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
DEFAULT_FPS = 10
DEFAULT_STARTUP_TIMEOUT = 30.0
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._-]+$")


class FixtureError(RuntimeError):
    """WHEP 测试夹具无法安全启动。"""


def _free_port(sock_type: socket.SocketKind) -> int:
    with socket.socket(socket.AF_INET, sock_type) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _config_text(*, path: str, rtsp_port: int, webrtc_port: int, ice_port: int) -> str:
    return f"""webrtcIPsFromInterfaces: false
webrtcAdditionalHosts: [127.0.0.1]
rtspAddress: :{rtsp_port}
webrtcAddress: :{webrtc_port}
webrtcLocalUDPAddress: :{ice_port}
paths:
  {path}:
"""


def _wait_for_http(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1):
                return
        except HTTPError:
            return
        except (OSError, URLError):
            time.sleep(0.25)
    raise FixtureError(f"MediaMTX WebRTC HTTP 端口启动超时：{port}")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover - 目标开发环境是 WSL2
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        process.wait()


def _remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _tail(path: Path, limit: int = 40) -> str:
    if not path.is_file():
        return "<无日志>"
    return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-limit:])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动固定版本 MediaMTX 和合成 RTSP 流，并运行后续 WHEP 测试命令"
    )
    parser.add_argument("--path", default=DEFAULT_PATH, help="MediaMTX 测试 path")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT)
    parser.add_argument("--image", default=MEDIA_IMAGE, help="MediaMTX 镜像或固定 digest")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 后面的测试命令")
    return parser


def _run(arguments: argparse.Namespace) -> int:
    if os.name == "nt":
        raise FixtureError("WHEP 夹具需要 Linux/WSL2 的 host network")
    if not _SAFE_PATH.fullmatch(arguments.path):
        raise FixtureError(f"无效的 MediaMTX path：{arguments.path!r}")
    if min(arguments.width, arguments.height, arguments.fps) <= 0:
        raise FixtureError("width、height 和 fps 必须为正数")
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise FixtureError("必须通过 -- 提供要运行的测试命令")

    rtsp_port = _free_port(socket.SOCK_STREAM)
    webrtc_port = _free_port(socket.SOCK_STREAM)
    ice_port = _free_port(socket.SOCK_DGRAM)
    container = f"nvsop-whep-{os.getpid()}"
    ffmpeg: subprocess.Popen[bytes] | None = None

    with tempfile.TemporaryDirectory(prefix="nvsop-whep-") as directory:
        root = Path(directory)
        config = root / "mediamtx.yml"
        ffmpeg_log = root / "ffmpeg.log"
        config.write_text(
            _config_text(
                path=arguments.path,
                rtsp_port=rtsp_port,
                webrtc_port=webrtc_port,
                ice_port=ice_port,
            ),
            encoding="utf-8",
        )
        _remove_container(container)
        try:
            started = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--detach",
                    "--network",
                    "host",
                    "--name",
                    container,
                    "--volume",
                    f"{config}:/mediamtx.yml:ro",
                    arguments.image,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if started.returncode != 0:
                raise FixtureError(f"MediaMTX 启动失败：{started.stderr.strip()}")
            _wait_for_http(webrtc_port, arguments.startup_timeout)

            ffmpeg = subprocess.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-nostdin",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc2=size={arguments.width}x{arguments.height}:rate={arguments.fps}",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "baseline",
                    "-level",
                    "3.1",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-g",
                    str(arguments.fps),
                    "-keyint_min",
                    str(arguments.fps),
                    "-sc_threshold",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    f"rtsp://127.0.0.1:{rtsp_port}/{arguments.path}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=ffmpeg_log.open("wb"),
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
            time.sleep(1)
            if ffmpeg.poll() is not None:
                raise FixtureError(f"ffmpeg 合成流启动失败：\n{_tail(ffmpeg_log)}")

            media_address = f"http://127.0.0.1:{webrtc_port}"
            environment = os.environ.copy()
            environment.update(
                {
                    "NVSOP_MEDIA_WEBRTC_ADDRESS": media_address,
                    "NVSOP_MEDIA_EXPECTED_PATH": arguments.path,
                }
            )
            environment.setdefault("NVSOP_BASE_URL", media_address)
            print(
                f"WHEP fixture ready: address={media_address} path={arguments.path} "
                f"image={arguments.image}",
                flush=True,
            )
            completed = subprocess.run(command, env=environment, check=False)
            return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
        finally:
            if ffmpeg is not None:
                _terminate(ffmpeg)
            _remove_container(container)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return _run(arguments)
    except KeyboardInterrupt:
        return 130
    except (FixtureError, OSError, subprocess.SubprocessError) as error:
        print(f"test_whep: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
