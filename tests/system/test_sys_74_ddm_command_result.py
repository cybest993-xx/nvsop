"""S074 — persistent command HTTP, real PG/files/DDM query and readonly Nginx access.

Only preconditions and the synthetic training outcome are arranged locally. Assertions
cross command/result/job HTTP and the deployed gateway; no GPU or checkpoint loading.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client, HTTPError
from sqlalchemy import Engine
from test_sys_machine_identity_over_nginx_https import NGINX_IMAGE, _render_nginx_config

from factory_sop.auth.adapters.current_caller import current_caller_resolver
from factory_sop.dataset.adapters.dependencies import ddm_executor
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.job.adapters.worker import dispatch_ddm_training_job
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
# Reuse S072/S073's synthetic frozen-input arrangement, not another product path.
INTEGRATION = ROOT / "apps/control-api/tests/integration"
if str(INTEGRATION) not in sys.path:
    sys.path.insert(0, str(INTEGRATION))
from test_ddm_dispatch import _ARTIFACT, DdmHttpServer, _write_base_result_files  # noqa: E402
from test_ddm_training_command import _SOURCE_BYTES, _seed_passing_ddm  # noqa: E402


@pytest.fixture
def deployment_environ(deployment_environ: dict[str, str], tmp_path: Path) -> dict[str, str]:
    return {
        **deployment_environ,
        "SOP_ANNOTATION_BACKEND_URL": "http://127.0.0.1:9",
        "SOP_ANNOTATION_MEDIA_ORIGIN": "https://localhost:8444",
        "SOP_ANNOTATION_DATA_ROOT": str(tmp_path / "annotation"),
    }


@contextmanager
def _result_gateway(center: Client, tmp_path: Path, objects: Path) -> Iterator[tuple[Client, str]]:
    config, tls, control_port = _render_nginx_config(center, tmp_path, gateway_scheme="https")
    assert tls is not None
    ports = re.findall(r"listen (\d+) ssl", config.read_text())
    assert len(ports) == 2
    media_url = f"https://127.0.0.1:{ports[1]}"
    docker = shutil.which("docker")
    assert docker is not None
    name = f"nvsop-ddm-results-{uuid4().hex[:12]}"
    process = subprocess.Popen(
        [
            docker,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "host",
            "-v",
            f"{config}:/etc/nginx/conf.d/default.conf:ro",
            "-v",
            f"{tls}:/etc/nginx/tls:ro",
            "-v",
            f"{objects}:/var/lib/nvsop/dataset-files:ro",
            NGINX_IMAGE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with Client(
            base_url=f"https://127.0.0.1:{control_port}", verify=False, trust_env=False, timeout=3
        ) as gateway:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise AssertionError(f"Nginx exited: {stdout}\n{stderr}")
                try:
                    if gateway.get("/api/v1/liveness").status_code == 200:
                        break
                except HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Nginx did not start within 30 seconds")
            yield gateway, media_url
    finally:
        subprocess.run(
            [docker, "stop", "-t", "1", name], check=False, capture_output=True, timeout=10
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_sys_74_persistent_command_and_registered_result_access(
    client: Client,
    bootstrap: BootstrapCommand,
    engine: Engine,
    deployment_environ: dict[str, str],
    tmp_path: Path,
) -> None:
    log_in(client, bootstrap)
    objects = Path(deployment_environ["SOP_DATASET_STORAGE_ROOT"])
    storage = LocalFileObjectStorage(objects)
    with storage.writing(object_key="datasets/source-a.mp4") as sink:
        sink.write(_SOURCE_BYTES)
    inputs = []
    with session_factory(engine).begin() as session:
        for role in ("training", "validation"):
            dataset_id = uuid4()
            check_id, artifact_id = _seed_passing_ddm(
                session,
                label=role,
                dataset_id=dataset_id,
                member_id=uuid4(),
                context_id=uuid4(),
                submission_id=uuid4(),
                execution_id=uuid4(),
                now=datetime.now(UTC),
                artifact_content=_ARTIFACT,
            )
            with storage.writing(object_key=f"artifacts/{role}.json") as sink:
                sink.write(_ARTIFACT)
            inputs.append(
                {
                    "dataset_id": str(dataset_id),
                    "usage_check_id": str(check_id),
                    "artifact_id": str(artifact_id),
                }
            )
    path = f"/api/v1/training-datasets/{inputs[0]['dataset_id']}/ddm-training-commands"
    body = {"training": inputs[0], "validation": inputs[1]}
    headers = {**csrf_header(client), "Idempotency-Key": "sys-74"}
    mismatch = client.post(
        path.replace(inputs[0]["dataset_id"], inputs[1]["dataset_id"]), json=body, headers=headers
    )
    assert mismatch.status_code == 409, mismatch.text
    accepted = client.post(path, json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    command_id = accepted.json()["command"]["id"]
    job_id = accepted.json()["job"]["id"]
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["command"]["id"] == command_id

    dataset_root, results_root = tmp_path / "ddm-inputs", tmp_path / "ddm-results"
    config = tmp_path / "ddm_train_config.yaml"
    config.write_bytes(
        (
            ROOT
            / "vendor"
            / "sop-monitoring-blueprints"
            / "microservices"
            / "sop-training-bp"
            / "assets"
            / "config"
            / "ddm_train_config.yaml"
        ).read_bytes()
    )
    with DdmHttpServer() as server:
        settings = Settings.from_environment(
            {
                **deployment_environ,
                "SOP_DDM_TRAINING_BACKEND_URL": server.url,
                "SOP_DDM_TRAINING_DATASET_ROOT": str(dataset_root),
                "SOP_DDM_TRAINING_RESULTS_ROOT": str(results_root),
                "SOP_DDM_TRAINING_CONFIG_PATH": str(config),
            }
        )
        factory = session_factory(engine)
        ctx = {
            "session_factory": factory,
            "ddm_executor": ddm_executor(settings, factory),
            "current_caller_resolver": current_caller_resolver(factory),
            "blocking_job_slots": asyncio.Semaphore(1),
        }
        asyncio.run(dispatch_ddm_training_job(ctx, job_id))
        bound = client.get(f"{path}/{command_id}").json()
        assert bound["status"] == "bound", bound
        base_id = bound["base_job_id"]
        assert base_id is not None
        assert len(server.starts) == 1
        server.jobs[base_id].status = "cancelled"
        asyncio.run(dispatch_ddm_training_job(ctx, job_id))
        cancelled = client.get(f"{path}/{command_id}").json()
        assert cancelled["base_status"] == "cancelled", cancelled
        assert cancelled["status"] == "bound"
        assert cancelled["observation_failure_code"] == "BASE_CANCELLATION_UNVERIFIED"
        server.jobs[base_id].status = "running"
        server.jobs[base_id].progress = 50.0
        server.jobs[base_id].current_step = 5
        asyncio.run(dispatch_ddm_training_job(ctx, job_id))
        running = client.get(f"{path}/{command_id}").json()
        assert running["base_progress"] == 50.0
        assert running["base_current_step"] == 5
        assert "backend_target" not in running
        server.query_error = True
        asyncio.run(dispatch_ddm_training_job(ctx, job_id))
        unavailable = client.get(f"{path}/{command_id}").json()
        assert unavailable["base_status"] == "running"
        assert unavailable["last_observed_at"] == running["last_observed_at"]
        assert unavailable["observation_failure_code"] == "BASE_OBSERVATION_UNAVAILABLE"
        assert "HTTP 503" in unavailable["observation_failure_detail"]
        assert client.get("/api/v1/templates/versions").status_code == 200
        server.query_error = False
        _write_base_result_files(
            results_root=results_root,
            dataset_root=dataset_root,
            base_job_id=base_id,
            training_dataset_id=f"ddm-{command_id}-training",
            validation_dataset_id=f"ddm-{command_id}-validation",
        )
        server.jobs[base_id].status = "completed"
        asyncio.run(dispatch_ddm_training_job(ctx, job_id))
        detail = client.get(f"{path}/{command_id}").json()
        assert detail["status"] == "completed", detail
        assert detail["result_status"] == "registered"
        assert detail["result_generation"] == 1
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "succeeded"
        assert len(server.starts) == 1
    # A fresh HTTP client models reopening the page, not an in-memory command cache.
    with Client(
        base_url=client.base_url, cookies=client.cookies, verify=False, trust_env=False
    ) as reopened:
        listed = reopened.get(path)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == command_id
        assert reopened.get(f"{path}/{command_id}").json()["status"] == "completed"
        result = reopened.get(f"{path}/{command_id}/result").json()
        assert result["access"]["checkpoint_url"].endswith(f"/ddm-results/{command_id}/checkpoint")
        assert "object_key" not in str(result)

    with _result_gateway(client, tmp_path, objects) as (gateway, media_url):
        checkpoint_url = f"{media_url}/ddm-results/{command_id}/checkpoint"
        assert gateway.get(checkpoint_url).status_code == 401
        gateway.cookies.update(client.cookies)
        downloaded = gateway.get(
            checkpoint_url, headers={"X-DDM-Result-Object": "datasets/source-a.mp4"}
        )
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == b"real-checkpoint-bytes"
        assert downloaded.headers["Cache-Control"] == "no-store"
        assert "X-DDM-Result-Object" not in downloaded.headers
        ranged = gateway.get(checkpoint_url, headers={"Range": "bytes=0-3"})
        assert ranged.status_code == 206
        assert ranged.content == b"real"
        assert gateway.get(f"{media_url}/ddm-results/{uuid4()}/checkpoint").status_code == 403
        assert gateway.get(f"{media_url}/ddm-results/{command_id}/config.yaml").status_code == 404
        assert (
            gateway.get(
                f"{media_url}/ddm-results/{command_id}/../../datasets/source-a.mp4"
            ).status_code
            == 404
        )
        # Revoke the current account over the published auth API; old cookies cannot read bytes.
        revoked = client.delete("/api/v1/auth/session", headers=csrf_header(client))
        assert revoked.status_code == 204, revoked.text
        assert gateway.get(checkpoint_url).status_code == 401
        assert client.get(path).status_code == 401
