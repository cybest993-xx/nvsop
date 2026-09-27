"""中心本地训练素材存储 adapter 的行为：流式写入、原子定稿与受限路径。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pydantic import SecretStr

from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.model import ObjectStat
from factory_sop.dataset.storage import ObjectNotFoundError, ObjectStorageUnavailableError
from factory_sop.settings import Settings


def _storage(root: Path) -> LocalFileObjectStorage:
    return LocalFileObjectStorage(root)


def test_writing_finalizes_content_atomically(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    with storage.writing(object_key="training-datasets/a/members/b/attempts/c/video") as sink:
        sink.write(b"first half")
        sink.write(b"second half")

    assert storage.stat(object_key="training-datasets/a/members/b/attempts/c/video") == ObjectStat(
        size=len(b"first halfsecond half")
    )
    destination = BytesIO()
    storage.download_to(
        object_key="training-datasets/a/members/b/attempts/c/video",
        destination=destination,
    )
    assert destination.getvalue() == b"first halfsecond half"


def test_writing_discards_the_object_when_the_caller_raises(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    def _aborted_write() -> None:
        with storage.writing(object_key="training-datasets/a/video") as sink:
            sink.write(b"partial")
            raise RuntimeError("upload aborted")

    with pytest.raises(RuntimeError):
        _aborted_write()

    with pytest.raises(ObjectNotFoundError):
        storage.stat(object_key="training-datasets/a/video")
    assert list((tmp_path / ".incoming").glob("*")) == []


def test_stat_reports_a_missing_object(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    with pytest.raises(ObjectNotFoundError):
        storage.stat(object_key="training-datasets/missing/video")


def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with storage.writing(object_key="training-datasets/a/video") as sink:
        sink.write(b"content")

    storage.delete(object_key="training-datasets/a/video")
    storage.delete(object_key="training-datasets/a/video")

    with pytest.raises(ObjectNotFoundError):
        storage.stat(object_key="training-datasets/a/video")


@pytest.mark.parametrize(
    "object_key",
    ["", "/absolute/video", "../escape/video", "training-datasets/../../escape/video"],
)
def test_object_keys_outside_the_root_are_refused(tmp_path: Path, object_key: str) -> None:
    storage = _storage(tmp_path)

    def _write() -> None:
        with storage.writing(object_key=object_key):
            pass

    with pytest.raises(ObjectStorageUnavailableError):
        _write()


def test_from_settings_refuses_a_missing_root() -> None:
    settings = Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),  # pragma: allowlist secret
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),  # pragma: allowlist secret
    )

    with pytest.raises(ObjectStorageUnavailableError):
        LocalFileObjectStorage.from_settings(settings)
