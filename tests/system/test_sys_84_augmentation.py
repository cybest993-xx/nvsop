"""S084：真实 Center HTTP/PG/outbox、原 NVIDIA handler 与本地真实文件登记。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client
from sqlalchemy import Engine

from factory_sop.auth.adapters.current_caller import current_caller_resolver
from factory_sop.dataset.adapters.dependencies import artifact_executor, augmentation_executor
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.job.adapters.worker import augment_dataset_job
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "apps/control-api/tests/integration"
if str(INTEGRATION) not in sys.path:
    sys.path.insert(0, str(INTEGRATION))
from test_augmentation_command import NvidiaAugmentationHttpServer, _input, _seed  # noqa: E402
from test_ddm_training_command import _SOURCE_BYTES  # noqa: E402


@pytest.fixture
def real_video_bytes(tmp_path: Path) -> bytes:
    """真实 H.264/MP4 合成媒体用于系统级 ffprobe 和不可丢失的对象复制。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.fail("增强系统验收需要 ffmpeg 和 ffprobe")
    output = tmp_path / "augmentation-synthetic.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=24",
            "-t",
            "1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0 or not output.is_file():
        pytest.fail("ffmpeg 无法生成真实增强验收视频")
    return output.read_bytes()


def test_sys_84_authorized_augmentation_registration_and_history(
    client: Client,
    bootstrap: BootstrapCommand,
    engine: Engine,
    deployment_environ: dict[str, str],
    tmp_path: Path,
    real_video_bytes: bytes,
) -> None:
    dataset_id, execution_id = _seed(engine)
    path = f"/api/v1/training-datasets/{dataset_id}/augmentation-commands"
    body = {"annotation_execution_id": str(execution_id), "frame_drop_iterations": 1}
    assert client.post(path, json=body).status_code == 401
    log_in(client, bootstrap)
    assert (
        client.post(path, json=body, headers={"Idempotency-Key": "missing-csrf"}).status_code == 403
    )
    headers = {**csrf_header(client), "Idempotency-Key": "sys-84"}
    accepted = client.post(path, json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    command = accepted.json()
    command_path = f"{path}/{command['id']}"
    assert command["status"] == "pending"
    assert command["base_augmentation_id"] is None
    assert command["result_status"] == "not_registered"
    assert client.post(path, json=body, headers=headers).json()["id"] == command["id"]
    conflict = client.post(path, json={**body, "frame_drop_iterations": 2}, headers=headers)
    assert conflict.status_code == 409, conflict.text
    media_root = tmp_path / "augmentation"
    _input(media_root)
    storage = LocalFileObjectStorage(Path(deployment_environ["SOP_DATASET_STORAGE_ROOT"]))
    with storage.writing(object_key="datasets/source-a.mp4") as target:
        target.write(_SOURCE_BYTES)
    with NvidiaAugmentationHttpServer(media_root) as server:
        settings = Settings.from_environment(
            {
                **deployment_environ,
                "SOP_AUGMENTATION_BACKEND_URL": server.url,
                "SOP_AUGMENTATION_DATA_ROOT": str(media_root),
            }
        )
        factory = session_factory(engine)
        executor = augmentation_executor(settings, factory)
        assert executor is not None
        ctx = {
            "session_factory": factory,
            "augmentation_executor": executor,
            "current_caller_resolver": current_caller_resolver(factory),
            "blocking_job_slots": asyncio.Semaphore(1),
        }
        asyncio.run(augment_dataset_job(ctx, command["job_id"]))
        bound = client.get(command_path).json()
        identity = bound["base_augmentation_id"]
        assert identity == "data-1_augmented_0"
        assert client.post(f"{command_path}/cancel", headers=csrf_header(client)).status_code == 409
        server.rows[identity].status = "completed"
        asyncio.run(augment_dataset_job(ctx, command["job_id"]))
        missing = client.get(command_path).json()
        assert missing["base_observation"]["status"] == "completed"
        assert missing["result_status"] == "not_registered"
        stage = media_root / identity / "bcq"
        (stage / "videos").mkdir(parents=True)
        (stage / "videos/clip.mp4").write_bytes(real_video_bytes)
        (stage / "bcq.json").write_text('[{"video":"videos/clip.mp4","conversations":[]}]')
        asyncio.run(augment_dataset_job(ctx, command["job_id"]))
        complete = client.get(command_path).json()
        assert complete["status"] == "completed"
        assert complete["result_status"] == "registered"
        assert complete["input_snapshot"]["source"] == "integration"
        assert complete["input_snapshot"]["annotation_execution_id"] == str(execution_id)
        assert client.get(f"/api/v1/jobs/{command['job_id']}").json()["status"] == "succeeded"
        assert client.get(path).json()["items"][0]["id"] == command["id"]
        artifact_executor(settings, factory).cleanup_candidates()
        for entry in complete["result_manifest"]["files"]:
            assert storage.stat(object_key=entry["object_key"]).size == entry["size"]
        assert (stage / "videos/clip.mp4").read_bytes() == real_video_bytes
        assert client.get(command_path).json()["result_manifest"] == complete["result_manifest"]
        pending = client.post(
            path, json=body, headers={**headers, "Idempotency-Key": "cancel-me"}
        ).json()
        cancelled = client.post(f"{path}/{pending['id']}/cancel", headers=csrf_header(client))
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        asyncio.run(augment_dataset_job(ctx, pending["job_id"]))
        assert len(server.rows) == 1
