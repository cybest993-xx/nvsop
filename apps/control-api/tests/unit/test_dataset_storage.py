from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from minio import Minio
from minio.datatypes import PostPolicy
from urllib3.connectionpool import ConnectionPool
from urllib3.exceptions import ReadTimeoutError

from factory_sop.dataset.adapters.storage import MinioObjectStorage
from factory_sop.dataset.storage import ObjectStorageUnavailableError


@dataclass
class RecordingSigner:
    policy: PostPolicy | None = None

    def presigned_post_policy(self, policy: PostPolicy) -> dict[str, str]:
        self.policy = policy
        return {"policy": "signed-policy"}


class TimeoutSigner:
    def presigned_post_policy(self, policy: PostPolicy) -> dict[str, str]:
        del policy
        raise ReadTimeoutError(cast(ConnectionPool, object()), None, "timed out")


def test_post_policy_network_timeout_becomes_a_recoverable_storage_error() -> None:
    storage = MinioObjectStorage(
        client=cast(Minio, object()),
        bucket="training",
        signer=cast(Minio, TimeoutSigner()),
    )

    with pytest.raises(ObjectStorageUnavailableError, match="上传说明"):
        storage.create_upload(
            object_key="training-datasets/dataset/member/attempt/video",
            declared_size=123,
            max_bytes=1024,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )


def test_post_policy_allows_multipart_envelope_without_loosening_declared_size() -> None:
    signer = RecordingSigner()
    storage = MinioObjectStorage(
        client=cast(Minio, object()),
        bucket="training",
        signer=cast(Minio, signer),
        upload_url="https://minio.test/training",
    )

    instructions = storage.create_upload(
        object_key="training-datasets/dataset/member/attempt/video",
        declared_size=123,
        max_bytes=1024,
        expires_at=datetime(2026, 9, 9, 1, 15, tzinfo=UTC),
    )

    assert instructions.url == "https://minio.test/training"
    assert instructions.fields["key"] == "training-datasets/dataset/member/attempt/video"
    assert signer.policy is not None
    assert signer.policy._lower_limit == 123
    assert signer.policy._upper_limit == 123 + 64 * 1024


def test_post_policy_still_rejects_declared_size_outside_deployment_limit() -> None:
    signer = RecordingSigner()
    storage = MinioObjectStorage(
        client=cast(Minio, object()),
        bucket="training",
        signer=cast(Minio, signer),
    )

    with pytest.raises(ObjectStorageUnavailableError, match="上传大小不在部署限制内"):
        storage.create_upload(
            object_key="training-datasets/dataset/member/attempt/video",
            declared_size=1025,
            max_bytes=1024,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
