#!/usr/bin/env python3
"""干净构建标注镜像并验证冻结安装、启动和合成视频切片；不触及运行实例。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx2
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

from annotation_dependencies import BASE_BACKEND, LOCK

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".nvsop/artifacts"


def verify_backend(image: str, directory: Path) -> dict[str, str]:
    password = uuid4().hex
    secret = directory / "training-password"
    secret.write_text(password)
    video = directory / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=24",
            "-t",
            "4",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        timeout=30,
    )
    with Network() as network:
        # 仅为依赖功能测试安装基座自己的 schema，不替代部署角色隔离验收。
        postgres = (
            PostgresContainer("postgres:17.6-alpine", driver="psycopg", password=password)
            .with_network(network)
            .with_network_aliases("annotation-db")
            .with_volume_mapping(
                str(ROOT / BASE_BACKEND / "db-init-scripts/01-init-tables.sql"),
                "/docker-entrypoint-initdb.d/01-init-tables.sql",
                "ro",
            )
        )
        with postgres:
            backend = (
                DockerContainer(image)
                .with_network(network)
                .with_exposed_ports(8100)
                .with_env("POSTGRES_HOST", "annotation-db")
                .with_env("POSTGRES_USER", "test")
                .with_env("POSTGRES_DB", "test")
                .with_volume_mapping(str(secret), "/run/secrets/training-runtime-password", "ro")
            )
            with backend:
                container = backend.get_wrapped_container()
                try:
                    checked = container.exec_run(["python", "-m", "pip", "check"])
                    assert checked.exit_code == 0, checked.output.decode()
                    # pip 的版本提示走 stderr；不能把它拼入机器可读的包清单。
                    installed = container.exec_run(
                        ["python", "-m", "pip", "list", "--format=json"], demux=True
                    )
                    assert installed.exit_code == 0, installed.output
                    inventory, _ = installed.output
                    assert inventory is not None, installed.output
                    packages = {
                        item["name"].lower().replace("_", "-"): item["version"]
                        for item in json.loads(inventory)
                        if item["name"].lower() not in {"pip", "setuptools", "wheel"}
                    }
                    expected = dict(
                        line.lower().split("==", 1)
                        for line in (ROOT / LOCK).read_text().splitlines()
                    )
                    assert packages == expected, (packages, expected)
                    url = (
                        f"http://{backend.get_container_host_ip()}:{backend.get_exposed_port(8100)}"
                    )
                    with httpx2.Client(base_url=url, timeout=30) as client:
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                if client.get("/health/ready").status_code == 200:
                                    break
                            except httpx2.HTTPError:
                                pass
                            if time.monotonic() >= deadline:
                                raise RuntimeError("annotation backend did not become ready")
                            time.sleep(0.2)
                        actions = client.post(
                            "/api/v1/actions/upload",
                            files={
                                "file": (
                                    "actions.json",
                                    b'{"actions":["(1) synthetic"]}',
                                    "application/json",
                                )
                            },
                        )
                        assert actions.status_code == 200, actions.text
                        with video.open("rb") as stream:
                            uploaded = client.post(
                                "/api/v1/upload",
                                files={"file": ("synthetic.mp4", stream, "video/mp4")},
                            )
                        assert uploaded.status_code == 200, uploaded.text
                        video_id = uploaded.json()["file_id"]
                        split = client.post(
                            f"/api/v1/videos/{video_id}/split",
                            json={
                                "timestamps": [
                                    {
                                        "start": 0,
                                        "end": 2,
                                        "actionIndex": 0,
                                        "actionDescription": "(1) synthetic",
                                    }
                                ],
                            },
                        )
                        assert split.status_code == 200, split.text
                        clips = split.json()["clips"]
                        assert len(clips) == 1, split.text
                        downloaded = client.get(f"/api/v1/chunks/{clips[0]['id']}/download")
                        assert downloaded.status_code == 200
                        assert len(downloaded.content) > 0
                    return packages
                finally:
                    (ARTIFACTS / "annotation-runtime.log").write_bytes(container.logs())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-network", choices=("default", "host"), default="default")
    args = parser.parse_args()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    image = f"nvsop-annotation-check:{uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="annotation-image-", dir=ARTIFACTS) as temporary:
        directory = Path(temporary)
        # 构建上下文仅含 Dockerfile 的公开源码输入，排除工作区的本地状态与 secrets。
        context = directory / "context"
        shutil.copytree(ROOT / BASE_BACKEND, context / BASE_BACKEND)
        deploy = context / "deploy/dev"
        deploy.mkdir(parents=True)
        for name in (
            "annotation-backend.Dockerfile",
            "annotation-requirements.lock",
            "annotation-entrypoint.sh",
        ):
            shutil.copyfile(ROOT / "deploy/dev" / name, deploy / name)
        with (ARTIFACTS / "annotation-build.log").open("w") as output:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--no-cache",
                    "--progress=plain",
                    "--platform=linux/amd64",
                    f"--network={args.build_network}",
                    "-f",
                    str(deploy / "annotation-backend.Dockerfile"),
                    "-t",
                    image,
                    str(context),
                ],
                check=True,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=600,
            )
        try:
            packages = verify_backend(image, directory)
            image_id = subprocess.check_output(
                ["docker", "image", "inspect", image, "--format={{.Id}}"], text=True
            ).strip()
            evidence = {
                "candidate_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "base_image": (ROOT / "deploy/dev/annotation-backend.Dockerfile")
                .read_text()
                .splitlines()[0],
                "image_id": image_id,
                "platform": "linux/amd64",
                "runtime_packages": packages,
                "checks": [
                    "pip check",
                    "runtime set equals lock",
                    "ready",
                    "actions upload",
                    "video upload",
                    "split",
                    "download",
                ],
            }
            (ARTIFACTS / "annotation-verification.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )
            print("annotation image: frozen install, startup, upload, split and download passed")
        finally:
            subprocess.run(["docker", "image", "rm", image], check=False, stdout=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
